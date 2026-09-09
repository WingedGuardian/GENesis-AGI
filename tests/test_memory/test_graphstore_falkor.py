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

import os
import time
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


async def test_a_truthy_non_boolean_enabled_does_not_arm_the_backend(monkeypatch):
    """`enabled: "false"` must DISABLE, not enable.

    The overlay is YAML a person hand-edits, and a quoted `false` is a non-empty
    string — truthy. A falsiness test therefore read an operator's clearest
    attempt to switch the backend OFF as permission to switch it on, in a module
    whose entire posture is degrade-toward-networkx. Only `settings_update`
    type-checks this key; the file does not, and the file is the surface being
    edited.
    """
    from genesis.memory import graphstore_config as cfg

    for value in ("false", "no", "off", 1, "true", [], {}):
        monkeypatch.setattr(cfg, "load_config", lambda v=value: {"enabled": v, "mode": "falkordb"})
        assert cfg.effective_mode() == "networkx", (
            f"enabled={value!r} is not exactly True and must degrade to networkx"
        )

    monkeypatch.setattr(cfg, "load_config", lambda: {"enabled": True, "mode": "falkordb"})
    assert cfg.effective_mode() == "falkordb", "a real boolean True must still arm it"


async def test_expiry_keeps_subsecond_precision():
    """Flooring an expiry to whole seconds hides a memory up to a second early.

    MEASURED on the live engine 2026-09-08, both directions: with the stored
    epoch and `now` both truncated to 100, `invalid_epoch > now` is false and the
    node is HIDDEN; at full precision (100.9 against 100.1) it is VISIBLE — which
    is what SQLite and NetworkX answer, since they compare the full ISO strings.
    Genesis writes microseconds (`db/timeutil.py::canonical_iso`), so the
    fractional part is real data.
    """
    epoch = falkor_mod._to_epoch("2026-09-08T12:00:00.900000+00:00")
    assert epoch is not None
    assert epoch % 1 != 0, "the fractional second must survive the conversion"

    just_before = epoch - 0.8  # same whole second, earlier fraction
    assert int(just_before) == int(epoch), "the two must share a whole second, or this is vacuous"
    assert not falkor_mod._is_hidden((epoch, 0), just_before), (
        "a memory expiring later this second is still visible"
    )
    assert falkor_mod._is_hidden((epoch, 0), epoch + 0.1), "and hidden once the moment passes"


