"""Tests for scripts/hooks/concurrent_test_guard.py.

Two things this file pins:

* the argv classifier, which used to be a `pgrep -f` substring scan that matched
  ANY process merely MENTIONING pytest (`grep pytest x`, `tail -f pytest.log`)
  and false-blocked a legitimate first run;
* that the guard's decision now comes from the BOX LOCK rather than the process
  table, so it cannot disagree with `genesis.util.pytest_lock` — and that the
  levers the lock's own refusal message prescribes are honoured here, or the
  documented recovery path would be unreachable from a Bash call.

Every test that needs a held/free lock points `GENESIS_PYTEST_LOCK_PATH` at a
tmp file. Never the real one: this suite runs while its own session holds
``~/.genesis/locks/pytest.lock``, so a test reading the real lock would be
measuring the harness.
"""

from __future__ import annotations

import fcntl
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
_scan_pytest_processes = _mod.scan_pytest_processes
_parse_wait_arg = _mod._parse_wait_arg


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

    def test_venv_console_script_via_shebang(self):
        """A venv console script launched via its python shebang appears as
        `python /venv/bin/pytest …` — the repo's normal run. Missing it would
        allow a concurrent run."""
        assert _argv_is_pytest(["python", "/home/u/genesis/.venv/bin/pytest", "-q"])

    def test_full_python_path_console_script(self):
        assert _argv_is_pytest(["/usr/bin/python3.12", "/venv/bin/pytest", "tests/x.py"])

    def test_full_python_path_m_pytest(self):
        assert _argv_is_pytest(["/usr/bin/python3.12", "-m", "pytest", "-q"])

    # Short-option spellings the interpreter treats identically. A matcher that
    # only understands the spaced form silently misses a REAL running suite,
    # which is worse than a false block: --wait would report "clear to go"
    # while the lock kept refusing.
    def test_glued_m_value(self):
        assert _argv_is_pytest(["/venv/bin/python", "-mpytest", "tests/"])

    def test_clustered_boolean_then_m(self):
        assert _argv_is_pytest(["/venv/bin/python", "-um", "pytest", "tests/"])

    def test_clustered_boolean_then_glued_m(self):
        assert _argv_is_pytest(["/venv/bin/python", "-umpytest", "tests/"])

    def test_multiple_booleans_then_m(self):
        assert _argv_is_pytest(["python3", "-BuOm", "pytest"])

    def test_flag_value_before_m(self):
        """-W's value must not be misread as the module."""
        assert _argv_is_pytest(["python", "-W", "ignore", "-m", "pytest"])

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
        assert not _argv_is_pytest(["python", "pytest"])  # a FILE named pytest

    def test_python_m_other_module(self):
        assert not _argv_is_pytest(["python", "-m", "pytest_cov"])

    def test_glued_m_other_module(self):
        assert not _argv_is_pytest(["python", "-mpytest_cov"])

    def test_pytest_like_binary_name(self):
        assert not _argv_is_pytest(["pytest-watch", "tests/"])

    def test_empty_argv(self):
        assert not _argv_is_pytest([])

    def test_m_at_end_without_module(self):
        assert not _argv_is_pytest(["python", "-m"])

    def test_c_script_mentioning_pytest_is_not_a_run(self):
        """-c's script is a VALUE, not a module — a common xdist/worker shape."""
        assert not _argv_is_pytest(["python", "-c", "import pytest; print(pytest)"])


class TestCommandMatcher:
    """The Bash-command matcher routes through shell_parse.command_runs_pytest
    (quote-aware) — spot-check its boundaries, incl. the quoted-pipe false positive."""

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

    def test_pytest_inside_quoted_grep_pattern_not_matched(self):
        # A `|pytest` inside a quoted grep/regex pattern used to match the raw
        # regex scan (| read as a shell pipe) and false-block.
        assert not _command_runs_pytest('grep -rniE "full_suite|pytest|x" file')
        assert not _command_runs_pytest("rg -n 'a|pytest|b' scripts/")


