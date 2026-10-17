"""Who holds the PID/task budget: a breakdown taken while it runs low.

The PID-budget alert (``awareness/loop.py::_check_pid_budget``) reports the
binding cgroup's ``pids.current`` against its ``pids.max``, which says THAT the
budget is nearly gone but not WHO holds it. By the time anyone reads the alert,
the processes that filled it may have exited, so this module records the
holders at the moment the budget runs low:

  - every cgroup under the binding level, to ``_CGROUP_DEPTH`` levels, with its
    ``pids.current`` (a cgroup's ``pids.current`` counts TASKS, i.e. threads, in
    its whole subtree);
  - every process in that subtree, grouped into trees, with the threads each
    tree holds. A tree is rooted at the nearest ``claude`` process (one Claude
    Code session and the MCP servers, tools and shells under it), or, for
    processes with no ``claude`` above them, at the topmost ancestor in the
    same cgroup (so each service or session scope is its own tree).

FORK-FREE: everything is read from ``/proc`` and the cgroup filesystem, because
the moment this runs is the moment a fork is most likely to fail. It reads only
world-readable files (``/proc/<pid>/stat``, ``cmdline``, ``cgroup``; the cgroup
``pids.current`` and ``cgroup.procs``), never ``environ``, so it works inside
genesis-server's systemd sandbox, where reading another process's ``environ``
is denied.

A process label is argv[0]'s basename, plus ``-m <module>`` or the script's
basename for an interpreter, and only when that word looks like a module or a
script path (no ``:`` or ``=``). Never any other argument: argv can carry
credentials.

Each snapshot is a JSON file under ``<genesis_home>/logs/pid_snapshots/``; the
writer prunes files older than 7 days and keeps the directory under 20 MB.
``GENESIS_PID_BREAKDOWN_DISABLED=1`` turns it off.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from genesis.util import proc_io
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

# Module-level so tests can repoint them at a fake tree.
_PROC = "/proc"
_CGROUP_ROOT = "/sys/fs/cgroup"

_DISABLE_ENV = "GENESIS_PID_BREAKDOWN_DISABLED"
# Cgroup levels below the binding one that the snapshot lists. MEASURED on a
# live install, the user slice holds sessions at depth 1 (session-N.scope) and
# services at depth 3 (the user manager, app.slice, the unit); 4 covers both with one
# level to spare. The pid walk itself is not depth-limited.
_CGROUP_DEPTH = 4
# Stops a pathological (or looping, via bind mounts) cgroup tree; real trees are
# a handful of levels deep.
_CGROUP_WALK_LIMIT = 32
_TOP_N = 5
_RETENTION_S = 7 * 86400
_RETENTION_BYTES = 20 * 1024 * 1024

# argv[0] basenames whose first positional argument says what is running.
_INTERPRETER_RE = re.compile(r"^(python[0-9.]*|node|bun|deno|bash|sh|dash|zsh|uv|uvx|npx)$")
# A positional argument used in a label must look like a name or a path: no
# ':' or '=' (URLs, user:token@host, key=value), so a credential cannot ride in.
_SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_.@/+-]+$")
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts", ".sh", ".rb", ".pl")


def _comm(pid: int) -> str:
    stat = proc_io.read_stat(pid)
    return f"[{stat[0]}]" if stat else "?"


def process_label(pid: int) -> str:
    """A short, credential-free name for a process (see the module docstring)."""
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        raw = b""
    args = [a.decode("utf-8", "replace") for a in raw.removesuffix(b"\x00").split(b"\x00")]
    if not args[0]:
        return _comm(pid)
    # A process that rewrites its title (redis, postgres, sshd) puts the whole
    # title, spaces and arguments included, in argv[0]: keep its first word only.
    # argv[0] is whatever the process set, so it is held to the same rule.
    first = args[0].split(" ", 1)[0]
    head = os.path.basename(first) or first
    if not _SAFE_ARG_RE.match(head):
        return _comm(pid)
    if not _INTERPRETER_RE.match(head):
        return head
    rest = iter(args[1:])
    for arg in rest:
        if arg == "-m":
            module = next(rest, "")
            return f"{head} -m {module}" if _SAFE_ARG_RE.match(module) else f"{head} -m"
        if arg == "-c":
            return f"{head} -c"
        if arg.startswith("-"):
            continue
        # The first positional may be an option's VALUE (`node --inspect-port 9229
        # x.js`), so only something shaped like a script path names the program.
        if _SAFE_ARG_RE.match(arg) and ("/" in arg or arg.endswith(_SCRIPT_SUFFIXES)):
            return f"{head} {os.path.basename(arg)}"
        return head
    return head


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _read_cgroup(pid: int) -> str | None:
    """The pid's cgroup-v2 path (the ``0::`` line of ``/proc/<pid>/cgroup``)."""
    try:
        with open(f"{_PROC}/{pid}/cgroup", "rb") as f:
            for line in f:
                if line.startswith(b"0::"):
                    return os.fsdecode(line[3:].removesuffix(b"\n"))
    except OSError:
        pass
    return None


