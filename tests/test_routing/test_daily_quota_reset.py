"""A provider-side DAILY quota 429 deselects the provider until the reset the
provider itself named, instead of being retried all day.

MEASURED on a live install (2026-10-06/07): the Gemini free tier allows 20
requests/day per model; once spent, every call returned the 429 below until
00:00 UTC (all 38 logged 429s named that reset), and Genesis kept calling it
because a 429 is RATE_LIMITED (fail fast, never trips the breaker) and nothing
remembered the reset. The body is verbatim from the journal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from genesis.routing.daily_budget import DailyBudgetLedger
from genesis.routing.retry import daily_quota_reset_s
from genesis.routing.types import CallResult, ProviderConfig

DAILY_429 = """litellm.RateLimitError: litellm.RateLimitError: geminiException - {
  "error": {
    "code": 429,
    "message": "You exceeded your current quota, please check your plan and billing details. \\n* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\\nPlease retry in 5h54m39.641065757s.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [
      {
        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
        "violations": [
          {
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {
              "location": "global",
              "model": "gemini-3.8-flash"
            },
            "quotaValue": "20"
          }
        ]
      },
      {
        "@type": "type.googleapis.com/google.rpc.RetryInfo",
        "retryDelay": "21279s"
      }
    ]
  }
}"""

# SYNTHETIC (not measured here): the same body for a per-MINUTE quota. A short
# backpressure 429 must stay fail-fast and never deselect for hours.
MINUTE_429 = DAILY_429.replace(
    "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
).replace('"21279s"', '"35s"')


@pytest.fixture(autouse=True)
def _no_kill_switch(monkeypatch):
    monkeypatch.delenv("GENESIS_DAILY_BUDGET_DISABLED", raising=False)


class TestParse:
    def test_a_daily_quota_429_yields_its_retry_delay(self):
        assert daily_quota_reset_s(DAILY_429) == 21279.0

    def test_a_per_minute_quota_is_not_a_daily_quota(self):
        assert daily_quota_reset_s(MINUTE_429) is None

    def test_a_plain_429_is_not_a_daily_quota(self):
        assert daily_quota_reset_s("litellm.RateLimitError: rate limited") is None
        assert daily_quota_reset_s("") is None

    def test_a_daily_quota_without_a_retry_delay_is_not_honoured(self):
        body = DAILY_429.replace('"retryDelay": "21279s"', '"other": "x"')
        assert daily_quota_reset_s(body) is None

    def test_an_absurd_delay_is_refused(self):
        # More than a day plus margin cannot be a daily reset: never deselect
        # a provider for longer on a value we cannot account for.
        assert daily_quota_reset_s(DAILY_429.replace('"21279s"', '"999999s"')) is None
        assert daily_quota_reset_s(DAILY_429.replace('"21279s"', '"-5s"')) is None

    def test_a_fractional_delay_is_read(self):
        assert daily_quota_reset_s(DAILY_429.replace('"21279s"', '"120.5s"')) == 120.5

    def test_a_malformed_body_never_raises(self):
        # It runs inside the delegate's 429 handler: a raise there would escape
        # the router instead of falling through to the next provider.
        head = DAILY_429[: DAILY_429.index('"violations": [')]
        for bad in ("5", "true", '"x"', "null", "{}"):
            body = head + '"violations": ' + bad + "}]}}"
            assert daily_quota_reset_s(body) is None, bad
        # Nested past the stack (the parser starts at the first "{").
        assert daily_quota_reset_s("x " + '{"a":' * 100_000) is None

    def test_litellms_own_exception_string_is_read(self):
        # Pins the str() form litellm gives the delegate (single prefix when
        # built directly; its exception mapping adds a second, as in DAILY_429).
        import litellm

        body = DAILY_429[DAILY_429.index("{") :]
        exc = litellm.RateLimitError(
            message="geminiException - " + body,
            llm_provider="gemini",
            model="gemini/gemini-3.8-flash",
        )
        assert daily_quota_reset_s(str(exc)) == 21279.0


def _cfg(name="gemini-free"):
    return ProviderConfig(
        name=name,
        provider_type="google",
        model_id="gemini-3.8-flash",
        is_free=True,
        rpm_limit=15,
        open_duration_s=120,
    )


def _ledger(tmp_path, *, persist=True, now="2026-10-07T04:00:00+00:00"):
    holder = {"now": datetime.fromisoformat(now)}
    ledger = DailyBudgetLedger(
        state_path=tmp_path / "budget.json",
        clock=lambda: holder["now"],
        persist=persist,
    )
    return ledger, holder


def _daily_429(delay=21279.0):
    return CallResult(
        success=False,
        error="429",
        status_code=429,
        retry_after_s=delay,
        daily_quota_exhausted=True,
    )


class TestLedger:
    def test_a_daily_quota_429_deselects_until_the_reset(self, tmp_path):
        """A provider with NO configured limit is still deselected: the
        provider's own word is the limit."""
        ledger, clock = _ledger(tmp_path)
        cfg = _cfg()
        assert ledger.exhausted(cfg) is False
        crossed = ledger.record(cfg, _daily_429(3600))
        assert crossed is True, "the first block is the crossing that emits once"
        assert ledger.exhausted(cfg) is True
        clock["now"] += timedelta(seconds=3599)
        assert ledger.exhausted(cfg) is True
        clock["now"] += timedelta(seconds=2)
        assert ledger.exhausted(cfg) is False, "callable again once the reset passed"

    def test_a_repeat_429_while_blocked_is_not_a_new_crossing(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg()
        assert ledger.record(cfg, _daily_429(3600)) is True
        assert ledger.record(cfg, _daily_429(3500)) is False

    def test_an_ordinary_429_never_deselects(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg()
        ledger.record(cfg, CallResult(success=False, error="x", status_code=429, retry_after_s=35))
        assert ledger.exhausted(cfg) is False

    def test_the_block_survives_a_restart(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        ledger.record(_cfg(), _daily_429(3600))
        again, _ = _ledger(tmp_path)
        assert again.exhausted(_cfg()) is True

    def test_a_reader_that_does_not_persist_still_honours_the_block(self, tmp_path):
        """MCP children load the server's state with persist=False."""
        ledger, _ = _ledger(tmp_path)
        ledger.record(_cfg(), _daily_429(3600))
        child, _ = _ledger(tmp_path, persist=False)
        assert child.exhausted(_cfg()) is True

    def test_an_expired_block_is_dropped_on_load(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        ledger.record(_cfg(), _daily_429(60))
        later, _ = _ledger(tmp_path, now="2026-10-08T04:00:00+00:00")
        assert later.exhausted(_cfg()) is False

    def test_a_corrupt_block_value_fails_open(self, tmp_path):
        (tmp_path / "budget.json").write_text(
            '{"version": 1, "providers": {"gemini-free": '
            '{"day": "2026-10-07", "requests": 0, "tokens": 0, "blocked_until": "not-a-time"}}}'
        )
        ledger, _ = _ledger(tmp_path)
        assert ledger.exhausted(_cfg()) is False

    def _stored_block(self, tmp_path, until, model='"gemini-3.8-flash"'):
        (tmp_path / "budget.json").write_text(
            '{"version": 1, "providers": {"gemini-free": {"day": "2026-10-07", '
            f'"requests": 0, "tokens": 0, "blocked_until": "{until}", "blocked_model": {model}}}}}}}'
        )

    def test_a_stored_block_round_trips(self, tmp_path):
        # Control for the two refusals below: the same row, well-formed, loads.
        self._stored_block(tmp_path, "2026-10-07T05:00:00+00:00")
        ledger, _ = _ledger(tmp_path)
        assert ledger.exhausted(_cfg()) is True

    def test_a_stored_block_further_out_than_any_daily_reset_fails_open(self, tmp_path):
        # A hand edit or a clock that jumped must not silence a provider for good.
        self._stored_block(tmp_path, "2099-01-01T00:00:00+00:00")
        ledger, _ = _ledger(tmp_path)
        assert ledger.exhausted(_cfg()) is False

    def test_a_stored_block_without_its_model_fails_open(self, tmp_path):
        self._stored_block(tmp_path, "2026-10-07T05:00:00+00:00", model="null")
        ledger, _ = _ledger(tmp_path)
        assert ledger.exhausted(_cfg()) is False

    def test_the_block_is_bound_to_the_model_it_was_spent_on(self, tmp_path):
        # The quota is per model: pointing the provider at another model (a
        # dashboard swap, a config reload) must not inherit the block.
        import dataclasses

        ledger, _ = _ledger(tmp_path)
        cfg = _cfg()
        ledger.record(cfg, _daily_429(3600))
        moved = dataclasses.replace(cfg, model_id="gemini-4-flash")
        assert ledger.exhausted(moved) is False
        assert ledger.status(moved) is None
        assert ledger.exhausted(cfg) is True, "the spent model stays blocked"
        again, _ = _ledger(tmp_path)
        assert again.exhausted(moved) is False and again.exhausted(cfg) is True

    def test_the_kill_switch_disables_the_block(self, tmp_path, monkeypatch):
        ledger, _ = _ledger(tmp_path)
        ledger.record(_cfg(), _daily_429(3600))
        monkeypatch.setenv("GENESIS_DAILY_BUDGET_DISABLED", "1")
        assert ledger.exhausted(_cfg()) is False

    def test_status_reports_the_block(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = _cfg()
        assert ledger.status(cfg) is None, "an unlimited, unblocked provider is untracked"
        ledger.record(cfg, _daily_429(3600))
        st = ledger.status(cfg)
        assert st["exhausted"] is True
        assert st["blocked_until"] == "2026-10-07T05:00:00+00:00"

    def test_a_limited_provider_keeps_its_counters_while_blocked(self, tmp_path):
        ledger, _ = _ledger(tmp_path)
        cfg = ProviderConfig(
            name="g",
            provider_type="google",
            model_id="m",
            is_free=True,
            rpm_limit=None,
            open_duration_s=120,
            rpd_limit=20,
        )
        for _ in range(3):
            ledger.record(cfg, CallResult(success=True, content="x"))
        ledger.record(cfg, _daily_429(3600))
        st = ledger.status(cfg)
        assert st["requests_used"] == 3, "the 429 itself is not usage"
        assert st["exhausted"] is True


@pytest.mark.asyncio
async def test_the_delegate_flags_a_daily_quota_429():
    from unittest.mock import AsyncMock, patch

    from genesis.routing.litellm_delegate import LiteLLMDelegate

    from .test_litellm_delegate import _config, _install_litellm_exceptions

    delegate = LiteLLMDelegate(_config())
    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(side_effect=mock_litellm.RateLimitError(DAILY_429))
        result = await delegate.call("test-provider", "m", [{"role": "user", "content": "Hi"}])
    assert result.status_code == 429
    assert result.daily_quota_exhausted is True
    assert result.retry_after_s == 21279.0


@pytest.mark.asyncio
async def test_a_parse_defect_never_escapes_the_delegate():
    from unittest.mock import AsyncMock, patch

    from genesis.routing.litellm_delegate import LiteLLMDelegate

    from .test_litellm_delegate import _config, _install_litellm_exceptions

    delegate = LiteLLMDelegate(_config())
    with (
        patch("genesis.routing.litellm_delegate.litellm") as mock_litellm,
        patch(
            "genesis.routing.litellm_delegate.daily_quota_reset_s",
            side_effect=RuntimeError("boom"),
        ),
    ):
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(side_effect=mock_litellm.RateLimitError(DAILY_429))
        result = await delegate.call("test-provider", "m", [{"role": "user", "content": "Hi"}])
    assert result.status_code == 429
    assert result.daily_quota_exhausted is False


@pytest.mark.asyncio
async def test_the_delegate_leaves_an_ordinary_429_alone():
    from unittest.mock import AsyncMock, patch

    from genesis.routing.litellm_delegate import LiteLLMDelegate

    from .test_litellm_delegate import _config, _install_litellm_exceptions

    delegate = LiteLLMDelegate(_config())
    with patch("genesis.routing.litellm_delegate.litellm") as mock_litellm:
        _install_litellm_exceptions(mock_litellm)
        mock_litellm.acompletion = AsyncMock(side_effect=mock_litellm.RateLimitError(MINUTE_429))
        result = await delegate.call("test-provider", "m", [{"role": "user", "content": "Hi"}])
    assert result.status_code == 429
    assert result.daily_quota_exhausted is False
    assert result.retry_after_s is None


class TestRouterWalk:
    """WIRING, through the real Router chain walk."""

    @pytest.mark.asyncio
    async def test_a_daily_quota_429_skips_the_provider_until_its_reset(self, tmp_path):
        from genesis.routing.circuit_breaker import CircuitBreakerRegistry
        from genesis.routing.degradation import DegradationTracker
        from genesis.routing.router import Router
        from genesis.routing.standalone import NullCostTracker
        from genesis.routing.types import CallSiteConfig, RetryPolicy, RoutingConfig

        from .conftest import MockDelegate
        from .test_daily_budget import _RecordingBus

        gem, other = _cfg("gemini-free"), _cfg("other")
        config = RoutingConfig(
            providers={"gemini-free": gem, "other": other},
            call_sites={"site": CallSiteConfig(id="site", chain=["gemini-free", "other"])},
            retry_profiles={"default": RetryPolicy(max_retries=0, base_delay_ms=1, jitter_pct=0.0)},
        )
        clock = {"now": datetime(2026, 10, 7, 4, 0, tzinfo=UTC)}
        ledger = DailyBudgetLedger(state_path=tmp_path / "b.json", clock=lambda: clock["now"])
        delegate = MockDelegate({"gemini-free": _daily_429(3600)})
        breakers = CircuitBreakerRegistry(config.providers, persist=False)
        bus = _RecordingBus()
        router = Router(
            config=config, breakers=breakers, cost_tracker=NullCostTracker(),
            degradation=DegradationTracker(), delegate=delegate, daily_budget=ledger,
            event_bus=bus,
        )
        msg = [{"role": "user", "content": "x"}]

        first = await router.route_call("site", msg)
        assert first.success and first.provider_used == "other"
        second = await router.route_call("site", msg)
        assert second.success and second.provider_used == "other"
        called = [c["provider"] for c in delegate.calls]
        assert called == ["gemini-free", "other", "other"], "skipped after the daily 429"
        assert breakers.get("gemini-free").is_available(), "a quota is not a health failure"

        events = [e for e in bus.events if e["type"] == "provider.budget_exhausted"]
        assert len(events) == 1
        assert "2026-10-07T05:00:00+00:00" in events[0]["message"]

        clock["now"] += timedelta(hours=1, seconds=1)
        delegate.responses.pop("gemini-free")
        third = await router.route_call("site", msg)
        assert third.provider_used == "gemini-free", "callable again after the reset"
