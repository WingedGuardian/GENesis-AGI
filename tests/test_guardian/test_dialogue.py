"""Tests for Guardian ↔ Genesis dialogue protocol."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.dialogue import (
    DialogueRequest,
    DialogueResponse,
    DialogueStatus,
    build_request,
    send_dialogue,
)
from genesis.guardian.health_signals import (
    HealthSnapshot,
    PauseState,
    SignalResult,
    SuspiciousResult,
)


@pytest.fixture
def config() -> GuardianConfig:
    return GuardianConfig()


def _snapshot_with_failures() -> HealthSnapshot:
    return HealthSnapshot(
        signals={
            "container_exists": SignalResult("container_exists", True, 1.0, "ok", "t"),
            "icmp_reachable": SignalResult("icmp_reachable", True, 1.0, "ok", "t"),
            "health_api": SignalResult("health_api", False, 1.0, "down", "t"),
            "heartbeat_canary": SignalResult("heartbeat_canary", False, 1.0, "down", "t"),
            "log_freshness": SignalResult("log_freshness", True, 1.0, "ok", "t"),
        },
        suspicious={
            "memory_pressure": SuspiciousResult("memory_pressure", False, "87.2%", "t"),
        },
        pause_state=PauseState(paused=False),
    )


class TestBuildRequest:

    def test_builds_from_snapshot(self) -> None:
        snapshot = _snapshot_with_failures()
        req = build_request(snapshot, duration_s=90.0, guardian_state="surveying")

        assert "health_api" in req.signals_failing
        assert "heartbeat_canary" in req.signals_failing
        assert "container_exists" in req.signals_ok
        assert req.duration_s == 90.0
        assert req.guardian_state == "surveying"
        assert "memory_pressure" in req.suspicious

    def test_to_dict(self) -> None:
        req = DialogueRequest(
            signals_failing=["health_api"],
            signals_ok=["container_exists"],
            duration_s=60.0,
            guardian_state="surveying",
            suspicious={"mem": "high"},
        )
        d = req.to_dict()
        assert d["type"] == "health_concern"
        assert d["signals_failing"] == ["health_api"]


class TestDialogueResponse:

    def test_unreachable_factory(self) -> None:
        resp = DialogueResponse.unreachable()
        assert resp.acknowledged is False
        assert resp.status == DialogueStatus.NEED_HELP

    def test_error_factory(self) -> None:
        resp = DialogueResponse.error("HTTP 500")
        assert resp.acknowledged is False
        assert "500" in resp.context


class TestSendDialogue:

    @pytest.mark.asyncio
    async def test_genesis_handling(self, config: GuardianConfig) -> None:
        """Genesis acknowledges and says it's handling the problem."""
        import urllib.request
        from unittest.mock import MagicMock

        response_body = json.dumps({
            "acknowledged": True,
            "status": "handling",
            "action": "restarting bridge",
            "eta_s": 60,
            "context": "Watchdog detected bridge crash",
        })

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body.encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        req = DialogueRequest(["health_api"], ["container_exists"], 30.0, "surveying", {})
        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            resp = await send_dialogue(config, req)

        assert resp.acknowledged is True
        assert resp.status == DialogueStatus.HANDLING
        assert resp.action == "restarting bridge"
        assert resp.eta_s == 60

    @pytest.mark.asyncio
    async def test_genesis_need_help(self, config: GuardianConfig) -> None:
        response_body = json.dumps({
            "acknowledged": True,
            "status": "need_help",
            "action": "",
            "eta_s": 0,
            "context": "Can't fix OOM from inside",
        })

        import urllib.request
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body.encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        req = DialogueRequest(["health_api"], [], 60.0, "surveying", {})
        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            resp = await send_dialogue(config, req)

        assert resp.acknowledged is True
        assert resp.status == DialogueStatus.NEED_HELP

    @pytest.mark.asyncio
    async def test_genesis_stand_down(self, config: GuardianConfig) -> None:
        response_body = json.dumps({
            "acknowledged": True,
            "status": "stand_down",
            "action": "maintenance",
            "eta_s": 0,
            "context": "Planned maintenance window",
        })

        import urllib.request
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body.encode()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        req = DialogueRequest(["health_api"], [], 30.0, "surveying", {})
        with patch.object(urllib.request, "urlopen", return_value=mock_resp):
            resp = await send_dialogue(config, req)

        assert resp.acknowledged is True
        assert resp.status == DialogueStatus.STAND_DOWN

    @pytest.mark.asyncio
    async def test_genesis_unreachable(self, config: GuardianConfig) -> None:
        import urllib.request

        with patch.object(
            urllib.request, "urlopen",
            side_effect=ConnectionRefusedError("refused"),
        ):
            req = DialogueRequest(["health_api"], [], 60.0, "surveying", {})
            resp = await send_dialogue(config, req)

        assert resp.acknowledged is False
        assert resp.status == DialogueStatus.NEED_HELP

    @pytest.mark.asyncio
    async def test_genesis_503_bootstrapping(self, config: GuardianConfig) -> None:
        import urllib.error
        import urllib.request

        error = urllib.error.HTTPError(
            url="http://test", code=503, msg="Service Unavailable",
            hdrs=None, fp=None,
        )
        # HTTPError.read() needs to return bytes
        error.read = lambda: b"bootstrapping"

        with patch.object(urllib.request, "urlopen", side_effect=error):
            req = DialogueRequest(["health_api"], [], 60.0, "surveying", {})
            resp = await send_dialogue(config, req)

        assert resp.acknowledged is False
        assert "503" in resp.context


