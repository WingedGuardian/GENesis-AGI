"""Tests for the box-wide pytest lock (``genesis.util.pytest_lock``).

Every test points the lock at a ``tmp_path`` file. Never at the real one: this
suite runs *while its own session holds* ``~/.genesis/locks/pytest.lock``, so a
test that contended for the real lock would be testing the harness, not the code.

Contention and auto-release are exercised against a REAL second process holding
a plain ``fcntl.flock`` — not a mock. The holder deliberately does not import
``genesis``, which proves the lock interoperates with any flock holder rather
than only with itself.
"""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis.util import pytest_lock
from genesis.util.pytest_lock import BoxLock, acquire, default_lock_path

# A plain flock holder: take the lock, signal readiness, then idle. No genesis
# import — a black-box peer, exactly like the gauntlet subprocess would be.
_HOLDER_SRC = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(fd, sys.argv[3].encode())
open(sys.argv[2], "w").write("ready")
time.sleep(float(sys.argv[4]))
"""


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    """Poll *predicate* until true or *timeout*. Condition-based, never a bare sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def holder(tmp_path):
    """Spawn a real external process holding the flock; kill it on teardown."""
    procs: list[subprocess.Popen] = []

    def _start(lock_path: Path, *, record: str = "", linger: float = 60.0):
        ready = tmp_path / f"ready-{len(procs)}"
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SRC, str(lock_path), str(ready), record, str(linger)],
        )
        procs.append(proc)
        assert _wait_for(ready.exists), "external flock holder never signalled ready"
        return proc

    yield _start

    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No ambient lock env leaks into a test (the running session sets HELD).

    CAVEAT: this removes HELD from the LIVE process environment for the
    duration of each test, so anything spawning a real pytest in that window
    would contend for the real box lock its own session holds and exit 200.
    Nothing here does — the holder fixture spawns a bare flock, not pytest —
    but a future test that shells out to pytest must set HELD back first.
    """
    for name in (
        pytest_lock.DISABLE_ENV,
        pytest_lock.WAIT_ENV,
        pytest_lock.WAIT_TIMEOUT_ENV,
        pytest_lock.HELD_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


class TestAcquire:
    def test_acquires_when_free(self, tmp_path):
        lock = acquire(lock_path=tmp_path / "pytest.lock")
        try:
            assert lock.blocked is False
            assert lock.acquired is True
        finally:
            lock.release()

    def test_creates_the_locks_leaf_directory(self, tmp_path):
        path = tmp_path / "locks" / "pytest.lock"
        lock = acquire(lock_path=path)
        try:
            assert path.exists()
        finally:
            lock.release()

    def test_blocked_by_a_real_external_holder(self, tmp_path, holder):
        path = tmp_path / "pytest.lock"
        holder(path, record="4242\n0\npytest tests/test_memory/")

        lock = acquire(lock_path=path)
        try:
            assert lock.blocked is True
            assert lock.acquired is False
        finally:
            lock.release()

    def test_release_frees_the_lock_for_the_next_run(self, tmp_path):
        path = tmp_path / "pytest.lock"
        first = acquire(lock_path=path)
        assert first.acquired
        first.release()
        # HELD_ENV must be cleared by release, or the next acquire no-ops as
        # an "inner" run and silently fails to take the lock.
        assert os.environ.get(pytest_lock.HELD_ENV) is None

        second = acquire(lock_path=path)
        try:
            assert second.acquired is True
        finally:
            second.release()

    def test_release_is_idempotent(self, tmp_path):
        lock = acquire(lock_path=tmp_path / "pytest.lock")
        lock.release()
        lock.release()  # must not raise

    def test_lock_frees_when_the_holder_is_SIGKILLed(self, tmp_path, holder):
        """No stale-lock problem: the kernel drops flock on process death."""
        path = tmp_path / "pytest.lock"
        proc = holder(path)
        assert acquire(lock_path=path).blocked is True

        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=5)

        def _free() -> bool:
            probe = acquire(lock_path=path)
            if probe.acquired:
                probe.release()
                return True
            return False

        assert _wait_for(_free, timeout=5.0), "lock stayed held after holder death"


class TestFailOpen:
    """A governor bug must never be able to stop the suite from running."""

    def test_disabled_by_env_even_when_held(self, tmp_path, holder, monkeypatch):
        path = tmp_path / "pytest.lock"
        holder(path)
        monkeypatch.setenv(pytest_lock.DISABLE_ENV, "0")

        lock = acquire(lock_path=path)
        assert lock.blocked is False
        assert lock.acquired is False  # no-op, nothing to release

    def test_inner_pytest_never_contends_with_its_parent(self, tmp_path, holder, monkeypatch):
        path = tmp_path / "pytest.lock"
        holder(path)
        monkeypatch.setenv(pytest_lock.HELD_ENV, "12345")

        assert acquire(lock_path=path).blocked is False

    def test_unusable_lock_directory_fails_open(self, tmp_path):
        # A regular file where the parent directory must be → mkdir raises.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")

        lock = acquire(lock_path=blocker / "pytest.lock")
        assert lock.blocked is False
        assert lock.acquired is False

    def test_unexpected_error_fails_open(self, tmp_path, monkeypatch):
        def _boom(*_args, **_kwargs):
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pytest_lock, "_acquire_inner", _boom)
        assert acquire(lock_path=tmp_path / "pytest.lock").blocked is False

    def test_io_error_is_not_treated_as_contention(self, tmp_path, monkeypatch):
        """EPERM/EIO from flock is a failure, not a busy lock → run anyway.

        The stub replaces the module REFERENCE inside pytest_lock, not
        ``flock`` on the global fcntl module — patching the latter would swap
        it out process-wide for the duration, which is only survivable because
        tests happen to run sequentially.
        """

        class _StubFcntl:
            LOCK_EX = fcntl.LOCK_EX
            LOCK_NB = fcntl.LOCK_NB
            LOCK_UN = fcntl.LOCK_UN

            @staticmethod
            def flock(*_args, **_kwargs):
                raise OSError(5, "I/O error")

        monkeypatch.setattr(pytest_lock, "fcntl", _StubFcntl)
        lock = acquire(lock_path=tmp_path / "pytest.lock")
        assert lock.blocked is False
        assert lock.acquired is False


class TestOffAnInstall:
    def test_no_lock_path_without_a_dot_genesis_dir(self, tmp_path, monkeypatch):
        """CI containers and bare checkouts no-op instead of creating state."""
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        assert default_lock_path() is None
        assert not (tmp_path / ".genesis").exists()  # nothing materialised

    def test_lock_path_under_a_real_install(self, tmp_path, monkeypatch):
        (tmp_path / ".genesis").mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        assert default_lock_path() == tmp_path / ".genesis" / "locks" / "pytest.lock"

    def test_acquire_no_ops_when_there_is_no_install(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        lock = acquire()  # no explicit path → default_lock_path() is None
        assert lock.blocked is False
        assert lock.acquired is False


class TestContentionMessage:
    def test_names_the_holder_and_the_wait_command(self, tmp_path, holder):
        path = tmp_path / "pytest.lock"
        holder(path, record="4242\n0\npytest tests/test_memory/test_drift.py")

        message = acquire(lock_path=path).message
        assert "pid 4242" in message
        assert "pytest tests/test_memory/test_drift.py" in message
        # The whole point of the change: a blocked caller is TOLD how to wait,
        # rather than improvising a `pgrep` loop that matches itself.
        assert pytest_lock.WAIT_COMMAND in message
        # The prescribed command must be ABSOLUTE: the lock governs runs from
        # any working directory, and a repo-relative path resolves wrong the
        # moment pytest is launched from a subdirectory.
        script = pytest_lock.WAIT_COMMAND.split()[-1]
        assert script.startswith("/"), f"relative wait command: {script}"
        assert pytest_lock.DISABLE_ENV in message  # the deliberate-override hatch

    def test_reports_holder_age(self, tmp_path, holder):
        path = tmp_path / "pytest.lock"
        holder(path, record=f"4242\n{time.time() - 252}\npytest tests/x.py")
        assert "running 4m12s" in acquire(lock_path=path).message

    @pytest.mark.parametrize(
        "record",
        [
            "",  # holder died between open and write
            "garbage-not-a-pid",
            "not-a-pid\n0\npytest x",
            "4242",  # torn: pid only, no age or command yet
            "4242\nnot-a-timestamp\npytest x",  # torn: unparseable age
        ],
    )
    def test_partial_or_corrupt_holder_record_still_blocks(
        self, tmp_path, holder, record
    ):
        """A torn/garbage record degrades the DESCRIPTION, never the decision.

        The holder truncates then rewrites, so a contender can read the file
        mid-write. Blocking must not depend on parsing it.
        """
        path = tmp_path / "pytest.lock"
        holder(path, record=record)

        lock = acquire(lock_path=path)
        assert lock.blocked is True
        assert pytest_lock.WAIT_COMMAND in lock.message  # still actionable


class TestWaitMode:
    def test_times_out_and_reports_the_holder(self, tmp_path, holder):
        path = tmp_path / "pytest.lock"
        holder(path, record="4242\n0\npytest tests/x.py")

        started = time.monotonic()
        lock = acquire(lock_path=path, wait=True, timeout=0.5)
        elapsed = time.monotonic() - started

        assert lock.blocked is True
        assert "waited 0s" in lock.message or "waited" in lock.message
        assert "pid 4242" in lock.message
        assert elapsed < 30, "wait must honour its timeout, not the poll interval"

    def test_acquires_once_the_holder_exits(self, tmp_path, holder, monkeypatch):
        monkeypatch.setattr(pytest_lock, "_POLL_SECONDS", 0.05)
        path = tmp_path / "pytest.lock"
        holder(path, linger=0.6)  # releases by exiting

        lock = acquire(lock_path=path, wait=True, timeout=20)
        try:
            assert lock.blocked is False
            assert lock.acquired is True
        finally:
            lock.release()

    def test_wait_is_opt_in_via_env(self, tmp_path, holder, monkeypatch):
        monkeypatch.setattr(pytest_lock, "_POLL_SECONDS", 0.05)
        monkeypatch.setenv(pytest_lock.WAIT_ENV, "1")
        monkeypatch.setenv(pytest_lock.WAIT_TIMEOUT_ENV, "20")
        path = tmp_path / "pytest.lock"
        holder(path, linger=0.6)

        lock = acquire(lock_path=path)  # no explicit wait= → env decides
        try:
            assert lock.acquired is True
        finally:
            lock.release()

    def test_default_is_fail_fast_not_wait(self, tmp_path, holder):
        path = tmp_path / "pytest.lock"
        holder(path)

        started = time.monotonic()
        assert acquire(lock_path=path).blocked is True
        assert time.monotonic() - started < 2.0, "default acquire must not block"

    @pytest.mark.parametrize("raw", ["", "  ", "nonsense", "-5", "0"])
    def test_malformed_wait_timeout_falls_back_to_the_default(self, raw, monkeypatch):
        monkeypatch.setenv(pytest_lock.WAIT_TIMEOUT_ENV, raw)
        assert pytest_lock._env_timeout() == pytest_lock.DEFAULT_WAIT_TIMEOUT


class TestHolderRecord:
    def test_records_this_process_for_a_contender_to_read(self, tmp_path):
        path = tmp_path / "pytest.lock"
        lock = acquire(lock_path=path)
        try:
            recorded = path.read_text().split("\n")
            assert recorded[0] == str(os.getpid())
            assert float(recorded[1]) <= time.time()
        finally:
            lock.release()

    def test_held_env_is_set_for_children(self, tmp_path):
        lock = acquire(lock_path=tmp_path / "pytest.lock")
        try:
            assert os.environ[pytest_lock.HELD_ENV] == str(os.getpid())
        finally:
            lock.release()


class TestBoxLockDefaults:
    def test_a_bare_boxlock_is_permissive(self):
        """The fail-open sentinel every error path returns."""
        lock = BoxLock()
        assert lock.blocked is False
        assert lock.acquired is False
        lock.release()  # must not raise


def test_module_exposes_a_distinct_exit_status():
    """Not 1 — that is 'tests failed', and would send a caller debugging."""
    assert pytest_lock.EXIT_LOCK_HELD == 200
    assert pytest_lock.EXIT_LOCK_HELD != 1


class TestTimeoutSanitising:
    """A non-finite wait makes the deadline unreachable — an unbounded hang on
    the critical path of every test run. It must not be representable."""

    @pytest.mark.parametrize("raw", [float("inf"), 1e999, float("nan")])
    def test_non_finite_falls_back_to_the_default(self, raw):
        assert pytest_lock._sanitize_timeout(raw) == pytest_lock.DEFAULT_WAIT_TIMEOUT

    @pytest.mark.parametrize("raw", [0, -1, -0.5])
    def test_non_positive_falls_back_to_the_default(self, raw):
        assert pytest_lock._sanitize_timeout(raw) == pytest_lock.DEFAULT_WAIT_TIMEOUT

    def test_huge_value_is_capped(self):
        assert pytest_lock._sanitize_timeout(1e12) == pytest_lock.MAX_WAIT_TIMEOUT

    def test_reasonable_value_passes_through(self):
        assert pytest_lock._sanitize_timeout(600) == 600.0

    @pytest.mark.parametrize("raw", ["inf", "1e999", "nan"])
    def test_env_timeout_rejects_non_finite(self, raw, monkeypatch):
        monkeypatch.setenv(pytest_lock.WAIT_TIMEOUT_ENV, raw)
        assert pytest_lock._env_timeout() == pytest_lock.DEFAULT_WAIT_TIMEOUT

    def test_a_non_finite_env_timeout_cannot_hang_a_real_acquire(
        self, tmp_path, holder, monkeypatch
    ):
        """The end the sanitiser exists to prevent: an `inf` deadline never
        expires, so a blocked run would sit in pytest_configure forever."""
        monkeypatch.setenv(pytest_lock.WAIT_TIMEOUT_ENV, "inf")
        monkeypatch.setenv(pytest_lock.WAIT_ENV, "1")
        monkeypatch.setattr(pytest_lock, "_POLL_SECONDS", 0.05)
        # Shrink the fallback so a SANITISED `inf` produces a bounded wait we
        # can assert on. Without this the test either passes `timeout=` (which
        # short-circuits the env entirely, making it vacuous) or waits out the
        # 7200s default by outliving the holder.
        monkeypatch.setattr(pytest_lock, "DEFAULT_WAIT_TIMEOUT", 0.5)
        path = tmp_path / "pytest.lock"
        holder(path)

        started = time.monotonic()
        # No explicit timeout= : passing one short-circuits _env_timeout()
        # entirely, so the env var under test would never be read and this would
        # pass even with the sanitiser removed.
        lock = acquire(lock_path=path)
        assert lock.blocked is True
        assert time.monotonic() - started < 30


class TestDisableHatchFailsOpen:
    """A documented escape hatch that silently fails CLOSED is worse than none:
    the caller types it, nothing says otherwise, and the lock stays armed."""

    @pytest.mark.parametrize("raw", ["0", "off", "false", "no", "n", ""])
    def test_recognised_and_near_miss_falsey_values_disable(self, raw, monkeypatch):
        monkeypatch.setenv(pytest_lock.DISABLE_ENV, raw)
        assert pytest_lock._env_flag(pytest_lock.DISABLE_ENV, True, on_unknown=False) is False

    @pytest.mark.parametrize("raw", ["disabled", "nope", "please-stop"])
    def test_unrecognised_values_also_disable_and_say_so(self, raw, monkeypatch, capsys):
        monkeypatch.setenv(pytest_lock.DISABLE_ENV, raw)
        assert pytest_lock._env_flag(pytest_lock.DISABLE_ENV, True, on_unknown=False) is False
        assert "not understood" in capsys.readouterr().err

    @pytest.mark.parametrize("raw", ["1", "on", "true", "yes"])
    def test_truthy_values_keep_the_lock_armed(self, raw, monkeypatch):
        monkeypatch.setenv(pytest_lock.DISABLE_ENV, raw)
        assert pytest_lock._env_flag(pytest_lock.DISABLE_ENV, True, on_unknown=False) is True

    def test_an_unrecognised_disable_value_really_lets_a_run_through(
        self, tmp_path, holder, monkeypatch
    ):
        path = tmp_path / "pytest.lock"
        holder(path)
        monkeypatch.setenv(pytest_lock.DISABLE_ENV, "disabled")
        assert acquire(lock_path=path).blocked is False

    def test_an_unrecognised_wait_value_does_not_hang(self, tmp_path, holder, monkeypatch):
        """The other direction: unknown must not silently opt into waiting."""
        path = tmp_path / "pytest.lock"
        holder(path)
        monkeypatch.setenv(pytest_lock.WAIT_ENV, "maybe")
        started = time.monotonic()
        assert acquire(lock_path=path).blocked is True
        assert time.monotonic() - started < 5


class TestLockPathOverride:
    def test_env_override_wins_over_the_default(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere.lock"
        monkeypatch.setenv(pytest_lock.PATH_ENV, str(target))
        assert default_lock_path() == target

    def test_override_is_used_by_acquire(self, tmp_path, monkeypatch):
        target = tmp_path / "elsewhere.lock"
        monkeypatch.setenv(pytest_lock.PATH_ENV, str(target))
        lock = acquire()
        try:
            assert lock.acquired is True
            assert target.exists()
        finally:
            lock.release()

    def test_blank_override_falls_back_to_the_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv(pytest_lock.PATH_ENV, "")
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
        (tmp_path / ".genesis").mkdir()
        assert default_lock_path() == tmp_path / ".genesis" / "locks" / "pytest.lock"


class TestContextManager:
    def test_releases_on_exit(self, tmp_path):
        path = tmp_path / "pytest.lock"
        with acquire(lock_path=path) as lock:
            assert lock.acquired is True
        second = acquire(lock_path=path)
        try:
            assert second.acquired is True, "the with-block did not release"
        finally:
            second.release()

    def test_releases_even_when_the_body_raises(self, tmp_path):
        path = tmp_path / "pytest.lock"
        with pytest.raises(RuntimeError), acquire(lock_path=path):
            raise RuntimeError("boom")
        second = acquire(lock_path=path)
        try:
            assert second.acquired is True
        finally:
            second.release()


class TestHolderRecordIsNeverEmpty:
    def test_stale_tail_is_truncated(self, tmp_path):
        """A shorter new record must not leave the old record's tail behind.

        Named for what it actually proves: the final file state is identical
        under truncate-then-write, so this cannot detect the write WINDOW it was
        once named for — only that the truncate happened at all."""
        path = tmp_path / "pytest.lock"
        path.write_bytes(b"x" * 4096)  # pre-existing longer content
        lock = acquire(lock_path=path)
        try:
            content = path.read_text()
            assert content.split("\n")[0] == str(os.getpid())
            assert not content.endswith("x"), "stale tail not truncated"
        finally:
            lock.release()


class TestLockFileHardening:
    def test_lock_file_is_not_world_readable(self, tmp_path):
        """The holder record carries the running pytest's argv, and the file is
        deliberately never unlinked — so on a multi-user host 0o644 would leak
        another user's -k filters and paths."""
        path = tmp_path / "pytest.lock"
        lock = acquire(lock_path=path)
        try:
            mode = path.stat().st_mode & 0o777
            assert mode & 0o077 == 0, f"lock is group/world accessible: {mode:o}"
        finally:
            lock.release()

    def test_holder_read_is_bounded(self, tmp_path, holder, monkeypatch):
        """The path is env-redirectable and the read sits on a blocked run's
        path, so it must not slurp an arbitrary file."""
        path = tmp_path / "pytest.lock"
        holder(path)  # holds the flock; the record is written below
        # Written to the FILE rather than passed through argv, which has its
        # own (much smaller) size limit.
        path.write_text("4242\n0\n" + ("x" * 200_000))

        message = acquire(lock_path=path).message
        assert "pid 4242" in message
        assert len(message) < pytest_lock._RECORD_READ_LIMIT + 2000

    def test_symlinked_lock_path_fails_open(self, tmp_path):
        """O_NOFOLLOW: a symlink where the lock should be is a misconfiguration,
        and ELOOP must land on the fail-open path rather than raising."""
        real = tmp_path / "real.lock"
        real.write_text("")
        link = tmp_path / "link.lock"
        link.symlink_to(real)

        lock = acquire(lock_path=link)
        try:
            # `blocked is False` alone is VACUOUS: it is also the value on the
            # success path, so the assertion held with O_NOFOLLOW removed
            # (measured). The distinguishing fact is that the symlink was NOT
            # opened — fail open means "run the tests", not "follow the link".
            assert lock.acquired is False, "O_NOFOLLOW did not prevent the open"
            assert lock.blocked is False, "must fail open, never a hard error"
        finally:
            lock.release()


