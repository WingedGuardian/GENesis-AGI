"""Tests for the Telegram callback query handler (inline button approval)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.outreach.reply_waiter import ReplyWaiter


def _make_ctx(*, reply_waiter=None, allowed_users=None):
    """Create a minimal HandlerContext-like object."""
    ctx = MagicMock()
    ctx.reply_waiter = reply_waiter
    ctx.authorized = MagicMock(side_effect=lambda uid: uid in (allowed_users or set()))
    return ctx


def _make_update(*, user_id, callback_data):
    """Create a minimal Update with callback_query."""
    update = MagicMock()
    update.effective_user = MagicMock()
    update.effective_user.id = user_id

    query = AsyncMock()
    query.data = callback_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()

    msg = MagicMock()
    msg.text_html = "Original message"
    msg.text = "Original message"
    query.message = msg

    update.callback_query = query
    return update


@pytest.mark.asyncio
async def test_callback_resolves_waiter():
    """Button press resolves the waiter with the action string."""
    waiter = ReplyWaiter()
    waiter.register("key-1")
    ctx = _make_ctx(reply_waiter=waiter, allowed_users={12345})
    update = _make_update(user_id=12345, callback_data="approve:key-1")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_awaited_once()
    call_kwargs = update.callback_query.edit_message_text.call_args
    assert "Approved" in call_kwargs.kwargs.get("text", call_kwargs.args[0] if call_kwargs.args else "")


@pytest.mark.asyncio
async def test_callback_rejects():
    """Reject button resolves with 'reject'."""
    waiter = ReplyWaiter()
    future = waiter.register("key-2")
    ctx = _make_ctx(reply_waiter=waiter, allowed_users={12345})
    update = _make_update(user_id=12345, callback_data="reject:key-2")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    assert future.done()
    assert future.result() == "reject"


@pytest.mark.asyncio
async def test_callback_unauthorized():
    """Unauthorized user gets rejected with alert."""
    waiter = ReplyWaiter()
    waiter.register("key-3")
    ctx = _make_ctx(reply_waiter=waiter, allowed_users={12345})
    update = _make_update(user_id=99999, callback_data="approve:key-3")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    update.callback_query.answer.assert_awaited_once_with("Not authorized", show_alert=True)
    # Waiter should NOT be resolved
    assert waiter.pending_count == 1


@pytest.mark.asyncio
async def test_callback_expired_waiter():
    """Button press on expired/processed waiter edits message with feedback."""
    waiter = ReplyWaiter()
    # Don't register — waiter doesn't exist
    ctx = _make_ctx(reply_waiter=waiter, allowed_users={12345})
    update = _make_update(user_id=12345, callback_data="approve:nonexistent")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    # Should edit message to show decision label with (expired) tag
    update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_malformed_data():
    """Malformed callback data (no colon) is silently ignored."""
    ctx = _make_ctx(reply_waiter=ReplyWaiter(), allowed_users={12345})
    update = _make_update(user_id=12345, callback_data="baddata")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    # Should answer the spinner but not try to resolve
    update.callback_query.answer.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()


# ---------------------------------------------------------------------------
# Autonomous CLI fallback: cli_approve and cli_approve_all
# ---------------------------------------------------------------------------


def _make_gate_ctx(*, allowed_users=None, resolve_ok=True, batch_count=0):
    """Create a ctx with an autonomous_cli_gate mock."""
    ctx = MagicMock()
    ctx.reply_waiter = None
    ctx.authorized = MagicMock(
        side_effect=lambda uid: uid in (allowed_users or set()),
    )
    gate = MagicMock()
    gate.resolve_request = AsyncMock(return_value=resolve_ok)
    gate.approve_all_pending = AsyncMock(return_value=batch_count)
    ctx.autonomous_cli_gate = gate
    return ctx, gate


@pytest.mark.asyncio
async def test_cli_approve_resolves_single_request():
    """cli_approve:{request_id} button resolves exactly that request
    via AutonomousCliApprovalGate.resolve_request, not the ReplyWaiter."""
    ctx, gate = _make_gate_ctx(allowed_users={12345})
    update = _make_update(user_id=12345, callback_data="cli_approve:req-abc")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    update.callback_query.answer.assert_awaited_once()
    gate.resolve_request.assert_awaited_once()
    call = gate.resolve_request.call_args
    assert call.args[0] == "req-abc"
    assert call.kwargs["decision"] == "approved"
    assert "telegram:button" in call.kwargs["resolved_by"]
    # approve_all_pending must NOT be called — this is single-approve
    gate.approve_all_pending.assert_not_called()
    # Message edit includes the decision
    update.callback_query.edit_message_text.assert_awaited_once()
    edit_text = update.callback_query.edit_message_text.call_args.kwargs["text"]
    assert "Approved" in edit_text


@pytest.mark.asyncio
async def test_cli_approve_all_resolves_batch():
    """cli_approve_all:{request_id} button resolves the triggering
    request AND calls approve_all_pending to clear the queue."""
    ctx, gate = _make_gate_ctx(allowed_users={12345}, batch_count=4)
    update = _make_update(user_id=12345, callback_data="cli_approve_all:req-xyz")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    # Both the individual resolve and the batch were called
    gate.resolve_request.assert_awaited_once()
    assert gate.resolve_request.call_args.args[0] == "req-xyz"
    gate.approve_all_pending.assert_awaited_once()
    # Edit message shows the total count
    edit_text = update.callback_query.edit_message_text.call_args.kwargs["text"]
    # 4 batch + 1 triggered = 5 total
    assert "5 total" in edit_text


@pytest.mark.asyncio
async def test_cli_approve_double_click_is_graceful():
    """A second cli_approve press on an already-resolved request
    (resolve_request returns False) must NOT crash — the handler should
    still edit the message with an 'already resolved' indicator."""
    ctx, gate = _make_gate_ctx(allowed_users={12345}, resolve_ok=False)
    update = _make_update(user_id=12345, callback_data="cli_approve:req-dup")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    # resolve_request was attempted
    gate.resolve_request.assert_awaited_once()
    # Message edit still happens, with "Already resolved" marker
    update.callback_query.edit_message_text.assert_awaited_once()
    edit_text = update.callback_query.edit_message_text.call_args.kwargs["text"]
    assert "Already resolved" in edit_text


@pytest.mark.asyncio
async def test_cli_approve_missing_gate_is_safe():
    """If autonomous_cli_gate is None (not wired), the handler must
    log an error and return WITHOUT crashing.  This guards against
    HandlerContext wiring regressions."""
    ctx = MagicMock()
    ctx.reply_waiter = None
    ctx.authorized = MagicMock(return_value=True)
    ctx.autonomous_cli_gate = None
    update = _make_update(user_id=12345, callback_data="cli_approve:req-none")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    # Should not raise
    await handle_callback_query(ctx, update, MagicMock())
    # Spinner still dismissed
    update.callback_query.answer.assert_awaited_once()
    # No message edit attempted (graceful bail)
    update.callback_query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_cli_approve_unauthorized_user():
    """Unauthorized user pressing cli_approve gets a rejection alert
    and the gate is NOT invoked."""
    ctx, gate = _make_gate_ctx(allowed_users={12345})
    update = _make_update(user_id=99999, callback_data="cli_approve:req-abc")

    from genesis.channels.telegram._handler_messages import handle_callback_query
    await handle_callback_query(ctx, update, MagicMock())

    update.callback_query.answer.assert_awaited_once_with(
        "Not authorized", show_alert=True,
    )
    gate.resolve_request.assert_not_called()


# ---------------------------------------------------------------------------
# Bare-text "approve"/"reject" in Approvals topic
# ---------------------------------------------------------------------------


def test_bare_decision_exact_word_match_only():
    """_bare_decision should only match single-token approve/reject
    words — general conversation must not trigger."""
    from genesis.channels.telegram._handler_messages import _bare_decision

    assert _bare_decision("approve") == "approved"
    assert _bare_decision("APPROVE") == "approved"
    assert _bare_decision("  approve  ") == "approved"
    assert _bare_decision("approved") == "approved"
    assert _bare_decision("ok") == "approved"
    assert _bare_decision("yes") == "approved"

    assert _bare_decision("reject") == "rejected"
    assert _bare_decision("REJECT") == "rejected"
    assert _bare_decision("no") == "rejected"
    assert _bare_decision("denied") == "rejected"

    # Not a bare word — must NOT match
    assert _bare_decision("") is None
    assert _bare_decision("approve this please") is None
    assert _bare_decision("please approve it") is None
    assert _bare_decision("I think we should approve") is None
    assert _bare_decision("yes, let's go") is None
    assert _bare_decision("random text") is None
    # Extra punctuation on a bare token still matches (user is informal)
    assert _bare_decision("approve!") == "approved"
    assert _bare_decision("reject.") == "rejected"