class TestDialogueStateMachineIntegration:
    """Test the new state machine states for dialogue."""

    def test_contacting_genesis_recovers(self) -> None:
        from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState

        config = GuardianConfig()
        sm = ConfirmationStateMachine(config)
        sm.set_contacting_genesis()

        # All signals recover during dialogue
        healthy = HealthSnapshot(
            signals={
                name: SignalResult(name, True, 1.0, "ok", "t")
                for name in ["container_exists", "icmp_reachable", "health_api",
                              "heartbeat_canary", "log_freshness"]
            },
            pause_state=PauseState(paused=False),
        )
        t = sm.process(healthy)
        assert t.new_state == GuardianState.HEALTHY
        assert "recovered during dialogue" in t.reason

    def test_awaiting_self_heal_succeeds(self) -> None:
        from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState

        config = GuardianConfig()
        sm = ConfirmationStateMachine(config)
        sm.set_awaiting_self_heal(action="restarting bridge", eta_s=60)

        healthy = HealthSnapshot(
            signals={
                name: SignalResult(name, True, 1.0, "ok", "t")
                for name in ["container_exists", "icmp_reachable", "health_api",
                              "heartbeat_canary", "log_freshness"]
            },
            pause_state=PauseState(paused=False),
        )
        t = sm.process(healthy)
        assert t.new_state == GuardianState.HEALTHY
        assert "self-healed" in t.reason

    def test_awaiting_self_heal_eta_expires(self) -> None:
        from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState

        config = GuardianConfig()
        sm = ConfirmationStateMachine(config)
        sm.set_awaiting_self_heal(action="restarting bridge", eta_s=60)
        # Set dialogue_sent_at to the past so ETA is expired
        sm._state.dialogue_sent_at = "2026-03-25T11:00:00+00:00"

        dead = HealthSnapshot(
            signals={
                "container_exists": SignalResult("container_exists", False, 1.0, "down", "t"),
                "icmp_reachable": SignalResult("icmp_reachable", False, 1.0, "down", "t"),
            },
            pause_state=PauseState(paused=False),
        )
        t = sm.process(dead)
        assert t.new_state == GuardianState.CONFIRMED_DEAD
        assert "ETA expired" in t.reason
        assert t.action_needed is True

    def test_awaiting_self_heal_still_waiting(self) -> None:
        from datetime import UTC, datetime

        from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState

        config = GuardianConfig()
        sm = ConfirmationStateMachine(config)
        sm.set_awaiting_self_heal(action="restarting bridge", eta_s=300)
        # ETA is 5 min, set sent_at to now so it hasn't expired
        sm._state.dialogue_sent_at = datetime.now(UTC).isoformat()

        dead = HealthSnapshot(
            signals={
                "container_exists": SignalResult("container_exists", True, 1.0, "ok", "t"),
                "health_api": SignalResult("health_api", False, 1.0, "down", "t"),
            },
            pause_state=PauseState(paused=False),
        )
        t = sm.process(dead)
        assert t.new_state == GuardianState.AWAITING_SELF_HEAL
        assert "waiting" in t.reason.lower()
