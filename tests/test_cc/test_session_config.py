"""Tests for SessionConfigBuilder."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from genesis.cc.session_config import (
    _READONLY_DISALLOWED,
    SessionConfigBuilder,
)


@pytest.fixture
def builder():
    return SessionConfigBuilder()


class TestBuildReflectionConfig:
    def test_deep_defaults(self, builder):
        cfg = builder.build_reflection_config()
        assert cfg["model"] == "opus"
        assert cfg["effort"] == "high"
        assert cfg["disallowed_tools"] == _READONLY_DISALLOWED
        assert cfg["skip_permissions"] is True
        assert "system_prompt" in cfg

    def test_strategic_uses_opus(self, builder):
        cfg = builder.build_reflection_config("strategic")
        assert cfg["model"] == "opus"

    def test_deep_uses_opus(self, builder):
        cfg = builder.build_reflection_config("deep")
        assert cfg["model"] == "opus"

    def test_strategic_uses_max_effort(self, builder):
        cfg = builder.build_reflection_config("strategic")
        assert cfg["effort"] == "max"


class TestBuildTaskConfig:
    def test_basic_config(self, builder):
        cfg = builder.build_task_config("do something")
        assert cfg["model"] == "sonnet"
        assert cfg["effort"] == "medium"
        # No disallowed_tools — destructive git ops guarded by PreToolUse hooks
        assert "disallowed_tools" not in cfg
        assert cfg["skip_permissions"] is True

    def test_with_skills_loaded(self, builder):
        with patch(
            "genesis.learning.skills.wiring.load_skill",
            side_effect=lambda name: f"content for {name}",
        ):
            cfg = builder.build_task_config("task", skill_names=["sk1", "sk2"])
        assert "## Skill: sk1" in cfg["system_prompt"]
        assert "## Skill: sk2" in cfg["system_prompt"]
        assert "content for sk1" in cfg["system_prompt"]

    def test_with_missing_skill(self, builder):
        with patch(
            "genesis.learning.skills.wiring.load_skill",
            return_value=None,
        ):
            cfg = builder.build_task_config("task", skill_names=["missing"])
        assert "## Skill:" not in cfg["system_prompt"]

    def test_no_skills(self, builder):
        cfg = builder.build_task_config("task", skill_names=None)
        assert "disallowed_tools" not in cfg


class TestBuildSurplusConfig:
    def test_surplus_config(self, builder):
        cfg = builder.build_surplus_config()
        assert cfg["model"] == "sonnet"
        assert cfg["effort"] == "medium"
        assert cfg["disallowed_tools"] == _READONLY_DISALLOWED
        assert cfg["skip_permissions"] is True


class TestLoadIdentityBlock:
    def test_loads_soul_md(self, builder):
        result = builder._load_identity_block()
        # SOUL.md exists in the repo, so should load real content
        assert len(result) > 0
        assert result != "You are Genesis, an autonomous AI agent."

    def test_fallback_when_missing(self, builder):
        with patch("pathlib.Path.exists", return_value=False):
            result = builder._load_identity_block()
        assert result == "You are Genesis, an autonomous AI agent."


class TestGroundworkStubs:
    def test_mcp_config_returns_none(self, builder):
        assert builder.build_mcp_config() is None

    def test_hook_config_returns_none(self, builder):
        assert builder.build_hook_config() is None