async def test_the_config_cache_notices_a_rewrite_that_keeps_its_timestamp(tmp_path, monkeypatch):
    """A cache keyed on a float mtime alone can pin the old config forever.

    `st_mtime` carries the filesystem's resolution, so a rewrite landing inside
    one tick is invisible to it — a coarse filesystem, a fast `settings_update`,
    or an editor or restore that preserves timestamps. The module's contract is
    that a lever change takes effect on the next traversal, and a cache that
    cannot see the write breaks exactly that.

    The timestamp is pinned identically on both writes, which is the whole point:
    if the key were mtime-only this would be indistinguishable from no write.
    The write is an ATOMIC REPLACE, which is how every writer here actually puts
    a config down (`_atomic_yaml_write` does mkstemp + rename, and a restore
    writes a new file too) — so the inode moves even when the clock does not.

    The one shape this deliberately does NOT claim to catch is an IN-PLACE
    rewrite that keeps the size AND the timestamp. No writer in this repo does
    that, and closing it would mean hashing the file — which is the read the
    cache exists to avoid. `reset_config_cache()` is the escape if one appears.
    """
    import os as _os

    from genesis.memory import graphstore_config as cfg

    base = tmp_path / "graphstore.yaml"
    base.write_text("enabled: true\nmode: networkx\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: base)
    monkeypatch.setattr(cfg, "local_overlay_key", lambda _p: (0, 0, 0))
    cfg.reset_config_cache()

    assert cfg.load_config()["mode"] == "networkx"
    stamp = base.stat().st_mtime_ns
    before_ino = base.stat().st_ino

    # Atomic replace, then pin the timestamp back — same clock, same byte count
    # (both modes are nine characters), different inode.
    tmp = tmp_path / "graphstore.yaml.tmp"
    tmp.write_text("enabled: true\nmode: falkordb\n")
    _os.replace(tmp, base)
    _os.utime(base, ns=(stamp, stamp))

    st = base.stat()
    assert st.st_mtime_ns == stamp, "the fixture must really pin the mtime"
    assert st.st_ino != before_ino, "the fixture must really replace the file"

    assert cfg.load_config()["mode"] == "falkordb", (
        "an atomic replace that keeps its timestamp must still invalidate the cache"
    )
    cfg.reset_config_cache()


async def test_an_unparseable_timestamp_takes_sqlites_answer(tmp_path):
    """ "Unparseable" and "NULL" are different, and SQLite treats them differently.

    SQLite never parses `invalid_at`: it compares the raw TEXT lexicographically
    against an ISO `now`. So `2020-bad` sorts before a 2026 timestamp and is
    HIDDEN there — while a mirror that read it as unparseable-therefore-NULL
    kept the same memory VISIBLE. The schema does not constrain the format, so
    nothing prevents such a value existing.

    MEASURED 2026-09-08: 0 of 1,633 live non-null values are unparseable, so this
    is latent. Both directions are asserted, because a fix that hid everything
    malformed would pass a one-sided test.
    """
    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
        )
        await db.executemany(
            "INSERT INTO memory_metadata VALUES (?, ?, 0)",
            [
                ("past_garbage", "2020-bad"),  # sorts BEFORE now -> SQLite hides
                ("future_garbage", "9999-bad"),  # sorts AFTER now -> SQLite shows
                ("null_stamp", None),  # NULL -> visible on both
                ("real", "2099-01-01T00:00:00+00:00"),  # parseable, future
            ],
        )
        await db.commit()

        meta = await FalkorGraphStore._metadata(db)
        now = time.time()

        assert falkor_mod._is_hidden(meta["past_garbage"], now), (
            "a malformed stamp sorting before now is hidden by SQLite and must be here too"
        )
        assert not falkor_mod._is_hidden(meta["future_garbage"], now), (
            "a malformed stamp sorting after now stays visible in SQLite — hiding "
            "everything malformed would be a different bug, not a fix"
        )
        assert not falkor_mod._is_hidden(meta["null_stamp"], now)
        assert not falkor_mod._is_hidden(meta["real"], now)
    finally:
        await db.close()


async def test_the_traversal_sends_an_unfloored_now(tmp_path, monkeypatch):
    """The conversion above is worthless if the query still floors the comparand.

    The clock is pinned rather than sampled. Asserting `now % 1 != 0` on a real
    `time.time()` is a ~1-in-10^6 flake whose failure would read as a genuine
    regression, and a test that cries wolf gets deleted by whoever hits it.
    """
    monkeypatch.setattr(falkor_mod.time, "time", lambda: 1757000000.75)
    seen: dict = {}

    class _Graph:
        async def query(self, _cypher, params):
            seen.update(params)
            raise RuntimeError("stop here — the parameters are the assertion")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def select_graph(self, _key):
            return _Graph()

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _Client),
        pytest.raises(GraphUnavailableError),
    ):
        await store.traverse(None, "root", max_depth=2, min_strength=0.3)

    assert seen["now"] == 1757000000.75, (
        "`now` must reach the engine unfloored — flooring it to 1757000000 hides a "
        f"memory expiring later in that second; got {seen['now']!r}"
    )


