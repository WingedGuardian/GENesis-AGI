"""Caller-chosen graph visibility at the MCP SURFACE — issue #1896.

Split out of ``test_graph_visibility.py``, which covers the same issue at the
STORE seam (protocol, the three backends, and their agreement with each other).
The two halves ask different questions and the file was over CONTRIBUTING's
1,000-line cap, so they are separated along that seam:

  * that file  — do the backends HONOUR the caller's visibility choice?
  * this file  — do the two MCP tools PASS it, and LABEL what comes back?

Written as behavioural tests driving the real tool functions rather than as an
AST check for the keyword: a call site can contain ``include_deprecated=...``
and still be unreachable, and a keyword can be present and bound to the wrong
thing. One of these tests was silently deleted by an earlier restructure and
only a mutation sweep noticed, which is why the defaults test asserts both tools
in one place.
"""

from __future__ import annotations

import aiosqlite
import pytest

pytestmark = pytest.mark.asyncio


# ── the callers: the flag has to actually be threaded ─────────────────────
#
# `test_graph_visibility.py` proves the stores HONOUR the flag. These prove the
# two MCP surfaces PASS it — the defect as reported. Written as behavioural
# tests driving the real tool functions, not as an AST check for the keyword: a
# call site can contain `include_deprecated=...` and still be unreachable, and a
# keyword can be present and bound to the wrong thing.


