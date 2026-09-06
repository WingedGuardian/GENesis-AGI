"""Tests for scripts/hooks/full_suite_guard.py — block full-suite / whole-directory
local pytest runs (targeted = a specific file or a -k/-m selector).

The prior guard only warned on path-LESS pytest and treated any arg containing '/'
as targeted, so a whole-directory run like `pytest tests/test_scripts/` (1285 tests)
sailed through silently. This guard blocks bare/dir runs (exit 2) with an explicit
`# full-suite-ok` override.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _WORKTREE / "scripts" / "hooks" / "full_suite_guard.py"
_PYTHON = sys.executable

_spec = importlib.util.spec_from_file_location("full_suite_guard", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


class TestTargetsSpecificTest:
    """Pure-function matrix: does an arg list target a specific test (allow) or is
    it a bare/directory run (block)?"""

    def test_specific_file(self):
        assert _mod._targets_specific_test(["tests/foo/test_bar.py"])

    def test_nodeid(self):
        assert _mod._targets_specific_test(["tests/foo/test_bar.py::test_x"])

    def test_k_selector_separate(self):
        assert _mod._targets_specific_test(["tests/", "-k", "test_bar"])

    def test_k_selector_glued(self):
        assert _mod._targets_specific_test(["-ktest_bar"])

    def test_m_selector(self):
        assert _mod._targets_specific_test(["-m", "slow"])

    def test_bare_no_args(self):
        assert not _mod._targets_specific_test([])

    def test_flags_only(self):
        assert not _mod._targets_specific_test(["-v", "-q"])

    def test_directory_only(self):
        assert not _mod._targets_specific_test(["tests/test_scripts/"])

    def test_value_flag_then_dir(self):
        # -p consumes 'no:cacheprovider'; the remaining positional is a directory
        assert not _mod._targets_specific_test(
            ["tests/test_scripts/", "-q", "-p", "no:cacheprovider"]
        )

    def test_value_flag_then_file(self):
        assert _mod._targets_specific_test(["-p", "no:cacheprovider", "tests/foo.py"])

    def test_dir_with_dotpy_substring_not_targeted(self):
        # a directory that merely CONTAINS '.py' must not read as a file target
        assert not _mod._targets_specific_test(["tests/.pytest_cache/"])
        assert not _mod._targets_specific_test(["tests/foo.python_stuff/"])

    def test_pyargs_is_targeted(self):
        assert _mod._targets_specific_test(["--pyargs", "genesis.tests.test_mod"])

    def test_file_mixed_with_dir_not_targeted(self):
        # a specific file AND a bare directory still runs the WHOLE directory (pytest
        # unions positionals) → the OOM-inducing full run → block.
        assert not _mod._targets_specific_test(["tests/foo.py", "tests/"])
        assert not _mod._targets_specific_test(["tests/", "tests/foo.py"])

    def test_value_flag_path_value_then_file_allowed(self):
        # A path/glob-valued flag now in _VALUE_FLAGS (--basetemp/--confcutdir) consumes
        # its value, so the value is NOT mistaken for a directory positional and does not
        # wrongly block a run that names a real .py file. (The friction bug this PR fixes.)
        assert _mod._targets_specific_test(["tests/foo.py", "--basetemp", "/wt/.pytmp"])
        assert _mod._targets_specific_test(["--confcutdir", "/wt", "tests/foo.py"])

    def test_value_flag_dotpy_value_alone_still_blocks(self):
        # _VALUE_FLAGS earns its keep on the fail-OPEN direction too: a value ENDING in
        # .py (`-p foo.py`, `--doctest-glob '*.py'`) is the flag's value, not a test
        # target, so a bare run must still BLOCK — the value must not fake a `.py` file.
        assert not _mod._targets_specific_test(["-p", "foo.py"])
        assert not _mod._targets_specific_test(["--doctest-glob", "*.py"])

    def test_selector_with_dir_still_targeted(self):
        # a -k/-m selector narrows the run even when a directory is present
        assert _mod._targets_specific_test(["tests/", "-k", "test_bar"])


def _run_guard(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "tool_name": "Bash"})
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)], input=payload, capture_output=True, text=True, timeout=15
    )


class TestEndToEnd:
    # --- blocked: bare / whole-directory runs ---
    def test_bare_pytest_blocks(self):
        r = _run_guard("pytest -v")
        assert r.returncode == 2
        assert "BLOCKED" in r.stderr

    def test_whole_suite_root_blocks(self):
        assert _run_guard("pytest tests/").returncode == 2

    def test_directory_run_blocks(self):
        # the exact shape of the mistake this hook exists to prevent
        assert (
            _run_guard("python -m pytest tests/test_scripts/ -q -p no:cacheprovider").returncode
            == 2
        )

    def test_chained_full_suite_blocks(self):
        assert _run_guard("ruff check . && pytest -q").returncode == 2

    # --- allowed: targeted runs ---
    def test_specific_file_allowed(self):
        r = _run_guard("pytest tests/test_hooks/test_full_suite_guard.py")
        assert r.returncode == 0, r.stderr

    def test_nodeid_allowed(self):
        assert _run_guard("pytest tests/foo/test_bar.py::test_x -q").returncode == 0

    def test_selector_allowed(self):
        assert _run_guard("pytest tests/ -k test_bar").returncode == 0

    def test_file_after_value_flag_allowed(self):
        assert _run_guard("python -m pytest -p no:cacheprovider tests/foo.py").returncode == 0

    # --- override + non-pytest ---
    def test_override_allows_dir_run(self):
        r = _run_guard("pytest tests/test_scripts/ -q  # full-suite-ok")
        assert r.returncode == 0, r.stderr

    def test_non_pytest_command_ignored(self):
        r = _run_guard("git status")
        assert r.returncode == 0
        assert r.stderr == ""

    def test_pytest_mention_in_quoted_grep_ignored(self):
        # must not fire on a grep whose pattern merely mentions pytest
        r = _run_guard("grep -rniE 'a|pytest|b' scripts/")
        assert r.returncode == 0

    def test_dotpy_substring_dir_blocks(self):
        # a directory containing '.py' as a substring must NOT bypass the block
        assert _run_guard("pytest tests/.pytest_cache/").returncode == 2
        assert _run_guard("python -m pytest tests/foo.python_stuff/").returncode == 2

    def test_pyargs_run_allowed(self):
        r = _run_guard("pytest --pyargs genesis.tests.test_mod")
        assert r.returncode == 0, r.stderr

    def test_file_plus_dir_blocks(self):
        # a file mixed with a whole directory runs the dir (pytest unions positionals)
        # → the OOM full run → must block (would fail-OPEN under `has_selector or has_file`).
        assert _run_guard("pytest tests/foo/test_bar.py tests/").returncode == 2

    def test_doctest_glob_py_value_with_dir_blocks(self):
        # a .py-glob flag value must not fake a file target and let a whole-dir run pass:
        # --doctest-glob consumes '*.py', leaving a bare tests/ directory run → block.
        assert _run_guard("pytest --doctest-glob '*.py' tests/").returncode == 2

    def test_value_flag_path_then_file_allowed_e2e(self):
        # the friction bug end-to-end: a path-valued flag (--basetemp) no longer
        # false-blocks a run that names a specific file.
        r = _run_guard("pytest tests/test_hooks/test_full_suite_guard.py --basetemp /tmp/x")
        assert r.returncode == 0, r.stderr

    def test_redirect_then_file_allowed(self):
        # post-#1455 a redirect (2>&1) is stripped from argv, so a named file still allows.
        assert _run_guard("pytest tests/foo/test_bar.py 2>&1").returncode == 0

    def test_redirect_target_py_no_file_still_blocks(self):
        # post-#1455 the redirect TARGET (errors.py) is stripped from argv, so this is a
        # bare pytest → BLOCK (no phantom .py file target).
        assert _run_guard("pytest 2> errors.py").returncode == 2


class TestUvCarrierBypasses:
    """Four reported shapes, all measured fail-OPEN before this change.

    The resolver models uv's option grammar to find the carried command, and that
    grammar is an OPEN set: every missing entry is the next round's finding, which
    is how this PR reached four. Two of them are closed by encoding CLOSED-set
    facts (`uv tool run` is a literal token pair; `pkg@version` is a documented
    spelling), one by removing a wrongly-listed boolean flag, and the residual —
    an unknown value-taking flag before `run` — by refusing to let an unresolved
    carrier read as clean.

    Recorded per-shape rather than as one loop so a regression names the spelling
    that broke.
    """

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest",
            "uv run pytest",
            "uv run --isolated pytest",  # --isolated is BOOLEAN in uv
            "uv --color always run pytest",  # unlisted value flag BEFORE run
            "uv --cache-dir /tmp/c run pytest",  # ditto, different flag
            "uv tool run pytest",  # documented uvx alias
            "uvx pytest@8.3.5",  # documented versioned name
            "poetry run pytest",
            "uv run pytest tests/",  # whole-directory run
        ],
    )
    def test_a_full_suite_run_is_blocked_through_every_carrier_spelling(self, cmd):
        assert _run_guard(cmd).returncode == 2, f"fail-OPEN: {cmd!r} was allowed"

    @pytest.mark.parametrize(
        "cmd",
        [
            "uv run pytest tests/foo.py",
            # Targeted THROUGH an unresolved carrier — the fail-closed leg must
            # still read the pytest args rather than blanket-blocking, or it
            # impedes ordinary work behind an exotic flag.
            "uv --color always run pytest tests/foo.py",
            "uvx pytest@8.3.5 tests/foo.py",
            "uv run pytest -k mytest",
            # Not a pytest run at all — a carrier doing something else must not
            # be caught by the carrier check.
            "uv pip install requests",
            "uv run ruff check .",
            # A mere textual MENTION is not an invocation.
            "echo pytest",
            "git commit -m 'run pytest later'",
        ],
    )
    def test_a_targeted_or_unrelated_command_is_not_over_blocked(self, cmd):
        assert _run_guard(cmd).returncode == 0, f"OVER-BLOCK: {cmd!r} was refused"

    def test_the_carrier_check_does_not_swallow_a_destructive_subcommand(self):
        """`uv rm -rf /` must still resolve to `uv`, not past it.

        The resolver deliberately gates on the `run` literal so a blanket
        positional-consuming entry cannot skip over `rm` and hide it from the
        destructive gate. Adding `tool run` must not weaken that, so this pins
        the neighbouring safety property the change could plausibly have broken.
        """
        from shell_parse import analyze

        seg = [s for s in analyze("uv rm -rf /") if s.depth == 0][0]
        assert seg.exe == "uv", seg.exe
