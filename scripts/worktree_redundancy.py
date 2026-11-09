"""Is a reaper archive redundant with its merged head? (#2504)

`worktree_lifecycle.py --expire-trash` deletes an archive only when this module
proves that deleting it loses none of the archive's content: the archived head is in main, and every
file the archive holds is either byte-identical to that head's tree or on a
short allowlist of regenerable paths. Every failure, of any kind, keeps the
archive. Stdlib-only and loaded by path, like the rest of the lifecycle
contract.

The content check streams the tarball and hashes each member as a git blob
(``sha1(b"blob <size>\\0" + content)``), comparing it with ``git ls-tree`` of the
head. Nothing is extracted to disk.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tarfile
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path

ARCHIVE_SUFFIX = ".tar.gz"

# The verdicts that may expire an archive. `patch-id` is refused: `git cherry`
# ignores whitespace and cannot see a merge commit, and when `gh` is unreachable
# a `pr`-shaped archive falls through to it.
ACCEPTED_VERDICTS = frozenset({"ancestor", "pr"})

# Regenerable paths (owner ruling 2026-11-08). Any path component in this set.
_CACHE_PARTS = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "node_modules",
        ".tox",
    }
)
_CACHE_SUFFIXES = (".pyc", ".pyo")

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_CHUNK = 1 << 20
_FILE_MODES = ("100644", "100755")
_LINK_MODE = "120000"
_READ_ERRORS = (OSError, EOFError, zlib.error, tarfile.TarError, UnicodeError)
# Variables that make git (or gh) act on a repository other than ``repo_root``.
# Wider than worktree_lifecycle.py's `_GIT_LOCATION_VARS`, which `_merge_verdict`
# uses: there, a variable left in place can only make git fail (a keep), never
# prove an ancestry that is not there.
GIT_LOCATION_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GH_REPO",
)


def archive_redundancy(
    stored: Path,
    meta: dict,
    repo_root: Path,
    *,
    verdict: Callable[[str], str],
    reflog_dirs: Sequence[Path],
) -> tuple[bool, str]:
    """``(True, reason)`` only when deleting ``stored`` loses nothing.

    ``verdict(head)`` returns `_merge_verdict`'s method for the archived head; it is
    called only once the head resolves. ``reflog_dirs`` (required, so no caller
    skips the check by omission) are the admin dirs of the registrations expiry
    would prune: a commit only their reflogs reach would be
    collectable afterwards, so any commit there that no branch, remote-tracking
    ref or tag reaches keeps the archive. Archive pins (``refs/archived/*``) are
    not counted: expiry deletes the entry's own pin.
    """
    if not (stored.name.endswith(ARCHIVE_SUFFIX) and stored.is_file()):
        return False, "format not content-checked"
    head = meta.get("head") or meta.get("commit")
    if not isinstance(head, str) or not _SHA.match(head):
        return False, "no recorded head"
    if _git(repo_root, ["cat-file", "-e", f"{head}^{{commit}}"]) is None:
        return False, f"head {head[:12]} does not resolve"
    try:
        method = verdict(head)
    except Exception as e:  # noqa: BLE001 — an unproven verdict keeps the archive
        return False, f"merge verdict failed ({type(e).__name__})"
    if method not in ACCEPTED_VERDICTS:
        return False, f"head not proven in main (verdict: {method or 'none'})"
    try:
        logged = _reflog_commits(reflog_dirs)
    except OSError as e:
        return False, f"worktree reflog unreadable ({type(e).__name__})"
    if logged:
        # Asked per archive (about 0.1 s), so a ref deleted while a long run is
        # under way is still seen. `--not` on the command line does not apply
        # to the `--stdin` revisions, which stay positive.
        out = _git(
            repo_root,
            ["rev-list", "--stdin", "--not", "--branches", "--remotes", "--tags"],
            stdin="\n".join(sorted(logged)).encode() + b"\n",
        )
        if out is None:
            return False, "reflog commits could not be checked against refs"
        orphans = out.decode("ascii", "replace").split()
        if orphans:
            shown = ", ".join(c[:12] for c in orphans[:3])
            return False, f"worktree reflog holds unreachable commit(s): {shown}"
    tree = _head_tree(repo_root, head)
    if tree is None:
        return False, "head tree could not be read"
    entry = stored.name[: -len(ARCHIVE_SUFFIX)]
    try:
        unmatched = _unmatched_members(stored, entry, tree)
    except _Unsafe as e:
        return False, str(e)
    except _READ_ERRORS as e:
        return False, f"archive unreadable ({type(e).__name__})"
    if unmatched:
        shown = ", ".join(unmatched[:3])
        more = f" and {len(unmatched) - 3} more" if len(unmatched) > 3 else ""
        return False, f"holds files not in head {head[:12]}: {shown}{more}"
    return True, f"content matches head {head[:12]} ({method})"


class _Unsafe(Exception):
    """A member this check refuses to judge: the archive is kept."""


def git_env() -> dict[str, str]:
    """The environment with every repository-location override removed."""
    env = dict(os.environ)
    for var in GIT_LOCATION_VARS:
        env.pop(var, None)
    return env


def _raise(err: OSError) -> None:
    raise err


def _reflog_commits(dirs: Sequence[Path]) -> set[str]:
    """Every commit id in the reflogs (``logs/**``) of the admin dirs ``dirs``.

    Reflogs only: they are gc roots, so pruning the registration is what would
    expose their commits. Pseudorefs (ORIG_HEAD, REBASE_HEAD, ...) are not gc
    roots (MEASURED with git 2.43: a commit only they name is collected by
    `gc --prune=now` whether or not the registration exists), so they protect
    nothing and are not read. An unreadable directory raises (``os.walk`` would
    otherwise skip it), so the caller keeps the archive.
    """
    found: set[str] = set()
    for admin in dirs:
        files: list[Path] = []
        logs = admin / "logs"
        if logs.is_dir():
            for root, _dirs, names in os.walk(logs, onerror=_raise):
                files.extend(Path(root) / n for n in names)
        for f in files:
            for line in f.read_text(encoding="ascii", errors="replace").splitlines():
                for word in line.split("\t", 1)[0].split()[:2]:
                    if _SHA.match(word) and word != "0" * 40:
                        found.add(word)
    return found


def _git(
    repo_root: Path, args: list[str], *, timeout: int = 60, stdin: bytes | None = None
) -> bytes | None:
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            timeout=timeout,
            env=git_env(),
            input=stdin,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None


def _head_tree(repo_root: Path, head: str) -> dict[str, tuple[str, str]] | None:
    """``{path: (mode, blob id)}`` for every blob in ``head``'s tree."""
    out = _git(repo_root, ["ls-tree", "-r", "-z", "--full-tree", head])
    if out is None:
        return None
    tree: dict[str, tuple[str, str]] = {}
    for record in out.split(b"\0"):
        if not record:
            continue
        try:
            info, path = record.split(b"\t", 1)
            mode, kind, sha = info.decode("ascii").split(" ")
        except (ValueError, UnicodeError):
            return None
        if kind == "blob":
            tree[path.decode("utf-8", "surrogateescape")] = (mode, sha)
    return tree


def _relative(name: str, entry: str) -> str | None:
    """The member's path inside the worktree; ``""`` for the root itself."""
    if name == entry:
        return ""
    if not name.startswith(entry + "/"):
        raise _Unsafe(f"member outside the archive root: {name!r}")
    rel = name[len(entry) + 1 :]
    if any(part in ("", ".", "..") for part in rel.split("/")):
        raise _Unsafe(f"unsafe member name: {name!r}")
    return rel


def _allowlisted(rel: str, member: tarfile.TarInfo) -> bool:
    parts = rel.split("/")
    if any(p in _CACHE_PARTS for p in parts) or rel.endswith(_CACHE_SUFFIXES):
        return True
    if rel in (".git", ".trash_meta.json"):
        return member.isreg()  # the pointer file and the reaper's own metadata
    if rel == ".abide":
        return member.issym()
    if parts[:2] == ["config", ".generated"] and len(parts) > 2:
        return True
    return any(p.endswith(".egg-info") for p in parts[:-1])


def _blob_id(tf: tarfile.TarFile, member: tarfile.TarInfo) -> str:
    stream = tf.extractfile(member)
    if stream is None:
        raise _Unsafe(f"unreadable member: {member.name!r}")
    h = hashlib.sha1(b"blob %d\0" % member.size)  # noqa: S324 — git's object id
    got = 0
    while chunk := stream.read(_CHUNK):
        h.update(chunk)
        got += len(chunk)
    if got != member.size:
        raise _Unsafe(f"short member: {member.name!r}")
    return h.hexdigest()


def _link_blob_id(target: str) -> str:
    data = target.encode("utf-8", "surrogateescape")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


def _unmatched_members(
    stored: Path,
    entry: str,
    tree: dict[str, tuple[str, str]],
) -> list[str]:
    unmatched: list[str] = []
    with tarfile.open(stored, "r:gz") as tf:
        for member in tf:
            rel = _relative(member.name, entry)
            if member.isdir():
                continue
            if rel == "":
                raise _Unsafe("archive root is not a directory")
            if not (member.isreg() or member.issym()):
                raise _Unsafe(f"special member: {member.name!r}")
            want = tree.get(rel)
            # The allowlist is for paths the head does not track: a tracked
            # file under `node_modules/` is compared like any other.
            if want is None and _allowlisted(rel, member):
                continue
            if member.isreg():
                # Git records the owner's execute bit, and a mode-only change is
                # a change too (core.fileMode=false hides it from the dirty check).
                executable = bool(member.mode & 0o100)
                ok = (
                    want is not None
                    and want[0] in _FILE_MODES
                    and (want[0] == "100755") == executable
                    and want[1] == _blob_id(tf, member)
                )
            else:
                ok = (
                    want is not None
                    and want[0] == _LINK_MODE
                    and want[1] == _link_blob_id(member.linkname)
                )
            if not ok:
                unmatched.append(rel)
        # Iteration stops SILENTLY at a bad header past the first member, and
        # gzip checks its CRC only at end of stream. Read to the end: a CRC or
        # length error raises, and anything but end-of-archive padding keeps.
        # From `tf.offset`, the block iteration stopped AT: a bad header for
        # an empty or all-NUL member is not itself NUL, and would be skipped.
        tf.fileobj.seek(tf.offset)
        while chunk := tf.fileobj.read(_CHUNK):
            if chunk.strip(b"\0"):
                raise _Unsafe("data after the end of the archive")
    return unmatched
