#!/usr/bin/env python3
"""Retire a stale .git/index.lock before a deploy writes the index (#3792).

A git process that dies mid-write leaves a 0-byte ``index.lock`` behind, and
every later index write fails with ``Unable to create '.git/index.lock': File
exists``: the deploy's merge, its pre-merge resets, the periodic pulls. On one
live install it sat for two days with the checkout frozen and nothing naming
the cause. Genesis heals itself, so the deploy paths run this first, inside the
update and checkout locks they already hold (no Genesis mover can start then).

A lock is retired only when nothing could be writing through it:
- no process holds it open (``live_move_repair._held_open``: readable fd tables
  matched exactly; a live same-user ``git`` whose fds cannot be read counts as
  holding it);
- no live same-user git is working in this checkout (its cwd, or a
  ``-C``/``--git-dir``/``--work-tree`` argument, resolves to this repository).
  A git can own the lock with no fd open on it: ``git commit -a`` writes the
  index, closes the lock's fd and keeps the lock while it waits on the editor.
  A git is a process named ``git`` or a dashed entry point (``git-commit``),
  except the long-lived daemons, which never write the index;
- it is older than ``MIN_AGE_S``. A real writer holds the lock for milliseconds
  to seconds; the bound is a second margin on top of the two checks above;
- it is still the same file (inode and mtime) just before removal.

Residual: processes of other users (a root ``sudo git``) and of other hosts
sharing the filesystem are invisible here; only the age bound covers them.

Retiring follows ``live_move_repair``'s retire step: the lock is copied into
``deploy-backups/<ts>-stale-index-lock/`` with a ``LOCK.json`` beside it (its
size, mtime and age, and the git and claude processes alive now, for finding
what leaves these behind), made durable (the copy itself is fsynced), then
removed.

The checkout's git directory is ``.git``, or the directory a ``.git`` file names
(``git init --separate-git-dir``). A ``.git`` file naming a directory that holds
a ``commondir`` file is a linked worktree, which is skipped: its index is its
own and no deploy writes it. Backups older than
``KEEP_DAYS`` are pruned on each run.

Prints one line and exits 0 when there is nothing to do or the lock is left
alone, 3 when it retired a lock (the caller queues an alert), 1 when a stale
lock could not be retired (the caller's own git error follows; an uncaught
error also exits 1, which the callers treat the same way). Any other code
(python's own 2 for a missing script, for example) is a failure to check. Standard
library only; run with ``python3 -I -S``.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import time

_LIB = os.path.dirname(os.path.abspath(__file__))
MIN_AGE_S = 600
KEEP_DAYS = 30
SUFFIX = "-stale-index-lock"
RETIRED = 3
_PROC = "/proc"


def _repair():
    """live_move_repair, loaded by path (this runs isolated, with no sys.path)."""
    spec = importlib.util.spec_from_file_location(
        "live_move_repair", os.path.join(_LIB, "live_move_repair.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _processes() -> list[dict]:
    """The git and claude processes alive now: who might have left the lock."""
    out = []
    for pid in os.listdir(_PROC):
        if not pid.isdigit():
            continue
        try:
            with open(f"{_PROC}/{pid}/comm") as fh:
                comm = fh.read().strip()
            if not (comm.startswith("git") or comm.startswith("claude")):
                continue
            with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode(errors="replace").strip()
            try:
                cwd = os.readlink(f"{_PROC}/{pid}/cwd")
            except OSError:
                cwd = None
        except OSError:
            continue
        out.append({"pid": int(pid), "comm": comm, "cmdline": cmd[:300], "cwd": cwd})
    return out


def _owning_git_dir(path: str) -> str | None:
    """The ``.git`` entry governing ``path``: the nearest one walking up."""
    path = os.path.realpath(path)
    while True:
        candidate = os.path.join(path, ".git")
        if os.path.lexists(candidate):
            return candidate
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _git_places(pid: str) -> list[str] | None:
    """Every directory a git process names as its repository or work tree.

    Its cwd; each ``-C`` (cumulative: a later relative one is relative to the
    earlier one); ``--git-dir``/``--work-tree`` in both ``=X`` and ``X`` forms;
    and ``GIT_DIR``/``GIT_WORK_TREE`` in its environment. None when any of that
    cannot be read (the caller counts the process)."""
    try:
        cwd = os.readlink(f"{_PROC}/{pid}/cwd")
        with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
            argv = [a.decode(errors="replace") for a in fh.read().split(b"\0")]
        with open(f"{_PROC}/{pid}/environ", "rb") as fh:
            env = dict(
                e.decode(errors="replace").partition("=")[::2] for e in fh.read().split(b"\0") if e
            )
    except OSError:
        return None
    base = cwd
    places = [cwd]
    i = 1
    while i < len(argv):
        arg = argv[i]
        value = argv[i + 1] if i + 1 < len(argv) else ""
        if arg == "-C":
            base = os.path.join(base, value)
            places.append(base)
            i += 2
            continue
        if arg in ("--git-dir", "--work-tree"):
            places.append(os.path.join(base, value))
            i += 2
            continue
        for flag in ("--git-dir=", "--work-tree="):
            if arg.startswith(flag):
                places.append(os.path.join(base, arg[len(flag) :]))
        i += 1
    for name in ("GIT_DIR", "GIT_WORK_TREE"):
        if env.get(name):
            places.append(os.path.join(cwd, env[name]))
    return places


#: Global options that take their value as the next argument.
_VALUE_OPTS = frozenset(("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"))
#: The long-lived daemons, as a dashed program and as a subcommand.
_DAEMONS = frozenset(("daemon", "credential-cache--daemon", "fsmonitor--daemon"))


def _is_daemon(argv: list[str]) -> bool:
    """A long-lived git daemon (``git daemon``, ``git fsmonitor--daemon``,
    ``git-credential-cache--daemon``): it never writes the index, and counting it
    would block the heal for as long as it runs. Matched by exact name, the
    subcommand being the first argument after the global options."""
    prog = os.path.basename(argv[0])
    if prog.startswith("git-") and prog[4:] in _DAEMONS:
        return True
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _VALUE_OPTS else 1
    return i < len(argv) and argv[i] in _DAEMONS


def _git_in_repo(targets: set[str], alive) -> int | None:
    """The pid of a live same-user git working in the checkout whose ``.git``
    entry or git directory is in ``targets`` (real paths).

    A git is a process whose comm is ``git`` or starts with ``git-`` (a dashed
    entry point such as ``git-commit`` writes the index like ``git commit``).
    Other names never count: long-lived helpers such as ``gitstatusd`` would
    block the heal forever. Daemons are skipped for the same reason. A git whose
    cwd, arguments or environment cannot be read counts (fail toward leaving the
    lock). A zombie (``alive`` false) holds nothing: a killed git its parent never
    reaped is the very leftover this heals. A git in a nested linked worktree
    governs a different index and does not."""
    for pid in os.listdir(_PROC):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            if os.stat(f"{_PROC}/{pid}").st_uid != os.getuid():
                continue
            with open(f"{_PROC}/{pid}/comm") as fh:
                comm = fh.read().strip()
        except OSError:
            continue
        if comm != "git" and not comm.startswith("git-"):
            continue
        if not alive(int(pid)):
            continue
        try:
            with open(f"{_PROC}/{pid}/cmdline", "rb") as fh:
                argv = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
        except OSError:
            argv = None
        if argv and _is_daemon(argv):
            continue
        places = _git_places(pid)
        if places is None:
            if os.path.exists(f"{_PROC}/{pid}"):
                return int(pid)
            continue  # exited while we looked
        for place in places:
            real = os.path.realpath(place)
            if any(real == t or real.startswith(t + os.sep) for t in targets):
                return int(pid)
            owner = _owning_git_dir(place)
            if owner is not None and os.path.realpath(owner) in targets:
                return int(pid)
    return None


def _checkout_git_dir(entry: str) -> str | None:
    """The git directory of the primary checkout whose ``.git`` entry is
    ``entry``, or None for a linked worktree (or a ``.git`` file naming no
    directory). Raises OSError/ValueError when the entry cannot be read.

    MEASURED with git 2.43: a linked worktree's directory holds ``commondir``; a
    primary checkout's, ``--separate-git-dir`` included, does not."""
    if os.path.isdir(entry):
        return entry
    with open(entry, encoding="utf-8") as fh:
        first = fh.readline().strip()
    if not first.startswith("gitdir:"):
        raise ValueError(f"{entry} is not a gitdir pointer")
    target = os.path.realpath(os.path.join(os.path.dirname(entry), first[len("gitdir:") :].strip()))
    if not os.path.isdir(target) or os.path.exists(os.path.join(target, "commondir")):
        return None
    return target


