"""Tests for the ego realist gate (dreamer/realist architecture)."""

import json

import pytest

from genesis.db.crud import ego as ego_crud
from genesis.ego.session import (
    _build_realist_prompt,
    _parse_realist_response,
)
from genesis.ego.types import NEUTRAL_STATUS

# -- Realist prompt construction tests --


class TestBuildRealistPrompt:
    def test_basic_prompt_structure(self):
        """Prompt contains rules, history, and proposals."""
        proposals = [
            {"action_type": "dispatch", "content": "Send report", "confidence": 0.8},
        ]
        history = [
            {
                "action_type": "investigate",
                "content": "Check drift",
                "status": "withdrawn",
                "created_at": "2026-05-13T10:00:00",
            },
        ]
        prompt = _build_realist_prompt(proposals, history)
        assert "Realist" in prompt
        assert "Read operations are NOT proposals" in prompt
        assert "zombies" in prompt.lower()
        assert "Send report" in prompt
        assert "Check drift" in prompt

    def test_empty_history(self):
        """Prompt handles no recent history."""
        proposals = [{"action_type": "dispatch", "content": "Test", "confidence": 0.5}]
        prompt = _build_realist_prompt(proposals, [])
        assert "No recent proposals" in prompt
        assert "Test" in prompt

    def test_history_status_uses_neutral_labels(self):
        """History table uses neutral status labels."""
        proposals = [{"action_type": "dispatch", "content": "Test", "confidence": 0.5}]
        history = [
            {
                "action_type": "investigate",
                "content": "Old proposal",
                "status": "withdrawn",
                "created_at": "2026-05-13T10:00:00",
            },
        ]
        prompt = _build_realist_prompt(proposals, history)
        assert "recycled" in prompt  # neutral label for withdrawn

    def test_multiple_proposals_indexed(self):
        """Each proposal gets a 0-based index."""
        proposals = [
            {"action_type": "dispatch", "content": "First", "confidence": 0.8},
            {"action_type": "investigate", "content": "Second", "confidence": 0.6},
        ]
        prompt = _build_realist_prompt(proposals, [])
        assert "0. [dispatch]" in prompt
        assert "1. [investigate]" in prompt

    def test_long_content_truncated(self):
        """Content longer than 300 chars is truncated in the prompt."""
        proposals = [
            {"action_type": "dispatch", "content": "X" * 500, "confidence": 0.5},
        ]
        prompt = _build_realist_prompt(proposals, [])
        # Prompt should contain truncated content, not the full 500 chars
        assert "X" * 300 in prompt
        assert "X" * 500 not in prompt


# -- Realist response parsing tests --


