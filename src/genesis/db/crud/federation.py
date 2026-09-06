"""CRUD for the federation relay (v1) — ``federation_contacts`` + the
``federation_messages`` hash-chained transcript.

The persistence layer is deliberately thin: chain math (``payload_hash``,
``seq`` assignment) and crypto live in ``genesis.federation.crypto`` /
``outbound`` / ``inbound``. This module only stores and queries rows — with one
exception that must be atomic: :func:`append_message` advances the contact's
per-direction chain head in the SAME transaction as the message insert.

CONCURRENCY (the PR2 precondition, now MET): :func:`append_message` wraps its
CAS-UPDATE + INSERT in ``SerializedConnection.transaction()``, which holds the
shared connection's lock across the whole ``BEGIN IMMEDIATE`` … COMMIT. That
closes the fork window the per-call lock left open — without it a PEER coroutine
could commit (or roll back) the shared connection's open transaction BETWEEN the
CAS-UPDATE and the INSERT and split the chain (head advanced with no row, or a
row past an unmoved head). The append is therefore CRASH-atomic AND
concurrency-atomic against the other coroutines that share Genesis's single
connection, so PR2's poll-loop and approval-drain can run concurrently with other
writers. (``db`` must be a ``SerializedConnection`` for this — the runtime always
supplies one; a raw ``aiosqlite.Connection`` has no ``transaction()``.)

Row shape is a plain ``dict`` (``aiosqlite.Row`` → ``dict``); BLOB columns come
back as ``bytes``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from genesis.db.connection import SerializedConnection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------

# The closed contact-state set (matches the CHECK constraint in the DDL).
_CONTACT_STATES = ("pending", "active", "revoked")

# States a contact can never LEAVE — revocation is the owner's final decision.
# set_contact_state refuses to transition out of these so a stale activation
# worker racing the revoke can't resurrect the contact (mirrors
# _TERMINAL_HITL_STATES on the message side).
_TERMINAL_CONTACT_STATES = ("revoked",)


def _require_contact_state(state: str) -> None:
    if state not in _CONTACT_STATES:
        raise ValueError(f"contact state must be one of {_CONTACT_STATES}, got {state!r}")


async def create_contact(
    db: aiosqlite.Connection,
    *,
    contact_id: str,
    display_name: str,
    peer_ed25519_pub: bytes,
    peer_x25519_pub: bytes,
    local_mailbox_id: str,
    peer_mailbox_id: str,
    relay_url: str,
    paired_at: str,
    peer_write_cap_enc: bytes | None = None,
    state: str = "pending",
) -> str:
    """Insert a paired-peer row. ``peer_write_cap_enc`` is SecretBox-sealed by
    the caller (never a plaintext cap). Returns ``contact_id``."""
    _require_contact_state(state)  # typo fails loud, not as a downstream IntegrityError
    await db.execute(
        """INSERT INTO federation_contacts
             (contact_id, display_name, peer_ed25519_pub, peer_x25519_pub,
              local_mailbox_id, peer_mailbox_id, relay_url, peer_write_cap_enc,
              state, paired_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            contact_id,
            display_name,
            peer_ed25519_pub,
            peer_x25519_pub,
            local_mailbox_id,
            peer_mailbox_id,
            relay_url,
            peer_write_cap_enc,
            state,
            paired_at,
        ),
    )
    await db.commit()
    return contact_id


