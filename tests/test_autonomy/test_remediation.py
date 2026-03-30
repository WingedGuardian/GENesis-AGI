"""Tests for genesis.autonomy.remediation -- self-healing registry."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.remediation import (
    DEFAULT_REMEDIATIONS,
    RemediationAction,
    RemediationRegistry,
    register_defaults,
)
from genesis.observability.types import ProbeResult, ProbeStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _healthy_probe(name: str = "qdrant") -> ProbeResult:
    return ProbeResult(
        name=name,
        status=ProbeStatus.HEALTHY,
        latency_ms=1.0,
        checked_at="2026-03-18T00:00:00+00:00",
    )


def _down_probe(name: str = "qdrant") -> ProbeResult:
    return ProbeResult(
        name=name,
        status=ProbeStatus.DOWN,
        latency_ms=1.0,
        message="Connection refused",
        checked_at="2026-03-18T00:00:00+00:00",
    )


def _l2_action(name: str = "test_restart", probe: str = "qdrant") -> RemediationAction:
    return RemediationAction(
        name=name,
        probe_name=probe,
        condition="Test condition",
        command=["echo", "restarting"],
        governance_level=2,
        reversible=True,
        cooldown_s=10,
        max_attempts=3,
    )


def _l3_action() -> RemediationAction:
    return RemediationAction(
        name="disk_cleanup",
        probe_name="disk",
        condition="Disk usage high",
        command=["sudo", "journalctl", "--vacuum-size=100M"],
        governance_level=3,
        cooldown_s=60,
        max_attempts=2,
    )


def _l4_action() -> RemediationAction:
    return RemediationAction(
        name="ollama_alert",
        probe_name="ollama",
        condition="Ollama unreachable",
        command=[],
        governance_level=4,
        cooldown_s=60,
        max_attempts=1,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_action(self):
        reg = RemediationRegistry()
        action = _l2_action()
        reg.register(action)
        assert len(reg.actions) == 1
        assert reg.actions[0].name == "test_restart"

    def test_idempotent_registration(self):
        reg = RemediationRegistry()
        action = _l2_action()
        reg.register(action)
        reg.register(action)
        assert len(reg.actions) == 1

    def test_register_defaults(self):
        reg = RemediationRegistry()
        register_defaults(reg)
        assert len(reg.actions) == len(DEFAULT_REMEDIATIONS)
        names = {a.name for a in reg.actions}
        assert "qdrant_restart" in names
        assert "tmp_cleanup" in names
        assert "awareness_restart" in names
        assert "ollama_alert" in names
        assert "disk_cleanup" in names


# ---------------------------------------------------------------------------
# L2: Auto-run
# ---------------------------------------------------------------------------

class TestL2AutoRun:
    @pytest.mark.asyncio
    async def test_healthy_probe_no_action(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        outcomes = await reg.check_and_remediate({"qdrant": _healthy_probe()})
        assert len(outcomes) == 1
        assert not outcomes[0].triggered
        assert not outcomes[0].executed

    @pytest.mark.asyncio
    async def test_down_probe_triggers_remediation(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert len(outcomes) == 1
        o = outcomes[0]
        assert o.triggered
        assert o.executed
        assert o.success is True

    @pytest.mark.asyncio
    async def test_failed_command_increments_failures(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].success is False
        assert "attempt 1/3" in outcomes[0].message

    @pytest.mark.asyncio
    async def test_max_attempts_blocks_further_runs(self):
        reg = RemediationRegistry()
        reg.register(_l2_action(name="maxed", probe="qdrant"))
        # Simulate reaching max
        reg._consecutive_failures["maxed"] = 3
        outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].triggered
        assert not outcomes[0].executed
        assert "Max attempts" in outcomes[0].message

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rerun(self):
        reg = RemediationRegistry()
        action = _l2_action()
        reg.register(action)
        # Simulate recent run
        reg._last_run[action.name] = time.monotonic()
        outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].triggered
        assert not outcomes[0].executed
        assert "cooldown" in outcomes[0].message.lower()

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_rerun(self):
        reg = RemediationRegistry()
        action = _l2_action()
        reg.register(action)
        # Simulate old run (well past cooldown)
        reg._last_run[action.name] = time.monotonic() - 9999
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].executed
        assert outcomes[0].success is True

    @pytest.mark.asyncio
    async def test_healthy_resets_failure_counter(self):
        reg = RemediationRegistry()
        action = _l2_action()
        reg.register(action)
        reg._consecutive_failures[action.name] = 2
        await reg.check_and_remediate({"qdrant": _healthy_probe()})
        assert action.name not in reg._consecutive_failures

    @pytest.mark.asyncio
    async def test_missing_probe_skipped(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        outcomes = await reg.check_and_remediate({"other_probe": _down_probe("other")})
        assert len(outcomes) == 0


# ---------------------------------------------------------------------------
# L3: Propose
# ---------------------------------------------------------------------------

class TestL3Propose:
    @pytest.mark.asyncio
    async def test_l3_proposes_via_outreach(self):
        outreach = AsyncMock()
        reg = RemediationRegistry(outreach_fn=outreach)
        reg.register(_l3_action())
        outcomes = await reg.check_and_remediate({
            "disk": _down_probe("disk"),
        })
        assert len(outcomes) == 1
        assert outcomes[0].triggered
        assert not outcomes[0].executed
        assert "Proposing" in outcomes[0].message
        outreach.assert_called_once()
        call_args = outreach.call_args[0]
        assert call_args[0] == "error"  # severity

    @pytest.mark.asyncio
    async def test_l3_without_outreach_fn(self):
        reg = RemediationRegistry()  # No outreach_fn
        reg.register(_l3_action())
        outcomes = await reg.check_and_remediate({
            "disk": _down_probe("disk"),
        })
        assert outcomes[0].triggered
        assert not outcomes[0].executed


# ---------------------------------------------------------------------------
# L4: Alert only
# ---------------------------------------------------------------------------

class TestL4Alert:
    @pytest.mark.asyncio
    async def test_l4_alerts_via_outreach(self):
        outreach = AsyncMock()
        reg = RemediationRegistry(outreach_fn=outreach)
        reg.register(_l4_action())
        outcomes = await reg.check_and_remediate({
            "ollama": _down_probe("ollama"),
        })
        assert len(outcomes) == 1
        assert outcomes[0].triggered
        assert not outcomes[0].executed
        assert "Alert" in outcomes[0].message
        outreach.assert_called_once()
        call_args = outreach.call_args[0]
        assert call_args[0] == "warning"

    @pytest.mark.asyncio
    async def test_l4_without_outreach_fn(self):
        reg = RemediationRegistry()
        reg.register(_l4_action())
        outcomes = await reg.check_and_remediate({
            "ollama": _down_probe("ollama"),
        })
        assert outcomes[0].triggered
        assert not outcomes[0].executed


# ---------------------------------------------------------------------------
# Command failure modes
# ---------------------------------------------------------------------------

class TestCommandFailures:
    @pytest.mark.asyncio
    async def test_command_timeout(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(side_effect=TimeoutError)
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].executed
        assert outcomes[0].success is False

    @pytest.mark.asyncio
    async def test_command_not_found(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("echo"),
        ):
            outcomes = await reg.check_and_remediate({"qdrant": _down_probe()})
        assert outcomes[0].executed
        assert outcomes[0].success is False

    @pytest.mark.asyncio
    async def test_empty_command_skipped(self):
        reg = RemediationRegistry()
        action = RemediationAction(
            name="empty",
            probe_name="test",
            condition="test",
            command=[],
            governance_level=2,
        )
        reg.register(action)
        outcomes = await reg.check_and_remediate({
            "test": _down_probe("test"),
        })
        assert outcomes[0].executed
        assert outcomes[0].success is False

    @pytest.mark.asyncio
    async def test_max_attempts_sends_outreach(self):
        outreach = AsyncMock()
        reg = RemediationRegistry(outreach_fn=outreach)
        action = _l2_action()
        reg.register(action)
        reg._consecutive_failures[action.name] = 3
        await reg.check_and_remediate({"qdrant": _down_probe()})
        outreach.assert_called_once()
        assert "exhausted" in outreach.call_args[0][1].lower()


# ---------------------------------------------------------------------------
# Dict-based probe results
# ---------------------------------------------------------------------------

class TestDictProbes:
    @pytest.mark.asyncio
    async def test_dict_probe_down(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            outcomes = await reg.check_and_remediate({
                "qdrant": {"status": "down"},
            })
        assert outcomes[0].triggered
        assert outcomes[0].executed

    @pytest.mark.asyncio
    async def test_dict_probe_healthy(self):
        reg = RemediationRegistry()
        reg.register(_l2_action())
        outcomes = await reg.check_and_remediate({
            "qdrant": {"status": "healthy"},
        })
        assert not outcomes[0].triggered


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_failures(self):
        reg = RemediationRegistry()
        reg._consecutive_failures["foo"] = 5
        reg.reset_failures("foo")
        assert "foo" not in reg._consecutive_failures

    def test_reset_nonexistent(self):
        reg = RemediationRegistry()
        reg.reset_failures("nonexistent")  # Should not raise
