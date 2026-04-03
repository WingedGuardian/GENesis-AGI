"""Tests for silent model downgrade detection."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.cc.invoker import CCInvoker
from genesis.cc.types import CCInvocation, CCModel, CCOutput

# ── CCModel.from_full_name ──────────────────────────────────────────


class TestFromFullName:
    def test_opus(self):
        assert CCModel.from_full_name("claude-opus-4-6") == CCModel.OPUS

    def test_sonnet(self):
        assert CCModel.from_full_name("claude-sonnet-4-6") == CCModel.SONNET

    def test_haiku(self):
        assert CCModel.from_full_name("claude-haiku-4-5-20251001") == CCModel.HAIKU

    def test_unknown_returns_none(self):
        assert CCModel.from_full_name("unknown-model-x") is None

    def test_empty_returns_none(self):
        assert CCModel.from_full_name("") is None

    def test_case_insensitive(self):
        assert CCModel.from_full_name("Claude-OPUS-4-6") == CCModel.OPUS


# ── _detect_downgrade ───────────────────────────────────────────────


class TestDetectDowngrade:
    def test_opus_to_sonnet_is_downgrade(self):
        assert CCInvoker._detect_downgrade(CCModel.OPUS, "claude-sonnet-4-6") is True

    def test_opus_to_haiku_is_downgrade(self):
        assert CCInvoker._detect_downgrade(CCModel.OPUS, "claude-haiku-4-5") is True

    def test_sonnet_to_haiku_is_downgrade(self):
        assert CCInvoker._detect_downgrade(CCModel.SONNET, "claude-haiku-4-5") is True

    def test_same_model_not_downgrade(self):
        assert CCInvoker._detect_downgrade(CCModel.OPUS, "claude-opus-4-6") is False

    def test_upgrade_not_downgrade(self):
        assert CCInvoker._detect_downgrade(CCModel.HAIKU, "claude-sonnet-4-6") is False

    def test_unknown_model_fails_open(self):
        assert CCInvoker._detect_downgrade(CCModel.OPUS, "unknown-model") is False


# ── CCOutput fields ─────────────────────────────────────────────────


class TestCCOutputFields:
    def test_defaults(self):
        output = CCOutput(
            session_id="s1", text="hi", model_used="sonnet",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
        )
        assert output.model_requested == ""
        assert output.downgraded is False

    def test_explicit_values(self):
        output = CCOutput(
            session_id="s1", text="hi", model_used="claude-sonnet-4-6",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
            model_requested="opus", downgraded=True,
        )
        assert output.model_requested == "opus"
        assert output.downgraded is True


# ── Integration: _parse_result_dict sets downgraded ─────────────────


class TestParseResultDictDowngrade:
    def test_downgrade_detected_in_parse(self):
        invoker = CCInvoker()
        inv = CCInvocation(prompt="test", model=CCModel.OPUS)
        result_data = {
            "type": "result",
            "session_id": "sess-1",
            "result": "response text",
            "total_cost_usd": 0.1,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "modelUsage": {"claude-sonnet-4-6": {"input_tokens": 100}},
        }
        output = invoker._parse_result_dict(result_data, inv, 1000)
        assert output.downgraded is True
        assert output.model_requested == "opus"
        assert output.model_used == "claude-sonnet-4-6"

    def test_no_downgrade_when_model_matches(self):
        invoker = CCInvoker()
        inv = CCInvocation(prompt="test", model=CCModel.OPUS)
        result_data = {
            "type": "result",
            "session_id": "sess-1",
            "result": "response text",
            "total_cost_usd": 0.1,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "modelUsage": {"claude-opus-4-6": {"input_tokens": 100}},
        }
        output = invoker._parse_result_dict(result_data, inv, 1000)
        assert output.downgraded is False
        assert output.model_requested == "opus"


# ── Callback fires on downgrade ─────────────────────────────────────


class TestDowngradeCallback:
    @pytest.mark.asyncio
    async def test_callback_fires_on_downgrade(self):
        callback = AsyncMock()
        invoker = CCInvoker(on_model_downgrade=callback)

        output = CCOutput(
            session_id="s1", text="hi", model_used="claude-sonnet-4-6",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
            model_requested="opus", downgraded=True,
        )
        await invoker._fire_downgrade_callback(output)
        callback.assert_awaited_once_with("opus", "claude-sonnet-4-6", "s1")

    @pytest.mark.asyncio
    async def test_callback_not_fired_when_no_downgrade(self):
        callback = AsyncMock()
        invoker = CCInvoker(on_model_downgrade=callback)

        output = CCOutput(
            session_id="s1", text="hi", model_used="claude-opus-4-6",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
            model_requested="opus", downgraded=False,
        )
        await invoker._fire_downgrade_callback(output)
        callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_callback_failure_does_not_raise(self):
        callback = AsyncMock(side_effect=RuntimeError("boom"))
        invoker = CCInvoker(on_model_downgrade=callback)

        output = CCOutput(
            session_id="s1", text="hi", model_used="claude-sonnet-4-6",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
            model_requested="opus", downgraded=True,
        )
        # Should not raise
        await invoker._fire_downgrade_callback(output)
        callback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_callback_configured(self):
        invoker = CCInvoker()  # no on_model_downgrade
        output = CCOutput(
            session_id="s1", text="hi", model_used="claude-sonnet-4-6",
            cost_usd=0.0, input_tokens=0, output_tokens=0,
            duration_ms=0, exit_code=0,
            model_requested="opus", downgraded=True,
        )
        # Should not raise even with downgrade and no callback
        await invoker._fire_downgrade_callback(output)
