"""Synthetic-fixture parity: NX control ≡ SQL oracle on a tiny hand-computed graph.

The fixture includes a STRADDLING parallel edge (m1->m2 has supports 0.9 AND
categorized_as 0.4) so it distinguishes the MultiDiGraph control from a lossy
DiGraph — see ``test_digraph_control_drops_straddle_edge`` (the permanent
RED-documenting test: a DiGraph control fails q1).

Expected sets are hand-derived in ``EXPECTED`` and asserted against BOTH the SQL
oracle and the NX control, plus oracle≡control.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import deque

import pytest

from genesis.eval.graph_bakeoff import parity
from genesis.eval.graph_bakeoff.anchors import resolve_anchors
from genesis.eval.graph_bakeoff.engines.nx_incremental import NxIncrementalEngine
from genesis.eval.graph_bakeoff.queries import by_id

# (source, target, link_type, strength)
EDGES = [
    ("m1", "m2", "supports", 0.9),
    ("m1", "m2", "categorized_as", 0.4),  # parallel + straddles the 0.5 threshold
    ("m2", "m3", "supports", 0.8),
    ("m3", "m4", "supports", 0.3),  # below 0.5 -> not traversed by q1
    ("m1", "m5", "related_to", 0.6),
    ("m5", "m6", "succeeded_by", 0.7),
    ("m6", "m7", "succeeded_by", 0.7),
    ("m2", "m8", "decided", 0.7),
    ("m2", "m8", "contradicts", 0.7),  # m2 older than m8 -> does NOT qualify for q6
    ("m8", "m3", "contradicts", 0.7),  # m8 newer than m3 -> qualifies for q6
]
# memory_id -> (valid_at, invalid_at, deprecated, deprecated_at, created_at)
META = {
    "m1": ("2026-01-01", None, 0, None, "2026-01-01"),
    "m2": ("2026-01-01", None, 0, None, "2026-01-01"),
    "m3": ("2026-02-01", "2026-05-01", 0, None, "2026-02-01"),  # invalid as-of 2026-06-01
    "m4": ("2026-01-01", None, 0, None, "2026-01-01"),
    "m5": ("2026-07-01", None, 0, None, "2026-07-01"),  # not yet valid as-of 2026-06-01
    "m6": ("2026-01-01", None, 0, None, "2026-01-01"),
    "m7": ("2026-01-01", None, 0, None, "2026-01-01"),
    "m8": ("2026-03-01", None, 0, None, "2026-03-01"),
}
MENTIONS = [("m1", "eA"), ("m1", "eB"), ("m4", "eB")]

# Deterministic anchors (D pinned so q3 is stable regardless of median logic).
ANCHORS = {
    "q1_entity_dossier": {"entity_id": "eA"},
    "q2_decision_chain": {},
    "q3_as_of_neighborhood": {"memory_id": "m1", "as_of": "2026-06-01"},
    "q4_chain_closure": {"root": "m5"},
    "q5_cross_entity": {"entity_a": "eA", "entity_b": "eB"},
    "q6_contradiction_sweep": {},
}
EXPECTED = {
    "q1_entity_dossier": {"m1", "m2", "m3", "m5", "m6", "m8"},
    "q2_decision_chain": {"m2"},
    "q3_as_of_neighborhood": {"m2", "m8"},
    "q4_chain_closure": {"m5", "m6", "m7"},
    "q5_cross_entity": {"m1", "m2", "m5"},
    "q6_contradiction_sweep": {"m8"},
}


@pytest.fixture
def fixture_db(tmp_path):
    path = tmp_path / "synthetic.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT, strength REAL);
        CREATE TABLE memory_metadata (memory_id TEXT, valid_at TEXT, invalid_at TEXT,
            deprecated INTEGER, deprecated_at TEXT, created_at TEXT);
        CREATE TABLE entity_mentions (memory_id TEXT, entity_id TEXT);
        CREATE TABLE entities (entity_id TEXT, name TEXT);
        CREATE TABLE entity_links (source_id TEXT, target_id TEXT, link_type TEXT,
            valid_at TEXT, invalid_at TEXT);
        """
    )
    conn.executemany("INSERT INTO memory_links VALUES (?,?,?,?)", EDGES)
    conn.executemany(
        "INSERT INTO memory_metadata VALUES (?,?,?,?,?,?)",
        [(mid, *vals) for mid, vals in META.items()],
    )
    conn.executemany("INSERT INTO entity_mentions VALUES (?,?)", MENTIONS)
    conn.commit()
    conn.close()
    return str(path)