class TestParseRealistResponse:
    def test_parse_valid_json_array(self):
        """Parses a clean JSON array response."""
        response = json.dumps([
            {"index": 0, "verdict": "pass", "reasoning": "Good proposal"},
            {"index": 1, "verdict": "reject", "reasoning": "Zombie"},
        ])
        result = _parse_realist_response(response, 2)
        assert result[0]["verdict"] == "pass"
        assert result[1]["verdict"] == "reject"
        assert result[1]["reasoning"] == "Zombie"

    def test_parse_markdown_code_block(self):
        """Parses JSON inside a markdown code block."""
        response = """Here's my evaluation:
```json
[{"index": 0, "verdict": "amend", "reasoning": "Too vague", "amended_content": "Specific version"}]
```"""
        result = _parse_realist_response(response, 1)
        assert result[0]["verdict"] == "amend"
        assert result[0]["amended_content"] == "Specific version"

    def test_parse_bracket_extraction(self):
        """Falls back to bracket extraction."""
        response = "My analysis: [" + json.dumps(
            {"index": 0, "verdict": "pass", "reasoning": "OK"}
        ) + "]"
        result = _parse_realist_response(response, 1)
        assert result[0]["verdict"] == "pass"

    def test_parse_empty_response(self):
        """Returns empty dict on empty response (pass-through)."""
        assert _parse_realist_response("", 1) == {}
        assert _parse_realist_response("   ", 1) == {}

    def test_parse_invalid_json(self):
        """Returns empty dict on unparseable response (pass-through)."""
        assert _parse_realist_response("not json at all", 1) == {}

    def test_parse_invalid_verdict_defaults_pass(self):
        """Unknown verdict defaults to 'pass'."""
        response = json.dumps([
            {"index": 0, "verdict": "maybe", "reasoning": "Unsure"},
        ])
        result = _parse_realist_response(response, 1)
        assert result[0]["verdict"] == "pass"

    def test_parse_out_of_range_index(self):
        """Ignores verdicts with out-of-range indices."""
        response = json.dumps([
            {"index": 0, "verdict": "pass", "reasoning": "OK"},
            {"index": 5, "verdict": "reject", "reasoning": "Bad"},
        ])
        result = _parse_realist_response(response, 2)
        assert 0 in result
        assert 5 not in result

    def test_parse_truncates_long_reasoning(self):
        """Reasoning is capped at 500 chars."""
        response = json.dumps([
            {"index": 0, "verdict": "pass", "reasoning": "R" * 1000},
        ])
        result = _parse_realist_response(response, 1)
        assert len(result[0]["reasoning"]) == 500

    def test_default_verdict_for_missing_index(self):
        """Proposals without a verdict default to pass in the filter logic."""
        # Simulate: realist only returns verdict for index 0, but there are 2 proposals
        response = json.dumps([
            {"index": 0, "verdict": "reject", "reasoning": "Bad"},
        ])
        result = _parse_realist_response(response, 2)
        assert 0 in result
        assert 1 not in result  # Missing = pass (handled by filter logic)


# -- Neutral status labels tests --


class TestNeutralStatus:
    def test_all_statuses_have_labels(self):
        """Every known proposal status has a neutral label."""
        expected_statuses = {
            "pending", "approved", "rejected", "expired",
            "tabled", "withdrawn", "executed", "failed", "cancelled",
        }
        for status in expected_statuses:
            assert status in NEUTRAL_STATUS, f"Missing label for status: {status}"

    def test_neutral_labels_no_judgment(self):
        """Labels avoid judgment language."""
        assert NEUTRAL_STATUS["rejected"] == "passed on"
        assert NEUTRAL_STATUS["withdrawn"] == "recycled"
        assert NEUTRAL_STATUS["tabled"] == "deferred"
        assert NEUTRAL_STATUS["failed"] == "attempted"

    def test_positive_labels_unchanged(self):
        """Approved/executed/pending are unchanged."""
        assert NEUTRAL_STATUS["approved"] == "approved"
        assert NEUTRAL_STATUS["executed"] == "completed"
        assert NEUTRAL_STATUS["pending"] == "pending"


# -- Context visibility tests --


class TestProposalHistoryContext:
    @pytest.mark.asyncio
    async def test_history_shows_status_and_realist(self, db):
        """Proposal history includes neutral status and realist columns."""
        from genesis.ego.user_context import UserEgoContextBuilder

        await ego_crud.create_proposal(
            db,
            id="ctx1",
            action_type="dispatch",
            content="Test proposal",
            realist_verdict="pass",
            realist_reasoning="Looks good",
        )

        builder = UserEgoContextBuilder(db=db)
        section = await builder._proposal_history_section()
        assert "Outcome" in section  # column header
        assert "Realist" in section  # column header
        assert "pending" in section  # neutral status
        assert "pass" in section  # realist verdict
        assert "Looks good" in section  # realist reasoning

    @pytest.mark.asyncio
    async def test_history_without_realist_annotations(self, db):
        """Proposals without realist annotations show empty realist column."""
        from genesis.ego.user_context import UserEgoContextBuilder

        await ego_crud.create_proposal(
            db,
            id="ctx2",
            action_type="investigate",
            content="Pre-realist proposal",
        )

        builder = UserEgoContextBuilder(db=db)
        section = await builder._proposal_history_section()
        assert "Pre-realist proposal" in section
        # No realist annotation in the row
        assert "| investigate |" in section


