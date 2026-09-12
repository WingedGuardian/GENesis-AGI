"""Tests for genesis.util.proc_kill — the shared process-group kill helpers.

Real-subprocess tests lock the two behaviors that matter (whole-group reap,
and reap-after-the-leader-was-reaped — the getpgid trap from PR #1409 round
3); mock tests lock the pgid<=1 safety guard (killpg(1) == kill every process
we own — see the process_kill_safety procedure).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from unittest.mock import MagicMock

import pytest

from genesis.util.proc_kill import kill_process_group, reap_bounded
from tests.proc_asserts import process_terminated


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def test_kills_whole_group_while_leader_alive(tmp_path):
    marker = tmp_path / "child"
    proc = subprocess.Popen(
        ["bash", "-c", f"sleep 300 & echo $! > {marker}; sleep 300"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (marker.exists() and marker.read_text().strip()):
            time.sleep(0.05)
        child = int(marker.read_text().strip())
        assert child > 1
        kill_process_group(proc)
        proc.wait(timeout=5)
        time.sleep(0.2)
        # gone-or-zombie: os.kill(pid, 0) succeeds on a zombie, so a bare
        # ProcessLookupError assertion flakes on minimal-init hosts
        assert process_terminated(child)
    finally:
        kill_process_group(proc)


def test_kills_survivors_after_leader_reaped(tmp_path):
    """The leader exits and is waited on (reaped) while a child keeps the
    group alive. os.getpgid(leader) would raise here; signalling proc.pid AS
    the pgid must still reap the survivor (kernel reserves the pgid)."""
    marker = tmp_path / "child"
    proc = subprocess.Popen(
        ["bash", "-c", f"sleep 300 & echo $! > {marker}; exit 0"],
        start_new_session=True,
    )
    proc.wait(timeout=5)  # leader reaped — getpgid(proc.pid) now raises
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (marker.exists() and marker.read_text().strip()):
        time.sleep(0.05)
    child = int(marker.read_text().strip())
    assert child > 1
    with pytest.raises(ProcessLookupError):
        os.getpgid(proc.pid)  # precondition: the trap is armed
    kill_process_group(proc)
    time.sleep(0.3)
    assert process_terminated(child)  # gone-or-zombie (see tests/proc_asserts)


def test_pgid_guard_refuses_low_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda *a: killed.append(a),
    )
    proc = MagicMock()
    proc.pid = 1
    kill_process_group(proc)
    assert killed == []
    proc.kill.assert_called_once()


def test_pgid_guard_refuses_non_int_pid(monkeypatch):
    killed = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda *a: killed.append(a),
    )
    proc = MagicMock()  # .pid left as a MagicMock attribute, not an int
    kill_process_group(proc)
    assert killed == []
    proc.kill.assert_called_once()


def test_killpg_signal_and_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg",
        lambda pgid, sig: calls.append((pgid, sig)),
    )
    proc = MagicMock()
    proc.pid = 54321
    kill_process_group(proc)
    assert calls == [(54321, signal.SIGKILL)]
    proc.kill.assert_not_called()


@pytest.mark.asyncio
async def test_reap_bounded_returns_despite_hung_wait():
    class _Hung:
        async def wait(self):
            await asyncio.sleep(600)

    t0 = time.monotonic()
    await reap_bounded(_Hung(), timeout_s=0.1)
    assert time.monotonic() - t0 < 5


def test_process_group_alive_real_group(tmp_path):
    from genesis.util.proc_kill import process_group_alive

    proc = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        assert process_group_alive(proc) is True
    finally:
        kill_process_group(proc)
        proc.wait(timeout=5)
    time.sleep(0.2)
    assert process_group_alive(proc) is False


def test_process_group_alive_guards(monkeypatch):
    from genesis.util.proc_kill import process_group_alive

    probes = []
    monkeypatch.setattr(
        "genesis.util.proc_kill.os.killpg", lambda *a: probes.append(a)
    )
    bad = MagicMock()
    bad.pid = 1
    assert process_group_alive(bad) is False
    bad2 = MagicMock()  # non-int mock pid
    assert process_group_alive(bad2) is False
    assert probes == []  # guard refused the probe both times
