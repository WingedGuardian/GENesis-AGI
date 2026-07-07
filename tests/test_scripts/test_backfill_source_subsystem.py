"""Tests for scripts/backfill_source_subsystem.py.

Loads the script via importlib, drives it against a tmp SQLite + a duck-typed
stub Qdrant client (no shared FakeQdrant fixture exists — test_scripts
convention is a local stub). Verifies attribution mapping, that user-context
pipelines and already-tagged rows are untouched, orphans (no metadata row) are
skipped, dry-run is a no-op, and --apply is idempotent.
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
    / "scripts" / "backfill_source_subsystem.py"
)


def _load_script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_source_subsystem", _SCRIPT)
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

    def scroll(self, collection_name, limit, offset, with_payload, with_vectors):  # noqa: ANN001
        # Single page; second call (offset set) returns empty + None.
        if offset is not None:
            return [], None
        return list(self._points), None


def _make_db(path: pathlib.Path, rows: list[tuple[str, str | None]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE memory_metadata "
        "(memory_id TEXT PRIMARY KEY, source_subsystem TEXT)"
    )
    conn.executemany(
        "INSERT INTO memory_metadata (memory_id, source_subsystem) VALUES (?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def _subsystem_map(path: pathlib.Path) -> dict[str, str | None]:
    conn = sqlite3.connect(path)
    out = {
        r[0]: r[1]
        for r in conn.execute("SELECT memory_id, source_subsystem FROM memory_metadata")
    }
    conn.close()
    return out


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    db = tmp_path / "genesis.db"
    _make_db(db, [
        ("refl-1", None),          # reflection, NULL -> should tag reflection
        ("deep-1", None),          # deep_reflection, NULL -> reflection
        ("auto-1", None),          # module:automaton_supervisor -> autonomy
        ("user-1", None),          # session_observer -> stays NULL
        ("done-1", "reflection"),  # already tagged -> untouched
        # note: "orphan-1" below has NO metadata row (Qdrant-only)
    ])
    points = [
        _Point("refl-1", "reflection"),
        _Point("deep-1", "deep_reflection"),
        _Point("auto-1", "module:automaton_supervisor"),
        _Point("user-1", "session_observer"),
        _Point("done-1", "reflection"),
        _Point("orphan-1", "reflection"),   # no metadata row
    ]
    stub = _StubQdrant(points)

    module = _load_script()
    import genesis.env as genv

    monkeypatch.setattr(genv, "genesis_db_path", lambda: db)
    monkeypatch.setattr(genv, "qdrant_url", lambda: "http://stub")
    monkeypatch.setattr("qdrant_client.QdrantClient", lambda **_kw: stub)
    return module, db


def test_dry_run_changes_nothing(scenario):
    module, db = scenario
    asyncio.run(module.main(apply=False))
    assert _subsystem_map(db) == {
        "refl-1": None, "deep-1": None, "auto-1": None,
        "user-1": None, "done-1": "reflection",
    }


def test_apply_tags_by_attribution(scenario):
    module, db = scenario
    asyncio.run(module.main(apply=True))
    result = _subsystem_map(db)
    assert result["refl-1"] == "reflection"
    assert result["deep-1"] == "reflection"
    assert result["auto-1"] == "autonomy"      # module folds into autonomy
    assert result["user-1"] is None            # user-context untouched
    assert result["done-1"] == "reflection"    # already tagged untouched
    # orphan-1 has no metadata row; nothing to insert
    assert "orphan-1" not in result


def test_apply_is_idempotent(scenario):
    module, db = scenario
    asyncio.run(module.main(apply=True))
    first = _subsystem_map(db)
    asyncio.run(module.main(apply=True))
    assert _subsystem_map(db) == first
