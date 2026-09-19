"""Regression tests for the proactive hook's aggregate run deadline."""

from __future__ import annotations

import asyncio
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
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_args: None)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())

    started = time.monotonic()
    await hook._run("why did recall stall", session_id="deadline-session")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "the aggregate deadline did not stop the slow recall"
    assert writer.lines == [("[Session trail] deferred", "session-metadata")]
