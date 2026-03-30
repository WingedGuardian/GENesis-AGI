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
_CC_MANAGED_TYPES = frozenset({"anthropic"})


_api_validation_cache: dict[str, dict] = {}


def api_key_health(routing_config: RoutingConfig | None) -> dict:
    """Check which configured providers have API keys present + validation status."""
    if not routing_config:
        known_keys = {
            "groq": "API_KEY_GROQ",
            "mistral": "API_KEY_MISTRAL",
            "openrouter": "API_KEY_OPENROUTER",
            "deepseek": "API_KEY_DEEPSEEK",
            "google": "GOOGLE_API_KEY",
        }
        results = {}
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
        return results

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
        results[name] = entry
    return results


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
