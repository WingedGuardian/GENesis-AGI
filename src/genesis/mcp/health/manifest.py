"""bootstrap_manifest, subsystem_heartbeats, and job_health tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)


async def _impl_bootstrap_manifest() -> dict:
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        return {
            "bootstrapped": rt.is_bootstrapped,
            "manifest": rt.bootstrap_manifest,
        }
    except Exception:
        logger.debug("bootstrap_manifest unavailable (standalone mode)", exc_info=True)
        return {
            "status": "unavailable",
            "message": "Bootstrap manifest unavailable in standalone mode",
        }


async def _impl_subsystem_heartbeats() -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    _event_bus = health_mcp_mod._event_bus

    # (expected_interval_s, overdue_threshold_s)
    expected = {
        "awareness": (300, 360),      # 5 min tick, error at 6 min
        "surplus": (300, 600),
        "inbox": (1800, 3600),
        "reflection": (600, 1200),
        "outreach": (86400, 172800),
        "dashboard": (120, 240),
    }

    result = {}
    now = datetime.now(UTC)

    for name, (_interval_s, overdue_s) in expected.items():
        last_ts = None

        if _service and _service._db:
            try:
                from genesis.db.crud import events as events_crud

                rows = await events_crud.query(
                    _service._db,
                    subsystem=name,
                    event_type="heartbeat",
                    limit=1,
                )
                if rows:
                    last_ts = rows[0].get("timestamp")
            except Exception:
                logger.debug("Heartbeat timestamp query failed", exc_info=True)

        if not last_ts and _event_bus and hasattr(_event_bus, "_ring"):
            for event in reversed(_event_bus._ring):
                sub_val = event.subsystem.value if hasattr(event.subsystem, "value") else str(event.subsystem)
                if sub_val == name and event.event_type == "heartbeat":
                    last_ts = event.timestamp
                    break

        if last_ts is None:
            result[name] = {"status": "no_heartbeat", "last_seen": None}
        else:
            try:
                age_s = (now - datetime.fromisoformat(last_ts)).total_seconds()
                overdue = age_s > overdue_s
                result[name] = {
                    "status": "overdue" if overdue else "alive",
                    "last_seen": last_ts,
                    "age_seconds": round(age_s, 1),
                }
            except (ValueError, TypeError):
                result[name] = {"status": "unknown", "last_seen": last_ts}

    return result


async def _impl_job_health() -> dict:
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt.job_health:
            return rt.job_health
    except Exception:
        logger.debug("Runtime job_health unavailable", exc_info=True)

    try:
        from pathlib import Path

        import aiosqlite

        db_path = Path.home() / "genesis" / "data" / "genesis.db"
        if not db_path.exists():
            return {}
        async with aiosqlite.connect(str(db_path)) as db:
            from genesis.db.connection import BUSY_TIMEOUT_MS

            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor = await db.execute(
                "SELECT job_name, last_run, last_success, last_failure, "
                "last_error, consecutive_failures FROM job_health"
            )
            result = {}
            for row in await cursor.fetchall():
                result[row[0]] = {
                    "last_run": row[1],
                    "last_success": row[2],
                    "last_failure": row[3],
                    "last_error": row[4],
                    "consecutive_failures": row[5],
                }
            return result
    except Exception:
        logger.debug("job_health unavailable (standalone fallback failed)", exc_info=True)
        return {}


@mcp.tool()
async def bootstrap_manifest() -> dict:
    """Which subsystems initialized successfully, failed, or degraded at startup."""
    return await _impl_bootstrap_manifest()


@mcp.tool()
async def subsystem_heartbeats() -> dict:
    """Last heartbeat time for each background subsystem. Detects silent deaths."""
    return await _impl_subsystem_heartbeats()


@mcp.tool()
async def job_health() -> dict:
    """Scheduled job health: last run, last success, consecutive failures per job."""
    return await _impl_job_health()
