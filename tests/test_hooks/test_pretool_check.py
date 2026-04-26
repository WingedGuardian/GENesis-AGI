"""Comprehensive tests for scripts/pretool_check.py — PreToolUse hook.

Tests the hook both as a subprocess (how CC actually invokes it) and via
direct function imports for finer-grained unit testing.

Exit codes:
  0 — allow (path is not CRITICAL)
  2 — block (path is CRITICAL, cannot be modified from relay channel)
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_SCRIPT_PATH = _WORKTREE / "scripts" / "pretool_check.py"
_CONFIG_PATH = _WORKTREE / "config" / "protected_paths.yaml"
_PYTHON = sys.executable  # Same interpreter running tests

# ---------------------------------------------------------------------------
# Import the script as a module for unit-level tests
# ---------------------------------------------------------------------------

_spec = importlib.util.spec_from_file_location("pretool_check", _SCRIPT_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_load_critical_patterns = _mod._load_critical_patterns
_matches = _mod._matches
_main = _mod.main
_FALLBACK_CRITICAL = _mod._FALLBACK_CRITICAL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_subprocess(stdin_text: str) -> subprocess.CompletedProcess:
    """Run pretool_check.py as a real subprocess, piping stdin_text."""
    return subprocess.run(
        [_PYTHON, str(_SCRIPT_PATH)],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _run_subprocess_json(file_path: str) -> subprocess.CompletedProcess:
    """Run pretool_check.py as a subprocess with a standard JSON payload."""
    payload = json.dumps({"file_path": file_path})
    return _run_subprocess(payload)


def _run_main_mock(file_path: str) -> int:
    """Run main() with mocked stdin containing the given file_path."""
    stdin_data = json.dumps({"file_path": file_path})
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.read.return_value = stdin_data
        old_stdin = _mod.sys.stdin
        _mod.sys.stdin = mock_stdin
        try:
            return _main()
        finally:
            _mod.sys.stdin = old_stdin


# ===================================================================
# SECTION 1: Subprocess integration tests (how CC actually invokes it)
# ===================================================================


class TestSubprocessCriticalBlocked:
    """CRITICAL paths must be blocked with exit code 2."""

    def test_secrets_env(self):
        result = _run_subprocess_json("usr/secrets.env")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "secrets.env" in result.stderr

    def test_dotenv(self):
        result = _run_subprocess_json(".env")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_claude_settings_json(self):
        result = _run_subprocess_json(".claude/settings.json")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_autonomy_config(self):
        result = _run_subprocess_json("config/autonomy.yaml")
        assert result.returncode == 2

    def test_protected_paths_yaml_itself(self):
        result = _run_subprocess_json("config/protected_paths.yaml")
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_autonomy_protection_module(self):
        result = _run_subprocess_json("src/genesis/autonomy/protection.py")
        assert result.returncode == 2

    def test_bridge_systemd_unit(self):
        result = _run_subprocess_json("config/genesis-bridge.service")
        assert result.returncode == 2

    def test_watchdog_service(self):
        result = _run_subprocess_json("config/genesis-watchdog.service")
        assert result.returncode == 2

    def test_watchdog_timer(self):
        result = _run_subprocess_json("config/genesis-watchdog.timer")
        assert result.returncode == 2

    def test_any_service_file(self):
        """Any *.service file should be blocked."""
        result = _run_subprocess_json("config/custom-thing.service")
        assert result.returncode == 2

    def test_any_timer_file(self):
        """Any *.timer file should be blocked."""
        result = _run_subprocess_json("anything.timer")
        assert result.returncode == 2

    def test_netplan_config(self):
        result = _run_subprocess_json("/etc/netplan/01-config.yaml")
        assert result.returncode == 2

    def test_iptables_rules(self):
        result = _run_subprocess_json("/etc/iptables/rules.v4")
        assert result.returncode == 2


class TestSubprocessNormalAllowed:
    """Normal paths must be allowed with exit code 0."""

    def test_router_module(self):
        result = _run_subprocess_json("src/genesis/router.py")
        assert result.returncode == 0
        assert result.stderr == ""

    def test_test_file(self):
        result = _run_subprocess_json("tests/test_foo.py")
        assert result.returncode == 0

    def test_docs_file(self):
        result = _run_subprocess_json("docs/something.md")
        assert result.returncode == 0

    def test_src_genesis_util(self):
        result = _run_subprocess_json("src/genesis/util/tasks.py")
        assert result.returncode == 0

    def test_python_init(self):
        result = _run_subprocess_json("src/genesis/__init__.py")
        assert result.returncode == 0

    def test_conftest(self):
        result = _run_subprocess_json("tests/conftest.py")
        assert result.returncode == 0

    def test_claude_md(self):
        result = _run_subprocess_json("CLAUDE.md")
        assert result.returncode == 0

    def test_pyproject_toml(self):
        result = _run_subprocess_json("pyproject.toml")
        assert result.returncode == 0


class TestSubprocessEdgeCases:
    """Edge cases tested as subprocess."""

    def test_json_parse_failure_allows(self):
        """Malformed stdin -> fail-open (exit 0)."""
        result = _run_subprocess("not json at all {{{{")
        assert result.returncode == 0
        assert "WARNING" in result.stderr
        assert "parse failed" in result.stderr

    def test_empty_stdin_allows(self):
        """Empty string stdin -> fail-open (exit 0)."""
        result = _run_subprocess("")
        assert result.returncode == 0

    def test_empty_file_path_allows(self):
        """Empty file_path in JSON -> exit 0."""
        result = _run_subprocess_json("")
        assert result.returncode == 0

    def test_missing_file_path_key_allows(self):
        """JSON without file_path key -> exit 0."""
        payload = json.dumps({"command": "ls -la"})
        result = _run_subprocess(payload)
        assert result.returncode == 0

    def test_null_file_path_allows(self):
        """null file_path in JSON -> exit 0."""
        payload = json.dumps({"file_path": None})
        result = _run_subprocess(payload)
        assert result.returncode == 0

    def test_stderr_contains_pattern_on_block(self):
        """Blocked paths report the matching pattern in stderr."""
        result = _run_subprocess_json("config/autonomy.yaml")
        assert result.returncode == 2
        assert "CRITICAL protected path" in result.stderr
        assert "relay/chat channel" in result.stderr


# ===================================================================
# SECTION 2: Unit tests for _matches()
# ===================================================================


class TestMatchesFunction:
    """Direct tests of the _matches() function."""

    def test_exact_match(self):
        assert _matches(".env", [".env"]) == ".env"

    def test_no_match(self):
        assert _matches("src/genesis/foo.py", ["*.service"]) is None

    def test_wildcard_prefix(self):
        """*/secrets.env matches any prefix before secrets.env."""
        assert _matches("usr/secrets.env", ["*/secrets.env"]) is not None
        assert _matches("deep/nested/secrets.env", ["*/secrets.env"]) is not None

    def test_wildcard_suffix(self):
        """*.service matches any file ending in .service."""
        assert _matches("foo.service", ["*.service"]) is not None
        assert _matches("config/genesis-bridge.service", ["*.service"]) is not None

    def test_glob_star_shallow(self):
        """src/genesis/channels/** matches files in that directory."""
        patterns = ["src/genesis/channels/**"]
        assert _matches("src/genesis/channels/foo.py", patterns) is not None

    def test_glob_star_deep_nesting(self):
        """src/genesis/channels/** matches deeply nested files."""
        patterns = ["src/genesis/channels/**"]
        assert _matches(
            "src/genesis/channels/telegram/handlers.py", patterns
        ) is not None
        assert _matches(
            "src/genesis/channels/a/b/c/d.py", patterns
        ) is not None

    def test_glob_star_no_match_sibling(self):
        """src/genesis/channels/** does NOT match src/genesis/runtime.py."""
        patterns = ["src/genesis/channels/**"]
        assert _matches("src/genesis/runtime.py", patterns) is None

    def test_glob_star_no_match_partial_prefix(self):
        """src/genesis/channels/** does NOT match src/genesis/channels_extra/foo.py."""
        patterns = ["src/genesis/channels/**"]
        # This depends on prefix split behavior — "src/genesis/channels/"
        # is the prefix, so "channels_extra" won't match because the prefix
        # check uses startswith on the split before **
        assert _matches("src/genesis/channels_extra/foo.py", patterns) is None

    def test_recursive_glob_etc_netplan(self):
        """/etc/netplan/** matches any file under /etc/netplan/."""
        patterns = ["/etc/netplan/**"]
        assert _matches("/etc/netplan/01-config.yaml", patterns) is not None
        assert _matches("/etc/netplan/subdir/file.txt", patterns) is not None

    def test_recursive_glob_etc_iptables(self):
        patterns = ["/etc/iptables/**"]
        assert _matches("/etc/iptables/rules.v4", patterns) is not None

    def test_backslash_normalization(self):
        r"""Backslashes in path get normalized to forward slashes."""
        patterns = ["src/genesis/channels/**"]
        assert _matches(
            "src\\genesis\\channels\\telegram\\handler.py", patterns
        ) is not None

    def test_backslash_normalization_exact(self):
        r"""Backslash in exact match."""
        patterns = [".claude/settings.json"]
        assert _matches(".claude\\settings.json", patterns) is not None

    def test_multiple_patterns_first_wins(self):
        """Returns the first matching pattern."""
        patterns = ["*.service", "config/*"]
        match = _matches("config/foo.service", patterns)
        assert match == "*.service"

    def test_empty_patterns_no_match(self):
        assert _matches("anything.py", []) is None

    def test_empty_path_no_match(self):
        assert _matches("", ["*.service"]) is None

    def test_watchdog_glob_pattern(self):
        """config/genesis-watchdog.* matches .service, .timer, etc."""
        patterns = ["config/genesis-watchdog.*"]
        assert _matches("config/genesis-watchdog.service", patterns) is not None
        assert _matches("config/genesis-watchdog.timer", patterns) is not None
        assert _matches("config/genesis-watchdog.conf", patterns) is not None


# ===================================================================
# SECTION 3: Unit tests for _load_critical_patterns()
# ===================================================================


class TestLoadCriticalPatterns:
    """Tests for config loading and fallback."""

    def test_loads_from_real_config(self):
        """When config exists, loads patterns from it."""
        patterns = _load_critical_patterns()
        assert len(patterns) > 0
        # Check some expected patterns from the real config
        assert "src/genesis/channels/**" in patterns
        assert "config/autonomy.yaml" in patterns
        assert ".claude/settings.json" in patterns
        assert "*/secrets.env" in patterns

    def test_fallback_on_missing_config(self, tmp_path):
        """When config file doesn't exist, returns fallback list."""
        with patch.object(_mod, "_CONFIG_PATH", tmp_path / "nonexistent.yaml"):
            patterns = _load_critical_patterns()
        assert patterns == list(_FALLBACK_CRITICAL)

    def test_fallback_on_corrupt_yaml(self, tmp_path):
        """When config is invalid YAML, returns fallback list."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(": : : [[[not yaml at all")
        with patch.object(_mod, "_CONFIG_PATH", bad_yaml):
            patterns = _load_critical_patterns()
        assert patterns == list(_FALLBACK_CRITICAL)

    def test_fallback_on_empty_config(self, tmp_path):
        """When config is valid YAML but has no 'critical' key, returns empty."""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("sensitive:\n  - pattern: foo\n    reason: bar\n")
        with patch.object(_mod, "_CONFIG_PATH", empty_yaml):
            patterns = _load_critical_patterns()
        # No 'critical' key means data.get("critical", []) returns []
        assert patterns == []

    def test_empty_yaml_raises_attribute_error(self, tmp_path):
        """Empty YAML file (safe_load returns None) → unhandled AttributeError.

        This is a known gap in the script: yaml.safe_load("") returns None,
        and None.get("critical", []) raises AttributeError which is NOT caught
        by the (OSError, yaml.YAMLError) handler. The script crashes rather
        than falling back gracefully. We test the actual behavior here.
        """
        none_yaml = tmp_path / "none.yaml"
        none_yaml.write_text("")
        with (
            patch.object(_mod, "_CONFIG_PATH", none_yaml),
            pytest.raises(AttributeError, match="'NoneType'.*'get'"),
        ):
            _load_critical_patterns()

    def test_custom_config(self, tmp_path):
        """Verify loading from a custom config with known patterns."""
        custom = tmp_path / "custom.yaml"
        custom.write_text(textwrap.dedent("""\
            critical:
              - pattern: "my/custom/path.py"
                reason: "test"
              - pattern: "other/*.txt"
                reason: "test2"
        """))
        with patch.object(_mod, "_CONFIG_PATH", custom):
            patterns = _load_critical_patterns()
        assert patterns == ["my/custom/path.py", "other/*.txt"]

    def test_fallback_critical_contents(self):
        """Verify the hardcoded fallback list protects the most dangerous paths."""
        assert "*/secrets.env" in _FALLBACK_CRITICAL
        assert ".claude/settings.json" in _FALLBACK_CRITICAL
        assert "src/genesis/autonomy/protection.py" in _FALLBACK_CRITICAL
        assert "config/protected_paths.yaml" in _FALLBACK_CRITICAL

    def test_fallback_returns_copy_not_reference(self, tmp_path):
        """Fallback list is a copy, not a reference to the module constant."""
        with patch.object(_mod, "_CONFIG_PATH", tmp_path / "nonexistent.yaml"):
            patterns = _load_critical_patterns()
        patterns.append("MUTATED")
        assert "MUTATED" not in _FALLBACK_CRITICAL


# ===================================================================
# SECTION 4: Unit tests for main() via mock stdin
# ===================================================================


class TestMainMocked:
    """Tests main() with mocked stdin for faster execution."""

    def test_critical_blocked(self):
        assert _run_main_mock("usr/secrets.env") == 2

    def test_normal_allowed(self):
        assert _run_main_mock("src/genesis/router.py") == 0

    def test_empty_path_allowed(self):
        assert _run_main_mock("") == 0

    def test_json_without_file_path(self):
        stdin_data = json.dumps({"something_else": "value"})
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = stdin_data
            old_stdin = _mod.sys.stdin
            _mod.sys.stdin = mock_stdin
            try:
                result = _main()
            finally:
                _mod.sys.stdin = old_stdin
        assert result == 0

    def test_json_parse_failure(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "<<<not json>>>"
            old_stdin = _mod.sys.stdin
            _mod.sys.stdin = mock_stdin
            try:
                result = _main()
            finally:
                _mod.sys.stdin = old_stdin
        assert result == 0


# ===================================================================
# SECTION 5: Fallback integration (subprocess with missing config)
# ===================================================================


class TestSubprocessFallback:
    """Test that hardcoded fallback patterns work when config is missing.

    We override _CONFIG_PATH by creating a wrapper script that patches the
    module-level constant before running main().
    """

    @pytest.fixture
    def wrapper_script(self, tmp_path):
        """Create a wrapper script that runs pretool_check with a bad config path."""
        wrapper = tmp_path / "wrapper.py"
        wrapper.write_text(textwrap.dedent(f"""\
            import sys
            sys.path.insert(0, "{_WORKTREE / 'scripts'}")
            import importlib.util
            from pathlib import Path

            spec = importlib.util.spec_from_file_location(
                "pretool_check", "{_SCRIPT_PATH}"
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Override config path to a nonexistent file
            mod._CONFIG_PATH = Path("{tmp_path / 'nonexistent.yaml'}")

            sys.exit(mod.main())
        """))
        return wrapper

    def test_fallback_blocks_secrets_env(self, wrapper_script):
        payload = json.dumps({"file_path": "usr/secrets.env"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_fallback_blocks_settings_json(self, wrapper_script):
        payload = json.dumps({"file_path": ".claude/settings.json"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2

    def test_fallback_blocks_protection_module(self, wrapper_script):
        payload = json.dumps({"file_path": "src/genesis/autonomy/protection.py"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2

    def test_fallback_blocks_protected_paths_yaml(self, wrapper_script):
        payload = json.dumps({"file_path": "config/protected_paths.yaml"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 2

    def test_fallback_allows_normal_path(self, wrapper_script):
        payload = json.dumps({"file_path": "src/genesis/router.py"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0

    def test_fallback_emits_warning_on_stderr(self, wrapper_script):
        """When config is missing, stderr should contain a warning."""
        payload = json.dumps({"file_path": "usr/secrets.env"})
        result = subprocess.run(
            [_PYTHON, str(wrapper_script)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "WARNING" in result.stderr
        assert "fallback" in result.stderr.lower()


# ===================================================================
# SECTION 6: Path normalization tests
# ===================================================================


class TestPathNormalization:
    """Backslash → forward slash conversion and edge cases."""

    def test_windows_style_backslash(self):
        """Windows-style paths with backslashes are normalized."""
        patterns = [".claude/settings.json"]
        assert _matches(".claude\\settings.json", patterns) is not None

    def test_mixed_slashes(self):
        patterns = ["src/genesis/channels/**"]
        assert _matches("src\\genesis/channels\\foo.py", patterns) is not None

    def test_double_backslash(self):
        patterns = [".env"]
        assert _matches(".env", patterns) is not None

    def test_forward_slash_unchanged(self):
        """Forward slashes pass through unchanged."""
        patterns = ["src/genesis/channels/**"]
        assert _matches("src/genesis/channels/foo.py", patterns) is not None


# ===================================================================
# SECTION 7: Config pattern completeness
# ===================================================================


class TestConfigCompleteness:
    """Verify all config patterns from protected_paths.yaml are loaded."""

    def test_all_critical_patterns_loaded(self):
        """Every critical pattern from the config should be in the loaded list."""
        patterns = _load_critical_patterns()
        expected = [
            "src/genesis/channels/**",
            "config/genesis-bridge.service",
            "config/genesis-watchdog.*",
            "src/genesis/autonomy/protection.py",
            "config/protected_paths.yaml",
            "config/autonomy.yaml",
            "*/secrets.env",
            ".env",
            "*.service",
            "*.timer",
            "/etc/netplan/**",
            "/etc/iptables/**",
            ".claude/settings.json",
        ]
        for pat in expected:
            assert pat in patterns, f"Expected pattern {pat!r} not in loaded patterns"

    def test_sensitive_patterns_not_in_critical(self):
        """Sensitive-level patterns should NOT appear in the critical list."""
        patterns = _load_critical_patterns()
        sensitive_examples = [
            "src/genesis/runtime.py",
            "src/genesis/db/schema/_tables.py",
            "src/genesis/identity/*.md",
            "config/model_routing.yaml",
            "config/resilience.yaml",
        ]
        for pat in sensitive_examples:
            assert pat not in patterns, (
                f"Sensitive pattern {pat!r} should not be in critical list"
            )


# ===================================================================
# SECTION 8: Regression tests for specific boundary cases
# ===================================================================


class TestBoundaryRegressions:
    """Boundary cases that could be easy to break during refactoring."""

    def test_channels_base_dir_without_trailing(self):
        """src/genesis/channels (no trailing /) or file extension — not a file."""
        # The pattern is "src/genesis/channels/**" — the ** prefix split gives
        # "src/genesis/channels/". A bare "src/genesis/channels" does NOT
        # start with "src/genesis/channels/" so it should not match.
        patterns = ["src/genesis/channels/**"]
        # fnmatch won't match because there's no ** expansion to match empty,
        # and the startswith check looks for the prefix "src/genesis/channels/"
        assert _matches("src/genesis/channels", patterns) is None

    def test_service_extension_in_nested_path(self):
        """*.service matches even deeply nested .service files.

        Python's fnmatch treats * as matching everything INCLUDING /
        on POSIX systems (unlike shell globbing). This means *.service
        blocks .service files at any depth, which is the desired behavior
        for protecting systemd units.
        """
        result = _matches("config/sub/thing.service", ["*.service"])
        assert result is not None

    def test_service_at_root_level(self):
        """*.service matches root-level .service files."""
        assert _matches("foo.service", ["*.service"]) is not None

    def test_dotenv_only_exact(self):
        """.env pattern should match exactly '.env', not 'something.env'."""
        assert _matches(".env", [".env"]) is not None
        # "foo.env" should NOT match ".env" (fnmatch is exact)
        assert _matches("foo.env", [".env"]) is None

    def test_secrets_env_wildcard(self):
        """*/secrets.env matches any prefix depth.

        Python's fnmatch * matches everything including / on POSIX,
        so */secrets.env matches secrets.env at any nesting depth.
        """
        assert _matches("usr/secrets.env", ["*/secrets.env"]) is not None
        # fnmatch * DOES cross / on POSIX — matches multi-segment paths
        result = _matches("a/b/secrets.env", ["*/secrets.env"])
        assert result is not None

    def test_json_with_extra_fields_still_works(self):
        """JSON payload with extra fields beyond file_path still works."""
        result = _run_subprocess(
            json.dumps({
                "file_path": "config/autonomy.yaml",
                "old_string": "foo",
                "new_string": "bar",
            })
        )
        assert result.returncode == 2

    def test_json_array_fails_open(self):
        """JSON array instead of object — .get() would fail → fail-open."""
        # json.loads('[]') succeeds but [].get() → AttributeError
        # This is NOT caught by JSONDecodeError handler.
        # It would be an unhandled exception → non-zero exit.
        # Let's test actual behavior.
        result = _run_subprocess(json.dumps(["not", "an", "object"]))
        # list doesn't have .get() → AttributeError → unhandled → exit 1
        assert result.returncode != 0

    def test_numeric_file_path_fails_gracefully(self):
        """Numeric file_path — str operations should still work or fail-open."""
        # data.get("file_path", "") returns an int → `not file_path` is False
        # for nonzero int → _matches gets an int → .replace() fails
        result = _run_subprocess(json.dumps({"file_path": 12345}))
        # int doesn't have .replace() → AttributeError → unhandled → exit 1
        assert result.returncode != 0
