"""The disposable copy a mutation sweep runs in. The caller's tree is only ever
read. The guarantees are in `mutation_sweep.py`'s docstring."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from mutation_stop import (
    _hold,
)


def _git(repo: Path, *args: str) -> bytes:
    """``git -C repo <args>`` -> stdout bytes; RuntimeError naming git's stderr.

    GIT_* is dropped from the environment. Run from inside a git hook (a
    pre-commit that runs the suite), an inherited GIT_DIR / GIT_WORK_TREE /
    GIT_INDEX_FILE aims every call at the HOOK's repository instead of ``repo``,
    and the copy would be of the wrong tree. Bytes, not text: a path git prints
    is a filename, and filenames need not be valid UTF-8.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=7200
        )
    except subprocess.TimeoutExpired as exc:  # a hook's daemon can hold git's pipes
        raise RuntimeError(f"git {' '.join(args)} in {repo} timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {repo} failed: {os.fsdecode(proc.stderr).strip()}"
        )
    return proc.stdout


def _git_paths(repo: Path, *args: str) -> set[str]:
    return {os.fsdecode(p) for p in _git(repo, *args).split(b"\0") if p}


def _contained(root: Path, path: Path) -> bool:
    """``path`` lies inside ``root`` once every symlinked DIRECTORY on the way
    is followed. The final component is not followed: it is what gets written,
    and every writer here removes whatever sits there first."""
    return path.parent.resolve().is_relative_to(root)


