"""StandaloneHealthDataService — health data from status.json for standalone MCP.

When the health MCP runs as a stdio subprocess (via .mcp.json), it cannot
access live GenesisRuntime objects (CircuitBreakerRegistry, CostTracker, etc.).
This service reads ~/.genesis/status.json instead, providing a slightly stale
but functional view of system health.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class StandaloneHealthDataService:
    """Reads health data from status.json file instead of live runtime objects.

    Compatible with health_mcp's _service interface (has snapshot() method).
    """

    def __init__(
        self,
        *,
        status_path: Path | str,
        db=None,
    ) -> None:
        self._status_path = Path(status_path)
        self._db = db
        # These are None in standalone — health_mcp code already handles None
        self._breakers = None
        self._routing_config = None
        self._dead_letter = None

    async def snapshot(self) -> dict:
        """Read status.json and return health snapshot.

        Returns a dict compatible with HealthDataService.snapshot() output,
        adapted from the status file's simpler format.
        """
        if not self._status_path.exists():
            return {
                "status": "unavailable",
                "message": f"Status file not found: {self._status_path}",
            }

        try:
            raw = self._status_path.read_text()
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            return {
                "status": "unavailable",
                "message": f"Failed to parse status JSON: {e}",
            }
        except OSError as e:
            return {
                "status": "unavailable",
                "message": f"Failed to read status file: {e}",
            }

        # Map status.json format to HealthDataService.snapshot() format
        result = {
            "timestamp": data.get("timestamp"),
            "resilience_state": data.get("resilience_state", {}),
            "queue_depths": data.get("queue_depths", {}),
            "human_summary": data.get("human_summary", ""),
            "last_recovery": data.get("last_recovery"),
            "call_sites": {},
            "cc_sessions": {},
            "infrastructure": {},
            "queues": data.get("queue_depths", {}),
            "cost": {"daily_usd": None, "monthly_usd": None, "budget_status": "unknown"},
            "surplus": {},
            "awareness": {},
            "outreach_stats": {},
        }

        # Enrich from DB where possible (capped at 5s to avoid stalling CC)
        if self._db:
            try:
                await asyncio.wait_for(self._enrich_from_db(result), timeout=5.0)
            except TimeoutError:
                logger.warning("Standalone health enrichment timed out after 5s", exc_info=True)
            except Exception:
                logger.warning("Standalone health enrichment failed", exc_info=True)

        # Add live service detection (works in standalone — calls systemctl --user)
        try:
            from genesis.observability.service_status import collect_service_status

            result["services"] = collect_service_status()
        except Exception:
            logger.debug("Service status collection failed", exc_info=True)
            result["services"] = {}

        # MCP server crash status — surface failed-to-start servers
        result["mcp_servers"] = _load_mcp_crash_status()

        return result

    async def _enrich_from_db(self, result: dict) -> None:
        """Enrich snapshot with real data from the shared DB."""
        # CC sessions — real counts, costs, durations from cc_sessions table
        try:
            from genesis.observability.snapshots.cc_sessions import (
                cc_sessions as cc_sessions_snap,
            )

            cc_data = await cc_sessions_snap(self._db, None, None)
            result["cc_sessions"] = cc_data
            # Wire shadow costs into the cost field
            if isinstance(cc_data, dict):
                result["cost"] = {
                    "daily_usd": cc_data.get("shadow_cost_today"),
                    "monthly_usd": cc_data.get("shadow_cost_month"),
                    "budget_status": "unknown",
                }
        except Exception:
            logger.warning("CC sessions snapshot failed in standalone mode", exc_info=True)

        # Call sites — last activity per call site from call_site_last_run table
        try:
            cursor = await self._db.execute(
                "SELECT call_site_id, last_run_at, provider_used, model_id, "
                "input_tokens, output_tokens FROM call_site_last_run"
            )
            sites = {}
            now = datetime.now(UTC)
            for row in await cursor.fetchall():
                last_run = row["last_run_at"]
                status = "unknown"
                if last_run:
                    try:
                        run_dt = datetime.fromisoformat(last_run)
                        if run_dt.tzinfo is None:
                            run_dt = run_dt.replace(tzinfo=UTC)
                        age = now - run_dt
                        if age < timedelta(hours=1):
                            status = "active"
                        elif age < timedelta(hours=24):
                            status = "idle"
                        else:
                            status = "stale"
                    except (ValueError, TypeError):
                        status = "unknown"
                sites[row["call_site_id"]] = {
                    "status": status,
                    "last_run_at": last_run,
                    "last_provider": row["provider_used"],
                    "last_model": row["model_id"],
                    "last_tokens": (row["input_tokens"] or 0) + (row["output_tokens"] or 0),
                }
            if sites:
                result["call_sites"] = sites
        except Exception:
            logger.warning("Call sites query failed in standalone mode", exc_info=True)

        # Infrastructure — disk, tmpfs, container memory, Qdrant, Ollama, DB health
        try:
            from genesis.observability.snapshots.infrastructure import (
                infrastructure as infra_snap,
            )

            result["infrastructure"] = await infra_snap(self._db, None, None, None)
        except Exception:
            logger.warning("Infrastructure snapshot failed in standalone mode", exc_info=True)

        # Awareness — tick counts, depth distribution, scoring
        try:
            from genesis.observability.snapshots.awareness import (
                awareness as awareness_snap,
            )

            result["awareness"] = await awareness_snap(self._db)
        except Exception:
            logger.warning("Awareness snapshot failed in standalone mode", exc_info=True)

        # Surplus — queue depth, recent tasks, executor status
        try:
            from genesis.observability.snapshots.surplus import surplus_status

            result["surplus"] = await surplus_status(self._db, None)
        except Exception:
            logger.warning("Surplus snapshot failed in standalone mode", exc_info=True)

        # Outreach — message counts, delivery stats
        try:
            from genesis.observability.snapshots.outreach import outreach_stats

            result["outreach_stats"] = await outreach_stats(self._db)
        except Exception:
            logger.warning("Outreach snapshot failed in standalone mode", exc_info=True)


_MCP_CRASH_DIR = Path.home() / ".genesis" / "mcp_crashes"
_EXPECTED_SERVERS = ("health", "memory", "outreach", "recon")


def _load_mcp_crash_status() -> dict:
    """Load MCP server crash status from per-server crash files.

    Returns a dict of server_name → {status, error?, crashed_at?} for all
    expected servers. Servers without crash files are reported as "up".
    """
    servers: dict[str, dict] = {}

    for name in _EXPECTED_SERVERS:
        crash_file = _MCP_CRASH_DIR / f"{name}.json"
        if crash_file.exists():
            try:
                info = json.loads(crash_file.read_text())
                servers[name] = {
                    "status": "crashed",
                    "error": info.get("error", "unknown"),
                    "crashed_at": info.get("timestamp", ""),
                }
            except (json.JSONDecodeError, OSError):
                servers[name] = {"status": "crashed", "error": "unreadable crash file"}
        else:
            servers[name] = {"status": "up"}

    return servers
