"""Tests for the routed-session NOTICE in the SessionStart hook."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_context.py"
_spec = importlib.util.spec_from_file_location("genesis_session_context", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_routed_session_notice = _mod._routed_session_notice


def test_native_session_has_no_notice():
    assert _routed_session_notice(None) is None
    assert _routed_session_notice("") is None


def test_peer_session_notice_names_the_model_and_steers_header():
    block = _routed_session_notice("glm-5.2")
    assert block is not None
    assert "glm-5.2" in block
    assert "NOT native Claude" in block
    # Steers the header to the true model and warns about MCP limits.
    assert "[glm-5.2 / <effort>]" in block
    assert "MCP" in block
