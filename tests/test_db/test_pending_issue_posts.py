"""Tests for pending_issue_posts — table, migration 0079, and CRUD.

The Contributor Work-Log hold store: a curator drafts a public GitHub issue,
which is sanitized + parked here (status='held') with a linked approval_requests
row; a resolution watcher posts it below the gate on approval, or expires the
hold on rejection/timeout. Mirrors the WS-8 pending_email_sends shape.

Covers the fresh-install path (create_all_tables), the versioned migration
(up/down/idempotency), the request_id UNIQUE + status CHECK constraints, and the
held→posted / held→rejected / held→dry_run transitions including the double-post
guard (a hold can leave 'held' exactly once; dry_run is terminal).
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import pending_issue_posts as pip

MIGRATION = importlib.import_module("genesis.db.migrations.0079_pending_issue_posts")

_ROW = {
    "id": "p1",
    "request_id": "req-1",
    "repo": "WingedGuardian/GENesis-AGI",
    "title": "Fix off-by-one in chunk()",
    "body": "The chunk() helper drops the last token when input lacks a newline.",
    "source": "follow_up",
    "source_ref": "fu-abc",
    "labels": '["good first issue"]',
    "cell_domain": "github",
    "cell_verb": "issue_create",
    "cell_risk_class": "bulk",
    "held_at": "2026-08-07T00:00:00",
    "mode": "propose_only",
}
_TS = "2026-08-07T01:00:00"


@pytest.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await MIGRATION.up(conn)
        await conn.commit()
        yield conn


# --------------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------------- #
class TestSchema:
    @pytest.mark.asyncio
    async def test_table_and_index_exist(self, db):
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_issue_posts'"
        )
        assert await cur.fetchone() is not None
        cur = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_pending_issue_posts_status'"
        )
        assert await cur.fetchone() is not None

    @pytest.mark.asyncio
    async def test_up_is_idempotent(self, tmp_path):
        path = str(tmp_path / "idem.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.up(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_issue_posts'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_down_drops_table(self, tmp_path):
        path = str(tmp_path / "down.db")
        async with aiosqlite.connect(path) as conn:
            await MIGRATION.up(conn)
            await MIGRATION.down(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_issue_posts'"
            )
            assert (await cur.fetchone())[0] == 0

    @pytest.mark.asyncio
    async def test_fresh_install_creates_table(self, tmp_path):
        from genesis.db.schema import create_all_tables

        path = str(tmp_path / "fresh.db")
        async with aiosqlite.connect(path) as conn:
            await create_all_tables(conn)
            await conn.commit()
            cur = await conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='pending_issue_posts'"
            )
            assert (await cur.fetchone())[0] == 1

    @pytest.mark.asyncio
    async def test_rejects_bad_status(self, db):
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO pending_issue_posts "
                "(id, request_id, repo, title, body, source, "
                " cell_domain, cell_verb, cell_risk_class, held_at, status) "
                "VALUES ('x','r','o/r','t','b','follow_up',"
                "'github','issue_create','bulk','t','BOGUS')"
            )

    @pytest.mark.asyncio
    async def test_rejects_bad_mode(self, db):
        # mode is CHECK-constrained to the postable lever values; 'off' (which
        # never creates a row) or any bad value is rejected at the DB.
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "INSERT INTO pending_issue_posts "
                "(id, request_id, repo, title, body, source, "
                " cell_domain, cell_verb, cell_risk_class, held_at, mode) "
                "VALUES ('x','r','o/r','t','b','follow_up',"
                "'github','issue_create','bulk','t','off')"
            )

    @pytest.mark.asyncio
    async def test_request_id_unique(self, db):
        await pip.create(db, **_ROW)
        with pytest.raises(aiosqlite.IntegrityError):
            await pip.create(db, **{**_ROW, "id": "p2"})  # same request_id


# --------------------------------------------------------------------------- #
# CRUD + transitions
# --------------------------------------------------------------------------- #
class TestCrud:
    @pytest.mark.asyncio
    async def test_create_and_get(self, db):
        await pip.create(db, **_ROW)
        row = await pip.get_by_id(db, "p1")
        assert row["status"] == "held"
        assert row["repo"] == "WingedGuardian/GENesis-AGI"
        assert row["title"] == "Fix off-by-one in chunk()"
        assert row["source"] == "follow_up"
        assert row["source_ref"] == "fu-abc"
        assert row["labels"] == '["good first issue"]'
        assert (await pip.get_by_request(db, "req-1"))["id"] == "p1"

    @pytest.mark.asyncio
    async def test_create_defaults_nullable(self, db):
        # labels + source_ref are optional; a codebase-sourced draft omits them.
        await pip.create(
            db,
            id="c1",
            request_id="req-c1",
            repo="WingedGuardian/GENesis-AGI",
            title="Add a test for parser.chunk",
            body="No test covers the empty-input case.",
            source="codebase",
            cell_domain="github",
            cell_verb="issue_create",
            cell_risk_class="bulk",
            held_at="2026-08-07T00:00:00",
            mode="live",
        )
        row = await pip.get_by_id(db, "c1")
        assert row["labels"] is None
        assert row["source_ref"] is None
        assert row["source"] == "codebase"
        assert row["mode"] == "live"  # stamped at propose time (dry-run-terminal invariant)

    @pytest.mark.asyncio
    async def test_list_held(self, db):
        await pip.create(db, **_ROW)
        await pip.create(db, **{**_ROW, "id": "p2", "request_id": "req-2"})
        held = await pip.list_held(db)
        assert {r["id"] for r in held} == {"p1", "p2"}

    @pytest.mark.asyncio
    async def test_mark_posted_once(self, db):
        await pip.create(db, **_ROW)
        assert (
            await pip.mark_posted(
                db,
                "p1",
                issue_number=42,
                issue_url="https://github.com/o/r/issues/42",
                posted_at=_TS,
            )
            is True
        )
        # double-post guard: second flip is a no-op.
        assert (
            await pip.mark_posted(
                db,
                "p1",
                issue_number=42,
                issue_url="https://github.com/o/r/issues/42",
                posted_at=_TS,
            )
            is False
        )
        row = await pip.get_by_id(db, "p1")
        assert row["status"] == "posted"
        assert row["issue_number"] == 42
        assert row["issue_url"] == "https://github.com/o/r/issues/42"
        assert row["posted_at"] == _TS
        assert await pip.list_held(db) == []

    @pytest.mark.asyncio
    async def test_mark_rejected_and_expired(self, db):
        await pip.create(db, **_ROW)
        assert await pip.mark_rejected(db, "p1", rejected_at=_TS) is True
        assert (await pip.get_by_id(db, "p1"))["status"] == "rejected"

        await pip.create(db, **{**_ROW, "id": "p2", "request_id": "req-2"})
        assert await pip.mark_rejected(db, "p2", rejected_at=_TS, expired=True) is True
        assert (await pip.get_by_id(db, "p2"))["status"] == "expired"

    @pytest.mark.asyncio
    async def test_prune_terminal_keeps_held_and_recent(self, db):
        # held row (never prunable), an OLD posted row, and a RECENT dry_run row.
        await pip.create(db, **{**_ROW, "id": "held", "request_id": "r-held"})
        await pip.create(db, **{**_ROW, "id": "old", "request_id": "r-old"})
        await pip.mark_posted(
            db, "old", issue_number=1, issue_url="u", posted_at="2026-01-01T00:00:00"
        )
        await pip.create(db, **{**_ROW, "id": "new", "request_id": "r-new"})
        await pip.mark_dry_run(db, "new", dry_run_at="2026-08-06T00:00:00")

        now = "2026-08-07T00:00:00"
        deleted = await pip.prune_terminal(db, older_than_days=30, now=now)
        assert deleted == 1  # only the old posted row
        assert await pip.get_by_id(db, "old") is None
        assert (await pip.get_by_id(db, "held"))["status"] == "held"  # held NEVER pruned
        assert (await pip.get_by_id(db, "new"))["status"] == "dry_run"  # recent kept

    @pytest.mark.asyncio
    async def test_prune_terminal_never_prunes_held_even_when_ancient(self, db):
        # a held row with an ancient held_at must survive — it awaits the owner.
        await pip.create(db, **{**_ROW, "held_at": "2020-01-01T00:00:00"})
        deleted = await pip.prune_terminal(db, older_than_days=1, now="2026-08-07T00:00:00")
        assert deleted == 0
        assert (await pip.get_by_id(db, "p1"))["status"] == "held"

    @pytest.mark.asyncio
    async def test_posted_then_reject_is_noop(self, db):
        await pip.create(db, **_ROW)
        await pip.mark_posted(
            db,
            "p1",
            issue_number=7,
            issue_url="u",
            posted_at=_TS,
        )
        # already 'posted' — reject must not override.
        assert await pip.mark_rejected(db, "p1", rejected_at=_TS) is False
        assert (await pip.get_by_id(db, "p1"))["status"] == "posted"

    @pytest.mark.asyncio
    async def test_mark_dry_run_once(self, db):
        # propose_only path: an approved hold is dry-run-terminal — shadow-observed
        # once, marked 'dry_run', and NEVER posted (flipping the lever to 'live'
        # must not retro-post it). Same single-flip guard as mark_posted.
        await pip.create(db, **_ROW)
        assert await pip.mark_dry_run(db, "p1", dry_run_at=_TS) is True
        row = await pip.get_by_id(db, "p1")
        assert row["status"] == "dry_run"
        assert row["dry_run_at"] == _TS
        # terminal: a second flip is a no-op, and it never reappears as work.
        assert await pip.mark_dry_run(db, "p1", dry_run_at=_TS) is False
        assert await pip.list_held(db) == []
        # dry_run is terminal — a later post/reject must not override it.
        assert (
            await pip.mark_posted(db, "p1", issue_number=9, issue_url="u", posted_at=_TS) is False
        )
        assert await pip.mark_rejected(db, "p1", rejected_at=_TS) is False
        assert (await pip.get_by_id(db, "p1"))["status"] == "dry_run"


# --------------------------------------------------------------------------- #
# WS-A close-loop join: posted_index_for_repo
# --------------------------------------------------------------------------- #
class TestPostedIndexForRepo:
    """issue_number → source_ref map for POSTED, follow_up-sourced rows only."""

    _REPO = "acme/widgets"  # synthetic — install-agnostic

    async def _make(self, db, *, id, source_ref, status, issue_number=None, repo=None):
        row = {**_ROW, "id": id, "request_id": f"req-{id}", "repo": repo or self._REPO}
        row["source_ref"] = source_ref
        await pip.create(db, **row)
        if status == "posted":
            await pip.mark_posted(db, id, issue_number=issue_number, issue_url="u", posted_at=_TS)
        elif status == "dry_run":
            await pip.mark_dry_run(db, id, dry_run_at=_TS)

    @pytest.mark.asyncio
    async def test_only_posted_followup_rows_indexed(self, db):
        await self._make(db, id="a", source_ref="fu-1", status="posted", issue_number=101)
        await self._make(db, id="b", source_ref="fu-2", status="held")  # not posted
        await self._make(db, id="c", source_ref=None, status="posted", issue_number=102)  # Cat-1
        await self._make(db, id="d", source_ref="fu-3", status="dry_run")  # terminal, no post
        await self._make(
            db, id="e", source_ref="fu-4", status="posted", issue_number=103, repo="other/repo"
        )
        idx = await pip.posted_index_for_repo(db, self._REPO)
        assert idx == {101: "fu-1"}  # b/c/d/e all correctly excluded

    @pytest.mark.asyncio
    async def test_empty_when_nothing_posted(self, db):
        await self._make(db, id="a", source_ref="fu-1", status="held")
        assert await pip.posted_index_for_repo(db, self._REPO) == {}
