"""What a restore of a live-move repair report may write (#3492 PR B1).

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
parent that is this report's own row, already put back, is neither an
``index-above`` nor a ``link-parent`` for its child. An index part whose blob
is neither saved nor in the object store is ``not-saved``. A directory row is
never restored whole: its saved copy can hold the update's own files, so it
lists as ``entry`` parts only the saved files whose bytes are not the move
target's (when the target cannot be read, only those that are foreign rows
themselves). It writes no report and no backup."""

from __future__ import annotations

import bisect
import calendar
import contextlib
import json
import os
import re
import stat
import subprocess
import time

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


def _path(p: object) -> bool:
    """A plain relative path outside `.git`, as git records one: no empty, `.`
    or `..` part, no NUL, at most PATH_BYTES file-system bytes."""
    if not isinstance(p, str) or "\0" in p:
        return False
    if any(part in ("", ".", "..", ".git") for part in p.split("/")):
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
        and s["type"] in _SPECIALS
        and isinstance(s["mode"], str)
        and re.fullmatch(r"[0-7]{4}", s["mode"]) is not None
    )


def _row_problem(row: dict, parents) -> str | None:
    """The first field of ``row`` the writer would not have written, else None."""
    if missing := ROW_KEYS - row.keys():
        return "missing " + ", ".join(sorted(missing))
    p, index = row["path"], row.get("index")
    if row.get("class") not in ("foreign", "engine") or row.get("disk") not in _DISKS:
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
    if row.get("flag") is not None and row["flag"] not in _FLAGS:
        return "flag"
    if (row["disk"] == "other") != (row.get("special") is not None) or (
        row.get("special") is not None and not _special(row["special"])
    ):
        return "special"
    unsaved = row.get("unsaved")
    if not isinstance(unsaved, list) or not all(
        isinstance(u, dict) and _path(u.get("path")) and u["path"].startswith(p + "/")
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
    """Why the report's SAVED.json (the integrity record REPAIR.json names) is
    not one the writer wrote, else None. Opened as load_report opens
    REPAIR.json: no symlink followed, no FIFO blocked on, a regular file of at
    most SAVED_BYTES, holding exactly {files, complete}: `tree/<path>` keys
    and the writer's three record shapes. Only the shape: whether a copy still
    matches its record is the restore's to check (B2b)."""
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(os.path.join(backup, "SAVED.json"), flags)
    except OSError as exc:
        return f"cannot be opened ({exc.strerror})"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return "not a regular file"
        chunks, size = [], 0
        while size <= rp.SAVED_BYTES and (chunk := os.read(fd, rp.SAVED_BYTES + 1 - size)):
            chunks.append(chunk)
            size += len(chunk)
    except OSError as exc:
        return f"cannot be read ({exc.strerror})"
    finally:
        os.close(fd)
    if size > rp.SAVED_BYTES:
        return f"over {rp.SAVED_BYTES} bytes"
    try:
        saved = json.loads(b"".join(chunks))
    except (ValueError, RecursionError):
        return "not JSON"
    if not isinstance(saved, dict) or set(saved) != {"files", "complete"}:
        return "not {files, complete}"
    if not isinstance(saved["complete"], bool) or not isinstance(saved["files"], dict):
        return "files or complete"
    if len(saved["files"]) > ENTRIES:
        return f"over {ENTRIES} records"
    for key, r in saved["files"].items():
        if not (key.startswith("tree/") and _path(key[len("tree/") :])) or not _record(r):
            return f"record {json.dumps(key)[:200]}"
    return None


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
        for r in found:
            if isinstance(r.get("path"), str):
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
    def __init__(self, rp, repo: str, backup: str, loaded: dict, newer: dict) -> None:
        self.rp, self.repo, self.backup = rp, repo, backup
        self.budget, self.blockers = _Budget(rp), set()
        report, self.rows = loaded["report"], loaded["rows"]
        self.fmt = rp._out(repo, "rev-parse", "--show-object-format")
        self.head = rp._tree(repo, "HEAD")
        self.tag = rp._tree(repo, report["tag"])
        try:
            self.target = rp._tree(repo, report["other"])
        except rp.RepairError:
            self.target = None
        self.index, self.flags = rp._index_state(repo)
        self.foreign = {r["path"]: r for r in self.rows if r["class"] == "foreign"}
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
            ["git", "-C", self.repo, "cat-file", "--batch-check"],
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

    def _left(self, p: str, whole: bool = True) -> bool:
        """Does the disk hold what the repair left at ``p``: the tag's? A tag
        directory is compared on the tag's own paths only (the repair never
        removes an untracked file there that is not a row), and only for a
        node part (``whole``); an index or flag part needs only the directory."""
        t = self.tag.get(p)
        if t and t[0] == "160000":
            return self._real_dir(self.repo, p) and self._walk(self.repo, p) == {}
        if t is None and p in self.tag_dirs:
            return self._real_dir(self.repo, p) and (
                not whole
                or all(
                    self._state(self.repo, f"{p}/{q}") == e for q, e in self._tag_below(p).items()
                )
            )
        return self._state(self.repo, p) == t

    def _saved_node(self, row: dict):
        """(is it saved, the saved node) of a non-directory row's disk part."""
        special = row.get("special")
        if row["saved_as"]:
            saved = self._state(self.backup, row["saved_as"])
            return saved is not None, saved
        return True, (special["type"], special["mode"]) if special else None

    def _link(self, row: dict) -> str | None:
        """``link-parent`` when a parent of the row's path is a symlink or a
        non-directory, decided before any disk read below it. A parent that is
        this report's own row, its node already put back, does not refuse the
        child the writer saw beyond it (a killed restore must converge)."""
        d = self.rp._link_parent(self.repo, row["path"])
        if d is None:
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
        # An entry above that this report's own row put back is the writer's
        # order (parents first), never a refusal of its child.
        if any(not self._index_back(d) for d in self.index.keys() & set(parents)):
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
        return self._verdict(link, done, gate, lambda: self._left(p))

    def _dir(self, row: dict, link, gate: str | None) -> list[tuple[str, str, str]]:
        """A directory row: its node, then each saved entry the restore would write."""
        p = row["path"]
        if not self._real_dir(self.backup, row["saved_as"]):
            return [(p, "node", "other:not-saved")]
        restore, kept, unread = {}, {}, []
        for q, s in self._walk(self.backup, row["saved_as"]).items():
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
            return all(have.get(q) == s for q, s in restore.items()) and all(
                s in (restore.get(q), kept.get(q), tagged.get(q)) for q, s in have.items()
            )

        v = self._verdict(link, done, gate, lambda: self._left(p))
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


def check(rp, repo: str, backup: str, say) -> int:
    """``--restore TS --check``: judge the report in ``backup``, print one line
    per part and a summary; 0 if every part is as-left or already-restored."""
    ts = rp.report_ts(os.path.basename(backup))
    try:
        _order(ts)
        loaded = rp.load_report(backup, repo)
        if loaded is None:
            gone = not os.path.lexists(backup)
            raise rp.BadReport("no such report" if gone else "it holds no REPAIR.json")
    except ValueError:
        say(f"report {ts} other:unreadable (its ts names no date); nothing judged")
        return 1
    except rp.BadReport as exc:
        say(f"report {ts} other:unreadable ({exc}); nothing judged")
        return 1
    if why := problem(loaded, rp.REPORT_BYTES, rp._parents):
        say(f"report {ts} other:bad-report ({why}); nothing judged")
        return 1
    # A report written before the integrity record existed names none: it is
    # judged as before (the restore refuses its node parts `no-hash`).
    if "integrity" in loaded["report"]:
        named = loaded["report"]["integrity"]
        why = saved_problem(rp, backup) if named == "SAVED.json" else "integrity names another file"
        if why:
            say(f"report {ts} other:bad-report (SAVED.json: {why}); nothing judged")
            return 1
    newer, why = _newer_paths(rp, repo, os.path.dirname(backup), ts)
    if newer is None:
        say(f"report {ts} other:newer-report-unreadable ({why}); nothing judged")
        return 1
    try:
        judge = Judge(rp, repo, backup, loaded, newer)
    except rp.RepairError as exc:
        say(f"report {ts} other:tree-unreadable (HEAD's or the tag's tree: {exc}); nothing judged")
        return 1
    try:
        parts = [part for row in judge.rows if row["class"] == "foreign" for part in judge.row(row)]
    except OSError as exc:
        say(f"report {ts} other:unreadable (a path cannot be read: {exc}); nothing judged")
        return 1
    for path, part, verdict in parts:
        say(f"{json.dumps(path)} {part} {verdict}")
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
