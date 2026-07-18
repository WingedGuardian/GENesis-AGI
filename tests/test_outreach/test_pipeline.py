"""Tests for the outreach pipeline orchestrator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.content.types import DraftRequest, DraftResult, FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach.config import OutreachConfig, QuietHours
from genesis.outreach.governance import GovernanceGate
from genesis.outreach.pipeline import OutreachPipeline
from genesis.outreach.types import (
    OutreachCategory,
    OutreachRequest,
    OutreachStatus,
)


@pytest.fixture
def config():
    return OutreachConfig(
        # Quiet hours are pinned off by the autouse _disable_quiet_hours
        # fixture (conftest.py) so this can't flake on wall-clock time.
        quiet_hours=QuietHours(start="22:00", end="07:00"),
        channel_preferences={"default": "telegram"},
        thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
        max_daily=5,
        surplus_daily=1,
        content_daily=3,
        notification_daily=10,
        morning_report_time="07:00",
        engagement_timeout_hours=24,
        engagement_poll_minutes=60,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def mock_drafter():
    drafter = AsyncMock()
    drafter.draft.return_value = DraftResult(
        content=FormattedContent(text="Drafted message", target=FormatTarget.TELEGRAM,
                                  truncated=False, original_length=15),
        model_used="test-model",
        raw_draft="Drafted message",
    )
    return drafter


@pytest.fixture
def mock_formatter():
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(
        text="Formatted message", target=FormatTarget.TELEGRAM,
        truncated=False, original_length=17,
    )
    return formatter


@pytest.fixture
def mock_channel():
    adapter = AsyncMock()
    adapter.send_message.return_value = "delivery-123"
    return adapter


@pytest.mark.asyncio
async def test_submit_surplus_allowed(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Good insight",
        context="Relevant context",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.DELIVERED
    assert result.delivery_id == "delivery-123"
    assert result.channel == "telegram"
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_governance_denied(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Low salience",
        context="Not important",
        salience_score=0.3,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.REJECTED
    mock_channel.send_message.assert_not_called()
    # The denial reason must reach result.error so the drain's "will retry"
    # log is diagnosable (was blank: "not delivered (rejected: )").
    assert result.error
    assert result.error == result.governance_result.reason


@pytest.mark.asyncio
async def test_submit_urgent_bypasses_governance(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="All APIs down",
        context="Critical failure",
        salience_score=1.0,
        signal_type="critical_failure",
    )
    result = await pipeline.submit_urgent(req)
    assert result.status == OutreachStatus.DELIVERED
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_records_in_db(config, db, mock_drafter, mock_formatter, mock_channel):
    from genesis.db.crud import outreach as outreach_crud

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="DB test",
        context="Verify DB write",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    row = await outreach_crud.get_by_id(db, result.outreach_id)
    assert row is not None
    assert row["channel"] == "telegram"
    assert row["delivered_at"] is not None
    assert row["delivery_id"] == "delivery-123"


@pytest.mark.asyncio
async def test_submit_raw_skips_governance_and_drafter(config, db, mock_drafter, mock_formatter, mock_channel):
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Infrastructure Alert (batched)",
        context="Raw alert text",
        salience_score=1.0,
        signal_type="health_alert",
    )
    result = await pipeline.submit_raw("Pre-formatted alert text", req)
    assert result.status == OutreachStatus.DELIVERED
    assert result.delivery_id == "delivery-123"
    # Drafter should NOT be called
    mock_drafter.draft.assert_not_called()
    # Channel should receive the formatted text
    mock_channel.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_submit_raw_records_in_db(config, db, mock_drafter, mock_formatter, mock_channel):
    from genesis.db.crud import outreach as outreach_crud

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Infrastructure Alert (batched)",
        context="Raw alert text",
        salience_score=1.0,
        signal_type="health_alert",
    )
    result = await pipeline.submit_raw("Pre-formatted alert text", req)
    row = await outreach_crud.get_by_id(db, result.outreach_id)
    assert row is not None
    assert row["channel"] == "telegram"
    assert row["delivered_at"] is not None


@pytest.mark.asyncio
async def test_delivery_failure_defers(config, db, mock_drafter, mock_formatter):
    failing_channel = AsyncMock()
    failing_channel.send_message.side_effect = ConnectionError("Network down")
    mock_deferred = AsyncMock()

    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": failing_channel},
        deferred_queue=mock_deferred,
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Defer test",
        context="Will fail delivery",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.FAILED
    mock_deferred.enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_alert_submit_passes_system_prompt(config, db, mock_drafter, mock_formatter, mock_channel):
    """Alert/blocker drafts should include a system_prompt from OUTREACH_ALERT.md."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Code audit: high finding in invoker.py",
        context="Exception handling issue",
        salience_score=0.8,
        signal_type="code_audit",
    )
    await pipeline.submit(req)
    mock_drafter.draft.assert_called_once()
    draft_request = mock_drafter.draft.call_args[0][0]
    assert isinstance(draft_request, DraftRequest)
    assert draft_request.system_prompt is not None
    assert "ONLY the final message" in draft_request.system_prompt
    assert draft_request.tone == "urgent"


