"""Best-effort queue-health timing, never indexing or recovery authority.

Only the fd8-serialized runner writes this small record. A queued notice is an
enqueue acknowledgement, not delivery or power-loss proof. A failed state write
can leave an older clock; reported spans are observations, not continuous samples.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path

THRESHOLD_NS = 2 * 3600 * 10**9
OUTCOMES = {"busy", "nonbusy", "error", "unobserved"}
PHASES = {
    "readiness",
    "inflight",
    "native",
    "pending-list",
    "claim",
    "disabled-claim",
    "escalation",
    "owner",
    "complete",
    "startup",
}
KINDS = {"prolonged_busy", "observation_error"}


def _integer(value):
    return type(value) is int and value >= 0


def _interval(value):
    if not isinstance(value, dict) or set(value) != {"boot_id", "first_ns", "last_ns", "phase"}:
        raise ValueError("invalid observation interval")
    if (
        str(uuid.UUID(value["boot_id"])) != value["boot_id"]
        or not _integer(value["first_ns"])
        or not _integer(value["last_ns"])
        or value["first_ns"] > value["last_ns"]
        or value["phase"] not in PHASES
    ):
        raise ValueError("invalid observation interval")


def _identity(kind, interval):
    raw = json.dumps([kind, interval], sort_keys=True).encode()
    return "code-intel-busy:" + hashlib.sha256(raw).hexdigest()


def _read(path):
    """Retain valid pending debt when only the active v1 clock is malformed."""
    empty = dict(version=1, active=None, pending={})
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if path.is_symlink():
            raise OSError("health record is a dangling symlink") from None
        return empty, False
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("health record is not regular")
        raw = stream.read(65537)
        if len(raw) > 65536:
            # Two finite notice classes fit comfortably. Refuse, never parse a
            # clipped record or replace an unread future format.
            raise OSError("oversized health record")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return empty, True
    if isinstance(value, dict) and type(value.get("version")) is int and value["version"] > 1:
        raise OSError("future health record version")
    try:
        if not isinstance(value, dict) or set(value) != {"version", "active", "pending"}:
            raise ValueError("invalid health record")
        if type(value["version"]) is not int or value["version"] != 1:
            raise ValueError("invalid health version")
        pending = value["pending"]
        if not isinstance(pending, dict):
            raise ValueError("invalid pending notices")
        malformed = False
        for kind, item in pending.items():
            try:
                if (
                    kind not in KINDS
                    or not isinstance(item, dict)
                    or set(item) != {"key", "count", "first", "latest"}
                ):
                    raise ValueError("invalid pending notice")
                _interval(item["first"])
                _interval(item["latest"])
                if (
                    not _integer(item["count"])
                    or item["count"] < 1
                    or item["key"] != _identity(kind, item["first"])
                ):
                    raise ValueError("invalid notice identity")
            except (ValueError, TypeError, AttributeError):
                malformed = True
            else:
                empty["pending"][kind] = item
        if malformed:
            return empty, True
        active = value["active"]
        if active is not None:
            if (
                not isinstance(active, dict)
                or set(active) != {"boot_id", "first_busy_ns", "last_busy_ns", "due_registered"}
                or type(active["due_registered"]) is not bool
            ):
                raise ValueError("invalid active clock")
            _interval(
                dict(
                    boot_id=active["boot_id"],
                    first_ns=active["first_busy_ns"],
                    last_ns=active["last_busy_ns"],
                    phase="readiness",
                )
            )
    except (ValueError, TypeError, AttributeError):
        return empty, True
    return value, False


def _publish(path, value):
    # Adopt index_marker._write_durable_event's file/replace/directory ordering.
    # fd8 excludes surviving writers. Retire only our owned regular temporaries;
    # repeated killed writes cannot accumulate a second unbounded health store.
    for old in path.parent.glob("." + path.name + ".*.tmp"):
        with contextlib.suppress(OSError):
            info = old.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid():
                old.unlink()
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def _due(pending, kind, interval):
    if kind in pending:
        pending[kind]["count"] += 1
        pending[kind]["latest"] = interval
    else:
        pending[kind] = dict(
            key=_identity(kind, interval), count=1, first=interval, latest=interval
        )


def observe(base, outcome, phase, enqueue, *, now_ns=None, boot_id=None, proc_root=Path("/proc")):
    """Advance timing and replay due notices, including after their episode ends."""
    if outcome not in OUTCOMES or phase not in PHASES:
        raise ValueError("unknown tick observation")
    if outcome == "unobserved":
        return True
    now = time.monotonic_ns() if now_ns is None else now_ns
    boot = (
        (proc_root / "sys/kernel/random/boot_id").read_text().strip()
        if boot_id is None
        else boot_id
    )
    current = dict(boot_id=boot, first_ns=now, last_ns=now, phase=phase)
    _interval(current)
    path = Path(base) / "code-intel-runner-busy.json"
    value, malformed = _read(path)
    active, pending = value["active"], value["pending"]
    if active and active["boot_id"] == boot and now < active["last_busy_ns"]:
        malformed, active = True, None
    if malformed:
        _due(pending, "observation_error", current)
    if outcome != "busy" or (active and active["boot_id"] != boot):
        active = None
    if outcome == "busy":
        if active is None:
            active = dict(boot_id=boot, first_busy_ns=now, last_busy_ns=now, due_registered=False)
        active["last_busy_ns"] = now
        if now - active["first_busy_ns"] >= THRESHOLD_NS and not active["due_registered"]:
            interval = dict(
                boot_id=boot, first_ns=active["first_busy_ns"], last_ns=now, phase=phase
            )
            _due(pending, "prolonged_busy", interval)
            active["due_registered"] = True
    value["active"] = active
    if active is None and not pending and not path.exists():
        return True
    _publish(path, value)  # Persist debt before notification or acknowledgement.
    complete = True
    for kind in sorted(list(pending)):
        item = pending[kind]
        if enqueue(kind, item, holders(Path(base), proc_root)) is not True:
            complete = False
            continue
        del pending[kind]
        _publish(path, value)  # Failure preserves actual outcome; no fictional rollback.
    return complete


def _safe(raw):
    # wg_proc._safe's rendering rule; comm is process-chosen display text.
    return (
        "".join(
            "?"
            if unicodedata.category(ch)[0] == "C"
            or unicodedata.category(ch) in ("Zl", "Zp")
            or ch == "\ufffd"
            else ch
            for ch in raw.decode("utf-8", "replace")
        )
        or "?"
    )


def _descriptor_locks(proc_root, pid, objects):
    """Join visible identity to an actual per-descriptor kernel lock record.

    On a measured layered filesystem stat/fstat report a different device from
    /proc/locks. An open descriptor alone is insufficient: require fdinfo's exact
    lock record and stable visible file identity across that read.
    """
    result = {}
    try:
        entries = list((proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        try:
            before = entry.stat()
            identity = (os.major(before.st_dev), os.minor(before.st_dev), before.st_ino)
            name = objects.get(identity)
            if name is None:
                continue
            rows = (proc_root / str(pid) / "fdinfo" / entry.name).read_text().splitlines()
            after = entry.stat()
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                continue
        except OSError:
            continue
        for row in rows:
            fields = row.split()
            if (
                len(fields) == 9
                and fields[0] == "lock:"
                and fields[2] in {"POSIX", "FLOCK", "OFDLCK"}
            ):
                result[tuple(fields[2:])] = name
    return result


def holders(base, proc_root=Path("/proc")):
    """Contemporaneous kernel lock owners, not open-FD guesses or stable identities."""
    objects = {}
    lines = [
        "Current lock snapshot only, not stable process identities; PID namespace visibility may omit holders."
    ]
    for name in ("queue.sqlite3", "queue.sqlite3-journal", ".queue.lock", ".queue-db-init.lock"):
        try:
            info = (base / "index-requests" / name).stat()
            objects[(os.major(info.st_dev), os.minor(info.st_dev), info.st_ino)] = name
        except OSError:
            lines.append(name + ": unavailable or absent")
    found = set()
    descriptor_cache = {}
    try:
        with (proc_root / "locks").open() as stream:
            for line in stream:
                fields = line.split()
                if len(fields) != 8 or fields[1] not in {"POSIX", "FLOCK", "OFDLCK"}:
                    continue  # In particular, blocked-waiter `->` rows are not holders.
                try:
                    major, minor, inode = fields[5].split(":")
                    name = objects.get((int(major, 16), int(minor, 16), int(inode)))
                    pid = int(fields[4])
                except ValueError:
                    continue
                if name is None and pid > 0:
                    if pid not in descriptor_cache:
                        descriptor_cache[pid] = _descriptor_locks(proc_root, pid, objects)
                    name = descriptor_cache[pid].get(tuple(fields[1:]))
                if name is None:
                    continue
                found.add(name)
                if pid <= 0:
                    lines.append(name + ": owner unknown (open-file-description lock)")
                    continue
                try:
                    comm = _safe((proc_root / str(pid) / "comm").read_bytes().removesuffix(b"\n"))
                except OSError:
                    comm = "? (exited or unreadable)"
                lines.append(f"{name}: PID {pid}, kernel comm {comm}; {fields[1]} {fields[3]}")
    except OSError:
        lines.append("Kernel lock list unavailable; holder unknown.")
    for name in sorted(set(objects.values()) - found):
        lines.append(name + ": no holder visible; this does not prove unlocked")
    return "\n".join(lines)


def _enqueue(captured, kind, item, snapshot):
    first, latest = item["first"], item["latest"]
    attempted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = (
        f"Enqueue-attempt snapshot time: {attempted}.\n"
        f"Retained notice: {kind}; observed intervals represented: {item['count']}.\n"
        f"First: boot {first['boot_id']}, monotonic {first['first_ns']}..{first['last_ns']}, "
        f"phase {first['phase']}.\nLatest: {json.dumps(latest, sort_keys=True)}.\n"
        "Spans cover consecutive observed busy ticks, not continuous samples. "
        "Timing writes may fail; continuity across failed writes is uncertain. "
        "Original holders were not retained. The following snapshot is current at this enqueue attempt.\n"
        + snapshot
    )
    base = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")).expanduser()
    environment = dict(os.environ, GENESIS_ALERT_QUEUE_ROOT=str(base / "alerts/queue"))
    command = [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-p",
        "-c",
        captured.buffers["scripts/lib/alert_queue.sh"].decode() + '\nqueue_alert_try "$@"',
        "code-intel-busy",
        "warning",
        "code_intel",
        "Code-intel queue health",
        body,
        item["key"],
    ]
    # No pass_fds: the notifier receives neither fd8 nor the source capsule.
    # The coordinator owns the whole worker process group's deadline.
    return (
        subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            check=False,
        ).returncode
        == 0
    )


def worker(captured, outcome, phase):
    base = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")).expanduser()

    def enqueue(kind, item, snapshot):
        return _enqueue(captured, kind, item, snapshot)

    try:
        return 0 if observe(base, outcome, phase, enqueue) else 76
    except (OSError, ValueError, TypeError) as error:
        print("code-intel: health timing unavailable: " + type(error).__name__, file=sys.stderr)
        # Storage may not retain an obligation. Attempt diagnosis; never claim persistence.
        now = time.monotonic_ns()
        interval = dict(
            boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            first_ns=now,
            last_ns=now,
            phase=phase,
        )
        pending = {}
        _due(pending, "observation_error", interval)
        # No writable episode record: one unavailable-monitor identity per root/boot.
        # Keep attempt timing in the body; the existing outreach window owns repeats.
        pending["observation_error"]["key"] = "code-intel-health-unavailable:" + hashlib.sha256(
            json.dumps([os.path.abspath(base), interval["boot_id"]]).encode()
        ).hexdigest()
        enqueue("observation_error", pending["observation_error"], holders(base))
        return 76
