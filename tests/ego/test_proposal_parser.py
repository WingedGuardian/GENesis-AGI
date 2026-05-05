"""Tests for parse_proposal_decisions() — Telegram reply parser."""


from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.ego.proposals import parse_proposal_decisions


class TestBulkOperations:
    def test_approve_all(self):
        assert parse_proposal_decisions("approve all") == {0: ("approved", None)}

    def test_approved_all(self):
        assert parse_proposal_decisions("approved all") == {0: ("approved", None)}

    def test_accept_all(self):
        assert parse_proposal_decisions("accept all") == {0: ("approved", None)}

    def test_go_ahead(self):
        assert parse_proposal_decisions("go ahead") == {0: ("approved", None)}

    def test_reject_all(self):
        assert parse_proposal_decisions("reject all") == {0: ("rejected", None)}

    def test_deny_all(self):
        assert parse_proposal_decisions("deny all") == {0: ("rejected", None)}

    def test_case_insensitive(self):
        assert parse_proposal_decisions("APPROVE ALL") == {0: ("approved", None)}
        assert parse_proposal_decisions("Reject All") == {0: ("rejected", None)}

    def test_whitespace_tolerance(self):
        assert parse_proposal_decisions("  approve all  ") == {0: ("approved", None)}


class TestNumberedDecisions:
    def test_number_first(self):
        result = parse_proposal_decisions("1 approve")
        assert result == {1: ("approved", None)}

    def test_word_first(self):
        result = parse_proposal_decisions("approve 1")
        assert result == {1: ("approved", None)}

    def test_reject_with_reason(self):
        result = parse_proposal_decisions("2 reject: too expensive")
        assert result == {2: ("rejected", "too expensive")}

    def test_multiple_comma_separated(self):
        result = parse_proposal_decisions("1 approve, 2 reject: bad idea")
        assert result == {
            1: ("approved", None),
            2: ("rejected", "bad idea"),
        }

    def test_multiple_newline_separated(self):
        result = parse_proposal_decisions("1 approve\n2 reject\n3 approve")
        assert result == {
            1: ("approved", None),
            2: ("rejected", None),
            3: ("approved", None),
        }

    def test_mixed_formats(self):
        result = parse_proposal_decisions("approve 1, 2 reject: not now")
        assert result == {
            1: ("approved", None),
            2: ("rejected", "not now"),
        }

    def test_yes_and_no_synonyms(self):
        result = parse_proposal_decisions("1 yes, 2 no")
        assert result == {
            1: ("approved", None),
            2: ("rejected", None),
        }

    def test_ok_synonym(self):
        result = parse_proposal_decisions("1 ok")
        assert result == {1: ("approved", None)}

    def test_skip_synonym(self):
        result = parse_proposal_decisions("1 skip")
        assert result == {1: ("rejected", None)}


class TestFallthrough:
    """Cases that should return empty dict (fall through to correction store)."""

    def test_bare_approve(self):
        """Bare 'approve' without 'all' or number must NOT match."""
        assert parse_proposal_decisions("approve") == {}

    def test_bare_reject(self):
        assert parse_proposal_decisions("reject") == {}

    def test_conversational_text(self):
        assert parse_proposal_decisions("sounds good to me") == {}

    def test_empty_string(self):
        assert parse_proposal_decisions("") == {}

    def test_random_sentence(self):
        assert parse_proposal_decisions("I think we should reconsider the approach") == {}

    def test_number_without_action(self):
        """A number alone shouldn't match."""
        assert parse_proposal_decisions("1") == {}

    def test_partial_match_ignored(self):
        """Unknown action words are skipped, not treated as failures."""
        result = parse_proposal_decisions("1 approve, 2 maybe")
        assert result == {1: ("approved", None)}  # Only valid one parsed


