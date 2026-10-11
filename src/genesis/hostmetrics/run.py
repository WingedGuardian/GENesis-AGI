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
from dataclasses import dataclass

from genesis.hostmetrics import readings
from genesis.hostmetrics.jobs import SCOPE_PREFIX, systemd_env

# Runs inside the scope: the command (with the report fd closed for it), then
# the scope's own peak memory, CPU time and OOM-kill count, written to that fd
# while the scope still exists (systemd drops its accounting the moment the
# last process exits). ``fd`` is a digit string this module generates.
_REPORT_SH = (
    "fd=$1; shift; rc=0; eval '\"$@\" '\"$fd\"'>&-' || rc=$?; "
    "cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
    'printf "%s|%s|%s\\n" "$(cat "$cg/memory.peak" 2>/dev/null)" '
    '"$(sed -n "s/^usage_usec //p" "$cg/cpu.stat" 2>/dev/null)" '
    '"$(sed -n "s/^oom_kill //p" "$cg/memory.events" 2>/dev/null)" >&"$fd"; exit $rc'
)
_SYSTEMD_TIMEOUT = 15  # a local D-Bus round trip; a hang means no reachable manager
# The probe runs inside its scope and prints the limits the kernel actually applies:
# systemd accepts MemoryMax/CPUQuota even where the controller is not delegated to
# the user manager, and the scope's memory.max then stays `max`.
_ENFORCEMENT_SH = (
    "cg=/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup); "
    'cat "$cg/memory.max" 2>/dev/null || echo missing; '
    'cat "$cg/cpu.max" 2>/dev/null || echo missing; '
    'cat "$cg/memory.swap.max" 2>/dev/null || echo missing'
)
MIN_RAM = 16 * 1024 * 1024  # below this the probe itself is OOM-killed or refused


class ProbeRefused(Exception):
    """systemd refused the job's scope: a bad estimate or slice, not a missing manager."""


@dataclass(frozen=True)
class Caps:
    """The properties a probe scope accepted, and which caps it did not enforce."""

    props: list[str]
    unenforced: tuple[str, ...] = ()  # "memory"/"cpu"/"swap": accepted, not applied
    unverified: tuple[str, ...] = ()  # "memory"/"cpu"/"swap": the limit file was unreadable


# Why a cap reads as not enforced. Swap has no controller of its own: its limit
# lives in the memory controller, so a non-zero reading means the kernel did not
# apply MemorySwapMax=0, not that something is undelegated (round-2 audit).
_UNENFORCED_WHY = {
    "memory": "the memory controller is not delegated to the user manager",
    "cpu": "the cpu controller is not delegated to the user manager",
    "swap": "the kernel did not apply MemorySwapMax=0",
}
_UNVERIFIED_WHY = {
    "memory": "memory.max was unreadable in the scope, e.g. cgroup v1",
    "cpu": "cpu.max was unreadable in the scope, e.g. cgroup v1",
    "swap": "memory.swap.max was unreadable: no memcg swap accounting, or cgroup v1",
}


def _swap_configured() -> bool:
    """Whether any swap device is active (/proc/swaps lists one). With none,
    nothing can swap and a missing swap limit is not worth a warning. An
    unreadable file counts as configured, so the check errs toward warning."""
    try:
        with open("/proc/swaps") as f:
            return len(f.read().strip().splitlines()) > 1
    except OSError:
        return True