class TestExportEnvOptOut:
    """A long-lived process must not publish HELD_ENV to its whole environment.

    genesis-server dispatches Claude-Code sessions with ``env = dict(os.environ)``
    (cc/invoker.py). A session started while the gauntlet holds the lock would
    inherit the flag and silently no-op the lock for its entire multi-hour life —
    running unserialized against interactive suites, which is exactly the thrash
    the lock exists to prevent, and it cannot self-correct because the recorded
    pid is a live process.
    """

    def test_export_env_false_keeps_the_flag_out_of_the_environment(self, tmp_path):
        lock = acquire(lock_path=tmp_path / "pytest.lock", export_env=False)
        try:
            assert lock.acquired is True, "must still hold the lock"
            assert os.environ.get(pytest_lock.HELD_ENV) is None, (
                "HELD_ENV leaked into the process environment, so every child "
                "spawned during this hold would skip the lock"
            )
        finally:
            lock.release()

    def test_export_env_true_still_publishes_for_normal_callers(self, tmp_path):
        """A pytest run's children ARE its own, so the default must still export."""
        lock = acquire(lock_path=tmp_path / "pytest.lock")
        try:
            assert os.environ[pytest_lock.HELD_ENV] == str(os.getpid())
        finally:
            lock.release()

    def test_release_after_opt_out_does_not_clear_someone_elses_flag(self, tmp_path):
        """release() must not pop a flag this lock never set."""
        os.environ[pytest_lock.HELD_ENV] = "99999"
        try:
            lock = acquire(lock_path=tmp_path / "pytest.lock", export_env=False)
            lock.release()
            assert os.environ.get(pytest_lock.HELD_ENV) == "99999"
        finally:
            os.environ.pop(pytest_lock.HELD_ENV, None)


class TestCancellableWait:
    def test_cancel_event_aborts_a_blocking_wait_promptly(self, tmp_path, holder):
        """Threads are not interruptible: without this the worker runs its FULL
        timeout after the caller has given up, then takes a lock nobody wants."""
        import threading

        path = tmp_path / "pytest.lock"
        holder(path)
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()

        started = time.monotonic()
        lock = acquire(lock_path=path, wait=True, timeout=60, cancel=cancel)
        elapsed = time.monotonic() - started

        assert lock.acquired is False
        assert lock.blocked is False, "an aborted wait is not a refusal"
        assert elapsed < 20, f"cancel ignored — waited {elapsed:.1f}s of 60"

    def test_cancel_already_set_returns_without_waiting(self, tmp_path, holder):
        import threading

        path = tmp_path / "pytest.lock"
        holder(path)
        cancel = threading.Event()
        cancel.set()

        started = time.monotonic()
        acquire(lock_path=path, wait=True, timeout=60, cancel=cancel)
        assert time.monotonic() - started < 20
