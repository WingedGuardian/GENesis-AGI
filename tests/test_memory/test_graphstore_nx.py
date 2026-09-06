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
