"""d0008 — reconcile cross-store memory inconsistencies (ghosts + lying mirrors).

Deletes aged ghost points (Qdrant point with no metadata row, payload exported
first) and re-queues aged lying mirrors (embedded metadata row with no point)
for re-embedding, while sparing recent offenders (the mid-write window) and
leaving healthy pairs alone.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import genesis.db.data_migrations.d0008_reconcile_memory_cross_store as d0008

_OLD = "2026-04-20T01:51:42.000000+00:00"  # older than the 1h min-age floor


def _recent() -> str:
    return datetime.now(UTC).isoformat()


_SCHEMA = """
CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, collection TEXT,
    created_at TEXT, embedding_status TEXT, confidence REAL,
    deprecated INTEGER DEFAULT 0);
CREATE TABLE memory_fts (memory_id TEXT, content TEXT, tags TEXT, collection TEXT);
CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT);
CREATE TABLE pending_embeddings (id TEXT, memory_id TEXT, content TEXT,
    memory_type TEXT, tags TEXT, collection TEXT, created_at TEXT, status TEXT,
    source TEXT, confidence REAL, source_session_id TEXT, transcript_path TEXT,
    source_line_range TEXT, extraction_timestamp TEXT, source_pipeline TEXT,
    source_subsystem TEXT, error_message TEXT);
