"""The live-move restore's report and row rules (split out of
live_move_restore.py, which loads this file beside it and re-exports every
name here): what a REPAIR.json row, a SAVED.json record and a report may
hold, and the budgets one check may spend."""

from __future__ import annotations

import json
import os
import re
import stat

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


def _raise(exc: OSError) -> None:
    raise exc
