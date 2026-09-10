"""Tests for the federation relay CRUD (federation_contacts + the
federation_messages hash-chained transcript). Exercises the real
``create_all_tables`` build path, so it also validates the migration-0090 DDL."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from genesis.db.connection import SerializedConnection
from genesis.db.crud import federation as fed
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db(tmp_path):
    # Wrap in SerializedConnection — the runtime type, and the one append_message
    # requires for its transaction()-scoped atomicity. Schema build runs on the
    # raw handle (unchanged path); CRUD is exercised through the wrapper.
    raw = await aiosqlite.connect(str(tmp_path / "t.db"))
    raw.row_factory = aiosqlite.Row
    await create_all_tables(raw)
    await raw.commit()
    try:
        yield SerializedConnection(raw)
    finally:
        await raw.close()


async def _contact(db, cid="c1", state="active"):
    await fed.create_contact(
        db,
        contact_id=cid,
        display_name="Peer",
        peer_ed25519_pub=b"\x01" * 32,
        peer_x25519_pub=b"\x02" * 32,
        local_mailbox_id="mbA",
        peer_mailbox_id="mbB",
        relay_url="https://relay.example",
        paired_at="2026-08-31T00:00:00Z",
        state=state,
    )


@pytest.mark.asyncio
async def test_create_and_get_contact(db):
    await _contact(db)
    row = await fed.get_contact(db, "c1")
    assert row["display_name"] == "Peer"
    assert row["peer_ed25519_pub"] == b"\x01" * 32  # BLOB round-trips as bytes
    assert row["state"] == "active"
    assert row["last_seq_sent"] == 0 and row["send_chain_head"] is None
    assert await fed.get_contact(db, "nope") is None


@pytest.mark.asyncio
async def test_list_contacts_filters_by_state(db):
    await _contact(db, cid="a", state="active")
    await _contact(db, cid="p", state="pending")
    await _contact(db, cid="r", state="revoked")
    assert {c["contact_id"] for c in await fed.list_contacts(db)} == {"a", "p", "r"}
    assert [c["contact_id"] for c in await fed.list_contacts(db, state="pending")] == ["p"]


@pytest.mark.asyncio
async def test_set_contact_state(db):
    await _contact(db, state="pending")
    assert await fed.set_contact_state(db, "c1", state="revoked", revoked_at="2026-09-01T00:00:00Z")
    row = await fed.get_contact(db, "c1")
    assert row["state"] == "revoked" and row["revoked_at"] == "2026-09-01T00:00:00Z"
    assert await fed.set_contact_state(db, "ghost", state="active") is False


@pytest.mark.asyncio
async def test_set_contact_state_revoked_is_terminal(db):
    """Revocation is the owner's final decision: a revoked contact can never be
    transitioned again, so a stale activation worker racing the revoke can't
    resurrect the contact or clear revoked_at (mirrors the HITL terminal-state
    guard on set_hitl_state)."""
    await _contact(db, state="pending")
    assert await fed.set_contact_state(db, "c1", state="revoked", revoked_at="2026-09-01T00:00:00Z")
    # a stale worker's activation must be refused, not clobber the revocation
    assert await fed.set_contact_state(db, "c1", state="active") is False
    assert await fed.set_contact_state(db, "c1", state="pending") is False
    row = await fed.get_contact(db, "c1")
    assert row["state"] == "revoked"
    assert row["revoked_at"] == "2026-09-01T00:00:00Z"  # never clobbered back to NULL


@pytest.mark.asyncio
async def test_set_contact_state_expected_current_cas(db):
    """expected_current makes the transition a compare-and-swap — it only applies
    when the row is still in that state, so two racing workers can't both win."""
    await _contact(db, state="pending")
    # wrong expected state → no-op
    assert (
        await fed.set_contact_state(db, "c1", state="active", expected_current="revoked") is False
    )
    assert (await fed.get_contact(db, "c1"))["state"] == "pending"
    # correct expected state → applies; a second identical CAS then loses
    assert await fed.set_contact_state(db, "c1", state="active", expected_current="pending")
    assert (
        await fed.set_contact_state(db, "c1", state="active", expected_current="pending") is False
    )


@pytest.mark.asyncio
async def test_revoke_requires_stamp_and_stamp_requires_revoke(db):
    """revoked_at travels ONLY with the revoke transition: a revoke without a
    stamp would be permanently un-stampable (the terminal guard refuses any
    later re-stamp), and a stamp on a non-revoke transition would mint an
    active contact carrying a revocation timestamp."""
    await _contact(db, state="pending")
    with pytest.raises(ValueError):
        await fed.set_contact_state(db, "c1", state="revoked")  # no stamp
    with pytest.raises(ValueError):
        await fed.set_contact_state(db, "c1", state="active", revoked_at="2026-09-01T00:00:00Z")
    assert (await fed.get_contact(db, "c1"))["state"] == "pending"  # nothing applied


@pytest.mark.asyncio
async def test_create_contact_rejects_unknown_state(db):
    """create_contact's state param is validated against the same closed set —
    a typo fails loud as ValueError, not as a downstream IntegrityError."""
    with pytest.raises(ValueError):
        await _contact(db, cid="bad", state="actve")


@pytest.mark.asyncio
async def test_set_hitl_state_rejects_unknown_states(db):
    """Same closed-set validation on the HITL side: a typo'd target state or
    expected_current must fail LOUD, not silently produce a CAS that can never
    match (returning False forever)."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    with pytest.raises(ValueError):
        await fed.set_hitl_state(db, "m1", hitl_state="aproved")
    with pytest.raises(ValueError):
        await fed.set_hitl_state(db, "m1", hitl_state="approved", expected_current="hld")


@pytest.mark.asyncio
async def test_set_contact_state_rejects_unknown_states(db):
    """Target and expected states are validated against the closed contact-state
    set — a typo must fail LOUD, not silently create an un-enumerable state that
    bypasses the terminal guard (or a CAS that can never match)."""
    await _contact(db, state="pending")
    with pytest.raises(ValueError):
        await fed.set_contact_state(db, "c1", state="zombie")
    with pytest.raises(ValueError):
        await fed.set_contact_state(db, "c1", state="active", expected_current="pnding")


@pytest.mark.asyncio
async def test_append_requires_active_contact(db):
    """The append CAS also requires state='active': a message can't be appended
    to a pending (not-yet-activated) contact, and owner revocation that commits
    before an in-flight worker's append wins the race — the append fails instead
    of landing on a revoked contact."""
    await _contact(db, cid="p1", state="pending")
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m1",
            contact_id="p1",
            direction="in",
            seq=1,
            payload_hash="h1",
            created_at="2026-08-31T00:00:00Z",
            hitl_state="received",
        )
    # revocation race: contact was active when the worker read it, revoke lands first
    await _contact(db, cid="a1", state="active")
    assert await fed.set_contact_state(db, "a1", state="revoked", revoked_at="2026-09-01T00:00:00Z")
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m2",
            contact_id="a1",
            direction="in",
            seq=1,
            payload_hash="h1",
            created_at="2026-09-01T00:00:01Z",
            hitl_state="received",
        )
    assert await fed.get_message(db, "m2") is None  # rolled back, nothing landed


@pytest.mark.asyncio
async def test_append_advances_chain_head_atomically(db):
    """The load-bearing invariant: appending a message moves the contact's
    per-direction chain head in the same transaction, so chain_tip reflects the
    just-written message (never a stale head)."""
    await _contact(db)
    assert await fed.chain_tip(db, "c1", direction="out") == (0, None)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        payload_hash="h1",
        created_at="2026-08-31T00:00:01Z",
        hitl_state="held",
    )
    assert await fed.chain_tip(db, "c1", direction="out") == (1, "h1")
    # inbound chain is independent
    assert await fed.chain_tip(db, "c1", direction="in") == (0, None)
    await fed.append_message(
        db,
        msg_id="m2",
        contact_id="c1",
        direction="out",
        seq=2,
        prev_hash="h1",
        payload_hash="h2",
        created_at="2026-08-31T00:00:02Z",
        hitl_state="held",
    )
    assert await fed.chain_tip(db, "c1", direction="out") == (2, "h2")
    # the contact row itself carries the advanced head
    row = await fed.get_contact(db, "c1")
    assert row["last_seq_sent"] == 2 and row["send_chain_head"] == "h2"
    assert row["last_seq_recv"] == 0  # untouched


@pytest.mark.asyncio
async def test_concurrent_independent_chains_stay_consistent(db):
    """Concurrency SMOKE/integration test: PR2 runs the inbound poll-loop and the
    outbound approval-drain concurrently on Genesis's single shared connection,
    appending to INDEPENDENT chains (separate seq + head columns). Exercises many
    concurrent atomic appends end-to-end and asserts both chains come out intact
    (no deadlock, no error, correct tips + row counts).

    NOTE: an all-SUCCESS interleaving does NOT distinguish the fix from the old
    per-call-commit form (a prematurely-committed but internally-consistent
    intermediate state is still consistent), so this is not the atomicity
    acceptance bar — that is
    ``test_connection.py::test_transaction_isolates_peer_from_uncommitted_write``,
    a deterministic known-positive replay of the actual fork defect (a peer
    rollback discarding another coroutine's uncommitted write)."""
    await _contact(db)

    async def out_chain():
        prev = None
        for i in range(1, 26):
            h = f"ho{i}"
            await fed.append_message(
                db,
                msg_id=f"o{i:02d}",
                contact_id="c1",
                direction="out",
                seq=i,
                prev_hash=prev,
                payload_hash=h,
                created_at="2026-08-31T00:00:00Z",
                hitl_state="held",
            )
            prev = h

    async def in_chain():
        prev = None
        for i in range(1, 26):
            h = f"hi{i}"
            await fed.append_message(
                db,
                msg_id=f"i{i:02d}",
                contact_id="c1",
                direction="in",
                seq=i,
                prev_hash=prev,
                payload_hash=h,
                created_at="2026-08-31T00:00:00Z",
                hitl_state="received",
            )
            prev = h

    await asyncio.gather(out_chain(), in_chain())

    # Both chains intact — tips at the last link, all rows present, no fork.
    assert await fed.chain_tip(db, "c1", direction="out") == (25, "ho25")
    assert await fed.chain_tip(db, "c1", direction="in") == (25, "hi25")
    assert len(await fed.list_messages(db, "c1", direction="out")) == 25
    assert len(await fed.list_messages(db, "c1", direction="in")) == 25
    row = await fed.get_contact(db, "c1")
    assert row["last_seq_sent"] == 25 and row["send_chain_head"] == "ho25"
    assert row["last_seq_recv"] == 25 and row["recv_chain_head"] == "hi25"


@pytest.mark.asyncio
async def test_append_rejects_bad_direction(db):
    await _contact(db)
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="x",
            contact_id="c1",
            direction="sideways",
            seq=1,
            payload_hash="h",
            created_at="2026-08-31T00:00:00Z",
            hitl_state="held",
        )


