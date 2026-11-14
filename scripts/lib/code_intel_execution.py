"""Explicit in-scope execution facts; no automatic caller, queue or kill authority.

The caller owns admission, an invocation-private pipe and bounded scope cleanup.
A delivered report is not proof of cleanup or permission to retry. Unsupported
proof refuses before launch; lost proof after launch remains uncertain.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import runpy
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_ADMISSION = globals().get("_CAPTURED_ADMISSION")
if _ADMISSION is None:
    _ADMISSION = runpy.run_path(str(Path(__file__).with_name("code_intel_cbm_admission.py")))
_NUMBER = _ADMISSION["number"]
_MAX = _ADMISSION["MAX_NUMBER"]
_EVENTS = {"low", "high", "max", "oom", "oom_kill"}
_LIMIT = 4096  # One atomic Linux pipe record; refuse rather than truncate facts.
_IDENTITY = (
    "version",
    "leaf",
    "root",
    "reporter",
    "membership",
    "mounts_sha256",
    "memory_max",
    "swap_max",
    "oom_score_adj",
)
_REPORT_KEYS = {
    "version",
    "outcome",
    "payload_started",
    "payload_returncode",
    "before",
    "after",
    "finality",
    "error",
}


def _events(path: Path) -> dict[str, int]:
    result = {}
    for row in path.read_text().splitlines():
        key, value = row.split()
        if key in result:
            raise ValueError("duplicate memory event")
        result[key] = _NUMBER(value, key)
    if not result.keys() >= _EVENTS:
        raise ValueError("missing memory event proof")
    return result


def _identity(path: Path) -> dict:
    info = path.stat()
    return {"path": str(path), "device": info.st_dev, "inode": info.st_ino}


def mount_digest(path: Path) -> str:
    mounts = []
    for row in path.read_text().splitlines():
        _, separator, right = row.partition(" - ")
        parts = right.split()
        if separator and parts and parts[0] == "cgroup2":
            if len(parts) != 3 or "memory_localevents" in parts[2].split(","):
                raise ValueError("hierarchical memory events unavailable")
            mounts.append(row)
    if not mounts:
        raise ValueError("hierarchical v2 proof required")
    return hashlib.sha256("\n".join(sorted(mounts)).encode()).hexdigest()


def _process_identity(directory: Path, pid: int):
    """Parse PID/start/parent/state; this alone proves no wait or process end."""
    process = (directory / "stat").read_text()
    tail = process.rpartition(") ")[2].split()
    if _NUMBER(process.split(" ", 1)[0], "reporter pid") != pid or len(tail) < 20:
        raise ValueError("reporter process identity mismatch or start unavailable")
    return pid, _NUMBER(tail[19], "reporter start"), _NUMBER(tail[1], "reporter parent"), tail[0]


def _process_state(directory: Path, pid: int, *, allow_zombie=False):
    """PID/start/parent with the existing live-subject/OwnedChild contract.

    Only an actual OwnedChild may retain Z identity; default live scope proof
    still refuses it. Identity alone never supplies a terminal wait result.
    https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html
    """
    if type(allow_zombie) is not bool:
        raise ValueError("invalid owned zombie identity selection")
    identity = _process_identity(directory, pid)
    if identity[3] in ("X", "x") or (identity[3] == "Z" and not allow_zombie):
        raise ValueError("scope subject has already ended")
    return identity[:3]


class ScopeProbe:
    """Read actual scope proof; proc injection is a Python fixture seam only."""

    def __init__(self, unit: str, cap: int, *, proc: Path = Path("/proc"), route=None):
        self.unit, self.cap, self.proc, self.route = unit, cap, proc, route
        if route is not None:
            if (type(route) is not tuple or len(route) != 2
                    or not all(isinstance(value, str) for value in route)
                    or re.fullmatch(r"[A-Za-z0-9_.\\-]+\.slice", route[1]) is None
                    or cap != 8 * 1024**3):
                raise ValueError("invalid queued scope route or cap")
            expected = re.fullmatch(r"code-intel-[a-f0-9]{12}-(cbm|gitnexus)-[0-9]+", unit)
            if expected is None or not (
                route == ("workload", "genesis-workload.slice")
                or (route == ("capped", "app-capped.slice") and expected[1] == "gitnexus")
                or (route[0] == "inherit" and expected[1] == "cbm")
            ):
                raise ValueError("queued route does not match provider")

    def capture(self, *, pid=None) -> dict:
        if pid is not None and (type(pid) is not int or not 0 < pid < 2**32):
            raise ValueError("invalid explicit scope subject")
        subject = self.proc / ("self" if pid is None else str(pid))
        actual_pid = os.getpid() if pid is None else pid
        process = _process_state(subject, actual_pid)
        if pid is not None and process[2] != os.getpid():
            raise ValueError("scope subject is not the original owner's direct child")
        boot_path = self.proc / "sys/kernel/random/boot_id"
        boot = str(uuid.UUID(boot_path.read_text().strip()))
        member_path, mount_path = subject / "cgroup", subject / "mountinfo"
        membership = member_path.read_text()
        if self.route is None:
            _ADMISSION["verify_workload_scope"](
                self.unit,
                self_path=member_path,
                mountinfo_path=mount_path,
            )
        leaf, root, version = _ADMISSION["resolve_cgroup"](member_path, mount_path)
        if self.route is not None and (leaf == root or leaf.name != self.unit + ".scope"
                                       or leaf.parent.name != self.route[1]):
            raise ValueError("batch is outside its explicitly reserved slice and scope")
        mounts_sha256 = mount_digest(mount_path)
        if pid is not None and mounts_sha256 != mount_digest(self.proc / "self/mountinfo"):
            raise ValueError("scope subject has another visible cgroup mount mapping")
        if version != 2:
            raise ValueError("hierarchical v2 proof required")
        leaf_identity = _identity(leaf)
        root_identity = _identity(root)
        if pid is not None:
            _, owner_root, owner_version = _ADMISSION["resolve_cgroup"](self.proc / "self/cgroup", self.proc / "self/mountinfo")
            if owner_version != version or _identity(owner_root) != root_identity:
                raise ValueError("scope subject has another visible cgroup root")
        memory_max = _NUMBER((leaf / "memory.max").read_text(), "memory.max")
        swap_max = _NUMBER((leaf / "memory.swap.max").read_text(), "memory.swap.max")
        adjustment = int((subject / "oom_score_adj").read_text())
        if memory_max != self.cap or swap_max != 0 or not -1000 <= adjustment < 1000:
            raise ValueError("scope limit or reporter protection mismatch")
        reporter = {
            "pid": actual_pid, "start_ticks": process[1], "boot_id": boot,
        }
        result = {
            "version": version,
            "leaf": leaf_identity,
            "root": root_identity,
            "reporter": reporter,
            "membership": membership,
            "mounts_sha256": mounts_sha256,
            "memory_max": memory_max,
            "swap_max": swap_max,
            "oom_score_adj": adjustment,
            "events": _events(leaf / "memory.events"),
            "local_events": _events(leaf / "memory.events.local"),
            "ancestors": [],
        }
        parent = leaf.parent
        while parent == root or root in parent.parents:
            diagnostic = {"identity": _identity(parent), "events": None, "error": None}
            try:
                diagnostic["events"] = _events(parent / "memory.events")
            except (OSError, ValueError):
                diagnostic["error"] = "ancestor_events_unavailable"
            result["ancestors"].append(diagnostic)
            if parent == root:
                break
            parent = parent.parent
        if (_identity(leaf) != leaf_identity or _identity(root) != root_identity
                or _process_state(subject, actual_pid) != process
                or str(uuid.UUID(boot_path.read_text().strip())) != boot
                or member_path.read_text() != membership or mount_digest(mount_path) != mounts_sha256):
            raise ValueError("scope subject or kernel identity changed while sampling")
        return result

    def _threads(self, facts: dict) -> list[int]:
        directory = self.proc / "self/task"
        threads = sorted(_NUMBER(path.name, "reporter tid") for path in directory.iterdir())
        if not threads or any(tid <= 0 for tid in threads):
            raise ValueError("reporter task identity unavailable")
        for tid in threads:
            if (directory / str(tid) / "cgroup").read_text() != facts["membership"]:
                raise ValueError("reporter thread outside validated leaf")
        return threads

    def finality(self, facts: dict) -> dict | None:
        if facts["reporter"]["pid"] != os.getpid():
            raise ValueError("finality requires the current reporter")
        leaf = Path(facts["leaf"]["path"])
        if _identity(leaf) != facts["leaf"]:
            raise ValueError("scope identity changed")
        threads = self._threads(facts)
        first = _NUMBER((leaf / "pids.current").read_text(), "subtree tasks")
        processes = set()
        pending = [leaf]
        while pending:
            directory = pending.pop()
            for value in (directory / "cgroup.procs").read_text().splitlines():
                pid = _NUMBER(value, "scope pid")
                if not pid:
                    raise ValueError("invalid scope pid")
                processes.add(pid)
            for child in directory.iterdir():
                kind = child.lstat().st_mode
                if stat.S_ISLNK(kind):
                    raise ValueError("unexpected scope symlink")
                if stat.S_ISDIR(kind):
                    pending.append(child)
        last = _NUMBER((leaf / "pids.current").read_text(), "subtree tasks")
        if self._threads(facts) != threads:
            raise ValueError("reporter task population changed")
        if first != last or last != len(threads) or processes != {os.getpid()}:
            return None
        return {"threads": threads, "processes": sorted(processes), "pids_current": last}


def _encode(report: dict) -> bytes:
    before, after = report.get("before"), report.get("after")
    if (
        isinstance(before, dict)
        and isinstance(after, dict)
        and all(before.get(key) == after.get(key) for key in _IDENTITY)
        and [item["identity"] for item in before["ancestors"]]
        == [item["identity"] for item in after["ancestors"]]
    ):
        # Identity was checked at both samples. Encode equality explicitly once,
        # retaining every counter/diagnostic and expanding losslessly on read.
        report = {
            **report,
            "after": {
                "same_scope_as_before": True,
                "events": after["events"],
                "local_events": after["local_events"],
                "ancestors": [
                    {"events": item["events"], "error": item["error"]}
                    for item in after["ancestors"]
                ],
            },
        }
    return (
        json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _worst_case(value):
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return -_MAX
    if isinstance(value, dict):
        return {key: _worst_case(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_worst_case(item) for item in value]
    return value


def _bounded_by(before: dict, after: dict) -> bool:
    """True when ``after`` has the shape the pre-launch worst-case size assumed.

    The bound encodes ``after`` as the same scope as ``before`` with the same
    counter keys; a changed identity or population could encode larger, so such
    facts are not retained (the report stays uncertain without them).
    """
    return (
        all(before[key] == after[key] for key in _IDENTITY)
        and before["events"].keys() == after["events"].keys()
        and before["local_events"].keys() == after["local_events"].keys()
        and len(before["ancestors"]) == len(after["ancestors"])
        and all(
            old["identity"] == new["identity"]
            and old["error"] == new["error"]
            and (old["events"] is None) == (new["events"] is None)
            and (old["events"] is None or old["events"].keys() == new["events"].keys())
            for old, new in zip(before["ancestors"], after["ancestors"], strict=True)
        )
    )


def _complete(before: dict, after: dict) -> None:
    if any(before[key] != after[key] for key in _IDENTITY) or [
        item["identity"] for item in before["ancestors"]
    ] != [item["identity"] for item in after["ancestors"]]:
        raise ValueError("scope identity or policy changed")
    for label in ["events", "local_events"]:
        if before[label].keys() != after[label].keys() or any(
            after[label][key] < value for key, value in before[label].items()
        ):
            raise ValueError("counter reset or population changed")


def _integer(value, *, positive: bool = False) -> bool:
    return type(value) is int and (1 if positive else 0) <= value <= _MAX


def _mapping(value) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"path", "device", "inode"}
        and isinstance(value["path"], str)
        and Path(value["path"]).is_absolute()
        and ".." not in Path(value["path"]).parts
        and "\x00" not in value["path"]
        and _integer(value["device"])
        and _integer(value["inode"], positive=True)
    )


def _counter_record(value) -> bool:
    return (
        isinstance(value, dict)
        and value.keys() >= _EVENTS
        and all(
            isinstance(key, str) and key.isidentifier() and _integer(item)
            for key, item in value.items()
        )
    )


def _facts(value) -> bool:
    fields = {*_IDENTITY, "events", "local_events", "ancestors"}
    if not isinstance(value, dict) or set(value) != fields:
        return False
    reporter = value["reporter"]
    if (
        type(value["version"]) is not int
        or value["version"] != 2
        or not _mapping(value["leaf"])
        or not _mapping(value["root"])
        or not isinstance(reporter, dict)
        or set(reporter) != {"pid", "start_ticks", "boot_id"}
        or not _integer(reporter["pid"], positive=True)
        or not _integer(reporter["start_ticks"], positive=True)
    ):
        return False
    try:
        if (
            type(reporter["boot_id"]) is not str
            or str(uuid.UUID(reporter["boot_id"])) != reporter["boot_id"]
        ):
            return False
    except ValueError:
        return False
    if (
        not isinstance(value["membership"], str)
        or not value["membership"].startswith("0::/")
        or not isinstance(value["mounts_sha256"], str)
        or len(value["mounts_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["mounts_sha256"])
        or not _integer(value["memory_max"], positive=True)
        or type(value["swap_max"]) is not int
        or value["swap_max"] != 0
        or type(value["oom_score_adj"]) is not int
        or not -1000 <= value["oom_score_adj"] < 1000
        or not _counter_record(value["events"])
        or not _counter_record(value["local_events"])
        or not isinstance(value["ancestors"], list)
    ):
        return False
    for ancestor in value["ancestors"]:
        if (
            not isinstance(ancestor, dict)
            or set(ancestor) != {"identity", "events", "error"}
            or not _mapping(ancestor["identity"])
        ):
            return False
        if ancestor["events"] is None:
            if ancestor["error"] != "ancestor_events_unavailable":
                return False
        elif not _counter_record(ancestor["events"]) or ancestor["error"] is not None:
            return False
    return True


def _payload_status(child: subprocess.Popen) -> int | None:
    """Only a real owned wait result supplies status; ECHILD stays unavailable."""
    pid, status = os.waitpid(child.pid, os.WNOHANG)
    if pid == 0:
        return None
    if pid != child.pid or not (os.WIFEXITED(status) or os.WIFSIGNALED(status)):
        raise ChildProcessError("terminal child status unavailable")
    child.returncode = os.waitstatus_to_exitcode(status)
    return child.returncode


@dataclass(frozen=True)
class QueuedLaunch:
    """Internal queued seam; caller retains diagnostics separately from facts."""

    baseline: dict
    before_spawn: Callable[[dict], str]
    after_spawn: Callable[[], None]
    pass_fds: tuple[int, ...] = ()
    env: dict[str, str] | None = None
    cwd: str | None = None
    diagnostics: list[str] = field(default_factory=list)

    @classmethod
    def _checked(cls, launch, before):
        if not isinstance(launch, cls) or not _facts(launch.baseline):
            raise ValueError("invalid registered baseline")
        _complete(launch.baseline, before)
        return launch

    def _spawn(self, command, before, deadline):
        try:
            acknowledgement = self.before_spawn(before)
        except RuntimeError as error:
            self.diagnostics.append(f"native_intent_callback_failed: {type(error).__name__}: {error}")
            raise ValueError("native intent callback did not authorize launch") from error
        if acknowledgement != "started":
            raise ValueError("first committed native intent not acknowledged")
        if time.monotonic() >= deadline:
            raise ValueError("deadline reached after native intent")
        return subprocess.Popen(command, close_fds=True, pass_fds=self.pass_fds,
                                env=self.env, cwd=self.cwd)

    def _released(self):
        try:
            self.after_spawn()
        except Exception as error:
            # observe already owns the child and records its creation. A release
            # failure stays diagnostic while actual completion is still observed.
            self.diagnostics.append(f"admission_release_failed: {type(error).__name__}: {error}")


def observe(
    probe: ScopeProbe, command: list[str], *, seconds: float, report_limit: int = _LIMIT,
    launch: QueuedLaunch | None = None,
) -> dict:
    """Produce facts, never classify a signal/exit code as OOM or claim cleanup.

    The run bound ends observation only. It never signals or reaps a payload
    still running at the deadline; stopping and cleanup belong to the caller's
    launcher, which should also pass an absolute executable path.
    """
    report = {
        "version": 1,
        "outcome": "refused",
        "payload_started": False,
        "payload_returncode": None,
        "before": None,
        "after": None,
        "finality": None,
        "error": "proof_unavailable",
    }
    child = None
    try:
        if not command or not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("invalid bounded invocation")
        if signal.getsignal(signal.SIGCHLD) == signal.SIG_IGN:
            raise ValueError("automatic child reaping prevents status proof")
        deadline = time.monotonic() + seconds
        before = probe.capture()
        if not _facts(before):
            raise ValueError("invalid scope facts")
        if launch is not None:
            launch = QueuedLaunch._checked(launch, before)
        finality = probe.finality(before)
        if finality is None:
            raise ValueError("scope already contains other tasks")
        # The size bound below is sized from this sample's reporter threads.
        launch_threads = len(finality["threads"])
        report["before"] = before
        bounded_after = {
            **before,
            "events": _worst_case(before["events"]),
            "local_events": _worst_case(before["local_events"]),
            "ancestors": [
                {**item, "events": _worst_case(item["events"])} for item in before["ancestors"]
            ],
        }
        maximum = {
            **report,
            "outcome": "uncertain",
            "payload_started": True,
            "payload_returncode": -_MAX,
            "after": bounded_after,
            "finality": _worst_case(finality),
            "error": "proof_unavailable",
        }
        if len(_encode(maximum)) > report_limit:
            raise ValueError("complete report cannot fit")
        if time.monotonic() >= deadline:
            raise ValueError("deadline reached before launch")
        child = (subprocess.Popen(command, close_fds=True) if launch is None
                 else launch._spawn(command, before, deadline))
        report.update(payload_started=True, outcome="uncertain")
        if launch is not None:
            launch._released()
        while True:
            status = _payload_status(child)
            if status is not None:
                report["payload_returncode"] = status
                break
            if time.monotonic() >= deadline:
                report["error"] = "deadline_reached"
                return report
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        while time.monotonic() < deadline:
            finality = probe.finality(before)
            if finality is not None:
                if len(finality["threads"]) != launch_threads:
                    raise ValueError("reporter task population changed")
                after = probe.capture()
                if not _facts(after) or not _bounded_by(before, after):
                    raise ValueError("final scope facts outside the checked report bound")
                report["after"] = after
                _complete(before, after)
                if probe.finality(after) is not None:
                    report.update(outcome="complete", finality=finality, error=None)
                    return report
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        report["error"] = "deadline_reached"
    except (OSError, ValueError):
        if child is not None:
            report.update(outcome="uncertain", payload_started=True)
    return report


def pipe_limit(fd: int) -> int:
    """Reject stdio aliases so the payload cannot inherit the report channel."""
    info = os.fstat(fd)
    if (
        fd < 3
        or not stat.S_ISFIFO(info.st_mode)
        or not os.readlink(f"/proc/self/fd/{fd}").startswith("pipe:[")
        or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_WRONLY
    ):
        raise ValueError("report requires a private writable anonymous pipe")
    for standard in range(3):
        try:
            other = os.fstat(standard)
        except OSError:
            continue
        if (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
            raise ValueError("report pipe aliases stdio")
    os.set_inheritable(fd, False)
    return min(_LIMIT, os.fpathconf(fd, "PC_PIPE_BUF"))


def write_report(fd: int, report: dict) -> None:
    limit = pipe_limit(fd)
    data = _encode(report)
    if len(data) > limit:
        raise ValueError("report exceeds atomic transport")
    os.set_blocking(fd, False)
    if os.write(fd, data) != len(data):
        raise OSError("incomplete report write")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate report field")
        result[key] = value
    return result


def _expand_after(report: dict) -> None:
    before, after = report["before"], report["after"]
    if not isinstance(after, dict) or "same_scope_as_before" not in after:
        return
    if (
        set(after) != {"same_scope_as_before", "events", "local_events", "ancestors"}
        or after["same_scope_as_before"] is not True
        or not _facts(before)
        or not isinstance(after["ancestors"], list)
        or len(after["ancestors"]) != len(before["ancestors"])
        or not all(
            isinstance(item, dict) and set(item) == {"events", "error"}
            for item in after["ancestors"]
        )
    ):
        raise ValueError("invalid shared scope reference")
    report["after"] = {
        **before,
        "events": after["events"],
        "local_events": after["local_events"],
        "ancestors": [
            {**original, **updated}
            for original, updated in zip(before["ancestors"], after["ancestors"], strict=True)
        ],
    }


def read_report(fd: int) -> dict:
    """Read one bounded record through EOF; the caller owns its wait deadline."""
    data = bytearray()
    while True:
        part = os.read(fd, _LIMIT + 1 - len(data))
        if not part:
            break
        data.extend(part)
        if len(data) > _LIMIT:
            raise ValueError("oversized report")
    if not data.endswith(b"\n") or b"\n" in data[:-1]:
        raise ValueError("missing or multiple report records")
    try:
        report = json.loads(data, object_pairs_hook=_unique)
    except (RecursionError, UnicodeError) as exc:
        raise ValueError("malformed report") from exc
    return validate_report(report)


def validate_report(report: dict) -> dict:
    """Validate the unchanged wire1 contract for pipe and durable consumers."""
    if (
        not isinstance(report, dict)
        or set(report) != _REPORT_KEYS
        or type(report["version"]) is not int
        or report["version"] != 1
        or report["outcome"] not in ["refused", "uncertain", "complete"]
        or type(report["payload_started"]) is not bool
        or (
            report["payload_returncode"] is not None
            and type(report["payload_returncode"]) is not int
        )
    ):
        raise ValueError("invalid report contract")
    _expand_after(report)
    if not all(report[key] is None or _facts(report[key]) for key in ["before", "after"]):
        raise ValueError("invalid scope facts")
    if report["outcome"] == "complete":
        if (
            report["payload_started"] is not True
            or report["error"] is not None
            or type(report["payload_returncode"]) is not int
            or not all(isinstance(report[key], dict) for key in ["before", "after", "finality"])
        ):
            raise ValueError("incomplete terminal proof")
        _complete(report["before"], report["after"])
        finality = report["finality"]
        if (
            set(finality) != {"threads", "processes", "pids_current"}
            or not isinstance(finality["threads"], list)
            or not finality["threads"]
            or not all(_integer(tid, positive=True) for tid in finality["threads"])
            or len(set(finality["threads"])) != len(finality["threads"])
            or not isinstance(finality["processes"], list)
            or not all(_integer(pid, positive=True) for pid in finality["processes"])
            or finality["processes"] != [report["before"]["reporter"]["pid"]]
            or report["before"]["reporter"]["pid"] not in finality["threads"]
            or not _integer(finality["pids_current"], positive=True)
            or finality["pids_current"] != len(finality["threads"])
        ):
            raise ValueError("invalid terminal task proof")
    elif report["outcome"] == "refused" and (
        report["payload_started"] or report["payload_returncode"] is not None
    ):
        raise ValueError("refusal cannot claim native execution")
    elif report["error"] not in ["proof_unavailable", "deadline_reached"]:
        raise ValueError("missing explicit uncertainty/refusal reason")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-fd", type=int, required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--cap-bytes", required=True)
    parser.add_argument("--seconds", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        limit = pipe_limit(args.report_fd)
        cap = _NUMBER(args.cap_bytes, "requested cap")
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        report = observe(
            ScopeProbe(args.unit, cap), command, seconds=args.seconds, report_limit=limit
        )
        write_report(args.report_fd, report)
    except (OSError, ValueError):
        print("code-intel: execution evidence transport failed", file=sys.stderr)
        return 125
    return 0  # Transport only; the bound report, not this status, describes work.


if __name__ == "__main__":
    raise SystemExit(main())
