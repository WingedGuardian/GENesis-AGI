"""Unit tests for scripts/hooks/hook_input.py — the shared hook payload parser.

`hook_input.py` is the single point through which every guard reads its input,
so a regression in any accessor would silently fail a guard open (the exact bug
this module was written to prevent). These tests exercise the accessors in
isolation, complementing the end-to-end guard coverage in
`test_hook_input_contract.py`. Stdlib-only, install-agnostic.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HELPER = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "hook_input.py"
_spec = importlib.util.spec_from_file_location("hook_input", _HELPER)
hook_input = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook_input)


# ── _loads ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"a": 1}', {"a": 1}),
        ("", {}),
        ("   ", {}),
        ("not json {{{", {}),
        ("[1, 2, 3]", {}),  # valid JSON, not an object
        ('"a string"', {}),  # valid JSON, not an object
        ("42", {}),  # valid JSON, not an object
        ("null", {}),
    ],
)
def test_loads_only_returns_dicts(raw, expected):
    assert hook_input._loads(raw) == expected


# ── tool_input ───────────────────────────────────────────────────────────
def test_tool_input_new_shape_returns_nested():
    p = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert hook_input.tool_input(p) == {"command": "ls"}


def test_tool_input_legacy_flat_shape_falls_back_to_payload():
    # Legacy CLAUDE_TOOL_INPUT was the tool-input dict itself (no wrapper key).
    p = {"command": "ls"}
    assert hook_input.tool_input(p) == {"command": "ls"}


def test_tool_input_non_dict_returns_empty():
    assert hook_input.tool_input(None) == {}
    assert hook_input.tool_input([1, 2]) == {}


def test_tool_input_non_dict_nested_value_falls_back_to_payload():
    # tool_input present but not a dict → treat payload as the tool-input dict.
    p = {"tool_input": "oops", "command": "ls"}
    assert hook_input.tool_input(p) == p


# ── field ────────────────────────────────────────────────────────────────
def test_field_extracts_from_nested():
    p = {"tool_input": {"command": "git push", "file_path": "/x"}}
    assert hook_input.field(p, "command") == "git push"
    assert hook_input.field(p, "file_path") == "/x"


def test_field_missing_returns_default():
    assert hook_input.field({"tool_input": {}}, "command") == ""
    assert hook_input.field({"tool_input": {}}, "command", "DEF") == "DEF"


def test_field_non_string_value_returns_default():
    # A numeric file_path must not reach str-only guard logic — return "".
    assert hook_input.field({"tool_input": {"file_path": 123}}, "file_path") == ""
    assert hook_input.field({"tool_input": {"command": None}}, "command") == ""


# ── tool_response ────────────────────────────────────────────────────────
def test_tool_response_present():
    p = {"tool_response": {"stdout": "ok"}}
    assert hook_input.tool_response(p) == {"stdout": "ok"}


def test_tool_response_absent_or_non_dict(monkeypatch):
    monkeypatch.delenv("CLAUDE_TOOL_USE_RESULT", raising=False)
    assert hook_input.tool_response({}) == {}
    assert hook_input.tool_response({"tool_response": None}) == {}
    assert hook_input.tool_response({"tool_response": "text"}) == {}


def test_tool_response_legacy_env_fallback(monkeypatch):
    monkeypatch.setenv("CLAUDE_TOOL_USE_RESULT", '{"stdout": "legacy"}')
    assert hook_input.tool_response({}) == {"stdout": "legacy"}


# ── session_id ───────────────────────────────────────────────────────────
def test_session_id_from_payload():
    assert hook_input.session_id({"session_id": "abc"}) == "abc"


def test_session_id_empty_falls_through(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    # Empty string in payload must not become a per-session key — use default.
    assert hook_input.session_id({"session_id": ""}) == "unknown"
    assert hook_input.session_id({}, default="none") == "none"


def test_session_id_legacy_env_fallback(monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sid")
    assert hook_input.session_id({}) == "env-sid"


# ── read_payload ─────────────────────────────────────────────────────────
def test_read_payload_stdin(monkeypatch):
    import io

    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"tool_name": "Bash", "tool_input": {"command": "x"}}')
    )
    monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)
    assert hook_input.read_payload() == {"tool_name": "Bash", "tool_input": {"command": "x"}}


def test_read_payload_empty_stdin_uses_legacy_env(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv("CLAUDE_TOOL_INPUT", '{"command": "legacy"}')
    assert hook_input.read_payload() == {"command": "legacy"}


def test_read_payload_malformed_stdin_warns(monkeypatch, capsys):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json {{{"))
    monkeypatch.delenv("CLAUDE_TOOL_INPUT", raising=False)
    assert hook_input.read_payload() == {}
    # A non-empty unparseable payload is surfaced, never silently swallowed.
    assert "WARNING" in capsys.readouterr().err
