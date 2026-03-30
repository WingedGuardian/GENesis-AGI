"""Init functions: _init_db, _init_tool_registry."""

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize the SQLite database."""
    try:
        from genesis.db.connection import init_db

        rt._db = await init_db()
        logger.info("Genesis DB initialized")
    except sqlite3.Error:
        logger.exception("DB error during initialization")
    except Exception:
        logger.exception("Failed to initialize Genesis DB")


async def init_tool_registry(rt: GenesisRuntime) -> None:
    """Bootstrap the tool registry."""
    from genesis.util.tool_bootstrap import bootstrap_tool_registry

    await bootstrap_tool_registry(rt._db)
