"""Regression tests for the proactive hook's aggregate run deadline."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_REPO_DIR = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _REPO_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import proactive_memory_hook as hook  # noqa: E402


class _RecordingWriter:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def emit(self, text: str, *, block: str) -> None:
        self.lines.append((text, block))


@pytest.mark.asyncio
async def test_run_flushes_deferred_lines_when_recall_exceeds_total_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A slow recall cannot strand metadata when the aggregate budget expires."""
    writer = _RecordingWriter()
    db_path = tmp_path / "genesis.db"
    db_path.touch()

    async def _slow_server(*_args, **_kwargs):
        await asyncio.sleep(0.25)
        return None, "slow recall"

    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 0.01)
    monkeypatch.setattr(hook, "_HOOK_MODE", "server")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_call_server", _slow_server)
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_args, **_kwargs: ["deadline"])
    monkeypatch.setattr(
        hook,
        "_update_and_format_trail",
        lambda *_args, **_kwargs: "[Session trail] deferred",
    )
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())

    started = time.monotonic()
    await hook._run("why did recall stall", session_id="deadline-session")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "the aggregate deadline did not stop the slow recall"
    assert writer.lines == [("[Session trail] deferred", "session-metadata")]
    assert (tmp_path / ".genesis" / ".knowledge_retrieved_count_migrated").exists()


@pytest.mark.asyncio
async def test_local_mode_stops_after_blocking_sync_phase_exhausts_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocking local phase cannot make later sync phases run past the deadline."""
    writer = _RecordingWriter()
    db_path = tmp_path / "genesis.db"
    db_path.touch()
    code_calls: list[bool] = []

    def _blocking_fts(*_args, **_kwargs):
        time.sleep(0.05)
        return []

    def _code_search(*_args, **_kwargs):
        code_calls.append(True)
        time.sleep(0.2)
        return []

    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 0.01)
    monkeypatch.setattr(hook, "_HOOK_MODE", "local")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_args, **_kwargs: ["deadline"])
    monkeypatch.setattr(
        hook,
        "_update_and_format_trail",
        lambda *_args, **_kwargs: "[Session trail] deferred",
    )
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())
    monkeypatch.setattr(hook, "_search_fts5", _blocking_fts)
    monkeypatch.setattr(hook, "_search_code_index", _code_search)

    started = time.monotonic()
    await hook._run("why did recall stall", session_id="deadline-session")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "a later sync phase ran after the aggregate deadline"
    assert code_calls == []
    assert writer.lines == [("[Session trail] deferred", "session-metadata")]
    assert not (tmp_path / ".genesis" / ".knowledge_retrieved_count_migrated").exists()


def test_knowledge_migration_adds_missing_column(tmp_path: Path) -> None:
    db_path = tmp_path / "genesis.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE knowledge_units (id INTEGER PRIMARY KEY)")

    assert hook._ensure_knowledge_retrieved_count(db_path) is True

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_units)")}
    assert "retrieved_count" in columns


def test_knowledge_migration_accepts_duplicate_column(tmp_path: Path) -> None:
    db_path = tmp_path / "genesis.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE knowledge_units "
            "(id INTEGER PRIMARY KEY, retrieved_count INTEGER NOT NULL DEFAULT 0)"
        )

    assert hook._ensure_knowledge_retrieved_count(db_path) is True


def test_knowledge_migration_propagates_interrupted_alter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _InterruptedConnection:
        def execute(self, _sql: str) -> None:
            raise sqlite3.OperationalError("interrupted")

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        hook,
        "_sqlite_connect",
        lambda *_args, **_kwargs: _InterruptedConnection(),
    )

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        hook._ensure_knowledge_retrieved_count(tmp_path / "genesis.db")
