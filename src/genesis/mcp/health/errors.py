"""health_errors and health_alerts tools."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)


async def _impl_health_errors(
    window_minutes: int = 60,
    pattern_group: bool = False,
) -> list[dict]:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    _event_bus = health_mcp_mod._event_bus

    if _service is None:
        return [{"error": "HealthDataService not initialized"}]

    errors: list[dict] = []

    if _service._dead_letter and _service._db:
        try:
            from datetime import timedelta

            from genesis.db.crud import dead_letter as dl_crud

            cutoff = (datetime.now(UTC) - timedelta(minutes=window_minutes)).isoformat()
            items = await dl_crud.query_pending(_service._db)
            for item in items:
                if item.get("created_at", "") >= cutoff:
                    errors.append({
                        "type": "dead_letter",
                        "provider": item.get("target_provider", "unknown"),
                        "reason": item.get("failure_reason", ""),
                        "operation": item.get("operation_type", ""),
                        "timestamp": item.get("created_at", ""),
                    })
        except Exception:
            logger.warning("Failed to query dead letter errors", exc_info=True)

    if _service._breakers and _service._routing_config:
        from genesis.routing.types import ProviderState

        for name in _service._routing_config.providers:
            try:
                cb = _service._breakers.get(name)
                if cb.state == ProviderState.OPEN:
                    errors.append({
                        "type": "circuit_breaker_open",
                        "provider": name,
                        "reason": "Circuit breaker tripped",
                        "failures": cb.consecutive_failures,
                    })
            except Exception:
                logger.debug("Dead letter timestamp parse failed", exc_info=True)

    from datetime import timedelta as _td

    cutoff = (datetime.now(UTC) - _td(minutes=window_minutes)).isoformat()

    db_events_loaded = False
    if _service and _service._db:
        try:
            from genesis.db.crud import events as events_crud

            db_rows = await events_crud.query(
                _service._db,
                severity="WARNING",
                since=cutoff,
                limit=50,
            )
            for sev in ("ERROR", "CRITICAL"):
                db_rows.extend(await events_crud.query(
                    _service._db,
                    severity=sev,
                    since=cutoff,
                    limit=50,
                ))
            for row in db_rows:
                errors.append({
                    "type": "event_bus",
                    "subsystem": row.get("subsystem", ""),
                    "event_type": row.get("event_type", ""),
                    "severity": row.get("severity", ""),
                    "message": row.get("message", ""),
                    "timestamp": row.get("timestamp", ""),
                })
            db_events_loaded = True
        except Exception:
            logger.debug("Event log query failed", exc_info=True)

    if not db_events_loaded and _event_bus and hasattr(_event_bus, "recent_events"):
        from genesis.observability.types import Severity

        for event in _event_bus.recent_events(min_severity=Severity.WARNING, limit=50):
            if event.timestamp >= cutoff:
                errors.append({
                    "type": "event_bus",
                    "subsystem": event.subsystem.value,
                    "event_type": event.event_type,
                    "severity": event.severity.value,
                    "message": event.message,
                    "timestamp": event.timestamp,
                })

    if pattern_group and errors:
        grouped: dict[str, dict] = {}
        for e in errors:
            key = f"{e.get('provider', e.get('subsystem', ''))}:{e.get('type', '')}:{e.get('event_type', '')}"
            if key not in grouped:
                grouped[key] = {**e, "count": 1}
            else:
                grouped[key]["count"] += 1
        return list(grouped.values())

    return errors


async def _impl_health_alerts(active_only: bool = True) -> list[dict]:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    _activity_tracker = health_mcp_mod._activity_tracker
    _job_retry_registry = health_mcp_mod._job_retry_registry
    _alert_history = health_mcp_mod._alert_history

    if _service is None:
        return [{"severity": "CRITICAL", "message": "HealthDataService not initialized"}]

    snap = await _service.snapshot()
    alerts: list[dict] = []
    current_ids: set[str] = set()

    for site_id, site_info in snap.get("call_sites", {}).items():
        status_val = site_info.get("status", "unknown")
        alert_id = f"call_site:{site_id}"

        if status_val == "down":
            alerts.append({
                "id": alert_id,
                "severity": "CRITICAL",
                "message": f"Call site {site_id} is DOWN (all providers exhausted)",
            })
            current_ids.add(alert_id)
        elif status_val == "degraded":
            alerts.append({
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Call site {site_id} is degraded (using fallback provider)",
                "active_provider": site_info.get("active_provider"),
            })
            current_ids.add(alert_id)

    queues = snap.get("queues", {})
    for queue_name, depth in queues.items():
        if isinstance(depth, int) and depth > 100:
            alert_id = f"queue:{queue_name}"
            alerts.append({
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Queue {queue_name} depth is {depth} (>100)",
            })
            current_ids.add(alert_id)

    cc = snap.get("cc_sessions", {})
    bg = cc.get("background", {})
    if bg.get("status") in ("throttled", "rate_limited"):
        alert_id = "cc:budget"
        alerts.append({
            "id": alert_id,
            "severity": "WARNING",
            "message": f"CC sessions {bg['status']} (budget: {bg.get('hourly_budget', '?')})",
        })
        current_ids.add(alert_id)

    cc_realtime = cc.get("realtime_status")
    if cc_realtime in ("UNAVAILABLE", "RATE_LIMITED"):
        alert_id = "cc:quota_exhausted"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": f"CC {cc_realtime.lower().replace('_', ' ')} — contingency mode active",
        })
        current_ids.add(alert_id)

    awareness = snap.get("awareness", {})
    tick_age = awareness.get("time_since_last_tick_seconds")
    if tick_age is not None and tick_age > 360:
        alert_id = "awareness:tick_overdue"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": f"Awareness tick overdue by {int(tick_age)}s (>360s threshold)",
        })
        current_ids.add(alert_id)

    dl_age = snap.get("queues", {}).get("dead_letter_oldest_age_seconds")
    if dl_age is not None and dl_age > 3600:
        alert_id = "queue:stale_dead_letters"
        alerts.append({
            "id": alert_id,
            "severity": "WARNING",
            "message": f"Dead letter queue has items {int(dl_age)}s old (>1h threshold)",
        })
        current_ids.add(alert_id)

    disk = snap.get("infrastructure", {}).get("disk", {})
    free_pct = disk.get("free_pct")
    if free_pct is not None and free_pct < 10:
        alert_id = "infra:disk_low"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL" if free_pct < 10 else "WARNING",
            "message": f"Disk space low: {free_pct}% free ({disk.get('free_gb', '?')}GB)",
        })
        current_ids.add(alert_id)

    tmpfs = snap.get("infrastructure", {}).get("tmpfs", {})
    tmp_free_pct = tmpfs.get("free_pct")
    if tmp_free_pct is not None and tmp_free_pct < 20:
        alert_id = "infra:tmpfs_low"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL" if tmp_free_pct < 10 else "WARNING",
            "message": f"/tmp tmpfs low: {tmp_free_pct}% free ({tmpfs.get('free_mb', '?')}MB) — filling /tmp kills CC sessions",
        })
        current_ids.add(alert_id)

    container_mem = snap.get("infrastructure", {}).get("container_memory", {})
    used_pct = container_mem.get("used_pct", 0)
    if used_pct > 85:
        alert_id = "infra:container_memory_high"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL" if used_pct > 90 else "WARNING",
            "message": f"Container memory at {used_pct}% ({container_mem.get('current_gb', '?')}/{container_mem.get('limit_gb', '?')}GB)",
        })
        current_ids.add(alert_id)

    qdrant_cols = snap.get("infrastructure", {}).get("qdrant_collections", {})
    missing_cols = qdrant_cols.get("missing", [])
    if missing_cols:
        alert_id = "infra:qdrant_collections_missing"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": f"Qdrant collections missing: {', '.join(missing_cols)} — memory operations will fail",
        })
        current_ids.add(alert_id)

    services = snap.get("services", {})
    genesis_svc = services.get("bridge", {})  # key is "bridge" for backward compat
    if genesis_svc.get("active_state") not in ("active", "unknown"):
        svc_label = genesis_svc.get("service_unit", "genesis-server.service")
        alert_id = "service:genesis_down"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": f"{svc_label} is {genesis_svc.get('active_state', 'unknown')}",
        })
        current_ids.add(alert_id)

    watchdog_timer = services.get("watchdog_timer", {})
    if watchdog_timer.get("active_state") not in ("active", "unknown"):
        alert_id = "service:watchdog_blind"
        alerts.append({
            "id": alert_id,
            "severity": "WARNING",
            "message": "genesis-watchdog.timer is inactive — infrastructure monitoring is blind",
        })
        current_ids.add(alert_id)

    watchdog_state = services.get("watchdog", {})
    wd_failures = watchdog_state.get("consecutive_failures", 0)
    if wd_failures > 3:
        alert_id = "service:watchdog_failing"
        alerts.append({
            "id": alert_id,
            "severity": "WARNING",
            "message": f"Watchdog has {wd_failures} consecutive failures (reason: {watchdog_state.get('last_reason', 'unknown')})",
        })
        current_ids.add(alert_id)

    ollama = snap.get("infrastructure", {}).get("ollama", {})
    missing_models = ollama.get("missing_models", [])
    if missing_models:
        alert_id = "infra:ollama_model_mismatch"
        names = ", ".join(f"{m['provider']}:{m['model']}" for m in missing_models)
        alerts.append({
            "id": alert_id,
            "severity": "WARNING",
            "message": f"Ollama missing configured models: {names}",
        })
        current_ids.add(alert_id)

    if _activity_tracker is not None:
        emb_summary = _activity_tracker.summary("episodic_memory_embedding")
        if (
            isinstance(emb_summary, dict)
            and emb_summary.get("calls", 0) > 0
            and emb_summary.get("error_rate", 0) > 0.5
        ):
            alert_id = "provider:embedding_failing"
            alerts.append({
                "id": alert_id,
                "severity": "CRITICAL",
                "message": (
                    f"Embedding provider error rate: {emb_summary['error_rate']:.0%} "
                    f"({emb_summary['errors']}/{emb_summary['calls']} calls failed)"
                ),
            })
            current_ids.add(alert_id)

        qdrant_summary = _activity_tracker.summary("qdrant.search")
        if (
            isinstance(qdrant_summary, dict)
            and qdrant_summary.get("calls", 0) > 0
            and qdrant_summary.get("error_rate", 0) == 1.0
        ):
            alert_id = "provider:qdrant_unreachable"
            alerts.append({
                "id": alert_id,
                "severity": "CRITICAL",
                "message": (
                    f"Qdrant search 100% failure rate "
                    f"({qdrant_summary['errors']} consecutive failures)"
                ),
            })
            current_ids.add(alert_id)

    if _job_retry_registry is not None:
        for job_name in _job_retry_registry.list_registered():
            if _job_retry_registry.is_quarantined(job_name):
                alert_id = f"job:quarantined:{job_name}"
                alerts.append({
                    "id": alert_id,
                    "severity": "WARNING",
                    "message": f"Job {job_name} is quarantined (max retries exhausted, auto-unquarantine in ≤24h)",
                })
                current_ids.add(alert_id)

    now = datetime.now(UTC).isoformat()
    for old_id in list(_alert_history.keys()):
        if old_id in current_ids:
            del _alert_history[old_id]
    for alert in alerts:
        aid = alert.get("id", "")
        if aid in _alert_history:
            del _alert_history[aid]

    if not active_only:
        for resolved_id, resolved_at in _alert_history.items():
            alerts.append({
                "id": resolved_id,
                "severity": "RESOLVED",
                "message": f"Previously active alert resolved at {resolved_at}",
            })

    health_mcp_mod._alert_history = {aid: now for aid in current_ids}

    return alerts


@mcp.tool()
async def health_errors(
    window_minutes: int = 60,
    pattern_group: bool = False,
) -> list[dict]:
    """Recent errors from dead-letter queue and circuit breaker failures."""
    return await _impl_health_errors(window_minutes, pattern_group)


@mcp.tool()
async def health_alerts(
    active_only: bool = True,
) -> list[dict]:
    """Active alerts: call sites down/degraded, resilience warnings, queue depth."""
    return await _impl_health_alerts(active_only)
