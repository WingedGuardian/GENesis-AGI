"""Phase-1 reconcile repair lane — run_reconcile behavior tests.

Adapted from the d0008 migration's test shapes (aged-vs-recent, idempotence,
delete-failure retry, stale-queue-row reset, no-content honesty, empty-install
no-op) plus the lane-specific rails: truncation asymmetry (ghosts proceed,
mirrors skipped), the per-run cap, dependency-outage skip, and the
concurrent-delete re-read guard.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.memory import integrity_repair
from genesis.qdrant import collections as qdrant_ops

from .conftest import FakeQdrantClient, build_db

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
_OLD = (_NOW - timedelta(days=30)).isoformat()  # far older than the 1h floor
_RECENT = (_NOW - timedelta(minutes=5)).isoformat()  # inside the floor — spared


def _fake_delete(client, *, fail_ids: set[str] | None = None):
    """delete_point stand-in operating on FakeQdrantClient's point store."""
    fail_ids = fail_ids or set()

    def _delete(c, *, collection: str, point_id: str) -> None:
        if point_id in fail_ids:
            raise ConnectionError("fake qdrant delete failure")
        client.points.get(collection, {}).pop(point_id, None)

    return _delete


async def _seed(
    path: str,
    memory_id: str,
    *,
    created_at: str,
    status: str = "embedded",
    collection: str = "episodic_memory",
    confidence: float | None = 0.9,
    content: str | None = "restore me",
    tags: str = "life_domain:work project_type:genesis",
) -> None:
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, created_at, collection, embedding_status, confidence) "
        "VALUES (?, ?, ?, ?, ?)",
        (memory_id, created_at, collection, status, confidence),
    )
    if content is not None:
        await conn.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES (?, ?, 'test', ?, ?)",
            (memory_id, content, tags, collection),
        )
    await conn.commit()
    await conn.close()


async def _run(path, client, monkeypatch, tmp_path, **kw):
    monkeypatch.setattr(
        qdrant_ops,
        "delete_point",
        _fake_delete(client, **{k: kw.pop(k) for k in ("fail_ids",) if k in kw}),
    )
    conn = await aiosqlite.connect(path)
    conn.row_factory = None
    try:
        return await integrity_repair.run_reconcile(
            db=conn,
            qdrant_client=client,
            db_path=path,
            export_dir=tmp_path / "export",
            now=_NOW,
            **kw,
        )
    finally:
        await conn.close()


async def _db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    await build_db(path)
    return path


def _points(client_points: dict[str, dict[str, dict]]) -> FakeQdrantClient:
    base = {"episodic_memory": {}, "knowledge_base": {}}
    base.update(client_points)
    return FakeQdrantClient(base)


async def _fetch(path: str, sql: str, *params):
    conn = await aiosqlite.connect(path)
    try:
        cursor = await conn.execute(sql, params)
        return await cursor.fetchall()
    finally:
        await conn.close()


# ── the core repair pass ─────────────────────────────────────────────────


