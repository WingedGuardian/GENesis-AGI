#!/usr/bin/env python3
"""Repair stored ``tool_trigger`` values with the tool-name normalizer.

Before the normalizer guarded every write, the procedure extractor and judge
stored CLI words (``ssh``), MCP short names and prose beside real tool names.
This runs every live row's ``tool_trigger`` through ``normalize_tool_trigger``:

- the cleaned tool list is written back (``NULL`` when every value drops);
- a CLI command mapped to ``Bash`` also writes ``trigger_spec`` with its
  command pattern and ``repeat: every``, unless the row already has a spec;
- lesson rows are never touched (they are authored, and validated on write).

Dry-run by default; ``--apply`` writes and then regenerates the trigger cache.
Apply it only once the advisor matches on ``patterns`` (the trigger-spec
advisor ticket): until then a CLI word mapped to ``Bash`` would match any
Bash call its tags match, on a row that reaches the cache.
Idempotent: a repaired row normalizes to itself, so a re-run changes nothing.
Run it after the schema migration that adds ``trigger_spec`` and ``lesson``,
from the main checkout (the regenerated cache lands under the checkout it runs
from, unless ``GENESIS_PROCEDURE_TRIGGERS`` points elsewhere).

    python scripts/backfill_tool_triggers.py            # dry-run, prints counts
    python scripts/backfill_tool_triggers.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def backfill(db, *, apply: bool) -> Counter:
    """Classify (and with ``apply``, repair) every live row; return the counts."""
    from genesis.learning.procedural.tool_names import normalize_tool_trigger

    rows = await db.execute_fetchall(
        "SELECT id, tool_trigger, trigger_spec FROM procedural_memory "
        "WHERE deprecated = 0 AND lesson IS NULL AND tool_trigger IS NOT NULL"
    )
    counts: Counter = Counter(rows_scanned=len(rows))
    for row_id, raw, spec in rows:
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            value = raw  # a bare string is one value
        tools, patterns, dropped = normalize_tool_trigger(value)
        counts["values_seen"] += len(value) if isinstance(value, list) else 1
        counts["values_dropped"] += len(dropped)
        update: dict[str, str | None] = {}
        if tools != value or not tools:  # a stored [] is a trigger to the promoter
            update["tool_trigger"] = json.dumps(tools) if tools else None
        if patterns and spec is None:
            update["trigger_spec"] = json.dumps({"patterns": patterns, "repeat": "every"})
        if not update:
            counts["unchanged"] += 1
            continue
        counts[
            "all_dropped" if not tools else "cli_mapped" if "trigger_spec" in update else "cleaned"
        ] += 1
        if apply:
            sets = ", ".join(f"{column} = ?" for column in update)
            # Only if the row still holds what was read: a refine that landed in
            # between is kept, and counted.
            cursor = await db.execute(
                f"UPDATE procedural_memory SET {sets} WHERE id = ? AND tool_trigger IS ? "  # noqa: S608 (fixed column names)
                "AND trigger_spec IS ? AND lesson IS NULL AND deprecated = 0",
                (*update.values(), row_id, raw, spec),
            )
            if cursor.rowcount == 0:
                counts["skipped_changed"] += 1
    if apply:
        await db.commit()
        from genesis.learning.procedural.trigger_cache import regenerate

        counts["cache_entries"] = await regenerate(db)
    return counts


async def _main(db_path: Path, apply: bool) -> int:
    import aiosqlite

    async with aiosqlite.connect(str(db_path)) as db:
        db.row_factory = aiosqlite.Row  # the cache regeneration reads rows by name
        columns = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(procedural_memory)")}
        if "lesson" not in columns:
            print(
                "procedural_memory has no lesson column: run after the schema migration (server start)."
            )
            return 1
        counts = await backfill(db, apply=apply)
    print(("APPLIED" if apply else "DRY-RUN") + ":", json.dumps(dict(counts), sort_keys=True))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true", help="write the repairs and regenerate the cache"
    )
    parser.add_argument("--db", type=Path, help="path to genesis.db (default: genesis_db_path())")
    args = parser.parse_args()
    from genesis.env import genesis_db_path

    sys.exit(asyncio.run(_main(args.db or genesis_db_path(), args.apply)))
