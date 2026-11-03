"""Owned Linux process scopes for the disposable recovery controller only."""

import contextlib
import json
import os
import signal
import sys
import time
from pathlib import Path


class OwnershipError(ValueError):
    """A scope or survivor cannot be attributed to this fixture."""


def parse_stat(text):
    head, separator, tail = text.rpartition(")")
    if not separator:
        raise ValueError("invalid proc stat")
    pid = int(head.split(" (", 1)[0])
    fields = tail.split()
    if pid <= 0 or len(fields) < 20:
        raise ValueError("incomplete proc stat")
    return {"pid": pid, "state": fields[0], "ppid": int(fields[1]), "pgid": int(fields[2]),
            "sid": int(fields[3]), "start": int(fields[19])}


def census():
    rows = {}
    for path in Path("/proc").iterdir():
        if path.name.isdecimal():
            try:
                row = parse_stat((path / "stat").read_text())
            except (FileNotFoundError, ProcessLookupError):
                continue
            rows[row["pid"]] = row
    return rows


def descendants(rows, parent):
    found = {parent}
    while True:
        expanded = found | {pid for pid, row in rows.items() if row["ppid"] in found}
        if found == expanded:
            return found
        found = expanded


def group_alive(pgid):
    if type(pgid) is not int or pgid <= 1 or pgid == os.getpgrp():
        raise OwnershipError("invalid or controller process group")
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(predicate, process=None, seconds=60):
    # Same ceiling as the qualified core's child watchdog; measured native turns
    # were about2s. An intentionally held marker must be resolved by controller.
    deadline = time.monotonic() + seconds
    while not predicate():
        if process is not None and process.poll() is not None:
            raise RuntimeError("owned process exited before its declared barrier")
        if time.monotonic() >= deadline:
            raise RuntimeError("owned barrier or scope closure not observed")
        time.sleep(0.01)


def scopes(process, rows, child=None):
    outer = rows.get(process.pid)
    if (outer is None or outer["ppid"] != os.getpid() or outer["pgid"] != process.pid
            or outer["sid"] != process.pid or process.poll() is not None):
        raise OwnershipError("unbound outer process scope")
    groups = {process.pid}
    members = descendants(rows, process.pid)
    if child is not None:
        pid = child["pid"]
        if type(pid) is not int or pid <= 1 or child != {"pid": pid, "pgid": pid}:
            raise OwnershipError("invalid child receipt")
        if pid in rows:
            if pid not in members or rows[pid]["pgid"] != pid or rows[pid]["sid"] != pid:
                raise OwnershipError("unbound child process scope")
            groups.add(pid)
        elif group_alive(pid):
            raise OwnershipError("child leader gone with unresolved descendants")
    if any(rows[pid]["pgid"] not in groups for pid in members):
        raise OwnershipError("detached native descendant requires design discussion")
    for pgid in groups:
        group_alive(pgid)  # Validate all identities before any signal.
    return groups


def stop(process, groups):
    # `groups` must come from scopes() while the fresh owned outer is alive.
    if process.pid not in groups:
        raise OwnershipError("outer group absent from proven scopes")
    for pgid in groups:
        group_alive(pgid)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=60)
    for pgid in groups - {process.pid}:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
    time.sleep(0.1)
    for pgid in groups:
        if group_alive(pgid):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
    # An adopted child can still hold the outer's pipe writers. Terminate all
    # proven groups before draining; reaping the direct outer alone is not EOF.
    stdout, stderr = process.communicate(timeout=60)
    wait_for(lambda: all(not group_alive(g) for g in groups))
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr, "timed_out": False}


def init_identity(controller="/fixture/ego_owned_recovery.py"):
    init_argv = [v.decode() for v in Path("/proc/1/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    controller_argv = [v.decode() for v in Path("/proc/self/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    executable = str(Path("/proc/1/exe").readlink())
    controller_exe = str(Path("/proc/self/exe").readlink())
    identity = {"controller": os.getpid(), "parent": os.getppid(), "init_executable": executable,
                "init_argv": init_argv, "controller_executable": controller_exe, "controller_argv": controller_argv}
    if (os.getpid() == 1 or os.getppid() != 1 or not all(init_argv) or not all(controller_argv)
            or Path(init_argv[0]).name not in {"docker-init", "tini"}
            or Path(executable).name != Path(init_argv[0]).name
            or "--" not in init_argv or init_argv[init_argv.index("--") + 1:] != controller_argv
            or len(controller_argv) != 2 or controller_argv[1] != controller
            or Path(controller_exe).resolve() != Path(sys.executable).resolve()
            or Path(controller_argv[0]).name not in {Path(sys.executable).name, Path(controller_exe).name}):
        raise OwnershipError("expected container init/controller topology missing: " + json.dumps(identity))
    return identity
