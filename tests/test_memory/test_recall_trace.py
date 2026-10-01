"""Per-recall retrieval trace (``genesis.memory.recall_trace``).

Pins: the persisted record shape (per-candidate retriever attribution, fused /
rerank / final scores, final rank, injected-vs-dropped outcome), that the real
engine fills the sink and stamps the join key onto ``recall_fired``, that the
proactive layer records drop reasons and joins graph expansion, that the write
is OFF the request path, and the retention prune. Install-agnostic: in-memory
SQLite + fakes, no live services.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.memory import proactive as P
from genesis.memory import recall_trace as RT
from genesis.memory.retrieval import HybridRetriever

# --------------------------------------------------------------------------- #
# build_record — pure
# --------------------------------------------------------------------------- #


def _sink() -> dict:
    return {
        "trace_id": "t" * 16,
        "caller": "proactive_endpoint",
        "profile": "cc_hook",
        "recall_limit": 2,  # rerank window = 6
        "lanes": {
            "vector": ["a", "b", "c"],
            "fts": ["b", "d"],
            "event": [],
            "activation": ["d", "a", "b", "c", "e", "f", "g", "h", "i"],
            "intent": [],
        },
        "fused": {m: 1.0 / (i + 1) for i, m in enumerate("abcdefghi")},
        "rerank": {"b": 0.91, "a": 0.40},
        "post_rerank": {"b": 1.0, "a": 0.5},
        "results": [("b", 1.0, 1.0), ("a", 0.5, 0.5), ("c", 0.3, 0.3)],
        "drops": {"a": "noise"},
        "graph_neighbors": [("z", 0.42)],
        "delivered": ["b", "z"],
        "timings_ms": {"total": 12.0},
        "degraded_reasons": [],
    }


def test_record_attributes_each_candidate():
    rec = RT.build_record(_sink())
    by_id = {c["id"]: c for c in rec["candidates"]}

    b = by_id["b"]
    assert b["found_by"] == {"vector": 2, "fts": 1}
    assert b["activation_rank"] == 3
    assert b["fused"] == 0.5 and b["rerank"] == 0.91 and b["post_rerank"] == 1.0
    assert b["final_rank"] == 1 and b["injected"] is True and b["outcome"] == "injected"

    assert by_id["a"]["outcome"] == "dropped:noise" and by_id["a"]["injected"] is False
    assert by_id["c"]["outcome"] == "not_reached"  # returned, past the budget
    assert by_id["d"]["outcome"] == "not_returned"  # fts-found, cut inside recall
    assert by_id["z"]["found_by"] == {"graph": 1}
    assert by_id["z"]["outcome"] == "injected_via_graph"
    # PR #2455 review: a graph-injected memory carries its graph score and its
    # position in what was delivered (it has no recall final_rank).
    assert by_id["z"]["graph_score"] == 0.42
    assert by_id["z"]["delivered_rank"] == 2
    assert by_id["b"]["delivered_rank"] == 1
    assert "delivered_rank" not in by_id["a"]


def test_record_window_is_bounded_by_meaning_and_counts_the_rest():
    rec = RT.build_record(_sink())
    ids = [c["id"] for c in rec["candidates"]]
    # rerank window (3 × limit = 6 by fused) + neighbor z; g/h/i are the tail.
    assert ids[:6] == list("abcdef")
    assert "z" in ids and not {"g", "h", "i"} & set(ids)
    assert rec["pool_size"] == 9
    assert rec["omitted"] == 3  # never silently cut — counted
    assert rec["window"] == len(ids)


def test_record_tolerates_a_partial_cancelled_sink():
    rec = RT.build_record(
        {"trace_id": "x", "outcome": "cancelled:recall", "timings_ms": {"embed": 5}}
    )
    assert rec["outcome"] == "cancelled:recall"
    assert rec["candidates"] == [] and rec["pool_size"] == 0
    assert rec["timings_ms"] == {"embed": 5}


def test_record_carries_zero_hit_and_lane_hits():
    rec = RT.build_record(
        {
            "trace_id": "x",
            "recall_limit": 4,
            "embedding_available": True,
            "fts_query_rewritten": False,
            "lane_hits": {"vector": 0, "fts": 0, "event": 0},
            "zero_hit": True,
        }
    )
    assert rec["zero_hit"] is True
    assert rec["lane_hits"] == {"vector": 0, "fts": 0, "event": 0}
    assert rec["recall_limit"] == 4 and rec["embedding_available"] is True


def test_record_degraded_flag_follows_reasons():
    s = _sink()
    s["degraded_reasons"] = ["rerank_timed_out"]
    rec = RT.build_record(s)
    assert rec["degraded"] is True and rec["degraded_reasons"] == ["rerank_timed_out"]
    assert RT.build_record(_sink())["degraded"] is False


# --------------------------------------------------------------------------- #
# write + retention on a real eval_events table
# --------------------------------------------------------------------------- #


async def _eval_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        "CREATE TABLE eval_events (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, "
        "dimension TEXT NOT NULL, event_type TEXT NOT NULL, subject_id TEXT, "
        "session_id TEXT, metrics_json TEXT NOT NULL, created_at TEXT, prompt_hash TEXT)"
    )
    return db


async def test_write_trace_row_id_is_the_trace_id():
    db = await _eval_db()
    try:
        await RT.write_trace(db, _sink(), session_id="sess-1")
        rows = await db.execute_fetchall(
            "SELECT id, dimension, event_type, session_id, metrics_json FROM eval_events"
        )
    finally:
        await db.close()
    assert len(rows) == 1
    rid, dim, etype, sid, mj = rows[0]
    assert (rid, dim, etype, sid) == ("t" * 16, "memory", "recall_trace", "sess-1")
    assert json.loads(mj)["candidates"][0]["id"] == "a"


async def test_write_trace_never_raises():
    db = await aiosqlite.connect(":memory:")  # no eval_events table
    try:
        await RT.write_trace(db, _sink())  # must swallow
    finally:
        await db.close()


async def test_prune_removes_only_old_recall_traces():
    from genesis.db.crud import j9_eval

    db = await _eval_db()
    try:
        old = "2020-01-01T00:00:00.000000Z"
        await j9_eval.insert_event(
            db, dimension="memory", event_type="recall_trace", metrics={}, timestamp=old
        )
        await j9_eval.insert_event(
            db, dimension="memory", event_type="recall_fired", metrics={}, timestamp=old
        )
        await j9_eval.insert_event(db, dimension="memory", event_type="recall_trace", metrics={})
        removed = await j9_eval.prune_event_type_older_than(
            db, event_type="recall_trace", days=RT.RETENTION_DAYS
        )
        left = await db.execute_fetchall("SELECT event_type, timestamp FROM eval_events")
    finally:
        await db.close()
    assert removed == 1
    assert sorted(e for e, _ in left) == ["recall_fired", "recall_trace"]
    assert all(ts > "2021" or e == "recall_fired" for e, ts in left)


# --------------------------------------------------------------------------- #
# the real engine fills the sink and stamps the join key
# --------------------------------------------------------------------------- #


@pytest.fixture
def _no_ghost_filter(monkeypatch):
    async def _none(db, ids):
        return set()

    monkeypatch.setattr("genesis.memory.retrieval.metadata_missing_ids", _none)


def _qdrant_hit(mid: str, score: float) -> dict:
    return {
        "id": mid,
        "score": score,
        "payload": {
            "content": f"content for {mid}",
            "source": "test",
            "memory_type": "episodic",
            "tags": [],
            "confidence": 0.8,
            "created_at": datetime.now(UTC).isoformat(),
            "retrieved_count": 1,
            "source_type": "memory",
        },
    }


@pytest.mark.usefixtures("_no_ghost_filter")
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_recall_fills_the_trace_sink_and_stamps_recall_fired(
    mock_qdrant, mock_crud, mock_links, _exp
):
    mock_qdrant.search.return_value = [_qdrant_hit("mem-1", 0.95), _qdrant_hit("mem-2", 0.5)]
    mock_qdrant.update_payload = MagicMock()
    mock_crud.search_ranked = AsyncMock(
        return_value=[
            {
                "memory_id": "mem-2",
                "content": "fts content",
                "source_type": "memory",
                "collection": "episodic_memory",
                "rank": -3.0,
                "origin_class": None,
                "wing": None,
                "room": None,
                "tags": "",
            }
        ]
    )
    mock_crud.batch_created_at = AsyncMock(return_value={})
    mock_crud.origin_class_by_ids = AsyncMock(return_value={})
    mock_links.batch_link_counts = AsyncMock(return_value={})
    mock_links.inter_candidate_links = AsyncMock(return_value=[])

    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    db = MagicMock(spec_set=["execute", "commit"])
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    retriever = HybridRetriever(embedding_provider=embed, qdrant_client=MagicMock(), db=db)

    trace = {"trace_id": "abc123"}
    with patch(
        "genesis.eval.j9_hooks.emit_recall_fired", new_callable=AsyncMock, return_value=None
    ) as emit:
        results = await retriever.recall("test", limit=2, trace=trace)

    assert results
    assert trace["lanes"]["vector"] == ["mem-1", "mem-2"]
    assert trace["lanes"]["fts"] == ["mem-2"]
    assert set(trace["fused"]) == {"mem-1", "mem-2"}
    assert trace["recall_limit"] == 2 and trace["embedding_available"] is True
    assert [mid for mid, _rs, _s in trace["results"]] == [r.memory_id for r in results]
    assert emit.await_args.kwargs["trace_id"] == "abc123"

    rec = RT.build_record(trace)
    by_id = {c["id"]: c for c in rec["candidates"]}
    assert by_id["mem-2"]["found_by"] == {"vector": 2, "fts": 1}


# --------------------------------------------------------------------------- #
# proactive layer: drop reasons + graph-expansion join
# --------------------------------------------------------------------------- #


def _rr(mid, collection="episodic_memory", content="an ordinary memory about things"):
    from genesis.memory.types import RetrievalResult

    return RetrievalResult(
        memory_id=mid,
        content=content,
        source="",
        memory_type="fact",
        score=0.5,
        vector_rank=1,
        fts_rank=None,
        activation_score=0.0,
        payload={},
        collection=collection,
    )


async def test_proactive_impl_records_drops_and_joins_graph_expansion():
    from genesis.mcp.memory import core as C

    received = {}

    class _Retriever:
        async def recall(self, *a, trace=None, **k):
            received["trace"] = trace
            return [_rr("m1"), _rr("junk", content="ok"), _rr("m2"), _rr("m3")]

    fake_mod = SimpleNamespace(_retriever=_Retriever(), _db=MagicMock(), _require_init=lambda: None)
    seen = {}

    async def _maybe_expand(db, kept, surface, recall_event_id=None, neighbor_sink=None):
        seen["recall_event_id"] = recall_event_id
        nb = _rr("nb1")
        if neighbor_sink is not None:
            neighbor_sink.append(nb)
        return [*kept, nb]

    async def _record(*a, **k):
        return None

    def _noise(collection, pipeline, content):
        return content == "ok"

    trace = {"trace_id": "tid-1"}
    with (
        patch.object(C, "_memory_mod", return_value=fake_mod),
        patch.object(C.graph_expansion, "maybe_expand", new=_maybe_expand),
        patch.object(C, "is_proactive_noise", new=_noise),
        patch.object(C.immunity_shadow, "should_enforce_drop", return_value=False),
        patch.object(C.immunity_shadow, "item_is_blockable", return_value=False),
        patch.object(C.immunity_shadow, "is_dispatched_session_env", return_value=False),
        patch.object(C.immunity_shadow, "record_would_block", new=_record),
    ):
        out = await C._proactive_impl("q", limit=2, filter_noise=True, trace=trace)

    assert received["trace"] is trace  # the same sink reaches recall()
    assert seen["recall_event_id"] == "tid-1"  # graph event joins the trace
    assert trace["drops"] == {"junk": "noise"}
    assert trace["graph_neighbors"] == [("nb1", 0.5)]
    assert [d["memory_id"] for d in out] == ["m1", "m2", "nb1"]


async def test_shadow_mode_graph_neighbors_reach_the_trace():
    """PR #2455 review: in shadow mode maybe_expand returns the results
    unchanged, so slicing its return found no neighbors. The trace must record
    the COMPUTED neighbors through the sink even when none is injected."""
    from genesis.mcp.memory import core as C

    class _Retriever:
        async def recall(self, *a, trace=None, **k):
            return [_rr("m1"), _rr("m2")]

    fake_mod = SimpleNamespace(_retriever=_Retriever(), _db=MagicMock(), _require_init=lambda: None)

    async def _shadow_expand(db, kept, surface, recall_event_id=None, neighbor_sink=None):
        if neighbor_sink is not None:
            neighbor_sink.append(_rr("nb-shadow"))
        return list(kept)  # shadow: computed, not injected

    async def _record(*a, **k):
        return None

    trace = {"trace_id": "tid-2"}
    with (
        patch.object(C, "_memory_mod", return_value=fake_mod),
        patch.object(C.graph_expansion, "maybe_expand", new=_shadow_expand),
        patch.object(C.graph_expansion, "expansion_mode", return_value="shadow"),
        patch.object(C.immunity_shadow, "should_enforce_drop", return_value=False),
        patch.object(C.immunity_shadow, "item_is_blockable", return_value=False),
        patch.object(C.immunity_shadow, "is_dispatched_session_env", return_value=False),
        patch.object(C.immunity_shadow, "record_would_block", new=_record),
    ):
        out = await C._proactive_impl("q", limit=2, trace=trace)

    assert [d["memory_id"] for d in out] == ["m1", "m2"]
    assert trace["graph_neighbors"] == [("nb-shadow", 0.5)]
    assert trace["graph_mode"] == "shadow"
    trace["delivered"] = ["m1", "m2"]
    rec = {c["id"]: c for c in RT.build_record(trace)["candidates"]}
    assert rec["nb-shadow"]["found_by"] == {"graph": 1}
    assert rec["nb-shadow"]["outcome"] == "graph_not_injected"
    assert rec["nb-shadow"]["graph_score"] == 0.5


async def test_real_maybe_expand_fills_the_sink_in_shadow_mode():
    """The sink is honoured by the REAL maybe_expand in shadow mode."""
    from genesis.memory import graph_expansion as G

    async def _neighbors(db, seed_ids, **kw):
        return [_rr("nb")]

    db = MagicMock()
    sink: list = []
    with (
        patch.object(G, "load_recall_config", return_value={"graph_expansion": {"mode": "shadow"}}),
        patch.object(G, "expand_neighbors", new=_neighbors),
        patch.object(G.j9_eval, "insert_event", new=AsyncMock()),
    ):
        out = await G.maybe_expand(db, [_rr("seed")], surface="proactive", neighbor_sink=sink)
    assert [r.memory_id for r in out] == ["seed"]  # shadow: nothing injected
    assert [r.memory_id for r in sink] == ["nb"]


# --------------------------------------------------------------------------- #
# proactive_context: off-path write, join key, cancellation, kill switch
# --------------------------------------------------------------------------- #


class _FakeDB:
    async def execute_fetchall(self, sql, params=()):
        return []


class _FakeRetriever:
    _db = _FakeDB()

    async def _embed_query(self, _q):
        return ([0.1] * 8, True)

    async def _ro_read(self, fn, *args, **kwargs):
        return await fn(self._db, *args, **kwargs)


class _FakeMod:
    _retriever = _FakeRetriever()
    _db = _FakeDB()

    @staticmethod
    def _require_init():
        return None


def _delivered_impl():
    async def _impl(prompt, **kwargs):
        trace = kwargs.get("trace")
        if trace is not None:
            trace["fused"] = {"m1": 0.5}
            trace["results"] = [("m1", 0.5, 0.5)]
            trace["recall_limit"] = 2
        return [
            {
                "memory_id": "m1",
                "content": "c",
                "collection": "episodic_memory",
                "memory_class": "fact",
                "origin_class": None,
                "source_pipeline": None,
                "score": 0.5,
                "payload": {},
                "via_graph": False,
            }
        ]

    return _impl


async def test_trace_is_written_off_path_with_the_returned_join_key():
    gate = asyncio.Event()
    written = []

    async def _slow_write(db, trace, *, session_id=None):
        await gate.wait()  # would block the response if it were awaited inline
        written.append((trace, session_id))

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_delivered_impl()),
        patch.object(RT, "write_trace", new=_slow_write),
    ):
        resp = await P.proactive_context(prompt="what did we decide about voice", session_id="s1")
        # The response exists while the write is still parked.
        assert written == []
        tid = resp["engine"]["trace_id"]
        assert isinstance(tid, str) and len(tid) == 16
        gate.set()
        for _ in range(50):
            if written:
                break
            await asyncio.sleep(0.01)

    trace, sid = written[0]
    assert trace["trace_id"] == tid and sid == "s1"
    assert trace["outcome"] == "ok" and trace["delivered"] == ["m1"]
    rec = RT.build_record(trace)
    assert rec["candidates"][0]["outcome"] == "injected"
    assert "total" in rec["timings_ms"]


async def test_cancelled_recall_still_writes_a_partial_trace():
    written = []

    async def _write(db, trace, *, session_id=None):
        written.append(trace)

    async def _cancel(prompt, **kwargs):
        raise asyncio.CancelledError

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_cancel),
        patch.object(RT, "write_trace", new=_write),
        pytest.raises(asyncio.CancelledError),
    ):
        await P.proactive_context(prompt="what did we decide", session_id="s")
    for _ in range(50):
        if written:
            break
        await asyncio.sleep(0.01)
    assert written and written[0]["outcome"] == "cancelled:recall"
    assert "embed" in written[0]["timings_ms"]


async def test_kill_switch_disables_the_trace():
    written = []

    async def _write(db, trace, *, session_id=None):
        written.append(trace)

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_delivered_impl()),
        patch.object(RT, "write_trace", new=_write),
        patch.object(P, "_proactive_config", return_value={"trace": "off"}),
    ):
        resp = await P.proactive_context(prompt="what did we decide about voice", session_id="s")
    await asyncio.sleep(0.05)
    assert resp["engine"]["trace_id"] is None
    assert written == []


@pytest.mark.usefixtures("_no_ghost_filter")
@patch("genesis.memory.retrieval.expand_query", new_callable=AsyncMock, return_value="test")
@patch("genesis.memory.retrieval.memory_links")
@patch("genesis.memory.retrieval.memory_crud")
@patch("genesis.memory.retrieval.qdrant_ops")
async def test_zero_hit_recall_still_fills_the_trace(mock_qdrant, mock_crud, mock_links, _exp):
    """PR #2455 review: the zero-candidate early return used to leave the trace
    without recall_limit/embedding/lane data — indistinguishable from a recall
    that never ran. It must be marked zero_hit with lane hit counts."""
    mock_qdrant.search.return_value = []
    mock_crud.search_ranked = AsyncMock(return_value=[])
    mock_crud.batch_created_at = AsyncMock(return_value={})
    mock_links.batch_link_counts = AsyncMock(return_value={})
    mock_links.inter_candidate_links = AsyncMock(return_value=[])
    embed = MagicMock()
    embed.embed = AsyncMock(return_value=[0.1] * 1024)
    db = MagicMock(spec_set=["execute", "commit"])
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    retriever = HybridRetriever(embedding_provider=embed, qdrant_client=MagicMock(), db=db)

    trace = {"trace_id": "zz"}
    assert await retriever.recall("test", limit=3, trace=trace) == []
    assert trace["zero_hit"] is True
    assert trace["recall_limit"] == 3
    assert trace["embedding_available"] is True
    assert trace["lane_hits"] == {"vector": 0, "fts": 0, "event": 0}
    # Named for what it measures: the inert-file-term fallback only.
    assert trace["file_lane_fallback_used"] is False
    # Round-2 class sweep: the FTS expression can be rewritten by the file
    # lane alone, so the field must not claim tag EXPANSION.
    assert trace["fts_query_rewritten"] is False
    assert "fts_query_expanded" not in trace
    assert "fts_fallback_used" not in trace
    rec = RT.build_record(trace)
    assert rec["zero_hit"] is True and rec["candidates"] == []


async def test_zero_budget_call_logs_timing_and_writes_a_skip_trace(caplog):
    """PR #2455 review: a configured zero budget returned before the timing
    line and the trace, so those endpoint calls vanished from both."""
    import logging

    written = []

    async def _write(db, trace, *, session_id=None):
        written.append(trace)

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch.object(P, "_budget_for", return_value=(0, 8)),
        patch.object(RT, "write_trace", new=_write),
        caplog.at_level(logging.INFO, logger="genesis.memory.proactive"),
    ):
        resp = await P.proactive_context(prompt="restart it", session_id="s")
    for _ in range(50):
        if written:
            break
        await asyncio.sleep(0.01)
    assert resp["results"] == []
    assert any("skipped=zero_budget" in r.getMessage() for r in caplog.records)
    assert written and written[0]["outcome"] == "skipped:zero_budget"
    assert resp["engine"]["trace_id"] == written[0]["trace_id"]


def test_rerank_degradation_is_reported_alongside_no_embedding():
    """PR #2455 round 2: no_embedding must not hide a rerank that was requested
    and did not run — reranking does not depend on the query vector."""
    reasons = P._degraded_reasons({}, None, True, {"embedding_available": False})
    assert reasons == ["no_embedding", "rerank_not_executed"]
    # A rerank-specific reason still replaces the generic one.
    reasons = P._degraded_reasons(
        {"rerank_timed_out": True}, None, True, {"embedding_available": False}
    )
    assert reasons == ["no_embedding", "rerank_timed_out"]
    assert P._degraded_reasons({"rerank_executed": True}, [0.1], True, None) == []


async def test_errored_recall_trace_carries_degraded_reasons():
    """The error path records degraded_reasons like the ok/cancel paths."""
    written = []

    async def _write(db, trace, *, session_id=None):
        written.append(trace)

    async def _boom(prompt, **kwargs):
        raise RuntimeError("engine failure")

    with (
        patch("genesis.mcp.memory.core._memory_mod", return_value=_FakeMod()),
        patch("genesis.mcp.memory.core._proactive_impl", new=_boom),
        patch.object(RT, "write_trace", new=_write),
        pytest.raises(RuntimeError),
    ):
        await P.proactive_context(prompt="what did we decide", session_id="s")
    for _ in range(50):
        if written:
            break
        await asyncio.sleep(0.01)
    assert written and written[0]["outcome"] == "error:recall"
    assert "degraded_reasons" in written[0]
