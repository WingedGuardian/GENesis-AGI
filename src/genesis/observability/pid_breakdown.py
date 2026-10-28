"""Who holds the PID/task budget: a breakdown taken while it runs low.

The PID-budget alert (``awareness/loop.py::_check_pid_budget``) reports the
binding cgroup's ``pids.current`` against its ``pids.max``, which says THAT the
budget is nearly gone but not WHO holds it. By the time anyone reads the alert,
the processes that filled it may have exited, so this module records the
holders at the moment the budget runs low:

  - cgroups sampled under the binding level, to ``_CGROUP_DEPTH`` levels, with their
    ``pids.current`` (a cgroup's ``pids.current`` counts TASKS, i.e. threads, in
    its whole subtree);
  - process candidates sampled in that subtree, grouped into trees, with the threads each
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
# Diagnostic work limits, independent of the binding cgroup's task ceiling.
# These are finite sampling bounds, not a guarantee about kernel I/O latency.
_CGROUP_NODE_LIMIT = 512
_CGROUP_ENTRY_LIMIT = 16384
_PROC_ENTRY_LIMIT = 16384
_PID_LIMIT = 8192
_CGROUP_PROCS_BYTES = 128 * 1024
_CMDLINE_BYTES = 4096
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


def _cmdline_args(pid: int) -> list[str]:
    """Decode a bounded prefix, retaining only complete arguments if truncated."""
    try:
        with open(f"{_PROC}/{pid}/cmdline", "rb") as f:
            raw = f.read(_CMDLINE_BYTES + 1)
    except OSError:
        raw = b""
    if len(raw) > _CMDLINE_BYTES:
        # Only complete arguments may supply labels; an incomplete argv[0]
        # cannot identify the executable. Preserve empty interior arguments.
        end = raw[:_CMDLINE_BYTES].rfind(b"\x00")
        if end < 0:
            return [""]
        raw = raw[: end + 1]
    return [a.decode("utf-8", "replace") for a in raw.removesuffix(b"\x00").split(b"\x00")]


def process_label(pid: int) -> str:
    """A short, credential-free name for a process (see the module docstring)."""
    args = _cmdline_args(pid)
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
    if len(args) < 2:
        return head
    arg = args[1]
    if arg == "-m" and head.startswith("python"):
        if len(args) < 3:
            return head
        module = args[2]
        return f"{head} -m {module}" if _SAFE_ARG_RE.match(module) else f"{head} -m"
    if arg == "-c":
        return f"{head} -c"
    # Unknown options stop enrichment: their following word can be a private
    # option value even when it looks exactly like a script or module name.
    if arg.startswith("-"):
        return head
    if _SAFE_ARG_RE.match(arg) and ("/" in arg or arg.endswith(_SCRIPT_SUFFIXES)):
        return f"{head} {os.path.basename(arg)}"
    return head


def _read_int(path: str) -> int | None:
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _cgroup_type(root: str) -> str | None:
    """Read the binding's accounting regime; unknown is not a process domain."""
    try:
        with open(f"{root}/cgroup.type") as f:
            kind = f.read(64).strip()
    except OSError:
        return None
    return kind if kind in {"domain", "domain threaded", "threaded", "domain invalid"} else None


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


def _cgroup_pids(path: str, pids: set[int], truncated: set[str]) -> None:
    """Add a bounded PID prefix without growing the sample past its limit."""
    with open(f"{path}/cgroup.procs", "rb") as f:
        raw = f.read(_CGROUP_PROCS_BYTES + 1)
    if len(raw) > _CGROUP_PROCS_BYTES:
        truncated.add("cgroup PID bytes")
        raw = raw[:_CGROUP_PROCS_BYTES].rpartition(b"\n")[0]
    values = raw.split()
    remaining = _PID_LIMIT - len(pids)
    if len(values) > remaining:
        truncated.add("processes")
    pids.update(int(x) for x in values[:remaining])


def _cgroup_children(
    path: str, capacity: int, entries_seen: int, truncated: set[str]
) -> tuple[list[str], int]:
    """Bound both frontier allocation and directory entries across the walk."""
    children = []
    with os.scandir(path) as entries:
        for entry in entries:
            if entries_seen >= _CGROUP_ENTRY_LIMIT:
                truncated.add("cgroup entries")
                break
            entries_seen += 1
            if not entry.is_dir(follow_symlinks=False):
                continue
            if len(children) >= capacity:
                truncated.add("cgroups")
                break
            children.append(entry.path)
    return children, entries_seen


