#!/usr/bin/env python3
"""Repair stored ``tool_trigger`` values with the tool-name normalizer.

Before the normalizer guarded every write, the procedure extractor and judge
stored CLI words (``ssh``), MCP short names and prose beside real tool names.
This runs every live row's ``tool_trigger`` through ``normalize_tool_trigger``:

- the cleaned tool list is written back (``NULL`` when every value drops);
- CLI words follow ``operations.commands_spec``, the rule every writer applies:
  on a row whose one tool they name (Bash) and with no spec already there, they
  become ``Bash`` with ``trigger_spec.commands`` (``repeat: every``, matched by
  the advisor on the parsed command); otherwise the command words are stored as
  written, inert as before, so no row is widened to every Bash call. A row that
  names Bash itself already fires on every Bash call, so its command words go;
- the trigger cache is rebuilt only from this checkout's ``data/genesis.db``
  (however its path is given), or with ``--rebuild-cache``; it is written
  where this checkout's trigger cache module puts it, which is the live
  advisor's only when run from the main checkout;
- lesson rows are never touched (they are authored, and validated on write).

Dry-run by default (the database is opened read-only); ``--apply`` writes
through the guarded read-write connection and then regenerates the trigger
cache. A fenced (quarantined) database is never opened: exit 3. A missing,
unreadable or busy database exits 2 with nothing written.
Idempotent: a repaired row normalizes to itself, so a re-run changes nothing.
Every row it would change (dry-run) or did change (``--apply``, after the
commit and the cache rebuild) is printed, one JSON line each with its old value, its new columns and the values
dropped, so the repair can be reviewed before it is applied and undone after.
The counts classify each row once (unchanged, cleaned, cli_mapped, all_dropped,
or skipped_changed when a write found the row changed since the read);
``cli_kept`` and ``spec_refused`` are cross-cuts of the changed rows. Exit 4:
the repairs were committed but the cache rebuild failed.
Run it after the schema migration that adds ``trigger_spec`` and ``lesson``,
from the main checkout.

    python scripts/backfill_tool_triggers.py            # dry-run, prints counts
    python scripts/backfill_tool_triggers.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


async def backfill(db, *, apply: bool, regenerate_cache: bool = True) -> Counter:
    """Classify (and with ``apply``, repair) every live row; return the counts."""
    from genesis.learning.procedural.operations import commands_spec
    from genesis.learning.procedural.tool_names import normalize_tool_trigger

    rows = await db.execute_fetchall(
        "SELECT id, tool_trigger, trigger_spec FROM procedural_memory "
        "WHERE deprecated = 0 AND lesson IS NULL AND tool_trigger IS NOT NULL"
    )
    counts: Counter = Counter(rows_scanned=len(rows))
    # Plan every row first, so an --apply holds the write lock only for the
    # short burst of updates below, never through the normalizing.
    plan = []
    for row_id, raw, spec in rows:
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            value = raw  # a bare string is one value
        items = value if isinstance(value, list) else [value]
        counts["values_seen"] += len(items)
        tools, commands, dropped = normalize_tool_trigger(items)
        update: dict[str, str | None] = {}
        marks: list[str] = []
        if commands:
            new_spec = None
            if spec is None:
                try:
                    new_spec, tools, _ = commands_spec(tools, commands)
                except ValueError:
                    marks.append("spec_refused")
            if new_spec is not None:
                update["trigger_spec"] = json.dumps(new_spec)
            else:  # kept as written, inert, as commands_spec does
                tools = [t for t in tools if t != "Bash"] + [c for c in commands if c not in tools]
                marks.append("cli_kept")
        if tools != value or not tools:  # a stored [] is a trigger to the promoter
            update["tool_trigger"] = json.dumps(tools) if tools else None
        if not update:
            counts["unchanged"] += 1
            continue
        kind = (
            "all_dropped" if not tools else "cli_mapped" if "trigger_spec" in update else "cleaned"
        )
        plan.append((row_id, raw, spec, update, kind, marks, dropped))
    records: list[str] = []
    for row_id, raw, spec, update, kind, marks, dropped in plan:
        if apply:
            sets = ", ".join(f"{column} = ?" for column in update)
            # Only if the row still holds what was read: a refine that landed in
            # between is kept, and counted instead of the repair.
            cursor = await db.execute(
                f"UPDATE procedural_memory SET {sets} WHERE id = ? AND tool_trigger IS ? "  # noqa: S608 (fixed column names)
                "AND trigger_spec IS ? AND lesson IS NULL AND deprecated = 0",
                (*update.values(), row_id, raw, spec),
            )
            if cursor.rowcount == 0:
                counts["skipped_changed"] += 1
                continue
        counts.update([kind, *marks])
        counts["values_dropped"] += len(dropped)
        records.append(
            json.dumps(
                {"id": row_id, "kind": kind, "from": raw, "to": update, "dropped": dropped},
                default=repr,  # a BLOB or other odd stored value is shown, never fatal
            )
        )
    if apply:
        await db.commit()
    # The cache is rebuilt before any record is printed: output that fails (a
    # closed pipe) can then never skip it or mask the exit status.
    if apply and regenerate_cache:
        from genesis.learning.procedural.trigger_cache import get_cache_path, regenerate

        try:
            counts["cache_entries"] = await regenerate(db)
        except Exception as exc:  # the repairs are committed; say so, and how to finish
            counts["cache_failed"] = 1
            _say(f"Repairs committed, but the trigger cache rebuild failed: {exc}")
        else:
            _say(f"Trigger cache regenerated: {get_cache_path()}")
    for record in records:  # under --apply, only rows the commit wrote
        _say(record)
    return counts


def _checkout() -> Path:
    """The checkout this script runs from, where its trigger cache is written."""
    return Path(__file__).resolve().parent.parent


def _say(line: str) -> None:
    """Print, never failing the run: a reader that closed the pipe loses output only."""
    with contextlib.suppress(BrokenPipeError, OSError):
        print(line, flush=True)


async def _main(db_path: Path, apply: bool, *, rebuild_cache: bool = False) -> int:
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
    if apply:
        from genesis.db.connection import connect_aiosqlite_rw
        from genesis.db.integrity import DatabaseIntegrityError

        try:  # admission is checked again before and after the open
            db = await connect_aiosqlite_rw(db_path, existing_only=True)
        except DatabaseIntegrityError as exc:  # the admission refusal
            print(f"{db_path} is fenced; not opened for writing: {exc}")
            return 3
        except Exception as exc:
            print(f"{db_path} not opened for writing: {exc}")
            return 2
    else:  # a dry run never writes: an encoded read-only URI (as_uri escapes ? and #)
        try:
            db = await aiosqlite.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
        except (sqlite3.Error, OSError) as exc:
            print(f"{db_path} not opened: {exc}")
            return 2
    try:
        db.row_factory = aiosqlite.Row  # the cache regeneration reads rows by name
        columns = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(procedural_memory)")}
        missing = sorted({"id", "tool_trigger", "trigger_spec", "lesson", "deprecated"} - columns)
        if missing:
            print(
                f"procedural_memory lacks {', '.join(missing)}: run after the schema migration "
                "(server start)."
            )
            return 1
        # The cache file is the running install's: rebuild it only from the
        # install's own database (this checkout's data/genesis.db, however the
        # path was given), into an explicitly redirected cache, or when asked
        # (--rebuild-cache, for an install whose live database lives elsewhere).
        from genesis.learning.procedural.trigger_cache import get_cache_path

        # This checkout is where the cache is written (trigger_cache resolves
        # its path from the module), so it is what decides ownership.
        checkout = _checkout()
        own = Path(db_path).resolve() == (checkout / "data" / "genesis.db").resolve()
        rebuild = own or rebuild_cache
        counts = await backfill(db, apply=apply, regenerate_cache=rebuild)
        if apply and not rebuild:
            _say(
                f"Cache not regenerated: {db_path} is not {checkout}/data/genesis.db. The cache "
                f"is {get_cache_path()}; the advisor reads the main checkout's, so run from "
                "there, or re-run with --rebuild-cache if this database is the live one."
            )
    except sqlite3.Error as exc:  # not a database, unreadable, or locked past the busy timeout
        # Raised before the commit, so an apply wrote nothing (close rolls it back).
        print(f"{db_path}: database unreadable or busy, nothing written: {exc}")
        return 2
    finally:
        try:
            await db.close()
        except sqlite3.Error as exc:  # after a commit the repairs stand; say so
            _say(f"{db_path}: closing the database failed: {exc}")
    _say(("APPLIED" if apply else "DRY-RUN") + ": " + json.dumps(dict(counts), sort_keys=True))
    return 4 if counts.get("cache_failed") else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true", help="write the repairs and regenerate the cache"
    )
    parser.add_argument("--db", type=Path, help="path to genesis.db (default: genesis_db_path())")
    parser.add_argument(
        "--rebuild-cache",
        action="store_true",
        help="rebuild the trigger cache from a database outside this checkout (a relocated live one)",
    )
    args = parser.parse_args()
    from genesis.env import genesis_db_path

    sys.exit(
        asyncio.run(
            _main(args.db or genesis_db_path(), args.apply, rebuild_cache=args.rebuild_cache)
        )
    )
