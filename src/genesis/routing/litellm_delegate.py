"""LiteLLM-based CallDelegate — routes LLM calls via litellm.acompletion()."""

from __future__ import annotations

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


def _should_log_failure(provider: str) -> bool:
    now = time.monotonic()
    last = _last_failure_log.get(provider, 0.0)
    if now - last >= _FAILURE_LOG_INTERVAL_S:
        _last_failure_log[provider] = now
        return True
    return False

# Maps our config 'type' field to LiteLLM model prefix.
# See https://docs.litellm.ai/docs/providers
_TYPE_TO_PREFIX: dict[str, str] = {
    "anthropic": "anthropic",
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
}


class LiteLLMDelegate:
    """CallDelegate implementation using litellm.acompletion().

    Resolves API keys from environment variables loaded from secrets.env
    at startup via python-dotenv.
    """

    def __init__(self, config: RoutingConfig) -> None:
        self._config = config

    async def call(
        self, provider: str, model_id: str, messages: list[dict], **kwargs
    ) -> CallResult:
        """Call a provider via litellm. Returns CallResult."""
        cfg = self._config.providers[provider]
        model_string = _build_model_string(cfg)
        api_key = _resolve_api_key(cfg.provider_type)

        call_kwargs = {**kwargs}
        if api_key:
            call_kwargs["api_key"] = api_key
        if cfg.base_url:
            call_kwargs["api_base"] = cfg.base_url
        if cfg.keep_alive is not None and cfg.provider_type == "ollama":
            call_kwargs["keep_alive"] = cfg.keep_alive

        try:
            response = await litellm.acompletion(
                model=model_string,
                messages=messages,
                drop_params=True,
                **call_kwargs,
            )
            content = response.choices[0].message.content
            usage = getattr(response, "usage", None)
            cost_known = True
            try:
                cost = litellm.completion_cost(completion_response=response)
            except Exception:
                cost = 0.0
                cost_known = False
                if _should_log_failure(provider):
                    logger.warning(
                        "Cost calculation failed for %s/%s — recording as $0.00",
                        provider, model_string, exc_info=True,
                    )
            return CallResult(
                success=True,
                content=content,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cost_usd=cost,
                cost_known=cost_known,
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
