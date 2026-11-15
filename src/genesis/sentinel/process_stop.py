"""Stop a single runaway process inside the container.

A single process can grow until the container runs out of memory, with no
child processes to blame (MEASURED on a live install, 2026-11-14: one
interactive Codex CLI grew from about 4.8 GB to 11.7 GB of RSS plus 2.7 GB of
swap in about two hours). The health alarm ``infra:process_memory_high`` names
it early, and this tool is the Sentinel's deterministic remedy for it.

DRY-RUN BY DEFAULT. It signals only with ``--execute``, which only the action
the owner approves is meant to carry. As with ``codex_mcp_stop``, that boundary
is the Sentinel prompt's rule, not an enforcement: the tool adds no capability
the dispatched session lacks, it makes the approved remedy deterministic.

A target is a process that is ALL of:

- this user's, and not pid 1;
- holding at least ``process_stop.propose_at_mb`` MB of RSS plus swap;
- not in a systemd ``.service`` unit and not ``init.scope`` (a service is
  restarted with systemctl, never killed: that keeps the Genesis server,
  Qdrant, the serena servers and the user manager out). Claude Code sessions,
  including the server-dispatched ones in ``run-*.scope``, and the Codex
  app-server ARE eligible: the owner approves each named stop;
- not this tool's own process or one of its ancestors (only meaningful for a
  dry-run run from inside a session: the approved action runs in the server);
- not a zombie and not a kernel thread.

The stop is bound to WHAT THE OWNER SAW. ``--execute`` requires ``--only
PID:STARTTIME,...``, copied exactly from the dry-run, and signals only those
identities that are still targets when it runs. A process that crossed the
line after the dry-run is never stopped by that approval, and a pid reused by
another process never matches its start time. Signals go through a pidfd
opened after the start-time check, so the check and the signal name the same
process. Only the process itself is signalled, not its group: its children see
their parent exit. Anything the tool cannot read is skipped, never signalled.

Exit codes: 0 everything targeted is gone (or nothing to do), 3 partial (a
signal refused, a candidate unreadable, ``/proc`` unlistable), 4 the lever is
``off``. Lever: ``process_stop.mode`` in ``config/sentinel.yaml`` (+ the local
overlay), ``propose_only`` (default) or ``off``; an unknown value reads as
``off``. ``GENESIS_PROCESS_STOP_DISABLED=1`` is the env kill switch.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from genesis.sentinel.codex_mcp_stop import _SAFE_LABEL, _argv, _safe_label, _same_uid
from genesis.util import proc_io

logger = logging.getLogger(__name__)

MODE_PROPOSE_ONLY = "propose_only"
MODE_OFF = "off"
DEFAULT_PROPOSE_AT_MB = 6144
_ENV_KILL_SWITCH = "GENESIS_PROCESS_STOP_DISABLED"
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "sentinel.yaml"
_SECTION = "process_stop"
TERM_GRACE_S = 10.0
# A process holding many GB tears its memory down before it becomes a zombie,
# so a successful SIGKILL can still read as alive for a few seconds.
KILL_GRACE_S = 10.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_PARTIAL = 3
EXIT_DISABLED = 4

_WARNED_ONCE: set[str] = set()


@dataclass
class Target:
    pid: int
    starttime: int
    label: str
    held_kb: int  # RSS + swap
    unit: str


@dataclass
class Scan:
    targets: list[Target] = field(default_factory=list)
    uncertain: list[str] = field(default_factory=list)
    threshold_mb: int = 0


def _warn_once(key: str, msg: str, *args: object) -> None:
    if key not in _WARNED_ONCE:
        _WARNED_ONCE.add(key)
        logger.warning(msg, *args, exc_info=True)


def _section(path: Path | None) -> dict | None:
    """The ``process_stop`` section with the local overlay; None when the config
    cannot be read (callers read that as off)."""
    cfg_path = path or _CONFIG_PATH
    try:
        import yaml

        from genesis._config_overlay import merge_local_overlay

        raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
        raw = merge_local_overlay(raw or {}, cfg_path, strict=True)
    except Exception as exc:
        _warn_once(
            f"load:{cfg_path}:{type(exc).__name__}:{exc}",
            "Failed to load %s; process_stop is off",
            cfg_path,
        )
        return None
    if not isinstance(raw, dict):
        return None
    section = raw.get(_SECTION, {})
    return section if isinstance(section, dict) else {"mode": None}


def load_mode(path: Path | None = None) -> str:
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return MODE_OFF
    section = _section(path)
    if section is None:
        return MODE_OFF
    if not section:
        return MODE_PROPOSE_ONLY  # absent section: the shipped default
    mode = section.get("mode", MODE_PROPOSE_ONLY)
    return mode if mode in (MODE_PROPOSE_ONLY, MODE_OFF) else MODE_OFF


def load_propose_at_mb(path: Path | None = None) -> int | None:
    """MB of RSS plus swap at which one process is a target; None turns it off."""
    section = _section(path)
    if section is None:
        return None
    if "propose_at_mb" not in section:
        return DEFAULT_PROPOSE_AT_MB
    value = section["propose_at_mb"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _warn_once(
            f"mb:{value!r}", "process_stop.propose_at_mb=%r is not a positive integer; off", value
        )
        return None
    return value


def _held_kb(pid: int) -> int | None:
    """VmRSS + VmSwap in kB; None when unreadable. A kernel thread has neither
    line and reads as 0."""
    try:
        # Bytes, then a lenient decode: a process may name itself with any
        # non-NUL bytes, and the Name: line would make a strict read raise.
        text = (Path(proc_io._PROC) / str(pid) / "status").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    total = 0
    try:
        for line in text.splitlines():
            if line.startswith(("VmRSS:", "VmSwap:")):
                total += int(line.split()[1])
    except (ValueError, IndexError):
        return None
    return total


def _service_cgroup(pid: int) -> bool | None:
    """True when any component of the pid's cgroup path is a systemd service
    other than the user manager (``user@UID.service``, which holds every user
    unit), so a sub-cgroup a service delegated still counts as that service.
    None when the cgroup cannot be read."""
    try:
        text = (Path(proc_io._PROC) / str(pid) / "cgroup").read_text()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            parts = [c for c in line[3:].strip().split("/") if c]
            return any(c.endswith(".service") and not c.startswith("user@") for c in parts)
    return None


def _ancestors(pid: int, max_depth: int = 64) -> set[int]:
    """This process and every ancestor up to pid 1."""
    out = {pid}
    current = pid
    for _ in range(max_depth):
        st = proc_io.read_stat(current)
        if st is None or st[2] <= 1:
            break
        current = st[2]
        out.add(current)
    return out


def scan(threshold_mb: int, *, self_pid: int | None = None) -> Scan:
    """Every eligible process at or over ``threshold_mb``. Raises ``OSError``
    when ``/proc`` cannot be listed: an empty list would read as "nothing"."""
    result = Scan(threshold_mb=threshold_mb)
    protected = _ancestors(self_pid if self_pid is not None else os.getpid())
    threshold_kb = threshold_mb * 1024
    for name in sorted(os.listdir(proc_io._PROC), key=lambda n: (len(n), n)):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1 or pid in protected:
            continue
        try:
            target = _classify(pid, threshold_kb, result)
        except Exception as exc:  # one odd process must never abort the scan
            result.uncertain.append(f"{pid} (unreadable: {type(exc).__name__})")
            continue
        if target is not None:
            result.targets.append(target)
    result.targets.sort(key=lambda t: -t.held_kb)
    return result


def _classify(pid: int, threshold_kb: int, result: Scan) -> Target | None:
    """The pid as a Target, None when it is not one (an unreadable candidate
    is recorded in ``result.uncertain``)."""
    held = _held_kb(pid)
    if held is None or held < threshold_kb:
        return None  # cheap filter first: only big processes are read further
    st = proc_io.read_stat(pid)
    argv = _argv(pid)
    label = f"{pid} {_safe_label(argv or [])}"
    if st is None or argv is None:
        result.uncertain.append(f"{label} (stat or cmdline unreadable)")
        return None
    if st[1] == "Z" or not argv:
        return None  # a zombie holds nothing; no argv is a kernel thread
    owner = _same_uid(pid)
    if owner is None:
        result.uncertain.append(f"{label} (owner unreadable)")
        return None
    if not owner:
        return None
    unit = proc_io.systemd_unit(pid)
    in_service = _service_cgroup(pid)
    if unit is None or in_service is None:
        result.uncertain.append(f"{label} (cgroup unreadable)")
        return None
    if in_service or unit == "init.scope":
        return None  # a service (or the user manager): systemctl, never this tool
    return Target(
        pid=pid,
        starttime=st[4],
        label=_safe_label(argv),
        held_kb=held,
        unit=_SAFE_LABEL.sub("_", unit)[:60],
    )


def _alive(pid: int, starttime: int) -> bool:
    st = proc_io.read_stat(pid)
    return st is not None and st[1] != "Z" and st[4] == starttime


def _pidfd_kill(pid: int, starttime: int, sig: int) -> None:
    """Signal ``pid`` only if it is still the process that started at
    ``starttime``. The pidfd is opened BEFORE the start-time check, so the
    check and the signal name the same process even if the pid is reused in
    between. Raises ProcessLookupError when it is gone or is another process."""
    fd = os.pidfd_open(pid)
    try:
        if proc_io.read_starttime(pid) != starttime:
            raise ProcessLookupError(f"pid {pid} is no longer the approved process")
        signal.pidfd_send_signal(fd, sig)
    finally:
        os.close(fd)


def stop(
    targets: list[Target],
    *,
    kill: Callable[[int, int, int], None] = _pidfd_kill,
    alive: Callable[[int, int], bool] = _alive,
    sleep: Callable[[float], None] = time.sleep,
    grace_s: float = TERM_GRACE_S,
    kill_grace_s: float = KILL_GRACE_S,
) -> tuple[list[Target], list[str]]:
    """SIGTERM each target, then SIGKILL what is still the same process."""
    failures: list[str] = []
    signalled: list[Target] = []
    gone: list[Target] = []
    for t in targets:
        if t.pid <= 1:
            failures.append(f"{t.pid}: unsafe pid; skipped")
            continue
        if not alive(t.pid, t.starttime):
            gone.append(t)  # exited (or the pid was reused) since the scan
            continue
        try:
            kill(t.pid, t.starttime, signal.SIGTERM)
            signalled.append(t)
        except ProcessLookupError:
            gone.append(t)
        except OSError as exc:  # PermissionError, or a pidfd error: never lose the report
            failures.append(f"{t.pid}: SIGTERM refused ({exc})")
    deadline = time.monotonic() + grace_s
    while any(alive(t.pid, t.starttime) for t in signalled) and time.monotonic() < deadline:
        sleep(0.2)
    stopped: list[Target] = []
    for t in signalled:
        if alive(t.pid, t.starttime):
            try:
                kill(t.pid, t.starttime, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                failures.append(f"{t.pid}: SIGKILL refused ({exc})")
                continue
            kill_deadline = time.monotonic() + kill_grace_s
            while alive(t.pid, t.starttime) and time.monotonic() < kill_deadline:
                sleep(0.2)
        if alive(t.pid, t.starttime):
            failures.append(f"{t.pid}: still alive after SIGKILL")
        else:
            stopped.append(t)
    return gone + stopped, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m genesis.sentinel.process_stop",
        description="List (default) or stop single processes over the memory threshold.",
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually signal (default: dry-run)."
    )
    parser.add_argument(
        "--only",
        default=None,
        help="PID:STARTTIME[,PID:STARTTIME...] exactly as the dry-run prints it; "
        "required with --execute. Only these processes are ever signalled.",
    )
    args = parser.parse_args(argv)
    if load_mode() == MODE_OFF:
        print("process_stop: disabled (process_stop.mode is off); nothing signalled")
        return EXIT_DISABLED
    threshold = load_propose_at_mb()
    if threshold is None:
        print(
            "process_stop: disabled (process_stop.propose_at_mb is not set to a positive integer)"
        )
        return EXIT_DISABLED
    only = _parse_only(args.only) if args.only is not None else None
    if args.only is not None and only is None:
        print(f"process_stop: --only {args.only!r} is not PID:STARTTIME[,...]; nothing signalled")
        return EXIT_USAGE
    if args.execute and not only:
        print(
            "process_stop: --execute needs --only PID:STARTTIME[,...] copied from the dry-run "
            "the owner approved; nothing signalled"
        )
        return EXIT_USAGE

    try:
        found = scan(threshold)
    except OSError as exc:
        print(f"process_stop: FAILED: /proc could not be listed ({exc}); nothing signalled")
        return EXIT_PARTIAL
    held_mb = sum(t.held_kb for t in found.targets) / 1024
    # Summary first: the Sentinel's executor keeps only the head of stdout.
    verb = "stopping" if args.execute else "would stop"
    print(
        f"process_stop: {verb} {len(found.targets)} process(es) at or over {threshold} MB, "
        f"{held_mb:.0f} MB RSS+swap; {len(found.uncertain)} unreadable"
    )
    if not args.execute:
        if found.targets:
            ids = ",".join(f"{t.pid}:{t.starttime}" for t in found.targets)
            print(f"  approve with: python -m genesis.sentinel.process_stop --execute --only {ids}")
        for t in found.targets:
            started = proc_io.read_proc_start_iso(t.pid) or "unknown start"
            print(
                f"  pid {t.pid}: {t.held_kb / 1024:.0f} MB — {t.label} (in {t.unit}, started {started})"
            )
        for u in found.uncertain:
            print(f"  unreadable {u}")
        return EXIT_OK
    # Only the approved identities, and only while each is still a target. A
    # process that crossed the line since the dry-run is never in this list.
    approved = [t for t in found.targets if (t.pid, t.starttime) in only]
    not_targets = sorted(only - {(t.pid, t.starttime) for t in approved})
    stopped, failures = stop(approved)
    freed_mb = sum(t.held_kb for t in stopped) / 1024
    print(
        f"process_stop: stopped {len(stopped)} of {len(only)} approved process(es), "
        f"~{freed_mb:.0f} MB freed, {len(failures)} failed. Whatever they were doing is lost."
    )
    for t in stopped:
        print(f"  stopped pid {t.pid} — {t.label}")
    for pid, st in not_targets:
        # Gone, under the threshold now, or the pid belongs to another process.
        print(f"  not stopped {pid}:{st}: no longer an eligible target")
    for f in failures:
        print(f"  FAILED {f}")
    return EXIT_OK if not failures else EXIT_PARTIAL


def _parse_only(raw: str) -> set[tuple[int, int]] | None:
    """``PID:STARTTIME[,...]`` to a set; None when any item does not parse."""
    out: set[tuple[int, int]] = set()
    for item in raw.split(","):
        pid_s, sep, st_s = item.strip().partition(":")
        if not sep or not (
            pid_s.isascii() and pid_s.isdigit() and st_s.isascii() and st_s.isdigit()
        ):
            return None
        if int(pid_s) <= 1:
            return None
        out.add((int(pid_s), int(st_s)))
    return out or None


if __name__ == "__main__":
    sys.exit(main())