class TestEdgeCases:
    def test_zero_index_ignored(self):
        """Index 0 is invalid for numbered decisions."""
        assert parse_proposal_decisions("0 approve") == {}

    def test_negative_index_ignored(self):
        assert parse_proposal_decisions("-1 approve") == {}

    def test_large_index(self):
        result = parse_proposal_decisions("99 approve")
        assert result == {99: ("approved", None)}

    def test_reason_with_colon(self):
        result = parse_proposal_decisions("1 reject: reason: has colons")
        assert result[1][0] == "rejected"
        # Reason captures everything after first colon
        assert "reason" in result[1][1]


# ---------------------------------------------------------------------------
# Integration test for _try_proposal_resolution handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_proposal_resolution_approve_all():
    """End-to-end: quote-reply 'approve all' resolves entire batch."""
    from genesis.channels.telegram._handler_messages import _try_proposal_resolution

    # Mock context
    ctx = MagicMock()
    ctx.db = AsyncMock()

    # Mock proposal_workflow
    workflow = AsyncMock()
    workflow.resolve_proposals = AsyncMock(return_value={
        "prop-1": "approved",
        "prop-2": "approved",
    })
    ctx.proposal_workflow = workflow

    # Mock message
    msg = MagicMock()
    msg.text = "approve all"
    msg.reply_text = AsyncMock()

    reply_to_id = "12345"

    with patch(
        "genesis.db.crud.ego.get_batch_for_delivery",
        new_callable=AsyncMock,
        return_value="batch-abc",
    ), patch(
        "genesis.db.crud.ego.list_proposals_by_batch",
        new_callable=AsyncMock,
        return_value=[{"id": "prop-1"}, {"id": "prop-2"}],
    ):
        result = await _try_proposal_resolution(ctx, msg, reply_to_id)

    assert result is True
    workflow.resolve_proposals.assert_called_once()
    call_args = workflow.resolve_proposals.call_args
    assert call_args[0][0] == "batch-abc"  # batch_id
    # All proposals should be approved
    decisions = call_args[0][1]
    assert all(s == "approved" for s, _ in decisions.values())
    msg.reply_text.assert_called_once()
    assert "2 approved" in msg.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_try_proposal_resolution_no_match():
    """Returns False when delivery_id doesn't match any batch."""
    from genesis.channels.telegram._handler_messages import _try_proposal_resolution

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.proposal_workflow = MagicMock()

    msg = MagicMock()
    msg.text = "approve all"

    with patch(
        "genesis.db.crud.ego.get_batch_for_delivery",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await _try_proposal_resolution(ctx, msg, "99999")

    assert result is False


@pytest.mark.asyncio
async def test_try_proposal_resolution_unparseable_falls_through():
    """Unparseable text returns False (falls through to correction store)."""
    from genesis.channels.telegram._handler_messages import _try_proposal_resolution

    ctx = MagicMock()
    ctx.db = AsyncMock()
    ctx.proposal_workflow = MagicMock()

    msg = MagicMock()
    msg.text = "hmm let me think about this"

    with patch(
        "genesis.db.crud.ego.get_batch_for_delivery",
        new_callable=AsyncMock,
        return_value="batch-xyz",
    ):
        result = await _try_proposal_resolution(ctx, msg, "12345")

    assert result is False


@pytest.mark.asyncio
async def test_try_proposal_resolution_numbered():
    """Numbered decisions resolve specific proposals."""
    from genesis.channels.telegram._handler_messages import _try_proposal_resolution

    ctx = MagicMock()
    ctx.db = AsyncMock()
    workflow = AsyncMock()
    workflow.resolve_proposals = AsyncMock(return_value={
        "prop-1": "approved",
        "prop-2": "rejected",
    })
    ctx.proposal_workflow = workflow

    msg = MagicMock()
    msg.text = "1 approve, 2 reject: bad idea"
    msg.reply_text = AsyncMock()

    with patch(
        "genesis.db.crud.ego.get_batch_for_delivery",
        new_callable=AsyncMock,
        return_value="batch-abc",
    ):
        result = await _try_proposal_resolution(ctx, msg, "12345")

    assert result is True
    call_args = workflow.resolve_proposals.call_args
    decisions = call_args[0][1]
    assert decisions[1] == ("approved", None)
    assert decisions[2] == ("rejected", "bad idea")