async def test_a_failure_mid_build_leaves_no_staging_graph(tmp_path):
    """Cleanup covers the WHOLE build, not just the swap.

    The earlier version wrapped only the rename, so a failure in index creation
    or in any batch orphaned the staging graph — and a staging graph is a full
    copy of the projection against a 512mb engine cap. The opening `delete`
    reclaims this PROCESS's own orphan next run, which bounds the leak at one per
    process; it does nothing for a process that never runs again, which is every
    CLI invocation and every server restart. Once the projector runs on a
    schedule this path stops being rare.
    """
    deleted: list[str] = []

    class _Conn:
        async def delete(self, key):
            deleted.append(key)

        async def rename(self, *a):  # pragma: no cover - never reached here
            raise AssertionError("the build failed before the swap")

        async def exists(self, *a):  # pragma: no cover
            return 1

        async def set(self, *a, **k):
            return True  # the cross-process publish claim is granted

        async def eval(self, *a, **k):  # pragma: no cover - release path
            return 1

    class _Graph:
        async def query(self, cypher, _params):
            if "CREATE INDEX" in cypher:
                return object()
            raise RuntimeError("engine died mid-batch")

    class _Client:
        connection = _Conn()

        def __init__(self, *a, **k):
            pass

        def select_graph(self, _key):
            return _Graph()

    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, "
            "link_type TEXT, strength REAL)"
        )
        await db.execute(
            "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
        )
        await db.execute("INSERT INTO memory_links VALUES ('a', 'b', 'related_to', 0.9)")
        await db.commit()

        store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"), graph_key="test_cleanup")
        with (
            patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
            patch.object(falkor_mod, "_FalkorDB", _Client),
            pytest.raises(GraphUnavailableError),
        ):
            await store.project(db)
    finally:
        await db.close()

    staging = f"test_cleanup_staging_{os.getpid()}"
    assert deleted.count(staging) == 2, (
        "the staging key must be deleted twice — once opening the build, once "
        f"cleaning up after it failed; saw {deleted}"
    )


async def test_two_projections_of_one_key_never_interleave(tmp_path):
    """Concurrent projections of the same key must serialise, not overlap.

    The staging key is per-PROCESS, so two coroutines projecting the same key in
    one process share it — and with the whole-build cleanup, the loser's cleanup
    wipes the winner's staging mid-build. The winner then writes its marker LAST
    and renames a partial graph that reads as complete: a silent wrong answer,
    which is the worst outcome this store can produce.

    Unreachable today (`project()` has one caller, the CLI, one run per process)
    but it is exactly the shape an in-server scheduled projector introduces, so
    the invariant is mechanical here rather than a note for a future PR.
    """
    import asyncio as _asyncio

    import aiosqlite

    order: list[str] = []

    class _Graph:
        def __init__(self, tag):
            self._tag = tag

        async def query(self, cypher, _params=None):
            if "CREATE INDEX" in cypher:
                order.append(f"{self._tag}:start")
                await _asyncio.sleep(0.05)  # a window for the other run to barge in
            elif "ProjectionMeta" in cypher:
                order.append(f"{self._tag}:end")
            return object()

    class _Conn:
        async def delete(self, _key):
            return 1

        async def rename(self, *_a):
            return True

        async def set(self, *a, **k):
            # The engine-side publish claim. Both runs are granted it here so
            # this test measures the IN-PROCESS lock; the cross-process claim
            # has its own test.
            return True

        async def eval(self, *a, **k):  # the EVAL release, not Python's eval
            return 1

    # ONE patch around the whole gather. Patching inside each coroutine has the
    # first to finish restore `_FALKOR_AVAILABLE` while the second is still
    # running — which is how this test failed on its first outing, for a reason
    # entirely unrelated to the one it names.
    tags = iter("AB")

    class _Client:
        connection = _Conn()

        def __init__(self, *a, **k):
            self._tag = next(tags)

        def select_graph(self, _key):
            return _Graph(self._tag)

    async def _run():
        db = await aiosqlite.connect(":memory:")
        try:
            await db.execute(
                "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, "
                "link_type TEXT, strength REAL)"
            )
            await db.execute(
                "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
            )
            await db.commit()
            store = FalkorGraphStore(
                socket_path=str(tmp_path / "s.sock"), graph_key="test_lock_key"
            )
            await store.project(db)
        finally:
            await db.close()

    falkor_mod._PROJECT_LOCKS.pop("test_lock_key", None)
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _Client),
    ):
        await _asyncio.gather(_run(), _run())

    assert order in (
        ["A:start", "A:end", "B:start", "B:end"],
        ["B:start", "B:end", "A:start", "A:end"],
    ), f"projections of one key must not interleave; saw {order}"