@pytest.fixture
def supergroup_topic_manager():
    """A TopicManager that resolves a forum thread_id (as in production)."""
    tm = MagicMock()
    tm.resolve_outreach_category.return_value = "general"
    tm.get_or_create_persistent = AsyncMock(return_value=42)
    return tm


@pytest.mark.asyncio
async def test_email_delivery_uses_email_recipient_not_forum_chat_id(
    config, db, mock_drafter, mock_formatter, supergroup_topic_manager
):
    """Email sends must NOT have their recipient overwritten with the Telegram
    forum chat id when the category routes to 'supergroup'.

    Regression for the incident where notification-category follow-up emails
    were addressed to the Telegram forum chat id (-1003741378738), which Gmail
    rejects as an invalid RFC 5321 address — poisoning the deferred queue and,
    via blocking SMTP, starving the event loop. The supergroup/forum routing
    override is Telegram-only.
    """
    email_adapter = AsyncMock()
    email_adapter.send_message.return_value = "<msgid@example.com>"

    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"email": email_adapter},
        db=db,
        config=config,
        recipients={"email": "prospect@example.com"},
    )
    pipeline.set_forum_chat_id(-1003741378738)
    pipeline.set_topic_manager(supergroup_topic_manager)

    request = OutreachRequest(
        category=OutreachCategory.NOTIFICATION,  # routes to supergroup by default
        topic="Follow-up",
        context="Following up on my note about Genesis",
        salience_score=0.6,
        signal_type="campaign_follow_up",
        channel="email",
    )

    result = await pipeline.submit_raw("Following up on my note", request)

    assert result.status == OutreachStatus.DELIVERED
    recipient_arg = email_adapter.send_message.call_args[0][0]
    assert recipient_arg == "prospect@example.com"
    # message_thread_id is a Telegram concept and must not leak to email
    assert email_adapter.send_message.call_args[1].get("message_thread_id") is None


@pytest.mark.asyncio
async def test_telegram_supergroup_routing_still_uses_forum_chat_id(
    config, db, mock_drafter, mock_formatter, supergroup_topic_manager
):
    """Regression guard: the channel-gating fix must NOT break Telegram.

    A supergroup-routed category on the Telegram channel must still deliver to
    the forum chat id with the resolved thread_id (the original 2026-04-10
    approval-routing behavior).
    """
    telegram_adapter = AsyncMock()
    telegram_adapter.send_message.return_value = "tg-123"

    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": telegram_adapter},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    pipeline.set_forum_chat_id(-1003741378738)
    pipeline.set_topic_manager(supergroup_topic_manager)

    request = OutreachRequest(
        category=OutreachCategory.NOTIFICATION,  # routes to supergroup
        topic="Approval",
        context="Approve pending action",
        salience_score=0.6,
        signal_type="approval",
        channel="telegram",
    )

    result = await pipeline.submit_raw("Approve pending action", request)

    assert result.status == OutreachStatus.DELIVERED
    # Telegram supergroup routing intact: delivered to forum chat id + thread
    assert telegram_adapter.send_message.call_args[0][0] == "-1003741378738"
    assert telegram_adapter.send_message.call_args[1].get("message_thread_id") == 42


