"""pr_verifications crud + the two-build-path schema parity (issue #1718 half B).

The invariants: (repo, pr_number) is unique at the SCHEMA level, so re-observing
a merged PR can never duplicate; a docs-only row is born closed with its reason;
a closed row never flips back and never has its record overwritten; OPEN rows
are never pruned — an open row IS the obligation; and the fresh-install DDL
(``schema/_tables.py``) and the migration produce the SAME table, because two
build paths that drift is two schemas diverging with nothing to notice.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import pr_verifications as verif_crud
from genesis.db.schema._tables import INDEXES, TABLES

MIG = importlib.import_module("genesis.db.migrations.20260906234824_pr_verifications")

NOW = "2026-09-06T23:00:00+00:00"
OLD = "2026-01-01T00:00:00+00:00"
REPO = "owner/repo"


@pytest.fixture
async def db(tmp_path):
    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(path)) as conn:
        conn.row_factory = aiosqlite.Row
        await MIG.up(conn)
        await conn.commit()
        yield conn


@pytest.fixture
async def bare_db(tmp_path):
    """No migration — the pre-migration subprocess window."""
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


async def _row(db, pr_number):
    cur = await db.execute(
        "SELECT * FROM pr_verifications WHERE repo = ? AND pr_number = ?", (REPO, pr_number)
    )
    r = await cur.fetchone()
    return dict(r) if r else None


# ── open_verification ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_creates_an_open_row(db):
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=7, pr_title="feat: x", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "created"
    row = await _row(db, 7)
    assert row["status"] == "open"
    assert row["closed_reason"] is None and row["closed_at"] is None
    assert row["created_at"] == NOW


@pytest.mark.asyncio
async def test_docs_only_row_is_born_closed_with_the_reason(db):
    out = await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=8,
        pr_title="docs: y",
        merged_at="2026-09-06T10:00:00Z",
        now=NOW,
        closed_reason="docs-only diff (2 path(s)) — deterministic exemption, no runtime surface",
    )
    assert out == "created"
    row = await _row(db, 8)
    assert row["status"] == "closed"
    assert "docs-only" in row["closed_reason"]
    assert row["closed_at"] == NOW


@pytest.mark.asyncio
async def test_reobserving_a_pr_is_absorbed_not_duplicated(db):
    """The dedup is the SCHEMA (unique index + INSERT OR IGNORE) — a re-covered
    enumeration window, or two workers racing, cannot create a second row, and
    the second write reports 'exists' so a lane never counts it as new."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=9, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=9, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "exists"
    cur = await db.execute(
        "SELECT COUNT(*) FROM pr_verifications WHERE repo = ? AND pr_number = 9", (REPO,)
    )
    assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_a_closed_row_is_not_reopened_by_a_late_open(db):
    """Window re-coverage after a validator verified the PR must not resurrect
    the obligation — the second write is ignored whatever status the row holds."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=10, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert await verif_crud.close_verification(
        db, repo=REPO, pr_number=10, reason="verified", evidence="ran the E2E", now=NOW
    )
    out = await verif_crud.open_verification(
        db, repo=REPO, pr_number=10, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert out == "exists"
    assert (await _row(db, 10))["status"] == "closed"


@pytest.mark.asyncio
async def test_same_pr_number_in_another_repo_is_a_distinct_obligation(db):
    """The key is (repo, pr_number), not pr_number — a fork or rename must not
    alias two different PRs onto one row."""
    a = await verif_crud.open_verification(
        db, repo=REPO, pr_number=11, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    b = await verif_crud.open_verification(
        db, repo="other/repo", pr_number=11, pr_title="b", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert (a, b) == ("created", "created")


# ── close_verification ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_close_records_evidence_and_only_touches_open_rows(db):
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=12, pr_title="a", merged_at="2026-09-06T10:00:00Z", now=NOW
    )
    assert await verif_crud.close_verification(
        db, repo=REPO, pr_number=12, reason="verified", evidence="health 200", now=NOW
    )
    row = await _row(db, 12)
    assert (row["status"], row["evidence"]) == ("closed", "health 200")
    # A second close must not overwrite the record — two validators cannot fight.
    assert not await verif_crud.close_verification(
        db, repo=REPO, pr_number=12, reason="other", evidence="other", now=NOW
    )
    assert (await _row(db, 12))["evidence"] == "health 200"


# ── readers ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_open_is_oldest_merge_first_and_open_only(db):
    for n, merged in ((20, "2026-09-03T00:00:00Z"), (21, "2026-09-01T00:00:00Z")):
        await verif_crud.open_verification(
            db, repo=REPO, pr_number=n, pr_title="t", merged_at=merged, now=NOW
        )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=22,
        pr_title="d",
        merged_at="2026-08-30T00:00:00Z",
        now=NOW,
        closed_reason="docs-only",
    )
    rows = await verif_crud.list_open(db)
    assert [r["pr_number"] for r in rows] == [21, 20]
    assert await verif_crud.counts(db) == {"open": 2, "closed": 1}


# ── retention ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_deletes_only_old_closed_rows_never_open_ones(db):
    """THE fail-direction test: an ancient OPEN row survives every prune —
    deleting it would silently forgive an unverified merge, which is the exact
    failure this table exists to prevent."""
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=30, pr_title="ancient open", merged_at=OLD, now=OLD
    )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=31,
        pr_title="old closed",
        merged_at=OLD,
        now=OLD,
        closed_reason="docs-only",
    )
    await verif_crud.open_verification(
        db,
        repo=REPO,
        pr_number=32,
        pr_title="fresh closed",
        merged_at=NOW,
        now=NOW,
        closed_reason="docs-only",
    )
    deleted = await verif_crud.prune_closed(db, older_than_days=180, now=NOW)
    assert deleted == 1
    assert (await _row(db, 30))["status"] == "open"  # ancient, open, UNTOUCHED
    assert await _row(db, 31) is None
    assert (await _row(db, 32))["status"] == "closed"


# ── pre-migration posture ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_everything_noops_before_the_migration(bare_db):
    assert not await verif_crud.tables_available(bare_db)
    assert (
        await verif_crud.open_verification(
            bare_db, repo=REPO, pr_number=1, pr_title=None, merged_at=NOW, now=NOW
        )
        == "unavailable"
    )
    assert not await verif_crud.exists(bare_db, repo=REPO, pr_number=1)
    assert await verif_crud.list_open(bare_db) == []
    assert await verif_crud.counts(bare_db) == {}
    assert await verif_crud.prune_closed(bare_db, now=NOW) == 0


# ── the two build paths produce ONE schema ───────────────────────────────


def _normalize(sql: str) -> str:
    return " ".join(sql.split())


@pytest.mark.asyncio
async def test_fresh_install_ddl_and_migration_are_the_same_schema(tmp_path):
    """schema/_tables.py builds fresh installs; the migration builds existing
    ones. If they drift, two populations run different schemas and nothing
    notices until a query fails on exactly one of them. Compare what SQLite
    itself recorded, table AND both indexes."""
    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as fresh:
        await fresh.execute(TABLES["pr_verifications"])
        for ddl in INDEXES:
            if "pr_verifications" in ddl:
                await fresh.execute(ddl)
        cur = await fresh.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='pr_verifications' "
            "AND sql IS NOT NULL ORDER BY name"
        )
        fresh_schema = {name: _normalize(sql) for name, sql in await cur.fetchall()}
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as migrated:
        await MIG.up(migrated)
        cur = await migrated.execute(
            "SELECT name, sql FROM sqlite_master WHERE tbl_name='pr_verifications' "
            "AND sql IS NOT NULL ORDER BY name"
        )
        migrated_schema = {name: _normalize(sql) for name, sql in await cur.fetchall()}
    assert fresh_schema == migrated_schema
    assert "pr_verifications" in fresh_schema
    assert "idx_prv_repo_pr" in fresh_schema, "the dedup index is load-bearing"


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "twice.db")) as db:
        await MIG.up(db)
        await MIG.up(db)  # a re-run must be a no-op, not an error


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "\n\t "])
async def test_close_refuses_an_empty_reason(db, bad):
    """A closed row with no reason is an unverifiable claim in permanent record:
    it says "handled" and carries nothing.

    The schema permits it (status='closed' with closed_reason NULL is valid
    DDL), so the guard lives at the writer — and it matters precisely because
    the eventual closer is a validator SESSION, i.e. an LLM caller, which is
    exactly the caller that passes an empty string. It RAISES rather than
    returning False: a caller that omitted its reason has a bug, and a silent
    no-op would leave the obligation open while the caller believes it closed.
    """
    await verif_crud.open_verification(
        db, repo=REPO, pr_number=40, pr_title="a", merged_at=NOW, now=NOW
    )
    with pytest.raises(ValueError, match="non-empty reason"):
        await verif_crud.close_verification(
            db, repo=REPO, pr_number=40, reason=bad, evidence="x", now=NOW
        )
    assert (await _row(db, 40))["status"] == "open", "the row must be untouched"
