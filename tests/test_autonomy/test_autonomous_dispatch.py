from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.autonomous_dispatch import (
    AutonomousCliApprovalGate,
    AutonomousDispatchRequest,
    AutonomousDispatchRouter,
)
from genesis.autonomy.cli_policy import (
    AutonomousCliPolicyExporter,
    load_autonomous_cli_policy,
)
from genesis.cc.types import CCInvocation, CCModel, EffortLevel
from genesis.db.schema import create_all_tables


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, recipient: str, text: str, **kwargs) -> str:
        self.sent.append((recipient, text))
        return "delivery-1"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def runtime():
    adapter = _FakeAdapter()
    pipeline = SimpleNamespace(
        _channels={"telegram": adapter},
        _recipients={"telegram": "12345"},
    )
    return SimpleNamespace(_outreach_pipeline=pipeline, adapter=adapter)


@pytest.fixture
def approval_manager(db):
    return ApprovalManager(db=db)


@pytest.fixture
def approval_gate(runtime, approval_manager):
    return AutonomousCliApprovalGate(
        runtime=runtime,
        approval_manager=approval_manager,
    )


def _invocation() -> CCInvocation:
    return CCInvocation(
        prompt="Please analyze this",
        model=CCModel.SONNET,
        effort=EffortLevel.HIGH,
        system_prompt="System",
    )


@pytest.mark.asyncio
async def test_approval_gate_requests_and_delivers(runtime, approval_gate, approval_manager):
    status, request_id, reason = await approval_gate.ensure_approval(
        subsystem="reflection",
        policy_id="reflection_deep",
        action_label="deep reflection",
        invocation=_invocation(),
        api_call_site_id="5_deep_reflection",
        api_error="All providers exhausted",
    )

    assert status == "pending"
    assert request_id
    assert "requested" in reason
    row = await approval_manager.get_by_id(request_id)
    assert row is not None
    assert runtime.adapter.sent


@pytest.mark.asyncio
async def test_approval_gate_reply_resolves_request(approval_gate, approval_manager):
    status, request_id, _ = await approval_gate.ensure_approval(
        subsystem="reflection",
        policy_id="reflection_deep",
        action_label="deep reflection",
        invocation=_invocation(),
        api_call_site_id="5_deep_reflection",
        api_error="api failed",
    )
    assert status == "pending"

    assert await approval_gate.resolve_from_reply("delivery-1", "approve") is True
    row = await approval_manager.get_by_id(request_id)
    assert row["status"] == "approved"


@pytest.mark.asyncio
async def test_dispatch_router_uses_api_first(approval_gate):
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=True,
        content="api-response",
        provider_used="claude-sonnet",
        model_id="claude-sonnet-4",
        cost_usd=0.12,
        input_tokens=10,
        output_tokens=20,
    ))
    dispatch = AutonomousDispatchRouter(router=router, approval_gate=approval_gate)

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_light",
        action_label="light reflection",
        messages=[{"role": "user", "content": "hi"}],
        cli_invocation=_invocation(),
        api_call_site_id="4_light_reflection",
    ))

    assert decision.mode == "api"
    assert decision.output is not None
    assert decision.output.text == "api-response"


@pytest.mark.asyncio
async def test_dispatch_router_blocks_pending_cli_fallback(approval_gate):
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=False,
        error="no providers",
    ))
    dispatch = AutonomousDispatchRouter(router=router, approval_gate=approval_gate)

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="ego",
        policy_id="ego_cycle",
        action_label="ego cycle",
        messages=[{"role": "user", "content": "hi"}],
        cli_invocation=_invocation(),
        api_call_site_id="7_ego_cycle_api",
    ))

    assert decision.mode == "blocked"
    assert "approval" in decision.reason


def test_load_autonomous_cli_policy_from_yaml(tmp_path: Path):
    path = tmp_path / "autonomous_cli_policy.yaml"
    path.write_text(
        "autonomous_cli_fallback_enabled: false\n"
        "manual_approval_required: true\n"
        "reask_interval_hours: 12\n"
        "approval_channel: telegram\n"
        "shared_export_enabled: true\n",
    )

    policy = load_autonomous_cli_policy(path)

    assert policy.autonomous_cli_fallback_enabled is False
    assert policy.reask_interval_hours == 12
    assert policy.source.startswith("config:")


def test_policy_exporter_writes_shared_mount_json(tmp_path: Path):
    shared_dir = tmp_path / "shared"
    exporter = AutonomousCliPolicyExporter(
        policy_loader=lambda: load_autonomous_cli_policy(
            tmp_path / "missing-policy.yaml",
        ),
    )

    out = exporter.export(shared_dir)

    assert out is not None
    assert out.exists()
    status = exporter.status()
    assert status["last_export_path"] == str(out)
    assert status["effective_policy"]["manual_approval_required"] is True