async def test_an_empty_traversal_spends_one_deadline_not_two(tmp_path, monkeypatch):
    """The budget belongs to the TRAVERSAL, not to each query inside it.

    An empty result makes a SECOND awaited query — the projection-marker check —
    and giving that its own full `_READ_TIMEOUT_S` let a slow engine spend twice
    the ceiling reaching an empty answer. That is the worst path to double: the
    empty one is also the one that then pays for a NetworkX rebuild.

    Discriminating, which the sibling hung-engine test is not: that one asserts a
    5s stub finishes under 4s, which is true whether the budget is spent once or
    twice. Here the engine ANSWERS, slowly, so one budget and two budgets give
    different outcomes — with one, the marker check inherits the remainder and
    times out; with two it gets a fresh ceiling and succeeds.
    """
    import asyncio as _asyncio

    monkeypatch.setattr(falkor_mod, "_READ_TIMEOUT_S", 0.2)

    class _Result:
        def __init__(self, rows):
            self.result_set = rows

    class _Graph:
        async def query(self, cypher, _params=None):
            await _asyncio.sleep(0.15)  # under one deadline, over the remainder
            if "ProjectionMeta" in cypher:
                return _Result([[1]])  # a marker exists — so any raise is the deadline
            return _Result([])  # empty traversal, which is what triggers the check

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
        started = _asyncio.get_running_loop().time()
        with pytest.raises(GraphUnavailableError):
            await store.traverse(None, "root", max_depth=2, min_strength=0.3)
        elapsed = _asyncio.get_running_loop().time() - started

    assert elapsed < 0.28, (
        "the whole traversal must fit in ONE deadline; a second independent one "
        f"would let it run to ~0.30s+ (took {elapsed:.3f}s)"
    )


async def test_a_second_process_is_refused_the_publish_claim(tmp_path):
    """The in-process lock cannot see another process; the engine can.

    Two projector PROCESSES get DIFFERENT (pid-suffixed) staging keys, so they
    never corrupt each other's build — and then both `RENAME` onto the canonical
    key. Publication order is then decided by who finishes LAST rather than who
    read newer data, so a run that read an older snapshot and built slowly
    overwrites a newer projection and walks the live graph backwards.

    Refusal must be LOUD rather than a silent no-op: a projector that quietly
    did nothing would look identical to one that succeeded.
    """
    import aiosqlite

    class _Conn:
        async def set(self, *a, **k):
            return None  # SET NX finds the key already held

        async def delete(self, *_a):
            return 1

        async def eval(self, *a, **k):  # pragma: no cover - not reached
            return 0

    class _Graph:
        async def query(self, *a, **k):
            # Reached only if the build started, which is the failure this test
            # is about. `select_graph` itself must stay harmless: `_connection()`
            # goes through it to reach the raw connection, so raising THERE would
            # fire during the claim and mask the thing being asserted.
            raise AssertionError("the build must not start without the publish claim")

    class _Client:
        connection = _Conn()

        def __init__(self, *a, **k):
            pass

        def select_graph(self, _key):
            return _Graph()

    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, "
            "link_type TEXT, strength REAL)"
        )
        await db.execute(
            "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
        )
        await db.commit()

        store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"), graph_key="test_claim")
        falkor_mod._PROJECT_LOCKS.pop("test_claim", None)
        with (
            patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
            patch.object(falkor_mod, "_FalkorDB", _Client),
            pytest.raises(GraphUnavailableError, match="already in progress"),
        ):
            await store.project(db)
    finally:
        await db.close()