def _clear(path: Path) -> None:
    """Remove whatever sits at ``path`` -- a file, a link, a directory -- without
    following a link."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _put(root: Path, path: Path, data: bytes, mode: int) -> None:
    """Make ``path`` a FRESH regular file holding ``data``, inside the copy.

    Fresh is the point: removing what is there and creating with O_EXCL |
    O_NOFOLLOW means no write ever lands in an inode or through a link that a
    test left behind -- a symlink or hard link to a file outside the copy would
    otherwise carry the write out of it. Refuses a directory that resolves
    outside ``root``. Raises ``OSError``.
    """
    if not _contained(root, path):
        raise OSError(f"{path} resolves outside the copy; refusing to write it")
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        os.fchmod(fh.fileno(), mode)


def _overlay(top: Path, tree: Path) -> str:
    """Make the fresh checkout at ``tree`` match ``top``'s WORKING TREE, and
    return a digest of exactly what was copied.

    The paths: `git diff HEAD` (staged and unstaged, renames split), untracked
    files that are not ignored, and every entry flagged assume-unchanged or
    skip-worktree, whose changes `git diff` does not report. Each is copied as
    it is on disk (a symlink as a symlink) or removed from the copy. Ignored
    files and untracked nested repositories (a trailing slash) are not part of
    this repository and are left out.

    The digest covers every overlaid path with what was put there (the bytes
    and mode copied, the link target, or "gone"), so two copies with the same
    HEAD and the same digest hold the same tree.
    """
    digest = hashlib.sha256()
    paths = _git_paths(top, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    paths |= _git_paths(top, "ls-files", "--others", "--exclude-standard", "-z")
    for entry in _git(top, "ls-files", "-v", "-z").split(b"\0"):
        if entry[:1].islower() or entry[:1] == b"S":
            paths.add(os.fsdecode(entry[2:]))
    for rel in sorted(p for p in paths if not p.endswith("/")):
        src, dst = top / rel, tree / rel
        if not _contained(tree, dst):
            raise RuntimeError(f"{rel} would be copied outside the copy")
        digest.update(os.fsencode(rel) + b"\0")
        if os.path.islink(src):
            link = os.readlink(src)
            _clear(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link, dst)
            digest.update(b"link\0" + os.fsencode(link) + b"\0")
        elif src.is_file():
            # Permission bits only: setuid/setgid have no business in a copy.
            data, mode = src.read_bytes(), src.stat().st_mode & 0o777
            _put(tree, dst, data, mode)
            digest.update(b"file\0%o\0" % mode + hashlib.sha256(data).digest())
        else:
            # Deleted -- or now a DIRECTORY, whose files are entries of their
            # own (sorted after it); HEAD's file must not stay in their way.
            _clear(dst)
            digest.update(b"gone\0")
    # Permission-only differences on otherwise clean tracked files: git records
    # only the executable bit (none at all under core.filemode=false), so a
    # 0644 -> 0600 change never shows in `git diff` and the checkout's mode would
    # silently replace the caller's. Every tracked regular file is compared.
    for rel in sorted(_git_paths(top, "ls-files", "-z") - paths):
        src, dst = top / rel, tree / rel
        if os.path.islink(src) or not src.is_file() or os.path.islink(dst) or not dst.is_file():
            continue
        if not _contained(tree, dst):  # never chmod through a symlinked directory
            continue
        mode = src.stat().st_mode & 0o777
        if dst.stat().st_mode & 0o777 != mode:
            os.chmod(dst, mode)
            digest.update(os.fsencode(rel) + b"\0mode\0%o\0" % mode)
    return digest.hexdigest()


#: The calls `shutil.rmtree` (Python 3.12) hands its error handler that mean "I
#: could not LIST or ENTER this directory" -- as opposed to "I could not remove
#: this entry". `os.lstat` is here because rmtree calls the handler with it when
#: `entry.stat()` fails, which happens when the PARENT has lost search
#: permission (READ from shutil's source, 3.12.3).
_RMTREE_LISTING = (os.open, os.scandir, os.lstat)


def _force_rmtree(path: Path) -> None:
    """rmtree that first makes whatever a test left unreadable writable again.

    A removal failure (`unlink`, `rmdir`) is fixed by making the PARENT
    writable and retrying the same call. A listing failure (`open`, `scandir`,
    `lstat`) is fixed by making the parent AND the path itself searchable, then
    removing the path's whole subtree again -- retrying `os.open(p)` alone would
    raise TypeError (it needs flags) and remove nothing. A path that has gone in
    the meantime counts as removed. Anything still failing is left for the
    caller's "could not fully remove" report; each path is retried as a listing
    failure at most once, so a mode that cannot be changed cannot loop.

    Nothing ABOVE ``path`` is ever chmodded: when the failing entry is ``path``
    itself, its parent is the caller's temp root, which the sweep does not own.
    `os.close` failing leaves nothing to retry, and a failed `os.path.islink`
    check is retried as the unlink it was guarding.
    """
    top = os.fspath(path)
    tried: set[str] = set()

    def retry(func, p, _exc):
        if func is os.close:
            return
        if func is os.path.islink:
            func = os.unlink
        if os.fspath(p) != top:
            with contextlib.suppress(OSError):
                os.chmod(os.path.dirname(p), 0o700)
        if func in _RMTREE_LISTING:
            if os.fspath(p) in tried:
                return
            tried.add(os.fspath(p))
            with contextlib.suppress(OSError):
                if not os.path.islink(p):  # never chmod THROUGH a link
                    os.chmod(p, 0o700)
            with contextlib.suppress(OSError):
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, onexc=retry)
                else:
                    os.unlink(p)
            return
        with contextlib.suppress(OSError):
            func(p)

    shutil.rmtree(path, onexc=retry)


@contextlib.contextmanager
def isolated_copy(
    repo: Path, tmp_root: Path | None = None, info: dict | None = None
) -> Iterator[Path]:
    """Yield ``repo``'s place inside a private copy of its whole repository.

    The copy is a detached `git worktree` of HEAD under a fresh directory in
    ``tmp_root`` (default ``~/tmp``: a real disk, never the session's TMPDIR,
    because a checkout is large temp), overlaid with the caller's working-tree
    changes (`_overlay`). On ANY exit the copy and its registration are removed
    -- this copy's only: `git worktree remove` on the path this call created,
    then, if git no longer recognises it (a test deleted the directory), the
    administrative entry recorded at creation.

    ``info``, when given, receives ``holder`` (the private directory the copy
    sits in, removed with it: the place for this copy's own TMPDIR) and
    ``fingerprint`` (HEAD plus the overlay digest), which is equal for two
    copies of the same caller tree.
    """
    try:
        top = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip()))
    except RuntimeError as exc:
        raise RuntimeError(
            f"the sweep copies the tree with git, and {repo} is not inside a git work tree ({exc})"
        ) from exc
    top, repo = top.resolve(), repo.resolve()
    parent = Path(tmp_root) if tmp_root is not None else Path.home() / "tmp"
    if parent.resolve().is_relative_to(top):
        raise ValueError(
            f"tmp_root {parent} is inside the repository {top}; the "
            "copy would be written into the tree it must not touch"
        )
    parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="mutation-sweep-", dir=str(parent))).resolve()
    tree = holder / holder.name  # the basename names git's admin entry too
    admin: Path | None = None
    try:
        with _hold():
            _git(top, "worktree", "add", "--detach", "--quiet", str(tree), "HEAD")
            admin = Path(os.fsdecode(_git(tree, "rev-parse", "--absolute-git-dir").strip()))
        overlay = _overlay(top, tree)
        if info is not None:
            head = os.fsdecode(_git(tree, "rev-parse", "HEAD").strip())
            info["holder"] = holder
            info["fingerprint"] = f"{head}:{overlay}"
        yield tree / repo.relative_to(top)
    finally:
        with _hold(then_stop=False):
            # Twice: one --force refuses a locked working tree.
            with contextlib.suppress(RuntimeError):
                _git(top, "worktree", "remove", "--force", "--force", str(tree))
            if os.path.lexists(holder):
                _force_rmtree(holder)
            if admin is not None and admin.parent.name == "worktrees" and admin.exists():
                shutil.rmtree(admin, ignore_errors=True)
            if os.path.lexists(holder) or (admin is not None and admin.exists()):
                print(
                    f"mutation-sweep: could not fully remove the copy at {holder}", file=sys.stderr
                )
