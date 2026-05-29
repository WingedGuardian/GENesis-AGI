"""Regression tests for file_context_hook path filtering.

The hook tracks which files a session touches. A bug (literal ``${HOME}``
comparison) silently rejected all files because CC passes absolute paths.
These tests ensure the fix holds.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Hook lives outside the package tree — add scripts/ to import path
SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import file_context_hook  # noqa: E402


@pytest.fixture()
def session_dir(tmp_path, monkeypatch):
    """Redirect session state writes to a temp directory."""
    sessions_base = tmp_path / ".genesis" / "sessions"
    sessions_base.mkdir(parents=True)
    # Patch os.path.expanduser globally — the hook calls it to resolve
    # ~/.genesis/sessions.  Keep the real expanduser for paths we don't
    # care about.
    _real = os.path.expanduser

    def _fake_expanduser(p: str) -> str:
        if "/.genesis/" in p:
            return p.replace("~", str(tmp_path), 1)
        return _real(p)

    monkeypatch.setattr(os.path, "expanduser", _fake_expanduser)
    return sessions_base


class TestPathFilter:
    """The hook must accept absolute project paths and reject non-project paths."""

    def test_absolute_project_path_accepted(self, session_dir):
        data = {
            "session_id": "test-001",
            "tool_name": "Read",
            "tool_input": {"file_path": f"{Path.home()}/genesis/src/genesis/memory/store.py"},
        }
        file_context_hook._process(data)
        state = session_dir / "test-001" / "recent_files.json"
        assert state.exists(), "recent_files.json should have been created"
        stored = json.loads(state.read_text())
        assert f"{Path.home()}/genesis/src/genesis/memory/store.py" in stored

    def test_non_project_path_rejected(self, session_dir):
        data = {
            "session_id": "test-002",
            "tool_name": "Read",
            "tool_input": {"file_path": "/etc/passwd"},
        }
        file_context_hook._process(data)
        state = session_dir / "test-002" / "recent_files.json"
        assert not state.exists(), "Non-project path should be filtered out"

    def test_home_dotgenesis_path_rejected(self, session_dir):
        data = {
            "session_id": "test-003",
            "tool_name": "Read",
            "tool_input": {"file_path": f"{Path.home()}/.genesis/config.yaml"},
        }
        file_context_hook._process(data)
        state = session_dir / "test-003" / "recent_files.json"
        assert not state.exists(), "~/.genesis/ paths should be filtered out"

    def test_literal_dollar_home_rejected(self, session_dir):
        """Regression: the old literal '${HOME}/genesis' must NOT match."""
        data = {
            "session_id": "test-004",
            "tool_name": "Read",
            "tool_input": {"file_path": "${HOME}/genesis/src/foo.py"},
        }
        file_context_hook._process(data)
        state = session_dir / "test-004" / "recent_files.json"
        assert not state.exists(), "Literal ${HOME} should not match"

    def test_glob_tool_uses_path_field(self, session_dir):
        data = {
            "session_id": "test-005",
            "tool_name": "Glob",
            "tool_input": {"path": f"{Path.home()}/genesis/src"},
        }
        file_context_hook._process(data)
        state = session_dir / "test-005" / "recent_files.json"
        assert state.exists()
