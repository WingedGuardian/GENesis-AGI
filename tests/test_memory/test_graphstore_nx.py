"""NetworkxGraphStore — cross-process staleness, and the cache it protects.

``invalidate()`` flips a flag on ONE store instance in ONE process. A dream
job that writes links in its own process could never mark the MCP server's
cached projection stale, so the server kept serving a graph that predated the
write until it happened to write a link itself. The store therefore also
compares SQLite's ``PRAGMA data_version``, which moves when ANOTHER connection
commits and deliberately does not move for our own writes.

Each property here was probed against WAL + aiosqlite before being designed on
(2026-09-06), including a control that must not flip.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory.graphstore_nx import NetworkxGraphStore

pytestmark = pytest.mark.asyncio

_SCHEMA = """
    CREATE TABLE memory_links (
        source_id   TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        link_type   TEXT NOT NULL,
        strength    REAL NOT NULL DEFAULT 0.5,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (source_id, target_id, link_type)
    )
"""


async def _seed(path, links):
    db = await aiosqlite.connect(str(path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(_SCHEMA)
    await db.execute(
        "CREATE TABLE memory_metadata ("
        " memory_id TEXT PRIMARY KEY, invalid_at TEXT, deprecated INTEGER)"
    )
    for src, tgt in links:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, 'supports', 0.9, '2026-09-06')",
            (src, tgt),
        )
    await db.commit()
    await db.close()


async def _add_link_via_other_connection(path, src, tgt):
    """Simulate the other-process writer: a SEPARATE connection commits."""
    other = await aiosqlite.connect(str(path))
    await other.execute(
        "INSERT INTO memory_links VALUES (?, ?, 'supports', 0.9, '2026-09-06')",
        (src, tgt),
    )
    await other.commit()
    await other.close()


async def test_another_connections_write_is_seen_without_invalidate(tmp_path):
    """THE POINT OF THE TOKEN: a writer in another process never calls our
    invalidate(), and the projection must still refresh."""
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        first = await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert {n.memory_id for n in first} == {"B"}

        await _add_link_via_other_connection(path, "A", "C")
        # NO invalidate() call — that is exactly what the other process cannot do.
        second = await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert {n.memory_id for n in second} == {"B", "C"}, (
            "another connection's committed write was not observed"
        )
    finally:
        await db.close()


async def test_no_external_write_means_no_rebuild(tmp_path):
    """CONTROL — the token must not cause a rebuild on every read. Asserted on
    the cached object's IDENTITY, which a rebuild necessarily replaces."""
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        cached = store._graph
        await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert store._graph is cached, "rebuilt with nothing to rebuild for"
    finally:
        await db.close()


async def test_our_own_write_does_not_alone_force_a_rebuild(tmp_path):
    """data_version deliberately ignores our OWN commits — otherwise every
    link this process writes would force a full reload. Correctness on that
    path comes from invalidate(), which the writer sites call."""
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        cached = store._graph
        await db.execute(
            "INSERT INTO memory_links VALUES ('A','D','supports',0.9,'2026-09-06')"
        )
        await db.commit()
        await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert store._graph is cached
        # ...and invalidate() is what makes it visible.
        store.invalidate()
        after = await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert "D" in {n.memory_id for n in after}
    finally:
        await db.close()


async def test_a_different_connection_rebuilds_conservatively(tmp_path):
    """data_version is per-connection, so a projection built from connection A
    cannot be validated against connection B's counter. Rebuild instead."""
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db1 = await aiosqlite.connect(str(path))
    db2 = await aiosqlite.connect(str(path))
    try:
        await store.traverse(db1, "A", max_depth=1, min_strength=0.0)
        cached = store._graph
        await store.traverse(db2, "A", max_depth=1, min_strength=0.0)
        assert store._graph is not cached, (
            "served a projection validated against a different connection's counter"
        )
    finally:
        await db1.close()
        await db2.close()


async def test_invalidate_is_safe_without_a_database_handle(tmp_path):
    """Every memory_links writer calls this from CRUD/dream paths holding no
    store reference and sometimes no live handle.

    The flag must be OBSERVED TO FLIP: ``_dirty`` is True from construction, so
    asserting it on a fresh store passes even with ``invalidate()`` gutted to
    ``pass``. Build a projection first so the flag is False going in.
    """
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert store._dirty is False, "precondition: a built store is clean"
    finally:
        await db.close()

    store.invalidate()  # no handle, no arguments — the writer sites' shape
    assert store._dirty is True


