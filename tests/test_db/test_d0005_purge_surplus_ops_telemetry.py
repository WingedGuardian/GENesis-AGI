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
        # (unit_id, qdrant_id, source_pipeline, domain, body) — first line = title,
        # then the machine-report body prefix the signature requires.
        # PURGE: action-task telemetry (title + machine prefix)
        (
            "u1",
            "q1",
            "surplus",
            "intelligence.surplus",
            "Db Maintenance\n\nDatabase maintenance report:   DB file size 266MB",
        ),
        # PURGE: pipeline-intermediate telemetry (specific title, no prefix guard)
        ("u3", "q3", "surplus", "intelligence.surplus", "Research Query Gen\n\nq1; q2; q3"),
        # KEEP: genuine insight (real LLM title, same pipeline/domain)
        ("u2", "q2", "surplus", "intelligence.surplus", "Optimize Memory and Process\n\n…"),
        # KEEP: crawled external intelligence (different domain — not matched)
        ("u4", "q4", "model_intelligence", "intelligence.models", "Model Intelligence\n\n{...}"),
        # KEEP: domain-scope guard — "Model Eval" title but in intelligence.models.
        ("u5", "q5", "surplus", "intelligence.models", "Model Eval\n\nnot telemetry here"),
        # KEEP: Codex-P2 false-delete guard — an INSIGHT finding an LLM titled
        # "Model Eval" in the SAME surplus/intelligence.surplus scope, but with a
        # prose body (no "Model evaluation:" machine prefix) → must survive.
        (
            "u6",
            "q6",
            "surplus",
            "intelligence.surplus",
            "Model Eval\n\nA brainstorm on how we should evaluate candidate models next quarter.",
        ),
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

    # Kept: insight (u2) + crawled (u4) + domain-guarded (u5) + the prose-body
    # "Model Eval" insight (u6, the Codex-P2 false-delete guard).
    assert units == {"u2", "u4", "u5", "u6"}
    assert fts == {"q2", "q4", "q5", "q6"}  # memory_fts cascade removed q1/q3
    assert meta == {"q2", "q4", "q5", "q6"}  # memory_metadata cascade removed q1/q3
    assert kfts == {"u2", "u4", "u5", "u6"}
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


def test_signature_titles_are_all_non_kb_routing():
    # Every purge signature title must be a non-KB-routing task's title() — so the
    # migration can NEVER match an insight-producing task's title. Guards against a
    # future edit adding an insight title to the signature map.
    from genesis.surplus.types import KB_ROUTING_TASK_TYPES, TaskType

    non_kb_titles = {
        tt.value.replace("_", " ").title() for tt in TaskType if tt not in KB_ROUTING_TASK_TYPES
    }
    assert set(d0005._OPS_SIGNATURES) <= non_kb_titles


def test_generic_title_with_prose_body_is_not_purged(tmp_path, monkeypatch):
    # Codex-P2 guard: a generic ops title ("Model Eval") on a real insight with a
    # prose body (no machine-report prefix) must NOT be purged.
    path = tmp_path / "genesis.db"
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    db.execute(
        "INSERT INTO knowledge_units (id, qdrant_id, source_pipeline, domain, body) "
        "VALUES ('x', 'qx', 'surplus', 'intelligence.surplus', "
        "'Model Eval\n\nMy analysis of which models to evaluate.')"
    )
    db.commit()
    db.close()
    _patch(monkeypatch, path)
    assert d0005.migrate()["purged"] == 0
    assert d0005.verify() is True
