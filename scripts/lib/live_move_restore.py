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
- ``already-restored``: it holds the saved state, every saved copy compared
  still the one SAVED.json records (a re-run converges);
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
whose bytes are not the move target's, or whose copy is not the one SAVED.json
records (when the target cannot be read, only those that are foreign rows
themselves); the restore builds the directory from those. A node part a
killed restore left half-done reads as-left by the resume rule
(Judge._resume), and so does the S a restore killed between a both-flags
entry's two writes left; the node part REPAIR.json names in flight reads
already-restored only while its aside is still the state moved there
(Judge._landed), and while one refused leaves its aside holding something
the restore did not move there every other as-left part is
``other:inflight-unresolved`` (Judge.unresolved: the record is kept, and it
has one slot; moving that content out releases them, and the in-flight path
is then judged by what it holds: the saved node, which a kill after the
put-in left there, reads already-restored; a path a kill after the aside
left empty, or one holding whatever appeared there after a failed put-back,
goes back by hand or the report is dismissed). ``check`` writes nothing;
``restore`` writes only parts judged as-left, each self-verifying (see _Writer)."""

from __future__ import annotations

import bisect
import calendar
import contextlib
import hashlib
import json
import os
import re
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
    shapes. Only the shape here: whether a copy still matches its record the
    judge checks before it reads a part already-restored (Judge._kept), and the
    restore again as it copies it."""
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
    """``records``: SAVED.json's files (a FIFO's restored mode is its record's;
    None for a report written before it, which only --check judges).
    ``runs``: deploy-backups as read (restore run folders, for the resume rule),
    ``shown`` as named in output; ``fd`` the descriptor ``runs`` names, handed
    to git when it reads an aside. ``strict``: the restore's write gate (a tag
    directory moved aside may hold no extra file git does not ignore)."""

    def __init__(self, rp, repo: str, backup: str, loaded: dict, newer: dict, *, records=None,
                 runs=None, shown=None, strict=False, fd=None) -> None:  # fmt: skip
        self.rp, self.repo, self.backup = rp, repo, backup
        self.budget, self.blockers = _Budget(rp), set()
        # _kept re-reads saved copies the walk already read once: its own budget,
        # so the integrity check never spends the walk's and turns a directory
        # that restored correctly into other:too-large.
        self.kept_budget = _Budget(rp)
        self.aside_budget = _Budget(rp)  # unresolved()'s one read of the in-flight aside
        report, self.rows = loaded["report"], loaded["rows"]
        self.records, self.runs, self.shown, self.strict = records or {}, runs, shown, strict
        self.integrity, self.git_dir = records is not None, None
        self.fds = () if fd is None else (fd,)
        self.record_keys = sorted(self.records)  # _dir's per-row range, by bisect
        block = restore_block(report)
        self.inflight = block["inflight"] if block else None
        self.notes, self.dirs, self.entry_of, self.resumed = {}, {}, {}, None
        self.held = None  # the unresolved in-flight aside, shown (set by _judged)
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

    def _ignored(self, paths: list[str], aside: str | None = None, below: str = "") -> set:
        """Which of ``paths`` git ignores: one `check-ignore`; none when it fails.
        ``aside``: a root holding the directory ``below`` moved out of the
        checkout (#3986 review, finding 3). Its own ignore rules moved with it,
        so a rule inside it (git's most specific) decides, read in the aside;
        else the checkout's rules outside it do."""
        if not paths:
            return set()
        if aside is None:
            p = self._check_ignore(["-C", self.repo], paths, verbose=False)
            return set(self.rp._z(p.stdout)) if p.returncode in (0, 1) else set()
        if self.git_dir is None:
            self.git_dir = self.rp._out(self.repo, "rev-parse", "--absolute-git-dir", check=False)
        inner = self._rules(["-C", aside, "--git-dir", self.git_dir, "--work-tree", aside], paths)
        outer = self._rules(["-C", self.repo], paths)
        out = set()
        for q in paths:
            src, pat = inner.get(q, ("", ""))
            if not src.startswith(below + "/"):
                src, pat = outer.get(q, ("", ""))
                if src.startswith(below + "/"):  # a node the restore put there: not the aside's
                    continue
            if pat and not pat.startswith("!"):
                out.add(q)
        return out

    def _rules(self, where: list[str], paths: list[str]) -> dict:
        """`check-ignore -v -n`: path -> (source, pattern) of the last rule
        matching it (a negation starts with "!"); {} when it fails."""
        p = self._check_ignore(where, paths, verbose=True)
        if p.returncode not in (0, 1):
            return {}
        f = [os.fsdecode(x) for x in p.stdout.split(b"\0")]
        return {f[i + 3]: (f[i], f[i + 2]) for i in range(0, len(f) - 3, 4)}

    def _check_ignore(self, where: list[str], paths: list[str], verbose: bool):
        # An aside below /proc/self/fd/N is only reachable by git through fd N.
        return subprocess.run(
            ["git", *self.rp.GIT_C, *where, "check-ignore", "-z", "--stdin",
             *(("-v", "-n") if verbose else ())],
            input=b"".join(os.fsencode(q) + b"\0" for q in paths),
            capture_output=True,
            env=_git_env(),
            pass_fds=self.fds,
        )  # fmt: skip

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
            aside = None if root == self.repo else root
            return all(have.get(q) == e for q, e in tagged.items()) and (
                set(extra) <= self._ignored(extra, aside, p)
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
        rel = row["saved_as"]
        done = lambda: (  # noqa: E731
            self._state(self.repo, p) == saved and (not rel or self._kept(rel)) and self._landed(p)
        )
        return self._verdict(link, done, gate, lambda: self._as_left(p))

    def _landed(self, p: str) -> bool:
        """The in-flight part reads already-restored only while its aside is
        still what the restore moved there (#3986 review): one killed after its
        put-in, before it judged the aside again, may have left a write through
        an open fd there, and clearing the in-flight record would lose it. No
        aside (the path was empty) holds nothing to lose."""
        inf = self.inflight
        if not inf or inf[1] != p or self.runs is None:
            return True
        aside = os.path.join(self.runs, inf[0], "aside")
        if self.rp._link_parent(aside, p) is None and not os.path.lexists(os.path.join(aside, p)):
            return True
        if self._left(p, root=aside, strict=True):  # a link parent: never as-left
            return True
        where = os.path.join(self.shown, inf[0], "aside", p)
        self.notes[(p, "node")] = f"newer content kept at {where}"
        return False

    def unresolved(self, parts) -> str | None:
        """The aside REPAIR.json's in-flight part left, as shown, while that
        part is neither as-left nor already-restored and its aside holds
        something that is not the state the restore moved there (#3986 review);
        else None. The record then stays, and since it
        has one slot, nothing else is written (live_move_restore_write) until
        what is there is moved out: every rerun names it."""
        inf = self.inflight
        if not inf or self.runs is None:
            return None
        # Only a verdict that judged the aside itself settles it: already-restored
        # (Judge._landed) and an as-left the resume rule read (path empty, aside
        # the as-left state). An as-left checkout (the tag's node put back at the
        # path) never looked at the aside, which may hold newer content.
        v = next((v for p, k, v in parts if p == inf[1] and k == "node"), None)
        if v == "already-restored" or (v == "as-left" and self.resumed == inf[1]):
            return None
        aside = os.path.join(self.runs, inf[0], "aside")
        if self.rp._link_parent(aside, inf[1]) is None and not os.path.lexists(
            os.path.join(aside, inf[1])
        ):
            return None
        # Only content the restore did not move there holds the record: an aside
        # still holding the repair's own as-left copy (a gate refusing the part
        # after a kill) is what git gives back, and must not stop the rest. Read
        # on its own budget: the rows judged before it may have spent the walk's.
        spent, self.budget = self.budget, self.aside_budget
        try:
            if self._left(inf[1], root=aside, strict=True):
                return None
        except (OSError, self.rp.TooLarge):
            pass  # unreadable: never taken for the as-left state
        finally:
            self.budget = spent
        return os.path.join(self.shown, inf[0], "aside", inf[1])

    def _kept(self, rel: str) -> bool:
        """Is the backup's copy at ``rel`` the one SAVED.json records (#3986
        review, finding 1)? A copy changed to match the checkout is not the
        saved state, so nothing reads already-restored from it (and the
        writer's staging check refuses it). A file recorded with no hash cannot
        be told apart, so it never is either. A report written before SAVED.json
        has nothing to check against (only --check judges one)."""
        if not self.integrity:
            return True
        rec, full = self.records.get(rel), os.path.join(self.backup, rel)
        try:
            if rec is None or rec["type"] == "symlink":
                return rec is not None and os.readlink(full) == rec["target"]
            if rec["type"] == "fifo":
                return stat.S_ISFIFO(os.lstat(full).st_mode)
            if rec["sha256"] is None:
                return False
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            try:
                st, h = os.fstat(fd), hashlib.sha256()
                if not stat.S_ISREG(st.st_mode):
                    return False
                self.kept_budget(st.st_size)
                while chunk := os.read(fd, 1 << 20):
                    h.update(chunk)
            finally:
                os.close(fd)
        except OSError:
            return False
        return h.hexdigest() == rec["sha256"]

    def _as_left(self, p: str) -> bool:
        return self._left(p, strict=self.strict) or self._resume(p)

    def _dir(self, row: dict, link, gate: str | None) -> list[tuple[str, str, str]]:
        """A directory row: its node, then each saved entry the restore would write."""
        p = row["path"]
        if not self._real_dir(self.backup, row["saved_as"]):
            return [(p, "node", "other:not-saved")]
        restore, kept, unread = {}, {}, []
        saved = self._walk(self.backup, row["saved_as"])
        # #3986 review: every copy SAVED.json records below the row must still
        # be there, or the restore would write the rest and clear the report.
        prefix, keys = row["saved_as"] + "/", self.record_keys
        i = bisect.bisect_left(keys, prefix)
        while i < len(keys) and keys[i].startswith(prefix):
            if keys[i][len(prefix) :] not in saved:
                return [(p, "node", "other:not-saved")]
            i += 1
        for q, s in saved.items():
            rel = f"{row['saved_as']}/{q}"
            rec = self.records.get(rel)
            if s and s[0] == "fifo" and rec and rec["type"] == "fifo":
                s = _fifo(rec["mode"])  # what the restore makes: the record's mode
            # The update's own bytes, left out, only while the copy is the one
            # SAVED.json records (#3986 review): a copy changed to the target's
            # bytes is restored instead, where staging refuses it.
            if self.target is not None and s == self.target.get(f"{p}/{q}") and self._kept(rel):
                kept[q] = s
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
            if not self._landed(p) or not all(
                self._kept(f"{row['saved_as']}/{q}") for q in restore
            ):
                return False  # #3986 review: the copies are the ones SAVED.json records
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
            # The restore sets skip-worktree, then assume-unchanged: one killed
            # between the two left S where s was saved, its own half (#3986 review).
            half = (None, "S") if row["flag"] == "s" else (None,)
            v = self._part(
                lambda: self._verdict(
                    link, lambda: flag == row["flag"], gate, lambda: left(lambda: flag in half)
                )
            )
            out.append((p, "flag", v))
        return out


# ── the restore (#3492 PR B2b) ──────────────────────────────────────────────

RUN_SUFFIX = "-live-move-restore"
#: A restore run's folder under deploy-backups: its UTC time, its pid.
RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+" + re.escape(RUN_SUFFIX))
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


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


class _Whole(Exception):
    """The whole report refused, by name, before anything is written."""


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
                      shown=ctx.shown, strict=write, fd=ctx.bfd)  # fmt: skip
    except rp.RepairError as exc:
        return f"other:tree-unreadable (HEAD's or the tag's tree: {exc})"
    try:
        parts = [part for row in judge.rows if row["class"] == "foreign" for part in judge.row(row)]
    except OSError as exc:
        return unreadable(exc)
    if where := judge.unresolved(parts):
        for n, (p, k, v) in enumerate(parts):
            if v == "as-left":
                parts[n] = (p, k, INFLIGHT_UNRESOLVED)
                judge.notes[(p, k)] = unresolved_note(where)
    judge.held = where
    return judge, parts, loaded, ctx


#: A part not written while an interrupted restore's aside is unresolved.
INFLIGHT_UNRESOLVED = "other:inflight-unresolved"


def unresolved_note(where: str) -> str:
    return (
        f"an interrupted restore's aside {where} holds newer content: nothing else is"
        " written until it is moved out; its own path is then judged by what it holds"
    )


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
