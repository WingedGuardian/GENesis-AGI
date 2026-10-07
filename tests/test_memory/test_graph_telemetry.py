"""Durable graph-traversal telemetry (A1a of the FalkorDB cutover gate).

The FalkorDB default-on cutover waits on 14 days with zero fallbacks, measured
from durable rows rather than logs (MCP processes log to stderr, which never
reaches the journal). These tests pin what ``graph.traverse`` records: one
``eval_events`` row per tally (one caller call), the outcome of every traversal
inside it, and nothing that could make a failure disappear silently.
"""

from __future__ import annotations

import asyncio
import json
import logging

import aiosqlite
import pytest

from genesis.memory import graph as graph_mod
from genesis.memory import graph_telemetry as telemetry
from genesis.memory.graphstore import GraphModeUnsupported, GraphUnavailableError


@pytest.fixture(autouse=True)
def _telemetry_on(monkeypatch, tmp_path):
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setattr(telemetry, "_write_failures", 0)
    # Lost-write lines go under genesis_home(); keep them in the test tmp dir.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "genesis_home"))


def _lost_lines() -> list[dict]:
    path = telemetry.lost_writes_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class _OkStore:
    def __init__(self, name: str = "falkordb"):
        self.name = name
        self.calls = 0

    async def traverse(self, db, root_id, *, max_depth, min_strength, include_deprecated=False):
        self.calls += 1
        return []

    async def centrality(self, db, top_n):
        return []

    def invalidate(self) -> None:
        pass


class _DeadStore(_OkStore):
    """Fails the way FalkorDB's own wrapper fails: the seam type, with the real
    cause chained (``graphstore_falkor._bounded``)."""

    async def traverse(self, db, root_id, *, max_depth, min_strength, include_deprecated=False):
        self.calls += 1
        raise GraphUnavailableError("engine not answering") from ConnectionRefusedError("socket")


class _RaisingStore(_OkStore):
    def __init__(self, exc: BaseException, name: str = "falkordb"):
        super().__init__(name)
        self._exc = exc

    async def traverse(self, db, root_id, *, max_depth, min_strength, include_deprecated=False):
        self.calls += 1
        raise self._exc


async def _rows(db) -> list[dict]:
    cur = await db.execute(
        "SELECT dimension, metrics_json FROM eval_events WHERE event_type = ?",
        (telemetry.TELEMETRY_EVENT_TYPE,),
    )
    rows = await cur.fetchall()
    assert all(r[0] == "system" for r in rows)
    return [json.loads(r[1]) for r in rows]


@pytest.mark.asyncio
async def test_an_untallied_traversal_writes_its_own_row(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())

    await graph_mod.traverse(db, "root-1")

    [row] = await _rows(db)
    assert row["traversals"] == 1
    assert row["outcomes"] == {"primary": 1}
    assert row["served"] == {"falkordb": 1}
    assert row["caller"] == "direct"
    assert row["events"] == []
    assert row["prior_write_failures"] == 0


@pytest.mark.asyncio
async def test_a_tally_writes_one_row_for_all_its_traversals(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())

    async with telemetry.traversal_tally(db, caller="recall"):
        for i in range(3):
            await graph_mod.traverse(db, f"root-{i}")
        assert await _rows(db) == [], "rows must not land before the tally closes"

    [row] = await _rows(db)
    assert row["caller"] == "recall"
    assert row["traversals"] == 3
    assert row["outcomes"] == {"primary": 3}


@pytest.mark.asyncio
async def test_a_tally_with_no_traversals_writes_nothing(db):
    async with telemetry.traversal_tally(db, caller="recall"):
        pass
    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_an_inner_tally_joins_the_outer_one(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())

    async with telemetry.traversal_tally(db, caller="ambient"):
        async with telemetry.traversal_tally(db, caller="drift"):
            await graph_mod.traverse(db, "a")
        await graph_mod.traverse(db, "b")

    [row] = await _rows(db)
    assert row["caller"] == "ambient"
    assert row["traversals"] == 2


@pytest.mark.asyncio
async def test_begin_and_end_record_the_same_as_the_context_manager(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())

    token = telemetry.begin_tally("expand")
    await graph_mod.traverse(db, "a")
    await telemetry.end_tally(token, db)

    [row] = await _rows(db)
    assert (row["caller"], row["traversals"]) == ("expand", 1)


@pytest.mark.asyncio
async def test_a_fallback_to_networkx_is_recorded_with_its_cause(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"fallback": 1}
    assert row["served"] == {"networkx": 1}
    [event] = row["events"]
    assert event["outcome"] == "fallback"
    assert event["served_by"] == "networkx"
    assert event["primary_reason"] == "ConnectionRefusedError"
    assert event["final_reason"] is None