def _walk_cgroups(root: str) -> tuple[list[dict], set[int], int]:
    """``(nodes, pids, unreadable)`` for the cgroup subtree at ``root``.

    ``nodes``: one ``{"cgroup", "depth", "tasks", "leaf"}`` per cgroup down to
    ``_CGROUP_DEPTH`` (``cgroup`` relative to ``root``; ``leaf`` when it has no
    child cgroup or sits at the depth limit). ``pids``: every process in the whole
    subtree, from each level's ``cgroup.procs``. ``unreadable``: cgroups whose
    ``cgroup.procs`` could not be read, so a gap is counted rather than hidden.
    """
    nodes: list[dict] = []
    pids: set[int] = set()
    unreadable = 0
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            with open(f"{path}/cgroup.procs") as f:
                pids.update(int(x) for x in f.read().split())
        except (OSError, ValueError):
            unreadable += 1
        children: list[str] = []
        if depth < _CGROUP_WALK_LIMIT:
            try:
                with os.scandir(path) as it:
                    children = [e.path for e in it if e.is_dir(follow_symlinks=False)]
            except OSError:
                pass
        if depth <= _CGROUP_DEPTH:
            nodes.append(
                {
                    "cgroup": os.path.relpath(path, root),
                    "depth": depth,
                    "tasks": _read_int(f"{path}/pids.current"),
                    "leaf": not children or depth == _CGROUP_DEPTH,
                }
            )
        stack.extend((c, depth + 1) for c in children)
    return nodes, pids, unreadable


def _cgroup_prefix(root: str) -> str | None:
    """The binding's /proc-relative path, or None outside the cgroup mount."""
    rel = os.path.relpath(root, _CGROUP_ROOT)
    if rel == ".." or rel.startswith("../"):
        return None
    return "/" if rel == "." else "/" + rel


def _in_cgroup(path: str | None, prefix: str | None) -> bool:
    return (
        path is not None
        and prefix is not None
        and (prefix == "/" or path == prefix or path.startswith(prefix + "/"))
    )


def _pids_by_cgroup_prefix(prefix: str | None) -> set[int]:
    """Every pid in /proc whose cgroup path lies at or under ``prefix``."""
    found: set[int] = set()
    if prefix is None:
        return found
    try:
        names = os.listdir(_PROC)
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        path = _read_cgroup(int(name))
        if _in_cgroup(path, prefix):
            found.add(int(name))
    return found