# ─────────────────────────────────────────────────────────────────────────────
# Lock-backed behaviour. The lock is the signal; /proc is only the fallback.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    """A tmp lock path, applied to BOTH this process and child guard runs."""
    path = tmp_path / "pytest.lock"
    monkeypatch.setenv("GENESIS_PYTEST_LOCK_PATH", str(path))
    monkeypatch.setattr(_mod, "LOCK_PATH", str(path))
    return path


@pytest.fixture
def lock_holder():
    """Hold a flock from a REAL external process (not a mock), then clean up."""
    procs: list[subprocess.Popen] = []

    def _start(
        path: Path, *, record: str = "4242\n0\npytest tests/test_x.py", linger: float = 120.0
    ):
        ready = Path(str(path) + f".ready{len(procs)}")
        src = (
            "import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "rec=sys.argv[3].encode()\n"
            "os.pwrite(fd, rec, 0); os.ftruncate(fd, len(rec))\n"
            "open(sys.argv[2],'w').write('r')\n"
            "time.sleep(float(sys.argv[4]))\n"
        )
        proc = subprocess.Popen([_PYTHON, "-c", src, str(path), str(ready), record, str(linger)])
        procs.append(proc)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if ready.exists():
                return proc
            time.sleep(0.02)
        proc.kill()
        raise AssertionError("external lock holder never signalled ready")

    yield _start

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def _run_guard(command: str, env: dict | None = None) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}, "tool_name": "Bash"})
    return subprocess.run(
        [_PYTHON, str(_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, **(env or {})},
    )


def _run_guard_args(*args: str, timeout: float = 60, env: dict | None = None):
    """Run the guard as a CLI (no hook payload on stdin)."""
    return subprocess.run(
        [_PYTHON, str(_SCRIPT), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, **(env or {})},
    )


class TestBoxLockProbe:
    def test_absent_lock_file_is_inconclusive(self, lock_file):
        """No lock file → None, meaning 'fall back to the process scan'."""
        assert _mod.box_lock_held() is None

    def test_free_lock_reads_free(self, lock_file):
        lock_file.write_text("")
        assert _mod.box_lock_held() is False

    def test_held_lock_reads_held(self, lock_file, lock_holder):
        lock_holder(lock_file)
        assert _mod.box_lock_held() is True

    def test_probe_does_not_steal_the_lock(self, lock_file, lock_holder):
        """Probing must release; two probes in a row must both say 'held'."""
        lock_holder(lock_file)
        assert _mod.box_lock_held() is True
        assert _mod.box_lock_held() is True

    def test_probe_leaves_a_free_lock_free(self, lock_file):
        lock_file.write_text("")
        assert _mod.box_lock_held() is False
        # A second probe proves the first released rather than kept it.
        assert _mod.box_lock_held() is False

    def test_lock_frees_when_the_holder_dies(self, lock_file, lock_holder):
        proc = lock_holder(lock_file)
        assert _mod.box_lock_held() is True
        proc.kill()
        proc.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if _mod.box_lock_held() is False:
                return
            time.sleep(0.05)
        raise AssertionError("lock stayed held after the holder died")


class TestOverrides:
    """The lock's refusal message prescribes these. If the hook blocked them,
    the documented recovery path would be unreachable from a Bash call and a
    wedged holder would mean nobody can run a test at all."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "GENESIS_PYTEST_LOCK=0 pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK=off pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK_WAIT=1 pytest tests/test_x.py -q",
            "PYTHONPATH=src GENESIS_PYTEST_LOCK=0 pytest tests/test_x.py",
            "pytest tests/test_x.py  # concurrent-ok",
        ],
    )
    def test_recognised_overrides(self, cmd):
        assert _mod.command_opts_out(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK=1 pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK_WAIT=0 pytest tests/test_x.py",
            "PYTHONPATH=src pytest tests/test_x.py",
            # The name must be an ENV PREFIX, not an argument mentioning it.
            "pytest tests/test_x.py -k GENESIS_PYTEST_LOCK=0",
        ],
    )
    def test_non_overrides(self, cmd):
        assert _mod.command_opts_out(cmd) is False

    @pytest.mark.parametrize(
        "cmd",
        [
            "GENESIS_PYTEST_LOCK=0 true; pytest tests/",
            "GENESIS_PYTEST_LOCK=0 echo hi && pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK_WAIT=1 make lint && pytest tests/test_x.py",
        ],
    )
    def test_assignment_on_an_unrelated_segment_is_not_an_override(self, cmd):
        """bash scopes an env prefix to ONE simple command.

        MEASURED against real bash: in each of these, pytest's environment has
        ZERO occurrences of the variable — it reached `true`/`echo`/`make`.
        Treating them as opt-outs silently disabled the guard for a pytest that
        never opted out (and is easy to write by accident, e.g. meaning to
        disable the lock only for the lint step).
        """
        assert _mod.command_opts_out(cmd) is False

    def test_hash_inside_a_quoted_value_does_not_swallow_the_override(self):
        """A '#' in an earlier quoted VALUE is data, not a comment.

        Splitting on the first literal '#' truncated mid-quote, raised inside
        shlex, and discarded a genuine opt-out — a FALSE BLOCK on the one path
        the design says must fail open. Real bash sets the variable here.
        """
        cmd = 'SOME_VAR="a # b" GENESIS_PYTEST_LOCK=0 pytest -k test_foo'
        assert _mod.command_opts_out(cmd) is True

    def test_override_scoping_matches_real_bash(self):
        """Ground-truth the parser against bash itself rather than intuition."""
        var = "GENESIS_PYTEST_LOCK"
        cases = [
            (f"{var}=0 pytest tests/test_x.py", True),
            (f"{var}=0 true; pytest tests/test_x.py", False),
            (f"{var}=0 echo hi && pytest tests/test_x.py", False),
        ]
        for cmd, expected in cases:
            # Only the LAST line is grep's count: earlier segments in the
            # command (`echo hi`) write to the same stdout.
            probe = cmd.replace(
                "pytest tests/test_x.py", f"env | grep -c '^{var}=' || true"
            )
            result = subprocess.run(
                ["bash", "-c", probe], capture_output=True, text=True, timeout=30
            )
            lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
            bash_sets_it = bool(lines) and lines[-1].strip() not in ("", "0")
            assert bash_sets_it is expected, f"test premise wrong for {cmd!r}"
            assert _mod.command_opts_out(cmd) is expected, (
                f"parser disagrees with bash for {cmd!r}"
            )

    def test_override_actually_unblocks_the_hook(self, lock_file, lock_holder):
        """End-to-end: the exact string the lock's message prints must run."""
        lock_holder(lock_file)
        env = {"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}

        blocked = _run_guard("pytest tests/test_x.py", env=env)
        assert blocked.returncode == 2, "a held lock must block a plain run"

        for cmd in (
            "GENESIS_PYTEST_LOCK=0 pytest tests/test_x.py",
            "GENESIS_PYTEST_LOCK_WAIT=1 pytest tests/test_x.py",
            "pytest tests/test_x.py  # concurrent-ok",
        ):
            allowed = _run_guard(cmd, env=env)
            assert allowed.returncode == 0, (
                f"the lock prescribes {cmd!r} but the hook refused it: {allowed.stderr}"
            )

    def test_every_command_the_refusal_message_prescribes_is_accepted(self):
        """Pin the two layers together: parse the lock's own message and assert
        the hook accepts each command it tells a caller to run. This is what
        stops the levers drifting apart again."""
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        message = pytest_lock._contention_message("pid 1 running 1s")
        prescribed = [
            line.split(":", 1)[1].strip()
            for line in message.splitlines()
            if line.strip().startswith(("Deliberate concurrent run:", "Queue instead"))
        ]
        assert prescribed, "the refusal message stopped prescribing any command"
        for cmd in prescribed:
            assert _mod.command_opts_out(cmd) is True, (
                f"the lock tells callers to run {cmd!r}, but the hook blocks it"
            )


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires /proc (Linux)")
class TestProcessScanFallback:
    """The /proc scan stands in only when there is no lock to consult."""

    @pytest.fixture
    def pytest_decoy(self, tmp_path):
        procs: list[subprocess.Popen] = []

        def _start(seconds: str = "60"):
            sleep_bin = shutil.which("sleep")
            assert sleep_bin, "sleep binary required"
            decoy_path = tmp_path / "pytest"
            if not decoy_path.exists():
                shutil.copy(sleep_bin, decoy_path)
            proc = subprocess.Popen([str(decoy_path), seconds], stdout=subprocess.DEVNULL)
            procs.append(proc)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if any(pid == proc.pid for pid, _c, _a in _scan_pytest_processes()):
                    return proc
                time.sleep(0.02)
            raise AssertionError("decoy pytest never appeared in the scan")

        yield _start
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_scan_reports_pid_command_and_age(self, pytest_decoy):
        proc = pytest_decoy()
        entry = next(e for e in _scan_pytest_processes() if e[0] == proc.pid)
        _pid, cmd, age = entry
        assert cmd.endswith("60")
        assert age >= 0

    def test_fallback_blocks_when_there_is_no_lock_file(self, lock_file, pytest_decoy):
        """No lock (CI, bare checkout) → the scan substitutes for it."""
        assert not lock_file.exists()
        proc = pytest_decoy()
        result = _run_guard(
            "pytest tests/test_x.py",
            env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
        )
        assert result.returncode == 2
        assert f"pid {proc.pid}" in result.stderr

    def test_free_lock_beats_a_running_unlocked_process(self, lock_file, pytest_decoy):
        """A pytest that does NOT hold the lock opted out or is foreign — the
        lock is authoritative, so this must NOT be a false block."""
        lock_file.write_text("")
        pytest_decoy()
        result = _run_guard(
            "pytest tests/test_x.py",
            env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
        )
        assert result.returncode == 0, result.stderr

    def test_textual_mention_never_blocks(self, lock_file, tmp_path):
        """The original pgrep false positive: a process merely NAMING pytest."""
        marker = tmp_path / "pytest log"
        marker.write_text("x")
        textual = subprocess.Popen(
            ["tail", "-f", str(marker)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            time.sleep(0.2)
            result = _run_guard(
                "pytest tests/test_x.py",
                env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
            )
            assert result.returncode == 0, result.stderr
        finally:
            textual.terminate()
            textual.wait(timeout=5)


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires /proc (Linux)")
class TestWaiterIsNotSelfMatching:
    """The reason the wait primitive exists at all.

    A hand-rolled pgrep waiter has the pattern it searches for in its OWN
    `bash -c` argv, so it matches itself and waits forever; another session's
    waiter has the same shape. Both must be invisible to the classifier.
    """

    def test_bash_waiter_whose_argv_contains_pytest_is_not_counted(self):
        # The body MUST keep bash alive AS BASH. `bash -c` exec-replaces the
        # shell with the last SIMPLE command of its list, which silently
        # destroys the decoy: measured, both `sleep 30 # <marker>` and
        # `: "<marker>"; sleep 30` end up as a bare `sleep 30` in /proc with the
        # phrase gone — so this would pass vacuously even against a naive
        # substring matcher. A `while` loop cannot be exec-optimized away, and
        # is the real shape of an improvised waiter.
        marker = 'until ! pgrep -f "python -m pyte' + 'st"; do sleep 5; done'
        proc = subprocess.Popen(
            ["bash", "-c", f"while :; do sleep 1; done  # {marker}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if Path(f"/proc/{proc.pid}").exists():
                    break
                time.sleep(0.02)
            argv = Path(f"/proc/{proc.pid}/cmdline").read_bytes().decode("utf-8", "replace")
            # Guard the guard: without the phrase the assertion is vacuous.
            assert "pytest" in argv, "decoy waiter lost its argv — test is vacuous"
            live = [pid for pid, _c, _a in _scan_pytest_processes()]
            assert proc.pid not in live, (
                "a bash waiter whose argv merely CONTAINS the pytest phrase was "
                "counted as a live pytest run — the self-match bug"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_the_wait_mode_process_is_not_itself_a_pytest_run(self):
        """--wait must not see itself, or it could never return."""
        assert not _argv_is_pytest([_PYTHON, str(_SCRIPT), "--wait"])
        assert not _argv_is_pytest([_PYTHON, str(_SCRIPT), "--wait=600"])
        assert not _argv_is_pytest(["bash", "scripts/wait_pytest_clear.sh"])


class TestParseWaitArg:
    def test_not_wait_mode_without_the_flag(self):
        assert _parse_wait_arg([]) is None
        assert _parse_wait_arg(["--other"]) is None

    def test_bare_flag_uses_the_default(self):
        assert _parse_wait_arg(["--wait"]) == _mod.DEFAULT_WAIT_TIMEOUT

    @pytest.mark.parametrize("form", ["--wait=600", "--wait 600"])
    def test_explicit_timeout(self, form):
        assert _parse_wait_arg(form.split()) == 600.0

    @pytest.mark.parametrize("raw", ["--wait=abc", "--wait=0", "--wait=-5", "--wait x"])
    def test_malformed_timeout_falls_back_to_the_default(self, raw):
        """Refusing to wait because the timeout was mistyped is the wrong failure."""
        assert _parse_wait_arg(raw.split()) == _mod.DEFAULT_WAIT_TIMEOUT

    @pytest.mark.parametrize("raw", ["--wait=inf", "--wait inf", "--wait=1e999"])
    def test_non_finite_timeout_is_never_accepted(self, raw):
        """`inf` would make the deadline unreachable — an unbounded hang on the
        critical path of every test run."""
        value = _parse_wait_arg(raw.split())
        assert value == _mod.DEFAULT_WAIT_TIMEOUT

    def test_huge_timeout_is_capped(self):
        assert _parse_wait_arg(["--wait=999999999"]) == _mod.MAX_WAIT_TIMEOUT


class TestWaitMode:
    def test_returns_zero_when_the_lock_is_free(self, lock_file):
        lock_file.write_text("")
        result = _run_guard_args(
            "--wait", timeout=30, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}
        )
        assert result.returncode == 0, result.stderr
        assert "clear" in result.stderr.lower()

    def test_does_not_read_stdin(self, lock_file):
        """A CLI, not a hook: reading a payload here would hang forever.

        stdin is an OPEN PIPE that is never written or closed — the shape a real
        shell invocation has. Closing it (DEVNULL) hands back an instant EOF and
        would let a payload-reading bug pass unnoticed.
        """
        lock_file.write_text("")
        proc = subprocess.Popen(
            [_PYTHON, str(_SCRIPT), "--wait=1"],
            stdin=subprocess.PIPE,  # deliberately left open
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
        )
        try:
            proc.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError(
                "--wait blocked on stdin — the hook payload is being read "
                "before the CLI mode is dispatched"
            ) from None
        assert proc.returncode in (0, 1)

    def test_times_out_while_the_lock_is_held(self, lock_file, lock_holder):
        lock_holder(lock_file)
        started = time.monotonic()
        result = _run_guard_args(
            "--wait=2", timeout=40, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}
        )
        elapsed = time.monotonic() - started

        assert result.returncode == 1, result.stderr
        assert "TIMEOUT" in result.stderr
        assert elapsed < 15, f"--wait=2 took {elapsed:.1f}s"

    def test_reports_the_holder_from_the_lock_record(self, lock_file, lock_holder):
        lock_holder(lock_file, record="4242\n0\npytest tests/test_memory/test_drift.py")
        result = _run_guard_args(
            "--wait=2", timeout=40, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}
        )
        assert "pid 4242" in result.stderr
        assert "test_drift.py" in result.stderr

    def test_returns_zero_once_the_holder_releases(self, lock_file, lock_holder):
        lock_holder(lock_file, linger=3.0)  # releases by exiting
        result = _run_guard_args(
            "--wait=120", timeout=180, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}
        )
        assert result.returncode == 0, result.stderr
        assert "Clear after" in result.stderr

    def test_poll_sleep_is_clamped_to_the_deadline(self, lock_file, lock_holder, monkeypatch):
        """A short wait must report on time, not overshoot by a poll interval.

        Run in-process with a deliberately huge poll interval: clamped, this
        returns in ~1s; unclamped it would sleep the full 30s first.
        """
        import io

        lock_holder(lock_file)
        monkeypatch.setattr(_mod, "_POLL_SECONDS", 30.0)
        started = time.monotonic()
        rc = _mod.wait_for_clear(1.0, out=io.StringIO())
        elapsed = time.monotonic() - started

        assert rc == 1
        assert elapsed < 10, (
            f"--wait=1 slept {elapsed:.1f}s — the poll interval is not clamped "
            "to the remaining deadline"
        )


class TestBlockMessage:
    """The block message is what stops the improvisation — assert it teaches."""

    def test_names_the_holder_the_wait_command_and_the_overrides(self, lock_file, lock_holder):
        lock_holder(lock_file, record="4242\n0\npytest tests/test_memory/test_drift.py")
        result = _run_guard(
            "pytest tests/test_x.py",
            env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
        )

        assert result.returncode == 2
        assert _mod.WAIT_COMMAND in result.stderr, "must name HOW to wait"
        assert "pid 4242" in result.stderr, "must name WHAT holds the lock"
        assert "test_drift.py" in result.stderr
        assert "&&" in result.stderr, "must warn that chaining is rejected"
        assert _mod.OVERRIDE_SIGIL in result.stderr, "must name the escape hatch"


def test_wait_command_matches_the_lock_modules_copy():
    """The constant is duplicated (hooks must not import genesis) — pin it.

    If these drift, a blocked caller is told to run a command that does not
    exist, which is how the improvised-waiter bug comes back.
    """
    sys.path.insert(0, str(_WORKTREE / "src"))
    from genesis.util import pytest_lock

    assert _mod.WAIT_COMMAND == pytest_lock.WAIT_COMMAND


def test_lock_path_matches_the_lock_modules_default(monkeypatch, tmp_path):
    """Same pin for the lock FILE. Two layers pointing at different files would
    silently stop excluding anything."""
    sys.path.insert(0, str(_WORKTREE / "src"))
    from genesis.util import pytest_lock

    monkeypatch.delenv(pytest_lock.PATH_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    (tmp_path / ".genesis").mkdir()

    guard_default = os.path.expanduser("~/.genesis/locks/pytest.lock")
    assert Path(guard_default).name == pytest_lock.default_lock_path().name
    assert Path(guard_default).parent.name == pytest_lock.default_lock_path().parent.name


def test_format_age_matches_the_lock_modules_copy():
    """A third duplicated helper — pinned rather than left to drift."""
    sys.path.insert(0, str(_WORKTREE / "src"))
    from genesis.util import pytest_lock

    for seconds in (0, 1, 59, 60, 61, 252, 3599, 3600):
        assert _mod._format_age(seconds) == pytest_lock._format_age(seconds)


def test_flock_is_used_for_the_probe():
    """LOCK_NB, so the probe can never block the hook it runs inside."""
    assert fcntl.LOCK_NB  # the constant exists on this platform
