"""Recoverable deletes: move user or project data into a Genesis-owned trash.

Stdlib only, importable without the Genesis runtime. Guide:
docs/reference/trash.md; CLI: ``<venv python> -m genesis.trash``.

A trashed item is always RENAMED, never copied, into a trash on its own volume:
``~/.genesis/trash/`` when it shares a device with ``~/.genesis``, otherwise
``<mountpoint>/.genesis-trash-<uid>/``. Each entry is a directory holding the
item under the fixed name ``item`` and a ``tombstone.json`` describing it. The
tombstone is written first, so a crash between the two steps leaves an entry
that lists as incomplete rather than an item nobody can trace.

Nothing here expires trash (#2504) and backups do not include it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home
from genesis.util.atomic import atomic_write_text

TOMBSTONE = "tombstone.json"
ITEM = "item"
_MAX_ENTRY_NAME = 200  # bytes; the entry name is a handle, the full basename is in the tombstone
_MAX_SUFFIX = 1000


class TrashRefused(Exception):
    """The item was NOT trashed and is untouched; the message says why."""


@dataclass(frozen=True)
class Tombstone:
    entry_id: str
    root: str
    original_path: str
    name: str
    kind: str  # file | dir | symlink | other
    size: int | None
    reason: str
    caller: str
    session_id: str | None
    trashed_at: str


@dataclass(frozen=True)
class Entry:
    path: Path
    tombstone: Tombstone | None
    complete: bool  # tombstone AND item present


def home_trash_root() -> Path:
    return genesis_home() / "trash"


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _mountpoint(path: Path) -> Path:
    path = path.resolve()
    while not os.path.ismount(path) and path != path.parent:
        path = path.parent
    return path


def _root_for(item_dev: int, item: Path) -> Path:
    home_root = home_trash_root()
    with contextlib.suppress(OSError):
        if os.stat(home_root.parent).st_dev == item_dev:
            return home_root
    return _mountpoint(item.parent) / f".genesis-trash-{os.getuid()}"


def _ensure_root(root: Path) -> None:
    """Create the root 0700, then refuse anything that is not ours."""
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise TrashRefused(f"cannot create trash root {root}: {exc.strerror}") from None
    st = os.lstat(root)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise TrashRefused(f"trash root {root} is not a plain directory")
    if st.st_uid != os.getuid():
        raise TrashRefused(f"trash root {root} is owned by another user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        raise TrashRefused(f"trash root {root} is not mode 0700")


def _kind(st: os.stat_result) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


def _size(item: Path, st: os.stat_result) -> int | None:
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    errors: list[OSError] = []
    for dirpath, _dirs, files in os.walk(item, onerror=errors.append, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except FileNotFoundError:
                continue  # gone mid-scan, so not part of what is trashed
            except OSError as exc:
                errors.append(exc)
    return None if errors else total  # a partial total would read as exact


def _claim_entry(root: Path, name: str) -> Path:
    # The random part keeps ids unique across roots: two volumes can trash the
    # same name in the same second, and restore() looks an id up in every root.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    # Filesystem encoding, so an undecodable name round-trips instead of raising.
    base = os.fsdecode(os.fsencode(f"{stamp}-{name}")[:_MAX_ENTRY_NAME])
    for n in range(_MAX_SUFFIX):
        entry = root / (base if n == 0 else f"{base}-{n}")
        try:
            os.mkdir(entry, 0o700)
        except FileExistsError:
            continue
        return entry
    raise TrashRefused(f"no free entry name under {root}")


def trash(path: str | os.PathLike[str], *, reason: str, caller: str) -> Tombstone:
    """Move ``path`` into its volume's trash and return the tombstone.

    Raises TrashRefused, leaving the item untouched, when it is missing, a mount
    point, a parent of the trash or of $HOME, already in a trash, on the Claude
    Code temp volume, or on another device than its trash. A symlink is trashed
    as the link itself, never its target.
    """
    raw = Path(os.path.abspath(path))
    if raw.name in ("", ".", ".."):
        raise TrashRefused(f"not a trashable path: {path}")
    item = raw.parent.resolve() / raw.name
    try:
        st = os.lstat(item)
    except FileNotFoundError:
        raise TrashRefused(f"{item} does not exist") from None
    if os.path.ismount(item):
        raise TrashRefused(f"{item} is a mount point")
    home = Path.home().resolve()
    root = _root_for(st.st_dev, item)
    for protected in (home, home_trash_root(), root):
        if _within(protected, item):
            raise TrashRefused(f"{item} contains {protected}")
    if _within(item, root) or _within(item, home_trash_root()):
        raise TrashRefused(f"{item} is already in a trash")
    if _within(item, (genesis_home() / "cc-tmp").resolve()):
        raise TrashRefused(f"{item} is on the Claude Code temp volume, which has its own retention")
    with contextlib.suppress(OSError):
        if os.stat(root.parent).st_dev != st.st_dev:
            raise TrashRefused(f"{item} is on another device than {root}; it is never copied")
    _ensure_root(root)
    entry = _claim_entry(root, item.name)
    stone = Tombstone(
        entry_id=entry.name,
        root=str(root),
        original_path=str(item),
        name=item.name,
        kind=_kind(st),
        size=_size(item, st),
        reason=reason,
        caller=caller,
        session_id=os.environ.get("CLAUDE_SESSION_ID") or os.environ.get("GENESIS_SESSION_ID"),
        trashed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        atomic_write_text(entry / TOMBSTONE, json.dumps(asdict(stone), indent=2) + "\n")
        os.rename(item, entry / ITEM)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(entry / TOMBSTONE)
        with contextlib.suppress(OSError):
            os.rmdir(entry)
        if exc.errno == errno.EXDEV:
            raise TrashRefused(
                f"{item} is on another device than {root}; it is never copied"
            ) from None
        raise TrashRefused(f"could not move {item} into the trash: {exc.strerror}") from None
    return stone


def _load(entry: Path) -> Tombstone | None:
    try:
        data = json.loads((entry / TOMBSTONE).read_text())
        return Tombstone(**data)
    except (OSError, ValueError, TypeError):
        return None


def _roots() -> list[Path]:
    """The home trash plus any per-mount trash of this uid that exists."""
    try:
        # surrogateescape: a mount path that is not UTF-8 must not abort listing.
        with open("/proc/self/mounts", encoding="utf-8", errors="surrogateescape") as f:
            mounts = [line.split()[1] for line in f if len(line.split()) > 1]
    except OSError:
        mounts = []
    return _roots_from([_unescape_mount(m) for m in mounts])


def _roots_from(mount_points: list[str]) -> list[Path]:
    roots = [home_trash_root()]
    seen = {_dir_key(roots[0])}
    for mount in mount_points:
        candidate = Path(mount) / f".genesis-trash-{os.getuid()}"
        # One directory can be reachable through two mount points; list it once.
        key = _dir_key(candidate)
        if key not in seen and _is_own_dir(candidate):
            seen.add(key)
            roots.append(candidate)
    return roots


def _dir_key(path: Path) -> tuple[int, int] | Path:
    try:
        st = os.stat(path)
    except OSError:
        return path
    return (st.st_dev, st.st_ino)


def _unescape_mount(field: str) -> str:
    """Undo the octal escapes the kernel writes in /proc/self/mounts (space,
    tab, newline and backslash appear as \\040, \\011, \\012, \\134)."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def _is_own_dir(path: Path) -> bool:
    """A plain directory (not a symlink) owned by this uid; False on any error,
    since an unreadable mount (MEASURED: /sys/fs/pstore) raises on stat."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid()


def list_entries(root: Path | None = None) -> list[Entry]:
    """Every entry, oldest first; incomplete entries are included and flagged."""
    out: list[Entry] = []
    for r in [root] if root else _roots():
        try:
            names = sorted(os.listdir(r))
        except OSError:
            continue
        for name in names:
            entry = r / name
            if not entry.is_dir() or entry.is_symlink():
                continue
            stone = _load(entry)
            out.append(Entry(entry, stone, stone is not None and os.path.lexists(entry / ITEM)))
    return out


def restore(
    entry_id: str, *, to: str | os.PathLike[str] | None = None, root: Path | None = None
) -> Path:
    """Rename an entry's item back to its original path (or ``to``).

    Refuses an incomplete entry or an existing destination. The existence check
    and the rename are two steps; the stdlib has no no-replace rename, so a file
    created in between would be replaced (a narrow, documented race).
    """
    match = [e for e in list_entries(root) if e.path.name == entry_id]
    if not match:
        raise TrashRefused(f"no trash entry {entry_id}")
    if len(match) > 1:
        roots = ", ".join(str(e.path.parent) for e in match)
        raise TrashRefused(f"trash entry {entry_id} exists in more than one root ({roots})")
    entry = match[0]
    if not entry.complete or entry.tombstone is None:
        raise TrashRefused(f"trash entry {entry_id} is incomplete")
    dest = Path(os.path.abspath(to)) if to is not None else Path(entry.tombstone.original_path)
    if os.path.lexists(dest):
        raise TrashRefused(f"{dest} already exists")
    if not dest.parent.is_dir():
        raise TrashRefused(f"{dest.parent} does not exist")
    try:
        os.rename(entry.path / ITEM, dest)
    except OSError as exc:
        raise TrashRefused(f"could not restore to {dest}: {exc.strerror}") from None
    with contextlib.suppress(OSError):
        os.unlink(entry.path / TOMBSTONE)
    with contextlib.suppress(OSError):
        os.rmdir(entry.path)
    return dest
