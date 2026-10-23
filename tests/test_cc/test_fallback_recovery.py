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
from genesis.cc.provider_readiness import READY
from genesis.cc.types import CCOutput, StreamEvent
from genesis.resilience.cc_fallback_probe import CCFallbackProbeWorker


def _output(*, is_error: bool = False, roster_model: str = "claude") -> CCOutput:
    return CCOutput(
        session_id="probe-1",
        text=READY,
        model_used="claude",
        cost_usd=0.0,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        exit_code=0 if not is_error else 1,
        is_error=is_error,
        roster_model=roster_model,
        tools_used=(),
    )


def _invoker(*, output=None, error=None):
    async def run(invocation, on_event):
        if error:
            raise error
        await on_event(
            StreamEvent(
                "init", raw={"type": "system", "subtype": "init", "tools": [], "mcp_servers": []}
            )
        )
        return output

    invoker = AsyncMock()
    invoker.run_streaming = AsyncMock(side_effect=run)
    return invoker


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
    @pytest.mark.asyncio
    async def test_uncertain_cleanup_quarantines_worker_until_runtime_recreation(self, monkeypatch):
        import asyncio

        from genesis.util.proc_kill import ProcessCleanupUncertain

        worker = CCFallbackProbeWorker(invoker=AsyncMock())
        worker._probe_once = AsyncMock(
            side_effect=[ProcessCleanupUncertain(), asyncio.CancelledError()]
        )
        monkeypatch.setattr("genesis.resilience.cc_fallback_probe.asyncio.sleep", AsyncMock())
        await worker._loop()
        assert worker._probe_once.await_count == 1
        worker.start()
        assert worker._task is None
        await worker.stop()
        worker.start()
        assert worker._task is None

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
        invoker.run_streaming.assert_not_awaited()  # zero cost in the healthy state
        assert _stub_overrides == []  # never resolved a model when healthy

    @pytest.mark.asyncio
    async def test_clears_on_home_success(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        invoker = _invoker(output=_output())
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        invoker.run_streaming.assert_awaited_once()
        probe = invoker.run_streaming.call_args.args[0]
        assert probe.safe_mode and probe.disable_tools and not probe.bare
        assert fallback_state.read().is_fallback is False

    @pytest.mark.asyncio
    async def test_probes_the_actual_home_model_not_native_claude(self, _stub_overrides):
        # default=peer: home is glm-5.2. The probe MUST resolve overrides for glm-5.2
        # (the down model), not hardcode native Claude — else it would falsely clear.
        fallback_state.enter("glm-5.2", "claude", "rate_limit")
        invoker = _invoker(output=_output(roster_model="glm-5.2"))
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert _stub_overrides == ["glm-5.2"]  # probed the HOME model
        assert fallback_state.read().is_fallback is False

    @pytest.mark.asyncio
    async def test_stays_in_fallback_on_rate_limit(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        invoker = _invoker(error=CCRateLimitError("still down"))
        await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert fallback_state.read().is_fallback is True  # not cleared

    @pytest.mark.asyncio
    async def test_stays_in_fallback_on_error_output(self, _stub_overrides):
        fallback_state.enter("claude", "glm-5.2", "rate_limit")
        from genesis.cc.exceptions import CCProcessError

        invoker = _invoker(output=_output(is_error=True))
        with pytest.raises(CCProcessError):
            await CCFallbackProbeWorker(invoker=invoker)._probe_once()
        assert fallback_state.read().is_fallback is True
