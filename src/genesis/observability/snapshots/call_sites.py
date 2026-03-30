"""Call sites snapshot — circuit breaker health per routing call site."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from genesis.observability._call_site_meta import _CALL_SITE_META
from genesis.routing.types import ProviderState

if TYPE_CHECKING:
    import aiosqlite

    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.types import RoutingConfig

logger = logging.getLogger(__name__)


async def call_sites(
    db: aiosqlite.Connection | None,
    routing_config: RoutingConfig | None,
    breakers: CircuitBreakerRegistry | None,
) -> dict:
    if not routing_config or not breakers:
        return {}

    result = {}
    for site_id, site_cfg in routing_config.call_sites.items():
        chain_health = []
        first_closed = None
        all_open = True

        for provider_name in site_cfg.chain:
            try:
                cb = breakers.get(provider_name)
                state = cb.state
                failures = cb.consecutive_failures
                trips = cb.trip_count
            except (AttributeError, TypeError):
                state = "error"
                failures = -1
                trips = 0
            except Exception:
                logger.debug("CB access failed for %s", provider_name, exc_info=True)
                state = "error"
                failures = -1
                trips = 0

            entry: dict = {
                "provider": provider_name,
                "state": str(state),
                "failures": failures,
            }
            if trips > 0:
                entry["trip_count"] = trips
            chain_health.append(entry)

            if state not in (ProviderState.OPEN, ProviderState.HALF_OPEN):
                all_open = False
                if first_closed is None:
                    first_closed = provider_name

        if not site_cfg.chain:
            status = "unknown"
        elif all_open:
            status = "down"
        else:
            first_provider = site_cfg.chain[0]
            try:
                first_state = breakers.get(first_provider).state
            except (KeyError, AttributeError):
                first_state = ProviderState.CLOSED
            status = "healthy" if first_state not in (ProviderState.OPEN, ProviderState.HALF_OPEN) else "degraded"

        recent_failures = 0
        last_failure_at: str | None = None
        if db and status in ("healthy", "degraded"):
            try:
                cutoff = (datetime.now(UTC) - timedelta(minutes=30)).isoformat()
                async with db.execute(
                    "SELECT COUNT(*), MAX(created_at) FROM events "
                    "WHERE event_type IN "
                    "('all_exhausted', 'provider.fallback', 'breaker.tripped') "
                    "AND message LIKE ? "
                    "AND created_at >= ?",
                    (f"%{site_id}%", cutoff),
                ) as cursor:
                    row = await cursor.fetchone()
                    if row and row[0] > 0:
                        recent_failures = row[0]
                        last_failure_at = row[1]
                        if status == "healthy" and last_failure_at:
                            status = "warning"
            except sqlite3.Error:
                logger.debug("DB query failed for failure check on %s", site_id, exc_info=True)

        site_data: dict = {
            "status": status,
            "active_provider": first_closed,
            "chain_health": chain_health,
        }
        if recent_failures > 0:
            site_data["recent_failures"] = recent_failures
            site_data["last_failure_at"] = last_failure_at
        meta = _CALL_SITE_META.get(site_id)
        if meta:
            site_data.update(meta)
        result[site_id] = site_data

    if db:
        try:
            cursor = await db.execute(
                "SELECT call_site_id, last_run_at, provider_used, model_id, response_text, input_tokens, output_tokens FROM call_site_last_run"
            )
            for row in await cursor.fetchall():
                sid = row[0]
                if sid in result:
                    result[sid]["last_run_at"] = row[1]
                    result[sid]["last_run_provider"] = row[2]
                    result[sid]["last_run_model"] = row[3]
                    result[sid]["last_response"] = row[4]
                    result[sid]["last_run_tokens"] = (row[5] or 0) + (row[6] or 0)
                else:
                    result[sid] = {
                        "status": "active",
                        "routing": False,
                        "last_run_at": row[1],
                        "last_run_provider": row[2],
                        "last_run_model": row[3],
                        "last_response": row[4],
                        "last_run_tokens": (row[5] or 0) + (row[6] or 0),
                    }
        except sqlite3.Error:
            logger.debug("call_site_last_run query failed", exc_info=True)

    return result
