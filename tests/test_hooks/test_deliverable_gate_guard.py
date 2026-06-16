"""Tests for the deliverable-builder Stop-hook gate.

The gate's contract (safety-critical — it can block session-end):
  - block (exit 2) ONLY when this session's marker is status=rendered_unverified
  - allow (exit 0) in every other state, no marker, foreign-session marker, or ANY error
  - fail-open is absolute: a bug must never prevent a session from ending
"""
import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HOOK_PATH = REPO / "scripts" / "hooks" / "deliverable_gate_guard.py"


def _load():
    spec = importlib.util.spec_from_file_location("deliverable_gate_guard", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load()


def _write_marker(root: Path, sid: str, spec: dict) -> None:
    d = root / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "deliverable.json").write_text(json.dumps(spec))


# --- _decide: the pure decision matrix ---

def test_allows_when_no_session_id(tmp_path):
    assert mod._decide({}, tmp_path) == 0


def test_allows_when_no_marker(tmp_path):
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_status_drafting(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1", "status": "drafting"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_blocks_status_rendered_unverified(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1", "status": "rendered_unverified"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 2


def test_allows_status_verified(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1", "status": "verified"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_status_shipped(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1", "status": "shipped"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_status_cancelled(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1", "status": "cancelled"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_foreign_session_marker(tmp_path):
    # marker claims a different session -> must NOT block this one
    _write_marker(tmp_path, "s1", {"session_id": "OTHER", "status": "rendered_unverified"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_marker_without_session_id_field(tmp_path):
    # ambiguous ownership (no session_id in the marker) -> never block
    _write_marker(tmp_path, "s1", {"status": "rendered_unverified"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_malformed_marker_json(tmp_path):
    d = tmp_path / "s1"
    d.mkdir()
    (d / "deliverable.json").write_text("{ not valid json")
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


def test_allows_missing_status_field(tmp_path):
    _write_marker(tmp_path, "s1", {"session_id": "s1"})
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


@pytest.mark.parametrize("bad", ["../evil", "a/b", "..", "/abs", "", "\x00bad"])
def test_allows_path_unsafe_session_id(tmp_path, bad):
    assert mod._decide({"session_id": bad}, tmp_path) == 0


def test_fail_open_when_marker_is_a_directory(tmp_path):
    # deliverable.json is a dir -> read raises -> allow
    (tmp_path / "s1" / "deliverable.json").mkdir(parents=True)
    assert mod._decide({"session_id": "s1"}, tmp_path) == 0


# --- main(): stdin parsing + sessions-root wiring + block message ---

def test_main_allows_on_empty_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert mod.main() == 0


def test_main_allows_on_malformed_stdin(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{ bad json"))
    assert mod.main() == 0


def test_main_blocks_and_warns(tmp_path, monkeypatch, capsys):
    sessroot = tmp_path / "sessions"
    _write_marker(sessroot, "s1", {"session_id": "s1", "status": "rendered_unverified"})
    monkeypatch.setattr(mod, "_sessions_root", lambda: sessroot)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    code = mod.main()
    assert code == 2
    assert "verif" in capsys.readouterr().err.lower()
