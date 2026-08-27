"""A CC-supplied ``session_id`` becomes a filesystem PATH COMPONENT in several
hooks, so an id carrying ``/`` or ``..`` escapes the per-session directory —
two of the sites ``mkdir(parents=True)``, so the escape CREATES directories.

The guard already existed in five other hooks, hand-copied in three different
shapes; these sites simply omitted it. This locks the shared validator
(``hook_input.is_safe_session_id``) and its adoption at the previously-unguarded
sites.

Install-agnostic: every hook's base directory is monkeypatched to ``tmp_path``;
nothing touches the real ``~/.genesis``.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook_input = _load("hook_input", "hooks/hook_input.py")

# Traversal ids that must never reach a path join.
TRAVERSALS = ["../../escaped", "a/b", "..", "../x", "a\\b", "with\x00nul", "abc\n"]
# Ids in the real CC shape (measured: hex UUID v4) must keep working.
# Synthetic ids in CC's observed shape (lowercase hex UUID v4). Deliberately
# NOT real session ids from any install — fixtures stay install-agnostic.
REAL_IDS = [
    "00000000-0000-4000-8000-000000000001",
    "abcdef01-2345-4678-89ab-cdef01234567",
    "0f1e2d3c-4b5a-4967-8877-66554433221f",
]


class TestIsSafeSessionId:
    @pytest.mark.parametrize("sid", REAL_IDS)
    def test_real_session_ids_accepted(self, sid):
        assert hook_input.is_safe_session_id(sid) is True

    @pytest.mark.parametrize("sid", TRAVERSALS)
    def test_traversal_shapes_rejected(self, sid):
        assert hook_input.is_safe_session_id(sid) is False

    def test_empty_rejected(self):
        assert hook_input.is_safe_session_id("") is False

    def test_non_string_rejected(self):
        assert hook_input.is_safe_session_id(None) is False

    def test_overlong_rejected(self):
        assert hook_input.is_safe_session_id("a" * 129) is False

    def test_trailing_newline_rejected(self):
        """``^…$`` would ACCEPT this (``$`` matches before a final newline);
        the shared validator anchors with ``\\A…\\Z``."""
        assert hook_input.is_safe_session_id(REAL_IDS[0] + "\n") is False


class TestSessionIdAccessorRejectsUnsafe:
    def test_unsafe_payload_id_collapses_to_default(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert hook_input.session_id({"session_id": "../../escaped"}) == "unknown"

    def test_safe_payload_id_passes_through(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert hook_input.session_id({"session_id": REAL_IDS[0]}) == REAL_IDS[0]

    def test_unsafe_env_id_collapses_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "a/b")
        assert hook_input.session_id({}) == "unknown"


class TestHooksDoNotEscapeSessionDir:
    """The load-bearing RED: these sites build a path from the raw id today."""

    def test_urgent_alerts_buffer_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("genesis_urgent_alerts", "genesis_urgent_alerts.py")
        monkeypatch.setattr(mod, "_GENESIS_DIR", tmp_path / ".genesis")
        mod._buffer_message("../../escaped", "hi", datetime.now(UTC))
        assert not (tmp_path / "escaped").exists(), "traversal created a dir outside sessions/"

    def test_urgent_alerts_staleness_marker_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("genesis_urgent_alerts", "genesis_urgent_alerts.py")
        monkeypatch.setattr(mod, "_GENESIS_DIR", tmp_path / ".genesis")
        mod._record_staleness_nudge("../../escaped2", datetime.now(UTC))
        assert not (tmp_path / "escaped2").exists()

    def test_proactive_trail_path_does_not_escape(self, tmp_path, monkeypatch):
        mod = _load("proactive_memory_hook", "proactive_memory_hook.py")
        monkeypatch.setattr(mod, "_TRAIL_DIR", tmp_path / "sessions")
        p = mod._trail_path("../../escaped")
        if p is None:
            return  # rejected outright is also correct
        base = (tmp_path / "sessions").resolve()
        # RESOLVE both sides — PurePath.parents does NOT collapse "..", so a
        # lexical containment check here would pass vacuously.
        assert base in p.resolve().parents, f"escaped to {p.resolve()}"
