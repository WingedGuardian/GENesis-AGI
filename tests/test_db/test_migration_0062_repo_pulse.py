"""Migration 0062 + CRUD — repo_pulse runs/annotations (PR-4a annotator store).

Verifies tables/columns/indexes/idempotency/down, the subprocess
table-existence guard (pre-migration no-op, cursor-preserving), the
run+annotations single-transaction write, the unique-index dedupe on
re-covered enumeration windows, validation, the proposed→terminal
resolution lifecycle, and retention pruning.
"""

from __future__ import annotations

import importlib

import aiosqlite
import pytest

from genesis.db.crud import repo_pulse as crud

M62 = importlib.import_module("genesis.db.migrations.0062_repo_pulse")

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
ITEM = "0123456789abcdef0123456789abcdef"


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cur = await db.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cur.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return await cur.fetchone() is not None


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "t.db")) as conn:
        conn.row_factory = aiosqlite.Row
        # the guard caches TRUE per-process — reset between tests
        crud._tables_verified = False
        yield conn
        crud._tables_verified = False


def _run_kwargs(**over):
    kw = dict(
        run_id="r1",
        started_at="2026-07-16T12:00:00+00:00",
        finished_at="2026-07-16T12:00:20+00:00",
        trigger="manual",
        repo="owner/repo",
        cursor_before="2026-07-09T00:00:00+00:00",
        cursor_after="2026-07-16T11:00:00+00:00",
        status="ok",
        n_prs=12,
        n_open_items=3,
        latency_ms=8000,
        prompt_version="v1",
        model="claude-haiku-4-5-20251001",
    )
    kw.update(over)
    return kw


def _annotation(**over):
    ann = dict(
        id="a1",
        observed_at="2026-07-16T12:00:20+00:00",
        tier="fuzzy",
        item_id=ITEM,
        item_session_id=SID,
        item_text="ship the repo-pulse annotator",
        pr_number=1080,
        pr_title="feat(session): repo-pulse annotator",
        pr_merged_at="2026-07-16T10:00:00+00:00",
        confidence=0.85,
        rationale="title names the same component",
        status="proposed",
    )
    ann.update(over)
    return ann


# ── migration shape ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_creates_tables_with_expected_columns(db):
    await M62.up(db)
    assert await _columns(db, "repo_pulse_runs") == {
        "run_id",
        "started_at",
        "finished_at",
        "trigger",
        "repo",
        "cursor_before",
        "cursor_after",
        "status",
        "n_prs",
        "n_open_items",
        "n_exact",
        "n_fuzzy",
        "latency_ms",
        "prompt_version",
        "model",
        "mode",
        "detail",
    }
    assert await _columns(db, "repo_pulse_annotations") == {
        "id",
        "run_id",
        "observed_at",
        "tier",
        "item_id",
        "item_session_id",
        "item_text",
        "pr_number",
        "pr_title",
        "pr_merged_at",
        "confidence",
        "rationale",
        "status",
        "resolved_at",
        "resolution_ref",
    }


@pytest.mark.asyncio
async def test_up_idempotent_and_down(db):
    await M62.up(db)
    await M62.up(db)  # IF NOT EXISTS — no raise
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_rp%'"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert names == {
        "idx_rpa_dedupe",
        "idx_rpa_status",
        "idx_rpa_session",
        "idx_rpr_started",
    }
    await M62.down(db)
    assert not await _table_exists(db, "repo_pulse_runs")
    assert not await _table_exists(db, "repo_pulse_annotations")


@pytest.mark.asyncio
async def test_status_and_tier_checks_enforced(db):
    await M62.up(db)
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO repo_pulse_runs (run_id, started_at, trigger, status) "
            "VALUES ('r', 't', 'manual', 'bogus')"
        )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO repo_pulse_annotations "
            "(id, run_id, observed_at, tier, item_id, pr_number, status) "
            "VALUES ('a', 'r', 't', 'bogus', 'i', 1, 'proposed')"
        )
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO repo_pulse_annotations "
            "(id, run_id, observed_at, tier, item_id, pr_number, status) "
            "VALUES ('a', 'r', 't', 'exact', 'i', 1, 'bogus')"
        )