class TestTabledProposalBoard:
    @pytest.mark.asyncio
    async def test_board_shows_tabled(self, db):
        """Board section includes deferred (tabled) proposals."""
        from genesis.ego.user_context import UserEgoContextBuilder

        await ego_crud.create_proposal(
            db,
            id="tbl1",
            action_type="maintenance",
            content="Tabled proposal",
        )
        await ego_crud.table_proposal(db, "tbl1")

        builder = UserEgoContextBuilder(db=db)
        section = await builder._proposal_board_section()
        assert "Deferred" in section
        assert "Tabled proposal" in section
        assert "tbl1" in section

    @pytest.mark.asyncio
    async def test_board_caps_tabled_at_8(self, db):
        """Board caps tabled proposals at 8."""
        from genesis.ego.user_context import UserEgoContextBuilder

        for i in range(10):
            await ego_crud.create_proposal(
                db,
                id=f"tbl_{i:02d}",
                action_type="maintenance",
                content=f"Tabled proposal {i}",
            )
            await ego_crud.table_proposal(db, f"tbl_{i:02d}")

        builder = UserEgoContextBuilder(db=db)
        section = await builder._proposal_board_section()
        assert "Deferred (10 tabled)" in section
        assert "and 2 more" in section


# -- Capability map aggregator fix tests --


class TestCapabilityMapFix:
    @pytest.mark.asyncio
    async def test_withdrawn_excluded_from_denominator(self, db):
        """Withdrawn/tabled proposals don't inflate failure rate."""
        from genesis.ego.capability_aggregator import compute_capability_map

        # Create proposals: 1 approved, 1 rejected, 3 withdrawn
        await ego_crud.create_proposal(
            db, id="cap1", action_type="maintenance", content="Approved"
        )
        await ego_crud.resolve_proposal(db, "cap1", status="approved")

        await ego_crud.create_proposal(
            db, id="cap2", action_type="maintenance", content="Rejected"
        )
        await ego_crud.resolve_proposal(db, "cap2", status="rejected")

        for i in range(3):
            await ego_crud.create_proposal(
                db, id=f"cap_w{i}", action_type="maintenance", content=f"Withdrawn {i}"
            )
            await ego_crud.withdraw_proposal(db, f"cap_w{i}")

        results = await compute_capability_map(db)
        maint = [r for r in results if r["domain"] == "maintenance"]

        # Without fix: 1/5 = 20%. With fix: 1/2 = 50%.
        if maint:
            assert maint[0]["confidence"] >= 0.4, (
                f"Expected >= 0.4 (withdrawn excluded), got {maint[0]['confidence']}"
            )


# -- Realist filter integration tests --


class TestRealistFilterIntegration:
    @pytest.mark.asyncio
    async def test_filter_passthrough_on_empty(self):
        """Empty proposals list passes through."""
        from genesis.ego.session import EgoSession

        session = object.__new__(EgoSession)
        result = await session._filter_proposals([])
        assert result == []

    @pytest.mark.asyncio
    async def test_annotations_stored_on_proposals(self, db):
        """Realist annotations persist through create_batch to DB."""
        from genesis.ego.proposals import ProposalWorkflow

        proposals = [
            {
                "action_type": "dispatch",
                "content": "Test proposal",
                "confidence": 0.8,
                "_realist_verdict": "pass",
                "_realist_reasoning": "Looks actionable",
            },
        ]

        workflow = ProposalWorkflow(db=db)
        batch_id, ids = await workflow.create_batch(proposals)

        prop = await ego_crud.get_proposal(db, ids[0])
        assert prop["realist_verdict"] == "pass"
        assert prop["realist_reasoning"] == "Looks actionable"

    @pytest.mark.asyncio
    async def test_annotations_absent_by_default(self, db):
        """Proposals without realist annotations have NULL columns."""
        from genesis.ego.proposals import ProposalWorkflow

        proposals = [
            {
                "action_type": "dispatch",
                "content": "No realist",
                "confidence": 0.5,
            },
        ]

        workflow = ProposalWorkflow(db=db)
        batch_id, ids = await workflow.create_batch(proposals)

        prop = await ego_crud.get_proposal(db, ids[0])
        assert prop["realist_verdict"] is None
        assert prop["realist_reasoning"] is None


