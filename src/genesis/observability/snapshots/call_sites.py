"""Call sites snapshot — circuit breaker health per routing call site."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from genesis.observability._call_site_meta import _CALL_SITE_META
from genesis.routing.types import ProviderState

if TYPE_CHECKING:
    import aiosqlite

    from genesis.resilience.state import ResilienceStateMachine
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.types import CallSiteConfig, RoutingConfig

logger = logging.getLogger(__name__)


def _derive_cost_policy(
    site_cfg: CallSiteConfig,
    routing_config: RoutingConfig,
) -> str:
    """Compute costPolicy from chain configuration.

    Returns a human-readable cost tier string derived from the actual
    provider chain, replacing hardcoded metadata that drifts from config.
    """
    if not site_cfg.chain:
        return "Not configured"
    if site_cfg.never_pays:
        return "Free only (never pays)"

    providers = []
    for name in site_cfg.chain:
        pcfg = routing_config.providers.get(name)
        if pcfg:
            providers.append(pcfg)

    if not providers:
        return "Not configured"

    all_free = all(p.is_free for p in providers)
    first_free = providers[0].is_free

    if all_free:
        return "Free"
    if first_free:
        return "Free primary, paid fallback"
    return f"Paid primary ({providers[0].name})"


async def call_sites(
    db: aiosqlite.Connection | None,
    routing_config: RoutingConfig | None,
    breakers: CircuitBreakerRegistry | None,
    *,
    probe_results: dict | None = None,
    state_machine: ResilienceStateMachine | None = None,
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
            # Add provider type + model for dashboard display names
            pcfg = routing_config.providers.get(provider_name)
            if pcfg:
                entry["type"] = pcfg.provider_type
                entry["model"] = pcfg.model_id
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
        # Derive costPolicy from chain config (CC-dispatched sites keep manual policy)
        dispatch = meta.get("dispatch") if meta else None
        if dispatch not in ("cc", "dual"):
            site_data["cost_policy"] = _derive_cost_policy(site_cfg, routing_config)
        result[site_id] = site_data

    if db:
        try:
            cursor = await db.execute(
                "SELECT call_site_id, last_run_at, provider_used, model_id,"
                " response_text, input_tokens, output_tokens, success"
                " FROM call_site_last_run"
            )
            for row in await cursor.fetchall():
                sid = row[0]
                run_data = {
                    "last_run_at": row[1],
                    "last_run_provider": row[2],
                    "last_run_model": row[3],
                    "last_response": row[4],
                    "last_run_tokens": (row[5] or 0) + (row[6] or 0),
                    "last_run_success": bool(row[7]) if row[7] is not None else True,
                }
                if sid in result:
                    result[sid].update(run_data)
                else:
                    result[sid] = {"status": "active", "routing": False, **run_data}
        except sqlite3.Error:
            logger.debug("call_site_last_run query failed", exc_info=True)

    # ── Groundwork sites → idle (gray) ──────────────────────────────────
    # Sites with wired=False and no last_run record are groundwork/ceremonial.
    for sid, site_data in result.items():
        meta = _CALL_SITE_META.get(sid)
        if meta and meta.get("wired") is False and not site_data.get("last_run_at"):
            site_data["status"] = "idle"

    # ── Resolve CC state for CC chain entries ───────────────────────────
    cc_cb_state = "closed"  # default: CC available
    cc_probe_status: str | None = None  # None = no CC state machine
    if state_machine:
        try:
            from genesis.resilience.state import CCStatus

            cc_state_value = state_machine.current.cc
            if cc_state_value == CCStatus.UNAVAILABLE:
                cc_cb_state = "open"
                cc_probe_status = "unreachable"
            elif cc_state_value in (CCStatus.RATE_LIMITED, CCStatus.THROTTLED):
                cc_cb_state = "half_open"
                cc_probe_status = "rate_limited"
            else:
                cc_probe_status = "reachable"
        except (AttributeError, ImportError):
            pass

    # ── Read YAML CC overrides (dashboard saves cc_model/position here) ──
    yaml_cc_overrides: dict[str, dict] = {}
    try:
        _yaml_path = Path(__file__).parent.parent.parent.parent / "config" / "model_routing.yaml"
        if _yaml_path.exists():
            _raw = yaml.safe_load(_yaml_path.read_text()) or {}
            for _csn, _csraw in (_raw.get("call_sites") or {}).items():
                if isinstance(_csraw, dict) and (_csraw.get("cc_model") or _csraw.get("cc_position") is not None):
                    yaml_cc_overrides[_csn] = _csraw
    except Exception:
        logger.debug("Failed to read CC overrides from YAML", exc_info=True)

    # ── Overlay probe_status + append CC entries + unified chain walk ──
    for sid, site_data in result.items():
        meta = _CALL_SITE_META.get(sid)
        yaml_ov = yaml_cc_overrides.get(sid, {})
        dispatch = yaml_ov.get("dispatch") or (meta.get("dispatch") if meta else None)
        chain = site_data.get("chain_health", [])

        # 1. Overlay probe_status on each API chain entry.
        #    Distinguish "not_configured" (no API key — permanent config state)
        #    from "unreachable" (transient outage). Conflating these causes
        #    Sentinel to treat missing-key providers as infrastructure failures.
        if probe_results:
            for entry in chain:
                probe = probe_results.get(entry["provider"])
                if probe is None:
                    continue  # no probe data → frontend falls back to CB state
                if not probe.configured:
                    entry["probe_status"] = "not_configured"
                    entry["probe_reason"] = "no_api_key"
                elif not probe.reachable:
                    entry["probe_status"] = "unreachable"
                elif probe.error == "rate limited":
                    entry["probe_status"] = "rate_limited"
                else:
                    entry["probe_status"] = "reachable"

        # 2. Insert CC entry for dual/cc dispatch sites
        if dispatch in ("dual", "cc"):
            cc_model = yaml_ov.get("cc_model") or (meta.get("cc_model", "?") if meta else "?")
            cc_entry: dict = {
                "provider": f"CC/{cc_model}",
                "state": cc_cb_state,
                "failures": 0,
                "is_cc": True,
            }
            if cc_probe_status is not None:
                cc_entry["probe_status"] = cc_probe_status
            # Insert at saved position, or append at end
            cc_pos = yaml_ov.get("cc_position")
            if cc_pos is not None and 0 <= cc_pos <= len(chain):
                chain.insert(cc_pos, cc_entry)
            else:
                chain.append(cc_entry)
            site_data["chain_health"] = chain

        # 3. Unified chain walk for site-level status
        #    Probe = display authority. CB = fallback when no probe data.
        #    Idle sites skip — they stay gray.
        if site_data.get("status") == "idle" or not chain:
            continue

        # Filter disabled providers (configured=False) before evaluating
        # site health — a provider without an API key is permanently
        # unavailable-by-config, not "down." It should not affect routing
        # authority decisions.
        active_chain = [c for c in chain if _provider_health(c) != "disabled"]

        if not active_chain:
            # Every provider in this chain is unconfigured. This is a
            # deployment-time config state, NOT an infrastructure alert.
            site_data["status"] = "disabled"
            site_data["disabled_reason"] = "no_api_keys_configured"
            continue

        prev_status = site_data.get("status")
        first_health = _provider_health(active_chain[0])
        if first_health == "up":
            # Preserve "warning" — DB found recent failures even though chain looks ok
            site_data["status"] = "warning" if prev_status == "warning" else "healthy"
        elif first_health == "suspect":
            site_data["status"] = "degraded"
        else:
            # First provider down — check fallbacks
            if any(_provider_health(c) in ("up", "suspect") for c in active_chain[1:]):
                site_data["status"] = "degraded"
            else:
                site_data["status"] = "down"

    return result


def _provider_health(entry: dict) -> str:
    """Most-negative-opinion of CB state and probe status wins.

    CB is the routing authority (controls whether traffic flows).
    Probe is the reachability check (can the endpoint respond?).
    If either says down, the provider is effectively down.

    Returns 'up', 'suspect', 'down', or 'disabled'.

    'disabled' is distinct from 'down': it means the provider has no API
    key configured in this deployment (permanent config state), not that
    it crashed. Callers should filter disabled providers out of routing
    decisions rather than treating them as failures.
    """
    # Probe says "no API key" — provider is disabled by config, not broken
    ps = entry.get("probe_status")
    if ps == "not_configured":
        return "disabled"
    # CB state checked first — router won't send traffic if CB is open
    state = entry.get("state", "closed")
    if state in ("open", "error"):
        return "down"
    if state == "half_open":
        return "suspect"
    # CB is closed — now check probe reachability
    if ps == "unreachable":
        return "down"
    if ps == "rate_limited":
        return "suspect"
    return "up"