async def _append_chain(db, contact_id, direction, seqs, *, hitl_state="received"):
    """Append a strictly-linked run of messages (CAS requires in-order + prev)."""
    prev = None
    for i in seqs:
        h = f"h{direction}{i}"
        await fed.append_message(
            db,
            msg_id=f"m{direction}{i}",
            contact_id=contact_id,
            direction=direction,
            seq=i,
            prev_hash=prev,
            payload_hash=h,
            created_at="2026-08-31T00:00:00Z",
            hitl_state=hitl_state,
        )
        prev = h


@pytest.mark.asyncio
async def test_list_messages_ordered_by_msg_id_within_direction(db):
    # within one direction, msg_id (ULID) order coincides with chain/seq order
    await _contact(db)
    await _append_chain(db, "c1", "in", (1, 2, 3))
    assert [m["msg_id"] for m in await fed.list_messages(db, "c1")] == ["min1", "min2", "min3"]


@pytest.mark.asyncio
async def test_list_messages_pagination(db):
    await _contact(db)
    await _append_chain(db, "c1", "in", (1, 2, 3))
    first = await fed.list_messages(db, "c1", limit=2)
    assert [m["msg_id"] for m in first] == ["min1", "min2"]
    # rows past the limit are reachable via after_msg_id — never permanently hidden
    rest = await fed.list_messages(db, "c1", after_msg_id=first[-1]["msg_id"])
    assert [m["msg_id"] for m in rest] == ["min3"]


@pytest.mark.asyncio
async def test_combined_transcript_is_chronological_not_seq_interleaved(db):
    """direction=None orders by msg_id (ULID/time), NOT by seq — the in/out seq
    sequences are independent, so a seq-sort would scramble the conversation."""
    await _contact(db)
    # interleave in time: out#1, in#1, out#2 (ULID-ish ids sort chronologically)
    await fed.append_message(
        db,
        msg_id="01out1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="ho1",
        created_at="2026-08-31T00:00:01Z",
        hitl_state="held",
    )
    await fed.append_message(
        db,
        msg_id="02in1",
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="hi1",
        created_at="2026-08-31T00:00:02Z",
        hitl_state="received",
    )
    await fed.append_message(
        db,
        msg_id="03out2",
        contact_id="c1",
        direction="out",
        seq=2,
        prev_hash="ho1",
        payload_hash="ho2",
        created_at="2026-08-31T00:00:03Z",
        hitl_state="held",
    )
    # chronological — NOT out-1, out-2, in-1 (which a seq-sort would produce)
    assert [m["msg_id"] for m in await fed.list_messages(db, "c1")] == ["01out1", "02in1", "03out2"]