async def test_networkx_store_satisfies_the_seam():
    """The protocol has no runtime enforcement — CI runs no type checker — so
    conformance is asserted here. PR-2's backend inherits this check, which is
    what keeps a second implementation from re-opening the shield defect by
    returning [] where the contract says raise."""
    store = NetworkxGraphStore()
    for member in ("name", "traverse", "centrality", "invalidate"):
        assert hasattr(store, member), f"GraphStore contract member missing: {member}"
    assert isinstance(store.name, str) and store.name


async def test_a_commit_inside_the_load_window_is_not_lost(tmp_path, monkeypatch):
    """THE BLOCKER, locked: stamping the token AFTER the load pins a stale
    projection forever.

    In WAL the read snapshot is fixed when the SELECT first steps, so a commit
    landing mid-load is absent from the rows but PRESENT in a token read
    afterwards — stamped == live, and _is_stale reports fresh for the rest of
    the process's life. Verified 2026-09-06 on this branch's own code before
    the fix; this test reproduces that window deterministically by committing
    from another connection between the SELECT and the stamp.
    """
    path = tmp_path / "g.db"
    await _seed(path, [("A", "B")])
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))

    real_execute = db.execute
    fired = {"n": 0}

    async def _execute_then_external_commit(sql, *a, **kw):
        cursor = await real_execute(sql, *a, **kw)
        if "FROM memory_links" in str(sql) and not fired["n"]:
            fired["n"] += 1
            # Lands AFTER the snapshot is fixed, BEFORE the stamp would be read.
            await _add_link_via_other_connection(path, "A", "C")
        return cursor

    monkeypatch.setattr(db, "execute", _execute_then_external_commit)
    try:
        first = await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert fired["n"] == 1, "the mid-load commit never fired — test is inert"
        assert {n.memory_id for n in first} == {"B"}  # correctly absent from THIS load

        monkeypatch.setattr(db, "execute", real_execute)
        second = await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        assert "C" in {n.memory_id for n in second}, (
            "a commit inside the load window was lost permanently — the token "
            "was stamped from a newer snapshot than the rows"
        )
    finally:
        await db.close()


# ── visibility parity with normal recall ────────────────────────────────────


async def _seed_with_metadata(path, links, meta):
    """links: [(src, tgt)]; meta: {memory_id: (invalid_at, deprecated)}."""
    db = await aiosqlite.connect(str(path))
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute(_SCHEMA)
    await db.execute(
        """CREATE TABLE memory_metadata (
               memory_id  TEXT PRIMARY KEY,
               invalid_at TEXT,
               deprecated INTEGER
           )"""
    )
    for src, tgt in links:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, 'supports', 0.9, '2026-09-06')",
            (src, tgt),
        )
    for mid, (invalid_at, deprecated) in meta.items():
        await db.execute(
            "INSERT INTO memory_metadata VALUES (?, ?, ?)", (mid, invalid_at, deprecated)
        )
    await db.commit()
    await db.close()


async def test_traverse_hides_what_recall_hides(tmp_path):
    """THE PARITY LOCK — the assertion that did not exist before this change.

    search_ranked hides bitemporally-expired and deprecated memories, and
    graph_expansion repeats that filter citing "visibility parity with normal
    recall". traverse did NEITHER, and its consumer (mcp/memory/core.py) emits
    raw memory_ids into `graph_neighbors` with no hydration and no filter — so
    the model was shown, as live context, memories recall itself deliberately
    suppresses. MEASURED on the live graph before the fix: 11.3% of edges and
    23.8% of top-5 slices involved such a memory.
    """
    path = tmp_path / "g.db"
    await _seed_with_metadata(
        path,
        [("A", "live"), ("A", "expired"), ("A", "deprecated"), ("A", "unstamped")],
        {
            "live": (None, 0),
            "expired": ("2020-01-01T00:00:00+00:00", 0),  # invalid_at in the past
            "deprecated": (None, 1),
            # 'unstamped' deliberately has NO metadata row — the dangling-link
            # class, which this predicate leaves alone by design.
        },
    )
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        reached = {
            n.memory_id
            for n in await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        }
        assert "live" in reached
        assert "expired" not in reached, "a bitemporally-expired memory reached the model"
        assert "deprecated" not in reached, "a deprecated memory reached the model"
        assert "unstamped" in reached, (
            "a memory with no metadata row was dropped — that is the dangling-link "
            "class, not this predicate's business"
        )
    finally:
        await db.close()


