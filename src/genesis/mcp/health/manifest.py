"""bootstrap_manifest, subsystem_heartbeats, and job_health tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from genesis.db.connection import BUSY_TIMEOUT_MS
from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)

# Module-level DB path so tests can monkeypatch it without touching
# ``Path.home()``. Matches the pattern used in ``update_history.py``.
_DB_PATH = Path.home() / "genesis" / "data" / "genesis.db"


async def _impl_bootstrap_manifest() -> dict:
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        return {
            "bootstrapped": rt.is_bootstrapped,
            "manifest": rt.bootstrap_manifest,
        }
    except (ImportError, AttributeError, RuntimeError):
        # Narrow catch: runtime module genuinely unimportable, singleton
        # __init__ failed, or runtime attribute access failure. In the
        # common "standalone mode" path the runtime IS importable and
        # the happy path returns {"bootstrapped": False, "manifest": {}}
        # via the real singleton — this except branch only fires on a
        # real bug, which is why it logs at ERROR. A broader catch
        # would hide those bugs.
        logger.error(
            "bootstrap_manifest fallback fired — runtime unreachable",
            exc_info=True,
        )
        return {
            "status": "unavailable",
            "message": "Bootstrap manifest unavailable — runtime unreachable",
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
            except (aiosqlite.Error, ImportError, AttributeError, TimeoutError):
                # Narrow catch: DB query failure (aiosqlite.Error,
                # TimeoutError), broken import chain for events_crud
                # (ImportError), or malformed row shape on access
                # (AttributeError). Log at ERROR per observability
                # rules — a heartbeat probe failure against a wired
                # service is an operational failure, not tracing
                # noise. Unexpected exception types bubble up on
                # purpose: we'd rather see ``is_error=True`` on the
                # FastMCP result than silently drop a real bug.
                logger.error(
                    "Heartbeat timestamp query failed for subsystem %s",
                    name, exc_info=True,
                )

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
    """Return scheduled-job health under a single normalized envelope.

    Contract (all four return paths share this shape)::

        {
            "jobs": {job_name: {...}, ...},
            "note": None | str,
            "source": "runtime" | "sqlite" | "missing_db" | "query_failed",
        }

    Callers can always read ``result["jobs"]`` and ``result["source"]``
    without branching on shape. A non-None ``note`` is a human-readable
    explanation of a degraded state (missing DB, query failure).
    """
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        if rt.job_health:
            return {
                "jobs": rt.job_health,
                "note": None,
                "source": "runtime",
            }
    except (ImportError, AttributeError, RuntimeError):
        # Probe of the runtime singleton. This is expected to miss in
        # standalone mode — we fall through to the sqlite fallback —
        # so DEBUG is the right level here. Narrow catch still lets
        # real bugs surface.
        logger.debug("Runtime job_health unavailable", exc_info=True)

    if not _DB_PATH.exists():
        return {
            "jobs": {},
            "note": (
                f"Genesis database not found at {_DB_PATH}; "
                "no job health data available."
            ),
            "source": "missing_db",
        }

    try:
        async with aiosqlite.connect(str(_DB_PATH)) as db:
            await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor = await db.execute(
                "SELECT job_name, last_run, last_success, last_failure, "
                "last_error, consecutive_failures FROM job_health"
            )
            jobs: dict[str, dict] = {}
            for row in await cursor.fetchall():
                jobs[row[0]] = {
                    "last_run": row[1],
                    "last_success": row[2],
                    "last_failure": row[3],
                    "last_error": row[4],
                    "consecutive_failures": row[5],
                }
            return {"jobs": jobs, "note": None, "source": "sqlite"}
    except (aiosqlite.Error, OSError):
        # aiosqlite.Error covers DB-level failures (corrupt file, busy
        # timeout, schema mismatch). OSError covers filesystem issues
        # between the exists() check and connect() (race, permission).
        # Log at ERROR — a DB probe failure is the kind of operational
        # failure the rules say to surface. Return a structured envelope
        # with an explicit ``source`` so callers can distinguish "no
        # jobs" from "check failed".
        logger.error(
            "job_health sqlite fallback failed at %s",
            _DB_PATH, exc_info=True,
        )
        return {
            "jobs": {},
            "note": "job_health check failed — see logs for details.",
            "source": "query_failed",
        }


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
    """Scheduled job health: last run, last success, consecutive failures per job.

    Returns a normalized envelope::

        {
          "jobs": {job_name: {last_run, last_success, last_failure,
                              last_error, consecutive_failures}},
          "note": null | "human-readable explanation",
          "source": "runtime" | "sqlite" | "missing_db" | "query_failed"
        }

    ``note`` is null on the happy path; non-null when the check
    degraded (missing DB or query failure). ``source`` identifies
    which path produced the result.
    """
    return await _impl_job_health()