@pytest.mark.asyncio
async def test_inbound_append_forces_external_untrusted_origin(db):
    """Peer content is untrusted: an inbound append with origin_class omitted is
    forced to external_untrusted so a later memory write stays quarantined."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="i1",
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="received",
    )
    row = await fed.get_message(db, "i1")
    assert row["origin_class"] == "external_untrusted"
    # outbound is left untouched (owner-authored)
    await fed.append_message(
        db,
        msg_id="o1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="ho",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    assert (await fed.get_message(db, "o1"))["origin_class"] is None


@pytest.mark.asyncio
async def test_inbound_append_overrides_explicit_trusted_origin(db):
    """The quarantine is UNCONDITIONAL for direction='in': an explicit
    origin_class — including a peer-influenced caller passing 'first_party' —
    must NOT pass through. Peer-sourced content is untrusted by construction;
    only forcing every inbound row keeps the injection boundary intact."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="i1",
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        origin_class="first_party",  # must be overridden, not respected
        created_at="2026-08-31T00:00:00Z",
        hitl_state="received",
    )
    assert (await fed.get_message(db, "i1"))["origin_class"] == "external_untrusted"
    # outbound explicit values still pass through (owner-authored side)
    await fed.append_message(
        db,
        msg_id="o1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="ho",
        origin_class="first_party",
        created_at="2026-08-31T00:00:01Z",
        hitl_state="held",
    )
    assert (await fed.get_message(db, "o1"))["origin_class"] == "first_party"


def test_inbound_origin_class_matches_provenance_constant():
    """The literal used at the db boundary must equal the canonical provenance
    enum value, or the injection-boundary tag would silently not match."""
    from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED

    assert fed._INBOUND_ORIGIN_CLASS == ORIGIN_EXTERNAL_UNTRUSTED


