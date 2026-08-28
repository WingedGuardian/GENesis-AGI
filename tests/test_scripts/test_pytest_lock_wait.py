"""Tests for scripts/pytest_lock_wait.py — the wait CLI.

This program decides NOTHING about whether a run may proceed; the box lock is
the only authority. What it must get right is: reading that lock honestly,
naming a holder, and never hanging.

The predecessor of this file tested a PreToolUse hook that made its own
allow/deny call from the command line. That second oracle drifted from the lock
five separate times, so it was deleted rather than patched — see the module
docstring. What survives here is the classifier (used only to NAME holders) and
the wait loop.

Every test that needs a held or free lock points ``GENESIS_PYTEST_LOCK_PATH`` at
a tmp file. Never the real one: this suite runs while its own session holds
``~/.genesis/locks/pytest.lock``, so a test reading the real lock would be
measuring the harness.
"""

from __future__ import annotations

import errno
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

_WORKTREE = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _WORKTREE / "scripts" / "pytest_lock_wait.py"
_PYTHON = sys.executable

_spec = importlib.util.spec_from_file_location("pytest_lock_wait", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

argv_is_pytest = _mod.argv_is_pytest
scan_pytest_processes = _mod.scan_pytest_processes
parse_wait_arg = _mod.parse_wait_arg


class TestArgvClassifier:
    """Pure-function matrix: what IS a pytest process."""

    def test_bare_pytest(self):
        assert argv_is_pytest(["pytest", "tests/"])

    def test_venv_pytest_path(self):
        assert argv_is_pytest(["/home/u/genesis/.venv/bin/pytest", "-q"])

    def test_python_m_pytest(self):
        assert argv_is_pytest(["python", "-m", "pytest", "tests/x.py"])

    def test_python3_m_pytest(self):
        assert argv_is_pytest(["python3", "-m", "pytest"])

    def test_venv_console_script_via_shebang(self):
        """The repo's normal run appears as `python /venv/bin/pytest …`."""
        assert argv_is_pytest(["python", "/home/u/genesis/.venv/bin/pytest", "-q"])

    def test_full_python_path_console_script(self):
        assert argv_is_pytest(["/usr/bin/python3.12", "/venv/bin/pytest", "tests/x.py"])

    # Short-option spellings the interpreter treats identically. Missing one
    # leaves a real holder unnamed.
    def test_glued_m_value(self):
        assert argv_is_pytest(["/venv/bin/python", "-mpytest", "tests/"])

    def test_clustered_boolean_then_m(self):
        assert argv_is_pytest(["/venv/bin/python", "-um", "pytest", "tests/"])

    def test_clustered_boolean_then_glued_m(self):
        assert argv_is_pytest(["/venv/bin/python", "-umpytest", "tests/"])

    def test_multiple_booleans_then_m(self):
        assert argv_is_pytest(["python3", "-BuOm", "pytest"])

    def test_flag_value_before_m(self):
        """-W's value must not be misread as the module."""
        assert argv_is_pytest(["python", "-W", "ignore", "-m", "pytest"])

    # NOT pytest — the original pgrep false positives.
    def test_grep_pytest(self):
        assert not argv_is_pytest(["grep", "pytest", "somefile"])

    def test_tail_on_pytest_log(self):
        assert not argv_is_pytest(["tail", "-f", "pytest", "log"])

    def test_editor_on_pytest_ini(self):
        assert not argv_is_pytest(["vi", "pytest.ini"])

    def test_shell_containing_word(self):
        assert not argv_is_pytest(["bash", "-c", "echo pytest done"])

    def test_python_without_m(self):
        assert not argv_is_pytest(["python", "pytest"])  # a FILE named pytest

    def test_python_m_other_module(self):
        assert not argv_is_pytest(["python", "-m", "pytest_cov"])

    def test_glued_m_other_module(self):
        assert not argv_is_pytest(["python", "-mpytest_cov"])

    def test_pytest_like_binary_name(self):
        assert not argv_is_pytest(["pytest-watch", "tests/"])

    def test_empty_argv(self):
        assert not argv_is_pytest([])

    def test_m_at_end_without_module(self):
        assert not argv_is_pytest(["python", "-m"])

    def test_c_script_mentioning_pytest_is_not_a_run(self):
        """-c's script is a VALUE, not a module."""
        assert not argv_is_pytest(["python", "-c", "import pytest; print(pytest)"])


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    """A tmp lock path, applied to BOTH this process and child runs."""
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


def _run_cli(*args: str, timeout: float = 60, env: dict | None = None):
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

    def test_repeated_probes_of_a_held_lock_stay_consistent(
        self, lock_file, lock_holder
    ):
        """The probe must be repeatable, not one-shot."""
        lock_holder(lock_file)
        assert _mod.box_lock_held() is True
        assert _mod.box_lock_held() is True

    def test_repeated_probes_of_a_free_lock_stay_free(self, lock_file):
        """The probe must not WEDGE a free lock by holding what it took.

        Named for the observable contract rather than the mechanism: these
        assertions also hold with the explicit LOCK_UN removed, because closing
        the fd releases the flock anyway. That is fine — the contract is what
        callers depend on — but the earlier name claimed to isolate the unlock,
        which it cannot.
        """
        lock_file.write_text("")
        assert _mod.box_lock_held() is False
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

    @pytest.mark.parametrize(
        "errno_value",
        [errno.EIO, errno.EPERM, errno.ENOLCK, errno.EDEADLK, errno.ENOSYS],
    )
    def test_non_contention_errno_is_inconclusive_not_held(
        self, lock_file, monkeypatch, errno_value
    ):
        """Only EWOULDBLOCK/EAGAIN mean 'held'.

        On an unhealthy or lock-less filesystem, treating any OSError as a
        holder would make the waiter sit out its whole timeout against a lock
        nobody holds — the opposite of this governor's fail-open contract, and
        inconsistent with the acquisition path, which already distinguishes the
        two.
        """
        lock_file.write_text("")

        def _boom(*_args, **_kwargs):
            raise OSError(errno_value, "synthetic")

        monkeypatch.setattr(_mod.fcntl, "flock", _boom)
        assert _mod.box_lock_held() is None

    # Symbolic, not literal: EWOULDBLOCK == EAGAIN == 11 on Linux, while 35 is
    # EDEADLK — a literal list asserted the wrong errno meant "held".
    @pytest.mark.parametrize("errno_value", [errno.EAGAIN, errno.EWOULDBLOCK])
    def test_contention_errno_is_held(self, lock_file, monkeypatch, errno_value):
        lock_file.write_text("")

        def _busy(*_args, **_kwargs):
            raise OSError(errno_value, "synthetic contention")

        monkeypatch.setattr(_mod.fcntl, "flock", _busy)
        assert _mod.box_lock_held() is True

    def test_symlinked_lock_is_inconclusive(self, tmp_path, monkeypatch):
        """O_NOFOLLOW: a symlink there is a misconfiguration, not a holder."""
        real = tmp_path / "real.lock"
        real.write_text("")
        link = tmp_path / "link.lock"
        link.symlink_to(real)
        monkeypatch.setattr(_mod, "LOCK_PATH", str(link))
        assert _mod.box_lock_held() is None


@pytest.mark.skipif(not Path("/proc").is_dir(), reason="requires /proc (Linux)")
class TestWaiterIsNotSelfMatching:
    """The reason this primitive exists at all.

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
        # substring matcher. A `while` loop cannot be exec-optimized away.
        marker = 'until ! pgrep -f "python -m pyte' + 'st"; do sleep 5; done'
        proc = subprocess.Popen(
            ["bash", "-c", f"while :; do sleep 1; done  # {marker}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            # /proc/<pid> exists from the fork, but cmdline stays EMPTY until
            # exec completes — poll the CONTENT, or the guard-the-guard assert
            # below fires on an empty string and the test flakes.
            argv = ""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    argv = Path(f"/proc/{proc.pid}/cmdline").read_bytes().decode(
                        "utf-8", "replace"
                    )
                except OSError:
                    argv = ""
                if "pytest" in argv:
                    break
                time.sleep(0.02)
            # Guard the guard: without the phrase the assertion is vacuous.
            assert "pytest" in argv, "decoy waiter lost its argv — test is vacuous"
            live = [pid for pid, _c, _a in scan_pytest_processes()]
            assert proc.pid not in live, (
                "a bash waiter whose argv merely CONTAINS the pytest phrase was "
                "counted as a live pytest run — the self-match bug"
            )
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_the_wait_process_is_not_itself_a_pytest_run(self):
        """The waiter must not see itself, or it could never return."""
        assert not argv_is_pytest([_PYTHON, str(_SCRIPT), "--wait"])
        assert not argv_is_pytest([_PYTHON, str(_SCRIPT), "--wait=600"])


class TestParseWaitArg:
    def test_absent_flag(self):
        assert parse_wait_arg([]) is None
        assert parse_wait_arg(["--other"]) is None

    def test_bare_flag_uses_the_default(self):
        assert parse_wait_arg(["--wait"]) == _mod.DEFAULT_WAIT_TIMEOUT

    @pytest.mark.parametrize("form", ["--wait=600", "--wait 600"])
    def test_explicit_timeout(self, form):
        assert parse_wait_arg(form.split()) == 600.0

    @pytest.mark.parametrize("raw", ["--wait=abc", "--wait=0", "--wait=-5", "--wait x"])
    def test_malformed_falls_back_to_the_default(self, raw):
        assert parse_wait_arg(raw.split()) == _mod.DEFAULT_WAIT_TIMEOUT

    @pytest.mark.parametrize("raw", ["--wait=inf", "--wait inf", "--wait=1e999"])
    def test_non_finite_is_never_accepted(self, raw):
        """`inf` would make the deadline unreachable — an unbounded hang."""
        assert parse_wait_arg(raw.split()) == _mod.DEFAULT_WAIT_TIMEOUT

    def test_huge_timeout_is_capped(self):
        assert parse_wait_arg(["--wait=999999999"]) == _mod.MAX_WAIT_TIMEOUT


class TestWaitMode:
    def test_returns_zero_when_the_lock_is_free(self, lock_file):
        lock_file.write_text("")
        result = _run_cli("--wait", timeout=30, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)})
        assert result.returncode == 0, result.stderr
        assert "clear" in result.stderr.lower()

    def test_bare_invocation_waits(self, lock_file):
        """No flag == --wait; the script has exactly one job."""
        lock_file.write_text("")
        result = _run_cli(timeout=30, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)})
        assert result.returncode == 0, result.stderr

    def test_does_not_read_stdin(self, lock_file):
        """stdin is an OPEN PIPE, never written or closed — the shape a real
        shell invocation has. DEVNULL would hand back an instant EOF and let a
        payload-reading regression pass unnoticed."""
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
            raise AssertionError("the waiter blocked on stdin") from None
        assert proc.returncode in (0, 1)

    def test_times_out_while_the_lock_is_held(self, lock_file, lock_holder):
        lock_holder(lock_file)
        started = time.monotonic()
        result = _run_cli("--wait=2", timeout=40, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)})
        elapsed = time.monotonic() - started

        assert result.returncode == 1, result.stderr
        assert "TIMEOUT" in result.stderr
        assert elapsed < 15, f"--wait=2 took {elapsed:.1f}s"

    def test_reports_the_holder_from_the_lock_record(self, lock_file, lock_holder):
        lock_holder(lock_file, record="4242\n0\npytest tests/test_memory/test_drift.py")
        result = _run_cli("--wait=2", timeout=40, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)})
        assert "pid 4242" in result.stderr
        assert "test_drift.py" in result.stderr

    def test_returns_zero_once_the_holder_releases(self, lock_file, lock_holder):
        lock_holder(lock_file, linger=3.0)  # releases by exiting
        result = _run_cli(
            "--wait=120", timeout=180, env={"GENESIS_PYTEST_LOCK_PATH": str(lock_file)}
        )
        assert result.returncode == 0, result.stderr
        assert "Clear after" in result.stderr

    def test_poll_sleep_is_clamped_to_the_deadline(self, lock_file, lock_holder, monkeypatch):
        """A short wait must report on time, not overshoot by a poll interval.

        Run in-process with a huge poll interval: clamped this returns in ~1s;
        unclamped it would sleep the full 30s first.
        """
        import io

        lock_holder(lock_file)
        monkeypatch.setattr(_mod, "_POLL_SECONDS", 30.0)
        started = time.monotonic()
        rc = _mod.wait_for_clear(1.0, out=io.StringIO())
        assert rc == 1
        assert time.monotonic() - started < 10, "poll interval is not clamped"

    def test_a_release_at_the_deadline_is_not_reported_as_a_timeout(self, lock_file, monkeypatch):
        """If the holder frees between the last poll and the deadline, the lock
        IS free — saying 'still busy, may be wedged' sends the caller chasing
        nothing."""
        import io

        lock_file.write_text("")
        states = iter([True, True, False])

        def _fake_active():
            try:
                held = next(states)
            except StopIteration:
                held = False
            return held, "  pid 4242"

        monkeypatch.setattr(_mod, "pytest_is_active", _fake_active)
        # The poll interval must EXCEED the timeout so the loop runs exactly
        # once and the third probe is the post-deadline one. With a small
        # interval the loop iterates several times, consumes the False itself,
        # and returns 0 via the loop path — leaving the branch under test
        # unexercised (this assertion passed with the fix reverted).
        monkeypatch.setattr(_mod, "_POLL_SECONDS", 5.0)
        out = io.StringIO()
        rc = _mod.wait_for_clear(0.02, out=out)
        assert rc == 0, out.getvalue()
        assert "TIMEOUT" not in out.getvalue()


class TestPinnedToTheLockModule:
    """The CLI duplicates two things from genesis.util.pytest_lock, because it
    must run without genesis importable. Pin both to real behaviour."""

    def test_the_prescribed_wait_command_points_at_a_real_script(self):
        """Stronger than string equality: assert the command the lock tells a
        caller to run actually EXISTS. A relative path silently broke this the
        moment pytest was launched from a subdirectory."""
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        parts = pytest_lock.WAIT_COMMAND.split()
        script = Path(parts[-1])
        assert script.is_absolute(), f"not absolute: {pytest_lock.WAIT_COMMAND}"
        assert script.is_file(), f"prescribed script does not exist: {script}"
        assert script.name == _SCRIPT.name

    def test_the_prescribed_command_actually_runs(self, lock_file):
        """End-to-end: run exactly what the refusal message tells you to run."""
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        lock_file.write_text("")
        parts = pytest_lock.WAIT_COMMAND.split()
        result = subprocess.run(
            [*parts, "--wait=2"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "GENESIS_PYTEST_LOCK_PATH": str(lock_file)},
        )
        assert result.returncode == 0, result.stderr

    def test_lock_path_default_matches(self, tmp_path, monkeypatch):
        """Pin the DEFAULT paths — the ones that can actually drift.

        Two earlier versions of this test were vacuous. The first compared path
        COMPONENTS of a hand-typed literal that expanduser resolved
        independently of the monkeypatched home, so the two sides were
        different files. The second set the shared PATH_ENV override — which
        BOTH layers read first — so both trivially equalled the override target
        and the duplicated defaults could diverge freely underneath it.

        The defaults are what duplication puts at risk, so measure those: unset
        the override, point HOME at a tmp dir (both `Path.home()` and
        `os.path.expanduser` honour it), and re-import the CLI so its
        module-level constant recomputes.
        """
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        monkeypatch.delenv(pytest_lock.PATH_ENV, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".genesis").mkdir()

        spec = importlib.util.spec_from_file_location("cli_repin", _SCRIPT)
        reloaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reloaded)

        assert Path(reloaded.LOCK_PATH) == pytest_lock.default_lock_path()

    def test_lock_path_override_is_honoured_by_both(self, tmp_path, monkeypatch):
        """The override path, kept as its own case rather than conflated."""
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        target = tmp_path / "pinned.lock"
        monkeypatch.setenv(pytest_lock.PATH_ENV, str(target))

        spec = importlib.util.spec_from_file_location("cli_repin2", _SCRIPT)
        reloaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(reloaded)

        assert Path(reloaded.LOCK_PATH) == pytest_lock.default_lock_path() == target

    def test_format_age_matches(self):
        sys.path.insert(0, str(_WORKTREE / "src"))
        from genesis.util import pytest_lock

        for seconds in (0, 1, 59, 60, 61, 252, 3599, 3600):
            assert _mod._format_age(seconds) == pytest_lock._format_age(seconds)


def test_the_deleted_concurrency_oracle_stays_gone():
    """The second oracle was deleted, not patched — keep it deleted.

    This is a NARROW guard, deliberately, and it is worth saying why. The class
    invariant is "no PreToolUse hook decides pytest CONCURRENCY", and that is not
    statically checkable. Three designs were tried:

      1. name-scoped (this one) — true, quiet, but does not cover the class;
      2. enumerate every PreToolUse/Bash hook against a hand-listed baseline —
         class-scoped, but fired in CI the moment someone added an unrelated
         `$?`-after-a-pipeline advisory;
      3. flag any such hook whose SOURCE mentions pytest — also class-scoped, and
         also wrong: `worktree_cwd_guard` and an inline guard both mention pytest
         in comments and help text while deciding nothing about it.

    A guard that cries wolf gets deleted by the next person to hit it, so the
    class invariant lives where invariants that cannot be tested belong: stated
    in `genesis.util.pytest_lock`'s docstring and in
    `.claude/docs/testing-patterns.md`, both of which name the one hook that
    legitimately refuses pytest on a different axis (`full_suite_guard`: SCOPE,
    not timing). This test only pins the specific regression — that the deleted
    file and its registration do not come back.
    """
    assert not (_WORKTREE / "scripts" / "hooks" / "concurrent_test_guard.py").exists()

    import json

    settings = json.loads((_WORKTREE / ".claude" / "settings.json").read_text())
    registered = json.dumps(settings.get("hooks", {}))
    assert "concurrent_test_guard" not in registered


def test_scan_reports_pid_command_and_age(tmp_path):
    """The scan's one remaining job: naming holders."""
    sleep_bin = shutil.which("sleep")
    assert sleep_bin, "sleep binary required"
    decoy = tmp_path / "pytest"
    shutil.copy(sleep_bin, decoy)
    proc = subprocess.Popen([str(decoy), "30"], stdout=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            match = [e for e in scan_pytest_processes() if e[0] == proc.pid]
            if match:
                _pid, cmd, age = match[0]
                assert cmd.endswith("30")
                assert age >= 0
                return
            time.sleep(0.02)
        raise AssertionError("decoy pytest never appeared in the scan")
    finally:
        proc.kill()
        proc.wait(timeout=5)
