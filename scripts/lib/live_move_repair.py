#!/usr/bin/env python3
"""Heal a `live` checkout a killed switch left half-written (#3492 PR D).

Recovery must not wait for a person and must never start code bootstrap did
not run on, so the repair only goes back toward the rollback tag, and loses
nothing: every node it changes is copied to deploy-backups/<ts>-live-move-repair/
first. A node the update did not write is restored too (leaving it would leave
the tree mixed) and reported with the step that puts it back. The path set,
the two-dot diff a switch writes, contains everything the guard counts as
residue, and only the guard's SAFE afterwards clears the state; a target whose
objects a power loss took cannot be judged, so the whole tree is restored to
HEAD and a clean tree clears it. Run from a kit outside the checkout (the
checkout is what may be half-written): python3 -I -S, standard library only.
Exit 1 only where a start would run a half-written tree; at boot every failure
before the first write exits 0 (the server starts as it did before). A main row
is out of scope (QD3): exit 3 by command, 0 at boot."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time

_LIB = os.path.dirname(os.path.abspath(__file__))
BOOT_BUDGET_S = 60  # every wait at boot, inside systemd's default 90 s start timeout
_POLL_S = 1.0
_PROC = "/proc"
SUFFIX = "-live-move-repair"
_now, _sleep = time.monotonic, time.sleep  # a test's fake clock replaces both
_wrote = False  # set at the first change to disk


class RepairError(Exception):
    pass


def _log(msg: str) -> None:
    print(f"live-move repair: {msg}", file=sys.stderr, flush=True)


def _git(repo: str, *args: str, check: bool = True, data: bytes | None = None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, input=data, env=env)
    if check and p.returncode != 0:
        why = p.stderr.decode(errors="replace").strip()
        raise RepairError(f"git {args[0]} failed (exit {p.returncode}): {why}")
    return p


def _out(repo: str, *args: str, check: bool = True) -> str:
    return os.fsdecode(_git(repo, *args, check=check).stdout.strip())


def _z(out: bytes) -> list[str]:
    return [os.fsdecode(x) for x in out.split(b"\0") if x]


def _tree(repo: str, rev: str) -> dict[str, tuple[str, str]]:
    """path -> (mode, oid) of every blob and gitlink in ``rev``."""
    entries = {}
    for rec in _z(_git(repo, "ls-tree", "-r", "-z", "--full-tree", rev).stdout):
        meta, path = rec.split("\t", 1)
        mode, _typ, oid = meta.split()
        entries[path] = (mode, oid)
    return entries


def _parents(p: str) -> list[str]:
    out, d = [], os.path.dirname(p)
    while d:
        out.append(d)
        d = os.path.dirname(d)
    return out


def _beyond_link(repo: str, p: str) -> bool:
    """Is a parent of ``p`` a symlink or a non-directory? Then ``p`` is not in the
    checkout: an lstat or unlink of it would act THROUGH that parent (a symlink
    can point outside the checkout), and git itself calls such a path absent."""
    for d in _parents(p):
        full = os.path.join(repo, d)
        if os.path.islink(full) or (os.path.lexists(full) and not os.path.isdir(full)):
            return True
    return False


def _node(repo: str, p: str, fmt: str) -> tuple[str, str] | None:
    """(mode, blob id) of the disk node, ("dir", "") or ("other", ""); None if absent,
    or beyond a symlinked or non-directory parent (never read through one)."""
    full = os.path.join(repo, p)
    if _beyond_link(repo, p):
        return None
    try:
        st = os.lstat(full)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(st.st_mode):
        mode, data = "120000", os.fsencode(os.readlink(full))
    elif stat.S_ISREG(st.st_mode):
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        with open(full, "rb") as fh:
            data = fh.read()
    else:
        return ("dir" if stat.S_ISDIR(st.st_mode) else "other", "")
    return mode, hashlib.new(fmt, b"blob %d\0" % len(data) + data).hexdigest()


def _as_disk(entry):
    return ("dir", "") if entry and entry[0] == "160000" else entry  # a gitlink is a directory


def _as_index(entry) -> list[tuple[str, str, str]]:
    return [(entry[0], entry[1], "0")] if entry else []


def _pid_alive(pid: int) -> bool:
    """Running and not a zombie (deploy_marker.sh's _deploy_marker_holder_live)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"{_PROC}/{pid}/stat") as fh:
            return fh.read().rsplit(") ", 1)[-1][:1] != "Z"
    except OSError:
        return True


def _flock(path: str, deadline: float) -> int | None:
    """flock, as every other holder of update.lock takes it; None if still held at ``deadline``."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if _now() >= deadline:
                os.close(fd)
                return None
            _sleep(min(_POLL_S, max(deadline - _now(), 0)))


def _held_open(path: str) -> bool:
    """Does a process hold ``path`` open (git holds index.lock open from creation)?
    A live process of this user whose fds cannot be read may: it counts. Other
    users' fds are never readable here (MEASURED: /proc/1/fd is EACCES), so a
    rule counting them would keep every stale lock forever; nor is a zombie's
    (EACCES too, and it holds nothing)."""
    for pid in os.listdir(_PROC):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            if os.stat(f"{_PROC}/{pid}").st_uid != os.getuid():
                continue
            fds = os.listdir(f"{_PROC}/{pid}/fd")
        except PermissionError:
            if _pid_alive(int(pid)):
                return True
            continue
        except OSError:
            continue
        for fd in fds:
            with contextlib.suppress(OSError):
                if os.readlink(f"{_PROC}/{pid}/fd/{fd}") == path:
                    return True
    return False


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: str, data: dict) -> None:
    global _wrote
    _wrote = True
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(os.path.dirname(path))


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def manifests(backups: str) -> list[tuple[str, dict]]:
    """(path, REPAIR.json) of every repair, oldest first."""
    try:
        names = sorted(n for n in os.listdir(backups) if n.endswith(SUFFIX))
    except OSError:
        return []
    found = [os.path.join(backups, n, "REPAIR.json") for n in names]
    return [(p, m) for p, m in ((p, _load(p)) for p in found) if m is not None]


def _ephemeral() -> str:
    """EPHEMERAL_DIRTY_RE; unreadable, a pattern matching nothing (bytes then decide)."""
    p = subprocess.run(
        ["bash", "-c", '. "$1/deploy_marker.sh" && printf %s "$EPHEMERAL_DIRTY_RE"', "re", _LIB],
        capture_output=True,
        text=True,
    )
    return p.stdout if p.returncode == 0 and p.stdout else r"(?!)"


def _load_guard():
    path = os.path.join(_LIB, "update_state_guard.py")
    spec = importlib.util.spec_from_file_location("_live_repair_guard", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reapply_step(repo: str, backup: str, row: dict) -> str:
    """The shell step that puts a foreign row back the way the repair found it."""
    q, path = shlex.quote, row["path"]
    dst = os.path.join(repo, path)
    steps = []
    if row["disk_changed"]:
        steps.append(f"rm -rf -- {q(dst)}")
        if row["saved_as"]:
            src = os.path.join(backup, row["saved_as"])
            steps += [f"mkdir -p -- {q(os.path.dirname(dst))}", f"cp -a -- {q(src)} {q(dst)}"]
    if [tuple(e) for e in row["index"]] != _as_index(row["tag"]):
        steps.append(f"git -C {q(repo)} update-index --force-remove -- {q(path)}")
        if row["index"]:
            lines = "".join(f"{m} {o} {s}\t{path}\n" for m, o, s in row["index"])
            steps.append(f"printf %s {q(lines)} | git -C {q(repo)} update-index --index-info")
    if row["flag"]:
        flag = "--skip-worktree" if row["flag"] in "Ss" else "--assume-unchanged"
        steps.append(f"git -C {q(repo)} update-index {flag} -- {q(path)}")
    return " && ".join(steps)


class Repair:
    def __init__(self, state_path: str, repo: str, boot: bool, wait: int):
        self.state_path, self.repo, self.boot, self.wait = state_path, repo, boot, wait
        self.home = os.path.dirname(os.path.abspath(state_path))
        self.backups = os.path.join(self.home, "deploy-backups")
        self.guard = _load_guard()
        self._fds: list[int] = []

    def _hold(self, path: str, deadline: float) -> int | None:
        fd = _flock(path, deadline)
        if fd is not None:
            self._fds.append(fd)
        return fd

    def _say(self, msg: str) -> None:
        if self.boot:
            _log(msg)
        else:
            print(msg, flush=True)

    def run(self) -> int:
        try:
            return self._run()
        finally:
            for fd in self._fds:
                os.close(fd)

    def _run(self) -> int:
        if not os.path.lexists(self.state_path):
            self._say("nothing to repair: there is no update state")
            return 0
        deadline = _now() + (BOOT_BUDGET_S if self.boot else self.wait)
        locks = os.path.join(os.environ.get("GENESIS_HOME") or self.home, "locks")
        os.makedirs(locks, exist_ok=True)
        lock = os.path.join(locks, "update.lock")
        if self._hold(lock, 0) is None:
            pid = (_load(self.state_path) or {}).get("pid")
            if self.boot and isinstance(pid, int) and pid > 1 and _pid_alive(pid):
                # update.sh or a deploy starts its own server under the lock; a
                # failure here would only send it to its degraded fallback.
                _log(f"update.lock is held and its update (pid {pid}) runs; it starts the server")
                return 0
            if self._hold(lock, deadline) is None:
                verdict, _ = self.guard.verdict(self.state_path, self.repo)
                self._say(f"update.lock is still held after the wait; the checkout is {verdict}")
                return 1 if verdict == self.guard.MIXED_TREE else 0
        common = _out(self.repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if self._hold(os.path.join(common, "genesis-checkout.lock"), deadline) is None:
            self._say("the checkout lock (genesis-checkout.lock) is still held after the wait")
            return 1
        index_lock = _out(
            self.repo, "rev-parse", "--path-format=absolute", "--git-path", "index.lock"
        )
        while os.path.lexists(index_lock) and _held_open(index_lock):
            if _now() >= deadline:
                self._say(f"{index_lock} is still held by a running git after the wait")
                return 1
            _sleep(_POLL_S)
        return self._judge_state(index_lock)

    def _judge_state(self, index_lock: str) -> int:
        with open(self.state_path, "rb") as fh:
            raw = fh.read()
        state = _load(self.state_path)
        verdict, reason = self.guard.state_verdict(state, self.repo)
        to = self._live_target(state)
        if verdict == self.guard.SAFE and to:
            self._clear(raw)  # a repair killed after it healed, before it cleared
            return 0
        if verdict != self.guard.MIXED_TREE:
            self._say(f"nothing to repair: the checkout is not half-written ({verdict}: {reason})")
            return 0
        if not to:
            self._say("a half-written checkout on main is not repaired here; only `live` rows are")
            return 0 if self.boot else 3
        head = _out(self.repo, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False)
        if head != self.guard._tag_commit(state, self.repo):
            # HEAD off the tag (on `live` at the target: a rollback killed partway).
            # Finishing it (moving `live` back, restoring the hooks) is the forward
            # repair, D1f. C2 does not arm `live` before D1f lands, so no real install
            # reaches this state until then, and PR C's message stays its remedy.
            # Judged by HEAD, never by the guard's wording.
            self._say("forward case: needs the forward repair (D1f); nothing changed")
            return 1
        return self._heal(state, raw, to, index_lock)

    def _live_target(self, state: object) -> str | None:
        """The move's target of a live row (rule 6's record, or 6a's own_head)."""
        if not isinstance(state, dict) or state.get("phase") != "merging":
            return None
        move = self.guard._move_record(state)
        return move[0] if move else self.guard._adopted_tip(state, self.repo)

    def _heal(self, state: dict, raw: bytes, to: str, index_lock: str) -> int:
        repo = self.repo
        self.tag = tag = self.guard._tag_commit(state, repo)
        if _out(repo, "rev-parse", "--verify", "HEAD^{commit}") != tag:
            raise RepairError("HEAD is not the rollback tag")
        readable = _git(repo, "cat-file", "-e", f"{to}^{{tree}}", check=False).returncode == 0
        self.fmt = _out(repo, "rev-parse", "--show-object-format")
        self.want, self.other = _tree(repo, tag), _tree(repo, to) if readable else {}
        index: dict[str, list[tuple[str, str, str]]] = {}
        for rec in _z(_git(repo, "ls-files", "-s", "-z").stdout):
            meta, path = rec.split("\t", 1)
            mode, oid, stage = meta.split()
            index.setdefault(path, []).append((mode, oid, stage))
        listing = _z(_git(repo, "ls-files", "-v", "-z").stdout)
        flags = {r[2:]: r[0] for r in listing if r[0] == "S" or r[0].islower()}
        if readable:
            paths = set(_z(_git(repo, "diff", "-z", "--no-renames", "--name-only", tag, to).stdout))
        else:  # every change against HEAD (the tag), flagged and untracked included
            paths = set(_z(_git(repo, "diff", "-z", "--no-renames", "--name-only", "HEAD").stdout))
            paths |= set(_z(_git(repo, "ls-files", "-z", "--others", "--exclude-standard").stdout))
            paths |= set(flags)
        # A symlink or non-directory parent of ANY path: its own row (in a tag
        # path's way it must go; a target-only path under it is not in the
        # checkout, and _node never reads or unlinks through it).
        for p in list(paths):
            for d in _parents(p):
                full = os.path.join(repo, d)
                if os.path.islink(full) or (os.path.lexists(full) and not os.path.isdir(full)):
                    paths.add(d)
        self.ephemeral = _ephemeral()
        rows = [r for r in (self._row(p, index, flags) for p in sorted(paths)) if r]
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
        backup = os.path.join(self.backups, ts + SUFFIX)
        manifest = {"ts": ts, "repo": repo, "state_row": state, "tag": tag, "other": to}
        manifest.update(result=None, paths=rows, reapply=[], dismissed_at=None)
        self._save(backup, manifest, index_lock)
        self._apply(rows, index)
        _git(repo, "update-index", "-q", "--refresh", check=False)
        return self._after(backup, manifest, state, raw, readable, index_lock)

    def _row(self, p: str, index: dict, flags: dict) -> dict | None:
        """The path's row when anything about it is not the tag's, else None."""
        node = _node(self.repo, p, self.fmt)
        w, o = self.want.get(p), self.other.get(p)
        entries = index.get(p, [])
        disk_changed = node != _as_disk(w)
        if not disk_changed and entries == _as_index(w) and p not in flags:
            return None
        foreign = (
            p in flags
            or "160000" in (w or ("",))[:1] + (o or ("",))[:1]
            or entries not in (_as_index(w), _as_index(o))
            or (disk_changed and not self._engine_wrote(p, node, o))
        )
        return {
            "path": p,
            "class": "foreign" if foreign else "engine",
            "disk": "absent" if node is None else node[0],
            "disk_changed": disk_changed,
            "tag": w,
            "other": o,
            "index": entries,
            "flag": flags.get(p),
            "saved_as": None,
        }

    def _engine_wrote(self, p: str, node, o) -> bool:
        """The switch's doing: the target's bytes; a path git unlinked before
        writing; an interrupted write (empty, or a prefix of the target's
        bytes); an ephemeral path; a directory of target files."""
        if node is None or node == o or re.search(self.ephemeral, " " + p):
            return True
        full = os.path.join(self.repo, p)
        if node[0] == "dir":
            return all(
                _node(self.repo, rel, self.fmt) == self.other.get(rel)
                for top, _subdirs, files in os.walk(full)
                for rel in (os.path.relpath(os.path.join(top, f), self.repo) for f in files)
            )
        if node[0] not in ("100644", "100755") or not o or o[0] not in ("100644", "100755"):
            return False
        with open(full, "rb") as fh:
            have = fh.read()
        blob = _git(self.repo, "cat-file", "blob", o[1]).stdout
        return len(have) < len(blob) and blob.startswith(have)

    def _save(self, backup: str, manifest: dict, index_lock: str) -> None:
        """Copy every node about to change, and the stale index.lock, and make
        them and REPAIR.json durable before anything changes."""
        global _wrote
        _wrote = True
        os.makedirs(backup)
        for older, m in manifests(self.backups):
            if m.get("result") is None and os.path.dirname(older) != backup:
                write_json(older, {**m, "result": "superseded"})  # a run killed partway
        for row in manifest["paths"]:
            if not row["disk_changed"] or row["disk"] in ("absent", "other"):
                continue
            row["saved_as"] = os.path.join("tree", row["path"])
            src, dst = os.path.join(self.repo, row["path"]), os.path.join(backup, row["saved_as"])
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if row["disk"] == "dir":
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst, follow_symlinks=False)
        if os.path.lexists(index_lock):
            os.makedirs(os.path.join(backup, "git"))
            shutil.copy2(index_lock, os.path.join(backup, "git", "index.lock"))
        foreign = [r for r in manifest["paths"] if r["class"] == "foreign"]
        manifest["reapply"] = [reapply_step(self.repo, backup, r) for r in foreign]
        for top, _subdirs, files in os.walk(backup, topdown=False):
            for f in files:
                if not os.path.islink(os.path.join(top, f)):
                    with open(os.path.join(top, f), "rb") as fh:
                        os.fsync(fh.fileno())
            _fsync_dir(top)
        write_json(os.path.join(backup, "REPAIR.json"), manifest)
        if os.path.lexists(index_lock):
            os.unlink(index_lock)

    def _apply(self, rows: list[dict], index: dict) -> None:
        repo = self.repo
        flagged = [r["path"] for r in rows if r["flag"]]
        for opt in ("--no-skip-worktree", "--no-assume-unchanged"):  # one flag per call
            if flagged:
                _git(repo, "update-index", opt, "--", *flagged)
        for row in sorted(rows, key=lambda r: r["path"], reverse=True):  # children first
            w = self.want.get(row["path"])
            full = os.path.join(repo, row["path"])
            if row["disk"] == "dir" and row["disk_changed"]:
                shutil.rmtree(full)
            elif row["disk"] != "absent" and (w is None or row["disk"] == "other"):
                os.unlink(full)
        # `git restore` drops an index entry the tag lacks but refuses a path known
        # to neither, and refuses an index path under a named tag file (P/inner
        # under P), which naming P covers (MEASURED, git 2.43).
        named = {r["path"] for r in rows if r["path"] in self.want or r["path"] in index}
        spec = sorted(p for p in named if not named.intersection(_parents(p)))
        if spec:
            data = b"".join(os.fsencode(":(literal)" + p) + b"\0" for p in spec)
            _git(repo, "restore", f"--source={self.tag}", "--staged", "--worktree",
                 "--pathspec-from-file=-", "--pathspec-file-nul", data=data)  # fmt: skip
        tag_dirs = {d for p in self.want for d in _parents(p)}
        for row in rows:
            for d in _parents(row["path"]):
                if d not in tag_dirs:
                    with contextlib.suppress(OSError):
                        os.rmdir(os.path.join(repo, d))

    def _after(self, backup, manifest, state, raw, readable, index_lock) -> int:
        if not readable:
            dirty = _git(self.repo, "status", "--porcelain", "-uall").stdout
            if dirty or os.path.lexists(index_lock):
                return self._finish(backup, manifest, "unresolved: the tree is not clean", 1)
            self._clear(raw)
            return self._finish(backup, manifest, "healed (the target was unreadable)", 0)
        judged = state
        move = state.get("live_move")
        partial = isinstance(move, dict) and move.get("phase") == "partial"
        if partial:
            # The guard calls a "partial" record mixed whatever is on disk; the
            # repair undid what it records, so the tree decides, as for "moving".
            judged = {**state, "live_move": {**move, "phase": "moving"}}
        verdict, reason = self.guard.state_verdict(judged, self.repo)
        if verdict == self.guard.SAFE:
            self._clear(raw)
            return self._finish(backup, manifest, "healed", 0)
        if verdict in (self.guard.GUARDS, self.guard.HOLD):
            # The tree is the tag's on the move's paths; what still guards is not
            # the move's (HOLD: a tracked edit off its paths, the state a person
            # keeps or Dismisses, #3532). The start policy for it is the guard's
            # callers', not this repair's (#3373). GUARDS is unreachable here:
            # HEAD is at the tag (checked above), where the guard's live rule
            # answers only MIXED_TREE, HOLD or SAFE; the arm is kept as a pin in
            # case that rule ever grows one.
            if partial:
                # The kept file must say what the guard just judged, or it reads
                # "partial" (MIXED_TREE) forever and every caller sends the
                # operator back here, a new backup each run.
                self._rewrite(raw, judged)
            kept = f"the move's paths were restored; what still holds: {reason}"
            return self._finish(backup, manifest, f"healed, state kept: {kept}", 0)
        return self._finish(backup, manifest, f"unresolved: {verdict}: {reason}", 1)

    def _rewrite(self, raw: bytes, state: dict) -> None:
        """Replace the state with ``state``, only while it is still the bytes that
        were judged (as _clear)."""
        try:
            with open(self.state_path, "rb") as fh:
                same = fh.read() == raw
        except OSError:
            return
        if not same:
            _log("the update state changed during the repair; it is kept")
            return
        write_json(self.state_path, state)

    def _clear(self, raw: bytes) -> None:
        """Remove the state, only while it is still the bytes that were judged."""
        try:
            with open(self.state_path, "rb") as fh:
                same = fh.read() == raw
        except OSError:
            return
        if not same:
            _log("the update state changed during the repair; it is kept")
            return
        os.unlink(self.state_path)
        _fsync_dir(self.home)

    def _finish(self, backup: str, manifest: dict, result: str, rc: int) -> int:
        manifest["result"] = result
        write_json(os.path.join(backup, "REPAIR.json"), manifest)
        body = f"{result}. The checkout went back to {manifest['tag'][:12]}; what changed is saved in {backup}."
        if manifest["reapply"]:
            body += " These paths were not the update's; to re-apply them:\n"
            body += "\n".join(manifest["reapply"])
        self._say(body)
        severity = "critical" if manifest["reapply"] or rc else "warning"
        key = f"live_move_repair:{manifest['ts']}"
        subprocess.run(
            ["bash", "-c", '. "$1/alert_queue.sh" && queue_alert "$2" live_move_repair "$3" "$4" "$5"',
             "alert", _LIB, severity, "Genesis repaired a half-written live checkout", body, key],
            capture_output=True,
        )  # fmt: skip
        return rc


