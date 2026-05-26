"""health_errors and health_alerts tools."""

from __future__ import annotations

import json
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
            logger.error("Failed to query dead letter errors", exc_info=True)

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
                logger.error(
                    "Circuit breaker state check failed for provider %s",
                    name, exc_info=True,
                )

    from datetime import timedelta as _td

    cutoff = (datetime.now(UTC) - _td(minutes=window_minutes)).isoformat()

    db_events_loaded = False
    if _service and _service._db:
        try:
            from genesis.db.crud import events as events_crud

            db_rows = await events_crud.query(
                _service._db,
                severity="warning",
                since=cutoff,
                limit=50,
            )
            for sev in ("error", "critical"):
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
            logger.error("Event log query failed", exc_info=True)

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
        return [{"id": "service:health_data_uninitialized", "severity": "CRITICAL", "message": "HealthDataService not initialized"}]

    snap = await _service.snapshot()
    alerts: list[dict] = []
    current_ids: set[str] = set()

    # Import lazily to avoid circular imports in hook/test paths
    from genesis.observability._call_site_meta import _CALL_SITE_META

    for site_id, site_info in snap.get("call_sites", {}).items():
        status_val = site_info.get("status", "unknown")
        alert_id = f"call_site:{site_id}"

        # Skip groundwork call sites — config exists but no code invokes
        # the router with this call_site_id. These are not infrastructure
        # alerts; they're placeholders for future wiring.
        meta = _CALL_SITE_META.get(site_id, {})
        if meta.get("wired") is False:
            continue

        # Skip disabled sites — every provider in the chain is unconfigured
        # (no API key in this deployment). This is a config state, not an
        # outage. Surfacing it as a CRITICAL alert caused Sentinel spam.
        if status_val == "disabled":
            continue

        # Skip idle sites — config exists but no invocations recorded.
        # These are either groundwork sites not yet wired or sites whose
        # callers haven't fired yet.  Not an outage.
        if status_val == "idle":
            continue

        if status_val == "down":
            # Call site DOWN means all provider circuit breakers are open —
            # a transient provider-side condition (rate limits, API outages).
            # The Sentinel has no remediation path for external providers;
            # circuit breakers auto-reset. Emit WARNING (→ Tier 3, reflexes
            # only) instead of CRITICAL (→ Tier 2, wakes Sentinel).
            alerts.append({
                "id": alert_id,
                "severity": "WARNING",
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
    # Only check fields that represent actual queue depths — exclude
    # cumulative counters (embedded_total), timestamps, error messages, etc.
    _QUEUE_DEPTH_FIELDS = {
        "pending_embeddings", "dead_letters", "deferred_work",
        "deferred_processing", "deferred_stuck", "failed_embeddings",
        "discarded_count",
    }
    for queue_name, depth in queues.items():
        if queue_name not in _QUEUE_DEPTH_FIELDS:
            continue
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

    # CC rate-limit / unavailability alert.
    #
    # Two design constraints shape this block:
    #
    # 1. The `realtime_status` comes from the resilience state machine,
    #    which latches RATE_LIMITED on CCInvoker errors but has flapping
    #    protection that can suppress the auto-recovery transition. Net
    #    effect: the state machine can stay RATE_LIMITED for long periods
    #    even when background sessions are healthy and the hourly budget
    #    says otherwise. The background budget tracker (bg.status) is the
    #    source of truth for actual throughput state. Cross-check before
    #    emitting — if the budget tracker disagrees, the state machine is
    #    stale, suppress the alert.
    #
    # 2. Severity is WARNING, not CRITICAL. Rationale: the Sentinel is
    #    the only CRITICAL-alert responder, and the Sentinel's only tool
    #    is dispatching a CC session. If CC is genuinely unavailable, a
    #    diagnostic CC session cannot run. Waking the tool to fix the
    #    tool is a self-defeating loop. WARNING routes to Tier 3
    #    (reflexes only) per the classifier — the user still sees it on
    #    the dashboard and via health_alerts, but Sentinel doesn't wake.
    cc_realtime = cc.get("realtime_status")
    if cc_realtime in ("UNAVAILABLE", "RATE_LIMITED"):
        bg_status = bg.get("status", "unknown")
        if bg_status == "healthy":
            logger.debug(
                "Suppressing cc:quota_exhausted: realtime_status=%s but bg.status=healthy "
                "(state machine is stale — budget tracker disagrees)",
                cc_realtime,
            )
        else:
            alert_id = "cc:quota_exhausted"
            alerts.append({
                "id": alert_id,
                "severity": "WARNING",
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
    if free_pct is not None and free_pct < 15:
        alert_id = "infra:disk_low"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL" if free_pct < 10 else "WARNING",
            "message": f"Disk space low: {free_pct}% free ({disk.get('free_gb', '?')}GB)",
        })
        current_ids.add(alert_id)

    container_mem = snap.get("infrastructure", {}).get("container_memory", {})
    # Use anon_pct (non-reclaimable memory) for alerts, not used_pct
    # (total cgroup including reclaimable page cache).
    anon_pct = container_mem.get("anon_pct", container_mem.get("used_pct", 0))
    if anon_pct > 85:
        alert_id = "infra:container_memory_high"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL" if anon_pct > 90 else "WARNING",
            "message": f"Container memory at {anon_pct}% anon+kernel ({container_mem.get('current_gb', '?')}/{container_mem.get('limit_gb', '?')}GB total)",
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

    # Guardian heartbeat — the host-side safety net
    #
    # The container-side GuardianWatchdog already tries SSH restart on
    # heartbeat staleness, but it only escalates to Sentinel on the
    # SECOND stage (Guardian stuck in confirmed_dead after reset-state
    # fails). If the Guardian is heartbeat-stale AND SSH is unreachable
    # (host down, network broken, auth drift), the Sentinel never sees
    # the problem via the watchdog path.
    #
    # Emitting guardian:heartbeat_stale CRITICAL here closes that gap.
    # Part 7's per-pattern backoff + 2-of-3 debounce prevents this from
    # being spammy. The classifier treats this as Tier 1 (defense
    # mechanism failure) so the Sentinel is woken promptly for diagnosis.
    guardian_info = snap.get("infrastructure", {}).get("guardian", {})
    guardian_status = guardian_info.get("status", "unknown")
    if guardian_status == "down":
        staleness = guardian_info.get("staleness_s")
        stale_part = (
            f" (stale {int(staleness)}s)" if isinstance(staleness, int | float) else ""
        )
        alert_id = "guardian:heartbeat_stale"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": (
                f"Guardian heartbeat not updating{stale_part} — "
                f"host-side safety net is blind"
            ),
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

    # ── Credit exhaustion detection ─────────────────────────────────
    # A provider that had >95% success over 7 days but dropped to <50%
    # in the last hour likely ran out of credits/quota (not a transient
    # error).  Only check providers that are in active routing chains.
    if _service and _service._db:
        try:
            from datetime import timedelta as _td2

            from genesis.routing.provider_criticality import derive_criticality

            routing_config = None
            try:
                from genesis.runtime import GenesisRuntime
                rt = GenesisRuntime.instance()
                routing_config = getattr(rt, "_routing_config", None)
            except Exception:
                pass

            crit_map = derive_criticality(routing_config) if routing_config else {}

            now_utc = datetime.now(UTC)
            recent_cutoff = (now_utc - _td2(hours=1)).isoformat()
            baseline_cutoff = (now_utc - _td2(days=7)).isoformat()

            # Recent window: last 1 hour
            cursor = await _service._db.execute(
                "SELECT provider, COUNT(*) as calls, "
                "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors "
                "FROM activity_log WHERE created_at >= ? "
                "GROUP BY provider HAVING calls >= 5",
                (recent_cutoff,),
            )
            recent_rows = await cursor.fetchall()

            for row in recent_rows:
                prov, recent_calls, recent_errors = row
                prov_crit = crit_map.get(prov, {})
                criticality = prov_crit.get("criticality", "dormant")
                if criticality == "dormant":
                    continue  # Skip providers not in any chain

                recent_error_rate = recent_errors / recent_calls if recent_calls else 0
                if recent_error_rate < 0.5:
                    continue  # Not failing enough to suspect exhaustion

                # Check 7-day baseline for this provider
                baseline_cursor = await _service._db.execute(
                    "SELECT COUNT(*) as calls, "
                    "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors "
                    "FROM activity_log WHERE created_at >= ? AND created_at < ? "
                    "AND provider = ?",
                    (baseline_cutoff, recent_cutoff, prov),
                )
                baseline_row = await baseline_cursor.fetchone()
                if not baseline_row:
                    continue

                baseline_calls, baseline_errors = baseline_row
                if baseline_calls < 10:
                    continue  # Not enough baseline data

                baseline_error_rate = baseline_errors / baseline_calls
                if baseline_error_rate > 0.05:
                    continue  # Wasn't healthy before — not credit exhaustion

                # Was healthy (>95% success) over 7 days, now failing (>50% errors)
                alert_id = f"provider:credit_exhaustion:{prov}"
                is_free = prov_crit.get("is_free", False)
                if criticality in ("sole", "systemic") and not is_free:
                    severity = "CRITICAL"
                elif criticality == "sole" and is_free:
                    severity = "WARNING"
                else:
                    severity = "WARNING"
                alerts.append({
                    "id": alert_id,
                    "severity": severity,
                    "message": (
                        f"Suspected credit/quota exhaustion for {prov}: "
                        f"was {1 - baseline_error_rate:.0%} success over 7d, "
                        f"now {recent_error_rate:.0%} errors in last hour "
                        f"({recent_errors}/{recent_calls} calls)"
                    ),
                })
                current_ids.add(alert_id)
        except Exception:
            logger.debug("Credit exhaustion detection failed", exc_info=True)

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

    # ── Genesis update available ─────────────────────────────────────
    if _service and _service._db:
        try:
            cursor = await _service._db.execute(
                "SELECT content FROM observations "
                "WHERE source = 'genesis_version' AND type = 'genesis_update_available' "
                "AND resolved = 0 ORDER BY created_at DESC LIMIT 1",
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row[0] if isinstance(row, tuple) else row["content"])
                behind = data.get("commits_behind", "?")
                tag = data.get("target_tag", "unknown")
                alert_id = "genesis:update_available"
                alerts.append({
                    "id": alert_id,
                    "severity": "INFO",
                    "message": f"New Genesis version available: {tag} ({behind} commits behind) — update from dashboard",
                })
                current_ids.add(alert_id)

            # Check for update failure
            cursor = await _service._db.execute(
                "SELECT content FROM observations "
                "WHERE source = 'genesis_version' AND type = 'genesis_update_failed' "
                "AND resolved = 0 ORDER BY created_at DESC LIMIT 1",
            )
            row = await cursor.fetchone()
            if row:
                data = json.loads(row[0] if isinstance(row, tuple) else row["content"])
                alert_id = "genesis:update_failed"
                alerts.append({
                    "id": alert_id,
                    "severity": "CRITICAL",
                    "message": (
                        f"Genesis update to {data.get('new_tag', '?')} failed, "
                        f"rolled back to {data.get('rollback_tag', '?')}"
                    ),
                })
                current_ids.add(alert_id)
        except Exception:
            logger.error("Genesis update alert check failed", exc_info=True)

    # ── Backup health ───────────────────────────────────────────────
    from pathlib import Path

    backup_status_file = Path.home() / ".genesis" / "backup_status.json"
    if backup_status_file.is_file():
        try:
            backup_data = json.loads(backup_status_file.read_text())
            if not backup_data.get("success", False):
                alert_id = "backup:last_failed"
                reason = backup_data.get("failure_reason") or "check backup log"
                ts = backup_data.get("timestamp", "unknown")
                alerts.append({
                    "id": alert_id,
                    "severity": "CRITICAL",
                    "message": f"Last backup failed at {ts}: {reason}",
                })
                current_ids.add(alert_id)
            else:
                # Check staleness — backup succeeded but too long ago
                ts = backup_data.get("timestamp")
                if ts:
                    try:
                        last = datetime.fromisoformat(ts)
                        age_h = (
                            datetime.now(UTC) - last
                        ).total_seconds() / 3600
                        if age_h > 8:  # 6h schedule + 2h grace
                            alert_id = "backup:overdue"
                            alerts.append({
                                "id": alert_id,
                                "severity": "CRITICAL",
                                "message": (
                                    f"Backup overdue — last success "
                                    f"was {age_h:.0f}h ago"
                                ),
                            })
                            current_ids.add(alert_id)
                    except (ValueError, TypeError):
                        pass
        except (json.JSONDecodeError, OSError):
            pass
    else:
        # No status file = backups never configured or never ran
        alert_id = "backup:not_configured"
        alerts.append({
            "id": alert_id,
            "severity": "CRITICAL",
            "message": "Backups not configured — no backup status file found",
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
