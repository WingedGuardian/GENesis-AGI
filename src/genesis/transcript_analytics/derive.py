"""Materialized snapshots of the derived views.

The live views assemble rows across ~15k per-source files and take 15-30 s per
query on the full store; a snapshot answers in ~0.02 s (measured 2026-10-04).
A snapshot is built after any ingest that changed a source:

    <data>/derived/<stamp>/<view>.parquet + MANIFEST.json
    <data>/derived/current -> <stamp>        (swapped atomically with os.replace)

Readers resolve ``current`` once, so they never see a mix of old and new views.
The snapshot it replaced is kept (an in-flight query may still read it); older
ones are removed. Builds hold the store lock, so two builds never interleave.
It is rebuildable at any time from the per-source files.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis.transcript_analytics import query, store
from genesis.transcript_analytics.locks import publication, restore_epoch

DERIVED_VIEWS = ("tool_calls", "turns", "hooks", "events", "session_meta", "agents", "sessions")


def is_current(data: Path) -> bool:
    return query.derived_current(data)


def build(data: Path, *, lock_path: Path | None = None) -> dict:
    t0 = time.monotonic()
    root = data / "derived"
    with store._locked(lock_path or store.DEFAULT_LOCK):
        root.mkdir(
            parents=True, exist_ok=True
        )  # BEFORE reading input_fp: creating it bumps data's mtime
        # Leftovers from a killed build (SIGKILL/SIGTERM skip Python cleanup): re-audit SF-4.
        if (root / ".staging").is_dir():
            for leftover in (root / ".staging").iterdir():
                shutil.rmtree(leftover, ignore_errors=True)
        for link in root.glob(".current.*"):
            link.unlink(missing_ok=True)
        input_fp = data.stat().st_mtime_ns  # under the lock: no publish can move it until we finish
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        staging = root / ".staging" / stamp
        staging.mkdir(parents=True)
        try:
            keys, excluded = store.compatible_sources(data)
            with contextlib.closing(query.connect(data, live=True)) as con:
                for v in DERIVED_VIEWS:
                    out = str(staging / f"{v}.parquet").replace("'", "''")
                    con.execute(
                        f"COPY (SELECT * FROM {v}) TO '{out}' (FORMAT parquet, COMPRESSION zstd)"  # noqa: S608 — fixed names, escaped path
                    )
            manifest = {
                "views_version": query.VIEWS_VERSION,
                "restore_epoch": restore_epoch(),
                "input_fp": input_fp,
                "semantics": {k.decode(): os.fsdecode(v) for k, v in store.semantics().items()},
                "coverage": {"included": len(keys), "excluded": excluded},
                "inventory": store.inventory(data),
                "sources": [
                    {
                        k.decode(): (
                            store.source_identity(os.fsdecode(v))
                            if k == b"ta.source"
                            else v.decode()
                        )
                        for k, v in store.source_metadata(data, key)[0].items()
                        if k in (b"ta.source", b"ta.fp", b"ta.generation")
                    }
                    for key in keys
                ],
                "built_at": datetime.now(UTC).isoformat(),
                "views": list(DERIVED_VIEWS),
            }
            (staging / "MANIFEST.json").write_text(json.dumps(manifest))
            final = root / stamp
            os.rename(staging, final)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        with publication(exclusive=True):
            previous = os.readlink(root / "current") if (root / "current").is_symlink() else None
            link_tmp = root / f".current.{os.getpid()}"
            os.symlink(stamp, link_tmp)
            os.replace(link_tmp, root / "current")
            keep = {stamp, previous}
            for d in root.iterdir():
                if (
                    d.is_dir()
                    and not d.is_symlink()
                    and not d.name.startswith(".")
                    and d.name not in keep
                ):
                    shutil.rmtree(d, ignore_errors=True)
    return {
        "dir": str(final),
        "views": sorted(DERIVED_VIEWS),
        "seconds": round(time.monotonic() - t0, 2),
    }
