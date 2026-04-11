"""Provider health probes — periodic /v1/models endpoint checks.

Probes each unique LLM provider's models-listing endpoint to confirm
reachability, API key validity, and model availability. All probes are
free (no tokens consumed). Results feed into call_sites snapshot to
replace circuit-breaker-default "healthy" with confirmed status.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.types import ProviderConfig, RoutingConfig

logger = logging.getLogger(__name__)

# Base URLs for providers where LiteLLM manages the endpoint internally.
# Providers with base_url in RoutingConfig use that instead.
_PROVIDER_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com/v1/models",
    "groq": "https://api.groq.com/openai/v1/models",
    "mistral": "https://api.mistral.ai/v1/models",
    "google": "https://generativelanguage.googleapis.com/v1beta/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
    "deepseek": "https://api.deepseek.com/v1/models",
    "openai": "https://api.openai.com/v1/models",
    "xai": "https://api.x.ai/v1/models",
}


def _resolve_api_key(provider_type: str) -> str | None:
    """Resolve API key from environment — mirrors litellm_delegate pattern."""
    service = provider_type.upper()
    for pattern in [
        f"API_KEY_{service}",
        f"{service}_API_KEY",
        f"{service}_API_TOKEN",
    ]:
        val = os.environ.get(pattern)
        if val and val not in ("None", "NA", ""):
            return val
    return None


def _sanitize_url(url: str) -> str:
    """Strip query parameters from URL to prevent API key leakage in logs."""
    return url.split("?")[0] if "?" in url else url


@dataclass(frozen=True)
class ProviderProbeResult:
    """Result of probing a single provider endpoint."""

    provider_name: str
    reachable: bool
    configured: bool = True  # False when no API key / probe URL found
    model_available: bool | None = None  # None = couldn't verify
    latency_ms: float = 0.0
    error: str | None = None
    checked_at: str = ""
    _models: frozenset[str] = field(default_factory=frozenset, repr=False)


class ProviderHealthChecker:
    """Probes provider /v1/models endpoints to confirm reachability.

    All probes are free — they hit models-listing endpoints, not completion
    endpoints. Results are cached with a configurable TTL.
    """

    def __init__(
        self,
        routing_config: RoutingConfig,
        *,
        breakers: CircuitBreakerRegistry | None = None,
        ttl_s: float = 300.0,
    ) -> None:
        self._config = routing_config
        self._breakers = breakers
        self._results: dict[str, ProviderProbeResult] = {}
        self._ttl_s = ttl_s
        self._last_probe_at = 0.0

    @property
    def results(self) -> dict[str, ProviderProbeResult]:
        """Current cached probe results (frozen, safe to share)."""
        return dict(self._results)

    def is_stale(self) -> bool:
        """True if cache is older than TTL."""
        return (time.monotonic() - self._last_probe_at) > self._ttl_s

    async def probe_all(self) -> dict[str, ProviderProbeResult]:
        """Probe all unique provider types concurrently.

        Deduplicates by provider_type — only one probe per type, then
        distributes the result to all providers of that type.
        """
        # Group providers by type
        type_to_providers: dict[str, list[ProviderConfig]] = {}
        for cfg in self._config.providers.values():
            if not cfg.enabled:
                continue
            type_to_providers.setdefault(cfg.provider_type, []).append(cfg)

        # Probe one representative per type concurrently
        ptypes = list(type_to_providers.keys())
        coros = [
            self._probe_one(type_to_providers[ptype][0]) for ptype in ptypes
        ]
        raw_results = await asyncio.gather(*coros, return_exceptions=True)

        # Distribute results: each provider gets its type's probe result
        for ptype, raw in zip(ptypes, raw_results, strict=True):
            if isinstance(raw, BaseException):
                base_result = ProviderProbeResult(
                    provider_name="",
                    reachable=False,
                    error=str(raw)[:120],
                    checked_at=datetime.now(UTC).isoformat(),
                )
            else:
                base_result = raw

            for cfg in type_to_providers[ptype]:
                # Per-provider: check if THIS provider's model is available
                model_ok = base_result.model_available
                if base_result.reachable and base_result._models:
                    model_ok = cfg.model_id in base_result._models
                self._results[cfg.name] = ProviderProbeResult(
                    provider_name=cfg.name,
                    reachable=base_result.reachable,
                    configured=base_result.configured,
                    model_available=model_ok,
                    latency_ms=base_result.latency_ms,
                    error=base_result.error,
                    checked_at=base_result.checked_at,
                )

        self._last_probe_at = time.monotonic()
        self._sync_to_breakers()
        return dict(self._results)

    def _sync_to_breakers(self) -> None:
        """Push probe findings to circuit breakers.

        Unreachable or rate-limited providers get tripped to HALF_OPEN
        (probe = scout, CB = judge). Only downgrades — never closes a CB.
        """
        if not self._breakers:
            return
        for name, result in self._results.items():
            if not result.configured:
                continue  # No API key — don't trip CB for missing config
            if not result.reachable or result.error == "rate limited":
                try:
                    cb = self._breakers.get(name)
                    cb.probe_suspect()
                except (KeyError, OSError):
                    logger.debug("CB sync failed for probed provider %s", name, exc_info=True)
                except Exception:
                    logger.debug("Unexpected CB sync error for %s", name, exc_info=True)

    async def _probe_one(self, cfg: ProviderConfig) -> ProviderProbeResult:
        """Probe a single provider's models endpoint."""
        url = self._resolve_url(cfg)
        if not url:
            return ProviderProbeResult(
                provider_name=cfg.name,
                reachable=False,
                configured=False,
                error="no API key configured",
                checked_at=datetime.now(UTC).isoformat(),
            )

        headers = self._resolve_headers(cfg)
        safe_url = _sanitize_url(url)
        start = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=5)
            async with (
                aiohttp.ClientSession(timeout=timeout) as session,
                session.get(url, headers=headers) as resp,
            ):
                latency = (time.monotonic() - start) * 1000
                if resp.status == 200:
                    models = await self._extract_models(resp, cfg.provider_type)
                    return ProviderProbeResult(
                        provider_name=cfg.name,
                        reachable=True,
                        model_available=cfg.model_id in models if models else None,
                        latency_ms=round(latency, 1),
                        checked_at=datetime.now(UTC).isoformat(),
                        _models=frozenset(models),
                    )
                if resp.status == 429:
                    return ProviderProbeResult(
                        provider_name=cfg.name,
                        reachable=True,
                        error="rate limited",
                        latency_ms=round(latency, 1),
                        checked_at=datetime.now(UTC).isoformat(),
                    )
                return ProviderProbeResult(
                    provider_name=cfg.name,
                    reachable=False,
                    error=f"HTTP {resp.status} from {safe_url}",
                    latency_ms=round(latency, 1),
                    checked_at=datetime.now(UTC).isoformat(),
                )
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            latency = (time.monotonic() - start) * 1000
            return ProviderProbeResult(
                provider_name=cfg.name,
                reachable=False,
                error=f"{type(exc).__name__}: {safe_url}",
                latency_ms=round(latency, 1),
                checked_at=datetime.now(UTC).isoformat(),
            )

    def _resolve_url(self, cfg: ProviderConfig) -> str | None:
        """Get the probe URL for a provider."""
        ptype = cfg.provider_type

        # Ollama has a different endpoint
        if ptype == "ollama":
            base = cfg.base_url or os.environ.get(
                "OLLAMA_URL", "http://localhost:11434"
            )
            return f"{base.rstrip('/')}/api/tags"

        # Providers with explicit base_url in config
        if cfg.base_url:
            if ptype != "ollama" and not _resolve_api_key(ptype):
                return None  # no API key configured, skip probe
            return f"{cfg.base_url.rstrip('/')}/models"

        # Google uses query-param auth
        if ptype == "google":
            key = _resolve_api_key(ptype)
            if not key:
                return None
            base = _PROVIDER_URLS["google"]
            return f"{base}?key={key}"

        # LiteLLM-managed providers — need API key to be worthwhile
        url = _PROVIDER_URLS.get(ptype)
        if url and not _resolve_api_key(ptype):
            return None  # no key configured, skip
        return url

    def _resolve_headers(self, cfg: ProviderConfig) -> dict[str, str]:
        """Build auth headers for the probe request."""
        key = _resolve_api_key(cfg.provider_type)
        if not key:
            return {}
        if cfg.provider_type == "anthropic":
            return {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
            }
        if cfg.provider_type == "google":
            return {}  # key in URL query param
        return {"Authorization": f"Bearer {key}"}

    @staticmethod
    async def _extract_models(
        resp: aiohttp.ClientResponse, provider_type: str
    ) -> set[str]:
        """Extract model IDs from a /v1/models response."""
        try:
            data = await resp.json()
        except (aiohttp.ContentTypeError, ValueError, UnicodeDecodeError):
            return set()

        # Ollama: {"models": [{"name": "..."}]}
        if provider_type == "ollama":
            return {m.get("name", "") for m in data.get("models", [])}

        # Google: {"models": [{"name": "models/gemini-..."}]}
        if provider_type == "google":
            return {
                m.get("name", "").removeprefix("models/")
                for m in data.get("models", [])
            }

        # OpenAI-compatible: {"data": [{"id": "..."}]}
        return {m.get("id", "") for m in data.get("data", [])}