async def test_a_hanging_engine_is_bounded_by_the_read_deadline(tmp_path):
    """Connect is inside the deadline, not outside it.

    The bound used to cover `graph.query` alone, so the client CONSTRUCTION — a
    blocking `Is_Cluster` round-trip — could hang forever with no ceiling at all,
    and a recall would wait on it past any budget.
    """
    import asyncio as _asyncio

    class _Client:
        def __init__(self, *a, **k):
            import time as _time

            _time.sleep(5)  # blocking, exactly like the real constructor

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    with (
        patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
        patch.object(falkor_mod, "_FalkorDB", _Client),
        patch.object(falkor_mod, "_READ_TIMEOUT_S", 0.05),
    ):
        started = _asyncio.get_running_loop().time()
        with pytest.raises(GraphUnavailableError, match="exceeded"):
            await store.traverse(None, "root", max_depth=2, min_strength=0.3)
        elapsed = _asyncio.get_running_loop().time() - started

    assert elapsed < 4.0, (
        f"the connect must be inside the deadline, not outside it (took {elapsed:.2f}s)"
    )


async def test_a_timed_out_connect_does_not_burn_a_thread_per_attempt(tmp_path):
    """The deadline must cancel the WAIT, not the CONSTRUCTION.

    MEASURED 2026-09-08: cancelling an `asyncio.wait_for` does NOT stop the
    `to_thread` worker underneath it. So bounding the connect naively trades the
    old "one thread held forever" for a NEW thread on every attempt — which
    exhausts the default executor under a wedged engine and then stalls every
    other `to_thread` in the process. This is the regression the bound would
    otherwise have introduced, so it is pinned rather than trusted.
    """
    import threading

    attempts = 0
    release = threading.Event()

    class _SlowClient:
        def __init__(self, *a, **k):
            nonlocal attempts
            attempts += 1
            release.wait(timeout=5)

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    try:
        with (
            patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
            patch.object(falkor_mod, "_FalkorDB", _SlowClient),
            patch.object(falkor_mod, "_READ_TIMEOUT_S", 0.05),
        ):
            for _ in range(4):
                with pytest.raises(GraphUnavailableError):
                    await store.traverse(None, "root", max_depth=2, min_strength=0.3)
    finally:
        release.set()

    assert attempts == 1, (
        "four timed-out traversals must join ONE construction, not start four; "
        f"the constructor ran {attempts} times"
    )


async def test_an_abandoned_connect_cannot_block_process_exit(tmp_path):
    """A connect we gave up on must not keep the process alive.

    `asyncio.to_thread` puts its worker in the loop's DEFAULT executor, and
    `asyncio.run` JOINS that executor during teardown. So bounding the connect
    stopped a wedged engine from blocking the TRAVERSAL and started it blocking
    process EXIT instead: `python -m genesis.memory.graphstore_project` would
    print its error and then hang forever with nothing left to do.

    A daemon thread is not joined at interpreter exit, so the cost of an
    abandoned connect is a parked thread until the process ends, rather than a
    process that cannot end.
    """
    import threading as _threading

    started = _threading.Event()
    release = _threading.Event()
    seen: dict = {}

    class _SlowClient:
        def __init__(self, *a, **k):
            seen["thread"] = _threading.current_thread()
            started.set()
            release.wait(timeout=5)

    store = FalkorGraphStore(socket_path=str(tmp_path / "s.sock"))
    try:
        with (
            patch.object(falkor_mod, "_FALKOR_AVAILABLE", True),
            patch.object(falkor_mod, "_FalkorDB", _SlowClient),
            patch.object(falkor_mod, "_READ_TIMEOUT_S", 0.05),
            pytest.raises(GraphUnavailableError),
        ):
            await store.traverse(None, "root", max_depth=2, min_strength=0.3)

        assert started.wait(timeout=2), "the constructor must actually have run"
        assert seen["thread"].daemon, (
            "the connect thread must be a daemon — a non-daemon one is joined at "
            "interpreter exit and turns a wedged engine into a process that cannot quit"
        )
    finally:
        release.set()


