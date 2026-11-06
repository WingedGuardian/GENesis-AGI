"""The restore of a live-move repair report: what it may write (#3492 PR
B1, ``check``) and the writes (PR B2b, ``restore``).

A kit source beside live_move_repair.py, loaded by it from the same directory
(as it loads the guard), standard library only. ``check`` validates a report's
rows against exactly what the repair writes, then judges every part of every
foreign row the restore would write, NOW, from git's trees and index read once
each: the node (only when the repair changed it on disk and saw it in the
checkout), the index entries (only when they differ from the tag's) and the
flag. A part is

- ``as-left``: the path holds what the repair left (the tag's entry; for a
  tag directory, the tag's paths in it, other untracked files aside; an empty
  directory for a gitlink; or nothing), so writing the saved state loses
  nothing git cannot give back;
- ``already-restored``: it holds the saved state (a re-run converges);
- ``other:<reason>``: anything else, which the restore must leave alone.

An as-left part may be written only when, at and below and above its path,
HEAD's tree equals the tag's (else ``changed-upstream``: a deploy since the
repair changed it), the index equals the tag's except at rows already
restored (``index-changed``: the #3721 Codex P1 case, a file staged below),
no index entry sits at a parent (``index-above``), no parent is a symlink or a
non-directory (``link-parent``: decided first, before anything below it is
read), and no newer report holds an overlapping path (``newer-report``). A
parent that is this report's own row, already put back (or, for an index
entry, one the same restore replaces), is neither an ``index-above`` nor a
``link-parent`` for its child. An index part whose blob is neither saved nor
in the object store is ``not-saved``. A directory row's saved copy can hold
the update's own files, so it lists as ``entry`` parts only the saved files
whose bytes are not the move target's (when the target cannot be read, only
those that are foreign rows themselves); the restore builds the directory
from those. A node part a killed restore left half-done reads as-left by
the resume rule (Judge._resume). ``check`` writes nothing; ``restore`` writes
only parts judged as-left, each self-verifying (see _Writer)."""

from __future__ import annotations

import bisect
import calendar
import contextlib
import errno
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import time
import types

_OID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MODES = frozenset({"100644", "100755", "120000", "160000"})
_DISKS = _MODES | {"absent", "dir", "other"}
#: The `ls-files -v` tags the writer records (_index_state): S, or a lowercase one.
_FLAGS = frozenset("Shsmrck")
_SPECIALS = frozenset({"fifo", "socket", "char-device", "block-device", "unknown"})
_CANNOT_MAKE = _SPECIALS - {"fifo"}
PATH_BYTES = 4096
#: Every key the writer's _row writes; a row missing one is not the writer's.
ROW_KEYS = frozenset(
    {"path", "class", "disk", "disk_changed", "tag", "other", "index", "flag", "special",
     "saved_as", "blobs", "unsaved", "unreachable_through"}
)  # fmt: skip
#: What one check may read, under the locks: no file over FILE_BYTES is hashed,
#: at most READ_BYTES in all, and at most ENTRIES nodes looked at (files hashed
#: and directory entries walked). Over a budget a part is `other:too-large`,
#: never a long read. A report holds at most REPORT_ROWS (20,000) rows and a
#: few nodes per row; directory rows walk their saved copy and the checkout's
#: directory, so ENTRIES is ten times the row cap. The writer itself copies
#: whatever it finds; FILE_BYTES is about what a person keeps in a checkout,
#: and a bigger foreign file is refused, never read (the safe direction).
FILE_BYTES = 256 * 1024 * 1024
READ_BYTES = 2 * 1024 * 1024 * 1024
ENTRIES = 200_000


def _path(p: object, *, nested_git: bool = False) -> bool:
    """A plain relative path outside `.git`, as git records one: no empty, `.`
    or `..` part, no NUL, at most PATH_BYTES file-system bytes. ``nested_git``:
    a saved copy's path below a row, where a nested checkout's `.git` is
    saved data (#3974 item 1); only a first part `.git`, the checkout's own,
    is refused."""
    if not isinstance(p, str) or "\0" in p:
        return False
    parts = p.split("/")
    banned = ("", ".", "..") if nested_git else ("", ".", "..", ".git")
    if parts[0] == ".git" or any(part in banned for part in parts):
        return False
    try:
        return len(os.fsencode(p)) <= PATH_BYTES
    except UnicodeError:
        return False


def _entry(e: object, n: int) -> bool:
    return (
        isinstance(e, list)
        and len(e) == n
        and all(isinstance(x, str) for x in e)
        and e[0] in _MODES
        and _OID.fullmatch(e[1]) is not None
    )


def _special(s: object) -> bool:
    return (
        isinstance(s, dict)
        and set(s) == {"type", "mode"}
        and isinstance(s["type"], str)
        and s["type"] in _SPECIALS
        and isinstance(s["mode"], str)
        and re.fullmatch(r"[0-7]{4}", s["mode"]) is not None
    )


def _row_problem(row: dict, parents) -> str | None:
    """The first field of ``row`` the writer would not have written, else None."""
    if missing := ROW_KEYS - row.keys():
        return "missing " + ", ".join(sorted(missing))
    p, index = row["path"], row.get("index")
    # isinstance first: a list or object here is unhashable (#3965 item 3).
    disk = row.get("disk")
    if (
        row.get("class") not in ("foreign", "engine")
        or not isinstance(disk, str)
        or disk not in _DISKS
    ):
        return "class or disk"
    if not isinstance(row.get("disk_changed"), bool):
        return "disk_changed"
    if any(row.get(k) is not None and not _entry(row[k], 2) for k in ("tag", "other")):
        return "tag or other entry"
    if not isinstance(index, list) or not all(_entry(e, 3) for e in index):
        return "index"
    stages = [e[2] for e in index]  # one stage-0 entry, or stages 1-3 at most once each
    if stages not in ([], ["0"]) and (
        stages != sorted(set(stages)) or not set(stages) <= set("123")
    ):
        return "index stages"
    own = {f"index-blobs/{e[1]}" for e in index if e[0] != "160000"}
    blobs = row.get("blobs")
    if (
        not isinstance(blobs, list)
        or not all(isinstance(b, str) for b in blobs)
        or len(set(blobs)) != len(blobs)
        or not set(blobs) <= own
    ):
        return "blobs"
    saves = row["disk_changed"] and row["disk"] not in ("absent", "other")
    if row.get("saved_as") != (f"tree/{p}" if saves else None):
        return "saved_as"
    through = row.get("unreachable_through")
    if through is not None and through not in parents(p):
        return "unreachable_through"
    if row.get("flag") is not None and (
        not isinstance(row["flag"], str) or row["flag"] not in _FLAGS
    ):
        return "flag"
    if (row["disk"] == "other") != (row.get("special") is not None) or (
        row.get("special") is not None and not _special(row["special"])
    ):
        return "special"
    unsaved = row.get("unsaved")
    if not isinstance(unsaved, list) or not all(
        isinstance(u, dict) and _path(u.get("path"), nested_git=True)
        and u["path"].startswith(p + "/")
        and _special({k: u.get(k) for k in ("type", "mode")}) and len(u) == 3
        for u in unsaved
    ):  # fmt: skip
        return "unsaved"
    return None


_SHA256 = re.compile(r"[0-9a-f]{64}")
_MODE = re.compile(r"[0-7]{4}")


def _record(r: object) -> bool:
    """One SAVED.json record, exactly as the writer's _copy_node writes it."""
    if not isinstance(r, dict):
        return False
    kind = r.get("type")
    if kind == "file":
        sha = r.get("sha256")
        return (
            set(r) == {"type", "mode", "sha256"}
            and isinstance(r["mode"], str)
            and _MODE.fullmatch(r["mode"]) is not None
            and (sha is None or (isinstance(sha, str) and _SHA256.fullmatch(sha) is not None))
        )
    if kind == "fifo":
        mode = r.get("mode")
        return set(r) == {"type", "mode"} and isinstance(mode, str) and bool(_MODE.fullmatch(mode))
    if kind == "symlink":
        t = r.get("target")
        if set(r) != {"type", "target"} or not isinstance(t, str) or not t or "\0" in t:
            return False
        try:
            return len(os.fsencode(t)) <= PATH_BYTES
        except UnicodeError:
            return False
    return False


