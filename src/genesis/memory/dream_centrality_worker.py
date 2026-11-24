"""Read and score a committed graph snapshot inside a capped scope."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable
    from typing import BinaryIO

PROTOCOL_VERSION = 1
MAX_RECORD_BYTES = 64 * 1024


async def score_snapshot(database: Path) -> tuple[str, list[tuple[str, float]]]:
    from genesis.db.connection import connect_aiosqlite_ro
    from genesis.memory.graphstore import invalid_memory_ids
    from genesis.memory.graphstore_nx import build_graph, ranked_betweenness

    async with connect_aiosqlite_ro(database, timeout=5) as db:
        await db.execute("BEGIN")
        snapshot_at = datetime.now(UTC).isoformat()
        invalid = await invalid_memory_ids(db, as_of=snapshot_at)
        cursor = await db.execute("SELECT source_id, target_id, link_type, strength FROM memory_links")
        rows = await cursor.fetchall()
    return snapshot_at, ranked_betweenness(build_graph(rows, invalid), top_n=None)


def write_result(output: BinaryIO, records: Iterable, byte_limit: int) -> None:
    # The producer and consumer both bound the stream. An exception leaves an
    # incomplete result and a nonzero process exit, so it cannot replace the cache.
    for record in records:
        line = json.dumps(record, ensure_ascii=True, allow_nan=False).encode() + b"\n"
        if len(line) > min(byte_limit, MAX_RECORD_BYTES):
            raise ValueError("centrality worker result exceeded its byte limit")
        output.write(line)
        byte_limit -= len(line)


def main() -> int:
    from genesis.hostmetrics.run import verify_current_caps

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("run_id")
    parser.add_argument("ram_bytes", type=int)
    parser.add_argument("cpu_percent", type=int)
    args = parser.parse_args()
    verify_current_caps(args.ram_bytes, args.cpu_percent)
    snapshot_at, scores = asyncio.run(score_snapshot(args.database))
    header = {
        "version": PROTOCOL_VERSION,
        "run_id": args.run_id,
        "snapshot_at": snapshot_at,
        "nodes_scored": len(scores),
        "nodes_persisted": sum(score > 0 for _, score in scores),
        "top": scores[0] if scores else None,
    }
    records = chain((header,), (row for row in scores if row[1] > 0))
    write_result(sys.stdout.buffer, records, args.ram_bytes // 8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
