"""Turn-independent CC fallback recovery — note_home_recovery + the safety probe.

GENESIS_HOME is redirected to a temp dir (autouse fixture) so these never touch
the real ~/.genesis/cc_fallback_state.json the live server reads.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.cc import fallback_state
from genesis.cc.exceptions import CCRateLimitError
from genesis.cc.fallback_recovery import note_home_recovery
from genesis.cc.types import CCOutput
from genesis.resilience.cc_fallback_probe import CCFallbackProbeWorker


def _output(*, is_error: bool = False, roster_model: str = "claude") -> CCOutput:
    return CCOutput(
        session_id="probe-1", text="pong", model_used="claude", cost_usd=0.0,
        input_tokens=1, output_tokens=1, duration_ms=1, exit_code=0 if not is_error else 1,
        is_error=is_error, roster_model=roster_model,
    )


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    fallback_state.clear()
    yield
    fallback_state.clear()


class TestNoteHomeRecovery:
    @pytest.mark.asyncio
    async def test_clears_active_and_returns_true(self):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        assert await note_home_recovery() is True
        assert fallback_state.read().is_fallback is False

    @pytest.mark.asyncio
    async def test_noop_when_inactive(self):
        assert await note_home_recovery() is False

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        assert await note_home_recovery() is True
        assert await note_home_recovery() is False  # already cleared → fires once


class TestProbeWorker:
    @pytest.fixture
    def _stub_overrides(self, monkeypatch):
        """Record overrides_for() calls and return {} (no real config/keys needed)."""
        from genesis.cc import roster

        calls: list[str] = []
        monkeypatch.setattr(roster, "overrides_for", lambda name, *a, **k: calls.append(name) or {})
        return calls

    @pytest.mark.asyncio
    async def test_noop_when_not_in_fallback(self, _stub_overrides):
        invoker = AsyncMock()
        invoker.run = AsyncMock()
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        invoker.run.assert_not_awaited()  # zero cost in the healthy state
        assert _stub_overrides == []  # never resolved a model when healthy

    @pytest.mark.asyncio
    async def test_clears_on_home_success(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        invoker = AsyncMock()
        invoker.run = AsyncMock(return_value=_output())
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        invoker.run.assert_awaited_once()
        assert fallback_state.read().is_fallback is False

    @pytest.mark.asyncio
    async def test_probes_the_actual_home_model_not_native_claude(self, _stub_overrides):
        # default=peer: home is glm-5.2. The probe MUST resolve overrides for glm-5.2
        # (the down model), not hardcode native Claude — else it would falsely clear.
        fallback_state.enter("glm-5.2", "claude", "rate_limit")
        invoker = AsyncMock()
        invoker.run = AsyncMock(return_value=_output(roster_model="glm-5.2"))
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert _stub_overrides == ["glm-5.2"]  # probed the HOME model
        assert fallback_state.read().is_fallback is False

    @pytest.mark.asyncio
    async def test_stays_in_fallback_on_rate_limit(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        invoker = AsyncMock()
        invoker.run = AsyncMock(side_effect=CCRateLimitError("still down"))
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert fallback_state.read().is_fallback is True  # not cleared

    @pytest.mark.asyncio
    async def test_stays_in_fallback_on_error_output(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        invoker = AsyncMock()
        invoker.run = AsyncMock(return_value=_output(is_error=True))
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert fallback_state.read().is_fallback is True