async def test_every_network_operation_goes_through_the_bounded_chokepoint():
    """Guard-the-guard: no call site may reach the socket unbounded.

    This is the actual finding rather than a restatement of it. An earlier
    version bounded `graph.query` and left the client construction and the raw
    `delete`/`rename` key operations with no deadline — three of five network
    paths unbounded, while the reviewer named two. A behavioural test can only
    cover the paths someone thought to exercise; this fails when a NEW one is
    added, which is the case that actually recurs.
    """
    import ast

    source = Path(falkor_mod.__file__).read_text()
    tree = ast.parse(source)

    # Attribute every call to its TOP-LEVEL enclosing method, not to whatever
    # closure it sits in: both helpers do their work inside a nested `_run`, so
    # walking every FunctionDef would credit the calls to `_run` and the guard
    # would pass while proving nothing about which method owns them.
    methods = [
        node
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    ]
    assert methods, "no methods found — the guard is parsing the wrong thing"

    wait_for_owners = set()
    key_op_owners = set()
    for node in methods:
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call) or not isinstance(inner.func, ast.Attribute):
                continue
            if inner.func.attr == "wait_for":
                wait_for_owners.add(node.name)
            if inner.func.attr == "_connection":
                key_op_owners.add(node.name)

    assert wait_for_owners == {"_bounded"}, (
        f"asyncio.wait_for must live only in _bounded; found in {sorted(wait_for_owners)}"
    )
    assert key_op_owners == {"_key_op"}, (
        "the raw connection must be reached only through _key_op, which bounds it; "
        f"found in {sorted(key_op_owners)}"
    )

    # The two assertions above locate the deadline; they do NOT prove anything
    # reaches it. Mutation-tested: deleting the `_bounded` call from `_query` —
    # the exact regression this guard exists to prevent — left both of them
    # green, because `wait_for` was still in `_bounded` and nothing else touched
    # `_connection`. A guard that passes its own motivating regression is worse
    # than none, so the delegation is asserted too.
    delegators = {
        node.name
        for node in methods
        for inner in ast.walk(node)
        if isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Attribute)
        and inner.func.attr == "_bounded"
    }
    assert delegators == {"_query", "_key_op"}, (
        "_query and _key_op must each route through _bounded — a wrapper that stops "
        f"calling it reaches the socket unbounded; found {sorted(delegators)}"
    )

    # And the accessor can be sidestepped entirely by touching the attribute:
    # `self._db.connection.delete(...)` never calls `_connection()`.
    raw = sorted(
        node.name
        for node in methods
        for inner in ast.walk(node)
        if isinstance(inner, ast.Attribute)
        and inner.attr == "connection"
        and node.name != "_connection"
    )
    assert not raw, f"self._db.connection must go through _connection(); touched by {raw}"


