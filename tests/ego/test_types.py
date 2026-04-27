"""Tests for genesis.ego.types."""

from genesis.ego.types import (
    EGO_OUTPUT_SCHEMA,
    EgoConfig,
    EgoCycle,
    EgoProposal,
    ProposalStatus,
    ProposalUrgency,
)


class TestEgoProposal:
    def test_defaults(self):
        p = EgoProposal()
        assert p.status == ProposalStatus.PENDING
        assert p.urgency == ProposalUrgency.NORMAL
        assert p.confidence == 0.0
        assert p.id  # auto-generated
        assert len(p.id) == 16

    def test_custom_fields(self):
        p = EgoProposal(
            action_type="investigate",
            action_category="system_health",
            content="Check observation backlog",
            rationale="Backlog at 229, growing",
            confidence=0.75,
            urgency=ProposalUrgency.HIGH,
        )
        assert p.action_type == "investigate"
        assert p.action_category == "system_health"
        assert p.confidence == 0.75
        assert p.urgency == "high"

    def test_unique_ids(self):
        p1 = EgoProposal()
        p2 = EgoProposal()
        assert p1.id != p2.id


class TestEgoCycle:
    def test_defaults(self):
        c = EgoCycle()
        assert c.output_text == ""
        assert c.proposals_json == "[]"
        assert c.cost_usd == 0.0
        assert c.compacted_into is None
        assert c.id

    def test_custom(self):
        c = EgoCycle(
            output_text="investigated backlog",
            focus_summary="observation backlog, CC bridge",
            model_used="opus",
            cost_usd=0.25,
        )
        assert c.model_used == "opus"
        assert c.cost_usd == 0.25
        assert c.focus_summary == "observation backlog, CC bridge"


class TestEgoConfig:
    def test_defaults(self):
        cfg = EgoConfig()
        assert cfg.enabled is True
        assert cfg.cadence_minutes == 60
        assert cfg.activity_threshold_minutes == 30
        assert cfg.max_interval_minutes == 240
        assert cfg.model == "opus"
        assert cfg.board_size == 3
        assert cfg.ego_thinking_budget_usd == 4.0
        assert cfg.ego_dispatch_budget_usd == 2.50
        assert cfg.consecutive_failure_limit == 3
        assert cfg.batch_digest is True
        assert cfg.shadow_morning_report is True

    def test_custom(self):
        cfg = EgoConfig(cadence_minutes=120, model="sonnet")
        assert cfg.cadence_minutes == 120
        assert cfg.model == "sonnet"

    def test_board_size_default(self):
        cfg = EgoConfig()
        assert cfg.board_size == 3

    def test_no_proposal_expiry(self):
        """proposal_expiry_minutes was removed in Phase B."""
        assert not hasattr(EgoConfig(), "proposal_expiry_minutes")


class TestOutputSchema:
    def test_schema_has_required_fields(self):
        assert "proposals" in EGO_OUTPUT_SCHEMA["properties"]
        assert "focus_summary" in EGO_OUTPUT_SCHEMA["properties"]
        assert "follow_ups" in EGO_OUTPUT_SCHEMA["properties"]
        assert set(EGO_OUTPUT_SCHEMA["required"]) == {
            "proposals", "focus_summary", "follow_ups",
        }

    def test_proposal_item_schema(self):
        item = EGO_OUTPUT_SCHEMA["properties"]["proposals"]["items"]
        assert "action_type" in item["properties"]
        assert "confidence" in item["properties"]
        assert "action_category" in item["properties"]

    def test_proposal_item_has_board_fields(self):
        item = EGO_OUTPUT_SCHEMA["properties"]["proposals"]["items"]
        assert "execution_plan" in item["properties"]
        assert "rank" in item["properties"]
        assert "recurring" in item["properties"]

    def test_schema_has_tabled_withdrawn(self):
        assert "tabled" in EGO_OUTPUT_SCHEMA["properties"]
        assert "withdrawn" in EGO_OUTPUT_SCHEMA["properties"]


class TestProposalStatus:
    def test_all_states(self):
        assert ProposalStatus.PENDING == "pending"
        assert ProposalStatus.APPROVED == "approved"
        assert ProposalStatus.REJECTED == "rejected"
        assert ProposalStatus.EXPIRED == "expired"
        assert ProposalStatus.EXECUTED == "executed"
        assert ProposalStatus.FAILED == "failed"
        assert ProposalStatus.TABLED == "tabled"
        assert ProposalStatus.WITHDRAWN == "withdrawn"
