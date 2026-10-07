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
def _telemetry_on(monkeypatch):
    monkeypatch.delenv("GENESIS_GRAPH_TELEMETRY_DISABLED", raising=False)
    monkeypatch.setattr(telemetry, "_write_failures", 0)


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
    ],
)
def test_process_role(argv, expected):
    assert telemetry._process_role(argv) == expected


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
