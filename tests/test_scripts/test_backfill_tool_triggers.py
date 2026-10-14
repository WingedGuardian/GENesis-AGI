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
    "cli_mapped": ['["ssh"]', None, None],
    "all_dropped": ['["Python", "review gate"]', None, None],
    "bare_string": ["pytest", None, None],
    "spec_kept": ['["gh"]', '{"patterns": ["own"], "repeat": "session"}', None],
    "lesson": ['["ssh"]', None, '{"origin": "owner"}'],
    "empty": ["[]", None, None],
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
    assert counts["rows_scanned"] == 7  # the lesson row is never read
    # spec_kept writes no spec, so it counts as cleaned, not cli_mapped.
    assert (
        counts["unchanged"],
        counts["cleaned"],
        counts["cli_mapped"],
        counts["all_dropped"],
    ) == (2, 1, 2, 2)  # [] is all_dropped; spec_kept keeps its word
    assert counts["values_seen"] == 9
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


async def test_the_entry_point_applies_and_regenerates_on_a_plain_connection(tmp_path, monkeypatch):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "t.db"
    async with aiosqlite.connect(str(path)) as db:
        await create_all_tables(db)
        await _seed(db)
    monkeypatch.setenv("GENESIS_PROCEDURE_TRIGGERS", str(trigger_cache.get_cache_path()))
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


async def test_a_stored_empty_list_becomes_null(db):
    await _seed(db)
    await backfill_module.backfill(db, apply=True)
    assert (await _stored(db))["empty"] == (None, None)


class _SpecAfterRead(_RefineAfterRead):
    async def execute_fetchall(self, sql, *args):
        rows = await self._db.execute_fetchall(sql, *args)
        await self._db.execute(
            'UPDATE procedural_memory SET trigger_spec = \'{"patterns": ["x"], "repeat": "every"}\' '
            "WHERE id = 'cli_mapped'"
        )
        await self._db.execute("UPDATE procedural_memory SET lesson = '{}' WHERE id = 'cleaned'")
        return rows


async def test_a_spec_or_lesson_set_after_the_read_is_kept(db):
    await _seed(db)
    counts = await backfill_module.backfill(_SpecAfterRead(db), apply=True)
    stored = await _stored(db)
    assert json.loads(stored["cli_mapped"][1])["patterns"] == ["x"]
    assert stored["cleaned"][0] == '["Read (code review)", "review gate"]'
    assert counts["skipped_changed"] == 2


async def test_a_spec_is_written_only_for_a_bash_only_row_and_only_when_valid(db):
    for pid, trigger in (
        ("mixed", '["Read", "ssh"]'),
        ("named_bash", '["Bash", "SSH"]'),
        ("annotated_bash", '["Bash (running tests)", "ssh"]'),
        (
            "too_many",
            '["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git add"]',
        ),
    ):
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags, "
            "created_at, tool_trigger) VALUES (?, ?, 'p', '[]', '[]', '[]', 'now', ?)",
            (pid, pid, trigger),
        )
    counts = await backfill_module.backfill(db, apply=True)
    stored = await _stored(db)
    assert json.loads(stored["mixed"][0]) == ["Read", "ssh"]  # kept as written, not widened
    assert stored["mixed"][1] is None  # a pattern would stop Read surfacing
    assert len(json.loads(stored["too_many"][0])) == 9  # kept as written, not widened
    assert stored["too_many"][1] is None  # nine patterns: over the validator's cap
    assert counts["spec_refused"] == 1
    assert json.loads(stored["named_bash"][0]) == ["Bash", "SSH"]
    assert json.loads(stored["annotated_bash"][0]) == ["Bash", "ssh"]
    assert stored["annotated_bash"][1] is None  # annotated Bash is still Bash named
    assert stored["named_bash"][1] is None  # it named Bash: every Bash call, not only ssh


async def test_an_alternate_database_does_not_rebuild_the_install_cache(tmp_path, monkeypatch):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    monkeypatch.delenv("GENESIS_PROCEDURE_TRIGGERS", raising=False)
    path = tmp_path / "copy.db"
    async with aiosqlite.connect(str(path)) as db:
        await create_all_tables(db)
        await _seed(db)
    assert await backfill_module._main(path, apply=True) == 0
    assert not trigger_cache.get_json_cache_path().exists()


async def test_a_mistyped_database_path_is_refused_not_created(tmp_path):
    path = tmp_path / "missing.db"
    assert await backfill_module._main(path, apply=False) == 2
    assert not path.exists()


async def test_a_path_with_uri_characters_opens_that_file(tmp_path, monkeypatch):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    monkeypatch.setenv("GENESIS_PROCEDURE_TRIGGERS", str(trigger_cache.get_cache_path()))
    path = tmp_path / "copy?old#1.db"
    async with aiosqlite.connect(str(path)) as db:
        await create_all_tables(db)
        await _seed(db)
    assert await backfill_module._main(path, apply=True) == 0
    assert not (tmp_path / "copy").exists()  # the prefix was never created
    async with aiosqlite.connect(str(path)) as db:
        rows = await db.execute_fetchall(
            "SELECT tool_trigger FROM procedural_memory WHERE id = 'cleaned'"
        )
    assert rows[0][0] == '["Read"]'


async def test_a_fenced_database_is_not_opened(tmp_path, monkeypatch):
    import sys
    import types

    fence = types.ModuleType("db_admission_check")
    fence.database_is_fenced = lambda _path: True
    monkeypatch.setitem(sys.modules, "db_admission_check", fence)
    path = tmp_path / "fenced.db"
    path.write_bytes(b"")
    assert await backfill_module._main(path, apply=True) == 3
