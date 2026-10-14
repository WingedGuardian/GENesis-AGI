"""The one-time tool_trigger backfill (scripts/backfill_tool_triggers.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from genesis.learning.procedural import tool_names, trigger_cache

_SPEC = importlib.util.spec_from_file_location(
    "_backfill_tool_triggers",
    Path(__file__).resolve().parents[2] / "scripts" / "backfill_tool_triggers.py",
)
backfill_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backfill_module)

_ROWS = {
    "unchanged": ['["Bash", "Read"]', None, None],
    "cleaned": ['["Read (code review)", "review gate"]', None, None],
    "cli_mapped": ['["ssh", "Bash"]', None, None],
    "all_dropped": ['["Python", "review gate"]', None, None],
    "bare_string": ["pytest", None, None],
    "spec_kept": ['["gh"]', '{"patterns": ["own"], "repeat": "session"}', None],
    "lesson": ['["ssh"]', None, '{"origin": "owner"}'],
}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", tmp_path / "procedure_triggers.yaml")


async def _seed(db) -> None:
    for pid, (trigger, spec, lesson) in _ROWS.items():
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags, "
            "created_at, tool_trigger, trigger_spec, lesson) VALUES (?, ?, 'p', '[]', '[]', '[]', 'now', ?, ?, ?)",
            (pid, pid, trigger, spec, lesson),
        )
    await db.commit()


async def _stored(db) -> dict:
    rows = await db.execute_fetchall("SELECT id, tool_trigger, trigger_spec FROM procedural_memory")
    return {r[0]: (r[1], r[2]) for r in rows}


async def test_dry_run_counts_every_class_and_writes_nothing(db):
    await _seed(db)
    before = await _stored(db)
    counts = await backfill_module.backfill(db, apply=False)
    assert await _stored(db) == before
    assert counts["rows_scanned"] == 6  # the lesson row is never read
    # spec_kept writes no spec, so it counts as cleaned, not cli_mapped.
    assert (
        counts["unchanged"],
        counts["cleaned"],
        counts["cli_mapped"],
        counts["all_dropped"],
    ) == (1, 2, 2, 1)
    assert counts["values_seen"] == 10
    assert counts["values_dropped"] == 3


async def test_apply_repairs_each_class_and_a_rerun_changes_nothing(db):
    await _seed(db)
    await backfill_module.backfill(db, apply=True)
    stored = await _stored(db)
    assert stored["unchanged"] == ('["Bash", "Read"]', None)
    assert stored["cleaned"] == ('["Read"]', None)
    assert json.loads(stored["cli_mapped"][0]) == ["Bash"]
    assert json.loads(stored["cli_mapped"][1]) == {
        "patterns": [tool_names._CLI_PATTERNS["ssh"]],
        "repeat": "every",
    }
    assert stored["all_dropped"] == (None, None)
    assert json.loads(stored["bare_string"][0]) == ["Bash"]
    assert json.loads(stored["spec_kept"][1]) == {
        "patterns": ["own"],
        "repeat": "session",
    }  # never overwritten
    assert stored["lesson"] == ('["ssh"]', None)  # lessons are left alone
    rerun = await backfill_module.backfill(db, apply=True)
    assert await _stored(db) == stored
    assert rerun["unchanged"] == 5  # every repaired row except the all-dropped one, now unread


async def test_the_entry_point_applies_and_regenerates_on_a_plain_connection(tmp_path):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "t.db"
    async with aiosqlite.connect(str(path)) as db:
        await create_all_tables(db)
        await _seed(db)
    assert await backfill_module._main(path, apply=True) == 0
    assert trigger_cache.get_json_cache_path().exists()


async def test_the_entry_point_refuses_an_unmigrated_database(tmp_path):
    import aiosqlite

    path = tmp_path / "old.db"
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("CREATE TABLE procedural_memory (id TEXT, tool_trigger TEXT)")
    assert await backfill_module._main(path, apply=True) == 1


class _RefineAfterRead:
    """The connection, with a server refine landing between the read and the writes."""

    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    async def execute_fetchall(self, sql, *args):
        rows = await self._db.execute_fetchall(sql, *args)
        await self._db.execute(
            "UPDATE procedural_memory SET tool_trigger = '[\"Edit\"]' WHERE id = 'cleaned'"
        )
        return rows


async def test_a_row_changed_after_the_read_is_kept(db):
    await _seed(db)
    counts = await backfill_module.backfill(_RefineAfterRead(db), apply=True)
    assert counts["skipped_changed"] == 1
    assert (await _stored(db))["cleaned"][0] == '["Edit"]'
