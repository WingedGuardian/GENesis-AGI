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
from genesis.outreach.types import (
    OutreachCategory,
    OutreachRequest,
    OutreachResult,
    OutreachStatus,
)


class _FakePipeline:
    """Minimal OutreachPipeline stub used by AutonomousCliApprovalGate tests.

    Records the text / OutreachRequest / reply_markup that the gate
    submitted and returns a successful ``OutreachResult`` with a unique
    ``delivery_id`` per call so tests can target specific messages.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, OutreachRequest, object | None]] = []
        # Tests that want to resolve "the first delivery" can use
        # ``delivery-1`` as the key — for multi-request tests, look at
        # ``self.delivery_ids`` in order.
        self.delivery_ids: list[str] = []

    async def submit_raw(
        self, text: str, request: OutreachRequest,
        *, reply_markup: object | None = None,
    ) -> OutreachResult:
        self.sent.append((text, request, reply_markup))
        delivery_id = f"delivery-{len(self.sent)}"
        self.delivery_ids.append(delivery_id)
        return OutreachResult(
            outreach_id=f"outreach-fake-{len(self.sent)}",
            status=OutreachStatus.DELIVERED,
            channel="telegram",
            message_content=text,
            delivery_id=delivery_id,
        )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def runtime():
    pipeline = _FakePipeline()
    return SimpleNamespace(_outreach_pipeline=pipeline, pipeline=pipeline)


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
    # The gate must have routed the message through the outreach pipeline
    # (not directly into the adapter) so topic routing sends it to the
    # "Approvals" supergroup topic.
    assert runtime.pipeline.sent
    sent_text, sent_request, sent_markup = runtime.pipeline.sent[0]
    assert sent_request.category == OutreachCategory.APPROVAL
    assert sent_request.signal_type == "cli_approval"
    assert request_id in sent_request.source_id
    # For a single pending request, only the single-approve button row is
    # shown (no "approve all" batch button).
    assert sent_markup is not None


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
async def test_dispatch_router_empty_content_is_not_success(approval_gate):
    """Regression: provider returning HTTP 200 with empty content must
    NOT be treated as successful API dispatch. This was the failure mode
    where gemini-free returned empty on contingency_inbox and silently
    produced frontmatter-only inbox response files."""
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=True,
        content="",  # empty content with success=True — the bug
        provider_used="gemini-free",
        model_id="gemini-1.5",
        cost_usd=0.0,
        input_tokens=5,
        output_tokens=0,
        error=None,
    ))
    dispatch = AutonomousDispatchRouter(router=router, approval_gate=approval_gate)

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="inbox",
        policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        messages=[{"role": "user", "content": "eval this"}],
        cli_invocation=_invocation(),
        api_call_site_id="contingency_micro",  # any API call site works
    ))

    # Should NOT return mode="api" — empty content means the API path
    # failed to produce usable output. Should fall through to the
    # approval-gated CLI path (which will be blocked because nothing
    # has approved yet, but the important thing is mode != "api").
    assert decision.mode != "api"
    assert decision.output is None
    # The api_error must describe the empty-content failure so it
    # flows into the Telegram approval prompt context for the user.
    assert decision.api_error is not None
    assert "empty content" in decision.api_error
    assert "gemini-free" in decision.api_error


@pytest.mark.asyncio
async def test_dispatch_router_whitespace_content_is_not_success(approval_gate):
    """Whitespace-only content is also empty for dispatch purposes."""
    router = AsyncMock()
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=True,
        content="   \n\t  \n  ",
        provider_used="mistral-large-free",
        model_id="mistral-7b",
        cost_usd=0.0,
        input_tokens=5,
        output_tokens=0,
        error=None,
    ))
    dispatch = AutonomousDispatchRouter(router=router, approval_gate=approval_gate)

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="ego",
        policy_id="ego_cycle",
        action_label="ego cycle",
        messages=[{"role": "user", "content": "hi"}],
        cli_invocation=_invocation(),
        api_call_site_id="7_ego_cycle",
    ))

    assert decision.mode != "api"


@pytest.mark.asyncio
async def test_dispatch_router_skips_api_when_call_site_none(approval_gate):
    """When api_call_site_id is None, the router must not call route_call
    at all and should flow directly to the approval-gated CLI path. This
    is the mode inbox uses — approval-gate-only, no API fallback."""
    router = AsyncMock()
    router.route_call = AsyncMock()  # should NOT be called
    dispatch = AutonomousDispatchRouter(router=router, approval_gate=approval_gate)

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="inbox",
        policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        messages=[{"role": "user", "content": "eval"}],
        cli_invocation=_invocation(),
        api_call_site_id=None,
    ))

    router.route_call.assert_not_called()
    # Mode will be "blocked" because no approval has been given yet —
    # but critically, it's not "api" and the router was not touched.
    assert decision.mode != "api"


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
        api_call_site_id="7_ego_cycle",
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


@pytest.mark.asyncio
async def test_find_site_pending_returns_matching_pending(
    approval_gate, approval_manager,
):
    """find_site_pending returns a pending row whose context matches
    the (subsystem, policy_id) tuple."""
    # Create a pending approval for (reflection, reflection_deep)
    await approval_gate.ensure_approval(
        subsystem="reflection",
        policy_id="reflection_deep",
        action_label="deep reflection",
        invocation=_invocation(),
        api_call_site_id="5_deep_reflection",
        api_error="api failed",
    )

    found = await approval_gate.find_site_pending(
        subsystem="reflection", policy_id="reflection_deep",
    )
    assert found is not None
    assert found["status"] == "pending"
    assert found["action_type"] == "autonomous_cli_fallback"


@pytest.mark.asyncio
async def test_find_site_pending_returns_none_for_no_match(
    approval_gate, approval_manager,
):
    """find_site_pending returns None when no matching pending row exists."""
    # Create a pending approval for a DIFFERENT site
    await approval_gate.ensure_approval(
        subsystem="ego",
        policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=_invocation(),
        api_call_site_id="7_ego_cycle",
        api_error="api failed",
    )

    # Look up a different site — no match
    found = await approval_gate.find_site_pending(
        subsystem="reflection", policy_id="reflection_deep",
    )
    assert found is None


@pytest.mark.asyncio
async def test_find_site_pending_ignores_non_pending(
    approval_gate, approval_manager,
):
    """find_site_pending skips rejected and approved rows — only pending counts."""
    _, request_id, _ = await approval_gate.ensure_approval(
        subsystem="inbox",
        policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None,
        api_error=None,
    )
    # Resolve the row as rejected
    await approval_manager.resolve(
        request_id, status="rejected", resolved_by="test",
    )

    found = await approval_gate.find_site_pending(
        subsystem="inbox", policy_id="inbox_evaluation",
    )
    assert found is None


@pytest.mark.asyncio
async def test_get_pending_count_counts_only_cli_fallback(
    approval_gate, approval_manager,
):
    """get_pending_count counts only autonomous_cli_fallback, not other
    approval types."""
    assert await approval_gate.get_pending_count() == 0

    # Add two CLI fallback approvals
    await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=CCInvocation(
            prompt="different prompt",
            model=CCModel.SONNET, effort=EffortLevel.HIGH,
            system_prompt="sys",
        ),
        api_call_site_id="7_ego_cycle", api_error="api down",
    )

    assert await approval_gate.get_pending_count() == 2

    # Add an unrelated approval request directly via the manager
    # (valid action_class per DB CHECK: reversible|costly_reversible|irreversible)
    await approval_manager.request_approval(
        action_type="ego_proposal_review",
        action_class="reversible",
        description="unrelated",
        context="{}",
    )
    # Count should remain 2 (not 3) — scoped to autonomous_cli_fallback
    assert await approval_gate.get_pending_count() == 2


@pytest.mark.asyncio
async def test_each_cli_tick_creates_new_approval_row(
    runtime, approval_gate, approval_manager,
):
    """For autonomous_cli_fallback, each tick with different content
    creates its own pending row + Telegram message.  Pass 2 (race-safety
    dedup by subsystem/policy_id) is intentionally skipped so the user
    sees every approval request."""
    _, first_id, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=_invocation(),
        api_call_site_id="7_ego_cycle", api_error=None,
    )

    # Second call — different prompt (different approval_key), same site.
    different_invocation = CCInvocation(
        prompt="tick 2: different context, different prompt",
        model=CCModel.SONNET, effort=EffortLevel.HIGH,
        system_prompt="System",
    )
    status, second_id, reason = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=different_invocation,
        api_call_site_id="7_ego_cycle", api_error=None,
    )
    # Each tick creates its own row — NOT deduplicated
    assert status == "pending"
    assert second_id != first_id
    assert "requested" in reason
    # Two separate Telegram messages sent
    assert len(runtime.pipeline.sent) == 2


@pytest.mark.asyncio
async def test_sentinel_still_deduplicates_via_pass2(
    runtime, approval_gate, approval_manager,
):
    """Sentinel action types retain Pass 2 dedup — alarm-based triggers
    should NOT spam the user with duplicate messages for the same alarm."""
    _, first_id, _ = await approval_gate.ensure_approval(
        subsystem="sentinel", policy_id="sentinel_dispatch",
        action_label="Sentinel dispatch: container OOM",
        action_type="sentinel_dispatch",
        invocation=None,
        extra_context={
            "tier_label": "Tier 2",
            "trigger_source": "guardian",
            "trigger_reason": "container OOM",
            "alarm_count": 1,
        },
    )

    # Second call — different extra_context (different approval_key)
    # but same (subsystem, policy_id) and sentinel action_type.
    status, second_id, reason = await approval_gate.ensure_approval(
        subsystem="sentinel", policy_id="sentinel_dispatch",
        action_label="Sentinel dispatch: container OOM again",
        action_type="sentinel_dispatch",
        invocation=None,
        extra_context={
            "tier_label": "Tier 2",
            "trigger_source": "guardian",
            "trigger_reason": "container OOM — second alarm",
            "alarm_count": 2,
        },
    )
    # Sentinel DOES deduplicate — Pass 2 still active
    assert status == "pending"
    assert second_id == first_id
    # Only one Telegram message sent (first call)
    assert len(runtime.pipeline.sent) == 1


@pytest.mark.asyncio
async def test_stable_key_one_approval_one_dispatch(
    runtime, approval_gate, approval_manager,
):
    """With a stable approval key (no invocation in key), one pending
    request is shared across ticks. Approving it authorizes exactly
    one dispatch; after consumption, a new pending request is created."""
    # Tick 1: create pending request (stable key = no invocation)
    status1, req_id1, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=None,  # stable key
        api_call_site_id="7_ego_cycle", api_error=None,
    )
    assert status1 == "pending"

    # Tick 2: same stable key — finds existing pending row, not a new one
    status2, req_id2, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=None,
        api_call_site_id="7_ego_cycle", api_error=None,
    )
    assert status2 == "pending"
    assert req_id2 == req_id1  # same row reused

    # Approve it
    count = await approval_gate.approve_all_pending(resolved_by="test:batch")
    assert count == 1

    # Tick 3: finds approved-unconsumed row
    status3, req_id3, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=None,
        api_call_site_id="7_ego_cycle", api_error=None,
    )
    assert status3 == "approved"
    assert req_id3 == req_id1

    # Consume it
    consumed = await approval_gate.mark_consumed(req_id3)
    assert consumed is True

    # Double-consume fails
    assert await approval_gate.mark_consumed(req_id3) is False

    # Tick 4: consumed row skipped, NEW pending request created
    status4, req_id4, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=None,
        api_call_site_id="7_ego_cycle", api_error=None,
    )
    assert status4 == "pending"
    assert req_id4 != req_id1  # brand new row


@pytest.mark.asyncio
async def test_resolve_from_reply_no_longer_auto_batches(
    approval_gate, approval_manager,
):
    """resolve_from_reply must resolve ONLY the keyed request.  The
    old 'single approve batch-approves everything' behavior is removed;
    batch approval is now explicit via the Approve-all button."""
    # Create two pending approvals with different prompts
    _, first_id, _ = await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    _, second_id, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=CCInvocation(
            prompt="different prompt for ego",
            model=CCModel.SONNET, effort=EffortLevel.HIGH,
            system_prompt="sys",
        ),
        api_call_site_id="7_ego_cycle", api_error="api down",
    )
    assert first_id != second_id
    assert await approval_gate.get_pending_count() == 2

    # Approve the first via reply — the second must remain pending
    ok = await approval_gate.resolve_from_reply("delivery-1", "approve")
    assert ok is True

    first_row = await approval_manager.get_by_id(first_id)
    second_row = await approval_manager.get_by_id(second_id)
    assert first_row["status"] == "approved"
    assert second_row["status"] == "pending", (
        "single-reply approve must NOT auto-batch — the second "
        "approval must remain pending"
    )


@pytest.mark.asyncio
async def test_resolve_most_recent_pending_resolves_latest(
    approval_gate, approval_manager,
):
    """resolve_most_recent_pending resolves the most recently created
    pending autonomous_cli_fallback approval (used by the bare-text-
    reply handler in the Approvals topic)."""
    _, first_id, _ = await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    _, second_id, _ = await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=CCInvocation(
            prompt="the most recent request",
            model=CCModel.SONNET, effort=EffortLevel.HIGH,
            system_prompt="sys",
        ),
        api_call_site_id="7_ego_cycle", api_error="api down",
    )

    resolved = await approval_gate.resolve_most_recent_pending(
        decision="approved", resolved_by="telegram:bare_text",
    )
    assert resolved == second_id

    first_row = await approval_manager.get_by_id(first_id)
    second_row = await approval_manager.get_by_id(second_id)
    assert first_row["status"] == "pending"
    assert second_row["status"] == "approved"


@pytest.mark.asyncio
async def test_send_request_builds_batch_button_when_two_pending(
    runtime, approval_gate, approval_manager,
):
    """When pending_count >= 2 at send time, the inline keyboard must
    include the 'Approve all N pending' batch row in addition to the
    single-approve row."""
    # Create the first approval (runs _send_request → single button)
    await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    # Create a SECOND approval — now _send_request runs with
    # pending_count == 2, so the batch button appears.
    await approval_gate.ensure_approval(
        subsystem="ego", policy_id="ego_cycle",
        action_label="ego cycle",
        invocation=CCInvocation(
            prompt="second prompt for the batch test",
            model=CCModel.SONNET, effort=EffortLevel.HIGH,
            system_prompt="sys",
        ),
        api_call_site_id="7_ego_cycle", api_error="down",
    )

    assert len(runtime.pipeline.sent) == 2
    first_markup = runtime.pipeline.sent[0][2]
    second_markup = runtime.pipeline.sent[1][2]
    # First send had pending_count=1 → single row only
    assert len(first_markup.inline_keyboard) == 1
    # Second send had pending_count=2 → two rows (single + batch)
    assert len(second_markup.inline_keyboard) == 2
    # The second row is the batch button with the "Approve all" callback prefix
    batch_button = second_markup.inline_keyboard[1][0]
    assert batch_button.callback_data.startswith("cli_approve_all:")


@pytest.mark.asyncio
async def test_failed_delivery_does_not_advance_reask_window(
    runtime, approval_gate, approval_manager,
):
    """If the outreach pipeline fails to deliver, ``_send_request`` must
    leave ``last_sent_at`` and ``next_reask_at`` unset so the very next
    scan tick can retry via ``_maybe_resend``.

    Regression guard for the silent-24h-stall bug: the prior code bumped
    the reask cadence unconditionally after calling submit_raw, even on
    exception or non-DELIVERED result.  That meant a single failed
    delivery made the approval invisible for ``reask_interval_hours``
    (24h default) because ``_maybe_resend`` short-circuited on the
    fresh ``next_reask_at`` in the future.
    """
    import json as _json

    # Force the pipeline to report FAILED (no delivery_id) on every call.
    async def _fail_submit(text, request, *, reply_markup=None):
        runtime.pipeline.sent.append((text, request, reply_markup))
        return OutreachResult(
            outreach_id="outreach-failed",
            status=OutreachStatus.FAILED,
            channel="telegram",
            message_content=text,
            error="simulated delivery failure",
        )

    runtime.pipeline.submit_raw = _fail_submit

    status, request_id, reason = await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    assert status == "pending"
    assert request_id is not None

    row = await approval_manager.get_by_id(request_id)
    assert row is not None
    context = _json.loads(row["context"])
    # delivery_id is recorded as None so the retry path can see "not delivered"
    assert context["delivery_id"] is None
    # Crucially, last_sent_at and next_reask_at must NOT be bumped —
    # otherwise _maybe_resend would sleep on a future next_reask_at
    # and never retry.
    assert context["last_sent_at"] is None
    assert context["next_reask_at"] is None


@pytest.mark.asyncio
async def test_successful_delivery_advances_reask_window(
    runtime, approval_gate, approval_manager,
):
    """Positive counterpart to the failed-delivery test: when delivery
    succeeds, last_sent_at and next_reask_at must be populated so the
    dedup logic in ``_maybe_resend`` can short-circuit until the reask
    window elapses."""
    import json as _json

    status, request_id, _ = await approval_gate.ensure_approval(
        subsystem="inbox", policy_id="inbox_evaluation",
        action_label="inbox evaluation",
        invocation=_invocation(),
        api_call_site_id=None, api_error=None,
    )
    assert status == "pending"

    row = await approval_manager.get_by_id(request_id)
    context = _json.loads(row["context"])
    assert context["delivery_id"] is not None
    assert context["last_sent_at"] is not None
    assert context["next_reask_at"] is not None


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
    assert status["effective_policy"]["manual_approval_required"] is False


# ---------------------------------------------------------------------------
# F1: dispatch mode honouring — config-driven + explicit override.
#
# These tests cover the per-call-site ``dispatch`` field on
# ``CallSiteConfig`` and the matching explicit ``dispatch_mode`` override
# on ``AutonomousDispatchRequest``.  They use a real ``RoutingConfig``
# instance rather than an ``AsyncMock`` so the router's
# ``_resolve_dispatch_mode`` path is exercised end-to-end.
# ---------------------------------------------------------------------------


def _router_with_dispatch(call_site_id: str, dispatch: str) -> AsyncMock:
    """Build a router stub whose ``config.call_sites[<id>].dispatch`` is set.

    ``AutonomousDispatchRouter._resolve_dispatch_mode`` reads the field
    via ``self._router.config.call_sites``, so the stub needs a real
    dict containing a CallSiteConfig-like object (SimpleNamespace is
    sufficient because we only read ``.dispatch``).
    """
    from genesis.routing.types import CallSiteConfig
    site = CallSiteConfig(
        id=call_site_id,
        chain=["claude-sonnet"],
        dispatch=dispatch,
    )
    router = AsyncMock()
    router.config = SimpleNamespace(call_sites={call_site_id: site})
    router.route_call = AsyncMock()
    return router


@pytest.mark.asyncio
async def test_dispatch_cli_skips_api_chain(approval_gate):
    """dispatch=cli must bypass the API chain entirely and go straight to
    the approval-gated CLI path.  This is the load-bearing fix: the user
    wants to flip a call site to CLI from the neural monitor and have the
    runtime actually honour it."""
    router = _router_with_dispatch("5_deep_reflection", "cli")
    dispatch = AutonomousDispatchRouter(
        router=router, approval_gate=approval_gate,
    )

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_deep",
        action_label="deep reflection",
        messages=[{"role": "user", "content": "reflect"}],
        cli_invocation=_invocation(),
        api_call_site_id="5_deep_reflection",
    ))

    # Router's API path must NOT be called — the whole point is that
    # "cli" bypasses it.
    router.route_call.assert_not_called()
    # Decision must surface an api_error explaining why CLI was forced,
    # so the approval prompt tells the operator what's happening.
    assert decision.api_error is not None
    assert "cli" in decision.api_error.lower()


@pytest.mark.asyncio
async def test_dispatch_api_blocks_on_exhaustion(approval_gate):
    """dispatch=api must NEVER escalate to CLI.  When the API chain
    exhausts, return mode=blocked with a reason that references the
    call-site config — not silently drop into CC subprocess dispatch
    that the operator explicitly disabled."""
    router = _router_with_dispatch("4_light_reflection", "api")
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=False,
        content=None,
        provider_used="claude-sonnet",
        model_id=None,
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
        error="authentication_error: x-api-key required",
    ))
    dispatch = AutonomousDispatchRouter(
        router=router, approval_gate=approval_gate,
    )

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_light",
        action_label="light reflection",
        messages=[{"role": "user", "content": "reflect"}],
        cli_invocation=_invocation(),
        api_call_site_id="4_light_reflection",
    ))

    router.route_call.assert_called_once()
    assert decision.mode == "blocked"
    assert "dispatch=api" in decision.reason
    # The API error must flow through so the caller can log / surface
    # the actual root cause (auth error, rate limit, etc.).
    assert decision.api_error is not None
    assert "authentication_error" in decision.api_error


@pytest.mark.asyncio
async def test_dispatch_dual_default_preserves_existing_behavior(approval_gate):
    """Call sites without an explicit dispatch field must behave
    identically to the pre-F1 world: API chain first, CLI fallback on
    failure.  This is the regression guard for every existing call site
    that doesn't set ``dispatch`` in model_routing.yaml."""
    router = _router_with_dispatch("4_light_reflection", "dual")
    router.route_call = AsyncMock(return_value=SimpleNamespace(
        success=True,
        content="dual-mode api response",
        provider_used="claude-sonnet",
        model_id="claude-sonnet-4",
        cost_usd=0.05,
        input_tokens=10,
        output_tokens=20,
    ))
    dispatch = AutonomousDispatchRouter(
        router=router, approval_gate=approval_gate,
    )

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_light",
        action_label="light reflection",
        messages=[{"role": "user", "content": "reflect"}],
        cli_invocation=_invocation(),
        api_call_site_id="4_light_reflection",
    ))

    assert decision.mode == "api"
    assert decision.output is not None
    assert decision.output.text == "dual-mode api response"


