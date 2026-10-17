"""Exact source retention outside temporary storage; never corpus approval.

Logical origin paths remain evidence identities. Independent backup origin is a
trusted operator assertion plus verified restored bytes, not proven by a copy.
"""
import hashlib
import os
import re
import stat
from contextlib import contextmanager
from pathlib import Path

from .evidence import Incomplete, canonical, check_directory, private_open, sync_directory


def store():
    return Path.home().resolve() / ".genesis" / "qualification-sources"


def restore_store():
    return Path.home().resolve() / ".genesis" / "qualification-source-restores"


def source_map(sources):
    if (not isinstance(sources, dict) or not sources
            or any(not isinstance(p, str) or not p or "\x00" in p
                   or not isinstance(h, str) or re.fullmatch("[0-9a-f]{64}", h) is None
                   for p, h in sources.items())):
        raise Incomplete("invalid retained source map")
    return dict(sources)


def _mkdir(path):
    """Make ancestor entries durable through the first existing parent."""
    missing, parent = [], path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        sync_directory(directory)
        sync_directory(directory.parent)
    check_directory(path)


@contextmanager
def _read(path, *, private):
    fd = private_open(path, os.O_RDONLY) if private else os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    )
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise Incomplete("retained source must be a regular file")
        yield stream
        fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        def stamp(info):
            return tuple(getattr(info, key) for key in fields)

        if stamp(before) != stamp(os.fstat(stream.fileno())) or stamp(before) != stamp(path.lstat()):
            raise Incomplete("source changed during retention verification")


def archive(sources, generation):
    """Publish one immutable generation; retain evidence on every failure."""
    sources = source_map(sources)
    if (not isinstance(generation, str) or not generation or generation in (".", "..")
            or Path(generation).name != generation or "\x00" in generation):
        raise Incomplete("invalid source generation")
    root = store()
    if not root.parent.parent.is_dir():
        raise Incomplete("source retention requires an existing HOME directory")
    if root.resolve() != root:
        raise Incomplete("source store must not have symlinked ancestors")
    _mkdir(root)
    # Existing ancestors may remain from failed initialization. Establish this
    # invocation's durability chain back to the pre-existing HOME boundary.
    for directory in (root, root.parent, root.parent.parent):
        sync_directory(directory)
    destination = root / generation
    destination.mkdir(mode=0o700)  # Existing and partial generations are never overwritten.
    sync_directory(root)
    blobs = destination / "blobs"
    blobs.mkdir(mode=0o700)
    written = set()
    for original, expected in sources.items():
        with _read(Path(original), private=False) as source:
            if expected in written:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            else:
                target = private_open(blobs / expected, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                with os.fdopen(target, "wb") as output:
                    digest = hashlib.sha256()
                    while block := source.read(1024 * 1024):
                        output.write(block)
                        digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                    actual = digest.hexdigest()
            if actual != expected:
                raise Incomplete("source bytes differ from frozen identity")
        written.add(expected)
    sync_directory(blobs)
    sync_directory(destination)
    manifest = private_open(destination / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    with os.fdopen(manifest, "wb") as output:
        output.write(canonical({"version": 1, "sources": sources}))
        output.flush()
        os.fsync(output.fileno())
    # Failure here reports failure with uncertain durability, even if a later
    # full reader finds complete bytes. Never overwrite, roll back or repair.
    sync_directory(destination)
    sync_directory(root)
    return destination


def _verify(directory, sources):
    check_directory(directory)
    blobs = directory / "blobs"
    check_directory(blobs)
    if ({p.name for p in directory.iterdir()} != {"blobs", "manifest.json"}
            or {p.name for p in blobs.iterdir()} != set(sources.values())):
        raise Incomplete("retained source archive has missing or foreign members")
    with _read(directory / "manifest.json", private=True) as stream:
        expected = canonical({"version": 1, "sources": sources})
        if stream.read(len(expected) + 1) != expected:
            raise Incomplete("retained source manifest differs from frozen identities")
    identities = set()
    for expected in set(sources.values()):
        with _read(blobs / expected, private=True) as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise Incomplete("retained source bytes changed")
            info = os.fstat(stream.fileno())
            identities.add((info.st_dev, info.st_ino))
    return identities


def retained(sources, retention):
    """Check actual primary and restored bytes; never certify backup independence."""
    sources = source_map(sources)
    if (not isinstance(retention, dict)
            or set(retention) != {"primary_generation", "restored_backup_generation", "backup_evidence"}
            or any(not isinstance(retention[k], str) or not retention[k].strip() for k in retention)):
        raise Incomplete("source retention and genuine restore evidence are required")
    primary = Path(retention["primary_generation"])
    restored = Path(retention["restored_backup_generation"])
    # A fixed durable store is an accident-prevention boundary, not protection
    # against an operator deliberately changing HOME or editing their own files.
    if (primary.parent != store() or restored.parent != restore_store()
            or primary.name in (".", "..") or restored.name in (".", "..")):
        raise Incomplete("source archives must use the durable primary and restore stores")
    for root in (store(), restore_store()):
        if root.resolve() != root:
            raise Incomplete("source stores must not have symlinked ancestors")
        check_directory(root)
    first, second = _verify(primary, sources), _verify(restored, sources)
    if first & second:
        raise Incomplete("restored backup aliases the primary source files")
    return dict(sources)