@pytest.mark.asyncio
async def test_set_hitl_state_terminal_is_immutable(db):
    """A terminal row (sent/rejected) can't be transitioned again — a racing
    worker must not resurrect a rejected message or unsend a delivered one."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    assert await fed.set_hitl_state(db, "m1", hitl_state="rejected")
    # rejected is terminal — a stale approve/send worker can't move it
    assert await fed.set_hitl_state(db, "m1", hitl_state="approved") is False
    assert await fed.set_hitl_state(db, "m1", hitl_state="sent") is False
    assert (await fed.get_message(db, "m1"))["hitl_state"] == "rejected"


@pytest.mark.asyncio
async def test_set_hitl_state_expected_current_cas(db):
    """expected_current makes the transition a compare-and-swap — only applies if
    the row is still in the expected state (two racing workers can't both win)."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    # wrong expected state → no-op
    assert (
        await fed.set_hitl_state(db, "m1", hitl_state="sent", expected_current="approved") is False
    )
    assert (await fed.get_message(db, "m1"))["hitl_state"] == "held"
    # correct expected state → applies
    assert await fed.set_hitl_state(db, "m1", hitl_state="approved", expected_current="held")


@pytest.mark.asyncio
async def test_direction_is_validated_everywhere(db):
    await _contact(db)
    for bad in ("incoming", "sideways", ""):
        with pytest.raises(ValueError):
            await fed.chain_tip(db, "c1", direction=bad)
        with pytest.raises(ValueError):
            await fed.list_messages(db, "c1", direction=bad)


@pytest.mark.asyncio
async def test_append_cas_rejects_out_of_order_and_wrong_prev(db):
    """The chain-tip CAS: an append that does not link strictly onto the current
    tip (skipped seq, repeated seq, or wrong prev_hash) is rejected and rolled
    back — the head can never move backward or fork."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    # skip seq 2
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m3",
            contact_id="c1",
            direction="out",
            seq=3,
            prev_hash="h1",
            payload_hash="h3",
            created_at="2026-08-31T00:00:02Z",
            hitl_state="held",
        )
    # correct seq but wrong prev_hash
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m2",
            contact_id="c1",
            direction="out",
            seq=2,
            prev_hash="WRONG",
            payload_hash="h2",
            created_at="2026-08-31T00:00:02Z",
            hitl_state="held",
        )
    # rollback left the tip at m1 and wrote nothing
    assert await fed.chain_tip(db, "c1", direction="out") == (1, "h1")
    assert await fed.get_message(db, "m3") is None and await fed.get_message(db, "m2") is None
    # the correct next link succeeds
    await fed.append_message(
        db,
        msg_id="m2",
        contact_id="c1",
        direction="out",
        seq=2,
        prev_hash="h1",
        payload_hash="h2",
        created_at="2026-08-31T00:00:03Z",
        hitl_state="held",
    )
    assert await fed.chain_tip(db, "c1", direction="out") == (2, "h2")


@pytest.mark.asyncio
async def test_approval_id_is_unique(db):
    """One owner approval can release at most one outbound message."""
    import sqlite3

    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        approval_id="ap1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    with pytest.raises(sqlite3.IntegrityError):
        await fed.append_message(
            db,
            msg_id="m2",
            contact_id="c1",
            direction="out",
            seq=2,
            prev_hash="h1",
            payload_hash="h2",
            approval_id="ap1",  # same approval → rejected
            created_at="2026-08-31T00:00:01Z",
            hitl_state="held",
        )
    assert await fed.get_message(db, "m2") is None  # rolled back


@pytest.mark.asyncio
async def test_set_hitl_state(db):
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="out",
        seq=1,
        payload_hash="h1",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="held",
    )
    assert await fed.set_hitl_state(
        db, "m1", hitl_state="sent", delivered_at="2026-08-31T01:00:00Z"
    )
    row = await fed.get_message(db, "m1")
    assert row["hitl_state"] == "sent" and row["delivered_at"] == "2026-08-31T01:00:00Z"


@pytest.mark.asyncio
async def test_prune_bodies_preserves_chain_skeleton(db):
    """Prune nulls plaintext/ciphertext/nonce but keeps seq/prev_hash/
    payload_hash — a pruned transcript must still verify. Rows are never
    deleted."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="old",
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        nonce=b"\x00" * 24,
        ciphertext=b"cipher",
        plaintext="secret",
        created_at="2026-08-01T00:00:00Z",
        hitl_state="received",
    )
    await fed.append_message(
        db,
        msg_id="new",
        contact_id="c1",
        direction="in",
        seq=2,
        prev_hash="h1",
        payload_hash="h2",
        plaintext="keep",
        created_at="2026-08-31T00:00:00Z",
        hitl_state="received",
    )
    # an OLD but still-HELD outbound (non-terminal) message: its body must NOT be
    # pruned or the payload is lost when the owner later approves the send.
    await fed.append_message(
        db,
        msg_id="pending",
        contact_id="c1",
        direction="out",
        seq=1,
        prev_hash=None,
        payload_hash="hp1",
        plaintext="dont-lose-me",
        created_at="2026-08-01T00:00:00Z",
        hitl_state="held",
    )
    pruned = await fed.prune_bodies(db, before="2026-08-15T00:00:00Z")
    assert pruned == 1  # only the terminal 'old' row
    old = await fed.get_message(db, "old")
    assert old is not None  # never deleted
    assert old["plaintext"] is None and old["ciphertext"] is None and old["nonce"] is None
    assert old["seq"] == 1 and old["payload_hash"] == "h1"  # skeleton intact
    new = await fed.get_message(db, "new")
    assert new["plaintext"] == "keep"  # newer row untouched
    pending = await fed.get_message(db, "pending")
    assert pending["plaintext"] == "dont-lose-me"  # non-terminal body PRESERVED though old
    # idempotent: re-running prunes nothing more
    assert await fed.prune_bodies(db, before="2026-08-15T00:00:00Z") == 0


