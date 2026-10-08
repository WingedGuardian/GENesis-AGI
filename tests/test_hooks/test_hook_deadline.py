"""scripts/hooks/hook_deadline.py — the process-level hard stop.

The subprocess tests are the ones that matter: they prove a REAL process whose
main thread is blocked (a sleep, a long sqlite query) exits at the timer, with
exit 0, the notice on stdout and the blocked frame on stderr.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

_HOOKS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts" / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))

import hook_deadline as hd  # noqa: E402


def _run_child(body: str, timeout: float = 30.0) -> tuple[subprocess.CompletedProcess, float]:
    script = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(_HOOKS_DIR)!r})
        import hook_deadline as hd
        """
    ) + textwrap.dedent(body)
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout
    )
    return proc, time.monotonic() - started


def test_a_blocked_main_thread_is_stopped_at_the_timer() -> None:
    proc, elapsed = _run_child(
        """
        hd.arm(0.5, lambda: "NOTICE-LINE", label="t")
        print("before", flush=True)
        time.sleep(30)
        print("never")
        """
    )
    assert proc.returncode == 0
    assert elapsed < 10, f"the hard stop did not end a sleeping process ({elapsed:.1f}s)"
    assert proc.stdout.splitlines() == ["before", "NOTICE-LINE"]
    assert "[t] hard stop fired 0.5s after arming" in proc.stderr
    assert "<string>:<module>" in proc.stderr  # the frame that was blocked


def test_a_long_sqlite_query_does_not_hold_the_timer_off() -> None:
    """sqlite3 releases the GIL while a statement runs, so the timer can fire."""
    proc, elapsed = _run_child(
        """
        import sqlite3
        hd.arm(0.5, lambda: None, label="t")
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c"
        ).fetchone()
        """
    )
    assert proc.returncode == 0
    assert elapsed < 10, f"a running query held the hard stop off ({elapsed:.1f}s)"
    assert proc.stdout == ""  # notice() returned None: nothing written
    assert "hard stop fired" in proc.stderr


def test_a_disarmed_stop_never_fires() -> None:
    """Negative control: the same child, disarmed, finishes normally."""
    proc, _ = _run_child(
        """
        t = hd.arm(0.3, lambda: "NOTICE-LINE", label="t")
        hd.disarm(t)
        time.sleep(0.8)
        print("finished")
        """
    )
    assert proc.returncode == 0
    assert proc.stdout.splitlines() == ["finished"]
    assert "hard stop" not in proc.stderr


def test_a_half_written_line_is_ended_before_the_notice() -> None:
    proc, _ = _run_child(
        """
        out = hd.LockedStdout()
        out.write("partial")
        out.flush()
        hd.arm(0.2, lambda: "NOTICE-LINE", label="t")
        time.sleep(30)
        """
    )
    assert proc.stdout.splitlines() == ["partial", "NOTICE-LINE"]


class _Exited(Exception):
    pass


@pytest.fixture
def fake_os(monkeypatch: pytest.MonkeyPatch):
    """Capture os.write / os._exit / set_blocking from _fire without leaving the test."""
    calls: dict = {"writes": [], "blocking": []}

    def _write(fd: int, data: bytes) -> int:
        calls["writes"].append((fd, data.decode()))
        return len(data)

    def _exit(code: int) -> None:
        calls["exit"] = code
        raise _Exited

    monkeypatch.setattr(hd.os, "write", _write)
    monkeypatch.setattr(hd.os, "_exit", _exit)
    monkeypatch.setattr(hd.os, "set_blocking", lambda fd, b: calls["blocking"].append((fd, b)))
    monkeypatch.setattr(hd, "_LINE_OPEN", False)
    hd._DONE.clear()
    yield calls
    # In production _fire never releases STDOUT_LOCK: the process exits holding
    # it. Here the patched exit returns, so release it or every later write and
    # test that takes the lock blocks forever.
    if hd.STDOUT_LOCK.locked():
        hd.STDOUT_LOCK.release()
    hd._DONE.clear()