async def get_contact(db: aiosqlite.Connection, contact_id: str) -> dict | None:
    """Fetch one contact by id, or None."""
    cursor = await db.execute(
        "SELECT * FROM federation_contacts WHERE contact_id = ?", (contact_id,)
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_contacts(db: aiosqlite.Connection, *, state: str | None = None) -> list[dict]:
    """All contacts, or only those in ``state`` (pending|active|revoked)."""
    if state is None:
        cursor = await db.execute("SELECT * FROM federation_contacts ORDER BY paired_at DESC")
    else:
        cursor = await db.execute(
            "SELECT * FROM federation_contacts WHERE state = ? ORDER BY paired_at DESC",
            (state,),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def set_contact_state(
    db: aiosqlite.Connection,
    contact_id: str,
    *,
    state: str,
    expected_current: str | None = None,
    revoked_at: str | None = None,
) -> bool:
    """Transition a contact's state (pending→active, →revoked). Returns whether
    a row changed. ``revoked_at`` is stamped when moving to revoked (and never
    cleared by a later transition — COALESCE keeps the existing stamp).

    Mirrors :func:`set_hitl_state`'s race-safety guards:
    - ``revoked`` is TERMINAL: a revoked contact can never be transitioned
      again, so a stale activation worker racing the owner's revoke can't
      resurrect the contact or clear ``revoked_at`` — whichever call commits
      last would otherwise erase the owner's decision;
    - ``expected_current`` (optional) makes the update a compare-and-swap: it
      only applies when the row is still in that state, so two workers racing
      the same transition don't both "succeed".

    Both ``state`` and ``expected_current`` are validated against the closed
    contact-state set — a typo fails loud rather than creating an un-enumerable
    state (or a CAS that can never match; note ``expected_current="revoked"``
    passes validation but can never match, because the terminal guard excludes
    revoked rows from the UPDATE entirely).

    ``revoked_at`` travels ONLY with the revoke transition, and is REQUIRED
    there: a revoke without a stamp would be permanently un-stampable (the
    terminal guard refuses any later re-stamp), and a stamp on a non-revoke
    transition would mint an active contact carrying a revocation timestamp."""
    _require_contact_state(state)
    if (state == "revoked") != (revoked_at is not None):
        raise ValueError(
            "revoked_at is required when (and only when) transitioning to "
            f"'revoked' — got state={state!r}, revoked_at={revoked_at!r}"
        )
    conds = [
        "contact_id = ?",
        f"state NOT IN ({', '.join('?' for _ in _TERMINAL_CONTACT_STATES)})",
    ]
    params: list = [contact_id, *_TERMINAL_CONTACT_STATES]
    if expected_current is not None:
        _require_contact_state(expected_current)
        conds.append("state = ?")
        params.append(expected_current)
    cursor = await db.execute(
        "UPDATE federation_contacts SET state = ?, revoked_at = COALESCE(?, revoked_at) "
        f"WHERE {' AND '.join(conds)}",
        (state, revoked_at, *params),
    )
    await db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# messages (hash-chained transcript)
# ---------------------------------------------------------------------------

_DIRECTIONS = ("in", "out")

# The closed HITL-state set (matches the CHECK constraint in the DDL).
_HITL_STATES = ("proposed", "held", "approved", "sent", "rejected", "quarantined", "received")

# HITL states whose message body is safe to prune. A NON-terminal outbound
# message (proposed/held/approved-but-undelivered) still needs its plaintext/
# ciphertext to be sent once the owner approves — pruning it would lose the
# payload permanently. Only genuinely-done rows are prunable.
_PRUNABLE_HITL_STATES = ("sent", "rejected", "quarantined", "received")

# HITL states a message can never LEAVE — the owner's decision (rejected) and a
# completed delivery (sent) are final. set_hitl_state refuses to transition out
# of these so a racing worker can't resurrect a rejected message or unsend one.
_TERMINAL_HITL_STATES = ("sent", "rejected")

# Peer-sourced content is untrusted. Every inbound row is forced to this
# provenance so a downstream memory write is quarantined (wrapped as external),
# never resolved to first_party. MUST equal
# genesis.memory.provenance.ORIGIN_EXTERNAL_UNTRUSTED (locked by a test) — kept
# as a literal here to avoid the db-layer importing the memory layer upward.
_INBOUND_ORIGIN_CLASS = "external_untrusted"


def _require_direction(direction: str) -> None:
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {_DIRECTIONS}, got {direction!r}")


def _require_hitl_state(state: str) -> None:
    if state not in _HITL_STATES:
        raise ValueError(f"hitl_state must be one of {_HITL_STATES}, got {state!r}")


async def append_message(
    db: SerializedConnection,
    *,
    msg_id: str,
    contact_id: str,
    direction: str,
    seq: int,
    payload_hash: str,
    created_at: str,
    hitl_state: str,
    prev_hash: str | None = None,
    nonce: bytes | None = None,
    ciphertext: bytes | None = None,
    plaintext: str | None = None,
    origin_class: str | None = None,
    source_pipeline: str | None = None,
    approval_id: str | None = None,
    delivered_at: str | None = None,
) -> str:
    """Append a transcript message AND advance the contact's per-direction chain
    head via a **compare-and-swap**, in one transaction (single commit).
    CRASH-atomic AND concurrency-atomic: the CAS+INSERT run inside
    ``db.transaction()``, which holds the shared connection's lock across the
    whole ``BEGIN IMMEDIATE`` … COMMIT, so no peer coroutine can commit/roll back
    between the two statements and fork the chain (see the module docstring).

    The head UPDATE is a CAS: it moves ``last_seq_{sent,recv}`` + ``{send,recv}_
    chain_head`` to ``(seq, payload_hash)`` ONLY when the stored tip currently
    equals ``(seq - 1, prev_hash)`` — i.e. this message links strictly onto the
    current tip. A zero-row result raises ``ValueError`` and rolls the whole thing
    back, so an unknown contact, an out-of-order append (seq 2 before seq 1), or a
    stale-tip writer can NEVER move the head backward or attach a row to the wrong
    predecessor — which ``UNIQUE(contact,direction,seq)`` alone does not prevent
    (it only rejects a duplicate seq). Genesis (first message) is ``seq = 1``,
    ``prev_hash = None``, matched against the fresh contact's ``(0, NULL)`` tip.
    The CAS also requires the contact to be ``state='active'`` — a message can't
    land on a pending or revoked contact, so owner revocation that commits before
    an in-flight worker's append wins the race — and it subsumes the
    unknown-contact check (no matching row → 0 rows), uniformly regardless of
    ``PRAGMA foreign_keys``.

    ``origin_class`` is caller-controlled for OUTBOUND rows only: on
    ``direction="in"`` it is unconditionally overridden to the quarantine
    provenance (``external_untrusted``), whatever the caller passed.
    """
    _require_direction(direction)
    # Inbound peer content is untrusted — force the quarantine provenance
    # UNCONDITIONALLY (not just when the caller omits it): peer-sourced content
    # must never be stored as trusted, so an explicit origin_class on an inbound
    # append — including 'first_party' — is overridden, not respected. Without
    # this, a later memory write would resolve to first_party and lose the
    # injection boundary. Outbound (owner-authored) values pass through.
    if direction == "in":
        if origin_class not in (None, _INBOUND_ORIGIN_CLASS):
            # Surface the confused/influenced caller this override defends
            # against — the row is still quarantined either way.
            logger.warning(
                "append_message: inbound origin_class %r overridden to %r "
                "(contact %s, msg %s) — inbound provenance is not caller-controlled",
                origin_class,
                _INBOUND_ORIGIN_CLASS,
                contact_id,
                msg_id,
            )
        origin_class = _INBOUND_ORIGIN_CLASS
    col_seq = "last_seq_sent" if direction == "out" else "last_seq_recv"
    col_head = "send_chain_head" if direction == "out" else "recv_chain_head"
    # Atomic on the shared connection: transaction() holds the lock across the
    # whole BEGIN IMMEDIATE … COMMIT, commits on clean exit and ROLLS BACK on any
    # exception (the CAS ValueError and INSERT IntegrityError included) — so the
    # CAS-UPDATE and the INSERT land together or not at all, and no peer coroutine
    # interleaves between them.
    async with db.transaction():
        # CAS: only advance if the stored tip is exactly (seq-1, prev_hash).
        # `IS ?` compares NULL correctly (genesis prev_hash=None ↔ head IS NULL).
        cur = await db.execute(
            f"UPDATE federation_contacts SET {col_seq} = ?, {col_head} = ? "
            f"WHERE contact_id = ? AND state = 'active' "
            f"AND {col_seq} = ? AND {col_head} IS ?",
            (seq, payload_hash, contact_id, seq - 1, prev_hash),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"append_message: chain-tip CAS failed for contact {contact_id!r} "
                f"direction={direction} (expected prior seq={seq - 1}, "
                f"prev_hash={prev_hash!r}) — unknown/inactive/revoked contact, "
                "out-of-order, or fork"
            )
        await db.execute(
            """INSERT INTO federation_messages
                 (msg_id, contact_id, direction, seq, prev_hash, payload_hash,
                  nonce, ciphertext, plaintext, origin_class, source_pipeline,
                  hitl_state, approval_id, created_at, delivered_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_id,
                contact_id,
                direction,
                seq,
                prev_hash,
                payload_hash,
                nonce,
                ciphertext,
                plaintext,
                origin_class,
                source_pipeline,
                hitl_state,
                approval_id,
                created_at,
                delivered_at,
            ),
        )
    return msg_id


async def chain_tip(
    db: aiosqlite.Connection, contact_id: str, *, direction: str
) -> tuple[int, str | None]:
    """Return ``(last_seq, chain_head)`` for the contact's direction, read from
    the contact row (the atomic head). ``(0, None)`` if the contact is unknown or
    the chain is empty — the correct genesis state for a first message."""
    _require_direction(direction)  # never silently alias a typo to the inbound chain
    col_seq = "last_seq_sent" if direction == "out" else "last_seq_recv"
    col_head = "send_chain_head" if direction == "out" else "recv_chain_head"
    cursor = await db.execute(
        f"SELECT {col_seq} AS seq, {col_head} AS head FROM federation_contacts "
        "WHERE contact_id = ?",
        (contact_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return (0, None)
    return (row["seq"] or 0, row["head"])


async def get_message(db: aiosqlite.Connection, msg_id: str) -> dict | None:
    """Fetch one transcript message by id, or None."""
    cursor = await db.execute("SELECT * FROM federation_messages WHERE msg_id = ?", (msg_id,))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def list_messages(
    db: aiosqlite.Connection,
    contact_id: str,
    *,
    direction: str | None = None,
    after_msg_id: str = "",
    limit: int = 200,
) -> list[dict]:
    """Transcript for a contact in CHRONOLOGICAL order (by ``msg_id``, a
    time-sortable ULID), optionally one direction.

    Ordering by ``msg_id`` rather than ``seq`` is what keeps the combined
    (both-direction) view coherent: the inbound and outbound ``seq`` sequences are
    INDEPENDENT, so a seq-sort interleaves them wrong (outbound 1,2 + inbound 1 →
    out-1, in-1, out-2). Within a single direction the ULID order still matches the
    append (chain) order.

    Paginate with ``after_msg_id``: pass the ``msg_id`` of the last row you saw to
    get the next window (rows with ``msg_id > after_msg_id``). Without it, rows
    past ``limit`` would be permanently unreachable by audit/display consumers."""
    if direction is not None:
        _require_direction(direction)
    where = "contact_id = ? AND msg_id > ?"
    params: list = [contact_id, after_msg_id]
    if direction is not None:
        where += " AND direction = ?"
        params.append(direction)
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM federation_messages WHERE {where} ORDER BY msg_id ASC LIMIT ?",
        tuple(params),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def set_hitl_state(
    db: aiosqlite.Connection,
    msg_id: str,
    *,
    hitl_state: str,
    expected_current: str | None = None,
    delivered_at: str | None = None,
) -> bool:
    """Transition an outbound message's HITL state (held→approved→sent, or
    →rejected). When ``delivered_at`` is given it is stamped too. Returns whether a
    row changed.

    Two guards make this race-safe for PR2's approval gate:
    - a TERMINAL row (``sent``/``rejected``) can never be transitioned again, so a
      stale send-worker can't resurrect a message the owner rejected, nor unsend a
      delivered one — whichever call commits last would otherwise erase the
      owner's decision;
    - ``expected_current`` (optional) makes the update a compare-and-swap: it only
      applies when the row is still in that state, so two workers racing the same
      transition don't both "succeed".

    Both states are validated against the closed HITL set — a typo'd
    ``expected_current`` would otherwise silently produce a CAS that can never
    match (returning False forever, indistinguishable from losing the race)."""
    _require_hitl_state(hitl_state)
    conds = ["msg_id = ?", f"hitl_state NOT IN ({', '.join('?' for _ in _TERMINAL_HITL_STATES)})"]
    params: list = [msg_id, *_TERMINAL_HITL_STATES]
    if expected_current is not None:
        _require_hitl_state(expected_current)
        conds.append("hitl_state = ?")
        params.append(expected_current)
    cursor = await db.execute(
        "UPDATE federation_messages SET hitl_state = ?, delivered_at = COALESCE(?, delivered_at) "
        f"WHERE {' AND '.join(conds)}",
        (hitl_state, delivered_at, *params),
    )
    await db.commit()
    return cursor.rowcount > 0


async def prune_bodies(db: aiosqlite.Connection, *, before: str) -> int:
    """Retention: null the prunable bodies (plaintext/ciphertext/nonce) of
    messages created strictly before ``before`` (ISO ts), PRESERVING the chain
    skeleton (seq/prev_hash/payload_hash) so a pruned transcript still verifies.

    Only messages whose body is done being needed (``_PRUNABLE_HITL_STATES`` —
    sent/rejected/quarantined/received; a superset of the two truly-terminal
    states) are pruned — a ``held``/``approved``-but-undelivered outbound still needs its
    payload to be sent once the owner approves, so pruning it (even when old)
    would lose the message permanently. Rows are never deleted — a gap would break
    verification. Returns the number of rows pruned. Idempotent."""
    placeholders = ", ".join("?" for _ in _PRUNABLE_HITL_STATES)
    cursor = await db.execute(
        "UPDATE federation_messages "
        "SET plaintext = NULL, ciphertext = NULL, nonce = NULL "
        f"WHERE created_at < ? AND hitl_state IN ({placeholders}) "
        "AND (plaintext IS NOT NULL OR ciphertext IS NOT NULL OR nonce IS NOT NULL)",
        (before, *_PRUNABLE_HITL_STATES),
    )
    await db.commit()
    return cursor.rowcount
