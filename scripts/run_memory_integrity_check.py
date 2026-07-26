#!/usr/bin/env python3
"""Run the memory consistency check on demand and print the report.

Operator tool for the Phase-0 "make silence loud" spine. Runs the READ-ONLY
cross-backend consistency check (memory_metadata <-> Qdrant <-> memory_fts)
against the live DB and prints the classification. Persistence is opt-in
(``--persist``) — by default this touches nothing.

The recall-health probe is NOT run here: it needs the in-process HybridRetriever
(server context). This standalone runner covers the consistency half, which is
enough to eyeball the store-integrity state and time the scan.

Usage:
    python scripts/run_memory_integrity_check.py                 # print report
    python scripts/run_memory_integrity_check.py --db /path.db   # a copy
    python scripts/run_memory_integrity_check.py --persist       # also store a row
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path


def _live_db_path() -> str:
    """The live genesis.db, home-anchored (GENESIS_DB_PATH override honored).

    NOT genesis_db_path() — that is repo-root-anchored and resolves to an empty
    <worktree>/data/genesis.db when run from a worktree.
    """
    return os.environ.get("GENESIS_DB_PATH") or str(Path.home() / "genesis" / "data" / "genesis.db")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="DB path (default: the live genesis.db)")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="also write the report to memory_consistency_reports (default: read-only)",
    )
    args = parser.parse_args()

    from genesis.memory import integrity_config
    from genesis.memory.integrity import run_consistency_check
    from genesis.qdrant.collections import get_client

    # Home-anchor the live-DB default (env-override aware). NOT the repo-anchored
    # genesis_db_path(): run from a worktree that resolves to an empty
    # <worktree>/data/genesis.db and the scan reads no data (a documented trap).
    db_path = args.db or _live_db_path()

    cfg = integrity_config.load_config()
    report = await run_consistency_check(
        db_path=db_path,
        qdrant_client=get_client(),
        sample_fraction=integrity_config.knob_float01(cfg, "sample_fraction"),
        max_points=integrity_config.knob_int(cfg, "max_points"),
        severe_min_count=integrity_config.knob_int(cfg, "severe_min_count"),
        pollution_min_count=integrity_config.knob_int(cfg, "pollution_min_count"),
        pollution_fraction=integrity_config.knob_float01(cfg, "pollution_fraction"),
        max_offender_sample=integrity_config.knob_int(cfg, "max_offender_sample"),
    )
    print(json.dumps(dataclasses.asdict(report), indent=2, default=str))

    if args.persist:
        import aiosqlite

        from genesis.db.crud import memory_integrity as mi_crud

        conn = await aiosqlite.connect(db_path)
        try:
            rid = await mi_crud.insert_consistency_report(
                conn,
                status=report.status,
                counts=report.counts,
                total_rows=report.total_rows,
                sampled_rows=report.sampled_rows,
                sample_fraction=report.sample_fraction,
                truncated=report.truncated,
                offender_sample=report.offender_sample,
                unknown_reason=report.unknown_reason,
                duration_ms=report.duration_ms,
            )
            print(f"\npersisted report id: {rid}", file=sys.stderr)
        finally:
            await conn.close()

    # Non-zero exit on degraded so the runner is scriptable in a check.
    return 1 if report.status == "degraded" else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