@pytest.mark.asyncio
async def test_dispatch_explicit_override_wins_over_config(approval_gate):
    """A per-call ``dispatch_mode`` on the request bypasses the config
    lookup.  Used for tests and targeted one-shot overrides (e.g. a
    future '/reflect --force-cli' admin command)."""
    # Config says dual, but the request overrides to cli.
    router = _router_with_dispatch("4_light_reflection", "dual")
    dispatch = AutonomousDispatchRouter(
        router=router, approval_gate=approval_gate,
    )

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_light",
        action_label="light reflection",
        messages=[{"role": "user", "content": "reflect"}],
        cli_invocation=_invocation(),
        api_call_site_id="4_light_reflection",
        dispatch_mode="cli",
    ))

    router.route_call.assert_not_called()
    assert decision.api_error is not None
    assert "cli" in decision.api_error.lower()


@pytest.mark.asyncio
async def test_dispatch_cli_honours_fallback_disabled_flag(approval_gate):
    """dispatch=cli + cli_fallback_allowed=False must still return
    blocked — a caller that explicitly disables CLI fallback (e.g. a
    cost-sensitive call site) must not have that overridden by the
    user-level dispatch toggle."""
    router = _router_with_dispatch("5_deep_reflection", "cli")
    dispatch = AutonomousDispatchRouter(
        router=router, approval_gate=approval_gate,
    )

    decision = await dispatch.route(AutonomousDispatchRequest(
        subsystem="reflection",
        policy_id="reflection_deep",
        action_label="deep reflection",
        messages=[{"role": "user", "content": "reflect"}],
        cli_invocation=_invocation(),
        api_call_site_id="5_deep_reflection",
        cli_fallback_allowed=False,
    ))

    router.route_call.assert_not_called()
    assert decision.mode == "blocked"
    assert "CLI fallback disabled" in decision.reason
