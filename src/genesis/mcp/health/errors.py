"""health_errors and health_alerts tools."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime

from genesis.mcp.health import mcp  # noqa: E402
from genesis.mcp.health.constants import JOB_STALE_GAP_DAYS

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
                    errors.append(
                        {
                            "type": "dead_letter",
                            "provider": item.get("target_provider", "unknown"),
                            "reason": item.get("failure_reason", ""),
                            "operation": item.get("operation_type", ""),
                            "timestamp": item.get("created_at", ""),
                        }
                    )
        except Exception:
            logger.error("Failed to query dead letter errors", exc_info=True)

    if _service._breakers and _service._routing_config:
        from genesis.routing.types import ProviderState

        for name in _service._routing_config.providers:
            try:
                cb = _service._breakers.get(name)
                if cb.state == ProviderState.OPEN:
                    errors.append(
                        {
                            "type": "circuit_breaker_open",
                            "provider": name,
                            "reason": "Circuit breaker tripped",
                            "failures": cb.consecutive_failures,
                        }
                    )
            except Exception:
                logger.error(
                    "Circuit breaker state check failed for provider %s",
                    name,
                    exc_info=True,
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
                db_rows.extend(
                    await events_crud.query(
                        _service._db,
                        severity=sev,
                        since=cutoff,
                        limit=50,
                    )
                )
            for row in db_rows:
                errors.append(
                    {
                        "type": "event_bus",
                        "subsystem": row.get("subsystem", ""),
                        "event_type": row.get("event_type", ""),
                        "severity": row.get("severity", ""),
                        "message": row.get("message", ""),
                        "timestamp": row.get("timestamp", ""),
                    }
                )
            db_events_loaded = True
        except Exception:
            logger.error("Event log query failed", exc_info=True)

    if not db_events_loaded and _event_bus and hasattr(_event_bus, "recent_events"):
        from genesis.observability.types import Severity

        for event in _event_bus.recent_events(min_severity=Severity.WARNING, limit=50):
            if event.timestamp >= cutoff:
                errors.append(
                    {
                        "type": "event_bus",
                        "subsystem": event.subsystem.value,
                        "event_type": event.event_type,
                        "severity": event.severity.value,
                        "message": event.message,
                        "timestamp": event.timestamp,
                    }
                )

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


def _backups_enabled() -> bool:
    """Whether backups are configured on this install.

    Three "enabled" signals, any of which suffices:

    1. ``GENESIS_BACKUP_REPO`` in the process environment (genesis-server
       load_dotenv()s all of secrets.env at startup).
    2. The var in secrets.env itself — the standalone MCP health server
       only imports an allowlist of vars (scripts/genesis_mcp_server.py
       _MCP_VARS), so the env alone would wrongly report "disabled" there
       even on a fully configured install.
    3. An existing backup clone at backup.sh's BACKUP_DIR — backup.sh only
       needs the repo var for the FIRST clone (restore.sh --from <url>, or
       a transient shell var, creates the clone without persisting the
       var), and runs indefinitely off the clone's remote afterwards.

    Fails OPEN (enabled) on read errors: never hide a real backup failure
    because we couldn't determine the configuration.
    """
    val = os.environ.get("GENESIS_BACKUP_REPO", "").strip()
    if val and val not in ("None", "NA"):
        return True
    from pathlib import Path as _Path

    if (_Path.home() / "backups" / "genesis-backups" / ".git").is_dir():
        return True
    try:
        from genesis.env import secrets_path

        path = secrets_path()
        if not path.is_file():
            return False
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith("GENESIS_BACKUP_REPO="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                return bool(v) and v not in ("None", "NA")
        return False
    except Exception:
        logger.warning("Could not determine backup configuration", exc_info=True)
        return True


# How long an alert must have been continuously open before its message carries a
# duration. Most alerts flap briefly; decorating those is noise. An hour is well
# above the 5-minute awareness tick and well below any outage worth naming.
_ONGOING_FLOOR_S = 3600.0

# `_compute_alerts` recomputes the whole set every tick, so a message must never
# accrete suffixes. Stripping any existing one before recomputing gives BOTH
# idempotence and freshness — a skip-if-present guard would give only the first.
# That matters because the fail-open re-emit path below copies the message
# STORED in `alert_events`, which is captured at first fire and never rewritten:
# skipping a decorated message would pin the displayed age to whenever the row
# was written. A stale duration is worse than none, since it reads as current.
# Anchored at end-of-string so a legitimate parenthetical in the message body
# (e.g. "DOWN (all providers exhausted)") is untouched.
_ONGOING_MARK = "(ongoing for "
# Derived from the mark so the two cannot drift: changing the mark without
# this would silently stop the strip and let suffixes accrete.
_ONGOING_RE = re.compile(rf"\s*{re.escape(_ONGOING_MARK)}[^)]*\)\s*$")


def _ongoing_for(created_at: str | None, now: datetime) -> str | None:
    """Human duration an alert has been open, or None if it should not be shown.

    Returns None for anything younger than the floor, unparseable, or dated in
    the future (clock skew). Never raises: this decorates the alert path, and a
    bad row must cost a suffix, not the whole alert set.
    """
    if not created_at:
        return None
    try:
        started = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    elapsed = (now - started).total_seconds()
    if elapsed < _ONGOING_FLOOR_S:
        return None
    hours = int(elapsed // 3600)
    days, rem = divmod(hours, 24)
    if days and rem:
        return f"{days}d {rem}h"
    if days:
        return f"{days}d"
    return f"{hours}h"


def _apply_ongoing_duration(
    alerts: list[dict], open_rows: dict, *, now: datetime | None = None,
) -> None:
    """Append "(ongoing for Xd Yh)" to alerts with a durable open row.

    `alert_events` has carried `created_at` with 90-day retention since WS-2 M10
    and had no production reader — every surface rendered instantaneous state, so
    a 3-day provider outage read exactly like a 3-minute one (incident
    2026-08-28/29). Enriching the MESSAGE reaches the dashboard banner, the
    morning report and the Telegram path at once, because all three render
    `message`.

    Mutates in place. An alert firing for the FIRST time has no row yet (the
    awareness tick writes it after this runs), so it simply gets no suffix.

    KNOWN LIMIT, declined deliberately rather than patched (Codex P2): the only
    writer of `alert_events` is the awareness tick, so if that loop dies no new
    open rows appear and alerts first detected afterwards stay undecorated no
    matter how long they persist. The fail-soft behaviour is correct — no suffix
    is the honest output when the outage clock has no data.

    A fallback start time was considered and rejected: the only clock available
    without that writer is "when this process first saw the alert", which resets
    on every restart. That is precisely the flapping this whole change removes,
    and a duration that silently understates the outage is worse than none
    because it reads as current. A second persistence path would be a new store
    for data one tick already owns. The dead-writer condition is separately
    visible (`awareness:tick_overdue`), which is the right place to surface it.
    """
    now = now or datetime.now(UTC)
    for alert in alerts:
        try:
            raw = alert.get("message")
            if raw is None:
                # No message to decorate. Skipping rather than writing is
                # deliberate: this function widened from "only touch decorated
                # alerts that have a row" to "always rewrite", and without this
                # guard a messageless alert would gain `message=""` — which
                # `outreach/morning_report.py` renders as blank instead of
                # falling back to its own "Unknown". Not reachable from the
                # current append sites (all pass a literal message); the guard
                # keeps the widening from carrying a latent regression.
                continue
            # Strip FIRST, unconditionally: an alert whose row has gone must
            # not keep a suffix describing an age nothing can still vouch for.
            base = _ONGOING_RE.sub("", str(raw)).strip()
            row = open_rows.get(alert.get("id", ""))
            human = _ongoing_for(row.get("created_at"), now) if row else None
            alert["message"] = f"{base} {_ONGOING_MARK}{human})".strip() if human else base
        except Exception:  # noqa: BLE001 - never break the alert path
            logger.debug("ongoing-duration decoration failed", exc_info=True)


async def _compute_alerts() -> tuple[list[dict], set[str]]:
    """Pure alert computation — recompute the firing alert set from live health.

    Returns ``(alerts, current_ids)`` and carries NO ``_alert_history`` state.
    Two consumers: ``_impl_health_alerts`` (adds the in-memory one-generation
    resolved rendering, unchanged read contract) and the awareness-tick writer
    (persists the DURABLE open-set into ``alert_events`` — WS-2 M10). Keeping this
    a pure function is what lets a single designated writer own persistence
    without the multi-caller / cross-process double-write the read path causes.
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    _activity_tracker = health_mcp_mod._activity_tracker
    _job_retry_registry = health_mcp_mod._job_retry_registry

    # WS-2 P1b: ledger writer-hook failures — checked BEFORE the
    # uninitialized-service early return because the counter needs no health
    # snapshot (and a dropping commit path must surface even then). The
    # counter is per-process: in the runtime process (where the hooks run AND
    # where the awareness-tick alert writer lives) nonzero means predictions
    # are being dropped; the health-MCP process always computes zero here and
    # stays read-only — consistent with the single-designated-writer rule.
    alerts: list[dict] = []
    current_ids: set[str] = set()
    try:
        from genesis.ledger.writers import write_failure_counts

        for action_class, count in sorted(write_failure_counts().items()):
            if count > 0:
                alert_id = f"ledger:write_failed:{action_class}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"{count} ledger prediction write(s) failed for "
                            f"{action_class} since process start — the commit-path "
                            "hook is dropping predictions (coverage gap)"
                        ),
                    }
                )
                current_ids.add(alert_id)
    except Exception:
        logger.debug("ledger write-failure alert check failed", exc_info=True)

    # WS-2 P2: ledger grader alarms — registry-vanished metrics (schema-vs-code
    # drift) and resolver exceptions. Same per-process counter contract as the
    # writer-hook alarm above; checked before the uninitialized-service return.
    try:
        from genesis.ledger.grader import grade_failure_counts

        _gfc = grade_failure_counts()
        for action_class, count in sorted(_gfc["metric_vanished"].items()):
            if count > 0:
                alert_id = f"ledger:metric_vanished:{action_class}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"{count} {action_class} prediction(s) reference a metric "
                            "no longer in the ledger registry — the grader marked "
                            "them unresolvable (schema-vs-code drift)"
                        ),
                    }
                )
                current_ids.add(alert_id)
        for action_class, count in sorted(_gfc["grade_failed"].items()):
            if count > 0:
                alert_id = f"ledger:grade_failed:{action_class}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"{count} {action_class} prediction(s) raised in their "
                            "resolver during grading — left open (grader bug or "
                            "malformed evidence)"
                        ),
                    }
                )
                current_ids.add(alert_id)
    except Exception:
        logger.debug("ledger grade-failure alert check failed", exc_info=True)

    # WS-2 P3: calibration-cell recompute failures — grading still lands, but
    # every calibration consumer (MCP, dashboard, perception advisory) reads
    # stale cells until a pass succeeds. Same per-process counter contract.
    try:
        from genesis.ledger.cells import cell_recompute_failure_counts

        for kind, count in sorted(cell_recompute_failure_counts().items()):
            if count > 0:
                alert_id = f"ledger:cell_recompute_failed:{kind}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"{count} calibration-cell recompute(s) failed since "
                            "process start — grades landed but calibration_cells "
                            "is stale for all consumers"
                        ),
                    }
                )
                current_ids.add(alert_id)
    except Exception:
        logger.debug("cell recompute alert check failed", exc_info=True)

    # WS-2 P4: ego-proposal arbitration lookup failures — proposals still ship
    # (annotation is best-effort) but calibration badges/escalation notes are
    # silently absent. Same per-process counter contract.
    try:
        from genesis.ego.proposals import arbitration_failure_counts

        for action_type, count in sorted(arbitration_failure_counts().items()):
            if count > 0:
                alert_id = f"ledger:arbitration_failed:{action_type}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"{count} arbitration calibration lookup(s) failed for "
                            f"ego action_type {action_type} — proposals shipped "
                            "without calibration annotations"
                        ),
                    }
                )
                current_ids.add(alert_id)
    except Exception:
        logger.debug("arbitration alert check failed", exc_info=True)

    if _service is None:
        alerts.append(
            {
                "id": "service:health_data_uninitialized",
                "severity": "CRITICAL",
                "message": "HealthDataService not initialized",
            }
        )
        return alerts, current_ids

    snap = await _service.snapshot()

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
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "WARNING",
                    "message": f"Call site {site_id} is DOWN (all providers exhausted)",
                }
            )
            current_ids.add(alert_id)
        elif status_val == "degraded":
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "WARNING",
                    "message": f"Call site {site_id} is degraded (using fallback provider)",
                    "active_provider": site_info.get("active_provider"),
                }
            )
            current_ids.add(alert_id)

    queues = snap.get("queues", {})
    # Only check fields that represent actual queue depths — exclude
    # cumulative counters (embedded_total), timestamps, error messages, etc.
    #
    # NOTE: we alarm on `deferred_recovery` (genuine recovery backlog), NOT the
    # raw `deferred_work` total. The raw total includes scheduled batch worklists
    # (dream synthesis, ~500 slices draining over a week) that legitimately sit
    # hundreds-deep by design — alerting on the raw total fired `queue:deferred_work`
    # WARNING on every awareness tick. `deferred_recovery` folds a stalled worklist
    # back in (see resilience/deferred_work.py:STALE_WORKLIST_DAYS), so a genuinely
    # broken drain still surfaces. `deferred_work`/`deferred_worklist` stay in the
    # snapshot for honest display but are not depth-alarmed here.
    _QUEUE_DEPTH_FIELDS = {
        "pending_embeddings",
        "dead_letters",
        "deferred_recovery",
        "deferred_processing",
        "deferred_stuck",
        "failed_embeddings",
        "discarded_count",
    }
    for queue_name, depth in queues.items():
        if queue_name not in _QUEUE_DEPTH_FIELDS:
            continue
        if isinstance(depth, int) and depth > 100:
            alert_id = f"queue:{queue_name}"
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "WARNING",
                    "message": f"Queue {queue_name} depth is {depth} (>100)",
                }
            )
            current_ids.add(alert_id)

    cc = snap.get("cc_sessions", {})
    bg = cc.get("background", {})
    if bg.get("status") in ("throttled", "rate_limited"):
        alert_id = "cc:budget"
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": f"CC sessions {bg['status']} (budget: {bg.get('hourly_budget', '?')})",
            }
        )
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
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "WARNING",
                    "message": f"CC {cc_realtime.lower().replace('_', ' ')} — contingency mode active",
                }
            )
            current_ids.add(alert_id)

    awareness = snap.get("awareness", {})
    tick_age = awareness.get("time_since_last_tick_seconds")
    if tick_age is not None and tick_age > 360:
        alert_id = "awareness:tick_overdue"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": f"Awareness tick overdue by {int(tick_age)}s (>360s threshold)",
            }
        )
        current_ids.add(alert_id)

    dl_age = snap.get("queues", {}).get("dead_letter_oldest_age_seconds")
    if dl_age is not None and dl_age > 3600:
        alert_id = "queue:stale_dead_letters"
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Dead letter queue has items {int(dl_age)}s old (>1h threshold)",
            }
        )
        current_ids.add(alert_id)

    disk = snap.get("infrastructure", {}).get("disk", {})
    free_pct = disk.get("free_pct")
    if free_pct is not None and free_pct < 15:
        alert_id = "infra:disk_low"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL" if free_pct < 10 else "WARNING",
                "message": f"Disk space low: {free_pct}% free ({disk.get('free_gb', '?')}GB)",
            }
        )
        current_ids.add(alert_id)

    container_mem = snap.get("infrastructure", {}).get("container_memory", {})
    # Use anon_pct (non-reclaimable memory) for alerts, not used_pct
    # (total cgroup including reclaimable page cache).
    anon_pct = container_mem.get("anon_pct", container_mem.get("used_pct", 0))
    if anon_pct > 85:
        alert_id = "infra:container_memory_high"
        # CRITICAL at >85% (lowered from >90% on 2026-06-13). This is a
        # deliberate escalation-threshold expansion, not a cleanup: it moves
        # both the Telegram alert AND the voice chime earlier. anon_pct is
        # non-reclaimable memory, so >85% is genuine pressure (the box idles
        # ~76-79%). 6h dedup prevents repeat spam.
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": f"Container memory at {anon_pct}% anon+kernel ({container_mem.get('current_gb', '?')}/{container_mem.get('limit_gb', '?')}GB total)",
            }
        )
        current_ids.add(alert_id)

    qdrant_cols = snap.get("infrastructure", {}).get("qdrant_collections", {})
    missing_cols = qdrant_cols.get("missing", [])
    if missing_cols:
        alert_id = "infra:qdrant_collections_missing"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": f"Qdrant collections missing: {', '.join(missing_cols)} — memory operations will fail",
            }
        )
        current_ids.add(alert_id)

    services = snap.get("services", {})
    genesis_svc = services.get("bridge", {})  # key is "bridge" for backward compat
    if genesis_svc.get("active_state") not in ("active", "unknown"):
        svc_label = genesis_svc.get("service_unit", "genesis-server.service")
        alert_id = "service:genesis_down"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": f"{svc_label} is {genesis_svc.get('active_state', 'unknown')}",
            }
        )
        current_ids.add(alert_id)

    watchdog_timer = services.get("watchdog_timer", {})
    if watchdog_timer.get("active_state") not in ("active", "unknown"):
        alert_id = "service:watchdog_blind"
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": "genesis-watchdog.timer is inactive — infrastructure monitoring is blind",
            }
        )
        current_ids.add(alert_id)

    watchdog_state = services.get("watchdog", {})
    wd_failures = watchdog_state.get("consecutive_failures", 0)
    if wd_failures > 3:
        alert_id = "service:watchdog_failing"
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Watchdog triggered {wd_failures} consecutive restarts (reason: {watchdog_state.get('last_reason', 'unknown')})",
            }
        )
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
        stale_part = f" (stale {int(staleness)}s)" if isinstance(staleness, int | float) else ""
        alert_id = "guardian:heartbeat_stale"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": (
                    f"Guardian heartbeat not updating{stale_part} — host-side safety net is blind"
                ),
            }
        )
        current_ids.add(alert_id)

    ollama = snap.get("infrastructure", {}).get("ollama", {})
    missing_models = ollama.get("missing_models", [])
    if missing_models:
        alert_id = "infra:ollama_model_mismatch"
        names = ", ".join(f"{m['provider']}:{m['model']}" for m in missing_models)
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Ollama missing configured models: {names}",
            }
        )
        current_ids.add(alert_id)

    # Internet connectivity. WARNING (not CRITICAL): the container Sentinel can't
    # fix an ISP outage, so this stays out of the Sentinel-waking CRITICAL tier.
    # Fires only on a confirmed OFFLINE (down) — DEGRADED/unknown/absent do not
    # alert (a slow or unmonitored network is not an incident). The infra probe
    # already staleness-guards, so a stale sentinel reads 'unknown', not 'down'.
    internet = snap.get("infrastructure", {}).get("internet", {})
    if isinstance(internet, dict) and internet.get("status") == "down":
        alert_id = "infra:internet_down"
        detail = internet.get("message") or "offline"
        alerts.append(
            {
                "id": alert_id,
                "severity": "WARNING",
                "message": f"Internet connectivity down ({detail})",
            }
        )
        current_ids.add(alert_id)

    if _activity_tracker is not None:
        emb_summary = _activity_tracker.summary("episodic_memory_embedding")
        if (
            isinstance(emb_summary, dict)
            and emb_summary.get("calls", 0) > 0
            and emb_summary.get("error_rate", 0) > 0.5
        ):
            alert_id = "provider:embedding_failing"
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "CRITICAL",
                    "message": (
                        f"Embedding provider error rate: {emb_summary['error_rate']:.0%} "
                        f"({emb_summary['errors']}/{emb_summary['calls']} calls failed)"
                    ),
                }
            )
            current_ids.add(alert_id)

        qdrant_summary = _activity_tracker.summary("qdrant.search")
        if (
            isinstance(qdrant_summary, dict)
            and qdrant_summary.get("calls", 0) > 0
            and qdrant_summary.get("error_rate", 0) >= 0.5
        ):
            # Fire at >=50% failure (lowered from ==100% on 2026-06-13) to
            # match provider:embedding_failing and warn on partial memory-
            # search degradation, not just total outage. Id keeps the
            # "unreachable" name (referenced by config lists); message shows
            # the actual rate.
            alert_id = "provider:qdrant_unreachable"
            _qdrant_rate = qdrant_summary.get("error_rate", 0)
            _qdrant_errors = qdrant_summary.get("errors", 0)
            _qdrant_calls = qdrant_summary.get("calls", 0)
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "CRITICAL",
                    "message": (
                        f"Qdrant search {_qdrant_rate:.0%} failure rate "
                        f"({_qdrant_errors}/{_qdrant_calls} calls failed)"
                    ),
                }
            )
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
                # activity_log stores LLM calls as ``llm.<name>`` but crit_map
                # (derive_criticality) is keyed by provider_type. Resolve in two
                # hops: strip ``llm.`` prefix -> provider name -> provider_type ->
                # criticality. Non-routed rows (embedding / qdrant.* / mcp.*) have
                # no provider config and are skipped. (Pre-fix this lookup was
                # single-hop on the raw ``llm.<name>`` string, so it ALWAYS missed
                # -> every provider read "dormant" -> the detector was 100% dead.)
                prov_name = prov[4:] if isinstance(prov, str) and prov.startswith("llm.") else prov
                provider_cfg = routing_config.providers.get(prov_name) if routing_config else None
                if provider_cfg is None:
                    continue  # not a routed LLM provider (embeddings/mcp.*/qdrant.*)
                prov_crit = crit_map.get(provider_cfg.provider_type, {})
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

                # Was healthy (>95% success) over 7 days, now failing (>50%
                # errors). Dashboard WARNING ONLY -- never CRITICAL. Refilling
                # credits/quota is a user (financial) action the Sentinel cannot
                # take, and provider exhaustion alone is not an outage (routing
                # fallbacks cover it). Telegram/Sentinel are reserved for genuine
                # call-site-down + infra emergencies -- see the unified call-site
                # health detector below for the "whole call site down" signal.
                # GROUNDWORK(sentinel-auto-topup): CRITICAL_CALL_SITES
                # (mcp/health/critical_sites.py) + this per-provider signal are the
                # seam for a future user-gated auto-credit-top-up; see
                # remediation_map.UNMAPPED_BY_DESIGN["provider:credit_exhaustion:"].
                alert_id = f"provider:credit_exhaustion:{prov_name}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"Suspected credit/quota exhaustion for {prov_name}: "
                            f"was {1 - baseline_error_rate:.0%} success over 7d, "
                            f"now {recent_error_rate:.0%} errors in last hour "
                            f"({recent_errors}/{recent_calls} calls)"
                        ),
                    }
                )
                current_ids.add(alert_id)
        except Exception:
            logger.debug("Credit exhaustion detection failed", exc_info=True)

    # ── Unified critical call-site health ────────────────
    # "Is a whole call site down?" -- orthogonal to the per-provider credit
    # signal above. call_site_last_run holds ONE row per site (its latest run,
    # written by every execution path: router / cc / embedding / pipeline /
    # gate), so success=0 on the last run is the honest "this site's last
    # attempt failed" signal -- and it captures cc + embedding failures the
    # provider-tier checks above miss. Sites in CRITICAL_CALL_SITES render
    # CRITICAL/red; every other failing site renders WARNING/yellow (watched,
    # not alarming). Dashboard-only BY CONSTRUCTION: ``callsite:down:`` is never
    # on the outreach escalation whitelist (no Telegram) and is UNMAPPED in the
    # Sentinel remediation map (no firefighter) -- both fail-closed. The recency
    # window drops long-abandoned stale-failed rows (a weeks-old one-off is not
    # an actionable outage). This deliberately overlaps the escalation-path
    # alerts (cc:quota_exhausted / provider:embedding_failing) -- this is the
    # glanceable unified availability view, those are the paging path.
    if _service and _service._db:
        try:
            from datetime import timedelta as _td3

            from genesis.mcp.health.critical_sites import (
                CALLSITE_DOWN_RECENCY_HOURS,
                CRITICAL_CALL_SITES,
            )

            cs_cutoff = (datetime.now(UTC) - _td3(hours=CALLSITE_DOWN_RECENCY_HOURS)).isoformat()
            cs_cursor = await _service._db.execute(
                "SELECT call_site_id, last_run_at, provider_used "
                "FROM call_site_last_run WHERE success = 0 AND last_run_at >= ?",
                (cs_cutoff,),
            )
            for cs_row in await cs_cursor.fetchall():
                site_id, last_run_at, provider_used = cs_row
                is_critical = site_id in CRITICAL_CALL_SITES
                alert_id = f"callsite:down:{site_id}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL" if is_critical else "WARNING",
                        "message": (
                            f"Call site '{site_id}' last run failed "
                            f"(provider {provider_used or 'unknown'}, last attempt "
                            f"{str(last_run_at)[:19]})"
                            + (" -- CRITICAL site" if is_critical else "")
                        ),
                    }
                )
                current_ids.add(alert_id)
        except Exception:
            # call_site_last_run is a core table (migration 0015) -- a failure
            # here is a real operational fault, not an expected-absent case.
            logger.error("Call-site health check failed", exc_info=True)

    if _job_retry_registry is not None:
        for job_name in _job_retry_registry.list_registered():
            if _job_retry_registry.is_quarantined(job_name):
                alert_id = f"job:quarantined:{job_name}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": f"Job {job_name} is quarantined (max retries exhausted, auto-unquarantine in ≤24h)",
                    }
                )
                current_ids.add(alert_id)

    # ── Silently-stale jobs ──────────────────────────────────────────
    # A job whose last_run is well ahead of last_success is firing on
    # schedule but never completing. consecutive_failures MISSES this:
    # clear_stale_job_failures resets it to 0 on every server restart, and
    # an infrequent (weekly/monthly) job can't reach the failure threshold
    # between restarts anyway. The last_run − last_success gap survives
    # restarts (clear_stale never touches those two fields), so it is the
    # honest "running but not succeeding" signal. The gap only grows when a
    # job runs without succeeding, so a healthy job reads 0 regardless of
    # cadence — no per-job schedule lookup needed.
    if _service and _service._db:
        # Two INDEPENDENT detectors in separate try blocks: a raise in one must not
        # blind the other on the same tick (both query the same always-present table,
        # so correlated failure is likely, but the split makes it impossible for the
        # gap detector to swallow the never-succeeded check or vice versa).
        try:
            from genesis.db.crud import job_health as job_health_crud

            for row in await job_health_crud.get_stale_jobs(
                _service._db, threshold_days=JOB_STALE_GAP_DAYS
            ):
                alert_id = f"job_stale:{row['job_name']}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"Scheduled job '{row['job_name']}' has run since but not "
                            f"succeeded in {row['gap_days']:.0f} days (last success "
                            f"{str(row['last_success'])[:10]}) — silently failing "
                            f"(its failure counter resets on restart)"
                        ),
                    }
                )
                current_ids.add(alert_id)
        except Exception:
            # job_health is a core table that always exists — a failure here is a real
            # operational fault, not an expected-absent-table case, so ERROR.
            logger.error("Silently-stale job alert check failed", exc_info=True)

        # Never-succeeded jobs: the gap query above filters last_success IS NOT NULL,
        # so a job that has run + failed repeatedly but never once succeeded
        # (last_success NULL forever) is invisible to it AND to consecutive_failures
        # (reset on restart). total_runs/total_failures are monotonic → restart-proof.
        # (An 8-day OAuth outage on a daily actuator hid here until this was added.)
        try:
            from datetime import timedelta as _td4

            from genesis.db.crud import job_health as job_health_crud

            # Recency bound: only alarm jobs still running recently — a never-succeeded
            # job that was disabled/removed leaves a fossil row (nothing auto-purges
            # job_health) that would otherwise WARNING forever. The slowest scheduled
            # cadence is WEEKLY (verified — no monthly/quarterly jobs exist), so 35d
            # covers every real job with wide margin (and a plausible future monthly
            # actuator) while bounding a removed job's fossil WARNING to ≤35d — and this
            # alert is dashboard/health-only (not escalated), so that lag is harmless.
            # A job that runs LESS often than 35d would fall through both this and the
            # gap detector; documented in get_never_succeeded_jobs' CONTRACT.
            recent_since = (datetime.now(UTC) - _td4(days=35)).strftime("%Y-%m-%dT%H:%M:%SZ")
            for row in await job_health_crud.get_never_succeeded_jobs(
                _service._db, recent_since=recent_since
            ):
                alert_id = f"job_never_succeeded:{row['job_name']}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"Scheduled job '{row['job_name']}' has failed "
                            f"{row['total_failures']} times and has NEVER succeeded "
                            f"(last error: {str(row['last_error'] or 'n/a')[:80]}) — "
                            f"invisible to the gap + consecutive-failure alarms"
                        ),
                    }
                )
                current_ids.add(alert_id)
        except Exception:
            # job_health is a core table that always exists — a failure here is a real
            # operational fault, not an expected-absent-table case, so ERROR.
            logger.error("Never-succeeded job alert check failed", exc_info=True)

    # ── Stopped-firing subsystems (total-cessation) ──────────────────
    # Each subsystem below pulses a durable ``heartbeat`` event whose emission is
    # independent of pause (ego: dedicated ego_heartbeat job; dashboard + outreach:
    # dedicated daemon threads) or is downgraded to "paused" while paused (inbox, whose
    # pulse stops behind ``if paused: return`` — handled in compute_heartbeat_staleness).
    # When that pulse goes overdue past its per-subsystem threshold the
    # scheduler/loop has stopped — a SILENT death the failure-gap alarms above
    # cannot see (those need a RUNNING-but-failing job; a stopped job never
    # advances last_run either). Complements job_stale/job_never_succeeded.
    # SET = ego (CRITICAL — the dead-ego-scheduler failure this batch began with)
    # + inbox + dashboard + outreach (WARNING). Outreach's dedicated heartbeat
    # (``outreach/heartbeat.py``) pulses only while its scheduler is running and is
    # enable-gated (``_subsystem_enabled('outreach')`` = Telegram configured), so a
    # Telegram-less (dashboard-only) install is benign — closing the old false-alarm
    # trap (its pulse used to be config-gated + emergent). EXCLUDED: surplus — PR-B's
    # shipped tile already flips red on a wedged/dead surplus via its finer job_health
    # signal, and surplus's event-heartbeat is load-fragile (loop-END emit gaps under a
    # 15-30min dispatch); reflection — its pulse is the awareness loop, not
    # reflection-engine liveness; awareness — has the more precise
    # ``awareness:tick_overdue`` above. Coverage cap: the ~90 non-pulse scheduled jobs
    # get ONLY the failure-gap signals above; and outreach's own boundary — a
    # Telegram-configured install whose scheduler NEVER started (registration failed)
    # emits no pulse ever → benign no_heartbeat. Outreach IS a bootstrap-manifest entry
    # (records ``ok`` = scheduler CONSTRUCTED, not running), so it is explicitly exempted
    # from the started-silent never_started inference (manifest ``_CONDITIONAL_PULSE_SUBSYSTEMS``)
    # — a constructed-but-not-started scheduler is benign; only a genuine init FAILURE
    # (``failed:``/``degraded``) fires never_started, and only ``subsystem_stale`` (a
    # once-running scheduler that then died) fires on cessation. The shared
    # ``compute_heartbeat_staleness`` reads the same signal the ego dashboard tile
    # does, so alert and tile agree on the OVERDUE verdict. (They diverge by design
    # on ``no_heartbeat``: the tile applies a boot-grace and flips to error past it,
    # while the alert path treats no-pulse-ever as empty-state — a once-run subsystem
    # always retains a pulse via keep_latest GC, so this only differs on a truly
    # fresh, never-bootstrapped install.)
    _stale_read_failed: list[str] = []
    try:
        from genesis.mcp.health.manifest import (
            _subsystem_enabled,
            compute_heartbeat_staleness,
        )

        _stale_db = _service._db if _service else None
        # zero_drop joins the list because its silence is uniquely deceptive: a
        # dead stranded-work detector keeps answering "what fell through the
        # cracks?" with its last, stale, confident zero, and nothing else in the
        # system contradicts it. Enable-gated like the others via
        # ``_subsystem_enabled`` (its mode lever), so turning it off does not
        # buy a permanent alarm.
        for _hb_name in ("ego", "inbox", "dashboard", "outreach", "zero_drop"):
            try:
                # raise_on_error=True → a read failure fails LOUD (handled below by
                # preserving any open alert), never a silent green that lets
                # reconcile auto-resolve a genuinely-open subsystem_stale alert.
                _hb = await compute_heartbeat_staleness(_hb_name, db=_stale_db, raise_on_error=True)
            except Exception:
                logger.error(
                    "Pulse-staleness read failed for subsystem %s", _hb_name, exc_info=True
                )
                _stale_read_failed.append(_hb_name)
                continue
            _status = _hb.get("status")
            if _status == "unknown":
                # Unreadable pulse (corrupt / materially-future timestamp) → liveness
                # cannot be confirmed. Surface LOUDLY as a WARNING (honest: "can't
                # tell", NOT "died" — distinct id from subsystem_stale) instead of a
                # silent skip; auto-resolves when a fresh parseable pulse lands.
                logger.error(
                    "Subsystem %s heartbeat unreadable (unknown) — liveness unconfirmable",
                    _hb_name,
                )
                _unk_last = str(_hb.get("last_seen") or "")[:40]
                alert_id = f"subsystem_heartbeat_unknown:{_hb_name}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "WARNING",
                        "message": (
                            f"Subsystem '{_hb_name}' heartbeat is unreadable (corrupt "
                            f"or clock-skewed timestamp '{_unk_last}') — liveness "
                            f"cannot be confirmed"
                        ),
                    }
                )
                current_ids.add(alert_id)
                continue
            if _status == "never_started":
                # Registered/expected but failed to start or never pulsed once (#10) —
                # a dead liveness signal that no_heartbeat (empty-state) would hide.
                # Distinct id from subsystem_stale (a once-live scheduler that DIED)
                # and heartbeat_unknown (corrupt pulse). compute_heartbeat_staleness
                # has already gated this on _subsystem_enabled, so a disabled/
                # unconfigured subsystem never reaches here. ego CRITICAL, else WARNING;
                # auto-resolves when the subsystem finally pulses (verdict → alive).
                # Coverage: only ego + inbox can reach this — both are bootstrap init
                # steps recorded in the manifest. `dashboard` is in this loop for the
                # stale/unknown paths only; it is a daemon thread, NOT a manifest entry,
                # so its verdict is always benign here. A never-started dashboard needs
                # its own signal (a manifest entry for the thread) — a separate follow-up.
                _ns_failed = _hb.get("reason") == "init-failed"
                _detail = (
                    "failed to initialize at bootstrap"
                    if _ns_failed
                    else "started but has never emitted a heartbeat"
                )
                alert_id = f"subsystem_never_started:{_hb_name}"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL" if _hb_name == "ego" else "WARNING",
                        "message": (
                            f"Subsystem '{_hb_name}' is not running — it {_detail} "
                            f"(never started); a restart-safe config/code fault, not a "
                            f"transient stall"
                        ),
                    }
                )
                current_ids.add(alert_id)
                continue
            if _status != "overdue":
                # alive / paused / resuming / no_heartbeat (empty-state) → no alert
                continue
            if not _subsystem_enabled(_hb_name):
                # Intentionally DISABLED after having pulsed → its retained stale pulse
                # is deliberate, not a death; suppress the (else permanent) alert.
                continue
            _age = int(_hb.get("age_seconds") or 0)
            _last_seen = str(_hb.get("last_seen") or "")[:19]
            alert_id = f"subsystem_stale:{_hb_name}"
            alerts.append(
                {
                    "id": alert_id,
                    "severity": "CRITICAL" if _hb_name == "ego" else "WARNING",
                    "message": (
                        f"Subsystem '{_hb_name}' heartbeat overdue — no pulse in "
                        f"{_age}s (last seen {_last_seen}); its scheduler/loop may "
                        f"have died"
                    ),
                }
            )
            current_ids.add(alert_id)

        # Fail-open guard (P2-4): a subsystem whose staleness read FAILED this tick
        # is in an unknown state — we must NOT let the reconcile writer auto-resolve
        # a genuinely-open subsystem_stale alert for it on a transient read blip.
        # The reconcile writer (awareness/loop.py:158-168) builds its active-set
        # from this ``alerts`` LIST (it does not consult current_ids), so RE-EMIT
        # the already-open alert (reconstructed from its open row) — keeping it both
        # open and displayed — for only the SPECIFIC failed subsystems (ones that
        # read fine resolve/alert normally).
        if _stale_read_failed and _service and _service._db:
            try:
                from genesis.db.crud import alert_events as _ae

                _open_rows = {r.get("alert_id", ""): r for r in await _ae.list_open(_service._db)}
                # Preserve EVERY alert family this per-subsystem block can emit
                # (subsystem_stale: overdue, subsystem_heartbeat_unknown: unreadable,
                # subsystem_never_started: failed/silent start) — otherwise the omitted
                # family flaps (auto-resolved by the reconciler on the failed tick,
                # re-opened on the next successful one). Keep in lockstep with the
                # branches above that append to ``alerts``/``current_ids``.
                for _n in _stale_read_failed:
                    for _aid in (
                        f"subsystem_stale:{_n}",
                        f"subsystem_heartbeat_unknown:{_n}",
                        f"subsystem_never_started:{_n}",
                    ):
                        _row = _open_rows.get(_aid)
                        if _row is not None:
                            alerts.append(
                                {
                                    "id": _aid,
                                    "severity": _row.get("severity", "WARNING"),
                                    "message": _row.get("message", ""),
                                }
                            )
                            current_ids.add(_aid)  # lockstep, for alert-history dedup
            except Exception:
                logger.error(
                    "Preserving open subsystem-heartbeat alerts on read failure failed",
                    exc_info=True,
                )
    except Exception:
        # Own try/except so a pulse-staleness read failure can't blind the other
        # alert blocks; ERROR-logged (never a silent green).
        logger.error("Subsystem pulse-staleness alert check failed", exc_info=True)

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
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "INFO",
                        "message": f"New Genesis version available: {tag} ({behind} commits behind) — update from dashboard",
                    }
                )
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
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL",
                        "message": (
                            f"Genesis update to {data.get('new_tag', '?')} failed, "
                            f"rolled back to {data.get('rollback_tag', '?')}"
                        ),
                    }
                )
                current_ids.add(alert_id)
        except Exception:
            logger.error("Genesis update alert check failed", exc_info=True)

    # ── Backup health ───────────────────────────────────────────────
    # Gated on backups being ENABLED on this install: GENESIS_BACKUP_REPO is
    # the same signal backup.sh itself requires. On an install where backups
    # are intentionally disabled (another machine owns the offsite backups),
    # a stale/failed local status file is a dishonest CRITICAL — same class
    # as the call_site:* source-gating above. Where backups ARE enabled,
    # real failures still alert (user-facing: dashboard + the outreach
    # immediate-escalation whitelist — backup targets are external, so this
    # never wakes the Sentinel; see sentinel/remediation_map.py).
    from pathlib import Path

    backup_status_file = Path.home() / ".genesis" / "backup_status.json"
    if not _backups_enabled():
        pass
    elif backup_status_file.is_file():
        try:
            backup_data = json.loads(backup_status_file.read_text())
            if not backup_data.get("success", False):
                alert_id = "backup:last_failed"
                reason = backup_data.get("failure_reason") or "check backup log"
                ts = backup_data.get("timestamp", "unknown")
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL",
                        "message": f"Last backup failed at {ts}: {reason}",
                    }
                )
                current_ids.add(alert_id)
            else:
                # Check staleness — backup succeeded but too long ago
                ts = backup_data.get("timestamp")
                if ts:
                    try:
                        last = datetime.fromisoformat(ts)
                        age_h = (datetime.now(UTC) - last).total_seconds() / 3600
                        if age_h > 8:  # 6h schedule + 2h grace
                            alert_id = "backup:overdue"
                            alerts.append(
                                {
                                    "id": alert_id,
                                    "severity": "CRITICAL",
                                    "message": (
                                        f"Backup overdue — last success was {age_h:.0f}h ago"
                                    ),
                                }
                            )
                            current_ids.add(alert_id)
                    except (ValueError, TypeError):
                        pass
                # Check Tier 2 target configured
                t2_status = backup_data.get("tier2_status", "unknown")
                if t2_status in ("not_configured", "no_smbclient"):
                    alert_id = "backup:tier2_unconfigured"
                    alerts.append(
                        {
                            "id": alert_id,
                            "severity": "WARNING",
                            "message": (
                                "Large backup targets not configured — "
                                "Qdrant/SQL snapshots are local-only"
                            ),
                        }
                    )
                    current_ids.add(alert_id)
        except (json.JSONDecodeError, OSError):
            pass
    else:
        # No status file = backups never configured or never ran
        alert_id = "backup:not_configured"
        alerts.append(
            {
                "id": alert_id,
                "severity": "CRITICAL",
                "message": "Backups not configured — no backup status file found",
            }
        )
        current_ids.add(alert_id)

    # Credential-file integrity — the container self-heal writes this status;
    # corruption (or a failed/rate-capped restore) is CRITICAL, and a completed
    # auto-restore is also CRITICAL (the user must know creds were replaced, and
    # rotate any that may have changed since the backup).
    cred_status_file = Path.home() / ".genesis" / "cred_integrity_status.json"
    if cred_status_file.is_file():
        try:
            cred_data = json.loads(cred_status_file.read_text())
            targets = cred_data.get("targets", {})
            if not isinstance(targets, dict):
                targets = {}
            targets = {n: t for n, t in targets.items() if isinstance(t, dict)}
            # corrupt_pending is deliberately EXCLUDED: it is the 2-tick debounce
            # window (a writer possibly caught mid-rewrite). It self-clears or
            # escalates to a terminal status (restored / restore_failed) next
            # tick, so alerting on it would defeat the debounce's whole purpose.
            corrupt = {
                n: t for n, t in targets.items() if t.get("status") in ("corrupt", "restore_failed")
            }
            restored = {n: t for n, t in targets.items() if t.get("status") == "restored"}
            if corrupt:
                detail = "; ".join(
                    f"{n} ({t.get('detail', t.get('status'))})" for n, t in sorted(corrupt.items())
                )
                alert_id = "creds:corrupt"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL",
                        "message": f"Credential file corruption: {detail}",
                    }
                )
                current_ids.add(alert_id)
            if restored:
                detail = "; ".join(
                    f"{n} (from backup {t.get('backup_mtime', '?')})"
                    for n, t in sorted(restored.items())
                )
                alert_id = "creds:restored"
                alerts.append(
                    {
                        "id": alert_id,
                        "severity": "CRITICAL",
                        "message": (
                            f"Credential files auto-restored: {detail} — "
                            "rotate any that changed since the backup"
                        ),
                    }
                )
                current_ids.add(alert_id)
        except (json.JSONDecodeError, OSError):
            pass

    # Decorate with how long each alert has been continuously open. The durable
    # open-set in `alert_events` (written by the awareness tick, 90d retention)
    # has carried `created_at` since WS-2 M10 with no production reader, so every
    # surface rendered instantaneous state and a multi-day outage read exactly
    # like a momentary one. Self-isolating: a failure here costs the duration
    # suffix, never the alert set.
    if _service is not None and _service._db is not None:
        try:
            from genesis.db.crud import alert_events as _ae_dur

            _open = {
                r.get("alert_id", ""): r for r in await _ae_dur.list_open(_service._db)
            }
            _apply_ongoing_duration(alerts, _open)
        except Exception:  # noqa: BLE001 - observability must not break on this
            # ERROR, not debug: nothing else reports this path failing, so a
            # debug-level swallow means durations silently stop forever.
            logger.error("could not attach ongoing durations to alerts", exc_info=True)

    return alerts, current_ids


async def _impl_health_alerts(active_only: bool = True) -> list[dict]:
    """Live alert list + the in-memory one-generation resolved rendering.

    Read path — contract unchanged for all existing callers (sentinel, morning
    report, dashboard, the health_alerts MCP tool). Durable persistence is NOT
    done here (that would multi-write across the runtime + health-MCP processes);
    it lives in the single awareness-tick ``alert_events`` writer (WS-2 M10).
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    alerts, current_ids = await _compute_alerts()
    _alert_history = health_mcp_mod._alert_history

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
            alerts.append(
                {
                    "id": resolved_id,
                    "severity": "RESOLVED",
                    "message": f"Previously active alert resolved at {resolved_at}",
                }
            )

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
