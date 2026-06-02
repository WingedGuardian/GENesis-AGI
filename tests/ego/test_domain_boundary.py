"""Tests for ego domain boundary enforcement.

Covers:
- _normalize_to_infra() — prefix matching for infrastructure categories
- _enforce_user_domain_boundary() — filtering and redirect observation creation
- _enforce_domain_boundary() — genesis ego user-domain proposal dropping
- _build_realist_prompt() — domain-specific context and rules for both egos
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.ego.session import (
    _USER_EGO_INFRA_PREFIXES,
    _build_realist_prompt,
    _normalize_to_infra,
)

# ---------------------------------------------------------------------------
# _normalize_to_infra tests
# ---------------------------------------------------------------------------


class TestNormalizeToInfra:
    """Tests for prefix-based infrastructure category detection."""

    def test_exact_matches(self):
        """Exact category names in the prefix set should match."""
        for prefix in _USER_EGO_INFRA_PREFIXES:
            assert _normalize_to_infra(prefix) is True, f"Failed for: {prefix}"

    def test_prefix_variants(self):
        """LLM-generated variants with suffixes should match."""
        assert _normalize_to_infra("infrastructure_maintenance") is True
        assert _normalize_to_infra("infrastructure_bug") is True
        assert _normalize_to_infra("infrastructure_health") is True
        assert _normalize_to_infra("system_health_check") is True
        assert _normalize_to_infra("performance_tuning") is True
        assert _normalize_to_infra("maintenance_window") is True
        assert _normalize_to_infra("security_audit") is True
        assert _normalize_to_infra("cost_protection_budget") is True
        assert _normalize_to_infra("system_monitoring_alert") is True
        assert _normalize_to_infra("genesis_maintenance_task") is True

    def test_case_insensitive(self):
        """Matching should be case-insensitive."""
        assert _normalize_to_infra("Infrastructure") is True
        assert _normalize_to_infra("SYSTEM_HEALTH") is True
        assert _normalize_to_infra("Performance_Tuning") is True

    def test_non_infra_categories(self):
        """User-domain categories should NOT match."""
        assert _normalize_to_infra("career") is False
        assert _normalize_to_infra("content") is False
        assert _normalize_to_infra("networking") is False
        assert _normalize_to_infra("marketing") is False
        assert _normalize_to_infra("outreach") is False
        assert _normalize_to_infra("goal_management") is False
        assert _normalize_to_infra("portfolio") is False

    def test_empty_and_none(self):
        """Empty or falsy category should return False."""
        assert _normalize_to_infra("") is False
        # We don't pass None due to type hint, but test defensive behavior
        assert _normalize_to_infra("") is False

    def test_partial_non_match(self):
        """Strings that contain infra words but don't start with them."""
        assert _normalize_to_infra("career_infrastructure") is False
        assert _normalize_to_infra("my_performance") is False
        assert _normalize_to_infra("user_security") is False


# ---------------------------------------------------------------------------
# _enforce_user_domain_boundary tests
# ---------------------------------------------------------------------------


class TestEnforceUserDomainBoundary:
    """Tests for user ego infrastructure proposal redirection."""

    @pytest.fixture
    def mock_session(self):
        """Create a minimal mock EgoSession for testing."""
        session = MagicMock()
        session._source_tag = "user_ego_cycle"
        session._db = AsyncMock()
        # Bind the real method to our mock
        from genesis.ego.session import EgoSession

        session._enforce_user_domain_boundary = (
            EgoSession._enforce_user_domain_boundary.__get__(session)
        )
        session._create_redirect_observation = AsyncMock()
        return session

    @pytest.mark.asyncio
    async def test_infra_proposals_filtered(self, mock_session):
        """Infrastructure proposals are removed from the list."""
        proposals = [
            {"action_category": "infrastructure", "content": "Fix server", "urgency": "high"},
            {"action_category": "career", "content": "Apply to job", "urgency": "normal"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 1
        assert result[0]["action_category"] == "career"

    @pytest.mark.asyncio
    async def test_redirect_observation_created(self, mock_session):
        """Filtered proposals create redirect observations."""
        proposals = [
            {"action_category": "system_health_check", "content": "Check DB", "urgency": "high"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 0
        mock_session._create_redirect_observation.assert_called_once()
        call_kwargs = mock_session._create_redirect_observation.call_args
        assert call_kwargs[1]["redirect_type"] == "cross_domain_redirect"

    @pytest.mark.asyncio
    async def test_non_infra_proposals_pass_through(self, mock_session):
        """User-domain proposals pass through untouched."""
        proposals = [
            {"action_category": "career", "content": "Apply to job", "urgency": "normal"},
            {"action_category": "content", "content": "Write article", "urgency": "low"},
            {"action_category": "networking", "content": "Reach out", "urgency": "normal"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 3
        mock_session._create_redirect_observation.assert_not_called()

    @pytest.mark.asyncio
    async def test_prefix_variants_filtered(self, mock_session):
        """LLM-generated category variants are caught."""
        proposals = [
            {"action_category": "infrastructure_maintenance", "content": "Fix bug", "urgency": "normal"},
            {"action_category": "performance_tuning", "content": "Optimize", "urgency": "normal"},
            {"action_category": "cost_protection_alert", "content": "Budget", "urgency": "high"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 0
        assert mock_session._create_redirect_observation.call_count == 3

    @pytest.mark.asyncio
    async def test_empty_category_passes(self, mock_session):
        """Proposals with empty category pass through."""
        proposals = [
            {"action_category": "", "content": "Something", "urgency": "normal"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_mixed_proposals(self, mock_session):
        """Mixed list of infra and user-domain proposals."""
        proposals = [
            {"action_category": "career", "content": "Job search", "urgency": "normal"},
            {"action_category": "infrastructure", "content": "Fix server", "urgency": "high"},
            {"action_category": "content_publishing", "content": "Blog post", "urgency": "low"},
            {"action_category": "system_monitoring_alert", "content": "Alert", "urgency": "critical"},
        ]
        result = await mock_session._enforce_user_domain_boundary(proposals)
        assert len(result) == 2
        assert result[0]["action_category"] == "career"
        assert result[1]["action_category"] == "content_publishing"
        assert mock_session._create_redirect_observation.call_count == 2


# ---------------------------------------------------------------------------
# _enforce_domain_boundary tests (existing genesis ego boundary)
# ---------------------------------------------------------------------------


class TestEnforceDomainBoundary:
    """Tests for genesis ego user-domain proposal dropping."""

    @pytest.fixture
    def mock_session(self):
        """Create a minimal mock EgoSession for testing."""
        session = MagicMock()
        session._source_tag = "genesis_ego_cycle"
        from genesis.ego.session import EgoSession

        session._GENESIS_EGO_ALLOWED_CATEGORIES = (
            EgoSession._GENESIS_EGO_ALLOWED_CATEGORIES
        )
        session._enforce_domain_boundary = (
            EgoSession._enforce_domain_boundary.__get__(session)
        )
        return session

    def test_allowed_categories_pass(self, mock_session):
        """Infrastructure categories pass through."""
        proposals = [
            {"action_category": "system_health", "content": "Check health"},
            {"action_category": "infrastructure", "content": "Fix infra"},
            {"action_category": "performance", "content": "Optimize"},
            {"action_category": "maintenance", "content": "Maintain"},
            {"action_category": "security", "content": "Audit"},
        ]
        result = mock_session._enforce_domain_boundary(proposals)
        assert len(result) == 5

    def test_user_domain_proposals_dropped(self, mock_session):
        """User-domain categories are dropped."""
        proposals = [
            {"action_category": "career", "content": "Apply to job"},
            {"action_category": "content", "content": "Write blog"},
            {"action_category": "marketing", "content": "Campaign"},
            {"action_category": "networking", "content": "Reach out"},
        ]
        result = mock_session._enforce_domain_boundary(proposals)
        assert len(result) == 0

    def test_mixed_proposals(self, mock_session):
        """Only allowed categories survive."""
        proposals = [
            {"action_category": "infrastructure", "content": "Fix bug"},
            {"action_category": "career", "content": "Job app"},
            {"action_category": "security", "content": "Audit"},
        ]
        result = mock_session._enforce_domain_boundary(proposals)
        assert len(result) == 2
        assert result[0]["action_category"] == "infrastructure"
        assert result[1]["action_category"] == "security"


# ---------------------------------------------------------------------------
# _build_realist_prompt domain context tests
# ---------------------------------------------------------------------------


class TestRealistPromptDomainContext:
    """Tests for domain-specific context in the realist prompt."""

    def test_user_ego_gets_ego_section(self):
        """User ego source adds CEO jurisdiction section."""
        prompt = _build_realist_prompt(
            [{"content": "test", "action_type": "dispatch", "confidence": 0.8}],
            [],
            ego_source="user_ego_cycle",
        )
        assert "User ego (CEO)" in prompt
        assert "user value ONLY" in prompt
        assert "NO jurisdiction over" in prompt
        assert "Genesis infrastructure" in prompt

    def test_user_ego_gets_domain_rule(self):
        """User ego source adds Rule #7 for domain boundary."""
        prompt = _build_realist_prompt(
            [{"content": "test", "action_type": "dispatch", "confidence": 0.8}],
            [],
            ego_source="user_ego_cycle",
        )
        assert "Domain boundary (user ego only)" in prompt
        assert "REJECT any proposal about Genesis infrastructure" in prompt
        assert "Genesis ego (COO)" in prompt

    def test_user_ego_domain_rule_mentions_blocked_topics(self):
        """User ego Rule #7 lists the infrastructure topics to reject."""
        prompt = _build_realist_prompt(
            [{"content": "test", "action_type": "dispatch", "confidence": 0.8}],
            [],
            ego_source="user_ego_cycle",
        )
        for topic in ("system health", "cost optimization", "performance tuning",
                      "internal maintenance"):
            assert topic.lower() in prompt.lower(), f"Missing topic: {topic}"

    def test_genesis_ego_does_not_get_user_ego_section(self):
        """Genesis ego gets its own section, not user ego's."""
        prompt = _build_realist_prompt(
            [{"content": "test", "action_type": "dispatch", "confidence": 0.8}],
            [],
            ego_source="genesis_ego_cycle",
        )
        assert "User ego (CEO)" not in prompt
        assert "Genesis ego (COO/operations)" in prompt

    def test_empty_ego_source_no_domain_context(self):
        """Empty/missing ego source adds no ego section or domain rule."""
        prompt = _build_realist_prompt(
            [{"content": "test", "action_type": "dispatch", "confidence": 0.8}],
            [],
        )
        assert "User ego (CEO)" not in prompt
        assert "Genesis ego (COO/operations)" not in prompt
        # No rule 7
        assert "Domain boundary" not in prompt


# ---------------------------------------------------------------------------
# _USER_WORLD_CATEGORIES expansion tests
# ---------------------------------------------------------------------------


class TestUserWorldCategories:
    """Tests for expanded _USER_WORLD_CATEGORIES."""

    def test_original_categories_present(self):
        """Original categories are still present."""
        from genesis.ego.user_context import _USER_WORLD_CATEGORIES

        original = {"email_recon", "inbox", "finding", "interest", "interests",
                    "contribution", "user_model_delta"}
        assert original.issubset(_USER_WORLD_CATEGORIES)

    def test_expanded_categories_present(self):
        """New user-domain categories are present."""
        from genesis.ego.user_context import _USER_WORLD_CATEGORIES

        expanded = {
            "career", "career_advancement", "career_application",
            "content", "content_publishing", "content_distribution",
            "goal_management", "goal_review", "portfolio",
            "marketing", "outreach", "networking",
        }
        assert expanded.issubset(_USER_WORLD_CATEGORIES)

    def test_infra_categories_not_in_user_world(self):
        """Infrastructure categories should NOT be in user world."""
        from genesis.ego.user_context import _USER_WORLD_CATEGORIES

        infra = {"system_health", "infrastructure", "performance",
                 "maintenance", "security"}
        assert infra.isdisjoint(_USER_WORLD_CATEGORIES)
