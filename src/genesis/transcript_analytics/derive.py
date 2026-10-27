"""Disposable view cache; only per-source records are durable history.

Build under the writer flock, stage derived/<stamp>, then atomically swap
derived/current. Keep the previous cache; readers fully read it before use
and fall back to their captured live sources if cache cleanup races them.
Restore discards only derived/. No epoch, catalog, lease or reader lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from . import locks, query, store

DERIVED_VIEWS = query._DERIVED


def is_current(data: Path) -> bool:
    return query.derived_current(data)


def _remove_cache_entry(path: Path) -> None:
    if not path.is_symlink() and path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _sweep(root: Path, keep: frozenset[str] = frozenset()) -> None:
    """Remove interrupted artifacts and obsolete snapshot directories.

    Drops everything outside ``keep``: ``.staging`` trees, ``.current.*``
    links, a real-directory ``current`` selector (malformed disposable
    layout) and orphan snapshot directories.
    """
    _remove_cache_entry(root / ".staging")
    for link in root.glob(".current.*"):
        _remove_cache_entry(link)
    current = root / "current"
    if not current.is_symlink() and current.is_dir():
        shutil.rmtree(current)
    for directory in root.iterdir():
        if (
            not directory.is_symlink() and directory.is_dir()
            and not directory.name.startswith(".") and directory.name not in keep
        ):
            shutil.rmtree(directory)


def build(data: Path, *, lock_path: Path | None = None, if_stale: bool = False) -> dict:
    started = time.monotonic()
    root = data / "derived"
    with locks.writer(lock_path or store.DEFAULT_LOCK):
        # Discard malformed disposable layouts and interrupted artifacts
        # before deciding there is nothing to do: an if_stale no-op must
        # not leave a crash's leftovers behind.
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            _remove_cache_entry(root)
        if root.is_dir():
            current = root / "current"
            keep = frozenset({os.readlink(current)}) if current.is_symlink() else frozenset()
            _sweep(root, keep)
        selection = store.select_sources(data)
        if if_stale and query.derived_current(data, selection=selection):
            return {"derived": "current; nothing to do"}
        store._make_directory(data)
        store._make_directory(root)
        staging_root = root / ".staging"
        staging_root.mkdir(exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
        staging = staging_root / stamp
        staging.mkdir()
        temporary = root / f".current.{os.getpid()}"
        try:
            with contextlib.closing(query.connect(data, live=True, selection=selection)) as con:
                schemas = {}
                for view in DERIVED_VIEWS:
                    path = staging / f"{view}.parquet"
                    escaped = str(path).replace("'", "''")
                    con.execute(
                        f"COPY (SELECT * FROM {view}) TO '{escaped}' (FORMAT parquet, COMPRESSION zstd)"  # noqa: S608 — fixed view names, escaped path
                    )
                    schemas[view] = [
                        list(row[:2]) for row in con.execute(f"DESCRIBE {view}").fetchall()  # noqa: S608 — fixed view names
                    ]
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
                state = query._READS[con]
                manifest = {
                    "views_version": query.VIEWS_VERSION,
                    "inputs_signature": state["inputs_signature"],
                    "coverage": state["coverage"],
                    "sources": [
                        {
                            k.decode(): store.source_identity(os.fsdecode(v))
                            if k == b"ta.source" else os.fsdecode(v)
                            for k, v in record.metadata.items() if k.startswith(b"ta.")
                        } for record in selection[0]
                    ],
                    "turn_identity_coverage": query.turn_identity_coverage(con),
                    "views": list(DERIVED_VIEWS),
                    "schemas": schemas,
                    "built_at": datetime.now(UTC).isoformat(),
                }
            with (staging / "MANIFEST.json").open("w") as handle:
                json.dump(manifest, handle)
                handle.flush()
                os.fsync(handle.fileno())
            store._fsync_directory(staging)
            final = root / stamp
            os.replace(staging, final)
            store._fsync_directory(root)
            current = root / "current"
            previous = os.readlink(current) if current.is_symlink() else None
            temporary.symlink_to(stamp)
            os.replace(temporary, current)
            store._fsync_directory(root)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            temporary.unlink(missing_ok=True)
            raise
        _sweep(root, frozenset({stamp, previous}) - {None})
    return {
        "dir": str(final), "views": sorted(DERIVED_VIEWS),
        "seconds": round(time.monotonic() - started, 2),
    }


def discard_after_restore(data: Path, *, lock_path: Path | None = None):
    """Drop only the disposable cache; leave every source and stamp untouched."""
    with locks.writer(lock_path or store.DEFAULT_LOCK):
        root = data / "derived"
        if root.is_symlink() or root.exists():
            _remove_cache_entry(root)
        # A retry must finish a failed removal fence even when the cache is gone.
        if data.is_dir():
            store._fsync_directory(data)