async def test_the_projector_escapes_a_uri_significant_database_path(monkeypatch, tmp_path):
    """A `?` or `#` in the path must not be read as a query string or fragment.

    Interpolating the raw path into `file:...?mode=ro` lets SQLite parse
    everything after the first `?` as URI syntax, so a database at
    `.../memory?copy.db` silently opens `.../memory` — a DIFFERENT file, with no
    error. Operator-controlled rather than attacker-controlled, so this is a
    correctness bug and not a security one, but a silent wrong-file read is the
    worst shape a correctness bug can take.
    """
    from genesis.memory import graphstore_project

    weird = tmp_path / "memory?copy.db"
    monkeypatch.setattr(graphstore_project, "genesis_db_path", lambda: weird)

    seen: dict = {}

    async def _fake_connect(dsn, **kwargs):
        seen["uri"] = dsn
        raise RuntimeError("stop here — the URI is the assertion")

    import aiosqlite

    monkeypatch.setattr(aiosqlite, "connect", _fake_connect)

    with pytest.raises(RuntimeError, match="stop here"):
        await graphstore_project.build()

    assert seen["uri"].endswith("?mode=ro"), "mode=ro must remain the only query string"
    assert "memory%3Fcopy.db" in seen["uri"], (
        f"the path's `?` must be percent-encoded, got {seen['uri']!r}"
    )


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
async def test_an_unbuilt_projection_is_unavailable_not_neighbourless(tmp_path):
    """THE blocker: an unbuilt projection must raise, never answer [].

    The seam blesses "root absent -> []" as genuinely no-neighbours. An unbuilt
    projection makes EVERY root absent, so without this the store answers [] for
    every memory in the system while staying perfectly reachable — nothing
    raises, nothing falls back, nothing logs, and the health probe is documented
    not to care. The failure would be silent and total.

    UNBUILT now means NO PROJECTION MARKER, which is a different question from
    "holds no nodes" — see the sibling test below for why that distinction is a
    real install and not a hair split. It is also not the same as "the key is
    missing": MEASURED here, a read-only MATCH against a missing key CREATES it,
    so the traversal that reaches this check has already materialised a blank
    graph and key existence can no longer tell the two apart.
    """
    store = FalkorGraphStore(graph_key="test_f2_unbuilt")
    try:
        conn = await store._connection()
        await conn.delete("test_f2_unbuilt")
        with pytest.raises(GraphUnavailableError, match="no projection marker"):
            await store.traverse(None, "anything", max_depth=2, min_strength=0.3)
        # UNLATCHED, and this is the assertion that matters. An earlier version
        # cached "the projection exists" for the process lifetime; because the
        # engine holds no persistence, a restart then emptied the graph while the
        # cached answer said otherwise, and every traversal returned [] silently
        # until the SERVER restarted. Raising only the first time is the bug.
        with pytest.raises(GraphUnavailableError, match="no projection marker"):
            await store.traverse(None, "anything", max_depth=2, min_strength=0.3)
    finally:
        conn = await store._connection()
        await conn.delete("test_f2_unbuilt")


@engine_gated
async def test_a_built_but_empty_projection_answers_rather_than_raising(tmp_path):
    """The other side of the same contract, and the one that was wrong.

    Node count cannot tell "never built" from "built, and the graph really is
    empty". On a fresh or fully pruned install `memory_links` legitimately holds
    zero rows, so a correct projection of an empty graph counted zero and
    reported itself unavailable — the facade then fell back and logged a warning
    on every graph-enriched recall, about a backend that was representing the
    empty graph exactly right. It also contradicted the seam's own contract,
    which blesses an empty graph as an answer.

    Driven through the REAL `project()` against a link-free database, not by
    hand-building the end state — the point is that the projector produces a
    graph this check accepts, and a hand-made fixture could agree with the check
    while the projector disagreed with both.
    """
    import aiosqlite

    store = FalkorGraphStore(graph_key="test_f2_built_empty")
    db = await aiosqlite.connect(str(tmp_path / "empty.db"))
    try:
        await db.execute(
            "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, "
            "link_type TEXT, strength REAL)"
        )
        await db.execute(
            "CREATE TABLE memory_metadata (memory_id TEXT, invalid_at TEXT, deprecated INTEGER)"
        )
        await db.commit()

        conn = await store._connection()
        await conn.delete("test_f2_built_empty")

        stats = await store.project(db)
        assert stats == {"nodes": 0, "edges": 0, "hidden": 0}, (
            "a link-free database must project to an empty graph, not fail"
        )
        assert await store.traverse(None, "anything", max_depth=2, min_strength=0.3) == [], (
            "a built-but-empty projection answers [] — it is not unavailable"
        )
    finally:
        conn = await store._connection()
        await conn.delete("test_f2_built_empty")
        await db.close()
