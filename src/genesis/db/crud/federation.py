"""CRUD for the federation relay (v1) — ``federation_contacts`` + the
``federation_messages`` hash-chained transcript.

The persistence layer is deliberately thin: chain math (``payload_hash``,
``seq`` assignment) and crypto live in ``genesis.federation.crypto`` /
``outbound`` / ``inbound``. This module only stores and queries rows — with one
exception that MUST be atomic: :func:`append_message` advances the contact's
per-direction chain head in the SAME transaction as the message insert, so a
crash can never leave the head pointing at a message that was not written (or a
message written past a head that never moved) — either would fork the chain and
break verification.

Row shape is a plain ``dict`` (``aiosqlite.Row`` → ``dict``); BLOB columns come
back as ``bytes``.
"""

from __future__ import annotations

import aiosqlite

# ---------------------------------------------------------------------------
# contacts
# ---------------------------------------------------------------------------


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
    revoked_at: str | None = None,
) -> bool:
    """Transition a contact's state (e.g. pending→active, →revoked). Returns
    whether a row changed. ``revoked_at`` is stamped when moving to revoked."""
    cursor = await db.execute(
        "UPDATE federation_contacts SET state = ?, revoked_at = ? WHERE contact_id = ?",
        (state, revoked_at, contact_id),
    )
    await db.commit()
    return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# messages (hash-chained transcript)
# ---------------------------------------------------------------------------

_DIRECTIONS = ("in", "out")

# HITL states whose message body is safe to prune. A NON-terminal outbound
# message (proposed/held/approved-but-undelivered) still needs its plaintext/
# ciphertext to be sent once the owner approves — pruning it would lose the
# payload permanently. Only genuinely-done rows are prunable.
_PRUNABLE_HITL_STATES = ("sent", "rejected", "quarantined", "received")


def _require_direction(direction: str) -> None:
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction must be one of {_DIRECTIONS}, got {direction!r}")


async def append_message(
    db: aiosqlite.Connection,
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
    head via a **compare-and-swap**, atomically (single commit).

    The head UPDATE is a CAS: it moves ``last_seq_{sent,recv}`` + ``{send,recv}_
    chain_head`` to ``(seq, payload_hash)`` ONLY when the stored tip currently
    equals ``(seq - 1, prev_hash)`` — i.e. this message links strictly onto the
    current tip. A zero-row result raises ``ValueError`` and rolls the whole thing
    back, so an unknown contact, an out-of-order append (seq 2 before seq 1), or a
    stale-tip writer can NEVER move the head backward or attach a row to the wrong
    predecessor — which ``UNIQUE(contact,direction,seq)`` alone does not prevent
    (it only rejects a duplicate seq). Genesis (first message) is ``seq = 1``,
    ``prev_hash = None``, matched against the fresh contact's ``(0, NULL)`` tip.
    The CAS also subsumes the unknown-contact check (no matching row → 0 rows),
    uniformly regardless of ``PRAGMA foreign_keys``.
    """
    _require_direction(direction)
    col_seq = "last_seq_sent" if direction == "out" else "last_seq_recv"
    col_head = "send_chain_head" if direction == "out" else "recv_chain_head"
    try:
        # CAS: only advance if the stored tip is exactly (seq-1, prev_hash).
        # `IS ?` compares NULL correctly (genesis prev_hash=None ↔ head IS NULL).
        cur = await db.execute(
            f"UPDATE federation_contacts SET {col_seq} = ?, {col_head} = ? "
            f"WHERE contact_id = ? AND {col_seq} = ? AND {col_head} IS ?",
            (seq, payload_hash, contact_id, seq - 1, prev_hash),
        )
        if cur.rowcount == 0:
            raise ValueError(
                f"append_message: chain-tip CAS failed for contact {contact_id!r} "
                f"direction={direction} (expected prior seq={seq - 1}, "
                f"prev_hash={prev_hash!r}) — unknown contact, out-of-order, or fork"
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
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
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
    after_seq: int = 0,
    limit: int = 200,
) -> list[dict]:
    """Transcript for a contact in chain order (seq ASC), optionally one
    direction. ``msg_id`` (ULID) breaks ties deterministically.

    Paginate with ``after_seq``: pass the ``seq`` of the last row you saw to get
    the next window (rows with ``seq > after_seq``). Without it, rows past
    ``limit`` would be permanently unreachable — a long transcript would hide its
    newest entries from audit/display consumers."""
    if direction is not None:
        _require_direction(direction)
    where = "contact_id = ? AND seq > ?"
    params: list = [contact_id, after_seq]
    if direction is not None:
        where += " AND direction = ?"
        params.append(direction)
    params.append(limit)
    cursor = await db.execute(
        f"SELECT * FROM federation_messages WHERE {where} ORDER BY seq ASC, msg_id ASC LIMIT ?",
        tuple(params),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def set_hitl_state(
    db: aiosqlite.Connection,
    msg_id: str,
    *,
    hitl_state: str,
    delivered_at: str | None = None,
) -> bool:
    """Transition an outbound message's HITL state (held→approved→sent, or
    →rejected). When ``delivered_at`` is given it is stamped too. Returns whether
    a row changed."""
    cursor = await db.execute(
        "UPDATE federation_messages SET hitl_state = ?, "
        "delivered_at = COALESCE(?, delivered_at) WHERE msg_id = ?",
        (hitl_state, delivered_at, msg_id),
    )
    await db.commit()
    return cursor.rowcount > 0


async def prune_bodies(db: aiosqlite.Connection, *, before: str) -> int:
    """Retention: null the prunable bodies (plaintext/ciphertext/nonce) of
    messages created strictly before ``before`` (ISO ts), PRESERVING the chain
    skeleton (seq/prev_hash/payload_hash) so a pruned transcript still verifies.

    Only messages in a TERMINAL hitl_state (``_PRUNABLE_HITL_STATES``) are pruned
    — a ``held``/``approved``-but-undelivered outbound message still needs its
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
