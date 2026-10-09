"""Resource admission and enforced systemd scope for heavy analytics commands."""

from __future__ import annotations

import fcntl
import math
import os
import select
import subprocess
import sys
import time
from pathlib import Path

from .config import Config

_CHILD = "GENESIS_TRANSCRIPT_RESOURCE_CHILD"
_READY = "GENESIS_TRANSCRIPT_RESOURCE_READY_FD"
_START_TIMEOUT = 30


def _enforced(marker: str) -> bool:
    """Verify kernel limits in the current cgroup, never trust an env flag alone."""
    try:
        ram, cpu = (float(part) for part in marker.split(","))
        if not all(math.isfinite(value) and value > 0 for value in (ram, cpu)):
            return False
        group = next(
            line[3:]
            for line in Path("/proc/self/cgroup").read_text().splitlines()
            if line.startswith("0::")
        )
        root = Path("/sys/fs/cgroup") / group.lstrip("/")
        memory = int((root / "memory.max").read_text())
        swap = int((root / "memory.swap.max").read_text())
        quota, period = (int(v) for v in (root / "cpu.max").read_text().split())
        return 0 < memory <= ram and swap == 0 and 0 < quota / period * 100 <= cpu + 0.01
    except (OSError, ValueError, StopIteration, ZeroDivisionError):
        return False


def _acknowledge(enforced):
    descriptor = os.environ.pop(_READY, None)
    if descriptor is not None:
        fd = int(descriptor)
        try:
            os.write(fd, b"1" if enforced else b"0")
        finally:
            os.close(fd)


def _await_ready(owned, descriptor):
    deadline = time.monotonic() + _START_TIMEOUT
    readiness = select.poll()
    readiness.register(descriptor, select.POLLIN | select.POLLHUP | select.POLLERR)
    while time.monotonic() < deadline:
        owned.checkpoint()
        if owned.signals:
            return False
        readable = readiness.poll(max(0, min(100, (deadline - time.monotonic()) * 1000)))
        owned.checkpoint()
        if owned.signals:
            return False
        if readable:
            return os.read(descriptor, 1) == b"1"
        if owned.exited():
            return False
    return False


def _launch(argv, ram, cpu, lease):
    from genesis.hostmetrics import run
    from genesis.hostmetrics.jobs import systemd_env

    unit = run.unit_name("transcript-analytics", os.urandom(16).hex())
    props = [f"MemoryMax={ram}", "MemorySwapMax=0", f"CPUQuota={cpu:.2f}%", "RuntimeMaxSec=1h"]
    ready_read, ready_write = os.pipe()
    owned = run.OwnedProcess()
    code = 69
    try:
        with owned:
            command = run.scope_argv(unit, props, None, sys.executable, "-m", __name__, *argv)
            owned.check_cancelled()
            owned.start_scoped(
                command, unit,
                pass_fds=(ready_write,),
                start_new_session=True,
                env={**systemd_env(), _CHILD: f"{ram},{cpu:.2f}", _READY: str(ready_write)},
            )
            os.close(ready_write)
            ready_write = -1
            if not _await_ready(owned, ready_read):
                owned.check_cancelled()
                raise run.ProbeRefused("worker scope did not confirm enforced limits")
            owned.check_cancelled()
            # The confirmed named scope now participates in the admission ledger.
            fcntl.flock(lease, fcntl.LOCK_UN)
            code = owned.wait()
    except run.Cancelled:
        pass
    except (OSError, run.ProbeRefused, subprocess.TimeoutExpired) as exc:
        print(f"transcript analytics unavailable: {exc}", file=sys.stderr)
    finally:
        os.close(ready_read)
        if ready_write != -1:
            os.close(ready_write)
    return owned.outcome(128 - code if code < 0 else code)


def ensure_capped(argv: list[str], cfg: Config) -> int | None:
    """Strict named scopes: deferred=75, unavailable=69, verified child=None."""
    marker = os.environ.get(_CHILD)
    if marker is not None:
        enforced = _enforced(marker)
        _acknowledge(enforced)
        if enforced:
            return None
        print(
            "transcript analytics unavailable: resource limits were not enforced", file=sys.stderr
        )
        return 69
    try:
        return _admit(argv, cfg)
    except ValueError as exc:
        print(f"transcript analytics unavailable: configuration: {exc}", file=sys.stderr)
        return 69
    except OSError as exc:
        print(f"transcript analytics unavailable: admission infrastructure: {exc}", file=sys.stderr)
        return 69


def _admit(argv, cfg):
    from genesis.env import genesis_home
    from genesis.hostmetrics.__main__ import take_snapshot
    from genesis.hostmetrics.preflight import Request, evaluate, load_levers

    lock_path = genesis_home() / "locks/transcript-analytics-admission.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("transcript analytics deferred: admission in progress", file=sys.stderr)
            return 75
        # Uninstall holds this same lease while retiring the canonical base file.
        # A stale Config object or surviving private opt-in cannot readmit work.
        from . import config

        cfg = config.load()
        if not cfg.enabled or (cfg.data_dir / "DISABLED").exists():
            print("transcript analytics unavailable: collection was disabled", file=sys.stderr)
            return 69
        snap = take_snapshot([], 0.5)
        if snap.memory is None:
            print(
                "transcript analytics deferred: ASK (memory capacity unavailable)", file=sys.stderr
            )
            return 75
        if snap.reserved_beyond_use is None:
            print(
                "transcript analytics unavailable: systemd admission ledger unreachable",
                file=sys.stderr,
            )
            return 69
        ram = cfg.ram_bytes or max(1, int(snap.memory.total * cfg.ram_pct / 100))
        from genesis.hostmetrics.run import MIN_RAM

        if ram < MIN_RAM:
            print(
                f"transcript analytics unavailable: RAM cap must be at least {MIN_RAM} bytes",
                file=sys.stderr,
            )
            return 69
        cpu = snap.cpu_capacity * cfg.cpu_pct
        result = evaluate(snap, Request("transcript analytics", ram=ram, cpu=cpu), load_levers())
        if result.verdict != "GO":
            reasons = "; ".join(c.reason for c in result.checks if c.verdict != "GO")
            print(f"transcript analytics deferred: {result.verdict}: {reasons}", file=sys.stderr)
            return 75
        return _launch(argv, ram, cpu, lease)


def _child_main():
    """Confirm scope startup before importing the actual CLI command."""
    import runpy

    enforced = _enforced(os.environ.get(_CHILD, ""))
    _acknowledge(enforced)
    if not enforced:
        raise SystemExit(69)
    sys.argv[0] = "genesis"
    runpy.run_module("genesis", run_name="__main__")


if __name__ == "__main__":
    _child_main()
