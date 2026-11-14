"""The live-move restore's run-folder and origin rules (split out of
live_move_restore.py, which loads this file beside it and re-exports every
name here): the name of a restore run's folder, the restore block REPAIR.json
keeps, and who may own deploy-backups and a report folder."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat


def _sibling(name: str):
    """A kit source beside this one, loaded the way live_move_repair._load_kit
    loads the kit (by path, so the installed kit never imports from the repo)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
    spec = importlib.util.spec_from_file_location(f"_live_repair_{name[:-3]}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"{path} cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rows = _sibling("live_move_restore_rows.py")
_path, ENTRIES, _raise = rows._path, rows.ENTRIES, rows._raise

RUN_SUFFIX = "-live-move-restore"
_TS = r"[0-9]{8}T[0-9]{6}Z-[0-9]+"
#: A restore run's folder under deploy-backups: its UTC time, its pid, and
#: (from #4011) the ts of the report it restores, which ties the folder to that
#: report: every run of a report is found by its name, never through the one
#: in-flight slot. Folders from before carry no ts and are found only through it.
RUN_NAME = re.compile(_TS + "(?:-(" + _TS + "))?" + re.escape(RUN_SUFFIX))
#: The most restore runs one report's restore reads; past it the report is
#: refused whole (fail closed: a run past the cap could hold the only copy of
#: newer content). A run that leaves nothing behind removes its own folder.
RUNS_CAP = 64


def run_ts(name: str) -> str | None:
    """The report ts a restore run folder's name carries, or None."""
    m = RUN_NAME.fullmatch(name)
    return m.group(1) if m else None


def absent(root: str, p: str) -> bool:
    """Is nothing at ``root``/``p``? Only "no such file" reads absent: any
    other error (a parent with no search bit, EACCES) raises OSError, since
    the restore cannot see there (#4364 review: os.path.lexists returns False
    then, and a held aside read as absent was released in silence)."""
    try:
        os.lstat(os.path.join(root, p))
    except FileNotFoundError:
        return True
    return False


def held_covers(holds, q: str) -> bool:
    """Does a hold among ``holds`` (each ``[run, path, ...]``: the in-flight
    slot's unresolved aside, and every aside the run folders show held) keep
    the part at ``q`` from being written (#4025 item 1)? Only a part at a held
    path, below it or above it (writing one moves or replaces what the held
    aside belongs to); every other part restores. Coverage comes from the run
    folders as well as the slot, so a hold still covers its path after a later
    node's intent overwrote the slot (#4025 PR3 review). An old-form hold
    covers every part (held_stops_all)."""
    if held_stops_all(holds):
        return True
    return any(q == h[1] or q.startswith(h[1] + "/") or h[1].startswith(q + "/") for h in holds)


def held_stops_all(holds) -> bool:
    """A hold in a folder named before #4011 carries no ts, so only the one
    in-flight slot can find it, and the next node's intent would overwrite
    that: it stops every part, the index call and the flags (review SF-8)."""
    return any(run_ts(h[0]) is None for h in holds)


_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def restore_block(report: dict, ts: str | None = None) -> dict | None:
    """REPAIR.json's `restore` block when it is exactly what restore() writes,
    {inflight: [run, path, "node"] | None, refused: int, restored_at: str |
    None}; else None (and a malformed one never resumes anything). With
    ``ts``, an in-flight run that names a report must name this one."""
    b = report.get("restore")
    if not isinstance(b, dict) or set(b) != {"inflight", "refused", "restored_at"}:
        return None
    inf = b["inflight"]
    if inf is not None and not (
        isinstance(inf, list) and len(inf) == 3 and isinstance(inf[0], str)
        and RUN_NAME.fullmatch(inf[0]) and run_ts(inf[0]) in (None, ts or run_ts(inf[0]))
        and _path(inf[1]) and inf[2] == "node"
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


def aside_parts(judge) -> list:
    """Every restore run folder of this report, judged on each run (#4011):
    an ``aside`` part per aside that holds content the restore did not put
    there (``other:aside-held``: named, counted refused, so restored_at stays
    unwritten and the report's alert stays up), per run folder that is not
    this user's alone (``other:not-owned``, never skipped), and one for an
    in-flight record whose whole run folder is gone (``other:aside-lost``: a
    pruned or removed folder; an aside moved out by hand while its folder
    stays is a release, as before). The in-flight aside itself is judged by
    Judge.unresolved. An aside holding the repair's own as-left copy names
    nothing. Over RUNS_CAP folders, or a folder list that cannot be read,
    refuses the report whole (_Whole)."""
    if judge.runs is None or judge.ts is None:
        return []
    out, inf = [], judge.inflight

    def part(key: str, why: str, note: str) -> None:
        # Keyed by the aside itself (<run>/aside/<path>), never the row's path:
        # one path held in two runs is two parts, and a row's own index part
        # never reads refused because an aside of it is (#4011 review SF-1).
        judge.notes[(key, "aside")] = note
        out.append((key, "aside", why))

    if inf:
        key = f"{inf[0]}/aside/{inf[1]}"
        try:
            os.lstat(os.path.join(judge.runs, inf[0]))
        except FileNotFoundError:
            gone = os.path.join(judge.shown, key)
            note = (
                f"the held aside {gone} is gone with its run folder: what it held was not restored"
            )
            part(key, "other:aside-lost", note)
        except OSError as exc:  # cannot see it: never read as gone, nor as there (#4364 review)
            part(key, "other:aside-unreadable",
                 f"{os.path.join(judge.shown, key)} cannot be read ({exc.strerror})")  # fmt: skip
    try:
        names = sorted(n for n in os.listdir(judge.runs) if run_ts(n) == judge.ts)
    except OSError as exc:
        raise _Whole(
            f"other:unreadable (deploy-backups cannot be listed: {exc.strerror})"
        ) from None
    if len(names) > RUNS_CAP:
        raise _Whole(f"other:too-large (over {RUNS_CAP} restore runs)")
    paths = sorted({r["path"] for r in judge.rows if r["class"] == "foreign" and r["disk_changed"]})
    for name in names:
        run, aside = os.path.join(judge.runs, name), os.path.join(judge.runs, name, "aside")
        try:
            ok = all(_private(os.lstat(d)) for d in (run, aside) if d == run or not absent(run, "aside"))
        except FileNotFoundError:
            continue  # removed since it was listed: nothing left to judge
        except OSError as exc:  # cannot see into it: never read as nothing held
            note = (
                f"the restore run folder {os.path.join(judge.shown, name)} cannot be read"
                f" ({exc.strerror}), so what its aside holds cannot be judged"
            )
            part(name, "other:aside-unreadable", note)
            continue
        if not ok:
            note = (
                f"the restore run folder {os.path.join(judge.shown, name)} (or its aside/) is not"
                " this user's alone, so what its aside holds cannot be judged"
            )
            part(name, "other:not-owned", note)
            continue
        budget = type(judge.runs_budget)(judge.rp)  # each run folder on its own budget
        for p in paths:
            if inf and inf[:2] == [name, p]:
                continue  # Judge.unresolved judges the in-flight aside
            key = f"{name}/aside/{p}"
            try:
                if judge.rp._link_parent(aside, p) is None and absent(aside, p):
                    continue
            except OSError as exc:
                note = f"{os.path.join(judge.shown, key)} cannot be read ({exc.strerror})"
                part(key, "other:aside-unreadable", note)
                # What it holds cannot be seen, so it holds its path as a held
                # aside does, whatever the in-flight slot names (PR3 port review).
                judge.held_asides.append([name, p, os.path.join(judge.shown, key)])
                continue
            spent, judge.budget = judge.budget, budget
            try:
                left = judge._left(p, root=aside, strict=True)
            except (OSError, judge.rp.TooLarge) as exc:
                why = "over the read budget" if isinstance(exc, judge.rp.TooLarge) else str(exc)
                note = f"{os.path.join(judge.shown, key)} cannot be judged ({why[:200]})"
                part(key, "other:aside-unreadable", note)
                judge.held_asides.append([name, p, os.path.join(judge.shown, key)])  # as above
                continue
            finally:
                judge.budget = spent
            if left:
                judge.asides_left.append((name, p))  # _Writer._name_kept_aside reads it
                continue
            where = os.path.join(judge.shown, key)
            part(key, "other:aside-held", f"newer content kept at {where}: it was not restored")
            judge.held_asides.append([name, p, where])  # what it covers: held_covers
    return out


def _private(st: os.stat_result) -> bool:
    """A directory of this user's alone (0700-style: no group or world bits)."""
    return stat.S_ISDIR(st.st_mode) and _mine(st) and not st.st_mode & 0o077