@pytest.mark.asyncio
async def test_surplus_submit_no_system_prompt(config, db, mock_drafter, mock_formatter, mock_channel):
    """Non-urgent categories should NOT get the alert system prompt."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=mock_formatter,
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="Interesting finding",
        context="Some context",
        salience_score=0.9,
        signal_type="surplus_insight",
    )
    await pipeline.submit(req)
    mock_drafter.draft.assert_called_once()
    draft_request = mock_drafter.draft.call_args[0][0]
    assert draft_request.system_prompt is None
    assert draft_request.tone == "conversational"


@pytest.mark.asyncio
async def test_submit_to_email_scrubs_em_dash_end_to_end(config, db, mock_drafter):
    """E2E through the pipeline: a send to an external channel (email) arrives
    with the spaced em dash collapsed by the egress gate."""
    email_adapter = AsyncMock()
    email_adapter.send_message.return_value = "email-123"
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(
        text="ship it — now", target=FormatTarget.EMAIL,
    )
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter,
        formatter=formatter,
        channels={"email": email_adapter},
        db=db,
        config=config,
        recipients={"email": "prospect@example.com"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="t",
        context="c",
        salience_score=0.9,
        signal_type="surplus_insight",
        channel="email",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.DELIVERED
    sent_text = email_adapter.send_message.call_args.args[1]
    assert sent_text == "ship it—now"  # em dash collapsed by the egress gate


@pytest.mark.asyncio
async def test_submit_to_email_quarantines_secret_end_to_end(config, db, mock_drafter):
    """E2E: an external send carrying a secret is quarantined, never delivered."""
    email_adapter = AsyncMock()
    formatter = MagicMock()
    formatter.format.return_value = FormattedContent(
        text="the key is sk-abcdefghij1234567890ABCDXYZ",
        target=FormatTarget.EMAIL,
    )
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter,
        formatter=formatter,
        channels={"email": email_adapter},
        db=db,
        config=config,
        recipients={"email": "prospect@example.com"},
    )
    req = OutreachRequest(
        category=OutreachCategory.SURPLUS,
        topic="t",
        context="c",
        salience_score=0.9,
        signal_type="surplus_insight",
        channel="email",
    )
    result = await pipeline.submit(req)
    assert result.status == OutreachStatus.FAILED
    assert "quarantine" in (result.error or "").lower()
    email_adapter.send_message.assert_not_called()


# ── self-send / recipient-less terminal guards at the _deliver chokepoint ──


def _email_adapter(from_address):
    from genesis.channels.email_adapter import EmailAdapter
    a = EmailAdapter(
        smtp_host="smtp.invalid", smtp_port=465, username="u",
        password="p", from_address=from_address,
    )
    a.send_message = AsyncMock(return_value="<id@x>")
    return a


def test_email_adapter_exposes_from_address():
    assert _email_adapter("me@self.com").from_address == "me@self.com"


@pytest.mark.asyncio
async def test_email_to_own_address_is_ignored_not_held(
    config, db, mock_drafter, mock_formatter
):
    """A send to the agent's own address is terminally IGNORED — never HELD
    (approval flood) or DEFERred (retry loop)."""
    self_addr = "genesis@example.com"
    adapter = _email_adapter(self_addr)
    gate = AsyncMock()
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter, formatter=mock_formatter,
        channels={"email": adapter}, db=db, config=config,
        recipients={"email": self_addr},  # the misconfigured self default
    )
    pipeline._autonomy_gate = gate
    request = OutreachRequest(
        category=OutreachCategory.NOTIFICATION, topic="hi", context="body",
        salience_score=0.5, signal_type="x", channel="email",
    )

    result = await pipeline.submit_raw("body", request)

    assert result.status == OutreachStatus.IGNORED
    gate.check.assert_not_called()             # never reached the gate (no hold)
    adapter.send_message.assert_not_called()   # never sent


@pytest.mark.asyncio
async def test_email_without_recipient_is_ignored_not_deferred(
    config, db, mock_drafter, mock_formatter
):
    """A recipient-less email is terminally IGNORED, NOT FAILED/deferred — a
    deferred no-recipient email would just loop in the deferred-work queue."""
    adapter = _email_adapter("genesis@example.com")
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter, formatter=mock_formatter,
        channels={"email": adapter}, db=db, config=config,
        recipients={},  # no email default -> recipient resolves to ""
    )
    request = OutreachRequest(
        category=OutreachCategory.NOTIFICATION, topic="hi", context="body",
        salience_score=0.5, signal_type="x", channel="email",
    )

    result = await pipeline.submit_raw("body", request)

    assert result.status == OutreachStatus.IGNORED
    adapter.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_self_send_skipped_on_gate_cleared_resume(
    config, db, mock_drafter, mock_formatter
):
    """The self-guard fires even on the gate_cleared resume path
    (deliver_approved) — an approved self-send must still never be sent."""
    self_addr = "genesis@example.com"
    adapter = _email_adapter(self_addr)
    pipeline = OutreachPipeline(
        governance=GovernanceGate(config, db),
        drafter=mock_drafter, formatter=mock_formatter,
        channels={"email": adapter}, db=db, config=config,
        recipients={"email": self_addr},
    )
    request = OutreachRequest(
        category=OutreachCategory.NOTIFICATION, topic="t", context="c",
        salience_score=0.5, signal_type="x", channel="email",
        validated_recipient=self_addr,
    )
    formatted = FormattedContent(
        text="hi", target=FormatTarget.EMAIL, truncated=False, original_length=2,
    )

    result = await pipeline._deliver(
        "oid", "email", formatted, request, None, gate_cleared=True,
    )

    assert result.status == OutreachStatus.IGNORED
    adapter.send_message.assert_not_called()


# ── verbatim (factual notifications skip the LLM drafter) ──────────────────


def _echo_formatter():
    """A formatter that returns its input verbatim, so tests can assert the
    delivered text equals the source (the stock mock_formatter returns a
    constant)."""
    f = MagicMock()
    f.format.side_effect = lambda text, target: FormattedContent(
        text=text, target=target, truncated=False, original_length=len(text),
    )
    return f


@pytest.mark.asyncio
async def test_submit_verbatim_skips_drafter(config, db, mock_drafter, mock_channel):
    """A verbatim request must NOT touch the LLM drafter and must deliver the
    context byte-for-byte. This is the structural guarantee that a factual
    notification can never be creatively rewritten or fabricated."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=_echo_formatter(),
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Task abc123",
        context="Proceeding with task: build a thing (5 steps)",
        salience_score=0.9,
        signal_type="task_progress",
        source_id="task:abc123",
        verbatim=True,
    )
    result = await pipeline.submit(req)

    assert result.status == OutreachStatus.DELIVERED
    mock_drafter.draft.assert_not_called()          # the load-bearing assertion
    assert result.message_content == req.context    # delivered exactly


