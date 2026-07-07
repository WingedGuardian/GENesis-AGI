"""Tests for scripts/cleanup_subsystem_qdrant.py — focus on the orphan sweep.

The SQLite-driven purge only deletes Qdrant points that have a tagged
memory_metadata row. The payload-based orphan sweep (Step 2b) additionally
deletes machine-leak points that have NO metadata row (Qdrant-only orphans),
while leaving non-leak points (e.g. session_observer) untouched.
"""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sqlite3
import types

import pytest

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts" / "cleanup_subsystem_qdrant.py"
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("cleanup_subsystem_qdrant", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Point:
    def __init__(self, pid: str, pipeline: str | None) -> None:
        self.id = pid
        self.payload = {"source_pipeline": pipeline}


class _StubQdrant:
    def __init__(self, points: list[_Point]) -> None:
        self._points = points
        self.deleted: list[str] = []

    def get_collection(self, collection_name):  # noqa: ANN001
        return types.SimpleNamespace(points_count=len(self._points))

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):  # noqa: ANN001
        if offset is not None:
            return [], None
        return list(self._points), None

    def delete(self, collection_name, points_selector):  # noqa: ANN001
        self.deleted.extend(str(p) for p in points_selector.points)


def _make_db(path: pathlib.Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, "
        "source_subsystem TEXT, collection TEXT, invalid_at TEXT, "
        "embedding_status TEXT)"
    )
    conn.execute("CREATE TABLE memory_fts (memory_id TEXT, tags TEXT)")
    conn.execute("CREATE TABLE observations (id TEXT PRIMARY KEY, expires_at TEXT)")
    # One tagged row (matched) -> Step 2 deletes its point. Its
    # embedding_status is the stale 'embedded' the purge must reconcile to
    # 'fts5_only' (Step 2c).
    conn.execute(
        "INSERT INTO memory_metadata "
        "(memory_id, source_subsystem, collection, invalid_at, embedding_status) "
        "VALUES ('refl-1', 'reflection', 'episodic_memory', NULL, 'embedded')"
    )
    conn.commit()
    conn.close()


def _embedding_status(path: pathlib.Path, memory_id: str) -> str | None:
    conn = sqlite3.connect(path)
    row = conn.execute(
        "SELECT embedding_status FROM memory_metadata WHERE memory_id = ?",
        (memory_id,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _make_db(db)
    stub = _StubQdrant([
        _Point("refl-1", "reflection"),        # matched (has metadata row)
        _Point("orphan-1", "reflection"),      # leak pipeline, NO metadata row
        _Point("user-1", "session_observer"),  # non-leak, must survive
    ])
    module = _load_script()
    import genesis.env as genv

    monkeypatch.setattr(genv, "genesis_db_path", lambda: db)
    monkeypatch.setattr(genv, "qdrant_url", lambda: "http://stub")
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kw: stub)
    return module, stub


def test_apply_deletes_matched_and_orphan_not_user(scenario):
    module, stub = scenario
    asyncio.run(module.main(apply=True))
    # Matched (Step 2) + orphan (Step 2b) deleted; user-context point survives.
    assert "refl-1" in stub.deleted
    assert "orphan-1" in stub.deleted
    assert "user-1" not in stub.deleted


def test_dry_run_deletes_nothing(scenario):
    module, stub = scenario
    asyncio.run(module.main(apply=False))
    assert stub.deleted == []


def test_apply_reconciles_stale_embedding_status(tmp_path, monkeypatch):
    """The purge deletes the Qdrant vector, so a subsystem row that was
    'embedded' must be reconciled to 'fts5_only' (Step 2c) — otherwise the
    field lies about vector presence and mark_superseded would issue a doomed
    update_payload on the deleted point."""
    db = tmp_path / "genesis.db"
    _make_db(db)  # refl-1 tagged 'reflection', embedding_status='embedded'
    stub = _StubQdrant([_Point("refl-1", "reflection")])
    module = _load_script()
    import genesis.env as genv

    monkeypatch.setattr(genv, "genesis_db_path", lambda: db)
    monkeypatch.setattr(genv, "qdrant_url", lambda: "http://stub")
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kw: stub)

    # Dry-run: status untouched.
    asyncio.run(module.main(apply=False))
    assert _embedding_status(db, "refl-1") == "embedded"

    # Apply: reconciled to fts5_only.
    asyncio.run(module.main(apply=True))
    assert _embedding_status(db, "refl-1") == "fts5_only"

    # Idempotent: second apply leaves it fts5_only.
    asyncio.run(module.main(apply=True))
    assert _embedding_status(db, "refl-1") == "fts5_only"


def test_orphan_sweep_runs_with_no_tagged_rows(tmp_path, monkeypatch):
    """Regression: the sweep must run even when memory_metadata has no tagged
    rows (pure-orphan install), i.e. the early return must not skip Step 2b."""
    db = tmp_path / "genesis.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, "
        "source_subsystem TEXT, collection TEXT, invalid_at TEXT, "
        "embedding_status TEXT)"
    )
    conn.execute("CREATE TABLE memory_fts (memory_id TEXT, tags TEXT)")
    conn.execute("CREATE TABLE observations (id TEXT PRIMARY KEY, expires_at TEXT)")
    conn.commit()
    conn.close()  # no source_subsystem-tagged rows at all

    stub = _StubQdrant([_Point("orphan-1", "reflection")])
    module = _load_script()
    import genesis.env as genv

    monkeypatch.setattr(genv, "genesis_db_path", lambda: db)
    monkeypatch.setattr(genv, "qdrant_url", lambda: "http://stub")
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kw: stub)

    asyncio.run(module.main(apply=True))
    assert "orphan-1" in stub.deleted
