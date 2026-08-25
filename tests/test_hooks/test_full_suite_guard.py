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
        # a specific file AND a bare directory still runs the whole directory → block
        assert not _mod._targets_specific_test(["tests/foo.py", "tests/"])
        assert not _mod._targets_specific_test(["tests/", "tests/foo.py"])

    def test_selector_with_dir_still_targeted(self):
        # a -k/-m selector narrows the run even when a directory is present
        assert _mod._targets_specific_test(["tests/", "-k", "test_bar"])

    # ── redirections are stripped locally so they don't read as directories ──
    def test_bare_redirection_after_file_targeted(self):
        # `pytest foo.py > log` — the `> log` is a redirection, not a dir target
        assert _mod._targets_specific_test(["tests/foo.py", ">", "log"])

    def test_glued_redirection_after_file_targeted(self):
        assert _mod._targets_specific_test(["tests/foo.py", "2>/dev/null"])
        assert _mod._targets_specific_test(["tests/foo.py", ">", "log", "2>&1"])

    def test_redirection_does_not_rescue_whole_dir(self):
        # a redirection must not turn a whole-directory run into a targeted one
        assert not _mod._targets_specific_test(["tests/", ">", "log"])

    # ── value-flag desync guard: a `<`/`>`-leading flag VALUE must NOT be treated
    #    as a redirection (that would let the flag swallow a following directory —
    #    an under-block; #1457 round-2, caught by the architect re-review) ──
    def test_value_flag_angle_value_does_not_swallow_dir(self):
        # `-p '<x>' tests/ tests/foo.py` runs the whole tests/ dir → MUST block
        assert not _mod._targets_specific_test(["-p", "<x>", "tests/", "tests/foo.py"])
        assert not _mod._targets_specific_test(["-o", "<x>", "tests/"])
        assert not _mod._targets_specific_test(["--rootdir", ">x", "tests/", "tests/foo.py"])

    def test_value_flag_angle_value_then_file_targeted(self):
        # only a file after the consumed `<`-value → still a targeted run (allow)
        assert _mod._targets_specific_test(["-p", "<x>", "tests/foo.py"])

    def test_value_flag_normal_value_with_trailing_redirection(self):
        # normal plugin value AND a redirection together still resolve correctly
        assert _mod._targets_specific_test(["-p", "no:cacheprovider", "tests/foo.py", ">", "log"])


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

    def test_targeted_file_with_redirection_allowed(self):
        # F4: a targeted run with output redirection must not be blocked
        assert _run_guard("pytest tests/foo/test_bar.py > /tmp/log").returncode == 0
        assert _run_guard("pytest tests/foo/test_bar.py 2>/dev/null").returncode == 0

    def test_grouped_targeted_with_redirection_allowed(self):
        # control-position resolution + local redir strip: `(pytest file) > log`
        assert _run_guard("(pytest tests/foo/test_bar.py) > /tmp/log").returncode == 0

    def test_whole_dir_with_redirection_still_blocks(self):
        assert _run_guard("pytest tests/ > /tmp/log").returncode == 2

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
        # a file mixed with a whole directory runs the dir → must block
        assert _run_guard("pytest tests/foo/test_bar.py tests/").returncode == 2