def _walk_cgroups(root: str, truncated: set[str]) -> tuple[list[dict], set[int], int]:
    """``(nodes, pids, unreadable)`` for the cgroup subtree at ``root``.

    ``nodes``: one ``{"cgroup", "depth", "tasks", "leaf"}`` per cgroup down to
    ``_CGROUP_DEPTH`` (``cgroup`` relative to ``root``; ``leaf`` when it has no
    child cgroup or sits at the depth limit). ``pids``: process candidates from the
    sampled subtree, from bounded prefixes of ``cgroup.procs``. ``unreadable``: cgroups whose
    ``cgroup.procs`` could not be read, so a gap is counted rather than hidden.
    """
    nodes: list[dict] = []
    pids: set[int] = set()
    unreadable = 0
    admitted = 1
    entries_seen = 0
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        path, depth = stack.pop()
        try:
            _cgroup_pids(path, pids, truncated)
        except (OSError, ValueError):
            unreadable += 1
        children: list[str] = []
        if depth < _CGROUP_WALK_LIMIT:
            try:
                children, entries_seen = _cgroup_children(
                    path, _CGROUP_NODE_LIMIT - admitted, entries_seen, truncated
                )
                admitted += len(children)
            except OSError:
                truncated.add("cgroup entries unreadable")
        else:
            truncated.add("cgroup depth")
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


def _pids_by_cgroup_prefix(prefix: str | None, truncated: set[str]) -> set[int]:
    """A bounded /proc sample whose cgroup paths lie at or under ``prefix``."""
    found: set[int] = set()
    if prefix is None:
        return found
    try:
        with os.scandir(_PROC) as entries:
            for count, entry in enumerate(entries):
                if count >= _PROC_ENTRY_LIMIT:
                    truncated.add("proc entries")
                    break
                if not entry.name.isdigit():
                    continue
                pid = int(entry.name)
                if _in_cgroup(_read_cgroup(pid), prefix):
                    if len(found) >= _PID_LIMIT:
                        truncated.add("processes")
                        break
                    found.add(pid)
    except OSError:
        return found
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
        seen: set[int] = set()
        cur = pid
        while True:
            if cur in root_of:
                root = root_of[cur]
                break
            path.append(cur)
            seen.add(cur)
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
                or parent in seen
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
    kind = _cgroup_type(root)
    truncated: set[str] = set()
    nodes, pids, unreadable = _walk_cgroups(root, truncated)
    # cgroup.procs omits a process with no live threads (an unreaped zombie),
    # which pids.current still counts until it is reaped: find those through
    # their own /proc/<pid>/cgroup.
    prefix = _cgroup_prefix(root)
    trees = []
    threads_seen = vanished = excluded = None
    # A domain-threaded ROOT contains the whole thread domain; a threaded
    # interior can contain only some threads of a process. Whole-process stat
    # counts cannot be assigned to that interior, even through the /proc fallback.
    reason = None
    if kind in {"domain", "domain threaded"}:
        pids |= _pids_by_cgroup_prefix(prefix, truncated)
        if len(pids) > _PID_LIMIT:
            truncated.add("processes")
            pids = set(sorted(pids)[:_PID_LIMIT])
        trees, threads_seen, vanished, excluded = _group_processes(pids, prefix)
    else:
        reason = "binding cgroup type unavailable" if kind is None else "unsupported binding cgroup type"
    if _cgroup_type(root) != kind:
        reason = "binding cgroup type changed during collection"
        trees = []
        threads_seen = vanished = excluded = None
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
        "process_attribution": {
            "status": "unavailable" if reason else "sampled",
            "reason": reason,
            "cgroup_type": kind,
        },
        "threads_seen": threads_seen,
        "processes_seen": sum(t["processes"] for t in trees) if reason is None else None,
        "vanished": vanished,
        "excluded": excluded,
        "cgroups_unreadable": unreadable,
        "truncated": sorted(truncated),
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
    attribution = snapshot.get("process_attribution", {})
    if attribution.get("status") == "unavailable":
        text = (
            f"Process attribution unavailable ({attribution['reason']}). "
            f"Top cgroups: {cgroups or 'none readable'}. "
            f"Binding cgroup reports {snapshot['budget_current']} tasks."
        )
    else:
        text = (
            f"Top task holders: {trees or 'none readable'}. "
            f"Top cgroups: {cgroups or 'none readable'}. "
            f"Counted {snapshot['threads_seen']} of {snapshot['budget_current']} tasks."
        )
    if snapshot.get("truncated"):
        text += " Collection is a partial sample (" + ", ".join(snapshot["truncated"]) + ")."
    # Filesystem-surrogate paths retain byte identity in JSON; the alert is text.
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _snapshot_dir() -> Path:
    from genesis.env import genesis_home

    return genesis_home() / "logs" / "pid_snapshots"


def prune_snapshot_dir(directory: Path, now: float) -> None:
    """Delete snapshots older than 7 days, then the oldest until under 20 MB.

    Shared by every JSON snapshot directory under ``<genesis_home>/logs/``
    (``pid_snapshots``, ``lock_snapshots``)."""
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
            logger.debug("snapshot prune failed: %s", path, exc_info=True)


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
        prune_snapshot_dir(directory, time.time())
        return {"text": summarize(snapshot), "file": str(target) if target.is_file() else None}
    except Exception:
        logger.warning("pid budget breakdown snapshot failed", exc_info=True)
        return None
