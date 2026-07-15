"""Runner for data migrations (WS-C) — see ``__init__`` for the contract.

Runs POST-boot as a background ``tracked_task`` (never in the critical boot
path; a data-migration failure must not abort startup — the exact failure mode
this framework exists to replace). Serialized: one migration at a time, in id
order. Interruption-safe: idempotency covers a crash mid-run, and an orphaned
``running`` row is reset to ``pending`` on the next boot.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import re
from pathlib import Path

import aiosqlite

from genesis.db._migration_discovery import discover_numbered_modules
from genesis.db.crud import data_migrations as crud

logger = logging.getLogger(__name__)

_DATA_MIGRATIONS_DIR = Path(__file__).parent
_DATA_MIGRATION_PATTERN = re.compile(r"^(d\d{4})_\w+\.py$")


class DataMigrationRunner:
    """Discovers and runs pending data migrations against the shared DB."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    def _discover(self) -> list[tuple[str, str, Path]]:
        return discover_numbered_modules(_DATA_MIGRATIONS_DIR, _DATA_MIGRATION_PATTERN)

    async def run_pending(self) -> list[dict]:
        """Run every claimable data migration once. Returns per-migration outcomes.

        Best-effort: a single migration's failure is recorded to the ledger and
        the runner moves on — it never raises into the caller (the background
        task) and never blocks the others."""
        available = self._discover()

        # Pre-flight: two files sharing a dNNNN prefix would collide on the
        # ledger PRIMARY KEY. Unlike the schema runner (whose plain INSERT
        # surfaces the clash as a UNIQUE error mid-run), our ensure_row uses
        # INSERT OR IGNORE, so a collision would SILENTLY drop the second
        # migration forever (its id is marked completed by the first). Catch it
        # loudly here instead. Mirrors db/migrations/runner.py's guard.
        seen: dict[str, str] = {}
        for mid, name, _path in available:
            if mid in seen:
                raise RuntimeError(
                    f"Duplicate data-migration prefix '{mid}': '{seen[mid]}' and "
                    f"'{name}'. Rename one to the next free prefix."
                )
            seen[mid] = name

        # Register any new migrations, then re-dispatch orphaned 'running' rows
        # (crash mid-run) before claiming — both idempotent.
        for mid, name, path in available:
            requires_operator = _module_requires_operator(path)
            await crud.ensure_row(self._db, id=mid, name=name, requires_operator=requires_operator)
        reset = await crud.reset_running_to_pending(self._db)
        if reset:
            logger.info("Re-dispatched %d orphaned data-migration(s) to pending", reset)

        outcomes: list[dict] = []
        for mid, name, path in available:
            status = await crud.get_status(self._db, mid)
            if status in ("completed", "operator_pending"):
                # Already done, or deliberately operator-gated — never auto-run.
                continue
            # Atomic claim: only the winner proceeds (server/bridge double-run guard).
            if not await crud.claim(self._db, mid):
                continue
            outcomes.append(await self._run_one(mid, name, path))
        return outcomes

    async def _run_one(self, mid: str, name: str, path: Path) -> dict:
        try:
            mod = importlib.import_module(f"genesis.db.data_migrations.{path.stem}")
            if not hasattr(mod, "migrate") or not hasattr(mod, "verify"):
                raise AttributeError(f"Data migration {name} missing migrate()/verify()")

            # migrate() and verify() are SYNC (they open their own sqlite/qdrant
            # connections and do blocking I/O) — run off the event loop so a long
            # backfill never stalls the loop the awareness/reflection ticks share.
            summary = await asyncio.to_thread(mod.migrate)
            verified = await asyncio.to_thread(mod.verify)
            if not verified:
                await crud.mark_failed(
                    self._db, mid, error="verify() returned False after migrate()"
                )
                logger.error("Data migration %s did not verify — marked failed", name)
                return {"id": mid, "name": name, "success": False, "error": "verify failed"}

            summary_str = str(summary) if summary is not None else ""
            await crud.mark_completed(self._db, mid, summary=summary_str)
            logger.info("Data migration %s completed: %s", name, summary_str)
            return {"id": mid, "name": name, "success": True, "summary": summary}
        except Exception as exc:  # noqa: BLE001 — record + continue; never abort the batch
            await crud.mark_failed(self._db, mid, error=repr(exc))
            logger.error("Data migration %s failed: %s", name, exc, exc_info=True)
            return {"id": mid, "name": name, "success": False, "error": str(exc)}


def _module_requires_operator(path: Path) -> bool:
    """Read a migration's ``requires_operator`` flag without running it.

    Imported (not exec'd) so the flag is a plain module constant; defaults to
    False when absent."""
    try:
        mod = importlib.import_module(f"genesis.db.data_migrations.{path.stem}")
        return bool(getattr(mod, "requires_operator", False))
    except Exception:
        logger.warning("Could not read requires_operator for %s — treating as auto", path.stem)
        return False


async def run_data_migrations(db: aiosqlite.Connection) -> list[dict]:
    """Module entry point for the post-boot kick. Never raises."""
    try:
        return await DataMigrationRunner(db).run_pending()
    except Exception:
        logger.error("Data-migration runner failed", exc_info=True)
        return []