# ── CRUD ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_run_with_annotations_roundtrip(db):
    await M62.up(db)
    ok = await crud.record_run(db, **_run_kwargs(n_fuzzy=1), annotations=[_annotation()])
    assert ok is True
    runs = await crud.list_runs(db)
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["mode"] == "live"
    assert runs[0]["cursor_after"] == "2026-07-16T11:00:00+00:00"
    anns = await crud.list_annotations(db, session_id=SID)
    assert len(anns) == 1
    ann = anns[0]
    assert ann["run_id"] == "r1"
    assert ann["item_id"] == ITEM
    assert ann["pr_number"] == 1080
    assert ann["status"] == "proposed"


@pytest.mark.asyncio
async def test_zero_match_run_still_recorded(db):
    """no_new_prs runs must exist — they prove the cursor advanced honestly."""
    await M62.up(db)
    assert await crud.record_run(db, **_run_kwargs(status="no_new_prs", n_prs=0))
    runs = await crud.list_runs(db)
    assert runs[0]["status"] == "no_new_prs"
    assert await crud.list_annotations(db) == []


@pytest.mark.asyncio
async def test_unique_index_dedupes_recovered_windows(db):
    """A re-covered enumeration window re-observing the same
    (tier, item_id, pr_number) match is absorbed, never duplicated."""
    await M62.up(db)
    await crud.record_run(db, **_run_kwargs(run_id="r1"), annotations=[_annotation(id="a1")])
    await crud.record_run(
        db,
        **_run_kwargs(run_id="r2"),
        annotations=[_annotation(id="a2"), _annotation(id="a3", pr_number=1081)],
    )
    anns = await crud.list_annotations(db)
    assert len(anns) == 2  # a2 ignored (same tier/item/pr as a1); a3 is new
    assert {a["id"] for a in anns} == {"a1", "a3"}
    # both run rows exist regardless
    assert len(await crud.list_runs(db)) == 2


@pytest.mark.asyncio
async def test_dedupe_does_not_resurrect_resolved_proposals(db):
    """Once a proposal is rejected, a later run re-observing the same match
    must NOT flip it back to proposed (INSERT OR IGNORE leaves it be)."""
    await M62.up(db)
    await crud.record_run(db, **_run_kwargs(run_id="r1"), annotations=[_annotation(id="a1")])
    assert await crud.resolve_annotation(
        db, "a1", status="rejected", resolved_at="2026-07-16T13:00:00+00:00"
    )
    await crud.record_run(db, **_run_kwargs(run_id="r2"), annotations=[_annotation(id="a2")])
    anns = await crud.list_annotations(db)
    assert len(anns) == 1
    assert anns[0]["id"] == "a1"
    assert anns[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_pre_migration_guard_noops(db):
    """Subprocess writer against an un-migrated DB: no-op, no create, and
    the caller can see False (so it must NOT advance its cursor)."""
    assert await crud.record_run(db, **_run_kwargs()) is False
    assert not await _table_exists(db, "repo_pulse_runs")
    # self-heal: once the migration lands the same process writes fine
    await M62.up(db)
    assert await crud.record_run(db, **_run_kwargs()) is True


@pytest.mark.asyncio
async def test_validation_rejects_bad_values_before_any_insert(db):
    await M62.up(db)
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(status="bogus"))
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), annotations=[_annotation(tier="bogus")])
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), annotations=[_annotation(status="bogus")])
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), annotations=[_annotation(item_id="")])
    with pytest.raises(ValueError):
        await crud.record_run(db, **_run_kwargs(), annotations=[_annotation(pr_number="1080")])
    # validate-before-first-INSERT: nothing was stranded on the connection
    assert await crud.list_runs(db) == []
    assert await crud.list_annotations(db) == []