def _prune(backups: str, now: float) -> None:
    try:
        names = os.listdir(backups)
    except OSError:
        return
    for name in names:
        path = os.path.join(backups, name)
        if not name.endswith(SUFFIX) or not os.path.isdir(path):
            continue
        try:
            if now - os.lstat(path).st_mtime > KEEP_DAYS * 86400:
                shutil.rmtree(path)
        except OSError:
            continue


def heal(repo: str, home: str, *, now: float | None = None) -> tuple[int, str]:
    """Return (exit code, the line to print)."""
    now = time.time() if now is None else now
    lmr = _repair()
    backups = os.path.join(home, "deploy-backups")
    _prune(backups, now)
    # The physical path: /proc reports fds and cwds that way (a symlinked
    # checkout path would otherwise never match a holder).
    entry = os.path.join(os.path.realpath(repo), ".git")
    if not os.path.lexists(entry):
        return 0, "stale-index-lock: none"
    try:
        git_dir = _checkout_git_dir(entry)
    except (OSError, ValueError) as exc:
        return 1, f"stale-index-lock: cannot read {entry} ({type(exc).__name__})"
    if git_dir is None:
        return 0, "stale-index-lock: not a main checkout, skipped"
    lock = os.path.join(git_dir, "index.lock")
    try:
        st = os.lstat(lock)
    except FileNotFoundError:
        return 0, "stale-index-lock: none"
    except OSError as exc:
        return 1, f"stale-index-lock: cannot read {lock} ({type(exc).__name__})"
    age = now - st.st_mtime
    if age < MIN_AGE_S:
        return 0, f"stale-index-lock: {lock} is {age:.0f}s old, left for its writer"
    if lmr._held_open(lock):
        return 0, f"stale-index-lock: {lock} is held by a running process, left alone"
    git_pid = _git_in_repo({os.path.realpath(entry), os.path.realpath(git_dir)}, lmr._pid_alive)
    if git_pid is not None:
        return 0, f"stale-index-lock: git (pid {git_pid}) is working in {repo}, left alone"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + f"-{os.getpid()}"
    backup = os.path.join(backups, stamp + SUFFIX)
    try:
        os.makedirs(backup, exist_ok=True)
        saved = os.path.join(backup, "index.lock")
        shutil.copy2(lock, saved, follow_symlinks=False)
        if not os.path.islink(saved):  # the copy's data, before the original goes
            fd = os.open(saved, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        lmr.write_json(
            os.path.join(backup, "LOCK.json"),
            {
                "path": lock,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "age_s": round(age),
                "retired_at": now,
                "processes": _processes(),
            },
        )
        for top in (backup, backups, home):
            lmr._fsync_dir(top)
    except OSError as exc:
        shutil.rmtree(backup, ignore_errors=True)
        return 1, f"stale-index-lock: could not save {lock} ({type(exc).__name__}: {exc}); kept"
    try:
        now_st = os.lstat(lock)
        if (now_st.st_ino, now_st.st_mtime_ns) != (st.st_ino, st.st_mtime_ns):
            shutil.rmtree(backup, ignore_errors=True)
            return 0, f"stale-index-lock: {lock} changed while it was checked, left alone"
        os.unlink(lock)
    except FileNotFoundError:
        shutil.rmtree(backup, ignore_errors=True)
        return 0, f"stale-index-lock: {lock} went away while it was checked"
    except OSError as exc:
        return 1, f"stale-index-lock: could not retire {lock} ({type(exc).__name__}: {exc})"
    return RETIRED, (
        f"stale-index-lock: retired {lock} ({age / 3600:.1f}h old, no process held it); "
        f"saved under {backup}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("repo")
    ap.add_argument(
        "--home", default=os.environ.get("GENESIS_HOME") or os.path.expanduser("~/.genesis")
    )
    args = ap.parse_args(argv)
    rc, line = heal(os.path.abspath(args.repo), args.home)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