def _limits(
    stdout: bytes | str | None, swap_present: bool = True
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(unenforced, unverified) caps from the probe's readback of its own scope."""
    text = stdout.decode(errors="replace") if isinstance(stdout, bytes) else (stdout or "")
    lines = [ln.strip() for ln in text.splitlines()]
    memory = lines[0] if lines else "missing"
    cpu = lines[1].split()[0] if len(lines) > 1 and lines[1] else "missing"
    swap = lines[2] if len(lines) > 2 else "missing"
    values = (("memory", memory), ("cpu", cpu))
    # Every scope asks for MemorySwapMax=0, so anything but 0 read back means the
    # zero-swap limit is not applied (systemd tolerates its absence: review).
    swap_unenforced = swap_present and swap not in ("0", "missing", "")
    swap_unverified = swap_present and swap in ("missing", "")
    return (
        tuple(name for name, value in values if value == "max")
        + (("swap",) if swap_unenforced else ()),
        # Absent or unreadable (cgroup v1, a controller missing from subtree_control):
        # the cap cannot be confirmed either way.
        tuple(name for name, value in values if value in ("missing", ""))
        + (("swap",) if swap_unverified else ()),
    )


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


#: Where capped jobs run unless the caller names a slice. A child of app.slice
#: (dash nesting), so every ancestor limit and policy is unchanged; its own
#: memory.events counts every OOM kill inside it, and it outlives its scopes,
#: which is how the disk guardian tells a job killed at its own cap from any
#: other OOM kill without guessing from journal text.
CAPPED_SLICE = "app-capped.slice"


def scope_argv(unit: str, props: list[str], slice_name: str | None, *trailing: str) -> list[str]:
    # --collect: a failed scope is unloaded instead of lingering under its name.
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    argv.append(f"--slice={slice_name or CAPPED_SLICE}")
    for prop in props:
        argv += ["-p", prop]
    return [*argv, "--", *trailing]


def choose_properties(
    unit: str,
    ram: int,
    cpu_pct: float,
    slice_name: str | None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Caps | None:
    """The caps a probe scope accepts; None only when no manager can be reached.

    None means `systemd-run` is absent or the bus is unreachable (MEASURED stderr:
    "Failed to connect to bus: …"). Anything ambiguous — a probe that times out, a
    `systemd-run` that cannot execute — raises ProbeRefused, because running the
    job uncapped on a guess is the outcome the probe exists to prevent. So does a
    refused property, unless it is an ``OOMPolicy`` systemd does not know (then
    the next rung drops it).
    """
    if shutil.which("systemd-run") is None:
        return None
    for rung, oom_continue in enumerate((True, False)):
        props = scope_properties(ram, cpu_pct, oom_continue)
        probe_unit = f"{unit}-probe{rung}"
        try:
            probe = runner(
                scope_argv(probe_unit, props, slice_name, "/bin/sh", "-c", _ENFORCEMENT_SH),
                capture_output=True,
                timeout=_SYSTEMD_TIMEOUT,
                env=systemd_env(),
            )
        except FileNotFoundError:
            return None  # systemd-run vanished after `which` found it
        except subprocess.TimeoutExpired:
            stop_scope(probe_unit, runner)
            raise ProbeRefused(
                f"the probe scope did not finish within {_SYSTEMD_TIMEOUT}s "
                "(a slow manager is not a missing one)"
            ) from None
        except OSError as exc:
            raise ProbeRefused(f"systemd-run could not run: {exc}") from None
        if probe.returncode == 0:
            return Caps(props, *_limits(probe.stdout, _swap_configured()))
        err = probe.stderr.decode(errors="replace").strip() if probe.stderr else ""
        if "Failed to connect" in err:  # MEASURED: "Failed to connect to bus: …"
            return None
        if not (oom_continue and "OOMPolicy" in err):
            raise ProbeRefused(err or f"probe scope exited {probe.returncode}")
    raise ProbeRefused("no probe rung succeeded")  # unreachable; fails closed if not


def parse_report(raw: bytes) -> tuple[int | None, float | None, int | None]:
    """(peak bytes, CPU seconds, OOM kills) from the in-scope reporter's line."""
    # Delimited, not whitespace-split: an unreadable file leaves an EMPTY field,
    # and splitting on whitespace would shift every later metric into its slot.
    fields = [f.strip() for f in raw.decode(errors="replace").strip().split("|")]
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
        self.reason: str | None = None
        self.fired: str | None = None

    def tick(self, now: float) -> bool:
        reason = self.over()
        if reason is None:
            self.since = None
            return False
        # One continuous timer: the box over the line for `grace` seconds, whichever
        # resource it is (memory, then a disk, is still the box over the line).
        if self.since is None:
            self.since = now
        self.reason = reason
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
    caps = choose_properties(unit, ram, cpu_pct, slice_name)
    props = None if caps is None else caps.props
    state: dict = {"stop": None, "pending": False, "reaped": False, "signals": 0}

    def forward(signum, _frame):
        state["signals"] += 1
        # The first signal asks; a repeat insists (a job may ignore SIGTERM).
        sig = signal.SIGTERM if state["signals"] == 1 else signal.SIGKILL
        if state["stop"] is None:
            state["pending"] = True  # arrived before the job existed: stop it once it does
        else:
            state["stop"](sig)

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
            weak = caps.unenforced + caps.unverified
            proc = subprocess.Popen(
                argv,
                pass_fds=(write_fd,),
                env=systemd_env(),
                start_new_session=True,
                # A cap the kernel is not applying gets at least the uncapped fallback's
                # nice 19 and data limit (--scope execs, so they carry over to the job).
                preexec_fn=_uncapped_limits(ram) if weak else None,
            )

            def stop_scoped(sig: int) -> None:
                # Signal the launch's process group too: before systemd registers the
                # scope it is still the systemd-run client, and a stop that arrives first
                # would find no unit. After registration this also signals the in-scope
                # reporter, so a job that ignores SIGTERM keeps running until the scope's
                # own stop timeout while `run` has already reported its exit.
                if not state["reaped"]:
                    kill_group(proc.pid, sig)
                stop_scope(unit)

            state["stop"] = stop_scoped
            print(f"genesis-job {unit}: started ({', '.join(props)})", file=sys.stderr)
            for cap in caps.unenforced:
                print(
                    f"genesis-job {unit}: WARNING: the {cap} cap is NOT enforced here "
                    f"({_UNENFORCED_WHY.get(cap, 'not applied in the scope')}); the job "
                    "is visible to other sessions, runs at nice 19 with a data limit, but "
                    "its estimate is not a hard limit",
                    file=sys.stderr,
                )
            for cap in caps.unverified:
                print(
                    f"genesis-job {unit}: WARNING: could not verify the {cap} cap "
                    f"({_UNVERIFIED_WHY.get(cap, 'its limit file was unreadable')}); the "
                    "job runs at nice 19 with a data limit as well",
                    file=sys.stderr,
                )
        else:
            base = resource.getrusage(resource.RUSAGE_CHILDREN)  # earlier children excluded
            try:
                proc = subprocess.Popen(
                    cmd, preexec_fn=_uncapped_limits(ram), start_new_session=True
                )
            except OSError as exc:
                print(f"genesis-job {name}: cannot start: {exc}", file=sys.stderr)
                os.close(read_fd)
                return 127
            # After the job is reaped its pgid may be reused: never signal it then.
            state["stop"] = lambda sig: None if state["reaped"] else kill_group(proc.pid, sig)
            print(
                f"genesis-job {name}: UNCAPPED (systemd user manager unreachable): "
                "nice 19 and a data-segment limit; invisible to other sessions",
                file=sys.stderr,
            )
        os.close(write_fd)
        write_fd = -1
        if state["pending"]:
            state["stop"](signal.SIGKILL if state["signals"] > 1 else signal.SIGTERM)
        done = threading.Event()
        watchdog = Watchdog(over, lambda: state["stop"](signal.SIGTERM))
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
    scoped = props is not None
    _report(name if not scoped else unit, rc, ram, raw, scoped, watchdog.fired,
            None if scoped else base)
    return rc


def _report(
    label: str,
    rc: int,
    ram: int,
    raw: bytes,
    scoped: bool,
    fired: str | None,
    base: resource.struct_rusage | None = None,
) -> None:
    parts = [f"exit {rc}"]
    if scoped:
        peak, cpu_s, ooms = parse_report(raw)
        if peak is not None:
            parts.append(f"peak {peak / 2**20:.0f} MiB of {ram / 2**20:.0f} MiB estimate")
    else:
        # rusage covers this wrapper's children: the largest single process, not a group total.
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu_s, ooms = usage.ru_utime + usage.ru_stime, None
        if base is not None:  # the probe and any earlier child are not this job
            cpu_s -= base.ru_utime + base.ru_stime
        parts.append(f"largest process {usage.ru_maxrss / 1024:.0f} MiB")
    if cpu_s is not None:
        parts.append(f"cpu {cpu_s:.2f}s")
    if ooms:
        parts.append("killed at its memory cap (raise --ram)")
    if fired:
        parts.append(f"stopped by the watchdog: {fired}")
    print(f"genesis-job {label}: " + ", ".join(parts), file=sys.stderr)