@pytest.mark.asyncio
async def test_prune_compares_timestamps_as_instants_not_text(db):
    """created_at < before must compare INSTANTS, not text: a textual compare is
    wrong in both directions for valid ISO timestamps with different UTC
    offsets. '2026-08-01T00:00:00-05:00' (= 05:00Z) sorts textually BEFORE an
    '…03:00:00Z' cutoff though it is after it (would be wrongly pruned), and
    '2026-08-01T05:00:00+05:00' (= 00:00Z) sorts textually AFTER it though it is
    before it (would be wrongly kept)."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m-late",  # instant AFTER the cutoff, textually before it
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        plaintext="keep-me",
        created_at="2026-08-01T00:00:00-05:00",
        hitl_state="received",
    )
    await fed.append_message(
        db,
        msg_id="m-early",  # instant BEFORE the cutoff, textually after it
        contact_id="c1",
        direction="in",
        seq=2,
        prev_hash="h1",
        payload_hash="h2",
        plaintext="prune-me",
        created_at="2026-08-01T05:00:00+05:00",
        hitl_state="received",
    )
    pruned = await fed.prune_bodies(db, before="2026-08-01T03:00:00Z")
    # NB the count alone is INERT here — the textual compare also pruned exactly
    # one row, just the WRONG one. These two per-row assertions are what bite.
    assert pruned == 1
    assert (await fed.get_message(db, "m-late"))["plaintext"] == "keep-me"
    assert (await fed.get_message(db, "m-early"))["plaintext"] is None


@pytest.mark.asyncio
async def test_prune_rejects_a_cutoff_sqlite_cannot_parse(db):
    """An unparseable cutoff must raise, not silently prune NOTHING. datetime()
    returns NULL for it, `x < NULL` is never true, so the pass would return 0
    on every run forever — indistinguishable from "nothing to prune", while the
    whole table's bodies leak past retention. '+0000' is the sharp case: it is
    what strftime('%z')/`date +%z` emit, Python's fromisoformat ACCEPTS it, and
    SQLite rejects it (only the colon form parses)."""
    await _contact(db)
    await fed.append_message(
        db,
        msg_id="m1",
        contact_id="c1",
        direction="in",
        seq=1,
        prev_hash=None,
        payload_hash="h1",
        plaintext="body",
        created_at="2026-08-01T00:00:00Z",
        hitl_state="received",
    )
    for bad in (
        "2026-08-15T00:00:00+0000",
        "garbage",
        "now",
        "2026-08-15",
        "",
        "2026-13-45T00:00:00Z",
    ):
        with pytest.raises(ValueError):
            await fed.prune_bodies(db, before=bad)
    # the body is untouched, and a VALID cutoff still prunes
    assert (await fed.get_message(db, "m1"))["plaintext"] == "body"
    assert await fed.prune_bodies(db, before="2026-08-15T00:00:00Z") == 1


@pytest.mark.asyncio
async def test_append_rejects_unparseable_created_at(db):
    """Pin the stamp format at the WRITER: a row stored with a created_at that
    SQLite's datetime() NULLs would be silently un-prunable forever."""
    await _contact(db)
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m1",
            contact_id="c1",
            direction="in",
            seq=1,
            payload_hash="h1",
            created_at="31-08-2026 midnight",
            hitl_state="received",
        )
    assert await fed.get_message(db, "m1") is None  # nothing landed


@pytest.mark.asyncio
async def test_append_to_unknown_contact_raises(db):
    """A message for a non-existent contact must fail LOUD (not silently insert
    an orphan whose head never moved) regardless of PRAGMA foreign_keys."""
    with pytest.raises(ValueError):
        await fed.append_message(
            db,
            msg_id="m1",
            contact_id="ghost",
            direction="out",
            seq=1,
            payload_hash="h1",
            created_at="2026-08-31T00:00:00Z",
            hitl_state="held",
        )
    # nothing was left behind (the transaction rolled back)
    assert await fed.get_message(db, "m1") is None


def _norm_ddl(ddl: str) -> str:
    """Normalize a CREATE TABLE DDL for comparison: drop -- comments, lowercase,
    collapse all whitespace."""
    import re

    return " ".join(re.sub(r"--[^\n]*", "", ddl).split()).lower()


def test_migration_and_base_ddl_match_for_federation_tables():
    """Lock the two schema build paths together: the numbered migration 0090 DDL
    (existing installs) and the canonical _tables.py TABLES DDL (fresh installs)
    must be identical, so a future column added to one but not the other can't
    ship divergent schemas (the schema_both_build_paths class)."""
    import importlib

    from genesis.db.schema import TABLES

    mig = importlib.import_module("genesis.db.migrations.20260904231414_federation")
    assert _norm_ddl(mig._CONTACTS_DDL) == _norm_ddl(TABLES["federation_contacts"])
    assert _norm_ddl(mig._MESSAGES_DDL) == _norm_ddl(TABLES["federation_messages"])