def test_fire_writes_notice_then_stack_then_exits_zero(fake_os: dict) -> None:
    with pytest.raises(_Exited):
        hd._fire(8.5, "lbl", lambda: "NOTE")
    assert fake_os["exit"] == 0
    assert fake_os["blocking"] == [(1, False), (2, False)]
    assert fake_os["writes"][0] == (1, "NOTE\n")
    assert fake_os["writes"][1][0] == 2
    assert fake_os["writes"][1][1].startswith("[lbl] hard stop fired 8.5s after arming")


def test_fire_without_the_lock_never_touches_stdout(fake_os: dict) -> None:
    """If the main thread is mid-write, writing would interleave: stderr only."""
    notices: list[bool] = []
    hd.STDOUT_LOCK.acquire()
    try:
        holder_done = threading.Event()
        result: list[BaseException] = []

        def _run() -> None:
            try:
                hd._fire(1.0, "lbl", lambda: notices.append(True) or "NOTE")
            except BaseException as exc:  # noqa: BLE001 - captured for the assert
                result.append(exc)
            holder_done.set()

        t = threading.Thread(target=_run)
        started = time.monotonic()
        t.start()
        assert holder_done.wait(5)
        waited = time.monotonic() - started
    finally:
        hd.STDOUT_LOCK.release()
    assert isinstance(result[0], _Exited)
    assert 0.15 <= waited < 2.0  # it waited ~0.2 s for the lock, then gave up
    assert notices == []  # the notice callback was never even consulted
    assert [fd for fd, _ in fake_os["writes"]] == [2]


def test_fire_after_disarm_does_nothing(fake_os: dict) -> None:
    hd._DONE.set()
    hd._fire(1.0, "lbl", lambda: "NOTE")  # returns, does not exit
    assert "exit" not in fake_os
    assert fake_os["writes"] == []


def test_a_failing_notice_still_exits(fake_os: dict) -> None:
    def _boom() -> str:
        raise RuntimeError("notice broke")

    with pytest.raises(_Exited):
        hd._fire(1.0, "lbl", _boom)
    assert [fd for fd, _ in fake_os["writes"]] == [2]


def test_arm_from_spawn_counts_from_process_start(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(hd, "arm", lambda seconds, notice, label: seen.append(seconds))
    monkeypatch.setattr(hd, "process_age_s", lambda: 3.0)
    hd.arm_from_spawn(8.5, lambda: None)
    monkeypatch.setattr(hd, "process_age_s", lambda: None)
    hd.arm_from_spawn(8.5, lambda: None)
    assert seen == [5.5, 8.5]


def test_locked_stdout_tracks_an_open_line(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    out = hd.LockedStdout()
    out.write("abc")
    assert hd._LINE_OPEN is True
    out.write("\n")
    assert hd._LINE_OPEN is False
    assert capsys.readouterr().out == "abc\n"


def test_a_full_stdout_pipe_cannot_hang_the_stop() -> None:
    """The flush must not block: stdout's pipe is full and the reader never reads.

    Reproduces the review's case: the pipe is filled, a line sits in Python's
    buffer (written but not flushed), and the parent does not read stdout until
    the child has exited.
    """
    script = textwrap.dedent(
        f"""
        import os, sys, time
        sys.path.insert(0, {str(_HOOKS_DIR)!r})
        import hook_deadline as hd
        os.set_blocking(1, False)
        try:
            while True:
                os.write(1, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(1, True)
        hd.LockedStdout().write("pending")  # buffered, not flushed
        hd.arm(0.3, lambda: "NOTICE-LINE", label="t")
        time.sleep(30)
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        rc = proc.wait(timeout=10)  # NOT reading stdout: the pipe stays full
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("the hard stop hung on a full stdout pipe")
    proc.communicate()
    assert rc == 0