EXPLAIN = """The repair's steps, by hand, for when it cannot run:
1. Copy aside every path `git diff --name-only <rollback tag> <live_move.to>` lists
   (untracked files there included) and a .git/index.lock no git process holds.
2. git restore --source=<tag> --staged --worktree -- <those the tag or the index has>;
   delete the others.
3. Once scripts/lib/update_state_guard.py <state> <repo> exits 10, remove the state."""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="live_move_repair.py")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--boot", action="store_true")
    mode.add_argument("--command", action="store_true")
    mode.add_argument("--dismiss", metavar="TS")
    mode.add_argument("--explain", action="store_true")
    ap.add_argument("state", nargs="?")
    ap.add_argument("repo", nargs="?")
    ap.add_argument("--wait", type=int, default=7200)
    try:
        args = ap.parse_args(argv)
    except SystemExit:
        return 2
    if args.explain:
        print(EXPLAIN)
        return 0
    if not (args.state and args.repo):
        return 2
    if args.dismiss:  # the collector stops reporting a dismissed repair's saves
        home = os.path.dirname(os.path.abspath(args.state))
        path = os.path.join(home, "deploy-backups", args.dismiss + SUFFIX, "REPAIR.json")
        if (manifest := _load(path)) is None:
            _log(f"no repair report at {path}")
            return 1
        write_json(path, {**manifest, "dismissed_at": time.strftime("%FT%TZ", time.gmtime())})
        return 0
    try:
        return Repair(args.state, os.path.abspath(args.repo), args.boot, args.wait).run()
    except Exception as exc:  # noqa: BLE001 - every failure is named; at boot it sets the exit
        if args.boot and not _wrote:
            _log(f"skipped; nothing was changed ({type(exc).__name__}: {exc})")
            return 0
        _log(f"failed ({type(exc).__name__}: {exc}); the update state is kept")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
