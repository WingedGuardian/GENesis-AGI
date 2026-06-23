"""Tests for LiteLLMDelegate — real LLM calls via litellm."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from genesis.routing.litellm_delegate import (
    _DEFAULT_TIMEOUT_S,
    LiteLLMDelegate,
    _build_model_string,
    _resolve_api_key,
)
from genesis.routing.types import ProviderConfig, RoutingConfig


def _config(
    provider_type="groq",
    model_id="llama-3.3-70b-versatile",
    base_url=None,
    is_free=True,
) -> RoutingConfig:
    """Minimal config with one provider."""
    return RoutingConfig(
        providers={
            "test-provider": ProviderConfig(
                name="test-provider",
                provider_type=provider_type,
                model_id=model_id,
                is_free=is_free,
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


def _install_litellm_exceptions(mock_litellm):
    """Install real exception classes on a MagicMock'd litellm so the delegate's
    ``except litellm.X`` chain evaluates. An un-set Mock attribute used in an
    ``except`` clause raises ``TypeError: catching classes that do not inherit
    from BaseException``."""
    for _name in (
        "RateLimitError", "AuthenticationError", "NotFoundError", "Timeout",
        "ServiceUnavailableError", "BadRequestError", "UnprocessableEntityError",
    ):
        setattr(mock_litellm, _name, type(_name, (Exception,), {}))


# ── Model string construction ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider_type", "model_id", "expected"),
    [
        ("groq", "llama-3.3-70b-versatile", "groq/llama-3.3-70b-versatile"),
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
    config = _config(is_free=False)
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


async def test_free_provider_always_zero_cost():
    """Free providers should always report cost=0.0 without calling litellm.completion_cost."""
    config = _config(is_free=True)
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response(content="Free output", prompt_tokens=10, completion_tokens=5)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hello"}],
        )

    assert result.success is True
    assert result.cost_usd == 0.0
    assert result.cost_known is True
    # completion_cost should never be called for free providers
    mock_litellm.completion_cost.assert_not_called()


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


# ── Timeout default ───────────────────────────────────────────────────────


async def test_call_passes_default_timeout():
    """acompletion should receive timeout=_DEFAULT_TIMEOUT_S by default."""
    config = _config()
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

        call_kwargs = mock_litellm.acompletion.call_args.kwargs
        assert call_kwargs["timeout"] == _DEFAULT_TIMEOUT_S


async def test_call_allows_timeout_override():
    """Caller-supplied timeout should override the default."""
    config = _config()
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
            timeout=300,
        )

        call_kwargs = mock_litellm.acompletion.call_args.kwargs
        assert call_kwargs["timeout"] == 300


async def test_call_hard_timeout_cancels_hung_provider():
    """A provider that hangs past the timeout must be hard-cancelled and
    returned as a 408 — not allowed to run unbounded.

    litellm's own ``timeout`` param has been observed NOT to fire (PR #582;
    production hangs of ~361s on a 120s timeout because litellm retries
    internally). A hard ``asyncio.wait_for`` cap guarantees the ceiling.
    """
    import asyncio
    import time as _t

    config = _config()
    delegate = LiteLLMDelegate(config)

    async def _hang(*args, **kwargs):
        await asyncio.sleep(2.0)  # longer than the 0.2s cap below
        return _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = _hang

        start = _t.monotonic()
        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
            timeout=0.2,
        )
        elapsed = _t.monotonic() - start

    assert result.success is False
    assert result.status_code == 408
    # Cancelled promptly at the cap — did not wait the full 2s hang
    assert elapsed < 1.5


# ── Error classification ───────────────────────────────────────────────────


async def test_call_rate_limit_error():
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
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
        _install_litellm_exceptions(mock_litellm)
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
        _install_litellm_exceptions(mock_litellm)
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
        _install_litellm_exceptions(mock_litellm)
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


async def test_call_bad_request_error():
    """litellm.BadRequestError (and its ContextWindowExceeded / ContentPolicy
    subclasses) must map to status 400 so the router classifies it BAD_REQUEST
    and fails fast without same-provider retries or a breaker trip."""
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.BadRequestError("context window exceeded"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 400


async def test_call_unprocessable_entity_error():
    """litellm.UnprocessableEntityError must map to status 422 (→ BAD_REQUEST)."""
    config = _config()
    delegate = LiteLLMDelegate(config)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.UnprocessableEntityError("unprocessable"),
        )

        result = await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

    assert result.success is False
    assert result.status_code == 422


def test_litellm_badrequest_subclasses_are_stable():
    """Regression guard against a litellm upgrade: the delegate catches
    litellm.BadRequestError to map context-overflow + content-policy errors to
    400. That relies on these being BadRequestError subclasses — assert it
    against the INSTALLED litellm so a hierarchy change fails loudly here."""
    import litellm
    assert issubclass(litellm.ContextWindowExceededError, litellm.BadRequestError)
    assert issubclass(litellm.ContentPolicyViolationError, litellm.BadRequestError)


# ── Cost extraction fallback ───────────────────────────────────────────────


async def test_call_cost_extraction_fallback():
    """Graceful-degrade path: when completion_cost raises on a paid provider that
    has NO model_profiles entry (cfg.profile is unset here), cost_usd stays 0.0
    and cost_known False. The profile-PRESENT path is covered by
    test_call_cost_profile_fallback_when_litellm_unknown."""
    config = _config(is_free=False)
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
    assert result.cost_known is False


async def test_call_cost_profile_fallback_when_litellm_unknown():
    """When litellm.completion_cost raises (aggregator model not in litellm's DB)
    but the provider has a model_profiles entry, cost is computed from the
    profile's cost_per_mtok and cost_known is True — closing the blind-spend gap
    (ROUTE-03 / ROUT-03). Visibility only; never gates routing or budget.
    """
    from types import SimpleNamespace

    class _FakeRegistry:
        def get(self, name):
            if name == "glm-5.1":
                return SimpleNamespace(cost_per_mtok_in=0.80, cost_per_mtok_out=2.56)
            return None

    config = RoutingConfig(
        providers={
            "glm51": ProviderConfig(
                name="glm51", provider_type="zenmux", model_id="z-ai/glm-5.1",
                is_free=False, rpm_limit=None, open_duration_s=120, profile="glm-5.1",
            ),
        },
        call_sites={},
        retry_profiles={},
    )
    delegate = LiteLLMDelegate(config, profile_registry=_FakeRegistry())
    # 1M in + 1M out → cost = 1*0.80 + 1*2.56 = 3.36 (clean arithmetic).
    mock_resp = _mock_response(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.side_effect = Exception("This model isn't mapped yet")

        result = await delegate.call(
            "glm51", "z-ai/glm-5.1", [{"role": "user", "content": "Hi"}],
        )

    assert result.success is True
    assert result.cost_known is True
    assert result.cost_usd == pytest.approx(3.36)


async def test_call_cost_profile_fallback_missing_profile_stays_unknown():
    """Paid provider whose profile is absent from the registry → graceful
    degrade: cost 0.0, cost_known False (logged, not silent)."""
    from types import SimpleNamespace  # noqa: F401

    class _EmptyRegistry:
        def get(self, name):
            return None

    config = _config(is_free=False)
    delegate = LiteLLMDelegate(config, profile_registry=_EmptyRegistry())
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
    assert result.cost_known is False


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
        _install_litellm_exceptions(mock_litellm)
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
        _install_litellm_exceptions(mock_litellm)
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
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(
            side_effect=mock_litellm.AuthenticationError("bad key"),
        )

        # First call — should log
        await delegate.call("test-provider", "m", [{"role": "user", "content": "a"}])
        assert mock_logger.warning.call_count == 1

        # Second call — should be suppressed
        await delegate.call("test-provider", "m", [{"role": "user", "content": "b"}])
        assert mock_logger.warning.call_count == 1  # Still 1, not 2


# ── Per-provider params (extra litellm kwargs) — Groq EOL migration ─────────


def _config_with_params(params) -> RoutingConfig:
    """Single-provider config carrying a per-provider ``params`` block."""
    return RoutingConfig(
        providers={
            "test-provider": ProviderConfig(
                name="test-provider",
                provider_type="groq",
                model_id="openai/gpt-oss-20b",
                is_free=True,
                rpm_limit=30,
                open_duration_s=120,
                params=params,
            ),
        },
        call_sites={},
        retry_profiles={},
    )


async def test_call_applies_provider_params_extra_body():
    """A provider config with params={"extra_body": {...}} must pass that
    extra_body through to litellm.acompletion (Groq gpt-oss reasoning
    controls — keeps the reasoning field out of `content`)."""
    config = _config_with_params({"extra_body": {"include_reasoning": False}})
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "openai/gpt-oss-20b",
            [{"role": "user", "content": "Hi"}],
        )

        call_kwargs = mock_litellm.acompletion.call_args.kwargs
        assert call_kwargs["extra_body"] == {"include_reasoning": False}


async def test_call_provider_params_extra_body_deep_merges_caller_wins():
    """When BOTH the provider config and the caller supply extra_body, the two
    bodies deep-merge (union of keys) rather than one clobbering the other, and
    on a key collision the explicit caller value wins (provider params are
    defaults)."""
    config = _config_with_params(
        {"extra_body": {"include_reasoning": False, "reasoning_effort": "low"}},
    )
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "openai/gpt-oss-20b",
            [{"role": "user", "content": "Hi"}],
            # Caller overrides reasoning_effort and adds a new key.
            extra_body={"reasoning_effort": "high", "caller_key": 1},
        )

        body = mock_litellm.acompletion.call_args.kwargs["extra_body"]
        # Provider-only key preserved (deep merge, no clobber)
        assert body["include_reasoning"] is False
        # Caller wins on the collided key
        assert body["reasoning_effort"] == "high"
        # Caller-only key carried through
        assert body["caller_key"] == 1


async def test_call_no_params_passes_no_extra_body():
    """A provider WITHOUT a params block must not inject an extra_body kwarg."""
    config = _config()  # no params
    delegate = LiteLLMDelegate(config)
    mock_resp = _mock_response()

    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        mock_litellm.acompletion = AsyncMock(return_value=mock_resp)
        mock_litellm.completion_cost.return_value = 0.0

        await delegate.call(
            "test-provider", "llama-3.3-70b-versatile",
            [{"role": "user", "content": "Hi"}],
        )

        assert "extra_body" not in mock_litellm.acompletion.call_args.kwargs