@pytest.mark.parametrize("qid", list(EXPECTED))
def test_oracle_matches_expected(fixture_db, qid):
    conn = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
    try:
        got = parity.oracle_for(qid, conn, ANCHORS, by_id(qid).params)
    finally:
        conn.close()
    assert got == EXPECTED[qid], f"oracle {qid}: {got} != {EXPECTED[qid]}"


@pytest.mark.parametrize("qid", list(EXPECTED))
def test_control_matches_expected(fixture_db, qid):
    async def _run():
        eng = NxIncrementalEngine()
        await eng.load(fixture_db)
        eng.anchors = ANCHORS
        return await eng.run(by_id(qid), by_id(qid).params)

    result = asyncio.run(_run())
    assert result.node_ids == EXPECTED[qid], f"control {qid}: {result.node_ids} != {EXPECTED[qid]}"


def test_oracle_equals_control(fixture_db):
    """The S1 gate condition on the synthetic fixture: every exact_set query agrees."""

    async def _run():
        eng = NxIncrementalEngine()
        await eng.load(fixture_db)
        eng.anchors = ANCHORS
        conn = sqlite3.connect(f"file:{fixture_db}?mode=ro", uri=True)
        try:
            for qid in EXPECTED:
                oset = parity.oracle_for(qid, conn, ANCHORS, by_id(qid).params)
                cres = await eng.run(by_id(qid), by_id(qid).params)
                assert oset == cres.node_ids, f"{qid}: oracle {oset} != control {cres.node_ids}"
        finally:
            conn.close()

    asyncio.run(_run())


def test_digraph_control_drops_straddle_edge(fixture_db):
    """RED-documenting: a plain DiGraph collapses the m1->m2 parallel edges to the
    last-written (categorized_as 0.4), so a 0.5-threshold BFS from m1 fails to reach
    m2 (and thus m3, m8). This is WHY the control is a MultiDiGraph. If this ever
    passes with a DiGraph, the parallel-edge fidelity guarantee is broken."""
    import networkx as nx

    from genesis.memory.graph import _bfs_with_strength

    dg = nx.DiGraph()
    for s, t, lt, st in EDGES:
        dg.add_edge(s, t, link_type=lt, strength=st)  # last-write-wins collapses m1->m2 to 0.4
    reached = {n.memory_id for n in _bfs_with_strength(dg, "m1", max_depth=2, min_strength=0.5)}
    reached.add("m1")
    assert "m2" not in reached, (
        "DiGraph unexpectedly kept the strong parallel edge — fidelity assumption changed"
    )

    # And the MultiDiGraph control DOES reach m2 (the fix).
    async def _run():
        eng = NxIncrementalEngine()
        await eng.load(fixture_db)
        eng.anchors = ANCHORS
        return await eng.run(by_id("q1_entity_dossier"), by_id("q1_entity_dossier").params)

    assert "m2" in asyncio.run(_run()).node_ids


def test_resolve_anchors_on_synthetic(fixture_db):
    """The frozen anchor_sql resolves the intended rows on the fixture."""
    a = resolve_anchors(fixture_db)
    assert a["q4_chain_closure"]["root"] == "m5"  # only chain root
    # q5: eA<eB co-occur on m1
    assert {a["q5_cross_entity"]["entity_a"], a["q5_cross_entity"]["entity_b"]} == {"eA", "eB"}
    assert a["q3_as_of_neighborhood"]["as_of"] is not None  # median created_at resolved
    assert a["q2_decision_chain"]["contradicts_edges"] == 2