def saved_problem(rp, backup: str) -> str | None:
    """Why the report's SAVED.json is not one the writer wrote, else None."""
    return _saved(rp, backup)[0]


def _saved(rp, backup: str) -> tuple[str | None, dict | None]:
    """(why, None), or (None, SAVED.json) when it is the integrity record the
    writer wrote. Opened as load_report opens REPAIR.json: no symlink followed,
    no FIFO blocked on, a regular file of at most SAVED_BYTES, holding exactly
    {files, complete}: `tree/<path>` keys and the writer's three record
    shapes. Only the shape: whether a copy still matches its record the
    restore checks as it copies it."""
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(os.path.join(backup, "SAVED.json"), flags)
    except OSError as exc:
        return f"cannot be opened ({exc.strerror})", None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not a regular file", None
        chunks, size = [], 0
        while size <= rp.SAVED_BYTES and (chunk := os.read(fd, rp.SAVED_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return f"cannot be read ({exc.strerror})", None
    finally:
        os.close(fd)
    if size > rp.SAVED_BYTES:
        return f"over {rp.SAVED_BYTES} bytes", None
    try:
        saved = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return "not JSON", None
    if not isinstance(saved, dict) or set(saved) != {"files", "complete"}:
        return "not {files, complete}", None
    if not isinstance(saved["complete"], bool) or not isinstance(saved["files"], dict):
        return "files or complete", None
    if len(saved["files"]) > ENTRIES:
        return f"over {ENTRIES} records", None
    for key, r in saved["files"].items():
        if not (
            key.startswith("tree/") and _path(key[len("tree/") :], nested_git=True)
        ) or not _record(r):
            return f"record {json.dumps(key)[:200]}", None
    return None, saved


def problem(loaded: dict, budget: int, parents) -> str | None:
    """Why a loaded report is not one the writer wrote, else None. Paths are
    bounded first, Σ len(path) x depth over the rows at most ``budget``, so the
    later parent walks stay bounded. That is not the writer's own bound (its
    report bytes scale with length, not depth): a report of a legitimately deep
    tree can be refused, the safe direction."""
    report, rows = loaded["report"], loaded["rows"]
    if len(rows) != len(report["paths"]):
        return "a row is not an object"
    if not all(
        isinstance(report.get(k), str) and _OID.fullmatch(report[k]) for k in ("tag", "other")
    ):
        return "tag or other is not an object id"
    spent, seen = 0, set()
    for n, row in enumerate(rows):
        p = row.get("path")
        if not _path(p) or p in seen:
            return f"row {n}: path"
        seen.add(p)
        spent += len(p) * (p.count("/") + 1)
        if spent > budget:
            return f"row {n}: over the path budget"
        if why := _row_problem(row, parents):
            return f"row {n}: {why}"
    return None


class _Paths:
    """A path set answering: does any member overlap ``p`` by prefix, either way?"""

    def __init__(self, paths) -> None:
        self.set = set(paths)
        self.sorted = sorted(self.set)

    def which(self, p: str, parents: list[str]) -> str | None:
        """A member overlapping ``p``, or None."""
        if p in self.set:
            return p
        if above := next((d for d in parents if d in self.set), None):
            return above
        i = bisect.bisect_left(self.sorted, p + "/")
        ok = i < len(self.sorted) and self.sorted[i].startswith(p + "/")
        return self.sorted[i] if ok else None

    def overlaps(self, p: str, parents: list[str]) -> bool:
        return self.which(p, parents) is not None


def _order(ts: str) -> int:
    """A report ts's UTC second; ValueError if it names no date."""
    return calendar.timegm(time.strptime(ts.split("-", 1)[0], "%Y%m%dT%H%M%SZ"))


def _newer_paths(rp, repo: str, backups: str, ts: str) -> tuple[dict | None, str]:
    """Every row path of every report of this checkout at or after ``ts``'s
    second, with the newer report's ts (dismissed ones too: dismissing a newer
    report does not make an older one's restore safe); else None and which
    newer report blocked: one that cannot be read, a ts that names no date (it
    may be newer), or the newer reports together over one report's caps
    (REPORT_BYTES of REPAIR.json, REPORT_ROWS rows; looked at before reading
    on). A report naming another checkout cannot overlap this one. A dir
    holding no REPAIR.json has changed nothing (the writer writes it before
    the checkout changes)."""
    mine, paths, size, rows = _order(ts), {}, 0, 0
    for name in sorted(os.listdir(backups)):
        other = rp.report_ts(name)
        if other is None or other == ts:
            continue
        try:
            if _order(other) < mine:
                continue
        except ValueError:
            return None, f"{other}: its ts names no date"
        d = os.path.join(backups, name)
        with contextlib.suppress(OSError):
            size += os.lstat(os.path.join(d, "REPAIR.json")).st_size
        if size > rp.REPORT_BYTES:
            return None, f"{other}: the newer reports are over {rp.REPORT_BYTES} bytes together"
        try:
            loaded = rp.load_report(d, repo)
        except rp.NotThisCheckout:
            continue
        except rp.BadReport as exc:
            return None, f"{other}: {exc}"
        found = (loaded or {}).get("rows", ())
        rows += len(found)
        if rows > rp.REPORT_ROWS:
            return None, f"{other}: the newer reports hold over {rp.REPORT_ROWS} rows together"
        # Every row must name a plain path: one that does not could hide an
        # overlap (#3966 item 1). load_report drops a row that is not an object.
        if loaded and len(found) != len(loaded["report"].get("paths", ())):
            return None, f"{other}: a row is not an object"
        for r in found:
            if not _path(r.get("path")):
                return None, f"{other}: a row names no plain path"
            paths.setdefault(r["path"], other)
    return paths, ""


def _raise(exc: OSError) -> None:
    raise exc


def _git_env() -> dict:
    """The repair's git environment (no GIT_* from the caller), taking no optional lock."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return {**env, "GIT_OPTIONAL_LOCKS": "0"}


class _Budget:
    """FILE_BYTES, READ_BYTES and ENTRIES for one check; TooLarge when spent."""

    def __init__(self, rp) -> None:
        self.rp, self.bytes, self.entries = rp, READ_BYTES, ENTRIES

    def entry(self) -> None:
        self.entries -= 1
        if self.entries < 0:
            raise self.rp.TooLarge(f"over {ENTRIES} nodes")

    def __call__(self, size: int) -> None:  # a file about to be hashed
        self.entry()
        if size > FILE_BYTES or size > self.bytes:
            raise self.rp.TooLarge(f"a file of {size} bytes")
        self.bytes -= size


class Judge:
    """``records``: SAVED.json's files (a FIFO's restored mode is its record's).
    ``runs``: deploy-backups as read (restore run folders, for the resume rule),
    ``shown`` as named in output. ``strict``: the restore's write gate (a tag
    directory moved aside may hold no extra file git does not ignore)."""

    def __init__(self, rp, repo: str, backup: str, loaded: dict, newer: dict, *, records=None,
                 runs=None, shown=None, strict=False) -> None:  # fmt: skip
        self.rp, self.repo, self.backup = rp, repo, backup
        self.budget, self.blockers = _Budget(rp), set()
        report, self.rows = loaded["report"], loaded["rows"]
        self.records, self.runs, self.shown, self.strict = records or {}, runs, shown, strict
        block = restore_block(report)
        self.inflight = block["inflight"] if block else None
        self.notes, self.dirs, self.entry_of, self.resumed = {}, {}, {}, None
        self.fmt = rp._out(repo, "rev-parse", "--show-object-format")
        self.head = rp._tree(repo, "HEAD")
        self.tag = rp._tree(repo, report["tag"])
        try:
            self.target = rp._tree(repo, report["other"])
        except rp.RepairError:
            self.target = None
        self.index, self.flags = rp._index_state(repo)
        self.foreign = {r["path"]: r for r in self.rows if r["class"] == "foreign"}
        self.by_path = {r["path"]: r for r in self.rows}
        self.tag_dirs = {d for q in self.tag for d in rp._parents(q)}
        self.tag_sorted = sorted(self.tag)
        self.whole = {r["path"] for r in self.rows if r["disk"] == "dir" and r["disk_changed"]}
        keys = set(self.head) | set(self.tag)
        self.upstream = _Paths(q for q in keys if self.head.get(q) != self.tag.get(q))
        dirty = {
            q
            for q in set(self.index) | set(self.tag)
            if self.index.get(q, []) != rp._as_index(self.tag.get(q)) and not self._index_back(q)
        }
        self.dirty, self.newer, self.newer_ts = _Paths(dirty), _Paths(newer), newer
        self.stored = self._stored()

    @staticmethod
    def _saved_index(row: dict) -> list[tuple]:
        return [tuple(e) for e in row["index"]]

    def _index_back(self, q: str) -> bool:
        """Does the index at ``q`` hold what this report's row there saved?"""
        return q in self.foreign and self.index.get(q, []) == self._saved_index(self.foreign[q])

    def _replaced(self, d: str) -> bool:
        """Is the index at ``d`` the tag's, as the repair left it, at a row of
        this report that saved something else there (so the restore's one
        update-index call takes it away; it checks that call's footprint)?"""
        row, now = self.by_path.get(d), self.index.get(d, [])
        return (
            row is not None
            and now == self.rp._as_index(self.tag.get(d))
            and (self._saved_index(row) != now)
        )

    def _index_part(self, row: dict) -> bool:
        return self._saved_index(row) != self.rp._as_index(self.tag.get(row["path"]))

    def _stored(self) -> set:
        """The blob ids an index part needs that the object store holds: one
        `git cat-file --batch-check` for all of them (none, if it fails)."""
        want = sorted(
            {e[1] for r in self.foreign.values() if self._index_part(r)
             for e in r["index"] if e[0] != "160000"}
        )  # fmt: skip
        if not want:
            return set()
        p = subprocess.run(
            ["git", *self.rp.GIT_C, "-C", self.repo, "cat-file", "--batch-check"],
            input="".join(f"{o}\n" for o in want).encode(),
            capture_output=True,
            env=_git_env(),
        )
        if p.returncode != 0:
            return set()
        lines = (line.split() for line in p.stdout.decode(errors="replace").splitlines())
        return {f[0] for f in lines if len(f) == 3 and f[1] == "blob"} & set(want)

    def _blob_saved(self, row: dict, oid: str) -> bool:
        """index-blobs/<oid> in the row's blobs: a regular file, reached and
        opened without following a link, whose git blob id is ``oid``."""
        rel = f"index-blobs/{oid}"
        if rel not in row["blobs"] or self.rp._link_parent(self.backup, rel) is not None:
            return False
        full = os.path.join(self.backup, rel)
        try:
            st = os.lstat(full)
            return stat.S_ISREG(st.st_mode) and (
                self.rp._blob_id(full, st, self.fmt, self.budget) == oid
            )
        except OSError:
            return False

    def _blobs_missing(self, row: dict) -> bool:
        return any(
            e[0] != "160000" and e[1] not in self.stored and not self._blob_saved(row, e[1])
            for e in row["index"]
        )

    def _state(self, root: str, rel: str):
        """_node, with a FIFO, socket or device told apart by type and mode."""
        n = self.rp._node(root, rel, self.fmt, self.budget)
        if n and n[0] == "other":
            s = self.rp._special(os.path.join(root, rel))
            return s["type"], s["mode"]
        return n

    def _real_dir(self, root: str, rel: str) -> bool:
        """A directory at ``rel`` under ``root``, by lstat, with no symlinked
        or non-directory parent: the only kind ever walked."""
        if self.rp._link_parent(root, rel) is not None:
            return False
        try:
            return stat.S_ISDIR(os.lstat(os.path.join(root, rel)).st_mode)
        except OSError:
            return False

    def _walk(self, root: str, rel: str) -> dict:
        """Every non-directory below ``rel`` (a directory symlink is one, never
        followed), by its path relative to ``rel``; an unreadable directory
        raises. Only called on a _real_dir."""
        out, full = {}, os.path.join(root, rel)
        for top, dirs, files in os.walk(full, onerror=_raise):
            self.budget.entry()
            for name in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
                self.budget.entry()
                q = os.path.relpath(os.path.join(top, name), full)
                out[q] = self._state(root, os.path.join(rel, q))
        return out

    def _tag_below(self, p: str) -> dict:
        out, i = {}, bisect.bisect_left(self.tag_sorted, p + "/")
        while i < len(self.tag_sorted) and self.tag_sorted[i].startswith(p + "/"):
            q = self.tag_sorted[i]
            if self.tag[q][0] != "160000":  # a gitlink is a directory, not walked
                out[q[len(p) + 1 :]] = self.tag[q]
            i += 1
        return out

    def _ignored(self, paths: list[str]) -> set:
        """Which of ``paths`` git ignores: one `check-ignore`; none when it fails."""
        if not paths:
            return set()
        p = subprocess.run(
            ["git", *self.rp.GIT_C, "-C", self.repo, "check-ignore", "-z", "--stdin"],
            input=b"".join(os.fsencode(q) + b"\0" for q in paths),
            capture_output=True,
            env=_git_env(),
        )
        return set(self.rp._z(p.stdout)) if p.returncode in (0, 1) else set()

    def left_is_node(self, p: str) -> bool:
        """Did the repair leave a node at ``p`` (a tag entry, a gitlink's
        directory or a tag directory), rather than nothing?"""
        return p in self.tag or p in self.tag_dirs

    def _left(self, p: str, whole: bool = True, root: str | None = None, strict=False) -> bool:
        """Does ``root`` (the checkout; a run's aside) hold what the repair left
        at ``p``: the tag's? A tag directory is compared on the tag's own paths
        only (the repair never removes an untracked file there that is not a
        row), and only for a node part (``whole``); an index or flag part needs
        only the directory. ``strict`` (the restore moves the whole directory
        aside, review SF-D): every other file in it must be one git ignores."""
        root, t = root or self.repo, self.tag.get(p)
        if t and t[0] == "160000":
            return self._real_dir(root, p) and self._walk(root, p) == {}
        if t is None and p in self.tag_dirs:
            if not self._real_dir(root, p) or not whole:
                return self._real_dir(root, p)
            tagged = self._tag_below(p)
            if not strict:
                return all(self._state(root, f"{p}/{q}") == e for q, e in tagged.items())
            have = self._walk(root, p)
            extra = [f"{p}/{q}" for q in have if q not in tagged]
            return all(have.get(q) == e for q, e in tagged.items()) and (
                set(extra) <= self._ignored(extra)
            )
        return self._state(root, p) == t

    def _resume(self, p: str) -> bool:
        """The resume rule (review item 1, B-1, SF-C, SF-E): a restore killed
        after moving the node at ``p`` aside left the path empty. It reads
        as-left when REPAIR.json's in-flight part is this node, the path is
        absent (no link parent), the run folder and every folder the restore
        made under its aside/ are this user's alone (0700: no other account
        can have planted the aside; the aside itself, the checkout's own node,
        is only checked for its owner), and the aside is the as-left state."""
        inf = self.inflight
        if not inf or inf[1] != p or self.runs is None:
            return False
        if self.rp._link_parent(self.repo, p) or os.path.lexists(os.path.join(self.repo, p)):
            return False
        aside = os.path.join(self.runs, inf[0], "aside")
        made = [os.path.dirname(aside), aside] + [
            os.path.join(aside, d) for d in self.rp._parents(p)
        ]
        try:
            for d in made:
                st = os.lstat(d)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
                    return False
            if os.lstat(os.path.join(aside, p)).st_uid != os.geteuid():
                return False
        except OSError:
            return False
        if self._left(p, root=aside, strict=True):
            self.notes[(p, "node")] = (
                f"interrupted restore; the replaced copy is in {os.path.join(self.shown, inf[0])}"
            )
            self.resumed = p
            return True
        where = os.path.join(self.shown, inf[0], "aside", p)
        self.notes[(p, "node")] = f"newer content kept at {where}"
        return False

    def _saved_node(self, row: dict):
        """(is it saved, the saved node) of a non-directory row's disk part."""
        special = row.get("special")
        if row["saved_as"]:
            saved = self._state(self.backup, row["saved_as"])
            return saved is not None, saved
        if special and special["type"] == "fifo":
            return True, _fifo(special["mode"])
        return True, (special["type"], special["mode"]) if special else None

    def _link(self, row: dict) -> str | None:
        """``link-parent`` when a parent of the row's path is a symlink or a
        non-directory, decided before any disk read below it. A parent that is
        this report's own row, its node already put back, does not refuse the
        child the writer saw beyond it (a killed restore must converge)."""
        d = self.rp._link_parent(self.repo, row["path"])
        if d is None:
            return None
        if d in self.whole and self._left(d):
            # A directory row's path, still the tag's non-directory the repair
            # left there: this row is covered by it (index and flag parts only,
            # nothing read below), and the restore writes that directory first.
            return None
        parent = self.foreign.get(d)
        if parent and row["unreachable_through"] == d and parent["disk"] != "dir":
            try:
                ok, saved = self._saved_node(parent)
                if parent["disk_changed"] and ok and self._state(self.repo, d) == saved:
                    return None
            except self.rp.TooLarge:
                pass
        return "link-parent"

    def _part(self, judge) -> str:
        """One part's verdict; `other:too-large` once it would read over the budget."""
        try:
            return judge()
        except self.rp.TooLarge:
            return "other:too-large"

    def _gate(self, p: str) -> str | None:
        parents = self.rp._parents(p)
        if self.upstream.overlaps(p, parents):
            return "changed-upstream"
        if (hit := self.newer.which(p, parents)) is not None:
            self.blockers.add(self.newer_ts[hit])
            return "newer-report"
        # An entry above that this report's own row put back, or one its row's
        # index part replaces in the same update-index call (the restore checks
        # that call's whole footprint first), is never a refusal of its child.
        if any(not self._index_back(d) and not self._replaced(d)
               for d in self.index.keys() & set(parents)):  # fmt: skip
            return "index-above"
        if self.dirty.overlaps(p, parents):
            return "index-changed"
        return None

    def _verdict(self, link, done, gate, left, missing: str | None = None) -> str:
        if link:
            return f"other:{link}"
        if done():
            return "already-restored"
        if missing:
            return f"other:{missing}"
        if gate:
            return f"other:{gate}"
        return "as-left" if left() else "other:changed"

    def _node(self, row: dict, link, gate: str | None) -> str:
        p, special = row["path"], row.get("special")
        if special and special["type"] in _CANNOT_MAKE:
            return "other:cannot-make"
        ok, saved = self._saved_node(row)
        if not ok:
            return "other:not-saved"
        done = lambda: self._state(self.repo, p) == saved  # noqa: E731
        return self._verdict(link, done, gate, lambda: self._as_left(p))

    def _as_left(self, p: str) -> bool:
        return self._left(p, strict=self.strict) or self._resume(p)

    def _dir(self, row: dict, link, gate: str | None) -> list[tuple[str, str, str]]:
        """A directory row: its node, then each saved entry the restore would write."""
        p = row["path"]
        if not self._real_dir(self.backup, row["saved_as"]):
            return [(p, "node", "other:not-saved")]
        restore, kept, unread = {}, {}, []
        for q, s in self._walk(self.backup, row["saved_as"]).items():
            rec = self.records.get(f"{row['saved_as']}/{q}")
            if s and s[0] == "fifo" and rec and rec["type"] == "fifo":
                s = _fifo(rec["mode"])  # what the restore makes: the record's mode
            if self.target is not None and s == self.target.get(f"{p}/{q}"):
                kept[q] = s  # the update's own bytes
            elif self.target is not None or f"{p}/{q}" in self.foreign:
                restore[q] = s
            else:
                unread.append(f"{p}/{q}")

        def done() -> bool:
            if not self._real_dir(self.repo, p):
                return False
            have, tagged = self._walk(self.repo, p), self._tag_below(p)
            if not all(have.get(q) == s for q, s in restore.items()):
                return False
            # Review SF-6: a file git ignores (the server's __pycache__) is no change.
            extra = [f"{p}/{q}" for q, s in have.items()
                     if s not in (restore.get(q), kept.get(q), tagged.get(q))]  # fmt: skip
            return set(extra) <= self._ignored(extra)

        self.dirs[p] = restore
        v = self._verdict(link, done, gate, lambda: self._as_left(p))
        self.entry_of.update((f"{p}/{q}", p) for q in restore)
        out = [(p, "node", v)] + [(f"{p}/{q}", "entry", v) for q in sorted(restore)]
        out += [(q, "entry", "other:target-unreadable") for q in sorted(unread)]
        return out + [(u["path"], "entry", "other:cannot-make") for u in row["unsaved"]]

    def row(self, row: dict) -> list[tuple[str, str, str]]:
        p, out = row["path"], []
        link, gate = self._link(row), self._gate(row["path"])
        covered = not self.whole.isdisjoint(self.rp._parents(p))  # its parent's copy holds it
        node = None
        if row["disk_changed"] and not row["unreachable_through"] and not covered:
            if row["disk"] != "dir":
                out = [(p, "node", self._part(lambda: self._node(row, link, gate)))]
            else:
                try:
                    out = self._dir(row, link, gate)
                except self.rp.TooLarge:
                    out = [(p, "node", "other:too-large")]
            node = out[0][2]

        def disk_ok() -> bool:
            # A covered row's disk is its parent's node part (an index-only
            # write here, index == tag == HEAD, loses nothing on disk).
            if node is not None:
                return node in ("as-left", "already-restored")
            return covered or bool(row["unreachable_through"]) or self._left(p, whole=False)

        def left(ok) -> bool:
            return ok() and disk_ok()

        if self._index_part(row):
            tag_index = self.rp._as_index(self.tag.get(p))
            have = self.index.get(p, [])
            v = self._part(
                lambda: self._verdict(
                    link,
                    lambda: have == self._saved_index(row),
                    gate,
                    lambda: left(lambda: have == tag_index),
                    "not-saved" if self._blobs_missing(row) else None,
                )
            )
            out.append((p, "index", v))
        if row["flag"]:
            flag = self.flags.get(p)
            v = self._part(
                lambda: self._verdict(
                    link, lambda: flag == row["flag"], gate, lambda: left(lambda: flag is None)
                )
            )
            out.append((p, "flag", v))
        return out


# ── the restore (#3492 PR B2b) ──────────────────────────────────────────────

RUN_SUFFIX = "-live-move-restore"
#: A restore run's folder under deploy-backups: its UTC time, its pid.
RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+" + re.escape(RUN_SUFFIX))
#: A test's SIGKILL lands at the named step of the write path (see _step).
KILL_ENV = "GENESIS_LIVE_RESTORE_TEST_KILL"
RENAME_NOREPLACE = 1
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_BACK = ("restored", "already-restored")
_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
#: Refusals no re-run can turn into a write: the part goes back by hand.
_PERMANENT = ("other:cannot-make", "other:target-unreadable", "other:too-large", "other:no-hash")


def _fifo(mode: str) -> tuple[str, str]:
    """A FIFO's state as the restore makes it: the mode without setuid, setgid, sticky."""
    return "fifo", f"{int(mode, 8) & 0o777:04o}"


def restore_block(report: dict) -> dict | None:
    """REPAIR.json's `restore` block when it is exactly what restore() writes,
    {inflight: [run, path, "node"] | None, refused: int, restored_at: str |
    None}; else None (and a malformed one never resumes anything)."""
    b = report.get("restore")
    if not isinstance(b, dict) or set(b) != {"inflight", "refused", "restored_at"}:
        return None
    inf = b["inflight"]
    if inf is not None and not (
        isinstance(inf, list) and len(inf) == 3 and isinstance(inf[0], str)
        and RUN_NAME.fullmatch(inf[0]) and _path(inf[1]) and inf[2] == "node"
    ):  # fmt: skip
        return None
    if type(b["refused"]) is not int or b["refused"] < 0:
        return None
    return b if b["restored_at"] is None or isinstance(b["restored_at"], str) else None


def _step(name: str) -> None:
    """A named point of the write path (stage, intent, aside, put-in, index,
    index-call, post-judge). A test sets KILL_ENV to SIGKILL the run there."""
    if os.environ.get(KILL_ENV) == name:
        os.kill(os.getpid(), signal.SIGKILL)


_LIBC: list = []


def _renameat2(sfd: int, sname: str, dfd: int, dname: str, where: str) -> None:
    """renameat2(..., RENAME_NOREPLACE) through ctypes: the one move the
    restore makes (aside, put-in, put-back), which never replaces anything
    (EEXIST). ``where`` names the move in its error."""
    if not _LIBC:
        try:
            import ctypes

            fn = ctypes.CDLL(None, use_errno=True).renameat2
            fn.argtypes = (
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            )
            _LIBC.append((ctypes, fn))
        except (ImportError, OSError, AttributeError):
            raise OSError(errno.ENOSYS, "renameat2 is not available", where) from None
    ctypes, fn = _LIBC[0]
    if fn(sfd, os.fsencode(sname), dfd, os.fsencode(dname), RENAME_NOREPLACE) != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), where)