# ── resolution lifecycle ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_annotation_lifecycle(db):
    await M62.up(db)
    await crud.record_run(db, **_run_kwargs(), annotations=[_annotation(id="a1")])
    assert await crud.resolve_annotation(
        db,
        "a1",
        status="confirmed",
        resolved_at="2026-07-16T13:00:00+00:00",
        resolution_ref="PR #1080: absorbed via session_ledger_update",
    )
    ann = (await crud.list_annotations(db))[0]
    assert ann["status"] == "confirmed"
    assert ann["resolved_at"] == "2026-07-16T13:00:00+00:00"
    # terminal rows never flip
    assert not await crud.resolve_annotation(
        db, "a1", status="rejected", resolved_at="2026-07-16T14:00:00+00:00"
    )
    assert (await crud.list_annotations(db))[0]["status"] == "confirmed"


@pytest.mark.asyncio
async def test_resolve_rejects_applied_rows_and_bad_statuses(db):
    await M62.up(db)
    await crud.record_run(
        db,
        **_run_kwargs(),
        annotations=[_annotation(id="a1", tier="exact", status="applied", confidence=None)],
    )
    # applied rows are exact-tier facts — not resolvable
    assert not await crud.resolve_annotation(
        db, "a1", status="confirmed", resolved_at="2026-07-16T13:00:00+00:00"
    )
    with pytest.raises(ValueError):
        await crud.resolve_annotation(
            db, "a1", status="applied", resolved_at="2026-07-16T13:00:00+00:00"
        )


# ── summary + prune ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_counts_and_precision(db):
    await M62.up(db)
    await crud.record_run(
        db,
        **_run_kwargs(run_id="r1"),
        annotations=[
            _annotation(id="a1"),
            _annotation(id="a2", pr_number=1081),
            _annotation(id="a3", pr_number=1082),
            _annotation(id="a4", tier="exact", status="applied", pr_number=1083),
        ],
    )
    await crud.record_run(db, **_run_kwargs(run_id="r2", status="failed", detail="gh_exit_1"))
    await crud.resolve_annotation(
        db, "a1", status="confirmed", resolved_at="2026-07-16T13:00:00+00:00"
    )
    await crud.resolve_annotation(
        db, "a2", status="rejected", resolved_at="2026-07-16T13:00:00+00:00"
    )
    s = await crud.summary(db)
    assert s["runs"] == {"ok": 1, "failed": 1}
    assert s["annotations"] == {
        "fuzzy/confirmed": 1,
        "fuzzy/rejected": 1,
        "fuzzy/proposed": 1,
        "exact/applied": 1,
    }
    assert s["precision"] == 0.5


@pytest.mark.asyncio
async def test_prune_deletes_old_runs_and_annotations(db):
    await M62.up(db)
    await crud.record_run(
        db,
        **_run_kwargs(run_id="old", started_at="2026-05-01T00:00:00+00:00"),
        annotations=[_annotation(id="a-old", observed_at="2026-05-01T00:00:10+00:00")],
    )
    await crud.record_run(
        db,
        **_run_kwargs(run_id="new", started_at="2026-07-10T00:00:00+00:00"),
        annotations=[
            _annotation(id="a-new", observed_at="2026-07-10T00:00:10+00:00", pr_number=1081)
        ],
    )
    deleted = await crud.prune_repo_pulse(db, older_than_days=45, now="2026-07-16T00:00:00+00:00")
    assert deleted == 2
    assert [r["run_id"] for r in await crud.list_runs(db)] == ["new"]
    assert [a["id"] for a in await crud.list_annotations(db)] == ["a-new"]


@pytest.mark.asyncio
async def test_prune_noops_pre_migration(db):
    assert await crud.prune_repo_pulse(db, now="2026-07-16T00:00:00+00:00") == 0