@pytest.mark.asyncio
async def test_a_fallback_all_the_way_to_the_cte_is_recorded(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _DeadStore(name="networkx"))

    await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"fallback": 1}
    assert row["served"] == {"cte": 1}


@pytest.mark.asyncio
async def test_a_declined_mode_routed_to_the_cte_is_not_a_fallback(db, monkeypatch):
    decliner = _RaisingStore(GraphModeUnsupported("no hidden mode"), name="networkx")
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: decliner)

    await graph_mod.traverse(db, "root", include_deprecated=True)

    [row] = await _rows(db)
    assert row["outcomes"] == {"mode_unsupported": 1}
    assert row["served"] == {"cte": 1}
    assert row["events"] == [], "expected routing must not read as a clock-breaking event"


@pytest.mark.asyncio
async def test_an_escaping_error_is_recorded_and_still_raised(db, monkeypatch):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _RaisingStore(ValueError("bad row")))

    with pytest.raises(ValueError, match="bad row"):
        await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"error": 1}
    assert row["served"] == {"none": 1}
    [event] = row["events"]
    assert event["final_reason"] == "ValueError"


@pytest.mark.asyncio
async def test_a_cte_failure_after_a_fallback_is_an_error(db, monkeypatch):
    async def _boom(*a, **k):
        raise sqlite_locked()

    def sqlite_locked():
        import sqlite3

        return sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _DeadStore(name="networkx"))
    monkeypatch.setattr(graph_mod, "_traverse_cte", _boom)

    with pytest.raises(GraphUnavailableError):
        await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"error": 1}
    [event] = row["events"]
    assert event["primary_reason"] == "ConnectionRefusedError"
    assert event["final_reason"] == "OperationalError"


@pytest.mark.asyncio
async def test_cancellation_is_recorded_as_neutral_and_reraised(db, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "_traversal_store", lambda: _RaisingStore(asyncio.CancelledError())
    )

    with pytest.raises(asyncio.CancelledError):
        async with telemetry.traversal_tally(db, caller="recall"):
            await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"cancelled": 1}
    assert row["events"] == []


@pytest.mark.asyncio
async def test_the_configured_mode_is_the_one_selection_used(db, monkeypatch):
    from genesis.memory import graphstore_config

    monkeypatch.setattr(graphstore_config, "effective_mode", lambda: "falkordb")
    monkeypatch.setattr(graph_mod, "_falkor_store", _OkStore())

    await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["configured"] == {"falkordb": 1}
    assert row["served"] == {"falkordb": 1}


@pytest.mark.asyncio
async def test_a_networkx_configuration_is_recorded_as_such(db, monkeypatch):
    from genesis.memory import graphstore_config

    monkeypatch.setattr(graphstore_config, "effective_mode", lambda: "networkx")
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["configured"] == {"networkx": 1}
    assert row["outcomes"] == {"primary": 1}


@pytest.mark.asyncio
async def test_a_selection_failure_is_recorded(db, monkeypatch):
    from genesis.memory import graphstore_config, graphstore_falkor

    def _broken(*a, **k):
        raise ImportError("no falkordb client")

    monkeypatch.setattr(graphstore_config, "effective_mode", lambda: "falkordb")
    monkeypatch.setattr(graph_mod, "_falkor_store", None)
    monkeypatch.setattr(graphstore_falkor, "FalkorGraphStore", _broken)
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"selection_failed": 1}
    assert row["configured"] == {"falkordb": 1}
    assert row["served"] == {"networkx": 1}
    [event] = row["events"]
    assert event["primary_reason"] == "ImportError"


@pytest.mark.asyncio
async def test_the_kill_switch_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("GENESIS_GRAPH_TELEMETRY_DISABLED", "1")
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    async with telemetry.traversal_tally(db, caller="recall"):
        await graph_mod.traverse(db, "root")

    assert await _rows(db) == []


@pytest.mark.asyncio
async def test_a_failed_write_never_changes_the_traversal_and_is_carried_forward(
    db, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())
    bare = await aiosqlite.connect(tmp_path / "no_eval_events.db")
    try:
        with caplog.at_level(logging.WARNING, logger="genesis.memory.graph_telemetry"):
            result = await graph_mod.traverse(bare, "root")
        assert result.nodes == []
        assert any("telemetry" in r.getMessage() for r in caplog.records)
        assert telemetry._write_failures == 1
    finally:
        await bare.close()

    await graph_mod.traverse(db, "root")
    [row] = await _rows(db)
    assert row["prior_write_failures"] == 1
    assert telemetry._write_failures == 0


