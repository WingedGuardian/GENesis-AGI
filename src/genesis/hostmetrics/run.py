"""Launch an admitted job in a named, capped scope: ``genesis-job-<name>-<id>.scope``.

The scope carries the job's estimates as hard caps (``MemoryMax`` = RAM,
``CPUQuota`` = CPU), so it is both the enforcement and the ledger entry other
sessions read (see ``jobs.py``). The probe and the launch share one argv
builder: systemd-run rejects a property it cannot accept (exit 1), so a
property-free probe could pass while the real launch fails.

Missing ``systemd-run`` or an unreachable user manager refuses the launch.
A property systemd refuses is an error, never a silent downgrade. Named scopes
with weak enforcement retain their warnings and nice/data-limit fallback.
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
import tempfile
import threading
import time
from collections import deque
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

# Executed only after successful transient-scope registration. Isolated/no-site
# startup precedes this body; no project/site hook can run before the ACK.
# Report fd normalization is exclusive to the hostmetrics payload: its only
# other inherited descriptor is this registration writer, closed first.
_SCOPE_START = """
import os, signal, sys
ready, report = int(sys.argv[1]), sys.argv[2]
ctype_present, ctype = sys.argv[3:5]
if os.write(ready, b'1') != 1:
    raise SystemExit('scope registration acknowledgment failed')
os.close(ready)
if report != '-':
    old = int(report)
    os.dup2(old, 3, inheritable=True)
    if old != 3:
        os.close(old)
for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
    number = getattr(signal, name, None)
    if number is not None:
        signal.signal(number, signal.SIG_DFL)
if ctype_present == '1':
    os.environ['LC_CTYPE'] = ctype
else:
    os.environ.pop('LC_CTYPE', None)
os.execvpe(sys.argv[5], sys.argv[5:], os.environ)
"""
_DEFAULT_RUNNER = subprocess.run


class ProbeRefused(Exception):
    """systemd refused the job's scope: a bad estimate or slice, not a missing manager."""


class Cancelled(Exception):
    """An owned invocation received caller cancellation; selected after cleanup."""


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


#: Preserve the default-branch placement used by the disk guardian's OOM accounting.
CAPPED_SLICE = "app-capped.slice"


def scope_argv(unit: str, props: list[str], slice_name: str | None, *trailing: str) -> list[str]:
    # --collect: a failed scope is unloaded instead of lingering under its name.
    argv = ["systemd-run", "--user", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    argv.append(f"--slice={slice_name or CAPPED_SLICE}")
    for prop in props:
        argv += ["-p", prop]
    return [*argv, "--", *trailing]


def _probe_call(argv, unit, runner, owned):
    """Normalize transport failures while preserving owner cancellation."""
    check_cancelled = owned.check_cancelled if owned is not None else lambda: None
    check_cancelled()
    try:
        probe = (owned.probe(argv, unit) if owned is not None else
                 runner(argv, capture_output=True, timeout=_SYSTEMD_TIMEOUT, env=systemd_env()))
    except FileNotFoundError:
        check_cancelled()
        return None  # systemd-run vanished after `which` found it
    except subprocess.TimeoutExpired:
        check_cancelled()
        # Injected runners own cleanup; an intended name grants no authority.
        raise ProbeRefused(
            f"the probe scope did not finish within {_SYSTEMD_TIMEOUT}s "
            "(a slow manager is not a missing one)"
        ) from None
    except OSError as exc:
        check_cancelled()
        raise ProbeRefused(f"systemd-run could not run: {exc}") from None
    check_cancelled()
    return probe


def choose_properties(
    unit: str,
    ram: int,
    cpu_pct: float,
    slice_name: str | None,
    runner: Callable[..., subprocess.CompletedProcess] = _DEFAULT_RUNNER,
    *,
    owned: OwnedProcess | None = None,
) -> Caps | None:
    """The caps a probe scope accepts; None only when no manager can be reached.

    None means `systemd-run` is absent or the bus is unreachable (MEASURED stderr:
    "Failed to connect to bus: …"). Anything ambiguous — a probe that times out, a
    `systemd-run` that cannot execute — raises ProbeRefused, because running the
    job uncapped on a guess is the outcome the probe exists to prevent. So does a
    refused property, unless it is an ``OOMPolicy`` systemd does not know (then
    the next rung drops it).
    """
    if owned is None and runner is _DEFAULT_RUNNER:
        with OwnedProcess() as supervised:
            return choose_properties(unit, ram, cpu_pct, slice_name, runner, owned=supervised)
    check_cancelled = owned.check_cancelled if owned is not None else lambda: None
    check_cancelled()
    if shutil.which("systemd-run") is None:
        check_cancelled()
        return None
    for rung, oom_continue in enumerate((True, False)):
        props = scope_properties(ram, cpu_pct, oom_continue)
        probe_unit = f"{unit}-probe{rung}"
        argv = scope_argv(probe_unit, props, slice_name, "/bin/sh", "-c", _ENFORCEMENT_SH)
        probe = _probe_call(argv, probe_unit, runner, owned)
        if probe is None:
            return None
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
    """Signal the owned launcher's group. pgid 0/1 could hit our own group or init."""
    if pgid > 1:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)