# -- Cross-ego isolation tests --


class TestEgoSourceIsolation:
    @pytest.mark.asyncio
    async def test_ego_source_stored_on_proposal(self, db):
        """ego_source is persisted when creating proposals via batch."""
        from genesis.ego.proposals import ProposalWorkflow

        proposals = [{"action_type": "dispatch", "content": "Test", "confidence": 0.8}]
        workflow = ProposalWorkflow(db=db)
        _, ids = await workflow.create_batch(
            proposals, ego_source="user_ego_cycle",
        )
        prop = await ego_crud.get_proposal(db, ids[0])
        assert prop["ego_source"] == "user_ego_cycle"

    @pytest.mark.asyncio
    async def test_ego_source_null_by_default(self, db):
        """Proposals created without ego_source have NULL (backwards compat)."""
        from genesis.ego.proposals import ProposalWorkflow

        proposals = [{"action_type": "dispatch", "content": "Old", "confidence": 0.5}]
        workflow = ProposalWorkflow(db=db)
        _, ids = await workflow.create_batch(proposals)
        prop = await ego_crud.get_proposal(db, ids[0])
        assert prop["ego_source"] is None

    @pytest.mark.asyncio
    async def test_list_pending_filtered_by_ego(self, db):
        """list_pending_proposals with ego_source filters correctly."""
        await ego_crud.create_proposal(
            db, id="user1", action_type="dispatch", content="User proposal",
            ego_source="user_ego_cycle",
        )
        await ego_crud.create_proposal(
            db, id="gen1", action_type="maintenance", content="Genesis proposal",
            ego_source="genesis_ego_cycle",
        )

        user_pending = await ego_crud.list_pending_proposals(
            db, ego_source="user_ego_cycle",
        )
        assert len(user_pending) == 1
        assert user_pending[0]["id"] == "user1"

        gen_pending = await ego_crud.list_pending_proposals(
            db, ego_source="genesis_ego_cycle",
        )
        assert len(gen_pending) == 1
        assert gen_pending[0]["id"] == "gen1"

        # No filter returns both
        all_pending = await ego_crud.list_pending_proposals(db)
        assert len(all_pending) == 2

    @pytest.mark.asyncio
    async def test_null_ego_source_matches_any_filter(self, db):
        """Pre-migration proposals (NULL ego_source) match any filter."""
        await ego_crud.create_proposal(
            db, id="old1", action_type="dispatch", content="Old proposal",
            # No ego_source — simulates pre-migration row
        )

        user_pending = await ego_crud.list_pending_proposals(
            db, ego_source="user_ego_cycle",
        )
        assert len(user_pending) == 1  # NULL matches

    @pytest.mark.asyncio
    async def test_digest_pending_count_filtered(self, db):
        """send_digest pending count only shows same-ego proposals."""
        from unittest.mock import AsyncMock

        from genesis.ego.proposals import ProposalWorkflow

        # Create user ego proposal (the batch being sent)
        await ego_crud.create_proposal(
            db, id="u_new", action_type="dispatch", content="New user",
            batch_id="user_batch", ego_source="user_ego_cycle",
        )
        # Create another user ego pending from old batch
        await ego_crud.create_proposal(
            db, id="u_old", action_type="dispatch", content="Old user",
            batch_id="old_user_batch", ego_source="user_ego_cycle",
        )
        # Create genesis ego pending (should NOT appear in count)
        await ego_crud.create_proposal(
            db, id="g1", action_type="maintenance", content="Genesis",
            batch_id="gen_batch", ego_source="genesis_ego_cycle",
        )

        mock_tm = AsyncMock()
        mock_tm.send_to_category = AsyncMock(return_value="delivery123")
        workflow = ProposalWorkflow(db=db, topic_manager=mock_tm)

        await workflow.send_digest(
            "user_batch", ego_source="user_ego_cycle",
        )

        sent_html = mock_tm.send_to_category.call_args[0][1]
        # Should show 1 pending from old user batch, not 2 (which would include genesis)
        assert "1 proposal(s) pending" in sent_html
