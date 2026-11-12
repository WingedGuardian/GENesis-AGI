#!/usr/bin/env python3
"""Enumerate retained exact-tagged owner rulings without ranking or writes."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from inbox_coverage_report import connect_readonly  # noqa: E402

from genesis.env import genesis_db_path  # noqa: E402


def _json_value(value):
    """Keep malformed SQLite primitive types distinguishable in strict JSON."""
    if isinstance(value, bytes):
        return {"type": "blob", "hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"type": "nonfinite-real", "value": repr(value)}
    return value


def read_rulings(db: sqlite3.Connection) -> dict:
    """Read one snapshot; metadata is evidence, never an authority verdict."""
    db.execute("PRAGMA query_only = ON")
    db.execute("BEGIN")
    cursor = db.execute(
        "SELECT f.rowid, f.memory_id, f.content, f.source_type, f.tags, f.collection, m.* "
        "FROM memory_fts f LEFT JOIN memory_metadata m ON m.memory_id = f.memory_id "
        "ORDER BY f.rowid"
    )
    metadata_columns = [column[0] for column in cursor.description[6:]]
    records = []
    nontext_tag_rows = []
    for row in cursor:
        tags = row[4]
        if tags is None:
            continue
        if not isinstance(tags, str):
            nontext_tag_rows.append(row[0])
            continue
        # MATCH tokenization omits some Unicode whitespace boundaries. The
        # qualified full scan preserves the exact whitespace-token population.
        if "inbox_disposition" not in tags.split():
            continue
        metadata = dict(zip(metadata_columns, row[6:], strict=True))
        records.append({
            "fts_rowid": row[0],
            **{key: _json_value(value) for key, value in zip(
                ("memory_id", "content", "source_type", "tags", "collection"),
                row[1:6], strict=True,
            )},
            "metadata": {key: _json_value(value) for key, value in metadata.items()}
            if metadata.get("memory_id") is not None else None,
            "authority": "not_adjudicated",
        })
    return {
        "scope": "retained_exact_tagged_fts_only",
        "complete": not nontext_tag_rows,
        "nontext_tag_rows": nontext_tag_rows,
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        with closing(connect_readonly(args.db if args.db is not None else genesis_db_path())) as db:
            result = read_rulings(db)
        rendered = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"inbox ruling history unavailable: {exc}", file=sys.stderr)
        return 2
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