@pytest.mark.asyncio
async def test_a_read_only_caller_writes_through_db_path(db, tmp_path, monkeypatch):
    """The ambient worker traverses on a ``mode=ro`` connection; its rows must
    still land, through a separate read-write connection opened from the path."""
    from genesis.db.schema import create_all_tables

    path = tmp_path / "file.db"
    async with aiosqlite.connect(path) as setup:
        await create_all_tables(setup)
        await setup.commit()

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())
    ro = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
    try:
        async with telemetry.traversal_tally(ro, caller="ambient", db_path=str(path)):
            await graph_mod.traverse(ro, "root")
    finally:
        await ro.close()

    async with aiosqlite.connect(path) as check:
        rows = await _rows(check)
    assert [r["caller"] for r in rows] == ["ambient"]
    assert telemetry._write_failures == 0


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py", "--server", "memory"],
            "mcp-memory",
        ),
        (["/venv/bin/python", "-m", "genesis", "serve", "--port", "5000"], "server"),
        (["/venv/bin/python", "-m", "genesis.channels.bridge"], "bridge"),
        (["/venv/bin/python", "/repo/scripts/ambient_awareness_worker.py", "sid"], "ambient"),
        (["/venv/bin/python", "-m", "pytest"], "other"),
        ([], "other"),
        # argparse accepts the = form too.
        (
            ["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py", "--server=memory"],
            "mcp-memory",
        ),
        # Interpreter options before the script still mean the script is running.
        (
            ["python3", "-u", "/repo/scripts/genesis_mcp_server.py", "--server", "health"],
            "mcp-health",
        ),
        (["/venv/bin/python", "/repo/scripts/genesis_mcp_server.py"], "mcp-unknown"),
        # An interpreter option that takes a value is not mistaken for the script.
        (
            ["python3", "-X", "dev", "/repo/scripts/genesis_mcp_server.py", "--server", "memory"],
            "mcp-memory",
        ),
        # A process that only NAMES the file is not an MCP server.
        (["vim", "/repo/scripts/genesis_mcp_server.py"], "other"),
        (["bash", "-c", "pgrep -f genesis_mcp_server.py --server memory"], "other"),
        (["/venv/bin/python", "/repo/scripts/other.py", "genesis_mcp_server.py"], "other"),
    ],
)
def test_process_role(argv, expected):
    assert telemetry.process_role(argv) == expected


@pytest.mark.asyncio
async def test_a_cancellation_during_the_write_is_counted_and_reraised(monkeypatch):
    """A row lost to a cancellation mid-write still counts as lost, so the
    verdict cannot read a missing fallback as a clean day."""
    from genesis.db.crud import j9_eval

    async def _cancelled(*a, **k):
        raise asyncio.CancelledError

    monkeypatch.setattr(j9_eval, "insert_event", _cancelled)
    tally = telemetry._Tally(caller="recall")
    tally.add(outcome="fallback", configured="falkordb", served_by="networkx",
              primary_reason="ConnectionError", final_reason=None)

    with pytest.raises(asyncio.CancelledError):
        await telemetry._write_tally(tally, object())

    assert telemetry._write_failures == 1


@pytest.mark.asyncio
async def test_the_first_fallback_of_a_tally_is_written_at_once_and_only_once(
    db, monkeypatch
):
    """A process killed before its tally closes must not take the broken clock
    with it, so the call's FIRST clock-breaking traversal lands as its own row
    immediately. Later ones ride on the closing row (one write per call keeps
    an engine outage off recall's critical path), and nothing is counted twice."""
    stores = iter([_DeadStore(), _DeadStore(), _OkStore()])
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: next(stores))
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    async with telemetry.traversal_tally(db, caller="recall"):
        await graph_mod.traverse(db, "a")
        [early] = await _rows(db)  # durable before the tally closes
        assert early["caller"] == "recall"
        assert early["outcomes"] == {"fallback": 1}
        await graph_mod.traverse(db, "b")
        assert len(await _rows(db)) == 1  # the second fallback waits for the close
        await graph_mod.traverse(db, "c")

    rows = await _rows(db)
    assert len(rows) == 2
    [closing] = [r for r in rows if r is not early and r["traversals"] == 2]
    assert closing["outcomes"] == {"fallback": 1, "primary": 1}
    assert len(closing["events"]) == 1
    assert sum(r["traversals"] for r in rows) == 3


