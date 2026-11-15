"""The live-move restore's report and row rules (split out of
live_move_restore.py, which loads this file beside it and re-exports every
name here): what a REPAIR.json row, a SAVED.json record and a report may
hold, and the budgets one check may spend."""

from __future__ import annotations

import bisect
import hashlib
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


def under(keys: list[str], p: str) -> list[str]:
    """The sorted ``keys`` strictly below ``p``."""
    i, out = bisect.bisect_left(keys, p + "/"), []
    while i < len(keys) and keys[i].startswith(p + "/"):
        out.append(keys[i])
        i += 1
    return out


def footprint(rows, todo, back, refused, refuse, blobs, state, judged, sets_again, parents):
    """The index call's rule, one implementation for the writer (_Writer._index,
    with what it reads right before the call) and the judge (check, with what it
    read; #4025 item 3: --check names the refusal --restore makes).

    ``rows``: {path: row}; ``todo``: the sorted paths whose index part reads
    as-left; ``back(p)``: its node part (and a covering directory's) written or
    already restored; ``refused``: the entries with a refused part, which
    ``refuse(p, kind, why, note="")`` adds to; ``blobs(row)``: why a saved blob
    cannot be given to git, or ""; ``state()``: the index and its flags, read
    after the parts above are settled and so right before the call (the
    writer's read; the judge passes its own); ``judged``: the index the judge
    read; ``sets_again(q, go, flags)``: is q's flag set again after the call;
    ``parents``: the repair's _parents.

    What git drops is its directory/file rule: a stage-0 record drops every
    entry at and under its path and any file entry at a parent; any record, the
    entries at its path. A part whose footprint holds an entry that is not one
    of this report's rows as the judge read it is refused
    ``changed-during-restore``; one whose footprint holds a flag the call drops
    and nothing sets again, ``other:flag-changed``; a stage-0 part above a
    refused row, ``child-refused`` (a fixpoint: a refusal can cascade up).
    Returns ({p: the paths its record drops} for the parts that go in the
    call, the index ``state()`` read)."""
    paths = sorted(rows)

    def stage0(p: str) -> bool:  # a record that drops what is below it
        return any(e[2] == "0" for e in rows[p]["index"])

    go = []
    for p in todo:
        if not back(p):
            refuse(p, "index", "row-refused")
        elif stage0(p) and not refused.isdisjoint(under(paths, p)):  # SF-5
            refuse(p, "index", "child-refused")
        elif bad := blobs(rows[p]):
            refuse(p, "index", "other:hash-mismatch", bad)
        else:
            go.append(p)
    pre, flags = state()
    keys, feet = sorted(pre), {}
    for p in go:
        drop = [p]
        if stage0(p):
            drop += under(keys, p) + [d for d in parents(p) if d in pre]
        # A parent's entry is checked for any record (an unmerged one too: unmeasured).
        look = drop + [d for d in parents(p) if d in pre and d not in drop]
        stray = [q for q in look if q not in rows or q in refused
                 or pre.get(q, []) != judged.get(q, [])]  # fmt: skip
        flagged = [q for q in drop if q in flags and not sets_again(q, go, flags)]
        if stray:
            why = f"{json.dumps(stray[0])[:200]} changed since it was judged"
            refuse(p, "index", "changed-during-restore", why)
        elif flagged:
            q = flagged[0]
            why = f"{json.dumps(q)[:200]} holds a flag ({flags[q]}) the index call would drop"
            refuse(p, "index", "other:flag-changed", why)
        else:
            feet[p] = drop
    while dropped := [p for p in feet if stage0(p) and not refused.isdisjoint(under(paths, p))]:
        for p in dropped:
            del feet[p]
            refuse(p, "index", "child-refused")
    return feet, pre


def as_the_writer_would(rp, judge, parts) -> list:
    """The parts as the writer would refuse them in its index call and flags
    (#4025 item 3): footprint, the one rule _Writer._index runs, on the index
    the judge read, then _Writer._flags's cascade (a flag part whose index part
    is refused is refused ``row-refused``). A refusal here reads exactly as the
    writer prints it after ``refused:``. What only the write itself can show (a
    node write failing, a session staging in between) stays the writer's."""
    ok = ("as-left", "already-restored")
    verdict = {(p, k): v for p, k, v in parts}
    decided: dict[tuple[str, str], str] = {}
    refused = {judge.entry_of.get(p, p) for (p, _k), v in verdict.items() if v not in ok}

    def outcome(key) -> str:  # the writer's out[key]: as-left parts are written
        if key in decided:
            return "refused"
        v = verdict.get(key, "as-left")
        return "restored" if v == "as-left" else v

    def back(p: str, *kinds: str) -> bool:  # _Writer._back
        keys = [(q, "node") for q in (p, *rp._parents(p))] + [(p, k) for k in kinds]
        return all(outcome(key) in ("restored", "already-restored") for key in keys)

    def refuse(p: str, kind: str, why: str, note: str = "") -> None:  # _Writer._refuse
        decided[(p, kind)] = why
        refused.add(judge.entry_of.get(p, p))
        if note:
            judge.notes[(p, kind)] = note

    def sets_again(q: str, go: list[str], flags: dict) -> bool:
        v = verdict.get((q, "flag"))
        return v == "as-left" or (v == "already-restored" and q in go)

    todo = sorted(p for (p, k), v in verdict.items() if k == "index" and v == "as-left")
    if todo:
        rows = {r["path"]: r for r in judge.rows}
        state = lambda: (judge.index, judge.flags)  # noqa: E731 - what the judge read
        footprint(rows, todo, back, refused, refuse, lambda row: "", state,
                  judge.index, sets_again, rp._parents)  # fmt: skip
    for (p, k), v in sorted(verdict.items()):
        written = verdict.get((p, "index")) == "as-left" and (p, "index") not in decided
        if k != "flag" or (v != "as-left" and not (v == "already-restored" and written)):
            continue
        if not back(p, "index"):
            refuse(p, "flag", "row-refused")
    return [(p, k, decided.get((p, k), v)) for p, k, v in parts]


def damage_of(records: dict, backup: str, budget, rel: str) -> str | None:
    """Why the restore's staging (_Writer._stage_one) refuses the saved copy at
    ``rel`` against its SAVED.json record, or None; reads it under ``budget``
    (TooLarge when spent)."""
    rec, full = records.get(rel), os.path.join(backup, rel)
    if rec is None:
        return "no-hash"
    try:
        st = os.lstat(full)
    except OSError:
        return "not-saved"
    try:
        if rec["type"] == "symlink":
            ok = stat.S_ISLNK(st.st_mode) and os.readlink(full) == rec["target"]
            return None if ok else "hash-mismatch"
        if rec["type"] == "fifo":
            return None if stat.S_ISFIFO(st.st_mode) else "hash-mismatch"
        if rec["sha256"] is None:
            return "too-large"
        fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            st, h = os.fstat(fd), hashlib.sha256()
            if not stat.S_ISREG(st.st_mode):
                return "hash-mismatch"
            budget(st.st_size)
            while chunk := os.read(fd, 1 << 20):
                h.update(chunk)
        finally:
            os.close(fd)
    except OSError:
        return "hash-mismatch"
    return None if h.hexdigest() == rec["sha256"] else "hash-mismatch"


def damage_note(rel: str, why: str) -> str:
    """The writer's staging note for the same refusal (_Writer._stage_one)."""
    if why == "no-hash":
        return f"SAVED.json records no {rel}"
    if why == "not-saved":
        return f"{rel} is gone"
    if why == "too-large":
        return f"{rel} was over the hash budget"
    return f"{rel} is not what SAVED.json records"