def _dirfd(fd: int, rel: str, make: bool = False, mode: int = 0o777) -> int:
    """An fd of the directory ``rel`` below the directory ``fd``, every
    component opened without following a link (review L-1: no path-based
    call through a parent that may since be a symlink); a missing one made
    when ``make`` (os.mkdir, one at a time, never os.makedirs)."""
    fd = os.dup(fd)
    try:
        for part in (x for x in rel.split("/") if x):
            try:
                nfd = os.open(part, _DIR, dir_fd=fd)
            except FileNotFoundError:
                if not make:
                    raise
                os.mkdir(part, mode, dir_fd=fd)
                nfd = os.open(part, _DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _why(exc: OSError) -> str:
    if exc.errno == errno.EXDEV:
        return "other:cross-device"
    if exc.errno in (errno.EINVAL, errno.ENOSYS):
        return "other:no-noreplace"
    known = (errno.EEXIST, errno.ENOTEMPTY, errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
    return "changed-during-restore" if exc.errno in known else "other:write-failed"


class _Whole(Exception):
    """The whole report refused, by name, before anything is written."""


class _Part(Exception):
    """One part refused at write time; ``note`` says where things are."""

    def __init__(self, reason: str, note: str = "") -> None:
        super().__init__(reason)
        self.reason, self.note = reason, note


class _Stop(Exception):
    """The run stops (a record it cannot write, review L-7), naming why."""


def _mine(st: os.stat_result, link: bool = False) -> bool:
    return st.st_uid == os.geteuid() and (link or not st.st_mode & 0o022)


def _anchor(home: str) -> int:
    """deploy-backups, opened without following a link from the resolved home
    (review L-6), and this user's with no group or world write; its fd. Every
    later read goes through it (/proc/self/fd), never through a path someone
    who can write the home could swap."""
    hfd = os.open(os.path.realpath(home), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fd = os.open("deploy-backups", _DIR, dir_fd=hfd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise _Whole(f'other:not-owned "deploy-backups" ({exc.strerror})') from None
    finally:
        os.close(hfd)
    if not _mine(os.fstat(fd)):
        os.close(fd)
        raise _Whole('other:not-owned "deploy-backups"')
    return fd


def _origin(root: str, name: str) -> None:
    """The origin rule (owner rulings ced82b49, 73b6285d), before REPAIR.json
    is parsed: the report dir and every node below it (never following a
    link, at most ENTRIES of them, review N-5) are this user's, and every one
    but a symlink (its mode means nothing) has no group or world write.
    deploy-backups passed the same test (_anchor), so whoever can write the
    home could only swap in a folder that fails it, or a 0700 one of this
    user's they can never fill. A writer with this user's uid is out of scope:
    it can already edit the checkout."""
    top, left = os.path.join(root, name), ENTRIES
    try:
        st = os.lstat(top)
    except FileNotFoundError:
        raise _Whole("other:unreadable (no such report)") from None
    if not stat.S_ISDIR(st.st_mode) or not _mine(st):
        raise _Whole(f"other:not-owned {json.dumps(name)}")
    for d, dirs, files in os.walk(top, onerror=_raise):
        for n in dirs + files:
            left -= 1
            if left < 0:
                raise _Whole(f"other:too-large (the report holds over {ENTRIES} nodes)")
            st = os.lstat(os.path.join(d, n))
            if not _mine(st, link=stat.S_ISLNK(st.st_mode)):
                rel = os.path.relpath(os.path.join(d, n), root)
                raise _Whole(f"other:not-owned {json.dumps(rel)[:200]}")


def _open(rp, repo: str, home: str, ts: str, say, write: bool):
    """Every gate a --restore TS run passes before it judges or writes: the
    ts, the origin rule, the loader, the validators, the integrity record (a
    restore refuses a report without one whole, review N-3), no node part
    below another (review LOW-2: the writer never writes one), the newer
    reports, then B1's judge. (judge, parts, loaded, ctx), or an exit code
    once it has said why."""
    nothing = "nothing was written" if write else "nothing judged"
    shown = os.path.join(home, "deploy-backups")
    try:
        _order(ts)
        bfd = _anchor(home)
    except ValueError:
        say(f"report {ts} other:unreadable (its ts names no date); {nothing}")
        return 1
    except FileNotFoundError:
        say(f"report {ts} other:unreadable (no such report); {nothing}")
        return 1
    except _Whole as exc:
        say(f"report {ts} {exc}; {nothing}")
        return 1
    root = f"/proc/self/fd/{bfd}"
    ctx = types.SimpleNamespace(bfd=bfd, root=root, shown=shown, ts=ts, name=ts + rp.SUFFIX)
    ctx.backup, ctx.shown_backup = os.path.join(root, ctx.name), os.path.join(shown, ctx.name)
    try:
        got = _judged(rp, repo, ctx, say, write, nothing)
    except BaseException:
        os.close(bfd)
        raise
    if isinstance(got, str):
        os.close(bfd)
        say(f"report {ts} {got}; {nothing}")
        return 1
    return got


def _judged(rp, repo: str, ctx, say, write: bool, nothing: str):
    """_open's gates past the anchor: (judge, parts, loaded, ctx), or why not."""
    ts, backup = ctx.ts, ctx.backup

    def unreadable(exc: OSError) -> str:
        return f"other:unreadable (a path cannot be read: {str(exc).replace(ctx.root, ctx.shown)})"

    try:
        _origin(ctx.root, ctx.name)
        loaded = rp.load_report(backup, repo)
        if loaded is None:
            raise rp.BadReport("it holds no REPAIR.json")
    except _Whole as exc:
        return str(exc)
    except OSError as exc:
        return unreadable(exc)
    except rp.BadReport as exc:
        return f"other:unreadable ({exc})"
    if why := problem(loaded, rp.REPORT_BYTES, rp._parents):
        return f"other:bad-report ({why})"
    records, report = None, loaded["report"]
    if "integrity" not in report:
        if write:
            return "other:no-integrity (written before SAVED.json)"
        say(f"report {ts}: no integrity record (written before SAVED.json); a restore refuses it")
    else:
        named = report["integrity"] == "SAVED.json"
        why, saved = _saved(rp, backup) if named else ("integrity names another file", None)
        if why:
            return f"other:bad-report (SAVED.json: {why})"
        records = saved["files"]
    nodes = {r["path"] for r in loaded["rows"] if r["class"] == "foreign"
             and r["disk_changed"] and not r["unreachable_through"]}  # fmt: skip
    whole = {r["path"] for r in loaded["rows"] if r["disk"] == "dir" and r["disk_changed"]}
    for p in nodes:
        if whole.isdisjoint(rp._parents(p)) and not nodes.isdisjoint(rp._parents(p)):
            return f"other:bad-report (a node part below another: {json.dumps(p)[:200]})"
    newer, why = _newer_paths(rp, repo, ctx.root, ts)
    if newer is None:
        return f"other:newer-report-unreadable ({why})"
    try:
        judge = Judge(rp, repo, backup, loaded, newer, records=records, runs=ctx.root,
                      shown=ctx.shown, strict=write)  # fmt: skip
    except rp.RepairError as exc:
        return f"other:tree-unreadable (HEAD's or the tag's tree: {exc})"
    try:
        parts = [part for row in judge.rows if row["class"] == "foreign" for part in judge.row(row)]
    except OSError as exc:
        return unreadable(exc)
    return judge, parts, loaded, ctx


def _line(path: str, part: str, outcome: str, note: str | None) -> str:
    return f"{json.dumps(path)} {part} {outcome}" + (f" ({note})" if note else "")


def check(rp, repo: str, home: str, ts: str, say) -> int:
    """``--restore TS --check``: judge the report ``ts`` names, print one line
    per part and a summary; 0 if every part is as-left or already-restored."""
    got = _open(rp, repo, home, ts, say, write=False)
    if isinstance(got, int):
        return got
    judge, parts, loaded, ctx = got
    os.close(ctx.bfd)
    for path, part, verdict in parts:
        say(_line(path, part, verdict, judge.notes.get((path, part))))
    counts = {k: sum(1 for *_, v in parts if v == k) for k in ("as-left", "already-restored")}
    other = len(parts) - sum(counts.values())
    say(
        f"report {ts}: {len(parts)} part(s): {counts['as-left']} as-left,"
        f" {counts['already-restored']} already-restored, {other} other; nothing was written"
    )
    if judge.blockers:
        say("newer report(s) holding an overlapping path: " + ", ".join(sorted(judge.blockers)))
    if loaded["report"].get("index_rebuilt"):
        say(
            "the index was torn when the repair ran: its staged entries are kept only as"
            " git/index in the report's backup, and are not judged here"
        )
    return 1 if other else 0


def restore(rp, repo: str, home: str, ts: str, say) -> int:
    """``--restore TS``: put back every part the judge calls as-left, under the
    repair's locks (held by the caller), refusing every other part by name.
    0 when nothing was refused, else 1."""
    got = _open(rp, repo, home, ts, say, write=True)
    if isinstance(got, int):
        return got
    judge, parts, loaded, ctx = got
    try:
        return _Writer(rp, repo, ctx, judge, parts, loaded, say).run()
    finally:
        os.close(ctx.bfd)


class _Writer:
    """One restore run. Per node part (the in-flight one first, review SF-C):
    STAGE the saved node in <run>/new/<path> (each file's sha256 checked
    against SAVED.json as it is copied, its mode the record's & 0o777); record
    the part in flight; move what is at the path to <run>/aside/<path>;
    re-judge the aside (review SF-1: a session's write in between is put back,
    refused); PUT IN with renameat2(RENAME_NOREPLACE) (a node that appeared
    meanwhile is never replaced); judge the aside again (review L-2); record.
    Then the index parts in one update-index call, the flags, a refresh, and a
    second judge that must read already-restored for every part written."""

    def __init__(self, rp, repo, ctx, judge, parts, loaded, say) -> None:
        self.rp, self.repo, self.ctx, self.j, self.say = rp, repo, ctx, judge, say
        self.parts, self.loaded = parts, loaded
        self.verdict = {(p, k): v for p, k, v in parts}
        # self.refused: the rows with a refused part, kept by _refuse (review W1).
        self.out, self.notes, self.refused = {}, dict(judge.notes), set()
        self.prior = restore_block(loaded["report"])
        self.run_name = self.run_fd = self.repo_fd = None
        self.wrote, self.extras, self.flight = [], [], None

    def run(self) -> int:
        j = self.j
        for (p, k), v in self.verdict.items():
            if v == "already-restored":
                self.out[(p, k)] = v
            elif v != "as-left":
                self._refuse(p, k, v)
        nodes = sorted(
            (r for r in j.rows if self.verdict.get((r["path"], "node")) == "as-left"),
            key=lambda r: (r["path"] != j.resumed, r["path"]),
        )
        self.repo_fd = os.open(self.repo, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            for row in nodes:
                self._node(row)
            for (p, k), v in self.verdict.items():
                if k == "entry" and v == "as-left":
                    self.out[(p, k)] = self.out[(j.entry_of[p], "node")]
            _step("index")
            self._index()
            self._flags()
            if self.wrote:
                self.rp._git(self.repo, "update-index", "-q", "--refresh", check=False)
            _step("post-judge")
            self._post()
            self._finish()
        except _Stop as exc:
            self._report()
            self.say(str(exc))
            return 1
        finally:
            for fd in (self.repo_fd, self.run_fd):
                if fd is not None:
                    os.close(fd)
        return self._report()

    # ── the record in REPAIR.json (review SF-B, SF-C, SF-10) ────────────────

    def _save(self, inflight, refused: int | None = None, restored_at: str | None = None) -> None:
        """Re-read REPAIR.json (under update.lock: --dismiss takes it too) and
        write its `restore` block; a failure stops the run (review L-7)."""
        rp, prior = self.rp, self.prior or {}
        kept = prior.get("refused", 0) if refused is None else refused
        block = {"inflight": inflight, "refused": kept, "restored_at": restored_at}
        try:
            loaded = rp.load_report(self.ctx.backup, self.repo)
            if loaded is None:
                raise rp.BadReport("REPAIR.json is gone")
            report = {**loaded["report"], "restore": block}
            rp.write_json(os.path.join(self.ctx.backup, "REPAIR.json"), report, private=True)
        except (OSError, rp.BadReport) as exc:
            part = json.dumps(self.flight) + " node" if self.flight else "no part"
            folder = os.path.join(self.ctx.shown, self.run_name) if self.run_name else "none made"
            raise _Stop(
                f"stopped: REPAIR.json could not be written ({exc}); in flight: {part};"
                f" the run folder: {folder}"
            ) from None

    def _run(self) -> str:
        """The run folder, made at the first write (review NOTE-2), 0700."""
        if self.run_name is None:
            name = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}{RUN_SUFFIX}"
            try:
                os.mkdir(name, 0o700, dir_fd=self.ctx.bfd)
                self.run_fd = os.open(name, _DIR, dir_fd=self.ctx.bfd)
            except OSError as exc:
                raise _Stop(f"stopped: the run folder {name} cannot be made ({exc})") from None
            self.run_name = name
        return self.run_name

    # ── one node part ───────────────────────────────────────────────────────

    def _node(self, row: dict) -> None:
        p, j = row["path"], self.j
        flying = p == j.resumed  # REPAIR.json names it in flight
        moved = j.inflight[0] if flying else None  # the run whose aside holds what was here
        staged = None
        self.flight = p
        try:
            staged = self._stage(row)
            if not flying:
                self._save([self._run(), p, "node"])
                flying = True
                _step("intent")
                if j.left_is_node(p):
                    self._move(p, self.run_name, to_aside=True)
                    moved = self.run_name
                    _step("aside")
                    if not self._still_left(p, moved):
                        raise _Part("changed-during-restore", "it changed after it was judged")
                    self._extras(p, moved)
            if staged is not None:
                d, name = os.path.split(p)
                dst = _dirfd(self.repo_fd, d, make=True)
                try:
                    _renameat2(staged, name, dst, name, "put-in")
                finally:
                    os.close(dst)
                _step("put-in")
                if moved is not None and not self._still_left(p, moved):
                    where, moved = self._aside(p, moved), None  # the path is the restore's now
                    raise _Part("changed-during-restore", f"written after it was moved to {where}")
            self.out[(p, "node")] = "restored"
            self.wrote.append(p)
        except (_Part, OSError) as exc:
            reason = exc.reason if isinstance(exc, _Part) else _why(exc)
            note = exc.note if isinstance(exc, _Part) else f"{exc.filename or ''}: {exc.strerror}"
            if moved is not None and not self._move(p, moved, to_aside=False):
                note = f"{note}; what was at the path is kept at {self._aside(p, moved)}"
            self._refuse(p, "node", reason, note)
        finally:
            if staged is not None:
                os.close(staged)
        if flying:
            self._save(None)
        self.flight = None

    def _aside(self, p: str, run: str) -> str:
        return os.path.join(self.ctx.shown, run, "aside", p)

    def _move(self, p: str, run: str, to_aside: bool) -> bool:
        """The node at ``p`` into <run>/aside/<p> (raises), or back (False if it cannot)."""
        d, name = os.path.split(p)
        try:
            here = _dirfd(self.repo_fd, d)
        except OSError:
            if to_aside:
                raise
            return False
        try:
            there = _dirfd(self.ctx.bfd, f"{run}/aside/{d}", make=to_aside, mode=0o700)
            try:
                if to_aside:
                    _renameat2(here, name, there, name, "aside")
                else:
                    _renameat2(there, name, here, name, "put-back")
            finally:
                os.close(there)
        except OSError:
            if to_aside:
                raise
            return False
        finally:
            os.close(here)
        return True

    def _still_left(self, p: str, run: str) -> bool:
        try:
            return self.j._left(p, root=os.path.join(self.ctx.root, run, "aside"), strict=True)
        except (OSError, self.rp.TooLarge):
            return False

    def _extras(self, p: str, run: str) -> None:
        """Review SF-D: ignored files a tag directory moved aside held, to name."""
        j = self.j
        if p in j.tag or p not in j.tag_dirs:
            return
        with contextlib.suppress(OSError, self.rp.TooLarge):
            tagged = j._tag_below(p)
            have = j._walk(os.path.join(self.ctx.root, run, "aside"), p)
            self.extras += [f"{p}/{q}" for q in sorted(have) if q not in tagged]

    # ── staging in <run>/new ────────────────────────────────────────────────

    def _stage(self, row: dict) -> int | None:
        """The new node, built at <run>/new/<path>; its parent's fd. None for
        a node that was absent (review SF-9: the restore only moves aside)."""
        p, disk = row["path"], row["disk"]
        if disk == "absent":
            return None
        self._run()
        d, name = os.path.split(p)
        fd = _dirfd(self.run_fd, f"new/{d}", make=True, mode=0o700)
        try:
            if disk == "dir":
                self._stage_dir(row, fd, name)
            elif disk == "other":  # a FIFO (review N-8: its row's mode); sockets: cannot-make
                os.mkfifo(name, 0o600, dir_fd=fd)
                os.chmod(name, int(_fifo(row["special"]["mode"])[1], 8), dir_fd=fd)
            else:
                self._stage_one(row["saved_as"], fd, name)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _stage_dir(self, row: dict, fd: int, name: str) -> None:
        """A directory row built whole (review SF-2): every entry the judge
        restores, the update's own bytes left out; the directories the
        umask's, as a checkout makes them (git records no directory mode)."""
        os.mkdir(name, 0o777, dir_fd=fd)
        top = os.open(name, _DIR, dir_fd=fd)
        try:
            for q in sorted(self.j.dirs[row["path"]]):
                qd, qn = os.path.split(q)
                sub = _dirfd(top, qd, make=True)
                try:
                    self._stage_one(f"{row['saved_as']}/{q}", sub, qn)
                finally:
                    os.close(sub)
        finally:
            os.close(top)

    def _stage_one(self, rel: str, fd: int, name: str) -> None:
        rec = self.j.records.get(rel)
        if rec is None:
            raise _Part("other:no-hash", f"SAVED.json records no {rel}")
        src = os.path.join(self.ctx.backup, rel)
        try:
            st = os.lstat(src)
        except OSError:
            raise _Part("other:not-saved", f"{rel} is gone") from None
        bad = _Part("other:hash-mismatch", f"{rel} is not what SAVED.json records")
        if rec["type"] == "symlink":
            if not stat.S_ISLNK(st.st_mode) or os.readlink(src) != rec["target"]:
                raise bad
            os.symlink(rec["target"], name, dir_fd=fd)
        elif rec["type"] == "fifo":
            if not stat.S_ISFIFO(st.st_mode):
                raise bad
            os.mkfifo(name, 0o600, dir_fd=fd)
            os.chmod(name, int(_fifo(rec["mode"])[1], 8), dir_fd=fd)
        elif rec["sha256"] is None:
            raise _Part("other:too-large", f"{rel} was over the hash budget")
        else:
            self._copy(src, rec, fd, name, bad)

    def _copy(self, src: str, rec: dict, fd: int, name: str, bad: _Part) -> None:
        """Copy a saved file, hashing it as it goes; refused unless its sha256
        is the record's. The mode: the record's & 0o777 (owner ruling)."""
        try:
            sfd = os.open(src, _READ)
        except OSError:
            raise bad from None
        try:
            if not stat.S_ISREG(os.fstat(sfd).st_mode):
                raise bad
            out = os.open(name, _NEW, 0o600, dir_fd=fd)
            try:
                h = hashlib.sha256()
                while chunk := os.read(sfd, 1 << 20):
                    h.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        view = view[os.write(out, view) :]
                _step("stage")
                if h.hexdigest() != rec["sha256"]:
                    raise bad
                self.rp._copy_xattrs(sfd, out)  # the saved copy kept them (#3974 item 7)
                os.fchmod(out, int(rec["mode"], 8) & 0o777)
            finally:
                os.close(out)
        finally:
            os.close(sfd)

    # ── the index, the flags (§5) ───────────────────────────────────────────

    def _refuse(self, p: str, kind: str, why: str, note: str = "") -> None:
        self.out[(p, kind)] = f"refused:{why}"
        self.refused.add(self.j.entry_of.get(p, p))
        if note:
            self.notes[(p, kind)] = note

    # The row's node part (and ``kinds``), and a covering directory row's node
    # part (review N4), each written or already restored.
    def _back(self, p: str, *kinds: str) -> bool:
        keys = [(q, "node") for q in (p, *self.rp._parents(p))] + [(p, k) for k in kinds]
        return all(self.out.get(key, "restored") in _BACK for key in keys)

    def _index(self) -> None:
        """The index parts whose disk part (if any) is back, in ONE update-index
        call. Its real footprint is git's directory/file rule, not a drop list
        (review SF-A, MEASURED there): a stage-0 record drops every entry at
        and under its path and any file entry at a parent. So right before the
        call the whole index is read once; a part whose footprint holds an
        entry that is not one of this report's rows as the judge read it (no
        part of that row refused) is refused, and its parents `child-refused`;
        each footprint is dropped explicitly. After the call the WHOLE index
        must be the one expected. Residual (owner ruling): a session's git
        that stages between that read and the call, milliseconds; git has no
        compare-and-swap for the index."""
        j, rp = self.j, self.rp
        todo = sorted(p for (p, k), v in self.verdict.items() if k == "index" and v == "as-left")
        if not todo:
            return
        rows = {r["path"]: r for r in j.rows}
        paths = sorted(rows)

        def under(keys: list[str], p: str) -> list[str]:
            i, out = bisect.bisect_left(keys, p + "/"), []
            while i < len(keys) and keys[i].startswith(p + "/"):
                out.append(keys[i])
                i += 1
            return out

        def stage0(p: str) -> bool:  # a record that drops what is below it
            return any(e[2] == "0" for e in rows[p]["index"])

        go = []
        for p in todo:
            if not self._back(p):
                self._refuse(p, "index", "row-refused")
            elif stage0(p) and not self.refused.isdisjoint(under(paths, p)):  # SF-5
                self._refuse(p, "index", "child-refused")
            elif bad := self._blobs(rows[p]):
                self._refuse(p, "index", "other:hash-mismatch", bad)
            else:
                go.append(p)
        pre = rp._index_state(self.repo)[0]
        keys, feet = sorted(pre), {}
        for p in go:
            # What git drops: a stage-0 record, every entry under it and a file
            # entry at a parent; any record, the entries at its path. A parent's
            # entry is checked for any record (an unmerged one too: unmeasured).
            drop = [p]
            if stage0(p):
                drop += under(keys, p) + [d for d in rp._parents(p) if d in pre]
            look = drop + [d for d in rp._parents(p) if d in pre and d not in drop]
            stray = [q for q in look if q not in rows or q in self.refused
                     or pre.get(q, []) != j.index.get(q, [])]  # fmt: skip
            if stray:
                why = f"{json.dumps(stray[0])[:200]} changed since it was judged"
                self._refuse(p, "index", "changed-during-restore", why)
            else:
                feet[p] = drop
        while dropped := [
            p for p in feet if stage0(p) and not self.refused.isdisjoint(under(paths, p))
        ]:
            for p in dropped:
                del feet[p]
                self._refuse(p, "index", "child-refused")
        if not feet:
            return
        gone = {q for f in feet.values() for q in f}  # each footprint holds its own path
        kept = self._kept(pre, gone)
        null = "0" * (2 * hashlib.new(j.fmt).digest_size)
        recs = []
        for p in sorted(feet):
            recs += [f"0 {null}\t{q}" for q in feet[p]]
            recs += [f"{m} {o} {s}\t{p}" for m, o, s in rows[p]["index"]]
        data = b"".join(os.fsencode(r) + b"\0" for r in recs)
        try:
            rp._git(self.repo, "update-index", "-z", "--index-info", data=data)
        except rp.RepairError as exc:  # review NOTE-7: a session's git holds index.lock
            for p in feet:
                self._refuse(p, "index", "index-locked", str(exc)[:300])
            return
        self.wrote += list(feet)
        _step("index-call")
        want = {q: v for q, v in pre.items() if q not in gone}
        want.update((p, [tuple(e) for e in rows[p]["index"]]) for p in feet if rows[p]["index"])
        ok = rp._index_state(self.repo)[0] == want
        for p in feet:
            if ok:
                self.out[(p, "index")] = "restored"
            else:
                self._refuse(p, "index", "index-skipped", kept)

    # Review W2: the entries the call replaces, as `ls-files -s -z` records, in
    # <run>/index-before.txt (0600), written before it runs; the note naming it.
    def _kept(self, pre: dict, gone: set) -> str:
        shown = os.path.join(self.ctx.shown, self._run(), "index-before.txt")
        recs = [f"{m} {o} {s}\t{q}" for q in sorted(gone) for m, o, s in pre.get(q, ())]
        try:
            fd = os.open("index-before.txt", _NEW, 0o600, dir_fd=self.run_fd)
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"".join(os.fsencode(r) + b"\0" for r in recs))
        except OSError as exc:
            raise _Stop(f"stopped: {shown}: {exc}; the index is as it was") from None
        return f"the index is not the one written; the one before the call is in {shown}"

    def _blobs(self, row: dict) -> str:
        """Each saved blob the object store lacks, written back; why not, or ''."""
        for mode, oid, _stage in row["index"]:
            if mode == "160000" or oid in self.j.stored:
                continue
            # Read here, through the anchored folder, and given to git on stdin:
            # a /proc/self/fd path means nothing in git's own process.
            try:
                fd = os.open(os.path.join(self.ctx.backup, "index-blobs", oid), _READ)
                try:
                    data = os.read(fd, os.fstat(fd).st_size + 1)
                finally:
                    os.close(fd)
            except OSError:
                return f"index-blobs/{oid} cannot be read"
            args = ("hash-object", "-w", "--no-filters", "--stdin")
            got = self.rp._git(self.repo, *args, check=False, data=data)
            if got.returncode or os.fsdecode(got.stdout.strip()) != oid:
                return f"index-blobs/{oid} does not hash to its name"
        return ""

    def _flags(self) -> None:
        """Flags after the index call (it re-creates entries). Review N-1: S
        skip-worktree, a lowercase tag assume-unchanged, s both."""
        for (p, k), v in sorted(self.verdict.items()):
            if k != "flag" or v != "as-left":
                continue
            if not self._back(p, "index"):
                self._refuse(p, "flag", "row-refused")
                continue
            flag = self.j.foreign[p]["flag"]
            opts = [o for o, on in (("--skip-worktree", flag in "Ss"),
                                    ("--assume-unchanged", flag.islower())) if on]  # fmt: skip
            try:
                for opt in opts:
                    self.rp._git(self.repo, "update-index", opt, "--", p)
            except self.rp.RepairError as exc:
                self._refuse(p, "flag", "flag-failed", str(exc)[:300])
                continue
            self.out[(p, "flag")] = "restored"
            self.wrote.append(p)

    # ── after the writes (§6) ───────────────────────────────────────────────

    def _post(self) -> None:
        """A second judge: every part written must now read already-restored
        (review N-4: a disagreement counts as refused)."""
        if not self.wrote:
            return
        loaded = {**self.loaded, "report": {**self.loaded["report"], "restore": None}}
        try:
            j = Judge(self.rp, self.repo, self.ctx.backup, loaded, self.j.newer_ts,
                      records=self.j.records, strict=True)  # fmt: skip
            again = {(p, k): v for r in j.rows if r["class"] == "foreign" for p, k, v in j.row(r)}
        except (OSError, self.rp.RepairError):
            again = {}
        for key, o in list(self.out.items()):
            if o == "restored" and again.get(key) != "already-restored":
                self._refuse(*key, "post-check", f"the judge now reads {again.get(key, 'nothing')}")

    def _finish(self) -> None:
        """restored_at only when nothing was refused (review SF-4); a re-run
        that changes nothing writes nothing."""
        refused = sum(o.startswith("refused:") for o in self.out.values())
        stamp = None if refused else time.strftime("%FT%TZ", time.gmtime())
        p = self.prior
        same = p and p["inflight"] is None and p["refused"] == refused
        if same and self.run_name is None and (p["restored_at"] is None) == (stamp is None):
            return
        self._save(None, refused, stamp)

    def _report(self) -> int:
        say, ts, shown = self.say, self.ctx.ts, self.ctx.shown_backup
        out = [(p, k, self.out.get((p, k), "refused:not-reached")) for p, k, _v in self.parts]
        for p, k, o in out:
            say(_line(p, k, o, self.notes.get((p, k))))
        n = {x: sum(1 for *_, o in out if o == x) for x in ("restored", "already-restored")}
        refused = len(out) - sum(n.values())
        say(
            f"report {ts}: {len(out)} part(s): {n['restored']} restored,"
            f" {n['already-restored']} already-restored, {refused} refused"
        )
        if done := sorted({p for p, _k, o in out if o == "restored"}):
            more = f" and {len(done) - 50} more" if len(done) > 50 else ""
            say(
                "Restored: " + ", ".join(json.dumps(p) for p in done[:50]) + more + ". The"
                " checkout now holds them as uncommitted changes: tracked edits make"
                " deploy_code_only.sh and update.sh refuse to deploy until they are committed"
                " (on a branch, as a PR), moved out or removed; untracked files restored under"
                " src/, config/ or pyproject.toml block deploy_code_only.sh, untracked files"
                " elsewhere block neither. Nothing was restarted: a server that imports from a"
                " directory moved aside fails there until it restarts."
            )
        if self.run_name:
            folder = os.path.join(self.ctx.shown, self.run_name)
            say(f"What the restore replaced is in {folder}, pruned 90 days after its last write.")
        if self.extras:
            named = ", ".join(json.dumps(q) for q in self.extras[:50])
            say(f"Moved aside with their directory (git ignores them): {named}")
        if lost := sum(1 for *_, o in out if o[len("refused:") :] in _PERMANENT):
            say(
                f"{lost} part(s) the restore cannot write: put them back by hand from {shown}/tree"
                " (a saved directory's empty subdirectories are not rebuilt, and stay there),"
                f" then: scripts/deploy_candidates repair-move --dismiss {ts}"
            )
        return 1 if refused else 0