async def test_a_future_invalid_at_is_still_visible(tmp_path):
    """CONTROL — the predicate is 'expired', not 'has an invalid_at'. A memory
    whose validity window has not closed yet must still be reachable, or the
    filter is simply deleting every bitemporal row."""
    path = tmp_path / "g.db"
    await _seed_with_metadata(
        path,
        [("A", "future"), ("A", "past")],
        {
            "future": ("2099-01-01T00:00:00+00:00", 0),
            "past": ("2020-01-01T00:00:00+00:00", 0),
        },
    )
    store = NetworkxGraphStore()
    db = await aiosqlite.connect(str(path))
    try:
        reached = {
            n.memory_id
            for n in await store.traverse(db, "A", max_depth=1, min_strength=0.0)
        }
        assert reached == {"future"}, f"expected only the still-valid memory, got {reached}"
    finally:
        await db.close()


async def test_the_cte_fallback_applies_the_same_predicate(tmp_path):
    """The degraded path must not show what the primary path hides.

    Drives the facade with NetworkX forced absent, so traverse routes to the
    recursive CTE — the only path a NetworkX-less install ever takes.
    """
    from unittest.mock import patch

    from genesis.memory import graph as graph_mod
    from genesis.memory import graphstore_nx

    path = tmp_path / "g.db"
    await _seed_with_metadata(
        path,
        [("A", "live"), ("A", "expired"), ("A", "deprecated")],
        {
            "live": (None, 0),
            "expired": ("2020-01-01T00:00:00+00:00", 0),
            "deprecated": (None, 1),
        },
    )
    db = await aiosqlite.connect(str(path))
    try:
        with patch.object(graphstore_nx, "_NX_AVAILABLE", False):
            result = await graph_mod.traverse(db, "A", max_depth=1, min_strength=0.0)
        reached = {n.memory_id for n in result.nodes}
        assert reached == {"live"}, (
            f"the CTE fallback disagreed with the primary path: {reached}"
        )
    finally:
        await db.close()


async def test_both_paths_refuse_to_traverse_from_a_hidden_root(tmp_path):
    """The root is filtered too, on BOTH paths.

    The NX loader drops an edge when EITHER endpoint is hidden, so a hidden
    memory has no edges and traversing from it yields nothing. The CTE anchor
    (`WHERE source_id = ?`) had no such check, so it returned the whole subtree
    — the two paths disagreeing on identical input, which contradicts the
    seam's own "which store answers can never change WHICH memories the model
    is shown" invariant. Reachable: memory_expand's root is caller-supplied,
    and 2,827 live memories are hidden AND have out-edges.
    """
    from unittest.mock import patch

    from genesis.memory import graph as graph_mod
    from genesis.memory import graphstore_nx

    path = tmp_path / "g.db"
    await _seed_with_metadata(
        path,
        [("hidden_root", "child"), ("live_root", "child")],
        {
            "hidden_root": (None, 1),  # deprecated
            "live_root": (None, 0),
            "child": (None, 0),
        },
    )
    db = await aiosqlite.connect(str(path))
    try:
        nx_from_hidden = await NetworkxGraphStore().traverse(
            db, "hidden_root", max_depth=1, min_strength=0.0
        )
        assert nx_from_hidden == [], "NX store traversed from a hidden root"

        with patch.object(graphstore_nx, "_NX_AVAILABLE", False):
            cte_from_hidden = await graph_mod.traverse(
                db, "hidden_root", max_depth=1, min_strength=0.0
            )
        assert cte_from_hidden.nodes == [], (
            "the CTE traversed FROM a hidden root where the NX store returned "
            "nothing — the two paths disagree on identical input"
        )

        # CONTROL: a live root still reaches its child on both paths, so the
        # assertions above are not passing because everything returns empty.
        nx_live = await NetworkxGraphStore().traverse(
            db, "live_root", max_depth=1, min_strength=0.0
        )
        with patch.object(graphstore_nx, "_NX_AVAILABLE", False):
            cte_live = await graph_mod.traverse(
                db, "live_root", max_depth=1, min_strength=0.0
            )
        assert {n.memory_id for n in nx_live} == {"child"}
        assert {n.memory_id for n in cte_live.nodes} == {"child"}
    finally:
        await db.close()
