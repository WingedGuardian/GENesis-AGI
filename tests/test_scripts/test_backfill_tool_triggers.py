"""The one-time tool_trigger backfill (scripts/backfill_tool_triggers.py)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from genesis.learning.procedural import trigger_cache

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
    # spec_kept already has a spec: its command word stays as written, unchanged.
    assert (
        counts["unchanged"],
        counts["cleaned"],
        counts["cli_mapped"],
        counts["all_dropped"],
    ) == (2, 1, 2, 2)  # [] is all_dropped
    assert counts["cli_kept"] == 0  # spec_kept keeps its word unchanged: not a change
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
        "patterns": [],
        "commands": ["ssh"],
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
    assert await backfill_module._main(path, apply=True, rebuild_cache=True) == 0
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
    assert counts["cleaned"] == 0  # a class is counted only once its write lands
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
    assert counts["cli_mapped"] == 1  # bare_string only


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
    assert stored["mixed"][1] is None  # a spec would stop Read surfacing
    assert len(json.loads(stored["too_many"][0])) == 9  # kept as written, not widened
    assert stored["too_many"][1] is None  # nine commands: over the validator's cap
    assert counts["unchanged"] == 2  # mixed and too_many: already kept as written
    assert counts["spec_refused"] == 0
    # A row naming Bash already fires on every Bash call: the command word goes,
    # and no spec narrows it (the writers' rule, operations.commands_spec).
    assert json.loads(stored["named_bash"][0]) == ["Bash"]
    assert json.loads(stored["annotated_bash"][0]) == ["Bash"]
    assert stored["annotated_bash"][1] is None
    assert stored["named_bash"][1] is None


async def test_the_backfill_and_both_writers_store_the_same_trigger(db):
    from genesis.learning.procedural.operations import store_procedure, store_procedure_checked

    triggers = (
        ["ssh"],
        ["Read", "ssh"],
        ["Bash", "ssh"],
        ["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git add"],
        ["pytest", "mutation sweep", "review gate"],
    )
    for i, trigger in enumerate(triggers):
        plain = await store_procedure(
            db,
            task_type=f"w{i}",
            principle="p",
            steps=[],
            tools_used=[],
            context_tags=[],
            tool_trigger=trigger,
        )
        checked = await store_procedure_checked(
            db,
            task_type=f"c{i}",
            principle="p",
            steps=[],
            tools_used=[],
            context_tags=[],
            tool_trigger=trigger,
        )
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags, "
            "created_at, tool_trigger) VALUES (?, ?, 'p', '[]', '[]', '[]', 'now', ?)",
            (f"b{i}", f"b{i}", json.dumps(trigger)),
        )
        written = await _stored(db)  # what the writers stored, before the backfill
        await backfill_module.backfill(db, apply=True, regenerate_cache=False)
        stored = await _stored(db)
        assert written[plain] == written[checked.procedure_id], trigger
        assert stored[f"b{i}"] == written[plain], trigger
        assert stored[plain] == written[plain], trigger  # a writer's row is a fixed point


async def test_each_change_is_printed_with_what_it_drops(db, capsys):
    await _seed(db)
    await backfill_module.backfill(db, apply=False)
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    by_id = {x["id"]: x for x in lines}
    assert set(by_id) == {"cleaned", "cli_mapped", "all_dropped", "bare_string", "empty"}
    assert by_id["all_dropped"] == {
        "id": "all_dropped",
        "kind": "all_dropped",
        "from": '["Python", "review gate"]',
        "to": {"tool_trigger": None},
        "dropped": ["Python", "review gate"],
    }
    assert json.loads(by_id["cli_mapped"]["to"]["trigger_spec"])["commands"] == ["ssh"]


async def test_a_failed_cache_rebuild_after_the_commit_exits_4(tmp_path, monkeypatch, capsys):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    async def broken(_db):
        raise OSError("disk full")

    monkeypatch.setattr(trigger_cache, "regenerate", broken)
    path = tmp_path / "t.db"
    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await _seed(conn)
    assert await backfill_module._main(path, apply=True, rebuild_cache=True) == 4
    assert "Repairs committed" in capsys.readouterr().out
    async with aiosqlite.connect(str(path)) as conn:
        rows = await conn.execute_fetchall(
            "SELECT tool_trigger FROM procedural_memory WHERE id = 'cleaned'"
        )
    assert rows[0][0] == '["Read"]'  # the repair stands


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

    path = tmp_path / "copy?old#1.db"
    async with aiosqlite.connect(str(path)) as db:
        await create_all_tables(db)
        await _seed(db)
    assert await backfill_module._main(path, apply=True, rebuild_cache=True) == 0
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


async def test_a_dry_run_never_opens_the_database_for_writing(tmp_path, monkeypatch):
    import genesis.db.connection as connection

    def refuse(*_a, **_k):
        raise AssertionError("a dry run opened the database read-write")

    monkeypatch.setattr(connection, "connect_aiosqlite_rw", refuse)
    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await _seed(conn)
    assert await backfill_module._main(path, apply=False) == 0


async def _migrated(tmp_path):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await _seed(conn)
    return path


async def test_a_dry_run_connection_refuses_a_write(tmp_path, monkeypatch):
    import sqlite3

    attempted = []

    async def write(db, **_kw):
        attempted.append(True)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            await db.execute("UPDATE procedural_memory SET tool_trigger = NULL")
        return backfill_module.Counter()

    monkeypatch.setattr(backfill_module, "backfill", write)
    assert await backfill_module._main(await _migrated(tmp_path), apply=False) == 0
    assert attempted


async def test_an_unimportable_fence_check_is_treated_as_fenced(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "db_admission_check", None)  # import raises
    assert await backfill_module._main(await _migrated(tmp_path), apply=True) == 3


async def test_an_apply_open_failure_that_is_not_the_fence_exits_2(tmp_path, monkeypatch):
    import genesis.db.connection as connection

    def broken(*_a, **_k):
        raise OSError("disk I/O error")

    monkeypatch.setattr(connection, "connect_aiosqlite_rw", broken)
    assert await backfill_module._main(await _migrated(tmp_path), apply=True) == 2


async def test_an_apply_the_guarded_open_refuses_exits_3(tmp_path, monkeypatch):
    import genesis.db.connection as connection
    from genesis.db.integrity import DatabaseIntegrityError

    def quarantined(*_a, **_k):
        raise DatabaseIntegrityError("database is quarantined")

    monkeypatch.setattr(connection, "connect_aiosqlite_rw", quarantined)
    import aiosqlite

    from genesis.db.schema import create_all_tables

    path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await _seed(conn)
    assert await backfill_module._main(path, apply=True) == 3


@pytest.mark.parametrize("apply", [False, True])
async def test_a_file_that_is_not_a_database_exits_2(tmp_path, monkeypatch, capsys, apply):
    import sys
    import types

    fence = types.ModuleType("db_admission_check")
    fence.database_is_fenced = lambda _path: False
    monkeypatch.setitem(sys.modules, "db_admission_check", fence)
    monkeypatch.setattr("genesis.db.admission.assert_admitted", lambda _path: None)
    path = tmp_path / "genesis.db"
    path.write_bytes(b"not a database " * 512)
    assert await backfill_module._main(path, apply=apply) == 2
    out = capsys.readouterr().out
    assert "not opened" in out or "unreadable or busy" in out


async def test_a_database_locked_through_the_apply_exits_2_and_writes_nothing(
    tmp_path, monkeypatch, capsys
):
    import sqlite3

    import aiosqlite

    monkeypatch.setenv("GENESIS_DB_BUSY_TIMEOUT_MS", "100")

    path = await _migrated(tmp_path)
    holder = sqlite3.connect(path, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert await backfill_module._main(path, apply=True) == 2
    finally:
        holder.rollback()
        holder.close()
    assert "nothing written" in capsys.readouterr().out
    async with aiosqlite.connect(str(path)) as conn:
        rows = await conn.execute_fetchall(
            "SELECT tool_trigger FROM procedural_memory WHERE id = 'cleaned'"
        )
    assert rows[0][0] == '["Read (code review)", "review gate"]'


async def test_the_live_database_rebuilds_the_cache_however_its_path_is_given(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_PROCEDURE_TRIGGERS", raising=False)
    checkout = tmp_path / "checkout"
    (checkout / "data").mkdir(parents=True)
    live = await _migrated(checkout / "data")  # <checkout>/data/genesis.db
    monkeypatch.setattr(backfill_module, "_checkout", lambda: checkout)
    # An explicit --db naming the live file, through a path that is not literal.
    assert await backfill_module._main(checkout / "data" / ".." / "data" / "genesis.db", apply=True) == 0
    assert live.exists() and trigger_cache.get_json_cache_path().exists()


async def test_rebuild_cache_rebuilds_from_a_database_elsewhere(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_PROCEDURE_TRIGGERS", raising=False)
    path = await _migrated(tmp_path)
    assert await backfill_module._main(path, apply=True, rebuild_cache=True) == 0
    assert trigger_cache.get_json_cache_path().exists()


async def test_a_partly_migrated_database_is_refused_as_unmigrated(tmp_path, capsys):
    import aiosqlite

    path = tmp_path / "partial.db"
    async with aiosqlite.connect(str(path)) as db:
        await db.execute(
            "CREATE TABLE procedural_memory (id TEXT, tool_trigger TEXT, lesson TEXT, deprecated INTEGER)"
        )
    assert await backfill_module._main(path, apply=False) == 1
    assert "lacks trigger_spec" in capsys.readouterr().out


class _CommitFails(_RefineAfterRead):
    async def execute_fetchall(self, sql, *args):
        return await self._db.execute_fetchall(sql, *args)

    async def commit(self):
        import sqlite3

        raise sqlite3.OperationalError("database or disk is full")


async def test_applied_rows_are_printed_only_after_the_commit_lands(db, capsys):
    import sqlite3

    await _seed(db)
    with pytest.raises(sqlite3.OperationalError):
        await backfill_module.backfill(_CommitFails(db), apply=True, regenerate_cache=False)
    assert '{"id"' not in capsys.readouterr().out  # nothing advertised as applied


async def test_a_closed_output_never_skips_the_cache_rebuild(tmp_path, monkeypatch):
    import builtins

    real = builtins.print

    def closed_pipe(*args, **kwargs):
        if args and str(args[0]).startswith('{"id"'):
            raise BrokenPipeError
        real(*args, **kwargs)

    monkeypatch.setattr(builtins, "print", closed_pipe)
    path = await _migrated(tmp_path)
    assert await backfill_module._main(path, apply=True, rebuild_cache=True) == 0
    assert trigger_cache.get_json_cache_path().exists()


async def test_a_blob_trigger_is_reported_not_fatal(db, capsys):
    await db.execute(
        "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags, "
        "created_at, tool_trigger) VALUES ('blob', 'b', 'p', '[]', '[]', '[]', 'now', ?)",
        (b"\xff\xfe",),
    )
    counts = await backfill_module.backfill(db, apply=False)
    assert counts["all_dropped"] == 1
    assert '"id": "blob"' in capsys.readouterr().out


def test_a_closed_stdout_keeps_the_exit_code(tmp_path):
    import os
    import subprocess
    import sys

    read_end, write_end = os.pipe()
    os.close(read_end)  # the reader is gone before the first line
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(root / "src"), os.environ.get("PYTHONPATH", "")])}
    try:
        done = subprocess.run(
            [sys.executable, str(root / "scripts" / "backfill_tool_triggers.py"), "--db", str(tmp_path / "missing.db")],
            stdout=write_end,
            stderr=subprocess.PIPE,
            env=env,
            timeout=120,
        )
    finally:
        os.close(write_end)
    assert done.returncode == 2, done.stderr.decode()  # "No database", not a traceback or 120
    assert b"Traceback" not in done.stderr
