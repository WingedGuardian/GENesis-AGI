"""Recoverable deletes: move user or project data into a Genesis-owned trash.

Stdlib only, importable without the Genesis runtime. Guide:
docs/reference/trash.md; CLI: ``<venv python> -m genesis.trash``.

There is one trash, ``~/.genesis/trash/`` (under ``GENESIS_HOME``), and an item
is always RENAMED into it, never copied. An item on another volume than the
trash is refused and left untouched: the device number catches a separate disk
or a btrfs subvolume, and the kernel's EXDEV from rename(2) catches the rest
(a bind mount, an overlayfs lower layer). Each entry is a directory holding the
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
_ANOTHER_VOLUME = (
    "{item} is on another volume than the Genesis trash ({root}). It is never copied; "
    "it was left in place. Ask the user before deleting it any other way"
)


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


def _root() -> Path:
    # The parent resolved, so a symlinked GENESIS_HOME compares equal to
    # resolved items; the root itself not, so _ensure_root still refuses a
    # trash directory that is a symlink.
    # Raises on a symlink loop; trash() and list_entries() handle that.
    root = home_trash_root()
    return root.parent.resolve() / root.name


def _within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _ensure_root(root: Path) -> None:
    """Create the root 0700, then refuse anything that is not ours."""
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise TrashRefused(f"cannot create trash root {root}: {exc.strerror}") from None
    _check_root(root)


def _check_root(root: Path) -> None:
    """Refuse a root that is a symlink, not a directory, another user's, or not
    0700: deleting into it, or restoring a forged entry out of it, is unsafe."""
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


def _entry_name(stamp: str, name: str) -> str:
    """``<stamp>-<name>`` capped at _MAX_ENTRY_NAME bytes, cut on a character
    boundary (a surrogate-escaped byte is one character and one byte)."""
    base = f"{stamp}-{name}"
    while len(os.fsencode(base)) > _MAX_ENTRY_NAME:
        base = base[:-1]
    return base


def _claim_entry(root: Path, name: str) -> Path:
    base = _entry_name(datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"), name)
    for n in range(_MAX_SUFFIX):
        entry = root / (base if n == 0 else f"{base}-{n}")
        try:
            os.mkdir(entry, 0o700)
        except FileExistsError:
            continue
        except OSError as exc:  # disk full, read-only, permissions: the item is untouched
            raise TrashRefused(
                f"cannot create a trash entry under {root}: {exc.strerror}"
            ) from None
        return entry
    raise TrashRefused(f"no free entry name under {root}")


def _refuse_sentinel(path: str | os.PathLike[str]) -> None:
    """Refuse '', '.', '..' and any path ending in them, BEFORE normalising:
    abspath turns them into the working directory or its parent. A ``Path``
    has already dropped a trailing '.', so ``Path("sub/.")`` means ``sub``."""
    text = os.fsdecode(os.fspath(path))
    last = text.rstrip("/").rsplit("/", 1)[-1]
    if last in ("", ".", ".."):
        raise TrashRefused(f"not a trashable path: {text!r}")


def trash(path: str | os.PathLike[str], *, reason: str, caller: str) -> Tombstone:
    """Move ``path`` into the Genesis trash and return the tombstone.

    Raises TrashRefused, leaving the item untouched, when it is missing,
    unreadable, a mount point, a parent of the trash or of $HOME, already in
    the trash, on the Claude Code temp volume, or on another volume than the
    trash. A symlink is trashed as the link itself, never its target.
    """
    _refuse_sentinel(path)
    raw = Path(os.path.abspath(path))
    try:
        item = raw.parent.resolve() / raw.name
        st = os.lstat(item)
    except FileNotFoundError:
        raise TrashRefused(f"{raw} does not exist") from None
    except (OSError, ValueError, RuntimeError) as exc:  # a loop, a NUL, permissions
        raise TrashRefused(f"cannot read {raw}: {exc}") from None
    if os.path.ismount(item):
        raise TrashRefused(f"{item} is a mount point")
    try:
        root = _root()
    except (OSError, RuntimeError) as exc:
        raise TrashRefused(f"cannot read the trash location: {exc}") from None
    for protected in (Path.home().resolve(), root):
        if _within(protected, item):
            raise TrashRefused(f"{item} contains {protected}")
    if _within(item, root):
        raise TrashRefused(f"{item} is already in the trash")
    if _within(item, (genesis_home() / "cc-tmp").resolve()):
        raise TrashRefused(f"{item} is on the Claude Code temp volume, which has its own retention")
    try:
        same_device = os.stat(root.parent).st_dev == st.st_dev
    except OSError as exc:
        raise TrashRefused(
            f"cannot read the trash's parent {root.parent}: {exc.strerror}"
        ) from None
    if not same_device:
        raise TrashRefused(_ANOTHER_VOLUME.format(item=item, root=root))
    kind, size = _kind(st), _size(item, st)
    _ensure_root(root)
    entry = _claim_entry(root, item.name)
    stone = Tombstone(
        entry_id=entry.name,
        root=str(root),
        original_path=str(item),
        name=item.name,
        kind=kind,
        size=size,
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
        if exc.errno == errno.EXDEV:  # a bind mount or overlay: same device, other mount
            raise TrashRefused(_ANOTHER_VOLUME.format(item=item, root=root)) from None
        raise TrashRefused(f"could not move {item} into the trash: {exc.strerror}") from None
    return stone


def _load(entry: Path) -> Tombstone | None:
    try:
        data = json.loads((entry / TOMBSTONE).read_text())
        return Tombstone(**data)
    except (OSError, ValueError, TypeError):
        return None


def list_entries() -> list[Entry]:
    """Every entry, oldest first; incomplete entries are included and flagged.

    A trash that does not exist yet is empty. One that exists but cannot be
    read raises TrashRefused: reporting it as empty would hide recoverable data.
    """
    try:
        root = _root()
        if not os.path.lexists(root):
            return []
        _check_root(root)
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError) as exc:
        raise TrashRefused(f"cannot read the trash: {exc}") from None
    out: list[Entry] = []
    for name in names:
        entry = root / name
        if not entry.is_dir() or entry.is_symlink():
            continue
        stone = _load(entry)
        out.append(Entry(entry, stone, stone is not None and os.path.lexists(entry / ITEM)))
    return out


def restore(entry_id: str, *, to: str | os.PathLike[str] | None = None) -> Path:
    """Rename an entry's item back to its original path (or ``to``).

    Refuses an incomplete entry or an existing destination. The existence check
    and the rename are two steps; the stdlib has no no-replace rename, so a file
    created in between would be replaced (a narrow, documented race).
    """
    match = [e for e in list_entries() if e.path.name == entry_id]
    if not match:
        raise TrashRefused(f"no trash entry {entry_id}")
    entry = match[0]
    if not entry.complete or entry.tombstone is None:
        raise TrashRefused(f"trash entry {entry_id} is incomplete")
    dest = Path(os.path.abspath(to)) if to is not None else Path(entry.tombstone.original_path)
    try:
        if os.path.lexists(dest):
            raise TrashRefused(f"{dest} already exists")
        if not dest.parent.is_dir():
            raise TrashRefused(f"{dest.parent} does not exist")
        os.rename(entry.path / ITEM, dest)
    except (OSError, ValueError) as exc:
        raise TrashRefused(f"could not restore to {dest}: {exc}") from None
    with contextlib.suppress(OSError):
        os.unlink(entry.path / TOMBSTONE)
    with contextlib.suppress(OSError):
        os.rmdir(entry.path)
    return dest
