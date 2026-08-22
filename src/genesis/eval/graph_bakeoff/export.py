"""Frozen-snapshot export + manifest for the bake-off.

Reuses the sanctioned ``eval.bench.isolation.snapshot_prod_db`` (WAL-safe sqlite3
backup — NOT ``immutable=1``, which is WAL-blind and would miss un-checkpointed
writes) so we don't reinvent it. Adds a date-stamped filename, a sha256, and a
manifest so every downstream number cites the exact snapshot.

Large temp lives under ``~/tmp/graph-bakeoff/`` (never /tmp or cc-tmp — CLAUDE.md
temp rule). Timestamps are passed in by the caller (no wall-clock reads buried in
library code, so the manifest is reproducible/attributable).

Worktree note: when run from a worktree, ``genesis.env.genesis_db_path()`` may
resolve to the worktree path; pass ``source`` explicitly (or set
``GENESIS_REPO_ROOT`` to the main tree) to snapshot the real live DB.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from genesis.eval.bench.isolation import snapshot_prod_db

DEFAULT_OUT = Path.home() / "tmp" / "graph-bakeoff"
_MANIFEST_TABLES = (
    "memory_links",
    "memory_metadata",
    "entities",
    "entity_mentions",
    "entity_links",
)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _counts(snapshot: Path) -> dict[str, int]:
    import sqlite3

    conn = sqlite3.connect(f"file:{snapshot}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for t in _MANIFEST_TABLES:  # fixed allowlist, not user input
            out[t] = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
        return out
    finally:
        conn.close()


def export_snapshot(*, stamp: str, out_dir: Path = DEFAULT_OUT, source: Path | None = None) -> dict:
    """Snapshot the prod DB to ``out_dir/snapshot-<stamp>.db`` + write manifest.json.

    ``stamp`` is a caller-supplied date string (e.g. "2026-08-06"). Returns the
    manifest dict (also written to ``out_dir/manifest.json``).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    # snapshot_prod_db writes out_dir/genesis.db; move to the stamped, immutable name.
    tmp = snapshot_prod_db(out_dir, source=source)
    dest = out_dir / f"snapshot-{stamp}.db"
    if dest.exists():
        dest.unlink()
    tmp.rename(dest)

    manifest = {
        "stamp": stamp,
        "snapshot": str(dest),
        "size_mb": round(dest.stat().st_size / 1e6, 1),
        "sha256": _sha256(dest),
        "counts": _counts(dest),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest
