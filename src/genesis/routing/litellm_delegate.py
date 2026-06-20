"""LiteLLM-based CallDelegate — routes LLM calls via litellm.acompletion()."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import litellm

from genesis.routing.types import CallResult, ProviderConfig, RoutingConfig

logger = logging.getLogger(__name__)

# Rate-limit failure logging to avoid flooding when providers are permanently
# broken.  Log the first failure per provider, then suppress for 5 minutes.
_FAILURE_LOG_INTERVAL_S = 300
_last_failure_log: dict[str, float] = {}

# Default timeout for litellm.acompletion() calls (seconds).  Prevents
# indefinite hangs when a provider accepts TCP but stalls on response.
# litellm's own default is ambiguous (600s in completion(), 6000s global
# constant) and clearly failed to fire during a 20-minute production hang
# (deepseek-v4-pro via OpenRouter, 2026-06-08).  120s is generous for all
# Genesis call sites (max observed: ~1500 input / ~8K output tokens).
# Callers can override via kwargs if a specific call site needs longer.
_DEFAULT_TIMEOUT_S = 120


def _should_log_failure(provider: str) -> bool:
    now = time.monotonic()
    last = _last_failure_log.get(provider)
    # Log the FIRST failure per provider unconditionally (last is None means
    # we've never seen this provider). Previously used 0.0 as the "never
    # seen" sentinel, which silently suppressed the first failure when
    # process uptime was under _FAILURE_LOG_INTERVAL_S (e.g., fresh CI
    # runners): time.monotonic() is system-wide, not process-relative, so
    # the check `now - 0 >= 300` was False until 5 minutes of uptime had
    # elapsed. That broke the intended "log first failure, suppress for
    # 5 minutes" semantics on short-lived processes.
    if last is None or now - last >= _FAILURE_LOG_INTERVAL_S:
        _last_failure_log[provider] = now
        return True
    return False

# Maps our config 'type' field to LiteLLM model prefix.
# See https://docs.litellm.ai/docs/providers
_TYPE_TO_PREFIX: dict[str, str] = {
    "groq": "groq",
    "mistral": "mistral",
    "google": "gemini",
    "openai": "",
    "openrouter": "openrouter",
    "ollama": "ollama",
    "lmstudio": "openai",  # OpenAI-compatible with base_url
    "qwen": "openai",  # Alibaba — use OpenAI-compatible endpoint
    "glm": "openai",  # Zhipu — use OpenAI-compatible endpoint
    "zenmux": "openai",  # ZenMux — OpenAI-compatible aggregator
    "minimax": "openai",  # MiniMax — OpenAI-compatible with base_url
    "deepseek": "deepseek",  # DeepSeek — native LiteLLM support
    "xai": "xai",  # xAI/Grok — native LiteLLM support
    "cerebras": "cerebras",  # Cerebras — native LiteLLM support
    "github": "github",  # GitHub Models — native LiteLLM support (Azure-backed)
    "sambanova": "sambanova",  # SambaNova — native LiteLLM support
    "nvidia_nim": "nvidia_nim",  # NVIDIA NIM API Catalog — native LiteLLM support
}


# ── Custom model costs ──────────────────────────────────────────────────
# Models not yet in LiteLLM's built-in cost database.  Injected at import
# time so litellm.completion_cost() returns a real value instead of raising.
# Prices are per-token (NOT per million tokens).
# Source: https://openrouter.ai/deepseek (checked 2026-06-02)
_CUSTOM_MODEL_COSTS: dict[str, dict] = {
    # DeepSeek V4 Pro via OpenRouter — $0.435/$0.87 per MTok
    "openrouter/deepseek/deepseek-v4-pro": {
        "input_cost_per_token": 4.35e-7,
        "output_cost_per_token": 8.7e-7,
        "max_input_tokens": 131072,
        "max_output_tokens": 131072,
        "mode": "chat",
        "litellm_provider": "openrouter",
    },
    # DeepSeek V4 Flash via OpenRouter — $0.0983/$0.1966 per MTok
    "openrouter/deepseek/deepseek-v4-flash": {
        "input_cost_per_token": 9.83e-8,
        "output_cost_per_token": 1.966e-7,
        "max_input_tokens": 1048576,
        "max_output_tokens": 1048576,
        "mode": "chat",
        "litellm_provider": "openrouter",
    },
    # DeepSeek V4 Pro via NVIDIA NIM — free tier
    "nvidia_nim/deepseek-ai/deepseek-v4-pro": {
        "input_cost_per_token": 0.0,
        "output_cost_per_token": 0.0,
        "max_input_tokens": 131072,
        "max_output_tokens": 131072,
        "mode": "chat",
        "litellm_provider": "nvidia_nim",
    },
    # Response-model keys — OpenRouter's response.model omits the
    # provider prefix. Register these so litellm.completion_cost() can
    # look up costs even when it infers from response.model directly.
    # Assumption: litellm has no built-in cost entry for deepseek-v4-*
    # as of 2026-06-05 (litellm 1.78.7). Re-verify on litellm upgrades.
    "deepseek/deepseek-v4-pro": {
        "input_cost_per_token": 4.35e-7,
        "output_cost_per_token": 8.7e-7,
        "max_input_tokens": 131072,
        "max_output_tokens": 131072,
        "mode": "chat",
        "litellm_provider": "openrouter",
    },
    "deepseek/deepseek-v4-flash": {
        "input_cost_per_token": 9.83e-8,
        "output_cost_per_token": 1.966e-7,
        "max_input_tokens": 1048576,
        "max_output_tokens": 1048576,
        "mode": "chat",
        "litellm_provider": "openrouter",
    },
}

for _model, _cost in _CUSTOM_MODEL_COSTS.items():
    if _model not in litellm.model_cost:
        litellm.model_cost[_model] = _cost


# Module-level cache for the cost-fallback profile registry: loaded ONCE and
# shared across all delegate constructions. The production delegate is built once
# at startup, but the standalone/MCP, eval, and test paths build many — don't
# re-read the YAML (or re-warn) each time. Injected registries (tests) bypass it.
_PROFILE_REGISTRY = None
_PROFILE_REGISTRY_LOADED = False


def _load_profile_registry():
    """Load (once, cached) the model-profile registry for the cost-visibility
    fallback. Defensive: a missing/unreadable file caches None (cost stays
    "unknown"), never a delegate-construction failure."""
    global _PROFILE_REGISTRY, _PROFILE_REGISTRY_LOADED
    if _PROFILE_REGISTRY_LOADED:
        return _PROFILE_REGISTRY
    try:
        from genesis.env import repo_root
        from genesis.routing.model_profiles import ModelProfileRegistry

        registry = ModelProfileRegistry(repo_root() / "config" / "model_profiles.yaml")
        registry.load()
        _PROFILE_REGISTRY = registry
    except Exception:
        logger.warning("Could not load model_profiles for cost fallback", exc_info=True)
        _PROFILE_REGISTRY = None
    _PROFILE_REGISTRY_LOADED = True
    return _PROFILE_REGISTRY


class LiteLLMDelegate:
    """CallDelegate implementation using litellm.acompletion().

    Resolves API keys from environment variables loaded from secrets.env
    at startup via python-dotenv.
    """

    def __init__(self, config: RoutingConfig, *, profile_registry: object = None) -> None:
        self._config = config
        # Model-profile registry for the cost fallback (ROUTE-03 / ROUT-03):
        # when litellm.completion_cost can't price an aggregator model (the
        # OpenAI-compat-prefixed glm/minimax/qwen strings raise "model isn't
        # mapped yet"), compute cost from the provider's profile cost_per_mtok
        # instead of silently recording $0. Injected in tests; self-loaded from
        # config/model_profiles.yaml in production. Visibility ONLY — never
        # gates routing or budget ("cost tracking is observability, not control").
        self._profiles = (
            profile_registry if profile_registry is not None else _load_profile_registry()
        )

    def _cost_from_profile(self, cfg, usage) -> tuple[float, bool]:
        """Fallback cost from model_profiles cost_per_mtok when litellm can't
        price the model. Returns ``(cost_usd, cost_known)``."""
        if not getattr(cfg, "profile", None) or usage is None or self._profiles is None:
            return 0.0, False
        profile = self._profiles.get(cfg.profile)
        if profile is None:
            return 0.0, False
        in_tok = getattr(usage, "prompt_tokens", 0) or 0
        out_tok = getattr(usage, "completion_tokens", 0) or 0
        cost = (
            (in_tok / 1_000_000) * profile.cost_per_mtok_in
            + (out_tok / 1_000_000) * profile.cost_per_mtok_out
        )
        return cost, True

    async def call(
        self, provider: str, model_id: str, messages: list[dict], **kwargs
    ) -> CallResult:
        """Call a provider via litellm. Returns CallResult."""
        cfg = self._config.providers[provider]
        model_string = _build_model_string(cfg)
        api_key = _resolve_api_key(cfg.provider_type)

        call_kwargs = {**kwargs}
        if "timeout" not in call_kwargs:
            call_kwargs["timeout"] = _DEFAULT_TIMEOUT_S
        # Genesis's router owns retry + provider fallback. litellm must make
        # exactly ONE attempt — its internal retries stack on top of the
        # per-attempt timeout (observed: 3 retries × 120s ≈ 361s on a 120s
        # timeout, PR #582). Caller can still override.
        call_kwargs.setdefault("num_retries", 0)
        if api_key:
            call_kwargs["api_key"] = api_key
        if cfg.base_url:
            call_kwargs["api_base"] = cfg.base_url
        if cfg.keep_alive is not None and cfg.provider_type == "ollama":
            call_kwargs["keep_alive"] = cfg.keep_alive

        # Hard wall-clock ceiling around the whole call. litellm's own
        # ``timeout`` param has been observed not to fire (PR #582); this
        # asyncio.wait_for cap guarantees the call is cancelled even if
        # litellm/httpx ignore it. Belt-and-suspenders with num_retries=0.
        hard_timeout = call_kwargs["timeout"]
        try:
            response = await asyncio.wait_for(
                litellm.acompletion(
                    model=model_string,
                    messages=messages,
                    drop_params=True,
                    **call_kwargs,
                ),
                timeout=hard_timeout,
            )
            content = response.choices[0].message.content
            usage = getattr(response, "usage", None)
            # Extract provider-level prompt cache info when available
            cache_read = 0
            if usage:
                details = getattr(usage, "prompt_tokens_details", None)
                if details:
                    cache_read = getattr(details, "cached_tokens", 0) or 0
            if cfg.is_free:
                cost = 0.0
                cost_known = True
            else:
                cost_known = True
                try:
                    cost = litellm.completion_cost(
                        completion_response=response, model=model_string,
                    )
                except Exception:
                    # litellm can't price this model (aggregator strings not in
                    # its DB). Fall back to the provider's model_profiles cost so
                    # spend stays visible, not silently $0 (ROUTE-03 / ROUT-03).
                    cost, cost_known = self._cost_from_profile(cfg, usage)
                    if cost_known:
                        logger.debug(
                            "litellm couldn't price %s/%s; used model_profiles "
                            "fallback: $%.6f", provider, model_string, cost,
                        )
                    elif _should_log_failure(provider):
                        logger.warning(
                            "Cost calculation failed for %s/%s — recording as $0.00",
                            provider, model_string, exc_info=True,
                        )
            return CallResult(
                success=True,
                content=content,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cache_read_tokens=cache_read,
                cost_usd=cost,
                cost_known=cost_known,
            )
        except TimeoutError:
            # Hard wall-clock cap fired — the provider hung past hard_timeout.
            # Returned as 408 so the router classifies it TIMEOUT and fails
            # fast to the next provider (no same-provider retry).
            if _should_log_failure(provider):
                logger.warning(
                    "Provider %s hard-timed out after %ss (asyncio.wait_for cap)",
                    provider, hard_timeout,
                )
            return CallResult(
                success=False,
                error=f"litellm call exceeded hard timeout of {hard_timeout}s",
                status_code=408,
            )
        except litellm.RateLimitError as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s rate-limited: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=429)
        except litellm.AuthenticationError as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s auth failed: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=401)
        except litellm.NotFoundError as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s model not found: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=404)
        except litellm.Timeout as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s timed out: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=408)
        except litellm.ServiceUnavailableError as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s unavailable: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=503)
        except litellm.BadRequestError as e:
            # 400-family: context-window-exceeded, content-policy, malformed
            # request. ContextWindowExceededError + ContentPolicyViolationError
            # subclass BadRequestError, so this catches them too. Deterministic:
            # the router classifies BAD_REQUEST, fails fast to the next provider,
            # and does NOT trip the breaker (it's our payload, not the provider).
            if _should_log_failure(provider):
                logger.warning("Provider %s bad request: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=400)
        except litellm.UnprocessableEntityError as e:
            if _should_log_failure(provider):
                logger.warning("Provider %s unprocessable request: %s", provider, e)
            return CallResult(success=False, error=str(e), status_code=422)
        except Exception as e:
            raw_status = getattr(e, "status_code", None)
            status = raw_status if raw_status is not None else 500
            if _should_log_failure(provider):
                logger.exception("Unexpected error calling %s", provider)
            return CallResult(success=False, error=str(e), status_code=status)


def _build_model_string(cfg: ProviderConfig) -> str:
    """Build the litellm model string from provider config."""
    prefix = _TYPE_TO_PREFIX.get(cfg.provider_type, "")
    if prefix:
        return f"{prefix}/{cfg.model_id}"
    return cfg.model_id


def _resolve_api_key(provider_type: str) -> str | None:
    """Resolve API key from environment variables.

    Checks three naming patterns for flexibility:
    API_KEY_{SERVICE}, {SERVICE}_API_KEY, {SERVICE}_API_TOKEN
    """
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
