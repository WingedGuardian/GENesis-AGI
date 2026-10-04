#!/usr/bin/env python3
"""Restore one opt-in extra-directory archive (restore.sh §4c, backup.sh §6f).

Each archive holds ONE directory, members stored relative to $HOME. restore.sh
resolves where it goes and refuses unsafe destinations; this helper does the two
steps that need a real tar reader:

    extra_restore.py root <tar>
        Print the directory the archive holds (e.g. ``.genesis/analytics``); it
        must be a directory member of the archive.
    extra_restore.py swap <tar> <stage> <root> <target> <aside>
        Extract into <stage> (created by the caller next to <target>, so the
        final step is a rename on one filesystem), fsync what was written, move
        an existing <target> to <aside> (only when <aside> is non-empty), and
        rename the restored directory into place. Prints ``aside <path>`` when
        an existing directory was moved aside.

stdout carries values only; diagnostics go to stderr. Exit codes: 0 restored;
3 this Python's tarfile lacks the 2025 extraction-filter fixes (refused, fail
closed); 4 restored, with some members refused; 5 refused or failed, nothing
replaced.

Members are extracted through the stdlib ``data`` filter (absolute links, ``..``
escapes and special files are refused member by member), with two additions:
every member and every link target must stay inside the archive's directory,
and directories keep their archived mode minus setuid/setgid/sticky and
group/other write (the ``data`` filter alone drops directory modes, which would
leave every directory at the caller's umask).
"""

from __future__ import annotations

import os
import posixpath
import sys
import tarfile


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def parts(name: str) -> list[str]:
    return [p for p in name.lstrip("/").split("/") if p not in ("", ".")]


def archive_root(tf: tarfile.TarFile) -> str | None:
    """The one directory the archive holds: the longest common path of its safe
    members, which must itself be a DIRECTORY member (backup.sh's tar always
    writes one). None otherwise. Never trimmed to a parent: an archive whose
    common path is a lone file or symlink would otherwise name that member's
    parent as the directory to replace, and swap out unrelated siblings."""
    safe = [(parts(m.name), m) for m in tf.getmembers()]
    safe = [(p, m) for p, m in safe if p and ".." not in p]
    if not safe:
        return None
    prefix = safe[0][0]
    for p, _ in safe[1:]:
        n = 0
        while n < min(len(prefix), len(p)) and prefix[n] == p[n]:
            n += 1
        prefix = prefix[:n]
    if prefix and any(p == prefix and m.isdir() for p, m in safe):
        return "/".join(prefix)
    return None


def _inside(path_parts: list[str], want: list[str]) -> bool:
    return ".." not in path_parts and path_parts[: len(want)] == want


def make_filter(root: str):
    """A tarfile extraction filter confining members and link targets to ``root``.
    ``refused`` collects one line per refused member."""
    want = parts(root)
    refused: list[str] = []

    def keep(member: tarfile.TarInfo, dest: str):
        if not _inside(parts(member.name), want):
            refused.append(f"refused member {member.name!r}: outside {root}")
            return None
        if member.issym() and not member.linkname.startswith("/"):
            target = posixpath.normpath(
                posixpath.join(posixpath.dirname("/".join(parts(member.name))), member.linkname)
            )
            if not _inside(target.split("/"), want):
                refused.append(f"refused member {member.name!r}: link leaves {root}")
                return None
        if member.islnk() and not _inside(parts(member.linkname), want):
            refused.append(f"refused member {member.name!r}: hard link leaves {root}")
            return None
        try:
            out = tarfile.data_filter(member, dest)
        except tarfile.FilterError as e:
            refused.append(f"refused member {member.name!r}: {type(e).__name__}")
            return None
        if out is not None and member.isdir():
            out = out.replace(mode=member.mode & ~0o7022, deep=False)
        return out

    keep.refused = refused  # type: ignore[attr-defined]
    return keep


def _fsync_tree(top: str) -> None:
    """fsync every file and directory under ``top`` (only that filesystem), so a
    crash after the rename cannot leave a renamed directory of empty files. A
    global sync(2) would also wait on every other mount, including a dead one."""
    for dirpath, _dirs, files in os.walk(top):
        for name in files:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                continue
            fd = os.open(p, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        fd = os.open(dirpath, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _python_is_safe() -> bool:
    # data_filter arrived in 3.12 (backported to 3.8.17+/3.11.4); the 2025 filter
    # bypass fixes (CVE-2025-4517 and siblings) shipped together with
    # os.path.ALLOW_MISSING, which the fixed filter itself uses. That makes it a
    # correlated marker for the fix, not a documented guarantee of it.
    return hasattr(tarfile, "data_filter") and hasattr(os.path, "ALLOW_MISSING")


def cmd_root(tar: str) -> int:
    with tarfile.open(tar) as tf:
        root = archive_root(tf)
    if root is None:
        _err("the archive does not hold a single directory")
        return 5
    print(root)
    return 0


def cmd_swap(tar: str, stage: str, root: str, target: str, aside: str) -> int:
    keep = make_filter(root)
    with tarfile.open(tar) as tf:
        tf.extractall(stage, filter=keep)  # noqa: S202 — data filter + confinement to root
    for line in keep.refused:
        _err(line)
    new = os.path.join(stage, root)
    if os.path.islink(new) or not os.path.isdir(new):
        _err(f"nothing was extracted for {root}")
        return 5
    _fsync_tree(new)
    if os.path.lexists(target) and not aside:
        _err(f"{target} exists and no aside path was given")
        return 5
    moved = False
    try:
        if os.path.lexists(target):
            os.rename(target, aside)
            moved = True
        os.rename(new, target)
    except OSError as e:
        if moved:
            try:
                os.rename(aside, target)
            except OSError as e2:
                _err(f"could not put the previous directory back from {aside}: {e2}")
                print(f"aside {aside}")
        _err(f"could not move the restored directory into place: {e}")
        return 5
    try:
        fd = os.open(os.path.dirname(target), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        _err(f"restored, but could not fsync {os.path.dirname(target)}: {e}")
    if moved:
        print(f"aside {aside}")
    return 4 if keep.refused else 0


def main(argv: list[str]) -> int:
    if not _python_is_safe():
        _err("this Python's tarfile lacks the 2025 extraction-filter fixes; refusing to extract")
        return 3
    if len(argv) == 3 and argv[1] == "root":
        return cmd_root(argv[2])
    if len(argv) == 7 and argv[1] == "swap":
        return cmd_swap(*argv[2:7])
    _err("usage: extra_restore.py root <tar> | swap <tar> <stage> <root> <target> <aside>")
    return 5


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (OSError, tarfile.TarError) as e:
        _err(f"{type(e).__name__}: {e}")
        sys.exit(5)
