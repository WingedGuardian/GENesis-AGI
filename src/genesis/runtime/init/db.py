"""Init functions: _init_db, _init_tool_registry."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize the SQLite database and apply pending schema migrations."""
    try:
        from genesis.db.connection import init_db

        rt._db = await init_db()
        logger.info("Genesis DB initialized")
    except sqlite3.Error:
        logger.exception("DB error during initialization")
        return
    except Exception:
        logger.exception("Failed to initialize Genesis DB")
        return

    # Apply pending schema migrations before any other init step touches DB
    # data. runtime/_core._run_init_step_async runs init steps sequentially,
    # so no other coroutine holds rt._db here. SerializedConnection releases
    # its lock per-call, so the runner's BEGIN IMMEDIATE / COMMIT envelope
    # only stays atomic while this is the sole DB user. Do NOT move this
    # past the next init step.
    try:
        from genesis.db.migrations.runner import MigrationRunner

        runner = MigrationRunner(rt._db)
        results = await runner.run_pending()
        failed = [r for r in results if not r.success]
        if failed:
            for r in failed:
                logger.critical(
                    "Migration %s failed (%dms): %s",
                    r.name, r.duration_ms, r.error,
                )
            raise RuntimeError(
                f"{len(failed)} schema migration(s) failed at startup; "
                f"DB is in inconsistent state. See logs for details."
            )
        applied = [r for r in results if r.success]
        if applied:
            logger.info(
                "Applied %d schema migration(s) at startup: %s",
                len(applied), ", ".join(r.name for r in applied),
            )
    except Exception:
        try:
            await rt._db.close()
        except Exception:
            logger.exception("Failed to close DB after migration failure")
        rt._db = None
        raise


async def init_tool_registry(rt: GenesisRuntime) -> None:
    """Bootstrap the tool registry."""
    from genesis.util.tool_bootstrap import bootstrap_tool_registry

    await bootstrap_tool_registry(rt._db)
