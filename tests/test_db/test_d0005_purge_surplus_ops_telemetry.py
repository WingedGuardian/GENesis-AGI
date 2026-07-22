"""d0005 — purge surplus operational-telemetry rows from the knowledge base.

Non-KB-routing surplus tasks (action/maintenance/monitor/pipeline-intermediate)
wrote single_item ops-telemetry KB units before the KB_ROUTING gate. This purges
them across the full MemoryStore.delete() cross-store cascade, while leaving
genuine insight units (real titles) and non-surplus-domain rows intact.
"""

from __future__ import annotations

import sqlite3

import genesis.db.data_migrations.d0005_purge_surplus_ops_telemetry as d0005

# Minimal slices of the tables the migration touches (columns it reads/deletes).
_SCHEMA = """
CREATE TABLE knowledge_units (id TEXT PRIMARY KEY, qdrant_id TEXT,
    source_pipeline TEXT, domain TEXT, body TEXT);
CREATE TABLE knowledge_fts (unit_id TEXT, body TEXT);
CREATE TABLE memory_fts (memory_id TEXT, content TEXT, collection TEXT);
CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, collection TEXT);
CREATE TABLE memory_links (source_id TEXT, target_id TEXT, link_type TEXT);
CREATE TABLE pending_embeddings (id TEXT, memory_id TEXT);
CREATE TABLE entity_mentions (memory_id TEXT, entity_id TEXT);
"""


def _seed(path) -> None:
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    units = [
        # (unit_id, qdrant_id, source_pipeline, domain, body) — first line = title
        # PURGE: action-task telemetry
        ("u1", "q1", "surplus", "intelligence.surplus", "Db Maintenance\n\nDB size 266MB"),
        # PURGE: pipeline-intermediate telemetry
        ("u3", "q3", "surplus", "intelligence.surplus", "Research Query Gen\n\nq1; q2; q3"),
        # KEEP: genuine insight (real LLM title, same pipeline/domain)
        ("u2", "q2", "surplus", "intelligence.surplus", "Optimize Memory and Process\n\n…"),
        # KEEP: crawled external intelligence (different domain — not matched)
        ("u4", "q4", "model_intelligence", "intelligence.models", "Model Intelligence\n\n{...}"),
        # KEEP: a title collision guard — "Model Eval" is an ops title, but this
        # row is in a different domain, so the domain scope must protect it.
        ("u5", "q5", "surplus", "intelligence.models", "Model Eval\n\nnot telemetry here"),
    ]
    db.executemany(
        "INSERT INTO knowledge_units (id, qdrant_id, source_pipeline, domain, body) "
        "VALUES (?, ?, ?, ?, ?)",
        units,
    )
    # Mirror rows keyed by qdrant_id for every unit, plus the knowledge_fts pair.
    for uid, qid, *_ in units:
        db.execute("INSERT INTO knowledge_fts (unit_id, body) VALUES (?, ?)", (uid, "b"))
        db.execute(
            "INSERT INTO memory_fts (memory_id, content, collection) VALUES (?, ?, 'knowledge_base')",
            (qid, "c"),
        )
        db.execute(
            "INSERT INTO memory_metadata (memory_id, collection) VALUES (?, 'knowledge_base')",
            (qid,),
        )
        db.execute(
            "INSERT INTO memory_links (source_id, target_id, link_type) VALUES (?, 'x', 'rel')",
            (qid,),
        )
        db.execute(
            "INSERT INTO pending_embeddings (id, memory_id) VALUES (?, ?)", (f"p-{qid}", qid)
        )
        db.execute("INSERT INTO entity_mentions (memory_id, entity_id) VALUES (?, 'e1')", (qid,))
    db.commit()
    db.close()


def _patch(monkeypatch, path, *, deleted=None, qdrant_fail_ids=()):
    monkeypatch.setattr(d0005, "genesis_db_path", lambda: str(path))
    monkeypatch.setattr(d0005, "get_client", lambda: "FAKE_CLIENT")

    def _fake_delete(client, *, collection, point_id):
        if point_id in qdrant_fail_ids:
            raise RuntimeError("qdrant down")
        if deleted is not None:
            deleted.append((collection, point_id))

    monkeypatch.setattr(d0005, "delete_point", _fake_delete)


def test_purges_only_ops_telemetry_in_surplus_domain(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    _seed(path)
    deleted: list = []
    _patch(monkeypatch, path, deleted=deleted)

    assert d0005.verify() is False
    summary = d0005.migrate()
    assert summary["purged"] == 2  # u1 (Db Maintenance) + u3 (Research Query Gen)
    assert d0005.verify() is True

    db = sqlite3.connect(path)
    units = {r[0] for r in db.execute("SELECT id FROM knowledge_units").fetchall()}
    # Cross-store: no orphaned mirror rows for the purged points.
    fts = {r[0] for r in db.execute("SELECT memory_id FROM memory_fts").fetchall()}
    meta = {r[0] for r in db.execute("SELECT memory_id FROM memory_metadata").fetchall()}
    kfts = {r[0] for r in db.execute("SELECT unit_id FROM knowledge_fts").fetchall()}
    db.close()

    assert units == {"u2", "u4", "u5"}  # insight + crawled + domain-guarded kept
    assert fts == {"q2", "q4", "q5"}  # memory_fts cascade removed q1/q3
    assert meta == {"q2", "q4", "q5"}  # memory_metadata cascade removed q1/q3
    assert kfts == {"u2", "u4", "u5"}
    assert set(deleted) == {("knowledge_base", "q1"), ("knowledge_base", "q3")}


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    _seed(path)
    _patch(monkeypatch, path)
    assert d0005.migrate()["purged"] == 2
    assert d0005.migrate()["purged"] == 0
    assert d0005.verify() is True


def test_qdrant_failure_leaves_unit_for_retry(tmp_path, monkeypatch):
    # A failed Qdrant delete must NOT delete the SQLite rows (no orphan vector);
    # the unit stays a candidate so verify() is False and the migration retries.
    path = tmp_path / "genesis.db"
    _seed(path)
    _patch(monkeypatch, path, qdrant_fail_ids={"q1"})
    summary = d0005.migrate()
    assert summary["purged"] == 1  # only u3 (q3) purged; u1 (q1) failed
    assert summary["qdrant_failed"] == 1
    assert d0005.verify() is False  # u1 still present → will retry next boot

    db = sqlite3.connect(path)
    units = {r[0] for r in db.execute("SELECT id FROM knowledge_units").fetchall()}
    db.close()
    assert "u1" in units  # left intact for retry


def test_empty_db_verifies_clean(tmp_path, monkeypatch):
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.commit()
    db.close()
    _patch(monkeypatch, path)
    assert d0005.verify() is True
    assert d0005.migrate() == {"purged": 0, "qdrant_deleted": 0}


def test_purge_titles_match_live_enum_derivation():
    # The hardcoded signature list must equal the non-KB-routing task titles, so
    # it can't silently drift from the dispatch gate.
    from genesis.surplus.types import KB_ROUTING_TASK_TYPES, TaskType

    derived = {
        tt.value.replace("_", " ").title() for tt in TaskType if tt not in KB_ROUTING_TASK_TYPES
    }
    assert derived == d0005._OPS_TELEMETRY_TITLES
