"""Tests for genesis.autonomy.executor.resources."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.autonomy.executor.resources import (
    _extract_keywords,
    _load_skill_catalog,
    gather_resource_inventory,
    load_step_resources,
)

# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------


class TestExtractKeywords:
    def test_extracts_words(self) -> None:
        kw = _extract_keywords("Build a REST API endpoint for user auth")
        assert "build" in kw
        assert "rest" in kw
        assert "endpoint" in kw
        # Stop words excluded
        assert "a" not in kw
        assert "for" not in kw

    def test_respects_max(self) -> None:
        kw = _extract_keywords("a b c d e f g h i j k l m n o " * 3, max_keywords=5)
        assert len(kw) <= 5

    def test_empty_input(self) -> None:
        kw = _extract_keywords("")
        assert kw == []

    def test_deduplicates(self) -> None:
        kw = _extract_keywords("build build build endpoint endpoint")
        assert kw.count("build") == 1
        assert kw.count("endpoint") == 1


# ---------------------------------------------------------------------------
# Skill catalog loading
# ---------------------------------------------------------------------------


class TestLoadSkillCatalog:
    def test_missing_catalog_returns_none(self) -> None:
        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            Path("/nonexistent/path.json"),
        ):
            assert _load_skill_catalog() is None

    def test_valid_catalog(self, tmp_path: Path) -> None:
        catalog = {
            "tier1": [{"name": "dev", "description": "Development", "tier": 1}],
            "tier2": [{"name": "research", "description": "Deep research", "tier": 2}],
        }
        catalog_file = tmp_path / "skill_catalog.json"
        catalog_file.write_text(json.dumps(catalog))

        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            catalog_file,
        ):
            result = _load_skill_catalog()
            assert result is not None
            assert "dev" in result
            assert "research" in result
            assert "[T1]" in result
            assert "[T2]" in result

    def test_empty_catalog(self, tmp_path: Path) -> None:
        catalog_file = tmp_path / "skill_catalog.json"
        catalog_file.write_text(json.dumps({"tier1": [], "tier2": []}))

        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            catalog_file,
        ):
            assert _load_skill_catalog() is None


# ---------------------------------------------------------------------------
# Full inventory gathering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGatherResourceInventory:
    async def test_all_sources_missing(self) -> None:
        """With no DB, no retriever, no catalog — still returns MCP section."""
        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            Path("/nonexistent"),
        ):
            result = await gather_resource_inventory(None, None, None, "test task")
        # Should at least have MCP tool categories
        assert "MCP Tool Categories" in result

    async def test_with_procedures(self) -> None:
        """Procedures are included when DB is available."""
        @dataclass
        class FakeMatch:
            procedure_id: str = "p-1"
            task_type: str = "api-endpoint"
            confidence: float = 0.8
            success_count: int = 5
            failure_count: int = 1
            failure_modes: list = field(default_factory=list)
            workarounds: list = field(default_factory=list)
            steps: list = field(default_factory=lambda: ["step 1", "step 2"])
            principle: str = "Build REST endpoints"
            activation_tier: str = "L3"
            tool_trigger: list | None = None

        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            Path("/nonexistent"),
        ), patch(
            "genesis.learning.procedural.matcher.find_relevant",
            AsyncMock(return_value=[FakeMatch()]),
        ):
            result = await gather_resource_inventory(
                AsyncMock(), None, None, "build an API endpoint",
            )
        assert "api-endpoint" in result
        assert "80%" in result
        assert "Relevant Procedures" in result

    async def test_with_past_executions(self) -> None:
        """Past executions are included when retriever is available."""
        @dataclass
        class FakeResult:
            content: str = "Task execution: built API, succeeded"
            payload: dict = field(default_factory=lambda: {"source": "task_executor"})

        retriever = AsyncMock()
        retriever.recall = AsyncMock(return_value=[FakeResult()])

        with patch(
            "genesis.autonomy.executor.resources._CATALOG_PATH",
            Path("/nonexistent"),
        ):
            result = await gather_resource_inventory(
                None, None, retriever, "build an API",
            )
        assert "Past Task Executions" in result
        assert "built API" in result


# ---------------------------------------------------------------------------
# Step resource loading
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLoadStepResources:
    async def test_no_resources_assigned(self) -> None:
        step = {"idx": 0, "type": "code", "description": "write code"}
        result = await load_step_resources(None, step)
        assert result is None

    async def test_empty_lists(self) -> None:
        step = {"idx": 0, "skills": [], "procedures": []}
        result = await load_step_resources(None, step)
        assert result is None

    async def test_skill_loaded(self, tmp_path: Path) -> None:
        """Skills are loaded from SKILL.md when assigned."""
        skill_dir = tmp_path / "research"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Research Skill\nDo research.")

        catalog = {
            "tier1": [],
            "tier2": [{"name": "research", "description": "Research", "tier": 2, "path": ""}],
        }

        with patch(
            "genesis.autonomy.executor.resources._find_skill_path",
            return_value=skill_dir,
        ), patch(
            "genesis.autonomy.executor.resources._skill_catalog_cache",
            catalog,
        ):
            step = {"idx": 0, "skills": ["research"]}
            result = await load_step_resources(None, step)
            assert result is not None
            assert "Research Skill" in result
            assert "Do research" in result

    async def test_missing_skill_skipped(self) -> None:
        """Missing skills are skipped gracefully."""
        with patch(
            "genesis.autonomy.executor.resources._find_skill_path",
            return_value=None,
        ):
            step = {"idx": 0, "skills": ["nonexistent-skill"]}
            result = await load_step_resources(None, step)
            assert result is None
