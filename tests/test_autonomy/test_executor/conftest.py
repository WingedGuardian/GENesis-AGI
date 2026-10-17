"""Shared fixtures for the executor tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _private_worktree_base(tmp_path, monkeypatch):
    """Task worktrees resolve under ``<checkout>/.claude/worktrees`` from the
    engine's own file. The git calls are mocked, but the engine still writes
    ``TASK_NOTEPAD.md`` into that path, so a run from the deployed checkout could
    write into a real task worktree there. Only the base moves: every git
    argument (``_REPO_ROOT``) is unchanged."""
    from genesis.autonomy.executor import engine

    monkeypatch.setattr(engine, "_WORKTREE_BASE", tmp_path / "worktrees")
