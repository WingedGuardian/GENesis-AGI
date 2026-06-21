"""Standalone Router-compatible wrapper for offline experiment/eval runs.

`LLMJudgeScorer` (and the experimentation runner's generation step) expect a
``Router`` exposing ``route_call(call_site_id, messages, **kwargs)``. The live
Genesis Router needs the full runtime (breakers, cost tracker, degradation,
delegate). For an *offline* experiment we only need a single model call.

This wraps Genesis's own ``LiteLLMDelegate`` (the same path `eval/runner.py`
uses) so provider→litellm mapping, API keys, and custom base-urls are handled
natively — selecting a model by its routing-config *provider name* rather than a
raw litellm model string. Mirrors the pattern in
`run_reflection_calibration._LiteLLMRouter` (which should be refactored onto this
— tracked as cleanup).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from genesis.routing.config import load_config
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.types import RoutingConfig

logger = logging.getLogger(__name__)

# Transient errors worth retrying (free-tier judges 429 readily). Mirrors the
# retry posture of `eval/runner.py`.
_RETRYABLE = (
    "rate limit", "ratelimit", "too many requests", "429", "503",
    "overloaded", "timeout", "connection", "temporarily",
)
_MAX_RETRIES = 2
_RETRY_BASE_S = 5.0

# Default offline providers (routing-config names). Generation + judging use
# DIFFERENT providers to avoid self-judging bias; the judge defaults to the
# deepseek family (closest available to the golden-set's calibrated judge).
DEFAULT_GEN_PROVIDER = "groq-free"
DEFAULT_JUDGE_PROVIDER = "nvidia-nim-deepseek"


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "model_routing.yaml"


@dataclass
class StandaloneRoutingResult:
    """Minimal RoutingResult shape consumed by `LLMJudgeScorer`."""

    success: bool
    content: str | None
    model_id: str | None
    provider_used: str | None
    error: str | None


class StandaloneLiteLLMRouter:
    """Minimal Router for offline use — one provider, no fallback chain.

    Args:
        provider_name: A provider key from ``model_routing.yaml``
            (e.g. ``"groq-free"``).
        config: Pre-loaded routing config (optional; loaded from the default
            path if omitted).
        delegate: A shared ``LiteLLMDelegate`` (optional; one is built from
            ``config`` if omitted — pass a shared instance to avoid duplicate
            delegates across the gen + judge routers).
    """

    def __init__(
        self,
        provider_name: str,
        *,
        config: RoutingConfig | None = None,
        delegate: LiteLLMDelegate | None = None,
    ) -> None:
        # Load secrets.env into the environment so litellm finds provider API
        # keys (the same hook the offline calibration CLI uses). Idempotent.
        from genesis.eval.reflection_golden_set import _ensure_secrets

        _ensure_secrets()

        if config is None:
            config = load_config(_default_config_path())
        provider_cfg = config.providers.get(provider_name)
        if provider_cfg is None:
            raise ValueError(
                f"unknown provider {provider_name!r} — available: "
                f"{', '.join(sorted(config.providers))}"
            )
        self._provider_name = provider_name
        self._model_id = provider_cfg.model_id
        self._delegate = delegate or LiteLLMDelegate(config)

    async def route_call(
        self,
        call_site_id: str,
        messages: list[dict],
        **kwargs,
    ) -> StandaloneRoutingResult:
        result = None
        for attempt in range(_MAX_RETRIES + 1):
            result = await self._delegate.call(
                provider=self._provider_name,
                model_id=self._model_id,
                messages=messages,
                **kwargs,
            )
            if result.success:
                break
            msg = (result.error or "").lower()
            if attempt < _MAX_RETRIES and any(k in msg for k in _RETRYABLE):
                delay = _RETRY_BASE_S * (2 ** attempt)
                logger.info(
                    "standalone %s via %s transient (%s); retry %d/%d in %.0fs",
                    call_site_id, self._provider_name, result.error,
                    attempt + 1, _MAX_RETRIES, delay,
                )
                await asyncio.sleep(delay)
                continue
            break

        if not result.success:
            logger.warning(
                "standalone route_call failed (%s via %s): %s",
                call_site_id, self._provider_name, result.error,
            )
        return StandaloneRoutingResult(
            success=result.success,
            content=result.content,
            model_id=self._model_id,
            provider_used=self._provider_name,
            error=getattr(result, "error", None),
        )

    async def close(self) -> None:
        """No persistent resources to release (litellm manages its own)."""
