"""Tests for the federation relay CRUD (federation_contacts + the
federation_messages hash-chained transcript). Exercises the real
``create_all_tables`` build path, so it also validates the migration-0090 DDL."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import federation as fed
from genesis.db.schema import create_all_tables


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


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


@pytest.mark.asyncio
async def test_list_messages_orders_by_seq(db):
    await _contact(db)
    for i, mid in ((2, "m2"), (1, "m1"), (3, "m3")):
        await fed.append_message(
            db,
            msg_id=mid,
            contact_id="c1",
            direction="in",
            seq=i,
            payload_hash=f"h{i}",
            created_at="2026-08-31T00:00:00Z",
            hitl_state="received",
        )
    assert [m["msg_id"] for m in await fed.list_messages(db, "c1")] == ["m1", "m2", "m3"]


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
    pruned = await fed.prune_bodies(db, before="2026-08-15T00:00:00Z")
    assert pruned == 1
    old = await fed.get_message(db, "old")
    assert old is not None  # never deleted
    assert old["plaintext"] is None and old["ciphertext"] is None and old["nonce"] is None
    assert old["seq"] == 1 and old["payload_hash"] == "h1"  # skeleton intact
    new = await fed.get_message(db, "new")
    assert new["plaintext"] == "keep"  # newer row untouched
    # idempotent: re-running prunes nothing more
    assert await fed.prune_bodies(db, before="2026-08-15T00:00:00Z") == 0


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


@pytest.mark.asyncio
async def test_duplicate_seq_is_rejected_and_rolls_back(db):
    """UNIQUE(contact_id, direction, seq) prevents a chain fork; the failed insert
    rolls back so the head still reflects the first message."""
    import sqlite3

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
    with pytest.raises(sqlite3.IntegrityError):
        await fed.append_message(
            db,
            msg_id="m2",
            contact_id="c1",
            direction="out",
            seq=1,  # dup seq
            payload_hash="h2",
            created_at="2026-08-31T00:00:01Z",
            hitl_state="held",
        )
    # the rollback undid the second head-advance: tip still points at m1
    assert await fed.chain_tip(db, "c1", direction="out") == (1, "h1")
    assert await fed.get_message(db, "m2") is None


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

    mig = importlib.import_module("genesis.db.migrations.0090_federation")
    assert _norm_ddl(mig._CONTACTS_DDL) == _norm_ddl(TABLES["federation_contacts"])
    assert _norm_ddl(mig._MESSAGES_DDL) == _norm_ddl(TABLES["federation_messages"])