@pytest.fixture
async def labelled_db():
    """A real database for the label tests, closed UNCONDITIONALLY.

    Deliberately a fixture rather than an open/close pair inside the test body.
    An `await conn.close()` on the last line does not run when an assertion above
    it fails, and an unclosed aiosqlite connection holds a live THREAD — which
    blocks event-loop teardown, so the run HANGS instead of reporting the
    failure. Measured while verify-RED was mutating this file: a mutation that
    should have produced a clean red produced a 180s timeout.
    """
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE memory_metadata (
               memory_id TEXT PRIMARY KEY, invalid_at TEXT, deprecated INTEGER)"""
    )
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def _mcp_state():
    from unittest.mock import MagicMock

    from genesis.mcp import memory_mcp

    memory_mcp.init(db=MagicMock(), qdrant_client=MagicMock(), embedding_provider=MagicMock())
    yield memory_mcp
    memory_mcp._store = None
    memory_mcp._retriever = None
    memory_mcp._user_model_evolver = None
    memory_mcp._db = None
    memory_mcp._qdrant = None


class _TraverseRecorder:
    """Stands in for graph_traverse and records the visibility argument."""

    def __init__(self):
        self.calls: list[dict] = []

    async def __call__(self, db, root_id, **kwargs):
        self.calls.append({"root_id": root_id, **kwargs})

        class _R:
            nodes: list = []
            query_ms = 0.0

        return _R()


@pytest.mark.parametrize("asked", [False, True])
async def test_memory_recall_threads_the_visibility_choice(_mcp_state, monkeypatch, asked):
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    rec = _TraverseRecorder()
    monkeypatch.setattr(core_mod, "graph_traverse", rec)
    _mcp_state._retriever.recall = AsyncMock(
        return_value=[
            RetrievalResult(
                memory_id="11111111-1111-1111-1111-111111111111",
                content="c",
                source="test",
                memory_type="episodic",
                score=0.9,
                vector_rank=1,
                fts_rank=1,
                activation_score=0.8,
                payload={},
            )
        ]
    )

    await core_mod.memory_recall.fn(
        query="q",
        include_deprecated=asked,
        compact=False,
        include_graph=True,
        corrective=False,
    )

    assert rec.calls, "graph enrichment never ran — this test proves nothing"
    assert rec.calls[0]["include_deprecated"] is asked


@pytest.mark.parametrize("asked", [False, True])
async def test_memory_expand_threads_the_visibility_choice(_mcp_state, monkeypatch, asked):
    from types import SimpleNamespace

    from genesis.mcp.memory import core as core_mod

    rec = _TraverseRecorder()
    monkeypatch.setattr(core_mod, "graph_traverse", rec)
    mid = "22222222-2222-2222-2222-222222222222"
    point = SimpleNamespace(id=mid, payload={"content": "c", "origin_class": "x"})
    _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
        [point] if collection_name == "episodic_memory" else []
    )

    await core_mod.memory_expand.fn(memory_ids=[mid], include_deprecated=asked)

    assert rec.calls, "graph enrichment never ran — this test proves nothing"
    assert rec.calls[0]["include_deprecated"] is asked


@pytest.mark.parametrize("mode", ["drift", "standard"])
async def test_the_drift_pipeline_does_not_get_the_visibility_choice(_mcp_state, monkeypatch, mode):
    """Enrichment must match the pipeline that PRODUCED the results, not the ask.

    `drift_recall` takes no `include_deprecated` at all and hides deprecated
    memories in BOTH its lanes — `search_ranked` filters them in SQL, and
    `qdrant.search` defaults the flag False and adds a `must_not deprecated`
    condition. So honouring the caller's flag during enrichment on a drift recall
    puts an id in `graph_neighbors` that the same call's `results` could never
    contain: the identical defect as the expired-memory one, one pipeline over,
    and introduced by the #1896 fix rather than pre-existing (before it the
    traversal always hid deprecated, so it agreed with drift by accident).

    Parametrized against `standard` as the CONTROL: without it, a bug that
    withheld the flag from every pipeline would pass the drift arm and look like
    correct behaviour.
    """
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    hit = RetrievalResult(
        memory_id="33333333-3333-3333-3333-333333333333",
        content="c",
        source="test",
        memory_type="episodic",
        score=0.9,
        vector_rank=1,
        fts_rank=1,
        activation_score=0.8,
        payload={},
    )
    rec = _TraverseRecorder()
    monkeypatch.setattr(core_mod, "graph_traverse", rec)
    # Patch the module attribute drift mode imports, so the drift arm never needs
    # Qdrant or an embedding provider.
    drift_mod = __import__("genesis.memory.drift", fromlist=["drift_recall"])
    monkeypatch.setattr(drift_mod, "drift_recall", AsyncMock(return_value=[hit]))
    _mcp_state._retriever.recall = AsyncMock(return_value=[hit])

    await core_mod.memory_recall.fn(
        query="q",
        mode=mode,
        include_deprecated=True,
        compact=False,
        include_graph=True,
        corrective=False,
    )

    assert rec.calls, "graph enrichment never ran — this test proves nothing"
    got = rec.calls[0]["include_deprecated"]
    if mode == "drift":
        assert got is False, (
            "the drift pipeline hides deprecated memories in both lanes and takes "
            "no visibility flag, so enrichment must not un-hide them — that would "
            "return a neighbour this recall's own results could never contain"
        )
    else:
        assert got is True, (
            "the standard pipeline DOES honour include_deprecated, so withholding "
            "it here would reproduce #1896 for every ordinary audit recall"
        )


# ── the hidden neighbours are LABELLED, not merely returned ───────────────


class _TwoNodeTraversal:
    """A traversal result carrying one hidden and one visible neighbour."""

    def __init__(self, hidden_id, visible_id):
        from genesis.memory.graphstore import GraphNode

        self.nodes = [
            GraphNode(memory_id=hidden_id, link_type="supports", depth=1, strength=0.9),
            GraphNode(memory_id=visible_id, link_type="supports", depth=1, strength=0.8),
        ]
        self.query_ms = 0.0


@pytest.mark.parametrize("tool", ["memory_recall", "memory_expand"])
async def test_a_hidden_neighbour_is_LABELLED_when_the_caller_opted_in(
    _mcp_state, monkeypatch, tool, labelled_db
):
    """Un-hiding without labelling hands a caller superseded ids that look live.

    The flag is per-CALL, not per-root, so it also un-hides neighbours of VISIBLE
    memories — MEASURED on the live graph: 11,032 of 73,823 visible memories with
    out-edges (14.9%) gain at least one hidden neighbour at depth 1 alone. A
    caller following a ``[→ related: id:…]`` hint would otherwise have no way to
    tell a superseded neighbour from a live one, and the documented next step is
    to expand it.

    ``hidden`` is present ONLY on a hidden neighbour, so its ABSENCE means
    visible — asserted in both directions here, because a label that is always
    present or always absent carries no information.
    """
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    hidden_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    visible_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    root_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

    async def _fake_traverse(db, rid, **kw):
        return _TwoNodeTraversal(hidden_id, visible_id)

    monkeypatch.setattr(core_mod, "graph_traverse", _fake_traverse)
    # A REAL database, and `_label_hidden_neighbours` is NOT mocked. An earlier
    # version of this test monkeypatched it, which made the test grade a mock —
    # so deleting the real label logic entirely left it green. A mutation sweep
    # caught that. The lesson: the one function under test is the one thing a
    # test must never stand in for.
    await labelled_db.executemany(
        "INSERT INTO memory_metadata VALUES (?, NULL, ?)",
        [(hidden_id, 1), (visible_id, 0), (root_id, 0)],
    )
    await labelled_db.commit()
    monkeypatch.setattr(_mcp_state, "_db", labelled_db)

    async def run(*, include_deprecated):
        if tool == "memory_recall":
            _mcp_state._retriever.recall = AsyncMock(
                return_value=[
                    RetrievalResult(
                        memory_id=root_id,
                        content="c",
                        source="t",
                        memory_type="episodic",
                        score=0.9,
                        vector_rank=1,
                        fts_rank=1,
                        activation_score=0.8,
                        payload={},
                    )
                ]
            )
            out = await core_mod.memory_recall.fn(
                query="q",
                include_deprecated=include_deprecated,
                compact=False,
                include_graph=True,
                corrective=False,
            )
        else:
            from types import SimpleNamespace

            point = SimpleNamespace(id=root_id, payload={"content": "c", "origin_class": "x"})
            _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
                [point] if collection_name == "episodic_memory" else []
            )
            out = await core_mod.memory_expand.fn(
                memory_ids=[root_id],
                include_deprecated=include_deprecated,
            )
        return {n["memory_id"]: n for n in out[0]["graph_neighbors"]}

    by_id = await run(include_deprecated=True)
    assert by_id[hidden_id].get("hidden") is True, (
        "a hidden neighbour came back unlabelled — indistinguishable from a live one"
    )
    assert "hidden" not in by_id[visible_id], (
        "a VISIBLE neighbour was labelled hidden, so the label means nothing"
    )

    off = await run(include_deprecated=False)
    assert not any("hidden" in n for n in off.values()), (
        "the label appeared with the flag off, where no hidden neighbour can exist"
    )


async def test_the_label_lookup_is_skipped_entirely_when_not_opted_in(_mcp_state):
    """With the predicate applied, no hidden id can be in the result — so the
    lookup is a guaranteed-useless query on the hot path and must not run.

    `db=None` is the proof: it would raise if anything touched the database.
    """
    from genesis.mcp.memory import core as core_mod

    results = [{"graph_neighbors": [{"memory_id": "x"}]}]
    assert await core_mod._label_hidden_neighbours(None, results, include_deprecated=False) is True
    assert "hidden" not in results[0]["graph_neighbors"][0]


async def test_no_query_runs_when_there_are_no_neighbours_to_label(_mcp_state):
    """The other free case: the flag is ON but nothing came back. `memory_expand`
    defaults the flag on, so this is its every-miss path — it must not pay a
    query for an empty candidate set."""
    from genesis.mcp.memory import core as core_mod

    for empty in ([], [{"graph_neighbors": []}], [{"graph_neighbors": None}], [{}]):
        assert (
            await core_mod._label_hidden_neighbours(None, empty, include_deprecated=True) is True
        ), f"a query was attempted for {empty!r}"


async def test_the_label_fails_open_but_REPORTS_that_it_could_not_check(_mcp_state, caplog):
    """A label is an enrichment: failing to compute one must not cost the caller
    their neighbours. But it must not silently assert the opposite of the truth
    either.

    Returning "nothing is hidden" for a call that could not look would mark every
    neighbour live — MEASURED, 4,276 hidden ids on one live install — in a payload
    whose documented next step is to expand and trust them. So the contract is:
    neighbours survive, the return value is False, and it logs at WARNING rather
    than DEBUG, because the caller explicitly asked to see hidden memories and
    this is a failure of that request.
    """
    import logging

    from genesis.mcp.memory import core as core_mod

    class _Boom:
        async def execute(self, *a, **k):
            raise RuntimeError("database is gone")

    results = [{"graph_neighbors": [{"memory_id": "x"}, {"memory_id": "y"}]}]
    with caplog.at_level(logging.WARNING, logger="genesis.mcp.memory.core"):
        ok = await core_mod._label_hidden_neighbours(_Boom(), results, include_deprecated=True)
    assert ok is False, "a failed check must report itself, not return silently"
    assert len(results[0]["graph_neighbors"]) == 2, (
        "fail-open broken: the caller lost their neighbours"
    )
    assert not any("hidden" in n for n in results[0]["graph_neighbors"]), (
        "labels were invented on a call that could not check"
    )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING], (
        "the failure was logged below WARNING, where production will not see it"
    )


@pytest.mark.parametrize("tool", ["memory_recall", "memory_expand"])
async def test_a_failed_label_check_withdraws_the_absence_contract(_mcp_state, monkeypatch, tool):
    """END TO END: when labelling cannot run, the response must not let absence be
    read as "visible". Both tools mark it.

    Without this, a caller sees five unlabelled neighbours and cannot tell "all
    five are live" from "nobody checked" — and the safe-sounding reading is the
    wrong one.
    """
    from unittest.mock import AsyncMock

    from genesis.mcp.memory import core as core_mod
    from genesis.memory.types import RetrievalResult

    root_id = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    async def _fake_traverse(db, rid, **kw):
        return _TwoNodeTraversal("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", root_id)

    monkeypatch.setattr(core_mod, "graph_traverse", _fake_traverse)
    monkeypatch.setattr(core_mod, "_label_hidden_neighbours", AsyncMock(return_value=False))

    if tool == "memory_recall":
        _mcp_state._retriever.recall = AsyncMock(
            return_value=[
                RetrievalResult(
                    memory_id=root_id,
                    content="c",
                    source="t",
                    memory_type="episodic",
                    score=0.9,
                    vector_rank=1,
                    fts_rank=1,
                    activation_score=0.8,
                    payload={},
                )
            ]
        )
        out = await core_mod.memory_recall.fn(
            query="q",
            include_deprecated=True,
            compact=False,
            include_graph=True,
            corrective=False,
        )
    else:
        from types import SimpleNamespace

        point = SimpleNamespace(id=root_id, payload={"content": "c", "origin_class": "x"})
        _mcp_state._qdrant.retrieve = lambda collection_name, ids, with_payload: (
            [point] if collection_name == "episodic_memory" else []
        )
        out = await core_mod.memory_expand.fn(memory_ids=[root_id], include_deprecated=True)

    assert out[0].get("graph_neighbors"), (
        "guard-the-guard: there must be neighbours for the marker to apply to"
    )
    assert out[0].get("graph_neighbors_visibility") == "unknown", (
        "a response whose labels could not be computed did not say so"
    )


async def test_the_two_tools_defaults_are_deliberately_different(_mcp_state):
    """`memory_recall` defaults to hiding, `memory_expand` to showing, and the
    asymmetry is a decision rather than drift — so it is pinned here instead of
    being left to a reader.

    `memory_recall` is a SEARCH: enrichment must not surface what the search
    beside it hides. `memory_expand` is an explicit id lookup that already
    returns the named memory whatever its state, so suppressing only its
    neighbours made the tool asymmetric with itself — and it is the surface the
    proactive hook points callers at, where nothing signals that a handle is
    deprecated.

    This test existed, was deleted by accident during a restructure, and was
    restored only because a mutation sweep noticed that nothing failed when the
    default flipped back. A deleted test is invisible; a green sweep cell is not.
    """
    import inspect

    from genesis.mcp.memory import core as core_mod

    recall = inspect.signature(core_mod.memory_recall.fn).parameters
    expand = inspect.signature(core_mod.memory_expand.fn).parameters
    assert recall["include_deprecated"].default is False, (
        "memory_recall must keep hiding by default — graph enrichment must not "
        "surface what the search beside it hides"
    )
    assert expand["include_deprecated"].default is True, (
        "memory_expand must default to showing neighbours: it already returns "
        "the deprecated memory itself, so hiding only its edges is incoherent"
    )