def _group_processes(pids: set[int], prefix: str | None) -> tuple[list[dict], int, int, int]:
    """``(trees, threads_seen, vanished, excluded)`` for the given pids.

    A pid that exits between the cgroup read and its ``stat`` read is counted in
    ``vanished``. Unreadable/outside membership or a reused PID is ``excluded``.
    Trees are sorted by threads, largest first. Reads remain a best-effort sample.
    """
    # pid -> (comm, state, ppid, threads, starttime). A zombie reports 0 or 1
    # thread; it still holds a pid, so it counts at least 1.
    info: dict[int, tuple[str, str, int, int, int]] = {}
    cgroup: dict[int, str | None] = {}
    vanished = 0
    excluded = 0
    for pid in pids:
        stat = proc_io.read_stat(pid)
        if stat is None:
            vanished += 1
        else:
            path = _read_cgroup(pid)
            after = proc_io.read_stat(pid)
            if after is None:
                vanished += 1
                continue
            if after[4] != stat[4] or not _in_cgroup(path, prefix):
                excluded += 1
                continue
            comm, state, ppid, threads, starttime = stat
            info[pid] = (comm, state, ppid, max(1, threads), starttime)
            cgroup[pid] = path

    root_of: dict[int, int] = {}
    for pid in info:
        path: list[int] = []
        cur = pid
        while True:
            if cur in root_of:
                root = root_of[cur]
                break
            path.append(cur)
            parent = info[cur][2]
            # A claude process roots its own tree; so does a process whose parent
            # is outside the subtree, or in a different cgroup (a unit boundary:
            # every user service's parent is the user's systemd manager, which
            # would otherwise swallow them all into one tree). A ppid cycle
            # (impossible in a consistent /proc, possible across a racy read)
            # stops at the repeat.
            if (
                info[cur][0] == "claude"
                or parent not in info
                or parent in path
                or cgroup[parent] != cgroup[cur]
            ):
                root = cur
                break
            cur = parent
        for p in path:
            root_of[p] = root

    members: dict[int, list[int]] = {}
    for pid, root in root_of.items():
        members.setdefault(root, []).append(pid)

    trees = []
    for root, group in members.items():
        group.sort(key=lambda p: info[p][3], reverse=True)
        comm, _state, _ppid, _threads, starttime = info[root]
        unit = (cgroup[root] or "").rstrip("/").rsplit("/", 1)[-1] or None
        trees.append(
            {
                "root_pid": root,
                "root_starttime": starttime,
                "comm": comm,
                "unit": unit,
                "threads": sum(info[p][3] for p in group),
                "processes": len(group),
                # Many one-thread processes (a fork storm, unreaped children) tie
                # in "top"; what they ARE is in by_comm.
                "by_comm": Counter(info[p][0] for p in group).most_common(_TOP_N),
                "zombies": sum(1 for p in group if info[p][1] == "Z"),
                "top": [
                    {"pid": p, "starttime": info[p][4], "threads": info[p][3]}
                    for p in group[:_TOP_N]
                ],
            }
        )
    trees.sort(key=lambda t: t["threads"], reverse=True)
    return trees, sum(v[3] for v in info.values()), vanished, excluded


def _label_if_same(pid: int, starttime: int) -> str:
    """The label, unless the pid now belongs to a different process."""
    label = process_label(pid)
    return label if proc_io.read_starttime(pid) == starttime else "(exited)"