def _bfs_supports_reachable(edges, start, targets, max_depth):
    """Independent re-implementation for q2 cross-check (avoid mirroring the impl)."""
    adj = {}
    for s, t, lt, _ in edges:
        if lt == "supports":
            adj.setdefault(s, []).append(t)
    seen = {start}
    q = deque([(start, 0)])
    while q:
        n, d = q.popleft()
        if n in targets:
            return True
        if d < max_depth:
            for t in adj.get(n, []):
                if t not in seen:
                    seen.add(t)
                    q.append((t, d + 1))
    return False


def _chain_db(tmp_path, n_hops: int) -> str:
    """A single succeeded_by chain c0->c1->...->c{n_hops} (n_hops edges)."""
    path = tmp_path / "chain.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT, strength REAL)"
    )
    # load() also reads these (empty here) — mirror the real snapshot's tables.
    conn.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT, valid_at TEXT, invalid_at TEXT, "
        "deprecated INTEGER, deprecated_at TEXT, created_at TEXT)"
    )
    conn.execute("CREATE TABLE entity_mentions (memory_id TEXT, entity_id TEXT)")
    conn.executemany(
        "INSERT INTO memory_links VALUES (?,?,?,?)",
        [(f"c{i}", f"c{i + 1}", "succeeded_by", 0.7) for i in range(n_hops)],
    )
    conn.commit()
    conn.close()
    return str(path)


@pytest.mark.parametrize("max_depth,expected_n", [(2, 3), (30, 6)])
def test_q4_honors_max_depth_cap(tmp_path, max_depth, expected_n):
    """q4 must TRUNCATE at max_depth (regression: the cap was dead code). A 5-hop
    chain (c0..c5) capped at 2 yields {c0,c1,c2}; the full frozen cap (30) yields
    all 6. Oracle and control must agree at the boundary, not just on short data."""
    db = _chain_db(tmp_path, n_hops=5)  # c0->..->c5
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        oset = parity.oracle_q4(conn, "c0", link_type="succeeded_by", max_depth=max_depth)
    finally:
        conn.close()

    async def _run():
        eng = NxIncrementalEngine()
        await eng.load(db)
        return eng._q4("c0", link_type="succeeded_by", max_depth=max_depth)

    cset = asyncio.run(_run())
    assert oset == cset, f"oracle {oset} != control {cset}"
    assert len(oset) == expected_n, f"max_depth={max_depth}: {sorted(oset)}"
    if max_depth == 2:
        assert oset == {"c0", "c1", "c2"}


def test_q4_cap_dispatch_uses_frozen_param(tmp_path):
    """The dispatch path (oracle_for / engine.run) must thread max_depth from the
    frozen QuerySpec, not silently drop it (the reviewed bug was in dispatch)."""
    db = _chain_db(tmp_path, n_hops=40)  # longer than the frozen cap of 30
    q4 = by_id("q4_chain_closure")
    anchors = {"q4_chain_closure": {"root": "c0"}}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        oset = parity.oracle_for("q4_chain_closure", conn, anchors, q4.params)
    finally:
        conn.close()
    # Frozen cap is 30 -> c0..c30 = 31 nodes, NOT all 41.
    assert len(oset) == 31, f"expected frozen-cap truncation to 31, got {len(oset)}"


def test_q2_semantics_independent_crosscheck():
    """q2 result derived independently: only m2 is a decision AND a contradicts node."""
    decisions = {s for s, _, lt, _ in EDGES if lt == "decided"}
    contradicts_nodes = {x for s, t, lt, _ in EDGES if lt == "contradicts" for x in (s, t)}
    got = {d for d in decisions if _bfs_supports_reachable(EDGES, d, contradicts_nodes, 3)}
    assert got == EXPECTED["q2_decision_chain"]
