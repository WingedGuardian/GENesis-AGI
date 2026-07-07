"""Tests for the in-flight block emission logic in the SessionStart hook.

Guards the fold/divider wiring that the subprocess integration tests can't
reach (they run with HOME=tmp_path, so the real genesis.db is absent and
_load_inflight_block() short-circuits to "").
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "genesis_session_context.py"
)
_spec = importlib.util.spec_from_file_location("genesis_session_context", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_chunks = _mod._inflight_emission_chunks

_SENTINEL = "### In-flight state SENTINEL"


def test_empty_block_emits_nothing():
    assert _chunks("", ek_emitted=True, first=False) == []
    assert _chunks("", ek_emitted=False, first=True) == []


def test_folds_under_ek_with_no_divider():
    out = _chunks(_SENTINEL, ek_emitted=True, first=False)
    assert out == ["\n\n" + _SENTINEL]
    # Critical: no horizontal-rule divider when riding under Essential Knowledge.
    assert "---" not in "".join(out)


def test_standalone_after_other_blocks_gets_divider():
    # EK absent but something else already emitted → standard divider precedes it.
    out = _chunks(_SENTINEL, ek_emitted=False, first=False)
    assert out == ["\n\n---\n\n", _SENTINEL]


def test_standalone_first_block_no_divider():
    # EK absent and nothing emitted yet → block stands alone, no leading divider.
    out = _chunks(_SENTINEL, ek_emitted=False, first=True)
    assert out == [_SENTINEL]
    assert "---" not in "".join(out)
