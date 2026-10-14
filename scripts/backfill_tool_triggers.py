#!/usr/bin/env python3
"""Repair stored ``tool_trigger`` values with the tool-name normalizer.

Before the normalizer guarded every write, the procedure extractor and judge
stored CLI words (``ssh``), MCP short names and prose beside real tool names.
This runs every live row's ``tool_trigger`` through ``normalize_tool_trigger``:

- the cleaned tool list is written back (``NULL`` when every value drops);
- a CLI word becomes ``Bash`` only together with ``trigger_spec`` (its command
  pattern, ``repeat: every``), which needs Bash to be the row's one tool, no
  spec already there and a spec the validator accepts; otherwise the word is
  kept as written, inert as before. The patterns are provisional: the advisor
  that reads them sets the match contract, so apply only after it ships;
- the trigger cache is rebuilt only from this install's database, or into the
  cache ``GENESIS_PROCEDURE_TRIGGERS`` names;
- lesson rows are never touched (they are authored, and validated on write).

Dry-run by default; ``--apply`` writes and then regenerates the trigger cache.
Apply it only once the advisor matches on ``patterns`` (the trigger-spec
advisor ticket): until then a CLI word mapped to ``Bash`` would match any
Bash call its tags match, on a row that reaches the cache.
Idempotent: a repaired row normalizes to itself, so a re-run changes nothing.
The counts classify each row once (unchanged, cleaned, cli_mapped, all_dropped);
``cli_kept`` and ``spec_refused`` are cross-cuts that can overlap them.
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
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def backfill(db, *, apply: bool, regenerate_cache: bool = True) -> Counter:
    """Classify (and with ``apply``, repair) every live row; return the counts."""
    from genesis.learning.procedural.lesson_shape import validate_trigger_spec
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
        items = value if isinstance(value, list) else [value]
        counts["values_seen"] += len(items)
        # Judge each value alone. A CLI word becomes Bash only together with
        # its stored pattern; when the pattern cannot be stored (other tools on
        # the row, including a Bash the row names itself, plain or annotated; a
        # spec already there; or the validator refuses it), the word stays as written:
        # inert as before, and recoverable once the advisor can scope it.
        cli_items: list[str] = []
        kept_items: list = []
        patterns: list[str] = []
        for item in items:
            _, item_patterns, _ = normalize_tool_trigger([item])
            if item_patterns:
                cli_items.append(str(item).strip())
                patterns += [x for x in item_patterns if x not in patterns]
            else:
                kept_items.append(item)
        tools, _, dropped = normalize_tool_trigger(kept_items)
        counts["values_dropped"] += len(dropped)
        update: dict[str, str | None] = {}
        new_spec = None
        if cli_items and spec is None and not tools:
            try:
                new_spec = validate_trigger_spec({"patterns": patterns, "repeat": "every"})
            except ValueError:
                counts["spec_refused"] += 1
        if new_spec is not None:
            tools = ["Bash"]
            update["trigger_spec"] = json.dumps(new_spec)
        elif cli_items:
            tools += [c for c in dict.fromkeys(cli_items) if c not in tools]
            counts["cli_kept"] += 1
        if tools != value or not tools:  # a stored [] is a trigger to the promoter
            update["tool_trigger"] = json.dumps(tools) if tools else None
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
    if apply and regenerate_cache:
        from genesis.learning.procedural.trigger_cache import regenerate

        counts["cache_entries"] = await regenerate(db)
    return counts


async def _main(db_path: Path, apply: bool, *, own_db: bool = False) -> int:
    import aiosqlite

    if not Path(db_path).is_file():  # never create a database at a mistyped path
        print(f"No database at {db_path}")
        return 2
    # Admission fence (fail-closed): never write to a quarantined or restoring database.
    hooks = str(Path(__file__).resolve().parent / "hooks")
    if hooks not in sys.path:
        sys.path.insert(0, hooks)
    try:
        from db_admission_check import database_is_fenced

        fenced = database_is_fenced(db_path)
    except Exception:
        fenced = True
    if fenced:
        print(
            f"{db_path} is fenced (quarantined, restoring, or the fence is unreadable); not opened"
        )
        return 3
    # as_uri() escapes ? and # in the path; mode=rw never creates a database.
    db = await aiosqlite.connect(Path(db_path).resolve().as_uri() + "?mode=rw", uri=True)
    try:
        db.row_factory = aiosqlite.Row  # the cache regeneration reads rows by name
        columns = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(procedural_memory)")}
        if "lesson" not in columns:
            print(
                "procedural_memory has no lesson column: run after the schema migration (server start)."
            )
            return 1
        # The cache file is the running install's: rebuild it only from the
        # install's own database, or into an explicitly redirected cache.
        redirected = bool(os.environ.get("GENESIS_PROCEDURE_TRIGGERS"))
        counts = await backfill(db, apply=apply, regenerate_cache=own_db or redirected)
        if apply and not (own_db or redirected):
            print(
                "Cache not regenerated: --db is not this install's database and GENESIS_PROCEDURE_TRIGGERS is unset."
            )
    finally:
        await db.close()
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

    # Only the install's own database (no --db, no GENESIS_DB_PATH override) may
    # rebuild the install's cache.
    own_db = args.db is None and not os.environ.get("GENESIS_DB_PATH")
    sys.exit(asyncio.run(_main(args.db or genesis_db_path(), args.apply, own_db=own_db)))