async def test_repairs_aged_offenders_spares_recent(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    # healthy pair — untouched
    await _seed(path, "ok", created_at=_OLD)
    # aged mirror (embedded, no point) — requeued with confidence + comma tags
    await _seed(path, "mir_old", created_at=_OLD)
    # recent mirror — spared (in-flight window)
    await _seed(path, "mir_new", created_at=_RECENT)
    client = _points(
        {
            "episodic_memory": {
                "ok": {"created_at": _OLD},
                "ghost_old": {"created_at": _OLD},  # aged ghost → deleted
                "ghost_new": {"created_at": _RECENT},  # recent ghost → spared
            }
        }
    )

    result = await _run(path, client, monkeypatch, tmp_path)

    assert result.status == "ok"
    assert result.ghosts_deleted == 1
    assert result.ghost_delete_failed == 0
    assert result.mirrors_requeued == 1
    assert not result.truncated and not result.capped

    # ghost_old removed; ghost_new + ok survive in Qdrant.
    assert "ghost_old" not in client.points["episodic_memory"]
    assert "ghost_new" in client.points["episodic_memory"]
    assert "ok" in client.points["episodic_memory"]

    # export written before deletion, one line, payload preserved.
    exports = list((tmp_path / "export").glob("memory_reconcile_ghost_export-*.jsonl"))
    assert len(exports) == 1
    lines = [json.loads(ln) for ln in exports[0].read_text().splitlines()]
    assert [e["point_id"] for e in lines] == ["ghost_old"]
    assert lines[0]["payload"]["created_at"] == _OLD

    # mir_old requeued: metadata → pending; queue row carries confidence 0.9 and
    # space→comma translated tags. mir_new untouched; ok untouched.
    rows = await _fetch(
        path, "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mir_old'"
    )
    assert rows[0][0] == "pending"
    pend = await _fetch(
        path,
        "SELECT content, tags, confidence, status, source FROM pending_embeddings "
        "WHERE memory_id = 'mir_old'",
    )
    assert pend == [
        ("restore me", "life_domain:work,project_type:genesis", 0.9, "pending", "memory_reconcile")
    ]
    for untouched in ("mir_new", "ok"):
        rows = await _fetch(
            path, "SELECT embedding_status FROM memory_metadata WHERE memory_id = ?", untouched
        )
        assert rows[0][0] == "embedded"
        assert (
            await _fetch(
                path, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = ?", untouched
            )
        )[0][0] == 0


async def test_ghost_sweep_deletes_stray_rows(tmp_path, monkeypatch):
    """A ghost (Qdrant point, no metadata row) may still have stray SQLite rows
    keyed by its id in the satellite tables. After the point delete, the sweep
    must remove them — otherwise deleting the point leaves keyword/link/queue
    debris. (d0008 parity; the sweep SQL is otherwise untested.)"""
    path = await _db(tmp_path)
    # Stray rows for a ghost id 'g' — NO memory_metadata row (that's what makes
    # it a ghost). `async with` so a seeding failure can't strand the aiosqlite
    # worker thread and hang interpreter exit.
    async with aiosqlite.connect(path) as conn:
        await conn.execute(
            "INSERT INTO memory_fts (memory_id, content, source_type, tags, collection) "
            "VALUES ('g', 'stray', 'test', '', 'episodic_memory')"
        )
        await conn.execute(
            "INSERT INTO memory_links (source_id, target_id, link_type, strength, created_at) "
            "VALUES ('g', 'other', 'related_to', 1.0, ?)",
            (_OLD,),
        )
        await conn.execute(
            "INSERT INTO pending_embeddings (id, memory_id, content, memory_type, collection, "
            "created_at, status) VALUES ('pe-g', 'g', 'x', 'episodic', 'episodic_memory', ?, 'failed')",
            (_OLD,),
        )
        await conn.execute(
            "INSERT INTO entity_mentions (memory_id, entity_id, provenance, confidence, source, created_at) "
            "VALUES ('g', 'ent1', 'EXTRACTED', 1.0, 'test', ?)",
            (_OLD,),
        )
        await conn.commit()

    client = _points({"episodic_memory": {"g": {"created_at": _OLD}}})
    result = await _run(path, client, monkeypatch, tmp_path)
    assert result.ghosts_deleted == 1

    for table, col in (
        ("memory_fts", "memory_id"),
        ("pending_embeddings", "memory_id"),
        ("entity_mentions", "memory_id"),
    ):
        rows = await _fetch(path, f"SELECT COUNT(*) FROM {table} WHERE {col} = 'g'")
        assert rows[0][0] == 0, f"{table} stray row not swept"
    links = await _fetch(
        path, "SELECT COUNT(*) FROM memory_links WHERE source_id = 'g' OR target_id = 'g'"
    )
    assert links[0][0] == 0


async def test_second_run_is_noop(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    await _seed(path, "mir_old", created_at=_OLD)
    client = _points({"episodic_memory": {"ghost_old": {"created_at": _OLD}}})

    first = await _run(path, client, monkeypatch, tmp_path)
    assert first.ghosts_deleted == 1 and first.mirrors_requeued == 1

    second = await _run(path, client, monkeypatch, tmp_path)
    assert second.status == "ok"
    assert second.ghosts_deleted == 0
    assert second.mirrors_requeued == 0
    # no duplicate queue row on the second pass
    assert (
        await _fetch(path, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'mir_old'")
    )[0][0] == 1


async def test_empty_install_is_noop(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    result = await _run(path, _points({}), monkeypatch, tmp_path)
    assert result.status == "ok"
    assert result.ghosts_deleted == 0 and result.mirrors_requeued == 0
    assert not list((tmp_path / "export").glob("*")) if (tmp_path / "export").exists() else True


# ── failure / safety rails ───────────────────────────────────────────────


async def test_ghost_delete_failure_left_for_retry(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    client = _points({"episodic_memory": {"ghost_bad": {"created_at": _OLD}}})
    result = await _run(path, client, monkeypatch, tmp_path, fail_ids={"ghost_bad"})
    assert result.status == "partial"
    assert result.ghosts_deleted == 0 and result.ghost_delete_failed == 1
    assert "ghost_bad" in client.points["episodic_memory"]  # still there → next run


async def test_ghost_export_failure_skips_deletion(tmp_path, monkeypatch):
    """The export is a PRECONDITION for deletion: if retrieve() fails so the
    payload can't be captured, the ghost's point must NOT be deleted (its content
    would be lost with no recovery net) — it is left for the next run. A ghost
    whose export succeeds is deleted normally."""
    path = await _db(tmp_path)
    client = _points(
        {"episodic_memory": {"g_ok": {"created_at": _OLD}, "g_bad": {"created_at": _OLD}}}
    )
    real_retrieve = client.retrieve

    def _flaky_retrieve(*, collection_name, ids, **kw):
        if "g_bad" in ids:
            raise ConnectionError("retrieve boom")
        return real_retrieve(collection_name=collection_name, ids=ids, **kw)

    client.retrieve = _flaky_retrieve

    result = await _run(path, client, monkeypatch, tmp_path)

    assert result.ghosts_deleted == 1  # only g_ok
    assert result.status == "partial"
    assert result.details.get("ghost_export_skipped") == 1
    assert "g_bad" in client.points["episodic_memory"]  # NOT deleted → retried
    assert "g_ok" not in client.points["episodic_memory"]
    # export file contains only the successfully-captured ghost
    exports = list((tmp_path / "export").glob("*.jsonl"))
    lines = [json.loads(ln) for ln in exports[0].read_text().splitlines()]
    assert [e["point_id"] for e in lines] == ["g_ok"]


async def test_ghost_absent_at_export_time_is_deferred(tmp_path, monkeypatch):
    """Codex R3: the EMPTY-retrieve path (point vanished between enumeration and
    export) must ALSO be treated as export-failure — export captures only a null,
    so deleting/sweeping could still destroy stray content that IS present. Leave
    it for the next run rather than authorizing the delete."""
    path = await _db(tmp_path)
    client = _points({"episodic_memory": {"g_gone": {"created_at": _OLD}}})

    def _empty_retrieve(*, collection_name, ids, **kw):
        return []  # vanished between scroll and export

    client.retrieve = _empty_retrieve

    result = await _run(path, client, monkeypatch, tmp_path)

    assert result.ghosts_deleted == 0
    assert result.status == "partial"
    assert result.details.get("ghost_export_skipped") == 1
    # export file, if written at all, contains no record for the vanished ghost
    exports = list((tmp_path / "export").glob("*.jsonl"))
    if exports:
        lines = [json.loads(ln) for ln in exports[0].read_text().splitlines()]
        assert all(e["point_id"] != "g_gone" for e in lines)


async def test_ghost_delete_and_sweep_run_under_per_id_lock(tmp_path, monkeypatch):
    """The ghost point-delete + stray-row sweep hold memory_id_lock(pid), so the
    recovery worker (which may hold a 'pending' queue row for this id and takes
    the same lock) can't re-upsert the point between the delete and the sweep."""
    from genesis.memory._locks import memory_id_lock

    path = await _db(tmp_path)
    client = _points({"episodic_memory": {"g_lk": {"created_at": _OLD}}})
    held: dict[str, bool] = {}

    def _spy_delete(c, *, collection, point_id):
        held["at_delete"] = memory_id_lock(point_id).locked()
        client.points.get(collection, {}).pop(point_id, None)

    monkeypatch.setattr(qdrant_ops, "delete_point", _spy_delete)
    conn = await aiosqlite.connect(path)
    conn.row_factory = None
    try:
        result = await integrity_repair.run_reconcile(
            db=conn,
            qdrant_client=client,
            db_path=path,
            export_dir=tmp_path / "export",
            now=_NOW,
        )
    finally:
        await conn.close()

    assert result.ghosts_deleted == 1
    assert held.get("at_delete") is True  # delete ran inside the id-lock
    assert memory_id_lock("g_lk").locked() is False  # released after


async def test_qdrant_down_skips_run_touching_nothing(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    await _seed(path, "mir_old", created_at=_OLD)
    client = FakeQdrantClient(raise_on="scroll")
    result = await _run(path, client, monkeypatch, tmp_path)
    assert result.status == "skipped"
    assert result.unknown_reason and "qdrant_unavailable" in result.unknown_reason
    # nothing touched — mirror still 'embedded', no queue row.
    rows = await _fetch(
        path, "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mir_old'"
    )
    assert rows[0][0] == "embedded"


async def test_point_without_created_at_never_touched(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    client = _points({"episodic_memory": {"ageless": {}}})  # no created_at → can't age
    result = await _run(path, client, monkeypatch, tmp_path)
    assert result.ghosts_deleted == 0
    assert "ageless" in client.points["episodic_memory"]


async def test_truncation_skips_mirrors_but_repairs_ghosts(tmp_path, monkeypatch):
    """Truncation asymmetry: a partial point set keeps ghost classification
    sound (metadata read is complete) but cannot prove vector ABSENCE — mirror
    repair must be skipped, mirroring the checker's lying_mirror=-1 sentinel."""
    path = await _db(tmp_path)
    await _seed(path, "mir_old", created_at=_OLD)  # true mirror — must be SKIPPED
    # Two aged ghosts; max_points=1 truncates the scroll after the first.
    client = _points(
        {
            "episodic_memory": {
                "ghost_a": {"created_at": _OLD},
                "ghost_b": {"created_at": _OLD},
            }
        }
    )
    result = await _run(path, client, monkeypatch, tmp_path, max_points=1)
    assert result.truncated is True
    assert result.status == "partial"
    assert result.mirrors_requeued == 0  # skipped under truncation
    assert result.details.get("mirrors_skipped_truncated") is True
    assert result.ghosts_deleted >= 1  # scanned ghosts still repaired
    rows = await _fetch(
        path, "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mir_old'"
    )
    assert rows[0][0] == "embedded"  # untouched


async def test_cap_bounds_work_and_flags(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    for i in range(3):
        await _seed(path, f"mir{i}", created_at=_OLD)
    client = _points({"episodic_memory": {f"ghost{i}": {"created_at": _OLD} for i in range(3)}})
    result = await _run(path, client, monkeypatch, tmp_path, max_repairs_per_run=4)
    assert result.capped is True
    assert result.status == "partial"
    # ghosts first (3), then one mirror within the budget of 4.
    assert result.ghosts_deleted == 3
    assert result.mirrors_requeued == 1


# ── mirror-repair edge semantics ─────────────────────────────────────────


async def test_stale_queue_row_reset_to_drainable_pending(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    await _seed(path, "m_stale", created_at=_OLD, content="body", tags="wing:memory")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO pending_embeddings (id, memory_id, content, memory_type, collection, "
        "created_at, status, error_message) "
        "VALUES ('old-id', 'm_stale', 'stale', 'episodic', 'episodic_memory', ?, 'failed', 'boom')",
        (_OLD,),
    )
    await conn.commit()
    await conn.close()

    result = await _run(path, _points({}), monkeypatch, tmp_path)
    assert result.mirrors_requeued == 1
    rows = await _fetch(
        path,
        "SELECT status, error_message, content, tags FROM pending_embeddings "
        "WHERE memory_id = 'm_stale'",
    )
    assert rows == [("pending", None, "body", "wing:memory")]  # reset + refreshed, no dup


async def test_mirror_without_content_marked_failed(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    await _seed(path, "m_nc", created_at=_OLD, content=None)
    result = await _run(path, _points({}), monkeypatch, tmp_path)
    assert result.mirrors_requeued == 0
    assert result.mirrors_skipped_no_content == 1
    rows = await _fetch(
        path, "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'm_nc'"
    )
    assert rows[0][0] == "failed"  # honest vectorless state
    assert (await _fetch(path, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_nc'"))[
        0
    ][0] == 0


async def test_concurrent_delete_between_enumeration_and_repair_skips(tmp_path, monkeypatch):
    """The live re-read guard: a memory deleted after enumeration must be
    skipped — never re-queued (which would resurrect it as a ghost)."""
    path = await _db(tmp_path)
    stale_path = str(tmp_path / "stale.db")
    await build_db(stale_path)
    await _seed(stale_path, "m_race", created_at=_OLD)  # enumeration sees the mirror
    # live db: the row is already gone (concurrent MemoryStore.delete finished)

    real_open = integrity_repair.open_ro_connection

    async def stale_open(p=None):
        return await real_open(stale_path)

    monkeypatch.setattr(integrity_repair, "open_ro_connection", stale_open)
    result = await _run(path, _points({}), monkeypatch, tmp_path)
    assert result.mirrors_requeued == 0
    assert (
        await _fetch(path, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_race'")
    )[0][0] == 0


async def test_knowledge_collection_mirror_requeues_as_knowledge(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    await _seed(path, "kb_mir", created_at=_OLD, collection="knowledge_base")
    result = await _run(path, _points({}), monkeypatch, tmp_path)
    assert result.mirrors_requeued == 1
    rows = await _fetch(
        path,
        "SELECT memory_type, collection FROM pending_embeddings WHERE memory_id = 'kb_mir'",
    )
    assert rows == [("knowledge", "knowledge_base")]


# ── Tombstone drain (PR-2: cross-process deferred deletes) ──────────────────


async def _run_rowfactory(path, client, monkeypatch, tmp_path, **kw):
    """Like _run, but with the production Row factory — the tombstone drain
    reads deferred_work_queue rows as dicts (crud.query_pending), which needs
    aiosqlite.Row exactly as the runtime's shared connection provides."""
    monkeypatch.setattr(
        qdrant_ops,
        "delete_point",
        _fake_delete(client, **{k: kw.pop(k) for k in ("fail_ids",) if k in kw}),
    )
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        return await integrity_repair.run_reconcile(
            db=conn,
            qdrant_client=client,
            db_path=path,
            export_dir=tmp_path / "export",
            now=_NOW,
            **kw,
        )
    finally:
        await conn.close()


async def _seed_tombstone(path: str, memory_id: str) -> None:
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO deferred_work_queue (id, work_type, priority, payload_json, "
        "deferred_at, deferred_reason, staleness_policy, status, created_at) "
        "VALUES (?, 'memory_deferred_delete', 50, ?, ?, 'test', 'drain', 'pending', ?)",
        (
            f"tomb-{memory_id}",
            json.dumps({"topic": memory_id, "category": "memory_delete",
                        "signal_type": None, "memory_id": memory_id}),
            _OLD,
            _OLD,
        ),
    )
    await conn.commit()
    await conn.close()


async def test_tombstone_drain_deletes_and_completes(tmp_path, monkeypatch):
    """An open tombstone's delete is re-attempted via delete_memory; success
    marks the row completed and counts tombstones_drained — and the id is
    EXCLUDED from ghost classification even though its point is still
    enumerable this run."""
    path = await _db(tmp_path)
    await _seed_tombstone(path, "m_tomb")
    client = _points({"episodic_memory": {"m_tomb": {"created_at": _OLD, "content": "x"}}})

    deleted_ids: list[str] = []

    async def fake_delete_memory(mid: str) -> dict:
        deleted_ids.append(mid)
        client.points["episodic_memory"].pop(mid, None)
        return {"qdrant_deleted": 1, "metadata": True}

    result = await _run_rowfactory(
        path, client, monkeypatch, tmp_path, delete_memory=fake_delete_memory
    )
    assert deleted_ids == ["m_tomb"]
    assert result.tombstones_drained == 1
    assert result.status == "ok"
    assert result.ghosts_deleted == 0  # excluded from ghost set, not double-processed
    rows = await _fetch(
        path, "SELECT status FROM deferred_work_queue WHERE id = 'tomb-m_tomb'"
    )
    assert rows == [("completed",)]


async def test_tombstone_drain_failure_stays_pending(tmp_path, monkeypatch):
    """A drain attempt that defers again (Qdrant still down) goes back to
    pending for the next run; the run reports partial with the count."""
    path = await _db(tmp_path)
    await _seed_tombstone(path, "m_still")

    async def fake_delete_memory(mid: str) -> dict:
        return {"deferred": True}

    result = await _run_rowfactory(
        path, _points({}), monkeypatch, tmp_path, delete_memory=fake_delete_memory
    )
    assert result.tombstones_drained == 0
    assert result.status == "partial"
    assert result.details.get("tombstones_failed") == 1
    rows = await _fetch(
        path, "SELECT status, attempts FROM deferred_work_queue WHERE id = 'tomb-m_still'"
    )
    assert rows == [("pending", 1)]


async def test_tombstone_without_deleter_skipped_and_excluded(tmp_path, monkeypatch):
    """No delete callable (degraded boot): the drain is skipped loudly, the run
    is partial, and the tombstoned id is still excluded from mirror requeue —
    an intent-marked memory must never be re-embedded."""
    path = await _db(tmp_path)
    await _seed(path, "m_nodel", created_at=_OLD)  # would otherwise be a mirror
    await _seed_tombstone(path, "m_nodel")

    result = await _run_rowfactory(path, _points({}), monkeypatch, tmp_path)
    assert result.status == "partial"
    assert result.details.get("tombstones_skipped_no_deleter") == 1
    assert result.mirrors_requeued == 0
    rows = await _fetch(
        path, "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_nodel'"
    )
    assert rows[0][0] == 0


async def test_corrupt_tombstone_discarded(tmp_path, monkeypatch):
    path = await _db(tmp_path)
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO deferred_work_queue (id, work_type, priority, payload_json, "
        "deferred_at, deferred_reason, staleness_policy, status, created_at) "
        "VALUES ('tomb-bad', 'memory_deferred_delete', 50, 'not json', ?, 'test', "
        "'drain', 'pending', ?)",
        (_OLD, _OLD),
    )
    await conn.commit()
    await conn.close()

    async def fake_delete_memory(mid: str) -> dict:  # pragma: no cover - not reached
        raise AssertionError("must not be called for a corrupt row")

    result = await _run_rowfactory(
        path, _points({}), monkeypatch, tmp_path, delete_memory=fake_delete_memory
    )
    assert result.tombstones_drained == 0
    rows = await _fetch(
        path, "SELECT status FROM deferred_work_queue WHERE id = 'tomb-bad'"
    )
    assert rows == [("discarded",)]


async def test_duplicate_tombstones_for_one_id_drain_once(tmp_path, monkeypatch):
    """Two open tombstone rows for the SAME memory_id (best-effort multi-process
    dedup can produce dups): the drain attempts the delete exactly once, closes
    BOTH rows, counts one drain — and never re-opens a completed row."""
    path = await _db(tmp_path)
    await _seed_tombstone(path, "m_dup")
    conn = await aiosqlite.connect(path)
    await conn.execute(
        "INSERT INTO deferred_work_queue (id, work_type, priority, payload_json, "
        "deferred_at, deferred_reason, staleness_policy, status, created_at) "
        "VALUES ('tomb-m_dup-2', 'memory_deferred_delete', 50, ?, ?, 'test', "
        "'drain', 'pending', ?)",
        (
            json.dumps({"topic": "m_dup", "category": "memory_delete",
                        "signal_type": None, "memory_id": "m_dup"}),
            _OLD,
            _OLD,
        ),
    )
    await conn.commit()
    await conn.close()

    calls: list[str] = []

    async def fake_delete_memory(mid: str) -> dict:
        calls.append(mid)
        return {"qdrant_deleted": 1, "metadata": True}

    result = await _run_rowfactory(
        path, _points({}), monkeypatch, tmp_path, delete_memory=fake_delete_memory
    )
    assert calls == ["m_dup"]  # exactly one attempt
    assert result.tombstones_drained == 1
    assert result.details.get("tombstones_failed") is None
    rows = await _fetch(
        path,
        "SELECT id, status FROM deferred_work_queue WHERE work_type = 'memory_deferred_delete' "
        "ORDER BY id",
    )
    assert [(r[0], r[1]) for r in rows] == [
        ("tomb-m_dup", "completed"),
        ("tomb-m_dup-2", "completed"),
    ]
