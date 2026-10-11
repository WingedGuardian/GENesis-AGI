#!/usr/bin/env python3
"""Restore one opt-in extra-directory archive (restore.sh §4c, backup.sh §6f).

Each archive holds ONE directory, members stored relative to $HOME. restore.sh
resolves where it goes and refuses unsafe destinations; this helper does the two
steps that need a real tar reader:

    extra_restore.py check
        Exit 0 when this Python may extract (see _python_is_safe), else 3.
    extra_restore.py root <tar>
        Print the directory the archive holds (e.g. ``.genesis/analytics``); it
        must be a directory member of the archive.
    extra_restore.py verify <tar> <scratch-parent>
        Like ``root``, then extract the archive the way ``swap`` does (same
        filter, same order) into a temporary directory under <scratch-parent>,
        with every file's content left out, and remove it again. So backup learns
        exactly what a restore will refuse, including members refused only
        because of what an earlier member left on disk. Prints the directory;
        exit 4 (one line per member on stderr) when a restore would refuse any
        member, 5 when a restore would fail outright.
    extra_restore.py swap <tar> <stage> <root> <target> <aside>
        Extract into <stage> (created by the caller next to <target>, so the
        final step is a rename on one filesystem), fsync what was written, move
        an existing <target> aside (only when <aside> is non-empty; ``auto``
        builds a short name next to <target>), and
        rename the restored directory into place. Prints ``aside <path>`` when
        an existing directory was moved aside.

stdout carries values only; diagnostics go to stderr. Exit codes: 0 restored;
3 this Python's tarfile lacks the 2025 extraction-filter fixes (refused, fail
closed); 4 restored, with some members refused; 5 refused or failed, nothing
replaced; 6 restored, but a step after the rename failed (the parent directory
could not be fsynced, so the rename is not proven durable, or an archived
directory mode could not be applied).

Members are extracted through the stdlib ``data`` filter (absolute links, ``..``
escapes and special files are refused member by member), with two additions:
every member and every link target must stay inside the archive's directory,
and directories get their archived mode minus setuid/setgid/sticky and
group/other write (the ``data`` filter alone drops directory modes, which would
leave every directory at the caller's umask). Directories stay owner-writable
until the restored tree has been renamed into place, and only then get their
archived modes: a read-only directory (0555) cannot be renamed to a new parent,
and its staging copy could not be deleted after a failure.
"""

from __future__ import annotations

import copy
import os
import posixpath
import sys
import tarfile
import tempfile
import time


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _out(text: str) -> None:
    """Write a value to stdout as raw bytes: a path may not be valid in the locale's
    encoding (a non-UTF-8 name, or a UTF-8 one under a strict ASCII locale)."""
    sys.stdout.buffer.write(os.fsencode(text) + b"\n")
    sys.stdout.buffer.flush()


def aside_path(target: str) -> str:
    """<parent>/<first <=100 bytes of the name>.pre-restore-<UTC stamp>.<pid>, so the
    name stays far below NAME_MAX. A UTF-8 name is cut on a character boundary."""
    name = os.fsencode(os.path.basename(target))
    stem = name[:100]
    try:
        name.decode("utf-8")
        stem = stem.decode("utf-8", "ignore").encode("utf-8")
    except UnicodeDecodeError:
        pass  # not UTF-8 at all: there are no character boundaries to respect
    suffix = f".pre-restore-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.{os.getpid()}"
    return os.fsdecode(os.path.join(os.fsencode(os.path.dirname(target)), stem + suffix.encode()))


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
    dir_modes: dict[str, int] = {}  # path relative to root -> archived mode, applied last
    links: set[str] = set()  # paths of symlink members seen so far

    def keep(member: tarfile.TarInfo, dest: str):
        p = parts(member.name)
        if not _inside(p, want):
            refused.append(f"refused member {member.name!r}: outside {root}")
            return None
        # A member written THROUGH an earlier symlink member would land wherever that
        # link leads, so name-based checks below would judge the wrong place.
        if any("/".join(p[:i]) in links for i in range(1, len(p))):
            refused.append(f"refused member {member.name!r}: its path runs through a link")
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
        except OSError as e:
            # The filter resolves the path through what earlier members put on disk
            # (a link loop, a link through a file): refuse this member, never abort
            # the whole directory.
            refused.append(f"refused member {member.name!r}: {type(e).__name__}: {e.strerror}")
            return None
        if out is not None and member.issym():
            links.add("/".join(p))
        if out is not None and member.isdir():
            final = member.mode & ~0o7022
            dir_modes["/".join(parts(member.name)[len(want) :])] = final
            out = out.replace(mode=final | 0o700, deep=False)
        return out

    keep.refused = refused  # type: ignore[attr-defined]
    keep.dir_modes = dir_modes  # type: ignore[attr-defined]
    return keep