CREATE TABLE entity_mentions (memory_id TEXT, entity_id TEXT);
"""


class _FakePoint:
    def __init__(self, pid: str, created_at: str) -> None:
        self.id = pid
        self.payload = {"created_at": created_at} if created_at else {}


class _FakeClient:
    """Minimal Qdrant stand-in: scroll + retrieve + delete over a point set."""

    def __init__(self, points: dict[str, dict[str, str]], fail_ids: set[str] | None = None) -> None:
        # points: {collection: {point_id: created_at}}
        self._points = {c: dict(ids) for c, ids in points.items()}
        self._fail = fail_ids or set()
        self.deleted: list[tuple[str, str]] = []

    def scroll(self, *, collection_name, limit, offset, with_payload, with_vectors):
        pts = [_FakePoint(pid, ca) for pid, ca in self._points.get(collection_name, {}).items()]
        return pts, None  # single page

    def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        out = []
        for pid in ids:
            ca = self._points.get(collection_name, {}).get(pid)
            if ca is not None:
                out.append(_FakePoint(pid, ca))
        return out

    def delete(self, *, collection: str, point_id: str) -> None:
        if point_id in self._fail:
            raise RuntimeError("qdrant down")
        self._points.get(collection, {}).pop(point_id, None)
        self.deleted.append((collection, point_id))


def _patch(monkeypatch, path, client, export_path) -> None:
    monkeypatch.setattr(d0008, "genesis_db_path", lambda: str(path))
    monkeypatch.setattr(d0008, "get_client", lambda: client)
    monkeypatch.setattr(d0008, "_export_path", lambda: export_path)

    def _fake_delete_point(c, *, collection, point_id):
        c.delete(collection=collection, point_id=point_id)

    monkeypatch.setattr(d0008, "delete_point", _fake_delete_point)


def _seed(path) -> None:
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    now = _recent()
    # healthy pair (metadata + point) — untouched
    db.execute(
        "INSERT INTO memory_metadata (memory_id, collection, created_at, embedding_status) "
        "VALUES ('ok', 'episodic_memory', ?, 'embedded')",
        (_OLD,),
    )
    db.execute("INSERT INTO memory_fts VALUES ('ok', 'ok content', '', 'episodic_memory')")
    # OLD lying mirror (embedded metadata + FTS, no point) — re-queued. Its FTS
    # tags carry facet breadcrumbs that must survive the re-embed (space-separated
    # in FTS → comma-separated in the queue row), and its real confidence (0.9)
    # must be carried into the queue row, NOT reset to the worker's 0.5 default.
    db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, collection, created_at, embedding_status, confidence) "
        "VALUES ('mir_old', 'episodic_memory', ?, 'embedded', 0.9)",
        (_OLD,),
    )
    db.execute(
        "INSERT INTO memory_fts VALUES ('mir_old', 'restore me', "
        "'life_domain:work project_type:genesis', 'episodic_memory')"
    )
    # RECENT mirror (embedded, no point, but just written) — SPARED
    db.execute(
        "INSERT INTO memory_metadata (memory_id, collection, created_at, embedding_status) "
        "VALUES ('mir_new', 'episodic_memory', ?, 'embedded')",
        (now,),
    )
    db.execute("INSERT INTO memory_fts VALUES ('mir_new', 'in flight', '', 'episodic_memory')")
    db.commit()
    db.close()


def _points_with(ghost_old_age: str, ghost_new_age: str) -> dict[str, dict[str, str]]:
    return {
        "episodic_memory": {
            "ok": _OLD,  # healthy point (has metadata)
            "ghost_old": ghost_old_age,  # aged ghost (no metadata) → delete
            "ghost_new": ghost_new_age,  # recent ghost (no metadata) → spare
        },
        "knowledge_base": {},
    }


def test_reconciles_aged_offenders_spares_recent(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    export = tmp_path / "ghost_export.jsonl"
    _seed(path)
    client = _FakeClient(_points_with(_OLD, _recent()))
    _patch(monkeypatch, path, client, export)

    assert d0008.verify() is False  # ghost_old + mir_old are aged offenders
    summary = d0008.migrate()
    assert summary == {"ghosts_deleted": 1, "ghost_delete_failed": 0, "mirrors_requeued": 1}
    assert d0008.verify() is True

    # ghost_old deleted from Qdrant; ghost_new (recent) spared; ok untouched.
    assert ("episodic_memory", "ghost_old") in client.deleted
    assert "ghost_new" in client._points["episodic_memory"]
    assert "ok" in client._points["episodic_memory"]

    # payload exported before deletion — one line for the deleted ghost.
    exported = [json.loads(ln) for ln in export.read_text().splitlines()]
    assert [e["point_id"] for e in exported] == ["ghost_old"]
    assert exported[0]["payload"]["created_at"] == _OLD

    db = sqlite3.connect(path)
    # mir_old re-queued: metadata → pending, a pending_embeddings row created.
    status = db.execute(
        "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mir_old'"
    ).fetchone()[0]
    assert status == "pending"
    pend = db.execute(
        "SELECT content, memory_type, source, tags, confidence FROM pending_embeddings "
        "WHERE memory_id = 'mir_old'"
    ).fetchone()
    # facet tags carried through, space→comma translated for the recovery worker;
    # the real metadata confidence (0.9) is carried, NOT reset to the 0.5 default.
    assert pend == (
        "restore me",
        "episodic",
        "d0008_reconcile",
        "life_domain:work,project_type:genesis",
        0.9,
    )
    # mir_new (recent) spared — still embedded, no queue row.
    assert (
        db.execute(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'mir_new'"
        ).fetchone()[0]
        == "embedded"
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'mir_new'"
        ).fetchone()[0]
        == 0
    )
    # healthy row untouched.
    assert (
        db.execute(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'ok'"
        ).fetchone()[0]
        == "embedded"
    )
    db.close()


def test_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    _seed(path)
    client = _FakeClient(_points_with(_OLD, _recent()))
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    assert d0008.migrate()["ghosts_deleted"] == 1
    second = d0008.migrate()
    assert second == {"ghosts_deleted": 0, "ghost_delete_failed": 0, "mirrors_requeued": 0}
    assert d0008.verify() is True
    # no duplicate queue row for the mirror on the second pass
    db = sqlite3.connect(path)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'mir_old'"
        ).fetchone()[0]
        == 1
    )
    db.close()


def test_qdrant_delete_failure_leaves_ghost_for_retry(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    _seed(path)
    client = _FakeClient(_points_with(_OLD, _recent()), fail_ids={"ghost_old"})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    summary = d0008.migrate()
    assert summary["ghosts_deleted"] == 0 and summary["ghost_delete_failed"] == 1
    # ghost point NOT removed → still a candidate → verify retries.
    assert "ghost_old" in client._points["episodic_memory"]
    assert d0008.verify() is False
    # the mirror still got re-queued (independent lane).
    assert summary["mirrors_requeued"] == 1


def test_stale_queue_row_reset_to_drainable_pending(tmp_path, monkeypatch):
    # A lying mirror that still has a reaped 'failed' queue row: the migration must
    # RESET that row to a drainable 'pending' (the recovery worker only drains
    # status='pending'), not skip it while flipping the metadata mirror — otherwise
    # verify() passes but the vector is never rebuilt.
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.execute(
        "INSERT INTO memory_metadata (memory_id, collection, created_at, embedding_status) "
        "VALUES ('m_stale', 'episodic_memory', ?, 'embedded')",
        (_OLD,),
    )
    db.execute(
        "INSERT INTO memory_fts VALUES ('m_stale', 'body', 'wing:memory', 'episodic_memory')"
    )
    db.execute(
        "INSERT INTO pending_embeddings (id, memory_id, content, memory_type, collection, "
        "created_at, status, error_message) "
        "VALUES ('old-id', 'm_stale', 'stale', 'episodic', 'episodic_memory', ?, 'failed', 'boom')",
        (_OLD,),
    )
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    d0008.migrate()
    db = sqlite3.connect(path)
    row = db.execute(
        "SELECT status, error_message, content, tags FROM pending_embeddings "
        "WHERE memory_id = 'm_stale'"
    ).fetchone()
    assert row == ("pending", None, "body", "wing:memory")  # reset + refreshed
    # exactly one queue row (existing reset, not a duplicate insert)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_stale'"
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'm_stale'"
        ).fetchone()[0]
        == "pending"
    )
    db.close()


def test_mirror_without_content_marked_failed(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    # embedded metadata, aged, but NO FTS content to re-embed from.
    db.execute(
        "INSERT INTO memory_metadata (memory_id, collection, created_at, embedding_status) "
        "VALUES ('m_nc', 'episodic_memory', ?, 'embedded')",
        (_OLD,),
    )
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    d0008.migrate()
    db = sqlite3.connect(path)
    assert (
        db.execute(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'm_nc'"
        ).fetchone()[0]
        == "failed"
    )
    assert (
        db.execute("SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_nc'").fetchone()[0]
        == 0
    )
    db.close()


def test_empty_install_is_noop(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    assert d0008.verify() is True
    assert d0008.migrate() == {"ghosts_deleted": 0, "ghost_delete_failed": 0, "mirrors_requeued": 0}


def test_ghost_without_created_at_is_spared(tmp_path, monkeypatch):
    # A point whose payload lacks created_at can't be aged → fail safe, never
    # deleted (avoids nuking a point mid-write whose payload isn't fully visible).
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {"ageless": ""}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    assert d0008.migrate()["ghosts_deleted"] == 0
    assert "ageless" in client._points["episodic_memory"]


def test_deprecated_mirror_is_requeued_worker_restamps(tmp_path, monkeypatch):
    # A deprecated (superseded) lying mirror is requeued like any other mirror —
    # d0008 does NOT special-case it. Deprecation handling lives in the recovery
    # worker, which re-stamps the rebuilt payload as excluded-from-recall
    # (deprecated=True via get_taxonomy), so a normal requeue yields a correctly
    # flagged point rather than resurrecting the superseded memory. Keeping it in
    # ONE place (the worker) also closes the live superseded-while-pending path,
    # which d0008 never touches. See the worker test for the re-stamp itself.
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, collection, created_at, embedding_status, deprecated) "
        "VALUES ('m_dep', 'episodic_memory', ?, 'embedded', 1)",
        (_OLD,),
    )
    db.execute("INSERT INTO memory_fts VALUES ('m_dep', 'superseded body', '', 'episodic_memory')")
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")
    d0008.migrate()
    db = sqlite3.connect(path)
    # Requeued: metadata flipped to 'pending', a drainable queue row created.
    assert (
        db.execute(
            "SELECT embedding_status FROM memory_metadata WHERE memory_id = 'm_dep'"
        ).fetchone()[0]
        == "pending"
    )
    assert (
        db.execute("SELECT status FROM pending_embeddings WHERE memory_id = 'm_dep'").fetchone()[0]
        == "pending"
    )
    db.close()


def test_concurrent_metadata_delete_skips_requeue(tmp_path, monkeypatch):
    # Data migrations run concurrently with the live server. If MemoryStore.delete()
    # completes its SQLite cascade between _enumerate (which listed this mirror) and
    # _requeue_mirror reading the metadata row, the row is gone — the migration must
    # SKIP, never synthesize a pending row that the recovery worker would turn into a
    # Qdrant-only ghost for a memory the user just deleted.
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.execute(
        "INSERT INTO memory_metadata (memory_id, collection, created_at, embedding_status) "
        "VALUES ('m_race', 'episodic_memory', ?, 'embedded')",
        (_OLD,),
    )
    db.execute("INSERT INTO memory_fts VALUES ('m_race', 'body', '', 'episodic_memory')")
    db.commit()
    db.close()
    client = _FakeClient({"episodic_memory": {}, "knowledge_base": {}})
    _patch(monkeypatch, path, client, tmp_path / "exp.jsonl")

    # Simulate the concurrent delete landing between enumerate and requeue: drop the
    # mirror's metadata row just before the mirrors batch is applied.
    real_cib = d0008.commit_in_batches

    def racing_cib(conn, items, apply, **kw):
        if ("m_race", "episodic_memory") in list(items):
            conn.execute("DELETE FROM memory_metadata WHERE memory_id = 'm_race'")
            conn.commit()
        return real_cib(conn, items, apply, **kw)

    monkeypatch.setattr(d0008, "commit_in_batches", racing_cib)
    d0008.migrate()

    db = sqlite3.connect(path)
    # No pending row synthesized for the concurrently-deleted memory.
    assert (
        db.execute("SELECT COUNT(*) FROM pending_embeddings WHERE memory_id = 'm_race'").fetchone()[
            0
        ]
        == 0
    )
    db.close()
