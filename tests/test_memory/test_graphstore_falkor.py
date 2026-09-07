"""FalkorGraphStore — contract, degradation, and the lever that selects it.

These are the behaviours a second GraphStore must not get wrong: that
unreachable raises rather than answering empty, that the facade degrades in the
right order, and that the lever stays on networkx until someone moves it.

Two tiers, and the split is deliberate. Most tests here are CONTRACT tests that
stub the client, so they run anywhere. But contract tests alone proved
insufficient: a mutation sweep found five ways to break the traversal's
semantics that every one of them survived — reading the FIRST hop's label
instead of the last, inverting best-parent-wins, reversing every projected edge,
marking the whole projection deprecated, dropping the result ordering. All five
are invisible to a test that asserts on a query STRING, so the second tier
executes real Cypher against a live engine (`engine_gated`), and that is the
tier that kills them.

Engine-gated tests are SKIPPED where the engine is not armed — CI has no engine,
so they are not a substitute for the contract tier, they are a supplement to it.
They write only `test_`-prefixed graph keys and delete them in a `finally`; they
must never touch the canonical `genesis_memory` projection.

RUNNING THE ENGINE-GATED TIER, because a plain invocation will NOT run it:

    pytest tests/test_memory/test_graphstore_falkor.py --noconftest -q \\
        -o asyncio_mode=auto

`--noconftest` is required from an environment that has the `falkordb` client but
not Genesis's full dependency tree (the repo conftest imports the world). Two
consequences worth knowing rather than discovering:

* It escapes the repo's test lock (`genesis/util/pytest_lock.py` says so
  explicitly) and every autouse safety fixture. These tests are safe under that
  because they build their own tmp SQLite and touch only `test_`-prefixed graph
  keys — contained by their own discipline, not by the harness. Do not add a
  test here that writes anywhere else without re-checking that.
* Two sessions running this tier at once contend on the SHARED live engine,
  which no test lock covers. The staging keys are pid-suffixed so projections
  cannot corrupt each other, but the `test_`-prefixed graphs are not.

Without the flag the tier reports three SKIPs, which looks identical to passing
in a summary line — and the five semantic mutations it exists to kill quietly
come back into range.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from genesis.env import falkordb_socket_path
from genesis.memory import graph as graph_mod
from genesis.memory import graphstore_falkor as falkor_mod
from genesis.memory.graphstore import GraphNode, GraphUnavailableError
from genesis.memory.graphstore_falkor import FalkorGraphStore

pytestmark = pytest.mark.asyncio


async def test_the_store_satisfies_the_seam():
    """Same conformance check the NetworkX store carries, applied to this one.

    The protocol has no runtime enforcement — CI runs no type checker — so
    membership is asserted rather than assumed.
    """
    store = FalkorGraphStore()
    for member in ("name", "traverse", "centrality", "invalidate"):
        assert hasattr(store, member), f"GraphStore contract member missing: {member}"
    assert isinstance(store.name, str) and store.name


async def test_an_unreachable_engine_raises_and_never_answers_empty(tmp_path):
    """THE contract. An empty list means "no neighbours"; unreachable must not
    borrow that sentence.

    This is the defect the whole seam exists to prevent: `centrality_scores`
    returning [] once let dream-centrality read "no bridges", wipe
    centrality_cache, and silently disarm the importance shield.

    `_FALKOR_AVAILABLE` is forced TRUE and the client stubbed to fail on
    CONNECT. Without that this test is VACUOUS — verified by mutation: the
    client is absent in CI and in the prod venv, so it short-circuits on the
    not-importable branch and passes even with BOTH raise sites swallowing.
    That branch has its own test; this one must exercise the socket path.
    """

    class _RefusingClient:
        def __init__(self, *a, **k):
            raise ConnectionError("no such socket")

    store = FalkorGraphStore(socket_path=str(tmp_path / "absent.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _RefusingClient),
        pytest.raises(GraphUnavailableError, match="cannot reach"),
    ):
        await store.traverse(None, "any-root", max_depth=2, min_strength=0.3)


async def test_a_query_that_fails_mid_flight_is_unavailable_not_empty(tmp_path):
    """The second half of the same contract, and the one that matters at
    runtime: the engine answered the connect and then failed the QUERY.

    BusyLoadingError lands here — the engine refuses reads for ~0.9s after a
    restart while it loads a snapshot. Returning [] for that window would tell
    every reader the graph is empty exactly when it is merely starting.
    """

    class _Graph:
        async def query(self, *a, **k):
            raise RuntimeError("LOADING Redis is loading the dataset in memory")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def select_graph(self, _key):
            return _Graph()

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _Client),
        pytest.raises(GraphUnavailableError, match="query failed"),
    ):
        await store.traverse(None, "r", max_depth=2, min_strength=0.3)


async def test_centrality_refuses_rather_than_substituting_a_metric(tmp_path):
    """FalkorDB has no betweenness, and the honest answer is to say so.

    Returning PageRank or degree would change WHICH memories the importance
    shield protects while every caller kept working — the seam's docstring
    forbids exactly this.
    """
    store = FalkorGraphStore(socket_path=str(tmp_path / "absent.sock"))
    with pytest.raises(GraphUnavailableError, match="betweenness"):
        await store.centrality(None, top_n=10)


async def test_invalidate_is_safe_without_a_database_handle():
    """Every memory_links writer calls this through a lazy import, holding no
    handle. It must stay cheap and total."""
    FalkorGraphStore().invalidate()  # must not raise


async def test_a_missing_client_library_is_unavailable_not_empty(tmp_path):
    """Most installs will never have the client. That is unavailability, and it
    must reach the caller as a raise so the facade can fall back."""
    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", False),
        pytest.raises(GraphUnavailableError, match="not importable"),
    ):
        await store.traverse(None, "r", max_depth=1, min_strength=0.0)


async def test_results_are_sorted_here_not_merely_trusted_from_the_engine(tmp_path):
    """The seam's `(depth, -strength)` order is enforced in THIS process.

    Found by mutation: deleting the sort left the engine-backed round-trip test
    green, because the engine's own ORDER BY already returns rows in that order,
    so a real projection cannot distinguish "we sort" from "the engine happened
    to". That is a sibling layer masking the mutation, not a vacuous test — and
    it means the Python sort's guarantee needs a test the engine cannot satisfy
    for it. So this one hands back rows in deliberately WRONG order and requires
    them to come out right, which is the only way the guarantee is pinned if a
    future engine, dialect change, or query rewrite stops ordering for us.
    """

    class _Graph:
        async def query(self, *a, **k):
            class _R:
                # [id, depth, strength, link_type] — scrambled on purpose.
                result_set = [
                    ["far", 2, 0.9, "z"],
                    ["near_weak", 1, 0.1, "z"],
                    ["near_strong", 1, 0.9, "z"],
                ]

            return _R()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def select_graph(self, _key):
            return _Graph()

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _Client),
    ):
        nodes = await store.traverse(None, "root", max_depth=2, min_strength=0.0)

    assert [n.memory_id for n in nodes] == ["near_strong", "near_weak", "far"], (
        "traverse must impose (depth, -strength), not pass the engine's order through"
    )


async def test_a_zero_depth_traversal_asks_the_engine_nothing(tmp_path):
    """Guard the query builder: `*1..0` is not a legal variable-length pattern,
    and an unreachable socket would otherwise mask that as unavailability."""
    store = FalkorGraphStore(socket_path=str(tmp_path / "absent.sock"))
    assert await store.traverse(None, "r", max_depth=0, min_strength=0.0) == []


async def test_timestamps_that_cannot_be_parsed_stay_visible():
    """The predicate's fail direction. A NULL invalid_at means "never expires",
    so an unparseable one must mean the same — hiding a memory because of a
    formatting problem would silently shrink what recall can reach."""
    assert falkor_mod._to_epoch(None) is None
    assert falkor_mod._to_epoch("") is None
    assert falkor_mod._to_epoch("not-a-timestamp") is None
    assert falkor_mod._to_epoch("2026-09-07T00:00:00Z") == 1788739200


async def test_the_traversal_query_uses_the_named_path_form():
    """MEASURED against the live engine: a bare relationship-list variable binds
    as an Edge, not a List, so `ALL(x IN l ...)` throws at EVERY length —
    including *2..2. Only `ALL(x IN relationships(p) ...)` works.

    Pinned as structure because the failure is total and silent to a reader:
    the query simply always errors, and the store then looks permanently
    unavailable rather than wrong.
    """
    q = falkor_mod._TRAVERSE
    assert "MATCH p=" in q, "not the named-path form"
    assert "relationships(p)" in q
    assert "nodes(p)" in q


async def test_the_validity_predicate_matches_the_sqlite_one():
    """Both stores must hide the SAME memories, or which store answers changes
    what the model is shown — the one thing the seam promises it cannot."""
    v = falkor_mod._VALID
    assert "invalid_epoch IS NULL" in v, "a NULL invalid_at must stay visible"
    assert "> $now" in v, "expiry must be evaluated at QUERY time, not projection time"
    assert "deprecated = 0" in v
    # BOTH terms must be NULL-safe, and this one is the trap. MEASURED on the
    # live engine: for a node with no `deprecated` property, `x.deprecated = 0`
    # is NULL, so ALL(...) is NULL and the WHERE drops the path -- while
    # SQLite's `deprecated != 0` on NULL leaves the row OUT of the invalid set,
    # i.e. VISIBLE. Opposite answers from the same three-valued logic, in the
    # direction that HIDES memories. Without this assertion the test above
    # passes on the broken form as happily as on the fixed one.
    assert "deprecated IS NULL" in v, (
        "a node with no `deprecated` property must stay visible, matching SQLite"
    )


# ── the lever ─────────────────────────────────────────────────────────


async def test_the_lever_is_inert_until_someone_moves_it(monkeypatch):
    """Merging this must change nothing. The default selects NetworkX, and so
    does every unreadable or unrecognised value."""
    from genesis.memory import graphstore_config as cfg

    monkeypatch.delenv("GENESIS_FALKORDB_STORE_DISABLED", raising=False)
    assert cfg.effective_mode() == "networkx"

    for bad in ({"mode": "nonsense"}, {"mode": False}, {"enabled": False, "mode": "falkordb"}):
        with patch.object(cfg, "load_config", return_value={**cfg.DEFAULTS, **bad}):
            assert cfg.effective_mode() == "networkx", f"{bad} did not degrade"


async def test_the_kill_switch_beats_the_file(monkeypatch):
    """An operator must be able to pin reads to NetworkX without editing config."""
    from genesis.memory import graphstore_config as cfg

    monkeypatch.setenv("GENESIS_FALKORDB_STORE_DISABLED", "1")
    with patch.object(cfg, "load_config", return_value={"enabled": True, "mode": "falkordb"}):
        assert cfg.effective_mode() == "networkx"


async def test_centrality_is_pinned_to_networkx_whatever_the_lever_says():
    """Flipping to FalkorDB must NOT disable the importance shield.

    FalkorDB's centrality raises by design, and `centrality_scores` has no
    fallback by design. Routing centrality through the lever would compose
    those two correct decisions into a silent shutdown, so the facade reads
    the lever for TRAVERSAL only.
    """
    import inspect

    src = inspect.getsource(graph_mod.centrality_scores)
    assert "_traversal_store" not in src, (
        "centrality must not follow the traversal lever — FalkorDB cannot compute it"
    )
    assert "_store.centrality" in src


async def test_the_facade_degrades_falkordb_to_networkx_before_sql(monkeypatch):
    """Ordering matters: NetworkX answers the same question with the same
    visibility predicate, so it is a far smaller step down than the CTE."""
    calls: list[str] = []

    class _Unreachable:
        name = "falkordb"

        async def traverse(self, *a, **k):
            calls.append("falkordb")
            raise GraphUnavailableError("engine down")

        def invalidate(self):
            return None

    class _Nx:
        name = "networkx"

        async def traverse(self, *a, **k):
            calls.append("networkx")
            return [GraphNode(memory_id="m", link_type="related_to", depth=1, strength=0.9)]

        def invalidate(self):
            return None

    async def _never(*a, **k):
        calls.append("cte")
        return []

    monkeypatch.setattr(graph_mod, "_store", _Nx())
    monkeypatch.setattr(graph_mod, "_traversal_store", lambda: _Unreachable())
    monkeypatch.setattr(graph_mod, "_traverse_cte", _never)

    result = await graph_mod.traverse(None, "root", max_depth=2, min_strength=0.3)

    assert calls == ["falkordb", "networkx"], f"wrong degrade order: {calls}"
    assert "cte" not in calls, "reached SQL while NetworkX could still answer"
    assert [n.memory_id for n in result.nodes] == ["m"]


async def test_invalidate_reaches_every_store_not_just_the_selected_one(monkeypatch):
    """A store that missed invalidations while unselected would serve a stale
    projection the moment the lever chose it again."""
    seen: list[str] = []

    class _S:
        def __init__(self, tag):
            self.tag = tag
            self.name = tag

        def invalidate(self):
            seen.append(self.tag)

    monkeypatch.setattr(graph_mod, "_store", _S("nx"))
    monkeypatch.setattr(graph_mod, "_falkor_store", _S("falkor"))
    graph_mod.invalidate_graph_cache()
    assert sorted(seen) == ["falkor", "nx"]


# ── the projection, without an engine ─────────────────────────────────


async def test_metadata_mirrors_deprecated_as_a_comparable_int(tmp_path):
    """`deprecated` is projected as 0/1 because the Cypher compares it to 0.

    Pinned because mutating this mapping to a constant 1 marks the ENTIRE
    projection deprecated, so the validity predicate hides every node and the
    graph answers nothing for every root — a total outage that no test asserting
    on query strings can see.

    A NULL `deprecated` must mirror as 0. SQLite's `deprecated != 0` is NULL for
    that row, which leaves it OUT of the invalid set, i.e. visible; projecting
    NULL through would then hit the Cypher's own NULL-handling instead of
    stating the answer here, where it is cheap.
    """
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "meta.db"))
    await db.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
    )
    await db.executemany(
        "INSERT INTO memory_metadata VALUES (?, ?, ?)",
        [("live", None, 0), ("dead", None, 1), ("unset", None, None)],
    )
    await db.commit()
    try:
        meta = await FalkorGraphStore._metadata(db)
    finally:
        await db.close()

    assert meta["live"] == (None, 0)
    assert meta["dead"] == (None, 1)
    assert meta["unset"] == (None, 0), "a NULL deprecated must project as VISIBLE"


async def test_the_base_config_file_matches_the_defaults():
    """`config/graphstore.yaml` and DEFAULTS must not drift apart.

    The sibling domains carry this lock (tests/test_ego/test_reconcile_config.py).
    Without it the shipped file could say one thing and the fallback another, and
    the disagreement would only surface on an install whose file failed to read.
    """
    import yaml

    from genesis.memory import graphstore_config as cfg

    # Resolved from THIS FILE, matching tests/ego/test_reconcile_config.py, not
    # via repo_root() — that honours GENESIS_REPO_ROOT, which the worktree test
    # convention points at the main tree, where a file added on a branch does
    # not exist yet. Same reason the sibling does it this way.
    base = Path(__file__).parents[2] / "config" / "graphstore.yaml"
    assert yaml.safe_load(base.read_text()) == cfg.DEFAULTS


# ── engine-gated: real Cypher against a live engine ───────────────────
#
# Everything above stubs the client. That is what let five semantic mutations
# survive a full green suite, so these execute the real thing. They SKIP where
# the engine is not armed (CI), which is why they supplement the contract tier
# rather than replacing it.

_ENGINE_ARMED = falkor_mod._FALKOR_AVAILABLE and Path(falkordb_socket_path()).exists()

engine_gated = pytest.mark.skipif(
    not _ENGINE_ARMED,
    reason="graph engine not armed here (no falkordb client, or no socket)",
)

#: Deliberately `test_`-prefixed and never the canonical `genesis_memory`.
_TEST_GRAPH_KEY = "test_f2_roundtrip"


async def _diamond_db(tmp_path):
    """A SQLite fixture carrying exactly what `project()` reads.

    The shape is chosen so each mutation that survived the contract tier breaks a
    DIFFERENT assertion:

      root -B(0.9,"aaa")-> B        B is reachable at depth 1 AND at depth 2
      root -C(0.5,"mmm")-> C        via C, where the depth-2 edge is STRONGER
                C -(0.95,"zzz")-> D  D's last hop differs from its first
                C -(1.0,"zzz")--> B
    """
    import aiosqlite

    db = await aiosqlite.connect(str(tmp_path / "diamond.db"))
    await db.execute(
        "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT, strength REAL)"
    )
    await db.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
    )
    await db.executemany(
        "INSERT INTO memory_links VALUES (?, ?, ?, ?)",
        [
            ("root", "B", "aaa", 0.9),
            ("root", "C", "mmm", 0.5),
            ("C", "D", "zzz", 0.95),
            ("C", "B", "zzz", 1.0),
        ],
    )
    await db.executemany(
        "INSERT INTO memory_metadata VALUES (?, NULL, 0)",
        [("root",), ("B",), ("C",), ("D",)],
    )
    await db.commit()
    return db


async def _drop_test_graph(store) -> None:
    import os

    conn = await store._connection()
    await conn.delete(store._graph_key)
    # Staging is per-run (pid-suffixed) so concurrent projections cannot delete
    # each other's half-built graph.
    await conn.delete(f"{store._graph_key}_staging_{os.getpid()}")


@engine_gated
async def test_a_real_projection_reports_the_last_hop_of_the_shallowest_path(tmp_path):
    """THE semantics test: project a fixture, traverse it, check what comes back.

    Kills, by construction, every mutation the string-asserting tests missed:
      * `relationships(p)[-1]` -> `[0]`  — D would report 0.5/"mmm" (first hop)
      * `ORDER BY d ASC` -> `DESC`       — B would report depth 2
      * projection's `{s,t}` swapped     — root has no out-edges, result empty
      * `deprecated` pinned to 1         — everything hidden, result empty
      * dropped result sort              — order is not (depth, -strength)
    """
    store = FalkorGraphStore(graph_key=_TEST_GRAPH_KEY)
    db = await _diamond_db(tmp_path)
    try:
        stats = await store.project(db)
        assert stats["nodes"] == 4, stats
        assert stats["edges"] == 4, stats

        nodes = await store.traverse(db, "root", max_depth=2, min_strength=0.3)
        by_id = {n.memory_id: n for n in nodes}
        assert set(by_id) == {"B", "C", "D"}, "edge direction or visibility is wrong"

        # Shallowest path wins even though the deeper one is stronger.
        assert by_id["B"].depth == 1
        assert by_id["B"].strength == pytest.approx(0.9)
        assert by_id["B"].link_type == "aaa"

        # root->C->D: the reported label must come from the LAST hop, and this
        # is the assertion that fails when the query reads relationships(p)[0].
        assert by_id["D"].depth == 2
        assert by_id["D"].strength == pytest.approx(0.95)
        assert by_id["D"].link_type == "zzz"

        # The seam's documented order.
        assert [n.memory_id for n in nodes] == ["B", "C", "D"]
    finally:
        await _drop_test_graph(store)
        await db.close()


@engine_gated
async def test_projecting_twice_swaps_atomically_and_leaves_no_staging_key(tmp_path):
    """A projection is re-runnable, and leaves nothing behind.

    The first version of `project()` was NOT idempotent — `DETACH DELETE` drops
    nodes but keeps the index, so the second run died on "already indexed". Only
    running it twice found that. It now builds under a staging key and RENAMEs,
    so this also pins that the staging key does not survive the swap.
    """
    store = FalkorGraphStore(graph_key=_TEST_GRAPH_KEY)
    db = await _diamond_db(tmp_path)
    try:
        first = await store.project(db)
        second = await store.project(db)
        assert first == second, "a re-projection of identical data must be identical"

        import os

        conn = await store._connection()
        assert not await conn.exists(f"{_TEST_GRAPH_KEY}_staging_{os.getpid()}"), (
            "the staging key must not outlive the swap"
        )
        nodes = await store.traverse(db, "root", max_depth=2, min_strength=0.3)
        assert len(nodes) == 3, "the graph must still answer after a re-projection"
    finally:
        await _drop_test_graph(store)
        await db.close()


@engine_gated
async def test_an_empty_projection_is_unavailable_not_neighbourless(tmp_path):
    """THE blocker: an unbuilt projection must raise, never answer [].

    The seam blesses "root absent -> []" as genuinely no-neighbours. An unbuilt
    projection makes EVERY root absent, so without this the store answers [] for
    every memory in the system while staying perfectly reachable — nothing
    raises, nothing falls back, nothing logs, and the health probe is documented
    not to care. The failure would be silent and total.
    """
    store = FalkorGraphStore(graph_key="test_f2_empty")
    try:
        conn = await store._connection()
        await conn.delete("test_f2_empty")
        # Create the key with an index but no nodes — reachable, and empty.
        await store._ensure_index(key="test_f2_empty")
        with pytest.raises(GraphUnavailableError, match="holds no nodes"):
            await store.traverse(None, "anything", max_depth=2, min_strength=0.3)
        # UNLATCHED, and this is the assertion that matters. An earlier version
        # cached "the projection exists" for the process lifetime; because the
        # engine holds no persistence, a restart then emptied the graph while the
        # cached answer said otherwise, and every traversal returned [] silently
        # until the SERVER restarted. Raising only the first time is the bug.
        with pytest.raises(GraphUnavailableError, match="holds no nodes"):
            await store.traverse(None, "anything", max_depth=2, min_strength=0.3)
    finally:
        conn = await store._connection()
        await conn.delete("test_f2_empty")
