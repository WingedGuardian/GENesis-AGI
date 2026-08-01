"""Tests for scripts/hooks/concurrent_test_guard.py — pgrep over-match fix.

The old detection (`pgrep -f "pytest( |$)"`) matched ANY process whose cmdline
merely contained the word — `grep pytest x`, `tail -f pytest.log` — falsely
blocking a legitimate first test run. The rewrite classifies a process as
pytest only when its argv IS a pytest invocation.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _WORKTREE / "scripts" / "hooks" / "concurrent_test_guard.py"
_PYTHON = sys.executable

_spec = importlib.util.spec_from_file_location("concurrent_test_guard", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_argv_is_pytest = _mod._argv_is_pytest
_command_runs_pytest = _mod._command_runs_pytest


class TestArgvClassifier:
    """Pure-function matrix: what IS a pytest process."""

    # Real pytest invocations
    def test_bare_pytest(self):
        assert _argv_is_pytest(["pytest", "tests/"])

    def test_venv_pytest_path(self):
        assert _argv_is_pytest(["/home/u/genesis/.venv/bin/pytest", "-q"])

    def test_python_m_pytest(self):
        assert _argv_is_pytest(["python", "-m", "pytest", "tests/x.py"])

    def test_python3_m_pytest(self):
        assert _argv_is_pytest(["python3", "-m", "pytest"])

    def test_full_python_path_m_pytest(self):
        assert _argv_is_pytest(["/usr/bin/python3.12", "-m", "pytest", "-q"])

    # NOT pytest — the old pgrep false positives
    def test_grep_pytest(self):
        assert not _argv_is_pytest(["grep", "pytest", "somefile"])

    def test_tail_on_pytest_log(self):
        assert not _argv_is_pytest(["tail", "-f", "pytest", "log"])

    def test_editor_on_pytest_ini(self):
        assert not _argv_is_pytest(["vi", "pytest.ini"])

    def test_shell_containing_word(self):
        assert not _argv_is_pytest(["bash", "-c", "echo pytest done"])

    def test_python_without_m(self):
        assert not _argv_is_pytest(["python", "pytest"])  # runs a FILE named pytest? no -m

    def test_python_m_other_module(self):
        assert not _argv_is_pytest(["python", "-m", "pytest_cov"])

    def test_pytest_like_binary_name(self):
        assert not _argv_is_pytest(["pytest-watch", "tests/"])

    def test_empty_argv(self):
        assert not _argv_is_pytest([])

    def test_m_at_end_without_module(self):
        assert not _argv_is_pytest(["python", "-m"])


class TestCommandMatcher:
    """The Bash-command matcher is unchanged — spot-check its boundaries."""

    def test_plain_pytest(self):
        assert _command_runs_pytest("pytest tests/foo.py")

    def test_chained(self):
        assert _command_runs_pytest("ruff check . && pytest -q")

    def test_python_m(self):
        assert _command_runs_pytest("python -m pytest tests/")

    def test_env_prefixed(self):
        assert _command_runs_pytest("PYTHONPATH=src pytest tests/")

    def test_grep_not_matched(self):
        assert not _command_runs_pytest("grep pytest scripts/foo.py")

    def test_cat_ini_not_matched(self):
        assert not _command_runs_pytest("cat pytest.ini")


def _run_guard(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "tool_name": "Bash"})
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
    )


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires /proc (Linux)")
class TestEndToEnd:
    """Subprocess behavior against the real /proc.

    NOTE: this suite itself runs under pytest, but the guard subprocess's
    PARENT is the pytest process and {pid, ppid} are excluded — mirroring the
    real deployment, where the hook's parent is the CC process.
    """

    def test_decoy_textual_process_does_not_block(self, tmp_path):
        """A live process whose cmdline merely CONTAINS 'pytest ' must not
        block (the old pgrep FP). The decoy is `sleep` disguised only in its
        ARGUMENTS — argv[0] is sleep."""
        decoy = subprocess.Popen(["sleep", "5"], stdout=subprocess.DEVNULL)
        try:
            # Give the textual-mention shape via a second harmless decoy:
            # tail reading a file literally named 'pytest <something>'.
            marker = tmp_path / "pytest log"
            marker.write_text("x")
            textual = subprocess.Popen(
                ["tail", "-f", str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            try:
                time.sleep(0.2)
                result = _run_guard("pytest tests/test_hooks/test_run_guard.py -q")
                assert result.returncode == 0, (
                    f"textual 'pytest' mention in another process falsely blocked: {result.stderr}"
                )
            finally:
                textual.terminate()
                textual.wait(timeout=5)
        finally:
            decoy.terminate()
            decoy.wait(timeout=5)

    def test_real_pytest_named_process_blocks(self, tmp_path):
        """A process whose argv[0] basename is `pytest` blocks a new run.

        Hermetic decoy: copy the `sleep` binary to a file named `pytest` and
        run it — its /proc cmdline argv[0] ends in /pytest without running any
        actual tests."""
        sleep_bin = shutil.which("sleep")
        assert sleep_bin, "sleep binary required"
        decoy_path = tmp_path / "pytest"
        shutil.copy(sleep_bin, decoy_path)
        decoy_path.chmod(0o755)
        decoy = subprocess.Popen([str(decoy_path), "10"], stdout=subprocess.DEVNULL)
        try:
            time.sleep(0.2)
            result = _run_guard("pytest tests/test_hooks/test_run_guard.py -q")
            assert result.returncode == 2, "a running pytest process must block a new run"
            assert "already running" in result.stderr
        finally:
            decoy.terminate()
            decoy.wait(timeout=5)

    def test_non_pytest_command_never_scans(self):
        result = _run_guard("git status")
        assert result.returncode == 0
        assert result.stderr == ""

    def test_own_invocation_when_no_pytest_running(self):
        """No pytest-shaped process (other than the excluded test runner
        itself) → allow. If a REAL concurrent pytest is running on the box,
        skip rather than false-fail."""
        # Scan for pytest processes other than our own chain, mirroring the
        # guard's logic, to decide whether this environment can assert.
        me = os.getpid()
        others = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit() or int(entry) == me:
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            argv = [t.decode("utf-8", "replace") for t in raw.split(b"\x00") if t]
            if _argv_is_pytest(argv):
                others.append(int(entry))
        # Our own pytest runner is expected; anything else means a genuinely
        # concurrent suite — the guard SHOULD block then, so don't assert allow.
        if [p for p in others if p != os.getppid()]:
            pytest.skip("another pytest is genuinely running on this box")
        result = _run_guard("pytest tests/test_hooks/test_run_guard.py -q")
        # ppid exclusion covers the test runner (direct parent of the guard).
        assert result.returncode == 0, result.stderr
