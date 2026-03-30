"""Module state persistence — survives restarts via module_config table."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)


async def save_module_state(
    db: aiosqlite.Connection,
    module_name: str,
    *,
    enabled: bool | None = None,
    config_json: str | None = None,
) -> None:
    """Persist module enabled state and/or config to DB.

    Uses INSERT OR REPLACE with COALESCE to only update provided fields.
    """
    try:
        # Check if row exists
        cursor = await db.execute(
            "SELECT enabled, config_json FROM module_config WHERE module_name = ?",
            (module_name,),
        )
        existing = await cursor.fetchone()

        if existing is None:
            # Insert new row
            await db.execute(
                "INSERT INTO module_config (module_name, enabled, config_json, updated_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (
                    module_name,
                    int(enabled) if enabled is not None else 1,
                    config_json or "{}",
                ),
            )
        else:
            # Update only the fields that were provided
            if enabled is not None and config_json is not None:
                await db.execute(
                    "UPDATE module_config SET enabled = ?, config_json = ?, updated_at = datetime('now') "
                    "WHERE module_name = ?",
                    (int(enabled), config_json, module_name),
                )
            elif enabled is not None:
                await db.execute(
                    "UPDATE module_config SET enabled = ?, updated_at = datetime('now') "
                    "WHERE module_name = ?",
                    (int(enabled), module_name),
                )
            elif config_json is not None:
                await db.execute(
                    "UPDATE module_config SET config_json = ?, updated_at = datetime('now') "
                    "WHERE module_name = ?",
                    (config_json, module_name),
                )

        await db.commit()
    except Exception:
        logger.error("Failed to persist module state for %s", module_name, exc_info=True)


async def load_all_module_states(db: aiosqlite.Connection) -> dict[str, dict[str, Any]]:
    """Load all persisted module states from DB.

    Returns dict of module_name -> {"enabled": bool, "config": dict}.
    """
    result: dict[str, dict[str, Any]] = {}
    try:
        cursor = await db.execute(
            "SELECT module_name, enabled, config_json FROM module_config"
        )
        for row in await cursor.fetchall():
            name, enabled, config_str = row
            try:
                config = json.loads(config_str) if config_str else {}
            except (json.JSONDecodeError, TypeError):
                config = {}
            result[name] = {"enabled": bool(enabled), "config": config}
    except Exception:
        logger.error("Failed to load module states from DB", exc_info=True)
    return result
