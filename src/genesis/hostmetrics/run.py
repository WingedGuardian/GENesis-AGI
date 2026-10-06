"""Launch an admitted job in a named, capped scope: ``genesis-job-<name>-<id>.scope``.

The scope carries the job's estimates as hard caps (``MemoryMax`` = RAM,
``CPUQuota`` = CPU), so it is both the enforcement and the ledger entry other
sessions read (see ``jobs.py``). The probe and the launch share one argv
builder: systemd-run rejects a property it cannot accept (exit 1), so a
property-free probe could pass while the real launch fails.

Only a missing ``systemd-run`` or an unreachable user manager runs the job
UNCAPPED (``nice 19`` and a data-segment limit, invisible to other sessions).
A property systemd refuses is an error, never a silent downgrade.
"""

from __future__ import annotations

import contextlib
import os
import re
import resource
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from genesis.hostmetrics import readings
from genesis.hostmetrics.jobs import SCOPE_PREFIX, systemd_env

# Runs inside the scope: the command (with the report fd closed for it), then
# the scope's own peak memory, CPU time and OOM-kill count, written to that fd
# while the scope still exists (systemd drops its accounting the moment the
# last process exits). ``fd`` is a digit string this module generates.
_REPORT_SH = (
    "fd=$1; shift; rc=0; eval '\"$@\" '\"$fd\"'>&-' || rc=$?; "
    "cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
    'printf "%s %s %s\\n" "$(cat "$cg/memory.peak" 2>/dev/null)" '
    '"$(sed -n "s/^usage_usec //p" "$cg/cpu.stat" 2>/dev/null)" '
    '"$(sed -n "s/^oom_kill //p" "$cg/memory.events" 2>/dev/null)" >&"$fd"; exit $rc'
)
_SYSTEMD_TIMEOUT = 15  # a local D-Bus round trip; a hang means no reachable manager
MIN_RAM = 16 * 1024 * 1024  # below this the probe itself is OOM-killed or refused


class ProbeRefused(Exception):
    """systemd refused the job's scope: a bad estimate or slice, not a missing manager."""


def unit_name(name: str, job_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name)[:40] or "job"
    return f"{SCOPE_PREFIX}{safe}-{job_id}"


def scope_properties(ram: int, cpu_pct: float, oom_continue: bool = True) -> list[str]:
    """Caps from the estimates. ``OOMPolicy=continue`` lets only the process that
    hit ``MemoryMax`` die, so the reporter survives to say so; systemd before
    253 lacks it for scopes, hence the flag. ``IOWeight`` takes effect only where
    the io controller is delegated; elsewhere systemd accepts and ignores it."""
    props = [
        f"MemoryMax={ram}",
        "MemorySwapMax=0",
        f"CPUQuota={max(1, round(cpu_pct))}%",
        "IOWeight=50",
    ]
    return [*props, "OOMPolicy=continue"] if oom_continue else props


def scope_argv(unit: str, props: list[str], slice_name: str | None, *trailing: str) -> list[str]:
    # --collect: a failed scope is unloaded instead of lingering under its name.
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    if slice_name:
        argv.append(f"--slice={slice_name}")
    for prop in props:
        argv += ["-p", prop]
    return [*argv, "--", *trailing]


