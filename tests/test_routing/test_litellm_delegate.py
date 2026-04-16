"""Tests for LiteLLMDelegate — real LLM calls via litellm."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from genesis.routing.litellm_delegate import (
    LiteLLMDelegate,
    _build_model_string,
    _resolve_api_key,
)
from genesis.routing.types import ProviderConfig, RoutingConfig


def _config(
    provider_type="groq",
    model_id="llama-3.3-70b-versatile",
    base_url=None,
) -> RoutingConfig:
    """Minimal config with one provider."""
    return RoutingConfig(
        providers={
            "test-provider": ProviderConfig(
                name="test-provider",
                provider_type=provider_type,
                model_id=model_id,
                is_free=True,
                rpm_limit=30,
                open_duration_s=120,
                base_url=base_url,
            ),
        },
        call_sites={},
        retry_profiles={},
    )


def _mock_response(content="Hello", prompt_tokens=10, completion_tokens=5):
    """Build a fake litellm response."""
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage)


# ── Model string construction ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider_type", "model_id", "expected"),
    [
        ("groq", "llama-3.3-70b-versatile", "groq/llama-3.3-70b-versatile"),
        ("anthropic", "claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"),
        ("google", "gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("openai", "gpt-5-nano", "gpt-5-nano"),  # no prefix
        ("ollama", "qwen2.5:3b", "ollama/qwen2.5:3b"),
        ("lmstudio", "TBD", "openai/TBD"),
        ("openrouter", "best-free", "openrouter/best-free"),
        ("mistral", "mistral-large-latest", "mistral/mistral-large-latest"),
    ],
)
def test_build_model_string(provider_type, model_id, expected):
    cfg = ProviderConfig(
        name="p", provider_type=provider_type, model_id=model_id,
        is_free=True, rpm_limit=None, open_duration_s=120,
    )
    assert _build_model_string(cfg) == expected


# ── API key resolution ─────────────────────────────────────────────────────


def test_resolve_api_key_primary_pattern():
    with patch.dict(os.environ, {"API_KEY_GROQ": "grok-key-123"}, clear=False):
        assert _resolve_api_key("groq") == "grok-key-123"


def test_resolve_api_key_secondary_pattern():
    with patch.dict(os.environ, {"GROQ_API_KEY": "secondary-key"}, clear=False):
        key = _resolve_api_key("groq")
        # Could match either pattern depending on what's set
        assert key is not None


def test_resolve_api_key_ignores_placeholders():
    with patch.dict(os.environ, {"API_KEY_GROQ": "None"}, clear=False):
        # Should skip "None" placeholder
        result = _resolve_api_key("groq")
        # Result depends on whether other patterns match
        assert result is None or result != "None"


def test_resolve_api_key_not_found():
    env = {k: v for k, v in os.environ.items() if "MISSING_PROVIDER" not in k}
    with patch.dict(os.environ, env, clear=True):
        assert _resolve_api_key("missing_provider") is None


# ── Successful call ────────────────────────────────────────────────────────


async def test_call_success():
    config = _config()
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response(content="Test output", prompt_tokens=15, completion_tokens=8)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0003

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hello"}],
        )

    assert result.success is True
    assert result.content == "Test output"
    assert result.input_tokens == 15
    assert result.output_tokens == 8
    assert result.cost_usd == 0.0003


async def test_call_passes_model_string():
    config = _config(provider_type="google", model_id="gemini-2.5-flash")
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "gemini-2.5-flash",
            [{"role": "user", "content": "Hi"}],
        )

        call_args = mock_litellm.acompletion.call_args
        assert call_args.kwargs["model"] == "gemini/gemini-2.5-flash"


async def test_call_passes_base_url():
    config = _config(
        provider_type="lmstudio", model_id="TBD",
        base_url="http://${LM_STUDIO_HOST:-localhost:1234}/v1",
    )
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "TBD",
            [{"role": "user", "content": "Hi"}],
        )

        call_args = mock_litellm.acompletion.call_args
        assert call_args.kwargs["api_base"] == "http://${LM_STUDIO_HOST:-localhost:1234}/v1"


async def test_call_passes_api_key():
    config = _config()
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with (
        patch.dict(os.environ, {"API_KEY_GROQ": "test-key-xyz"}, clear=False),
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
    ):
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

        call_args = mock_litellm.acompletion.call_args
        assert call_args.kwargs["api_key"] == "test-key-xyz"


# ── Error classification ───────────────────────────────────────────────────


async def test_call_rate_limit_error():
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.RateLimitError("rate limited"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 429


async def test_call_auth_error():
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.AuthenticationError("bad key"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 401


async def test_call_timeout_error():
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.Timeout("timed out"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 408


async def test_call_generic_error():
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=RuntimeError("something broke"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 500
    assert "something broke" in result.error


# ── Cost extraction fallback ───────────────────────────────────────────────


async def test_call_cost_extraction_fallback():
    """If completion_cost raises, cost_usd should be 0.0 not an exception."""
    config = _config()
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.side_effect = Exception("unknown model")

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is True
    assert result.cost_usd == 0.0


# ── Provider failure logging ──────────────────────────────────────────────


async def test_auth_error_logs_warning():
    """Auth errors must produce a log entry (not be silently swallowed)."""
    import genesis.routing.litellm_delegate as mod
    mod._last_failure_log.clear()  # Reset rate limiter for test isolation

    config = _config()
    delegate = LiteLLMDelegate(config)

    with (
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
        patch("genesis.routing.litellm_delegate.logger") as mock_logger,
    ):
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.AuthenticationError("bad key"),
        )

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    mock_logger.warning.assert_called_once()
    args = mock_logger.warning.call_args[0]
    assert "auth failed" in args[0]


async def test_rate_limit_logs_warning():
    import genesis.routing.litellm_delegate as mod
    mod._last_failure_log.clear()  # Reset rate limiter for test isolation

    config = _config()
    delegate = LiteLLMDelegate(config)

    with (
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
        patch("genesis.routing.litellm_delegate.logger") as mock_logger,
    ):
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.RateLimitError("rate limited"),
        )

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    mock_logger.warning.assert_called_once()
    args = mock_logger.warning.call_args[0]
    assert "rate-limited" in args[0]


def test_should_log_failure_first_call_regardless_of_monotonic_value():
    """Regression: first _should_log_failure call must log even when
    time.monotonic() returns a small value (e.g., fresh CI runner with
    system uptime < _FAILURE_LOG_INTERVAL_S).

    Previously `_last_failure_log.get(provider, 0.0)` meant the check
    `now - 0.0 >= 300` was False on fresh systems, silently suppressing
    the first failure log. Fixed by using None as the "never seen" sentinel
    (commit cf71db2). This test proves the invariant independently of host
    uptime.
    """
    import genesis.routing.litellm_delegate as mod

    mod._last_failure_log.clear()

    with patch("genesis.routing.litellm_delegate.time.monotonic", return_value=10.0):
        # now=10.0, no entry for "fresh-provider" → last is None → must log
        assert mod._should_log_failure("fresh-provider") is True
        # Second call at same "time" — now entry exists, interval not elapsed → suppress
        assert mod._should_log_failure("fresh-provider") is False

    # After the interval elapses, should log again
    with patch(
        "genesis.routing.litellm_delegate.time.monotonic",
        return_value=10.0 + mod._FAILURE_LOG_INTERVAL_S + 1.0,
    ):
        assert mod._should_log_failure("fresh-provider") is True


async def test_failure_logging_rate_limited():
    """Second failure within 5 minutes should NOT log."""
    import genesis.routing.litellm_delegate as mod
    mod._last_failure_log.clear()

    config = _config()
    delegate = LiteLLMDelegate(config)

    with (
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
        patch("genesis.routing.litellm_delegate.logger") as mock_logger,
    ):
        mock_litellm.RateLimitError = type("RateLimitError", (Exception,), {})
        mock_litellm.AuthenticationError = type("AuthenticationError", (Exception,), {})
        mock_litellm.NotFoundError = type("NotFoundError", (Exception,), {})
        mock_litellm.Timeout = type("Timeout", (Exception,), {})
        mock_litellm.ServiceUnavailableError = type("ServiceUnavailableError", (Exception,), {})
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.AuthenticationError("bad key"),
        )

        # First call — should log
        await delegate.call("test-provider", "m", [{"role": "user", "content": "a"}])
        assert mock_logger.warning.call_count == 1

        # Second call — should be suppressed
        await delegate.call("test-provider", "m", [{"role": "user", "content": "b"}])
        assert mock_logger.warning.call_count == 1  # Still 1, not 2
