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
#: A restore run's folder under deploy-backups: its UTC time, its pid.
RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9]+" + re.escape(RUN_SUFFIX))
_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


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
