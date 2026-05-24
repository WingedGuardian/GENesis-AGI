"""API key health snapshot and validation."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.routing.types import RoutingConfig

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_LOCAL_TYPES = frozenset({"ollama", "lmstudio"})
# Anthropic providers require ANTHROPIC_API_KEY when configured as direct
# API providers (via LiteLLM).  CC-dispatched call sites (dispatch=cli)
# bypass the provider chain entirely and work regardless.  Previously
# "anthropic" was exempted here, which caused phantom circuit-breaker
# failures: providers registered without keys, failed every API call,
# and counted as "down" — triggering false L2 resilience state.
_CC_MANAGED_TYPES: frozenset[str] = frozenset()


_api_validation_cache: dict[str, dict] = {}


def has_api_key(provider_cfg) -> bool:
    """Check if a provider has a non-empty API key in the environment.

    Local providers (ollama, lmstudio) always return True — they don't
    need cloud API keys.  Used by the config loader to auto-disable
    providers that have no credentials configured.
    """
    ptype = provider_cfg.provider_type
    if ptype in _LOCAL_TYPES:
        return True
    if ptype in _CC_MANAGED_TYPES:
        return True  # CC handles auth — no API key needed
    service = ptype.upper()
    for pattern in [f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN"]:
        val = os.environ.get(pattern)
        if val and val not in ("None", "NA", ""):
            return True
    return False


def api_key_health(
    routing_config: RoutingConfig | None,
    breakers: object | None = None,
) -> dict:
    """Provider API key health with chain-aware criticality and CB state.

    Returns ``{"providers": {name: entry}, "alerts": [...]}``.

    Each provider entry includes the original fields (status, provider_type)
    plus chain_count, criticality, is_free, cb_state, cb_reason, and
    alert_severity — derived from the routing config and circuit breakers.

    The ``alerts`` list contains pre-computed attention-strip items for
    critical/warning conditions (credit exhaustion, missing critical keys).
    """
    if not routing_config:
        known_keys = {
            "groq": "API_KEY_GROQ",
            "mistral": "API_KEY_MISTRAL",
            "openrouter": "API_KEY_OPENROUTER",
            "deepseek": "API_KEY_DEEPSEEK",
            "google": "GOOGLE_API_KEY",
        }
        results: dict = {}
        for name, env_var in known_keys.items():
            val = os.environ.get(env_var)
            if val and val not in ("None", "NA", ""):
                cached = _api_validation_cache.get(name)
                if cached and cached.get("valid"):
                    results[name] = {"status": "validated", "provider_type": name}
                elif cached:
                    results[name] = {"status": "failed", "provider_type": name, "error": cached.get("error", "")}
                else:
                    results[name] = {"status": "configured", "provider_type": name}
            else:
                results[name] = {"status": "missing", "provider_type": name}
        return {"providers": results, "alerts": []}

    # Compute criticality per provider type
    from genesis.routing.provider_criticality import derive_criticality

    crit_map = derive_criticality(routing_config)

    # Aggregate CB state per provider type
    cb_by_type: dict[str, tuple[str, str | None]] = {}
    if breakers:
        registry = getattr(breakers, "_breakers", None) or {}
        if isinstance(breakers, dict):
            registry = breakers
        for prov_name, cb in registry.items():
            pcfg = routing_config.providers.get(prov_name)
            if not pcfg:
                continue
            ptype = pcfg.provider_type
            state_str = str(cb.state.value) if hasattr(cb.state, "value") else str(cb.state)
            reason = None
            if cb.last_failure_category is not None:
                reason = cb.last_failure_category.value if hasattr(cb.last_failure_category, "value") else str(cb.last_failure_category)

            # Keep worst state per type (open > half_open > closed)
            _SEVERITY = {"open": 3, "half_open": 2, "closed": 1}
            existing = cb_by_type.get(ptype)
            if existing is None or _SEVERITY.get(state_str, 0) > _SEVERITY.get(existing[0], 0):
                cb_by_type[ptype] = (state_str, reason)

    results = {}
    for name, provider_cfg in routing_config.providers.items():
        ptype = provider_cfg.provider_type
        if ptype in _LOCAL_TYPES:
            results[name] = {"status": "local", "provider_type": ptype}
            continue
        if ptype in _CC_MANAGED_TYPES:
            results[name] = {"status": "cc_managed", "provider_type": ptype}
            continue
        service = ptype.upper()
        key = None
        for pattern in [f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN"]:
            val = os.environ.get(pattern)
            if val and val not in ("None", "NA", ""):
                key = val
                break
        entry: dict = {"provider_type": ptype}
        if not key:
            entry["status"] = "missing"
        else:
            cached = _api_validation_cache.get(ptype)
            if cached:
                if cached.get("valid"):
                    entry["status"] = "validated"
                else:
                    entry["status"] = "failed"
                    entry["error"] = cached.get("error", "validation failed")
                entry["validated_at"] = cached.get("checked_at")
            else:
                entry["status"] = "configured"

        # Enrich with criticality + CB state
        crit_info = crit_map.get(ptype, {})
        entry["chain_count"] = crit_info.get("chain_count", 0)
        entry["chain_usage"] = crit_info.get("chain_usage", [])
        entry["criticality"] = crit_info.get("criticality", "dormant")
        entry["is_free"] = crit_info.get("is_free", False)
        entry["sole_sites"] = crit_info.get("sole_sites", [])

        cb_state, cb_reason = cb_by_type.get(ptype, ("closed", None))
        entry["cb_state"] = cb_state
        entry["cb_reason"] = cb_reason

        entry["alert_severity"] = _compute_alert_severity(
            status=entry["status"],
            criticality=entry["criticality"],
            is_free=entry["is_free"],
            cb_state=cb_state,
            cb_reason=cb_reason,
        )
        results[name] = entry

    # Include disabled providers as dormant
    for name, ptype in getattr(routing_config, "disabled_providers", {}).items():
        if name not in results:
            crit_info = crit_map.get(ptype, {})
            results[name] = {
                "status": "missing",
                "provider_type": ptype,
                "chain_count": crit_info.get("chain_count", 0),
                "chain_usage": crit_info.get("chain_usage", []),
                "criticality": crit_info.get("criticality", "dormant"),
                "is_free": crit_info.get("is_free", False),
                "sole_sites": crit_info.get("sole_sites", []),
                "cb_state": "closed",
                "cb_reason": None,
                "alert_severity": _compute_alert_severity(
                    status="missing",
                    criticality=crit_info.get("criticality", "dormant"),
                    is_free=crit_info.get("is_free", False),
                    cb_state="closed",
                    cb_reason=None,
                ),
            }

    # Build attention-strip alerts
    alerts = _build_alerts(results)

    return {"providers": results, "alerts": alerts}


def _compute_alert_severity(
    *,
    status: str,
    criticality: str,
    is_free: bool,
    cb_state: str,
    cb_reason: str | None,
) -> str | None:
    """Compute alert severity from the intersection of criticality and state.

    Returns "critical", "warning", "info", or None.
    """
    if criticality == "dormant":
        return None

    is_exhausted = cb_state == "open" and cb_reason == "quota_exhausted"
    is_cb_open = cb_state == "open"
    is_missing = status == "missing"

    if is_exhausted and not is_free:
        return "critical" if criticality in ("sole", "systemic") else "warning"

    if is_cb_open and not is_free:
        return "critical" if criticality == "sole" else "warning"

    if is_cb_open and is_free:
        return "warning" if criticality == "sole" else "info"

    if is_missing and not is_free:
        return "warning" if criticality in ("sole", "systemic") else "info"

    if is_missing and is_free:
        return "info"

    return None


def _build_alerts(providers: dict) -> list[dict]:
    """Build attention-strip alerts from enriched provider entries."""
    alerts: list[dict] = []
    # Group by provider_type to avoid duplicate alerts for same type
    seen_types: set[str] = set()
    for name, info in providers.items():
        severity = info.get("alert_severity")
        if severity not in ("critical", "warning"):
            continue
        ptype = info.get("provider_type", name)
        if ptype in seen_types:
            continue
        seen_types.add(ptype)

        cb_reason = info.get("cb_reason")
        chain_count = info.get("chain_count", 0)

        if cb_reason == "quota_exhausted":
            reason = "credit_exhaustion"
            message = f"{ptype.title()} credits depleted — {chain_count} call site(s) affected"
        elif info.get("cb_state") == "open":
            reason = "provider_down"
            message = f"{ptype.title()} down (circuit breaker open) — {chain_count} call site(s) affected"
        elif info.get("status") == "missing":
            sole = info.get("sole_sites", [])
            reason = "missing_key"
            if sole:
                message = f"{ptype.title()} API key missing — sole provider for {sole[0]}"
            else:
                message = f"{ptype.title()} API key missing — {chain_count} call site(s) affected"
        else:
            continue

        alerts.append({
            "provider_type": ptype,
            "severity": severity,
            "reason": reason,
            "affected_sites": chain_count,
            "message": message,
        })

    # Sort: critical first, then warning
    alerts.sort(key=lambda a: (0 if a["severity"] == "critical" else 1, a["provider_type"]))
    return alerts


async def validate_api_keys(routing_config: RoutingConfig | None) -> None:
    """Test each provider's API key with a lightweight call. Cache results."""
    if not routing_config:
        return

    import httpx

    validators: dict[str, tuple[str, dict[str, str]]] = {}

    for _name, provider_cfg in routing_config.providers.items():
        ptype = provider_cfg.provider_type
        if ptype in _LOCAL_TYPES or ptype in _CC_MANAGED_TYPES or ptype in validators:
            continue
        service = ptype.upper()
        key = None
        for pattern in [f"API_KEY_{service}", f"{service}_API_KEY", f"{service}_API_TOKEN"]:
            val = os.environ.get(pattern)
            if val and val not in ("None", "NA", ""):
                key = val
                break
        if not key:
            continue

        base_url = provider_cfg.base_url
        if ptype == "groq":
            validators[ptype] = ("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"})
        elif ptype == "mistral":
            validators[ptype] = ("https://api.mistral.ai/v1/models", {"Authorization": f"Bearer {key}"})
        elif ptype == "openrouter":
            validators[ptype] = ("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {key}"})
        elif ptype == "deepseek":
            validators[ptype] = ("https://api.deepseek.com/v1/models", {"Authorization": f"Bearer {key}"})
        elif ptype == "google":
            validators[ptype] = (f"https://generativelanguage.googleapis.com/v1beta/models?key={key}", {})
        elif ptype == "zenmux":
            url = base_url or "https://zenmux.ai/api/v1"
            validators[ptype] = (f"{url}/models", {"Authorization": f"Bearer {key}"})
        elif ptype == "anthropic":
            validators[ptype] = ("https://api.anthropic.com/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"})

    now_iso = datetime.now(UTC).isoformat()
    async with httpx.AsyncClient(timeout=10.0) as client:
        for ptype, (url, headers) in validators.items():
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code < 400:
                    _api_validation_cache[ptype] = {
                        "valid": True, "checked_at": now_iso,
                    }
                else:
                    error_text = resp.text[:200] if resp.text else str(resp.status_code)
                    _api_validation_cache[ptype] = {
                        "valid": False, "checked_at": now_iso,
                        "error": f"HTTP {resp.status_code}: {error_text}",
                    }
            except httpx.RequestError as exc:
                _api_validation_cache[ptype] = {
                    "valid": False, "checked_at": now_iso,
                    "error": str(exc),
                }
            except Exception as exc:
                _api_validation_cache[ptype] = {
                    "valid": False, "checked_at": now_iso,
                    "error": str(exc),
                }
