"""Tests for dispatch information boundary and content firewall.

Covers:
- _is_content_dispatch() — content dispatch detection
- _build_dispatch_prompt() — context-selective world model injection
- Firewall rules appended to both dispatch paths
- Content-publish skill firewall step
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from genesis.ego.session import (
    _CONTENT_DISPATCH_KEYWORDS,
    _CONTENT_FIREWALL_RULES,
    _is_content_dispatch,
)

# ---------------------------------------------------------------------------
# _is_content_dispatch tests
# ---------------------------------------------------------------------------


class TestIsContentDispatch:
    """Tests for content dispatch detection heuristic."""

    def test_outreach_action_type(self):
        """action_type 'outreach' should be content dispatch."""
        assert _is_content_dispatch({"action_type": "outreach"}) is True

    def test_dispatch_action_type(self):
        """action_type 'dispatch' should be content dispatch."""
        assert _is_content_dispatch({"action_type": "dispatch"}) is True

    def test_content_action_type(self):
        """action_type 'content' should be content dispatch."""
        assert _is_content_dispatch({"action_type": "content"}) is True

    def test_publish_action_type(self):
        """action_type 'publish' should be content dispatch."""
        assert _is_content_dispatch({"action_type": "publish"}) is True

    def test_investigate_action_type_not_content(self):
        """action_type 'investigate' should NOT be content dispatch."""
        assert _is_content_dispatch({"action_type": "investigate"}) is False

    def test_maintenance_action_type_not_content(self):
        """action_type 'maintenance' should NOT be content dispatch."""
        assert _is_content_dispatch({"action_type": "maintenance"}) is False

    def test_content_keyword_in_body(self):
        """Keywords in content field should trigger detection."""
        assert _is_content_dispatch({
            "action_type": "investigate",
            "content": "Publish a Medium article about earned autonomy",
        }) is True

    def test_keyword_medium_in_body(self):
        """'medium' keyword should trigger."""
        assert _is_content_dispatch({
            "content": "Draft a Medium post",
        }) is True

    def test_no_keywords_not_content(self):
        """Proposals without content keywords are not content dispatch."""
        assert _is_content_dispatch({
            "action_type": "investigate",
            "content": "Check why the Qdrant service is down",
        }) is False

    def test_empty_proposal(self):
        """Empty dict should not be content dispatch."""
        assert _is_content_dispatch({}) is False

    def test_all_keywords_present(self):
        """Every keyword in _CONTENT_DISPATCH_KEYWORDS should trigger."""
        for kw in _CONTENT_DISPATCH_KEYWORDS:
            prop = {"content": f"Please {kw} this thing"}
            assert _is_content_dispatch(prop) is True, f"Keyword '{kw}' failed"


# ---------------------------------------------------------------------------
# _build_dispatch_prompt context-selectivity tests
# ---------------------------------------------------------------------------


class TestBuildDispatchPromptContextSelectivity:
    """Tests that content dispatches omit world model context."""

    @pytest.fixture()
    def session(self):
        """Minimal EgoSession mock with DB."""
        from genesis.ego.session import EgoSession

        s = object.__new__(EgoSession)
        s._db = AsyncMock()
        return s

    @pytest.mark.asyncio
    async def test_content_dispatch_omits_goals(self, session):
        """Content dispatch should NOT include user goals."""
        prop = {
            "action_type": "outreach",
            "content": "Publish article about autonomy",
            "execution_plan": "Medium publish via browser",
            "rationale": "Content strategy",
        }
        prompt = await session._build_dispatch_prompt(prop)
        assert "User's active goals" not in prompt

    @pytest.mark.asyncio
    async def test_content_dispatch_omits_contacts(self, session):
        """Content dispatch should NOT include contacts."""
        prop = {
            "action_type": "outreach",
            "content": "Publish article about autonomy",
        }
        prompt = await session._build_dispatch_prompt(prop)
        assert "Relevant contacts" not in prompt

    @pytest.mark.asyncio
    async def test_content_dispatch_omits_events(self, session):
        """Content dispatch should NOT include events."""
        prop = {
            "action_type": "outreach",
            "content": "Publish article about autonomy",
        }
        prompt = await session._build_dispatch_prompt(prop)
        assert "Upcoming events" not in prompt

    @pytest.mark.asyncio
    async def test_non_content_dispatch_includes_goals(self, session):
        """Non-content dispatch should attempt to include goals."""
        from genesis.db.crud import user_goals

        # Mock the goals lookup to return results
        mock_goals = [
            {"title": "Test Goal", "category": "career", "priority": "high"},
        ]
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(user_goals, "list_active", AsyncMock(return_value=mock_goals))
            prop = {
                "action_type": "investigate",
                "content": "Check why Qdrant is unreachable",
            }
            prompt = await session._build_dispatch_prompt(prop)
            assert "User's active goals" in prompt

    @pytest.mark.asyncio
    async def test_all_dispatches_include_firewall(self, session):
        """ALL dispatch prompts should include firewall rules."""
        # Content dispatch
        prop_content = {
            "action_type": "outreach",
            "content": "Publish article",
        }
        prompt_content = await session._build_dispatch_prompt(prop_content)
        assert "Content Firewall" in prompt_content

        # Non-content dispatch
        prop_investigate = {
            "action_type": "investigate",
            "content": "Check health",
        }
        prompt_investigate = await session._build_dispatch_prompt(prop_investigate)
        assert "Content Firewall" in prompt_investigate


# ---------------------------------------------------------------------------
# Firewall rules constant tests
# ---------------------------------------------------------------------------


class TestFirewallRulesConstant:
    """Tests for _CONTENT_FIREWALL_RULES content."""

    def test_contains_generic_rules(self):
        """Firewall should contain generic content hygiene rules."""
        assert "generic descriptions" in _CONTENT_FIREWALL_RULES.lower()
        assert "personal names" in _CONTENT_FIREWALL_RULES.lower()

    def test_contains_identity_protection(self):
        """Firewall should contain career/job search protection."""
        assert "job search" in _CONTENT_FIREWALL_RULES.lower()
        assert "career" in _CONTENT_FIREWALL_RULES.lower()

    def test_contains_least_privilege_principle(self):
        """Firewall should state the least-privilege principle."""
        assert "no more information than the task requires" in _CONTENT_FIREWALL_RULES.lower()


# ---------------------------------------------------------------------------
# Content-publish skill firewall step test
# ---------------------------------------------------------------------------


class TestContentPublishSkillFirewall:
    """Verify the content-publish skill has the firewall step."""

    def test_skill_has_firewall_step(self):
        """Content-publish SKILL.md should contain the firewall review step."""
        from pathlib import Path

        skill_path = Path.home() / ".claude" / "skills" / "content-publish" / "SKILL.md"
        if not skill_path.exists():
            pytest.skip("Content-publish skill not installed")
        content = skill_path.read_text()
        assert "Content Firewall Review" in content
        assert "Step 5.5" in content