def choose_properties(
    unit: str,
    ram: int,
    cpu_pct: float,
    slice_name: str | None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> list[str] | None:
    """The property set a probe scope accepts; None when no manager is reachable.

    Raises ProbeRefused when systemd refuses the properties for any reason other
    than an ``OOMPolicy`` it does not know (then the next rung drops it).
    """
    if shutil.which("systemd-run") is None:
        return None
    for rung, oom_continue in enumerate((True, False)):
        props = scope_properties(ram, cpu_pct, oom_continue)
        try:
            probe = runner(
                scope_argv(f"{unit}-probe{rung}", props, slice_name, "/bin/true"),
                capture_output=True,
                timeout=_SYSTEMD_TIMEOUT,
                env=systemd_env(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode == 0:
            return props
        err = probe.stderr.decode(errors="replace").strip() if probe.stderr else ""
        if "Failed to connect" in err:  # MEASURED: "Failed to connect to bus: …"
            return None
        if not (oom_continue and "OOMPolicy" in err):
            raise ProbeRefused(err or f"probe scope exited {probe.returncode}")
    return None


def parse_report(raw: bytes) -> tuple[int | None, float | None, int | None]:
    """(peak bytes, CPU seconds, OOM kills) from the in-scope reporter's line."""
    fields = raw.decode(errors="replace").split()
    values: list[float | None] = []
    for i in range(3):
        try:
            values.append(float(fields[i]))
        except (IndexError, ValueError):
            values.append(None)
    peak, usec, ooms = values
    return (
        None if peak is None else int(peak),
        None if usec is None else usec / 1e6,
        None if ooms is None else int(ooms),
    )


class Watchdog:
    """Stops OUR job after the box stays over the line for ``grace`` seconds.

    Policy: it stops this job even when other workloads caused the pressure,
    because this job is the one Genesis can stop safely. It never touches
    anything else.
    """

    def __init__(self, over: Callable[[], str | None], stop: Callable[[], None], grace=60.0):
        self.over, self.stop, self.grace = over, stop, grace
        self.since: float | None = None
        self.fired: str | None = None

    def tick(self, now: float) -> bool:
        reason = self.over()
        if reason is None:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        if now - self.since < self.grace:
            return False
        self.stop()
        self.fired = f"{reason} for {self.grace:g}s"
        return True

    def run(self, done: threading.Event, interval: float = 10.0) -> None:
        while not done.wait(interval):
            if self.tick(time.monotonic()):
                return


def over_the_line(
    threshold: float,
    disks: list[str],
    approved: frozenset[str],
    read_memory: Callable[[], readings.Memory | None] = readings.read_memory,
    read_disk: Callable[[str], tuple[int, int] | None] = readings.read_disk,
) -> Callable[[], str | None]:
    """The watchdog's question: is container memory or a watched disk over the line?

    A resource the owner approved running over the line is not watched.
    """

    def check() -> str | None:
        if "memory" not in approved:
            mem = read_memory()
            if mem is not None and mem.total - mem.available > threshold * mem.total:
                return "container memory over the line"
        if "disk" not in approved:
            for path in disks:
                disk = read_disk(path)
                if disk is not None and disk[0] - disk[1] > threshold * disk[0]:
                    return f"disk {path} over the line"
        return None

    return check


def kill_group(pgid: int, sig: int = signal.SIGTERM) -> None:
    """Signal an uncapped job's process group. pgid 0/1 would hit our own group or init."""
    if pgid > 1:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)


def stop_scope(unit: str, runner: Callable[..., object] = subprocess.run) -> None:
    """Ask systemd to stop the scope without waiting for it to finish stopping
    (a job may take longer than any timeout here to shut down). Never raises."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        runner(
            ["systemctl", "--user", "stop", "--no-block", f"{unit}.scope"],
            capture_output=True,
            timeout=_SYSTEMD_TIMEOUT,
            env=systemd_env(),
        )


def _uncapped_limits(ram: int) -> Callable[[], None]:
    # RLIMIT_DATA, not RLIMIT_AS: runtimes (JVM, Go, node, OpenBLAS) reserve large
    # address ranges they never touch, and an AS limit kills them at start.
    def apply() -> None:
        os.nice(19)
        resource.setrlimit(resource.RLIMIT_DATA, (ram, ram))

    return apply


def launch(
    name: str,
    cmd: list[str],
    ram: int,
    cpu_pct: float,
    over: Callable[[], str | None],
    slice_name: str | None = None,
) -> int:
    """Run ``cmd`` capped and supervised; returns its exit code (128+N on signal N).

    Raises ProbeRefused when systemd refuses the scope's properties.
    """
    unit = unit_name(name, secrets.token_hex(3))
    props = choose_properties(unit, ram, cpu_pct, slice_name)
    state: dict = {"stop": None, "pending": False, "reaped": False}

    def forward(signum, _frame):
        if state["stop"] is None:
            state["pending"] = True  # arrived before the job existed: stop it once it does
        else:
            state["stop"]()

    forwarded = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {s: signal.signal(s, forward) for s in forwarded}
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)  # a descendant may still hold the write end
    try:
        if props is not None:
            argv = scope_argv(
                unit,
                props,
                slice_name,
                "/bin/sh",
                "-c",
                _REPORT_SH,
                "genesis-job",
                str(write_fd),
                *cmd,
            )
            proc = subprocess.Popen(
                argv, pass_fds=(write_fd,), env=systemd_env(), start_new_session=True
            )
            state["stop"] = lambda: stop_scope(unit)
            print(f"genesis-job {unit}: started ({', '.join(props)})", file=sys.stderr)
        else:
            try:
                proc = subprocess.Popen(
                    cmd, preexec_fn=_uncapped_limits(ram), start_new_session=True
                )
            except OSError as exc:
                print(f"genesis-job {name}: cannot start: {exc}", file=sys.stderr)
                os.close(read_fd)
                return 127
            # After the job is reaped its pgid may be reused: never signal it then.
            state["stop"] = lambda: None if state["reaped"] else kill_group(proc.pid)
            print(
                f"genesis-job {name}: UNCAPPED (systemd user manager unreachable): "
                "nice 19 and a data-segment limit; invisible to other sessions",
                file=sys.stderr,
            )
        os.close(write_fd)
        write_fd = -1
        if state["pending"]:
            state["stop"]()
        done = threading.Event()
        watchdog = Watchdog(over, lambda: state["stop"]())
        threading.Thread(target=watchdog.run, args=(done,), daemon=True).start()
        try:
            rc = proc.wait()
        finally:
            state["reaped"] = True
            done.set()
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        if write_fd != -1:
            os.close(write_fd)
    try:
        raw = os.read(read_fd, 512)
    except BlockingIOError:
        raw = b""
    os.close(read_fd)
    rc = 128 - rc if rc < 0 else rc
    _report(name if props is None else unit, rc, ram, raw, props is not None, watchdog.fired)
    return rc


def _report(label: str, rc: int, ram: int, raw: bytes, scoped: bool, fired: str | None) -> None:
    parts = [f"exit {rc}"]
    if scoped:
        peak, cpu_s, ooms = parse_report(raw)
        if peak is not None:
            parts.append(f"peak {peak / 2**20:.0f} MiB of {ram / 2**20:.0f} MiB estimate")
    else:
        # rusage covers this wrapper's children: the largest single process, not a group total.
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_s, ooms = usage.ru_utime + usage.ru_stime, None
        parts.append(f"largest process {usage.ru_maxrss / 1024:.0f} MiB")
    if cpu_s is not None:
        parts.append(f"cpu {cpu_s:.2f}s")
    if ooms:
        parts.append("killed at its memory cap (raise --ram)")
    if fired:
        parts.append(f"stopped by the watchdog: {fired}")
    print(f"genesis-job {label}: " + ", ".join(parts), file=sys.stderr)