def _fsync_tree(top: str) -> None:
    """fsync every file and directory under ``top`` (only that filesystem), so a
    crash after the rename cannot leave a renamed directory of empty files. A
    global sync(2) would also wait on every other mount, including a dead one."""

    def _raise(err: OSError) -> None:
        raise err

    for dirpath, _dirs, files in os.walk(top, onerror=_raise):
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
    _out(root)
    return 0


def cmd_verify(tar: str, scratch_parent: str) -> int:
    with tarfile.open(tar) as tf:
        root = archive_root(tf)
        if root is None:
            _err("the archive does not hold a single directory")
            return 5
        keep = make_filter(root)

        def hollow(member: tarfile.TarInfo, dest: str):
            out = keep(member, dest)
            if out is not None and out.isreg():
                out = copy.copy(out)  # file content plays no part in what is refused
                out.size = 0
                out.sparse = None
            return out

        # swap's extractall into a fresh empty stage, minus the bytes: the filter
        # judges each member against what earlier members left on disk.
        with tempfile.TemporaryDirectory(prefix=".extra-verify.", dir=scratch_parent) as dest:
            tf.extractall(dest, filter=hollow)  # noqa: S202 — same filter as swap
    for line in keep.refused:
        _err(line)
    _out(root)
    return 4 if keep.refused else 0


def cmd_swap(tar: str, stage: str, root: str, target: str, aside: str) -> int:
    if aside == "auto":
        aside = aside_path(target)
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
    # An existing EMPTY directory counts as absent (restore.sh passes no aside for it).
    # It is removed only here, with the restored tree extracted and fsynced, and put
    # back if the rename fails, so a failed restore never leaves it missing.
    empty_target = (
        not aside
        and os.path.isdir(target)
        and not os.path.islink(target)
        and not os.listdir(target)
    )
    if os.path.lexists(target) and not aside and not empty_target:
        _err(f"{target} exists and no aside path was given")
        return 5
    moved = False
    removed_empty = False
    try:
        if empty_target:
            os.rmdir(target)
            removed_empty = True
        elif os.path.lexists(target):
            os.rename(target, aside)
            moved = True
        os.rename(new, target)
    except OSError as e:
        if removed_empty:
            try:
                os.mkdir(target)
            except OSError as e2:
                _err(f"could not recreate the empty directory {target}: {e2}")
        if moved:
            try:
                os.rename(aside, target)
            except OSError as e2:
                _err(f"could not put the previous directory back from {aside}: {e2}")
                _out(f"aside {aside}")
        _err(f"could not move the restored directory into place: {e}")
        return 5
    if moved:
        _out(f"aside {aside}")
    problem = False
    try:
        fd = os.open(os.path.dirname(target), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        _err(f"could not fsync {os.path.dirname(target)}: {e}")
        problem = True
    # Archived directory modes, deepest first, so a read-only parent never blocks
    # setting a child below it.
    real_target = os.path.realpath(target)  # $HOME itself may legitimately be a link
    for rel in sorted(keep.dir_modes, key=lambda r: r.count("/") + bool(r), reverse=True):
        path = os.path.join(target, rel) if rel else target
        try:
            # Never chmod through a link inside the restored tree (Linux has no lchmod).
            expected = os.path.normpath(os.path.join(real_target, rel)) if rel else real_target
            if os.path.realpath(path) != expected:
                raise OSError(f"{path} is, or runs through, a symlink")
            os.chmod(path, keep.dir_modes[rel])
        except OSError as e:
            _err(f"could not apply the archived mode to {path}: {e}")
            problem = True
    if problem:
        return 6
    return 4 if keep.refused else 0


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "check":
        # backup.sh asks before archiving: an archive a restore cannot extract is
        # better reported at backup time than discovered during a disaster recovery.
        if _python_is_safe():
            return 0
        _err(
            "its Python tarfile lacks the 2025 extraction-filter fixes (CPython 3.12.11+ or a distro backport)"
        )
        return 3
    if len(argv) == 3 and argv[1] == "root":
        return cmd_root(argv[2])  # reads names only; extracts nothing
    if len(argv) == 4 and argv[1] == "verify":
        if not _python_is_safe():
            _err("this Python's tarfile lacks the 2025 extraction-filter fixes")
            return 3
        return cmd_verify(argv[2], argv[3])
    if len(argv) == 7 and argv[1] == "swap":
        if not _python_is_safe():
            _err(
                "this Python's tarfile lacks the 2025 extraction-filter fixes; refusing to extract"
            )
            return 3
        return cmd_swap(*argv[2:7])
    _err(
        "usage: extra_restore.py check | root <tar> | verify <tar> <scratch-parent> | swap <tar> <stage> <root> <target> <aside>"
    )
    return 5


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (OSError, tarfile.TarError) as e:
        _err(f"{type(e).__name__}: {e}")
        sys.exit(5)