def _scope_quiescent(unit: str) -> bool | None:
    """Exact-scope completion, not acknowledgement of a stop request."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", f"{unit}.scope", "-p", "LoadState",
             "-p", "ActiveState", "-p", "ControlGroup", "-p", "TasksCurrent"],
            capture_output=True, text=True, timeout=_SYSTEMD_TIMEOUT, env=systemd_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    values = dict(line.partition("=")[::2] for line in result.stdout.splitlines() if "=" in line)
    if not {"LoadState", "ActiveState", "ControlGroup"} <= values.keys():
        return None
    absent = values["LoadState"] == "not-found" and values["ActiveState"] == "inactive"
    if result.returncode and not (absent and not values["ControlGroup"]):
        return None
    if values["ActiveState"] not in ("inactive", "failed"):
        return False
    return not values["ControlGroup"] or values.get("TasksCurrent") == "0"


def _signal_scope(unit: str, sig: int) -> bool:
    """v249+ defaults kill to all; TERM does not start a timed stop escalation."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "kill", f"--signal={sig}", f"{unit}.scope"],
            capture_output=True, timeout=_SYSTEMD_TIMEOUT, env=systemd_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 or _scope_quiescent(unit) is True


class OwnedProcess:
    """One main-thread CLI owner dispatches signals and consumes child status.

    Handler/watchdog producers only append. Linux WNOWAIT reserves the child's
    identity until numeric authority closes BEFORE final waitpid. Requires
    exclusive reaping and default SIGCHLD; arbitrary threaded/native embeddings
    and SA_NOCLDWAIT are unsupported. Normal detached-child behavior is retained.
    """

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.unit: str | None = None
        self.reaped = False
        self.signals = 0
        self.last_signal: int | None = None
        self._previous: dict = {}
        self._requests = deque()
        self._numeric_open = False
        self._finished = False
        self._finalizing = False
        self._registration_error = None
        self._exit = None
        self._sent: int | None = None
        self._numeric_sent: int | None = None
        self._scope_sent: int | None = None
        self._registration_fd = -1
        self._scope_authorized = False
        self._scope_retry_at = 0.0

    def __enter__(self):
        if sys.platform != "linux":
            raise ProbeRefused("process supervision requires Linux")
        if threading.current_thread() is not threading.main_thread():
            raise ProbeRefused("process supervision requires the main CLI thread")
        if signal.getsignal(signal.SIGCHLD) != signal.SIG_DFL:
            raise ProbeRefused("process supervision requires default SIGCHLD and exclusive reaping")
        required = ("waitid", "waitpid", "waitstatus_to_exitcode", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
        if not all(hasattr(os, name) for name in required):
            raise ProbeRefused("Linux non-reaping child supervision is unavailable")
        try:
            os.waitid(os.P_PID, os.getpid(), os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            pass  # our own PID is not a child; the syscall/flags work
        except OSError as exc:
            raise ProbeRefused(f"non-reaping child supervision is unavailable: {exc}") from None
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self._previous[sig] = signal.signal(sig, self._forward)
        except BaseException:
            self._restore()
            raise
        return self

    def _restore(self):
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)
        self._previous.clear()

    def _forward(self, signum, _frame):
        self._requests.append((True, signum))

    def stop(self, sig=signal.SIGTERM):
        """Thread-safe request only; watchdogs never dispatch or reap."""
        self._requests.append((False, sig))

    def checkpoint(self):
        if self._finished:
            return
        if threading.current_thread() is not threading.main_thread():
            raise ProbeRefused("only the main CLI thread may supervise children")
        while True:
            try:
                caller, value = self._requests.popleft()
            except IndexError:
                break
            if caller:
                self.signals += 1
                self.last_signal = value
                value = signal.SIGTERM if self.signals == 1 else signal.SIGKILL
            self._dispatch(value)
        self._receive_registration()
        self._dispatch_scope()

    def check_cancelled(self):
        self.checkpoint()
        if self.signals:
            raise Cancelled

    def outcome(self, code):
        """Call after context cleanup/finalization; no child-status substitution."""
        return 128 + self.last_signal if self.signals else code

    def bind(self, proc: subprocess.Popen, unit: str | None = None):
        """Bind numeric identity; a requested unit without ACK is unauthorized."""
        self._adopt(proc, unit, -1)
        self.checkpoint()
        if self.signals and self._sent is None:
            self._dispatch(signal.SIGTERM if self.signals == 1 else signal.SIGKILL)

    def _close_registration(self):
        if self._registration_fd != -1:
            descriptor, self._registration_fd = self._registration_fd, -1
            os.close(descriptor)

    def _adopt(self, proc, unit, registration_fd):
        if self.proc is not None and not self.reaped:
            raise ProbeRefused("cannot replace an unreaped owned child")
        self._close_registration()
        self.proc, self.unit = proc, unit
        self.reaped, self._numeric_open, self._exit, self._sent = False, True, None, None
        self._numeric_sent, self._scope_sent = None, None
        self._registration_fd = registration_fd
        self._scope_authorized, self._scope_retry_at = False, 0.0

    def start_scoped(self, argv, unit, *, report_fd=None, **options):
        """Spawn through the private registration gate; retain child on errors."""
        if self.proc is not None and not self.reaped:
            raise ProbeRefused("cannot spawn over an unreaped owned child")
        self.check_cancelled()
        payload_fds = tuple(options.pop("pass_fds", ()))
        if report_fd is not None and set(payload_fds) != {report_fd}:
            raise ProbeRefused("report normalization requires an exclusive report descriptor")
        separator = argv.index("--") + 1
        read_fd, write_fd = os.pipe()
        transferred = False
        try:
            os.set_blocking(read_fd, False)
            incoming_env = options.get("env")
            if incoming_env is None:
                incoming_env = os.environ
            wrapped = [*argv[:separator], sys.executable, "-I", "-S", "-c", _SCOPE_START,
                       str(write_fd), str(report_fd) if report_fd is not None else "-",
                       "1" if "LC_CTYPE" in incoming_env else "0",
                       incoming_env.get("LC_CTYPE", ""),
                       *argv[separator:]]
            self.check_cancelled()
            proc = subprocess.Popen(wrapped, pass_fds=(*payload_fds, write_fd), **options)
            self._adopt(proc, unit, read_fd)
            transferred = True
        finally:
            # No supervision checkpoint while the parent still holds a writer.
            os.close(write_fd)
            if not transferred:
                os.close(read_fd)
        self.checkpoint()
        if self.signals and self._sent is None:
            self._dispatch(signal.SIGTERM if self.signals == 1 else signal.SIGKILL)
        return proc

    def _receive_registration(self):
        try:
            self._read_registration()
        except (OSError, ProbeRefused) as error:
            if not self._finalizing:
                raise
            # Every cleanup checkpoint preserves the first proof error, while
            # independent numeric supervision continues until actual status reap.
            if self._registration_error is None:
                self._registration_error = error

    def _read_registration(self):
        if self._registration_fd == -1:
            return
        try:
            raw = os.read(self._registration_fd, 2)
        except BlockingIOError:
            return
        except OSError as exc:
            self._close_registration()
            raise ProbeRefused("scope registration acknowledgment could not be read") from exc
        if raw == b"1":
            self._scope_authorized = True
        elif raw:
            self._close_registration()
            raise ProbeRefused("scope registration acknowledgment is invalid")
        self._close_registration()  # EOF without proof never authorizes the name

    def _observe(self):
        if self._numeric_open:
            try:
                observed = os.waitid(
                    os.P_PID, self.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                )
            except ChildProcessError:
                self._numeric_open = False
                raise ProbeRefused("owned child wait status was consumed externally") from None
            if observed is not None:
                self._exit = observed
        return self._exit

    def _dispatch(self, sig):
        if self.proc is None:
            return
        if self._sent != signal.SIGKILL:
            self._sent = sig
        self._scope_retry_at = 0.0
        if self._numeric_open and self._numeric_sent != self._sent:
            self._observe()  # detect ownership loss; external competing reapers unsupported
            kill_group(self.proc.pid, self._sent)
            self._numeric_sent = self._sent
        self._receive_registration()
        self._dispatch_scope()

    def _dispatch_scope(self):
        if (not self._scope_authorized or self._sent is None or self._scope_sent == self._sent
                or time.monotonic() < self._scope_retry_at):
            return
        if not _signal_scope(self.unit, self._sent):
            print(f"genesis-job {self.unit}: scope signal was not verified", file=sys.stderr)
            self._scope_retry_at = time.monotonic() + 0.25
        else:
            self._scope_sent = self._sent

    def _wait_scope(self, deadline=None):
        while self._scope_authorized and self._sent is not None:
            self.checkpoint()
            complete = _scope_quiescent(self.unit)
            if complete is True:
                self._scope_authorized = False
                return
            if complete is None:
                raise ProbeRefused("owned scope cleanup could not be verified")
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.proc.args, _SYSTEMD_TIMEOUT)
            time.sleep(0.05)

    def wait(self, timeout=None):
        if self.proc is None:
            raise ProbeRefused("no owned child was bound")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.reaped:
            self.checkpoint()
            if self._observe() is not None:
                # An exited helper may have left its proof buffered in the pipe.
                # Consume that proof before deciding whether scope cleanup is owed.
                self.checkpoint()
                self._numeric_open = False  # BEFORE consuming status, including handler reentrancy
                try:
                    _, status = os.waitpid(self.proc.pid, 0)
                except ChildProcessError:
                    raise ProbeRefused("owned child final wait status was consumed externally") from None
                self.proc.returncode = os.waitstatus_to_exitcode(status)
                self.reaped = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.proc.args, timeout)
            time.sleep(0.05)
        self.checkpoint()
        self._wait_scope(deadline)
        return self.proc.returncode

    def exited(self):
        """Readiness may observe exit, never consume wait status."""
        self.checkpoint()
        return self._observe() is not None

    def probe(self, argv, unit):
        self.check_cancelled()
        with (
            tempfile.TemporaryDirectory(prefix="genesis-cap-probe-") as private,
            tempfile.TemporaryFile(dir=private) as out,
            tempfile.TemporaryFile(dir=private) as err,
        ):
            self.check_cancelled()
            self.start_scoped(argv, unit, stdout=out, stderr=err,
                              env=systemd_env(), start_new_session=True)
            try:
                code = self.wait(timeout=_SYSTEMD_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.checkpoint()
                if not self.signals:
                    self.stop(signal.SIGKILL)
                    self.wait(timeout=_SYSTEMD_TIMEOUT)
                else:
                    self.wait()  # caller chose cooperative cancellation, no timed force
                self.check_cancelled()
                raise
            self.check_cancelled()
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(argv, code, out.read(), err.read())

    def __exit__(self, exc_type, _exc, _tb):
        self._finalizing = True
        try:
            self.checkpoint()
            if self.proc is not None and not self.reaped:
                if not self.signals:
                    self.stop(signal.SIGKILL)
                    self.wait(timeout=_SYSTEMD_TIMEOUT)
                else:
                    self.wait()  # repeat caller signal escalates, first remains cooperative
            self.checkpoint()
            self._wait_scope()
        except (OSError, ProbeRefused, subprocess.TimeoutExpired):
            print("genesis-job: cleanup incomplete; owned process/scope exit was not verified", file=sys.stderr)
            if exc_type is None:
                raise
        finally:
            self._numeric_open = False
            # Freeze caller outcome without a handler event landing between the
            # final drain and restoration. Pending kernel signals after this
            # checkpoint are delivered to the restored handlers, outside this
            # invocation. Never hold the mask during cooperative shutdown.
            while True:
                previous_mask = signal.pthread_sigmask(
                    signal.SIG_BLOCK, (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
                )
                try:
                    if not self._requests:
                        try:
                            self._close_registration()
                        except OSError as error:
                            if self._registration_error is None:
                                self._registration_error = error
                        self._scope_authorized = False
                        self._finished = True
                        self._restore()
                        break
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                try:
                    self.checkpoint()
                    self._wait_scope()
                except (OSError, ProbeRefused, subprocess.TimeoutExpired):
                    print("genesis-job: final scope cleanup remains unverified", file=sys.stderr)
            if self._registration_error is not None:
                print(f"genesis-job: scope registration unverified: {self._registration_error}", file=sys.stderr)
        if self._registration_error is not None and exc_type is None:
            raise self._registration_error


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
    unit = unit_name(name, secrets.token_hex(16))
    read_fd, write_fd = -1, -1
    done = threading.Event()
    owned = OwnedProcess()
    report = None
    try:
        with owned:
            try:
                caps = choose_properties(unit, ram, cpu_pct, slice_name, owned=owned)
                owned.check_cancelled()
                if caps is None:
                    raise ProbeRefused("systemd user manager unavailable; refusing uncapped launch")
                props = caps.props
                read_fd, write_fd = os.pipe()
                os.set_blocking(read_fd, False)
                argv = scope_argv(
                    unit, props, slice_name, "/bin/sh", "-c", _REPORT_SH,
                    "genesis-job", "3", *cmd,
                )
                weak = caps.unenforced + caps.unverified
                owned.check_cancelled()
                owned.start_scoped(
                    argv, unit, report_fd=write_fd, pass_fds=(write_fd,),
                    env=systemd_env(), start_new_session=True,
                    preexec_fn=_uncapped_limits(ram) if weak else None,
                )
                print(f"genesis-job {unit}: started ({', '.join(props)})", file=sys.stderr)
                for cap in caps.unenforced:
                    print(
                        f"genesis-job {unit}: WARNING: the {cap} cap is NOT enforced here "
                        f"({_UNENFORCED_WHY.get(cap, 'not applied in the scope')}); the job "
                        "is visible to other sessions, runs at nice 19 with a data limit, but "
                        "its estimate is not a hard limit", file=sys.stderr,
                    )
                for cap in caps.unverified:
                    print(
                        f"genesis-job {unit}: WARNING: could not verify the {cap} cap "
                        f"({_UNVERIFIED_WHY.get(cap, 'its limit file was unreadable')}); the "
                        "job runs at nice 19 with a data limit as well", file=sys.stderr,
                    )
                os.close(write_fd)
                write_fd = -1
                watchdog = Watchdog(over, owned.stop)
                threading.Thread(target=watchdog.run, args=(done,), daemon=True).start()
                rc = owned.wait()
                try:
                    raw = os.read(read_fd, 512)
                except BlockingIOError:
                    raw = b""
                rc = 128 - rc if rc < 0 else rc
                report = (unit, ram, raw, True, watchdog.fired, None)
            finally:
                done.set()
                if read_fd != -1:
                    os.close(read_fd)
                if write_fd != -1:
                    os.close(write_fd)
    except Cancelled:
        rc = 0  # caller outcome is selected only after context cleanup
    except (OSError, ProbeRefused, subprocess.TimeoutExpired):
        if owned.signals:
            rc = 0
        else:
            raise
    rc = owned.outcome(rc)
    if report is not None:
        label, estimate, raw, scoped, fired, base = report
        _report(label, rc, estimate, raw, scoped, fired, base)
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
        parts.append(f"watchdog stop requested: {fired}")
    print(f"genesis-job {label}: " + ", ".join(parts), file=sys.stderr)