def take_snapshot(budget: dict) -> dict | None:
    """The breakdown for ``budget`` (a ``_collect_pid_budget`` result), or None
    when it names no cgroup ``path``."""
    root = budget.get("path")
    if not root:
        return None
    nodes, pids, unreadable = _walk_cgroups(root)
    # cgroup.procs omits a process with no live threads (an unreaped zombie),
    # which pids.current still counts until it is reaped: find those through
    # their own /proc/<pid>/cgroup.
    prefix = _cgroup_prefix(root)
    pids |= _pids_by_cgroup_prefix(prefix)
    trees, threads_seen, vanished, excluded = _group_processes(pids, prefix)
    # Labels need a cmdline read each, so only for what the snapshot shows.
    for tree in trees:
        tree["label"] = _label_if_same(tree["root_pid"], tree["root_starttime"])
        for member in tree["top"]:
            member["label"] = _label_if_same(member["pid"], member["starttime"])
    leaves = sorted(
        (n for n in nodes if n["leaf"]),
        key=lambda n: n["tasks"] or 0,
        reverse=True,
    )
    return {
        "taken_at": datetime.now(UTC).isoformat(),
        "scope": budget.get("scope"),
        "path": root,
        "budget_current": budget.get("current"),
        "budget_max": budget.get("max"),
        "threads_seen": threads_seen,
        "processes_seen": len(pids) - vanished - excluded,
        "vanished": vanished,
        "excluded": excluded,
        "cgroups_unreadable": unreadable,
        "top_cgroups": leaves[:_TOP_N],
        "trees": trees,
        "cgroups": nodes,
    }


def summarize(snapshot: dict) -> str:
    """One line for the alert: the top trees and cgroups, and the coverage."""

    def tree(t: dict) -> str:
        text = (
            f"{t['label']} (pid {t['root_pid']}, {t['unit'] or '?'}): {t['threads']} tasks"
            f" in {t['processes']} procs"
        )
        if t["processes"] > 1 and t["by_comm"][0][1] > 1:
            text += ", mostly " + ", ".join(f"{c} x{n}" for c, n in t["by_comm"][:2])
        if t["zombies"]:
            text += f", {t['zombies']} zombies"
        return text

    trees = "; ".join(tree(t) for t in snapshot["trees"][:_TOP_N])
    cgroups = "; ".join(
        f"{n['cgroup']}: {n['tasks'] if n['tasks'] is not None else '?'}"
        for n in snapshot["top_cgroups"]
    )
    text = (
        f"Top task holders: {trees or 'none readable'}. "
        f"Top cgroups: {cgroups or 'none readable'}. "
        f"Counted {snapshot['threads_seen']} of {snapshot['budget_current']} tasks."
    )
    # Filesystem-surrogate paths retain byte identity in JSON; the alert is text.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _snapshot_dir() -> Path:
    from genesis.env import genesis_home

    return genesis_home() / "logs" / "pid_snapshots"


def _prune(directory: Path, now: float) -> None:
    """Delete snapshots older than 7 days, then the oldest until under 20 MB."""
    files = []
    for path in directory.glob("*.json"):
        try:
            st = path.stat()
        except OSError:
            continue
        files.append((st.st_mtime, st.st_size, path))
    files.sort()
    total = sum(size for _m, size, _p in files)
    for mtime, size, path in files:
        if now - mtime <= _RETENTION_S and total <= _RETENTION_BYTES:
            break
        try:
            path.unlink()
            total -= size
        except OSError:
            logger.debug("pid snapshot prune failed: %s", path, exc_info=True)


def snapshot_for(budget: dict) -> dict | None:
    """Take, write and prune; ``{"text", "file"}`` for the alert, or None.

    ``file`` is None when the new snapshot exceeds the retention cap and is pruned.

    None when disabled or ``budget`` names no path. Never raises: a failed
    snapshot must not take the alert down with it, so it is logged and None
    returned. Blocking (file I/O over a few thousand small files): call it
    through ``asyncio.to_thread``.
    """
    if os.environ.get(_DISABLE_ENV) == "1":
        return None
    try:
        snapshot = take_snapshot(budget)
        if snapshot is None:
            return None
        directory = _snapshot_dir()
        directory.mkdir(parents=True, exist_ok=True)
        name = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json"
        target = directory / name
        atomic_write_text(target, json.dumps(snapshot, indent=1), encoding="utf-8")
        _prune(directory, time.time())
        return {"text": summarize(snapshot), "file": str(target) if target.is_file() else None}
    except Exception:
        logger.warning("pid budget breakdown snapshot failed", exc_info=True)
        return None