@pytest.mark.asyncio
async def test_submit_verbatim_empty_context_falls_back_to_topic(
    config, db, mock_drafter, mock_channel,
):
    """Verbatim with an empty context must never deliver an empty string —
    it falls back to the topic (defensive; addresses the reviewer footgun)."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=_echo_formatter(),
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Task abc123",
        context="",
        salience_score=0.9,
        signal_type="task_alert",
        verbatim=True,
    )
    result = await pipeline.submit(req)
    mock_drafter.draft.assert_not_called()
    assert result.message_content == "Task abc123"


@pytest.mark.asyncio
async def test_submit_non_verbatim_still_drafts(config, db, mock_drafter, mock_channel):
    """Regression guard: without verbatim, the drafter is still used (other
    outreach paths are unchanged)."""
    gate = GovernanceGate(config, db)
    pipeline = OutreachPipeline(
        governance=gate,
        drafter=mock_drafter,
        formatter=_echo_formatter(),
        channels={"telegram": mock_channel},
        db=db,
        config=config,
        recipients={"telegram": "12345"},
    )
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Health",
        context="disk at 92%",
        salience_score=0.9,
        signal_type="health_alert",
    )
    await pipeline.submit(req)
    mock_drafter.draft.assert_called_once()


# ── voice_text (short spoken TL;DR instead of the full text) ───────────────


def _voice_pipeline(config, db, mock_drafter):
    """Pipeline with a voice channel and task_alert on the voice allowlist."""
    import dataclasses

    voice_cfg = dataclasses.replace(
        config, voice_alert_ids=("task_alert", "task_complete"),
    )
    voice_channel = AsyncMock()
    voice_channel.send_message.return_value = "voice-1"
    telegram = AsyncMock()
    telegram.send_message.return_value = "tg-1"
    pipeline = OutreachPipeline(
        governance=GovernanceGate(voice_cfg, db),
        drafter=mock_drafter,
        formatter=_echo_formatter(),
        channels={"telegram": telegram, "voice": voice_channel},
        db=db,
        config=voice_cfg,
        recipients={"telegram": "12345"},
    )
    return pipeline, voice_channel


@pytest.mark.asyncio
async def test_voice_speaks_voice_text_not_full_message(config, db, mock_drafter):
    """When voice_text is set, the spoken string is the short TL;DR — NOT the
    full delivered text (which may carry paths / tokens the user shouldn't hear
    read aloud)."""
    from unittest.mock import patch

    pipeline, voice_channel = _voice_pipeline(config, db, mock_drafter)
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Task abc123",
        context="Build parked by scope gate: reason. Blocked paths: src/genesis/x.py",
        salience_score=0.9,
        signal_type="task_alert",
        source_id="task:abc123",
        verbatim=True,
        voice_text="A build was parked by the safety gate.",
    )
    with patch.object(pipeline, "_in_voice_hours", return_value=True):
        result = await pipeline.submit(req)
    await asyncio.sleep(0)  # let the fire-and-forget voice task record its call

    assert result.message_content == req.context  # Telegram keeps full detail
    voice_channel.send_message.assert_called_once()
    spoken = voice_channel.send_message.call_args.args[1]
    assert spoken == "A build was parked by the safety gate."
    assert "src/genesis" not in spoken  # no path read aloud


@pytest.mark.asyncio
async def test_voice_falls_back_to_full_text_without_voice_text(config, db, mock_drafter):
    """Callers that set no voice_text (e.g. health alerts) keep speaking their
    full text — unchanged behavior."""
    from unittest.mock import patch

    pipeline, voice_channel = _voice_pipeline(config, db, mock_drafter)
    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Task abc123",
        context="Task completed: build a thing",
        salience_score=0.9,
        signal_type="task_complete",
        source_id="task:abc123",
        verbatim=True,
    )
    with patch.object(pipeline, "_in_voice_hours", return_value=True):
        await pipeline.submit(req)
    await asyncio.sleep(0)

    voice_channel.send_message.assert_called_once()
    assert voice_channel.send_message.call_args.args[1] == "Task completed: build a thing"


# ── send-and-wait waiter ordering (context must attach to a REGISTERED waiter) ──
#
# Live failure 2026-07-16: submit_and_wait attached context before any waiter
# existed (registration happened later, inside wait_for_reply). set_context
# silently drops unregistered keys, so every waiter on this path was born
# contextless and standalone replies could never resolve it — they all
# degraded to implicit_activity. These tests drive the REAL ReplyWaiter
# through both send-and-wait paths and assert a standalone reply arriving
# mid-wait resolves the waiter.


def _delivered_result():
    from genesis.outreach.types import OutreachResult

    return OutreachResult(
        outreach_id="o-1", status=OutreachStatus.DELIVERED, channel="telegram",
        message_content="hi", delivery_id="555", chat_id="-100123", thread_id=42,
    )


async def _resolve_when_pending(waiter, text, thread_key):
    for _ in range(200):
        if waiter.pending_count:
            keys = waiter.resolve_scoped_pending(text, thread_key=thread_key)
            if keys:
                return keys
        await asyncio.sleep(0.005)
    return []


@pytest.mark.asyncio
async def test_submit_and_wait_standalone_reply_resolves(config):
    from genesis.outreach.reply_waiter import ReplyWaiter

    pipeline = OutreachPipeline.__new__(OutreachPipeline)
    pipeline._reply_waiter = ReplyWaiter()
    pipeline._inflight_awaited = set()
    pipeline.submit = AsyncMock(return_value=_delivered_result())

    task = asyncio.ensure_future(
        pipeline.submit_and_wait(MagicMock(), timeout_s=5.0),
    )
    keys = await _resolve_when_pending(
        pipeline._reply_waiter, "ok", "-100123:42",
    )
    assert keys == ["555"], "standalone reply did not resolve the waiter"
    result, reply = await task
    assert reply == "ok"
    assert pipeline._reply_waiter.pending_count == 0


@pytest.mark.asyncio
async def test_submit_raw_and_wait_no_key_standalone_reply_resolves(config):
    from genesis.outreach.reply_waiter import ReplyWaiter

    pipeline = OutreachPipeline.__new__(OutreachPipeline)
    pipeline._reply_waiter = ReplyWaiter()
    pipeline._inflight_awaited = set()
    pipeline.submit_raw = AsyncMock(return_value=_delivered_result())

    task = asyncio.ensure_future(
        pipeline.submit_raw_and_wait("txt", MagicMock(), timeout_s=5.0),
    )
    keys = await _resolve_when_pending(
        pipeline._reply_waiter, "approve", "-100123:42",
    )
    assert keys == ["555"]
    result, reply = await task
    assert reply == "approve"


@pytest.mark.asyncio
async def test_submit_raw_and_wait_with_key_still_resolves(config):
    """The pre-registered button-key path keeps working (context on both
    the uuid key and the delivery_id alias)."""
    from genesis.outreach.reply_waiter import ReplyWaiter

    pipeline = OutreachPipeline.__new__(OutreachPipeline)
    pipeline._reply_waiter = ReplyWaiter()
    pipeline._inflight_awaited = set()
    pipeline.submit_raw = AsyncMock(return_value=_delivered_result())

    task = asyncio.ensure_future(
        pipeline.submit_raw_and_wait(
            "txt", MagicMock(), timeout_s=5.0, waiter_key="uuid-1",
        ),
    )
    keys = await _resolve_when_pending(
        pipeline._reply_waiter, "go", "-100123:42",
    )
    assert set(keys) == {"uuid-1", "555"}
    result, reply = await task
    assert reply == "go"


@pytest.mark.asyncio
async def test_inflight_awaited_concurrent_duplicate_suppressed(config):
    """While an awaited approval is pending, a second identical one is
    SUPPRESSED (never sent) — two identical pending prompts would make a plain
    APPROVE ambiguous (resolve_scoped_pending len(eligible) != 1)."""
    from genesis.outreach.reply_waiter import ReplyWaiter

    pipeline = OutreachPipeline.__new__(OutreachPipeline)
    pipeline._reply_waiter = ReplyWaiter()
    pipeline._inflight_awaited = set()
    pipeline.submit_raw = AsyncMock(return_value=_delivered_result())

    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Grow container root to 40 GiB?",
        context="reply APPROVE / DENY",
        salience_score=1.0,
        signal_type="provision_approval",
    )

    # First call starts waiting (registers a waiter, blocks on the reply).
    task = asyncio.ensure_future(pipeline.submit_raw_and_wait("txt", req, timeout_s=5.0))
    for _ in range(200):
        if pipeline._reply_waiter.pending_count:
            break
        await asyncio.sleep(0.005)
    assert pipeline._reply_waiter.pending_count == 1

    # Second IDENTICAL call while the first is pending -> suppressed, no send.
    pipeline.submit_raw.reset_mock()
    result2, reply2 = await pipeline.submit_raw_and_wait("txt", req, timeout_s=5.0)
    assert result2.status == OutreachStatus.REJECTED
    assert reply2 is None
    pipeline.submit_raw.assert_not_called()
    assert pipeline._reply_waiter.pending_count == 1  # still exactly one prompt

    # Resolve the first; its key releases on completion.
    pipeline._reply_waiter.resolve_scoped_pending("APPROVE", thread_key="-100123:42")
    _r1, reply1 = await task
    assert reply1 == "APPROVE"
    assert pipeline._inflight_awaited == set()


@pytest.mark.asyncio
async def test_inflight_awaited_released_after_timeout_allows_retry(config):
    """A retry after the previous approval TIMED OUT is NOT suppressed — the
    in-flight key releases when the wait ends, the distinction that keeps the
    retry-after-timeout bug fixed (Phase-C 2026-07-18)."""
    from genesis.outreach.reply_waiter import ReplyWaiter

    pipeline = OutreachPipeline.__new__(OutreachPipeline)
    pipeline._reply_waiter = ReplyWaiter()
    pipeline._inflight_awaited = set()
    pipeline.submit_raw = AsyncMock(return_value=_delivered_result())

    req = OutreachRequest(
        category=OutreachCategory.BLOCKER,
        topic="Grow container root to 40 GiB?",
        context="reply APPROVE / DENY",
        salience_score=1.0,
        signal_type="provision_approval",
    )

    # First call times out (no resolver) -> key released.
    _r1, reply1 = await pipeline.submit_raw_and_wait("txt", req, timeout_s=0.05)
    assert reply1 is None
    assert pipeline._inflight_awaited == set()

    # Retry the SAME request -> NOT suppressed, delivered again.
    pipeline.submit_raw.reset_mock()
    _r2, reply2 = await pipeline.submit_raw_and_wait("txt", req, timeout_s=0.05)
    assert reply2 is None
    pipeline.submit_raw.assert_called_once()
