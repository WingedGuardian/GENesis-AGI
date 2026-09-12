"""Tests for the ``marketing_prospects`` CRUD — the owner-curated cold-outreach
prospect store (marketing cold-send substrate).

Real tmp DB, full schema, synthetic fixtures only (no network / real addresses).
Covers: create + get_by_id/get_by_email, list_active (status='active' AND NOT
opted_out), mark_contacted, mark_opted_out (permanent suppression).
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import marketing_prospects as mp
from genesis.db.schema import create_all_tables

_TS = "2026-08-25T00:00:00"


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_create_and_get_by_id(db):
    await mp.create(
        db,
        id="p1",
        email="dev@example.com",
        name="Dev One",
        company="Acme",
        source="seed",
        created_at=_TS,
        updated_at=_TS,
    )
    row = await mp.get_by_id(db, "p1")
    assert row is not None
    assert row["email"] == "dev@example.com"
    assert row["name"] == "Dev One"
    assert row["company"] == "Acme"
    assert row["status"] == "active"  # default
    assert row["opted_out"] == 0  # default
    assert row["source"] == "seed"


@pytest.mark.asyncio
async def test_get_by_email(db):
    await mp.create(db, id="p1", email="dev@example.com", created_at=_TS, updated_at=_TS)
    row = await mp.get_by_email(db, "dev@example.com")
    assert row is not None and row["id"] == "p1"
    assert await mp.get_by_email(db, "nobody@example.com") is None


@pytest.mark.asyncio
async def test_list_active_excludes_inactive_and_opted_out(db):
    await mp.create(db, id="active", email="a@example.com", created_at=_TS, updated_at=_TS)
    await mp.create(
        db,
        id="contacted",
        email="c@example.com",
        status="contacted",
        created_at=_TS,
        updated_at=_TS,
    )
    await mp.create(db, id="opted", email="o@example.com", created_at=_TS, updated_at=_TS)
    await mp.mark_opted_out(db, "opted", opted_out_at=_TS)

    active = await mp.list_active(db)
    ids = {r["id"] for r in active}
    assert ids == {"active"}  # 'contacted' (non-active) and opted-out excluded


@pytest.mark.asyncio
async def test_mark_contacted(db):
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    ok = await mp.mark_contacted(db, "p1", contacted_at="2026-08-25T01:00:00")
    assert ok is True
    row = await mp.get_by_id(db, "p1")
    assert row["status"] == "contacted"
    assert row["last_contacted_at"] == "2026-08-25T01:00:00"


@pytest.mark.asyncio
async def test_unique_email_index_is_case_insensitive(db):
    # Defense-in-depth: even a DIRECT DB insert that bypasses _normalize_email
    # must not let Target@x.com and target@x.com coexist (one opted-out, one not
    # → opt-out defeated). The UNIQUE index is COLLATE NOCASE, so case-only
    # variants collide at the index level.
    await db.execute(
        "INSERT INTO marketing_prospects (id, email, status, opted_out, "
        "created_at, updated_at) VALUES ('a', 'Target@x.com', 'active', 0, ?, ?)",
        (_TS, _TS),
    )
    await db.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO marketing_prospects (id, email, status, opted_out, "
            "created_at, updated_at) VALUES ('b', 'target@x.com', 'active', 0, ?, ?)",
            (_TS, _TS),
        )
        await db.commit()


@pytest.mark.asyncio
async def test_mark_opted_out_is_permanent_suppression(db):
    await mp.create(db, id="p1", email="a@example.com", created_at=_TS, updated_at=_TS)
    ok = await mp.mark_opted_out(db, "p1", opted_out_at="2026-08-25T02:00:00")
    assert ok is True
    row = await mp.get_by_id(db, "p1")
    assert row["opted_out"] == 1
    # An opted-out prospect never re-enters the active set.
    assert await mp.list_active(db) == []


@pytest.mark.asyncio
async def test_mark_contacted_by_email_only_advances_active(db):
    """The shared by-email stamp used by BOTH delivery paths (drain + granted
    pipeline). Advances an ACTIVE match → contacted; no-ops (returns False) on an
    absent recipient or a non-'active' row (never downgrades 'replied'/'contacted',
    never touches a non-marketing address). Case-insensitive on the email."""
    await mp.create(db, id="p1", email="Prospect@Example.com", created_at=_TS, updated_at=_TS)
    # active → contacted (email match is normalized/case-insensitive)
    assert await mp.mark_contacted_by_email(db, "prospect@example.com", contacted_at=_TS) is True
    assert (await mp.get_by_id(db, "p1"))["status"] == "contacted"
    # second call is a no-op — already non-active, never re-stamped/downgraded
    assert await mp.mark_contacted_by_email(db, "prospect@example.com", contacted_at=_TS) is False
    # a 'replied' prospect is NEVER downgraded to 'contacted'
    await mp.create(
        db, id="p2", email="replied@example.com", status="replied", created_at=_TS, updated_at=_TS
    )
    assert await mp.mark_contacted_by_email(db, "replied@example.com", contacted_at=_TS) is False
    assert (await mp.get_by_id(db, "p2"))["status"] == "replied"
    # absent recipient → no-op, no crash
    assert await mp.mark_contacted_by_email(db, "nobody@example.com", contacted_at=_TS) is False


@pytest.mark.asyncio
async def test_mark_contacted_by_email_matches_mixed_case_seeded_row(db):
    """A directly-seeded row with a mixed-case email (bypassing create()'s
    normalization — e.g. a future bulk-import path) is still matched
    case-insensitively (get_by_email COLLATE NOCASE), so the delivery stamp never
    misses it and re-pitches a delivered prospect."""
    await db.execute(
        "INSERT INTO marketing_prospects (id, email, status, opted_out, created_at, updated_at) "
        "VALUES ('mc', 'Mixed@Example.com', 'active', 0, ?, ?)",
        (_TS, _TS),
    )
    await db.commit()
    assert await mp.mark_contacted_by_email(db, "mixed@example.com", contacted_at=_TS) is True
    assert (await mp.get_by_id(db, "mc"))["status"] == "contacted"


@pytest.mark.asyncio
async def test_mark_contacted_by_email_is_atomic_no_read_then_write(db, monkeypatch):
    """The stamp is a single conditional UPDATE (`WHERE ... AND status = 'active'`),
    NOT a read-then-write — so a concurrent active→replied flip between a lookup and an
    update can't clobber the newer state back to 'contacted' (the never-downgrade
    guarantee holds under concurrency). Proven structurally: it performs NO get_by_email
    read before its write. RED on the old read-then-write (get_by_email → unconditional
    mark_contacted)."""
    calls: list[str] = []
    orig = mp.get_by_email

    async def spy_get_by_email(conn, email):
        calls.append(email)
        return await orig(conn, email)

    monkeypatch.setattr(mp, "get_by_email", spy_get_by_email)

    await mp.create(db, id="p1", email="prospect@example.com", created_at=_TS, updated_at=_TS)
    assert await mp.mark_contacted_by_email(db, "prospect@example.com", contacted_at=_TS) is True
    assert (await mp.get_by_id(db, "p1"))["status"] == "contacted"
    assert calls == []  # atomic: no read-before-write window a concurrent flip could exploit
