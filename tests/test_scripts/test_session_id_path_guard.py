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
import json
import os
import subprocess
import sys
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


class TestUnsafeIdSkipsOnlyThePathRead:
    """An unsafe id must skip the session-SCOPED read, not the whole hook.

    Both of these hooks do path-INDEPENDENT work after reading the id
    (`genesis_stop_hook` runs its giving-up / review-state checks from the hook
    payload alone; `genesis_session_end` writes last-session metadata that uses
    the id as JSON data, not as a path). An earlier revision of this guard
    returned early on an unsafe id and silently disabled that work.
    """

    def _run(self, script: str, payload: dict, home: Path):
        (home / ".genesis").mkdir(parents=True, exist_ok=True)
        (home / ".genesis" / "cc_context_enabled").touch()
        env = {**os.environ, "HOME": str(home)}
        env.pop("GENESIS_CC_SESSION", None)
        return subprocess.run(
            [sys.executable, str(_SCRIPTS / script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )

    def test_session_end_still_writes_metadata_with_unsafe_id(self, tmp_path):
        self._run(
            "genesis_session_end.py",
            {"session_id": "../../escaped", "reason": "clear"},
            tmp_path,
        )
        assert (tmp_path / ".genesis" / "last_foreground_session.json").exists(), (
            "path-independent last-session write was skipped for an unsafe id"
        )
        assert not (tmp_path / "escaped").exists()

    def test_session_end_writes_metadata_with_safe_id(self, tmp_path):
        self._run(
            "genesis_session_end.py",
            {"session_id": REAL_IDS[0], "reason": "clear"},
            tmp_path,
        )
        assert (tmp_path / ".genesis" / "last_foreground_session.json").exists()

    def test_stop_hook_still_runs_payload_only_checks_with_unsafe_id(self, tmp_path):
        """The giving-up check needs only the assistant message; an unsafe id
        must not suppress it."""
        payload = {
            "session_id": "../../escaped",
            "last_assistant_message": "You'll need to run the migration yourself.",
        }
        unsafe = self._run("genesis_stop_hook.py", payload, tmp_path)
        safe = self._run(
            "genesis_stop_hook.py", {**payload, "session_id": REAL_IDS[0]}, tmp_path
        )
        # Whatever the payload-only checks emit, an unsafe id must not change it.
        assert unsafe.stdout == safe.stdout, (
            "unsafe session id changed payload-only hook behaviour"
        )
        assert not (tmp_path / "escaped").exists()
