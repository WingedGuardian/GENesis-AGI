"""Tests for scripts/pretool_check.py — PreToolUse hook."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

# Load the script as a module (it's not a package, so use importlib)
_SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pretool_check.py"
_spec = importlib.util.spec_from_file_location("pretool_check", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_load_critical_patterns = _mod._load_critical_patterns
_matches = _mod._matches
main = _mod.main
_CONFIG_PATH = _mod._CONFIG_PATH
_FALLBACK_CRITICAL = _mod._FALLBACK_CRITICAL


def _run_main(file_path: str, *, dispatched: bool = True) -> int:
    """Run main() with a mock stdin containing the given file_path.

    pretool_check blocks CRITICAL paths in a DISPATCHED session only (matches
    the config's "direct sessions allowed" intent), so the block-behavior tests
    run dispatched by default; interactive is exercised explicitly below.
    """
    import os
    from unittest.mock import patch as _patch

    stdin_data = json.dumps({"file_path": file_path})
    env_patch = {"GENESIS_CC_SESSION": "1"} if dispatched else {}
    with patch("sys.stdin") as mock_stdin, _patch.dict(os.environ, env_patch, clear=False):
        if not dispatched:
            os.environ.pop("GENESIS_CC_SESSION", None)
        mock_stdin.read.return_value = stdin_data
        # Rebind the module's sys reference
        old_stdin = _mod.sys.stdin
        _mod.sys.stdin = mock_stdin
        try:
            return main()
        finally:
            _mod.sys.stdin = old_stdin


def test_blocks_critical_path():
    """Valid config + critical path (dispatched session) → exit 2."""
    result = _run_main("usr/secrets.env")
    assert result == 2


def test_interactive_session_allows_critical_path():
    """A direct interactive session (no GENESIS_CC_SESSION) is allowed to edit a
    CRITICAL path — the block targets dispatched/relay channels only."""
    assert _run_main("usr/secrets.env", dispatched=False) == 0


def test_allows_normal_path():
    """Valid config + normal path → exit 0."""
    result = _run_main("src/genesis/cc/invoker.py")
    assert result == 0


def test_fallback_blocks_secrets(tmp_path):
    """Missing config → fallback still blocks */secrets.env."""
    with patch.object(_mod, "_CONFIG_PATH", tmp_path / "nonexistent.yaml"):
        patterns = _load_critical_patterns()
    assert patterns == _FALLBACK_CRITICAL
    match = _matches("usr/secrets.env", patterns)
    assert match is not None


def test_fallback_blocks_settings(tmp_path):
    """Missing config → fallback still blocks .claude/settings.json."""
    with patch.object(_mod, "_CONFIG_PATH", tmp_path / "nonexistent.yaml"):
        patterns = _load_critical_patterns()
    match = _matches(".claude/settings.json", patterns)
    assert match is not None


def test_fallback_allows_normal_with_missing_config(tmp_path):
    """Missing config + normal path → exit 0 (fallback doesn't block everything)."""
    with patch.object(_mod, "_CONFIG_PATH", tmp_path / "nonexistent.yaml"):
        patterns = _load_critical_patterns()
    match = _matches("src/genesis/cc/invoker.py", patterns)
    assert match is None


def test_json_parse_failure_allows():
    """Malformed stdin → exit 0 (documented fail-open for parse errors)."""
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.read.return_value = "not json at all"
        old_stdin = _mod.sys.stdin
        _mod.sys.stdin = mock_stdin
        try:
            result = main()
        finally:
            _mod.sys.stdin = old_stdin
    assert result == 0


def test_empty_file_path_allows():
    """Empty file_path in JSON → exit 0."""
    result = _run_main("")
    assert result == 0


def test_glob_star_matching():
    """src/genesis/channels/foo.py matches src/genesis/channels/**."""
    patterns = ["src/genesis/channels/**"]
    match = _matches("src/genesis/channels/foo.py", patterns)
    assert match is not None

    # Also test deeper nesting
    match2 = _matches("src/genesis/channels/telegram/handlers.py", patterns)
    assert match2 is not None