@pytest.mark.asyncio
async def test_an_immediate_row_on_a_read_only_connection_goes_through_db_path(
    tmp_path, monkeypatch
):
    path = tmp_path / "rw.db"
    async with aiosqlite.connect(path) as setup:
        await setup.execute(
            "CREATE TABLE eval_events (id TEXT PRIMARY KEY, timestamp TEXT, dimension TEXT,"
            " event_type TEXT, subject_id TEXT, session_id TEXT, metrics_json TEXT,"
            " created_at TEXT, prompt_hash TEXT)"
        )
        await setup.commit()
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))

    ro = await aiosqlite.connect(f"file:{path}?mode=ro", uri=True)
    try:
        async with telemetry.traversal_tally(ro, caller="ambient", db_path=str(path)):
            await graph_mod.traverse(ro, "root")
    finally:
        await ro.close()

    async with aiosqlite.connect(path) as check:
        rows = await _rows(check)
    assert [(r["caller"], r["outcomes"]) for r in rows] == [("ambient", {"fallback": 1})]
    assert telemetry._write_failures == 0
    assert _lost_lines() == []


@pytest.mark.asyncio
async def test_a_failed_write_also_lands_in_the_lost_writes_file(tmp_path, monkeypatch):
    """The in-memory count dies with its process; the file line does not."""
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _OkStore(name="networkx"))
    bare = await aiosqlite.connect(tmp_path / "no_eval_events.db")
    try:
        await graph_mod.traverse(bare, "root")
    finally:
        await bare.close()

    [line] = _lost_lines()
    assert line["caller"] == "direct"
    assert line["traversals"] == 1
    assert line["clock_breaking"] == 1
    assert line["error"] == "OperationalError"
    assert line["ts"].endswith("Z")


@pytest.mark.asyncio
async def test_an_unwritable_lost_writes_file_never_raises(tmp_path, monkeypatch, caplog):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("GENESIS_HOME", str(blocker))  # mkdir under a file fails
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _OkStore())
    bare = await aiosqlite.connect(tmp_path / "no_eval_events.db")
    try:
        with caplog.at_level(logging.WARNING, logger="genesis.memory.graph_telemetry"):
            result = await graph_mod.traverse(bare, "root")
    finally:
        await bare.close()

    assert result.nodes == []
    assert telemetry._write_failures == 1
    assert any("lost-write record also failed" in r.getMessage() for r in caplog.records)


def test_old_lost_write_lines_are_pruned_and_recent_or_unreadable_ones_kept():
    path = telemetry.lost_writes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"ts": "2000-01-01T00:00:00.000000Z", "caller": "old"}\n'
        '{"ts": "2999-01-01T00:00:00.000000Z", "caller": "new"}\n'
        "not json\n"
    )

    assert telemetry.prune_lost_writes() == 1
    assert path.read_text().splitlines() == [
        '{"ts": "2999-01-01T00:00:00.000000Z", "caller": "new"}',
        "not json",
    ]
    assert telemetry.prune_lost_writes() == 0


def test_pruning_a_missing_lost_writes_file_is_a_no_op():
    assert telemetry.prune_lost_writes() == 0


def test_a_prune_keeps_a_line_appended_through_a_handle_opened_before_it():
    """An appender that opened the file before a prune ran must still land its
    line in the file the report reads (the prune rewrites in place; it never
    swaps the file out from under an open handle)."""
    path = telemetry.lost_writes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"ts": "2000-01-01T00:00:00.000000Z", "caller": "old"}\n')
    inode = path.stat().st_ino

    with path.open("a", encoding="utf-8") as early:  # opened before the prune
        assert telemetry.prune_lost_writes() == 1
        early.write('{"ts": "2999-01-01T00:00:00.000000Z", "caller": "late"}\n')

    assert path.stat().st_ino == inode
    assert [json.loads(line)["caller"] for line in path.read_text().splitlines()] == ["late"]


@pytest.mark.asyncio
async def test_a_cancellation_during_the_fallback_still_breaks_the_clock(db, monkeypatch):
    """FalkorDB failed, and the request was cancelled while the fallback store
    was answering: the row must say fallback, not a neutral cancellation."""

    class _SlowFallback(_OkStore):
        async def traverse(self, *a, **k):
            raise asyncio.CancelledError

    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _DeadStore())
    monkeypatch.setattr(graph_mod, "_store", _SlowFallback(name="networkx"))

    with pytest.raises(asyncio.CancelledError):
        await graph_mod.traverse(db, "root")

    [row] = await _rows(db)
    assert row["outcomes"] == {"fallback": 1}
    assert row["served"] == {"none": 1}
    [event] = row["events"]
    assert event["primary_reason"] == "ConnectionRefusedError"

