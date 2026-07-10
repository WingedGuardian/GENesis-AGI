"""Tests for genesis.util.process_lock."""

import os
import signal
import subprocess
import sys
import textwrap

from genesis.util.process_lock import ProcessLock


def test_lock_acquires_and_releases(tmp_path):
    """Lock acquires, writes PID, and releases cleanly."""
    lock = ProcessLock("test", pid_dir=tmp_path)

    with lock:
        assert lock.lock_path.exists()
        assert lock.lock_path.read_text() == str(os.getpid())

    # File cleaned up on normal exit
    assert not lock.lock_path.exists()


def test_duplicate_blocked(tmp_path):
    """Second process trying to acquire the same lock exits with code 200."""
    # First process holds the lock via a subprocess that sleeps
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""\
                import time
                from genesis.util.process_lock import ProcessLock
                from pathlib import Path
                with ProcessLock("dup", pid_dir=Path("{tmp_path}")):
                    time.sleep(30)
            """),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for lock file to appear
    lock_path = tmp_path / "dup.lock"
    for _ in range(50):
        if lock_path.exists():
            content = lock_path.read_text().strip()
            if content:
                break
        import time
        time.sleep(0.1)

    try:
        # Second process should fail
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(f"""\
                    from genesis.util.process_lock import ProcessLock
                    from pathlib import Path
                    with ProcessLock("dup", pid_dir=Path("{tmp_path}")):
                        pass
                """),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 200
        assert "already running" in result.stderr
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_lock_released_on_crash(tmp_path):
    """Lock is released when holder is killed with SIGKILL."""
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""\
                import time
                from genesis.util.process_lock import ProcessLock
                from pathlib import Path
                with ProcessLock("crash", pid_dir=Path("{tmp_path}")):
                    time.sleep(30)
            """),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    lock_path = tmp_path / "crash.lock"
    for _ in range(50):
        if lock_path.exists() and lock_path.read_text().strip():
            break
        import time
        time.sleep(0.1)

    # Kill -9 the holder
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=5)

    # New process should acquire fine
    with ProcessLock("crash", pid_dir=tmp_path):
        assert lock_path.read_text() == str(os.getpid())


def test_pid_file_content(tmp_path):
    """PID file contains the correct integer."""
    with ProcessLock("pidcheck", pid_dir=tmp_path):
        content = (tmp_path / "pidcheck.lock").read_text()
        assert int(content) == os.getpid()


def test_creates_pid_dir(tmp_path):
    """Lock creates the PID directory if it doesn't exist."""
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists()

    with ProcessLock("nested", pid_dir=nested):
        assert nested.exists()
        assert (nested / "nested.lock").exists()


def test_is_locked_false_when_free(tmp_path):
    """No holder (or no lock file at all) → not locked."""
    assert ProcessLock.is_locked("free", pid_dir=tmp_path) is False
    # Probing must not delete a pre-existing lock file left on disk
    (tmp_path / "stale.lock").write_text("12345")
    assert ProcessLock.is_locked("stale", pid_dir=tmp_path) is False
    assert (tmp_path / "stale.lock").exists()


def test_is_locked_true_while_held(tmp_path):
    """A live holder in another process → locked; released → unlocked."""
    import textwrap
    import time

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""\
                import time
                from pathlib import Path
                from genesis.util.process_lock import ProcessLock
                with ProcessLock("held", pid_dir=Path("{tmp_path}")):
                    time.sleep(30)
            """),
        ],
    )
    try:
        lock_path = tmp_path / "held.lock"
        for _ in range(100):
            if lock_path.exists() and lock_path.read_text().strip():
                break
            time.sleep(0.1)
        assert ProcessLock.is_locked("held", pid_dir=tmp_path) is True
    finally:
        holder.send_signal(signal.SIGTERM)
        holder.wait(timeout=10)
    # flock auto-releases on process death
    assert ProcessLock.is_locked("held", pid_dir=tmp_path) is False


def test_is_locked_vanishing_file_is_free(tmp_path, monkeypatch):
    """Holder unlinks the lock file between exists() and open() → free,
    not a crash (TOCTOU guard)."""
    (tmp_path / "gone.lock").write_text("1")
    real_open = os.open

    def _vanish(path, *a, **kw):
        if path.endswith("gone.lock"):
            raise FileNotFoundError(path)
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", _vanish)
    assert ProcessLock.is_locked("gone", pid_dir=tmp_path) is False


def test_is_locked_io_error_reports_locked_with_warning(tmp_path, monkeypatch, caplog):
    """Non-contention flock failures (EIO etc.) read as locked — refusing a
    fallback start is safer than risking a dual runtime — but loudly."""
    import errno as _errno
    import fcntl
    import logging

    (tmp_path / "weird.lock").write_text("1")

    def _eio(*a, **kw):
        raise OSError(_errno.EIO, "io error")

    monkeypatch.setattr(fcntl, "flock", _eio)
    with caplog.at_level(logging.WARNING):
        assert ProcessLock.is_locked("weird", pid_dir=tmp_path) is True
    assert any("inconclusive" in r.message for r in caplog.records)
