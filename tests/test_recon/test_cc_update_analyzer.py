"""Tests for CC update analyzer."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from genesis.recon.cc_update_analyzer import (
    IMPACT_ACTION_NEEDED,
    IMPACT_BREAKING,
    IMPACT_INFORMATIONAL,
    CCUpdateAnalyzer,
)


@pytest.fixture()
async def db():
    """In-memory SQLite with observations and knowledge tables."""
    async with aiosqlite.connect(":memory:") as conn:
        await conn.execute(
            "CREATE TABLE observations ("
            "  id TEXT PRIMARY KEY,"
            "  source TEXT, type TEXT, category TEXT,"
            "  content TEXT, priority TEXT, created_at TEXT,"
            "  resolved_at TEXT, resolution_notes TEXT,"
            "  content_hash TEXT"
            ")"
        )
        await conn.execute(
            "CREATE TABLE knowledge_units ("
            "  id TEXT PRIMARY KEY,"
            "  project_type TEXT, domain TEXT, source_doc TEXT,"
            "  source_platform TEXT, section_title TEXT,"
            "  concept TEXT, body TEXT, relationships TEXT,"
            "  caveats TEXT, tags TEXT, confidence REAL,"
            "  source_date TEXT, ingested_at TEXT,"
            "  qdrant_id TEXT, embedding_model TEXT"
            ")"
        )
        await conn.execute(
            "CREATE VIRTUAL TABLE knowledge_fts USING fts5("
            "  unit_id, concept, body, tags, domain, project_type"
            ")"
        )
        await conn.commit()
        yield conn


class TestAnalysis:
    """CC update analysis and finding storage."""

    @pytest.mark.asyncio
    async def test_analysis_without_router_stores_finding(self, db) -> None:
        """Without router, stores informational finding."""
        analyzer = CCUpdateAnalyzer(db=db)

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value=""):
            result = await analyzer.analyze("1.0.0", "1.1.0")

        assert result["impact"] == IMPACT_INFORMATIONAL
        assert "finding_id" in result

        # Verify stored in DB
        cursor = await db.execute(
            "SELECT content, priority FROM observations WHERE id = ?",
            (result["finding_id"],),
        )
        row = await cursor.fetchone()
        assert row is not None
        data = json.loads(row[0])
        assert data["old_version"] == "1.0.0"
        assert data["new_version"] == "1.1.0"

    @pytest.mark.asyncio
    async def test_analysis_with_router_parses_llm_response(self, db) -> None:
        """With router, uses LLM to classify impact."""
        router = AsyncMock()
        llm_result = MagicMock()
        llm_result.success = True
        llm_result.content = json.dumps({
            "impact": IMPACT_ACTION_NEEDED,
            "summary": "New --output flag changes JSON format",
            "details": "The result JSON schema has been updated",
        })
        router.route_call = AsyncMock(return_value=llm_result)

        analyzer = CCUpdateAnalyzer(db=db, router=router)

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="Some changelog"):
            result = await analyzer.analyze("1.0.0", "1.1.0")

        assert result["impact"] == IMPACT_ACTION_NEEDED
        router.route_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_breaking_change_stored_as_high_priority(self, db) -> None:
        """Breaking changes are stored with high priority."""
        router = AsyncMock()
        llm_result = MagicMock()
        llm_result.success = True
        llm_result.content = json.dumps({
            "impact": IMPACT_BREAKING,
            "summary": "Session resume removed",
            "details": "--resume flag deprecated",
        })
        router.route_call = AsyncMock(return_value=llm_result)

        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="changelog"):
            result = await analyzer.analyze("1.0.0", "2.0.0")

        cursor = await db.execute(
            "SELECT priority FROM observations WHERE id = ?",
            (result["finding_id"],),
        )
        row = await cursor.fetchone()
        assert row[0] == "high"

    @pytest.mark.asyncio
    async def test_informational_stored_as_low_priority(self, db) -> None:
        """Informational changes are stored with low priority."""
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value=""):
            result = await analyzer.analyze("1.0.0", "1.0.1")

        cursor = await db.execute(
            "SELECT priority FROM observations WHERE id = ?",
            (result["finding_id"],),
        )
        row = await cursor.fetchone()
        assert row[0] == "low"

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_gracefully(self, db) -> None:
        """LLM failure falls back to informational."""
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=ConnectionError("down"))

        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="some log"):
            result = await analyzer.analyze("1.0.0", "1.1.0")

        assert result["impact"] == IMPACT_INFORMATIONAL


class TestChangelogFetch:
    """GitHub-based changelog fetching."""

    @pytest.mark.asyncio
    async def test_fetch_parses_matching_release(self, db) -> None:
        """Fetches body from release matching the version tag."""
        releases = json.dumps([
            {"tag_name": "v1.1.0", "body": "## What's changed\n\n- Added feature X"},
            {"tag_name": "v1.0.0", "body": "Old release"},
        ])

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")

        assert "Added feature X" in result

    @pytest.mark.asyncio
    async def test_fetch_handles_claude_version_format(self, db) -> None:
        """Strips ' (Claude Code)' suffix from version when matching tags."""
        releases = json.dumps([
            {"tag_name": "v2.1.84", "body": "PowerShell tool added"},
        ])

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await analyzer._fetch_changelog("2.1.83 (Claude Code)", "2.1.84 (Claude Code)")

        assert "PowerShell" in result

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_gh_failure(self, db) -> None:
        """Returns empty string when gh CLI fails."""
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"auth required"))
        mock_proc.returncode = 1

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")

        assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_on_timeout(self, db) -> None:
        """Returns empty string on timeout."""
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc), \
             patch("genesis.recon.cc_update_analyzer.asyncio.wait_for",
                    side_effect=TimeoutError):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")

        assert result == ""
        mock_proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_returns_empty_when_tag_not_found(self, db) -> None:
        """Returns empty string when no release matches the version tag."""
        releases = json.dumps([
            {"tag_name": "v2.0.0", "body": "Different version"},
        ])

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")

        assert result == ""

    @pytest.mark.asyncio
    async def test_fetch_truncates_long_body(self, db) -> None:
        """Release body over 1000 chars is truncated."""
        long_body = "x" * 1500
        releases = json.dumps([
            {"tag_name": "v1.1.0", "body": long_body},
        ])

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0

        analyzer = CCUpdateAnalyzer(db=db)

        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                    return_value=mock_proc):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")

        assert len(result) < 1500
        assert result.endswith("(truncated)")


class TestVersionToTag:
    """Version string normalization."""

    def test_plain_version(self) -> None:
        assert CCUpdateAnalyzer._version_to_tag("2.1.84") == "v2.1.84"

    def test_claude_code_suffix(self) -> None:
        assert CCUpdateAnalyzer._version_to_tag("2.1.84 (Claude Code)") == "v2.1.84"

    def test_already_prefixed(self) -> None:
        assert CCUpdateAnalyzer._version_to_tag("v2.1.84") == "v2.1.84"

    def test_prefixed_with_suffix(self) -> None:
        assert CCUpdateAnalyzer._version_to_tag("v2.1.84 (Claude Code)") == "v2.1.84"


class TestAlertOutreach:
    """Outreach pipeline wiring for CC update alerts."""

    @pytest.mark.asyncio
    async def test_alert_sends_outreach_for_breaking(self, db) -> None:
        """Breaking change triggers outreach submit."""
        mock_pipeline = AsyncMock()
        mock_result = MagicMock()
        mock_result.status.value = "delivered"
        mock_pipeline.submit = AsyncMock(return_value=mock_result)

        analyzer = CCUpdateAnalyzer(db=db, pipeline=mock_pipeline)

        analysis = {
            "impact": IMPACT_BREAKING,
            "summary": "Session resume flag removed",
            "details": "--resume is gone",
        }
        await analyzer._alert(analysis, "2.1.83 (Claude Code)", "2.1.84 (Claude Code)")

        mock_pipeline.submit.assert_called_once()
        request = mock_pipeline.submit.call_args[0][0]

        assert "2.1.83" in request.context
        assert "2.1.84" in request.context
        assert "breaking" in request.context
        assert request.signal_type == "cc_version_update"
        assert request.category.value == "alert"
        assert request.salience_score == 0.9

    @pytest.mark.asyncio
    async def test_alert_sends_outreach_for_action_needed(self, db) -> None:
        """action_needed also triggers outreach."""
        mock_pipeline = AsyncMock()
        mock_result = MagicMock()
        mock_result.status.value = "delivered"
        mock_pipeline.submit = AsyncMock(return_value=mock_result)

        analyzer = CCUpdateAnalyzer(db=db, pipeline=mock_pipeline)

        analysis = {
            "impact": IMPACT_ACTION_NEEDED,
            "summary": "New flag available",
            "details": "",
        }
        await analyzer._alert(analysis, "1.0.0", "1.1.0")

        mock_pipeline.submit.assert_called_once()

    @pytest.mark.asyncio
    async def test_alert_without_pipeline_logs_warning(self, db) -> None:
        """Without pipeline, logs warning but doesn't crash."""
        analyzer = CCUpdateAnalyzer(db=db)  # No pipeline

        analysis = {"impact": IMPACT_BREAKING, "summary": "test", "details": ""}
        # Should not raise
        await analyzer._alert(analysis, "1.0.0", "2.0.0")

    @pytest.mark.asyncio
    async def test_alert_not_called_for_informational(self, db) -> None:
        """Informational impact does NOT trigger alert."""
        mock_pipeline = AsyncMock()
        analyzer = CCUpdateAnalyzer(db=db, pipeline=mock_pipeline)

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value=""):
            result = await analyzer.analyze("1.0.0", "1.0.1")

        assert result["impact"] == IMPACT_INFORMATIONAL
        mock_pipeline.submit.assert_not_called()

    @pytest.mark.asyncio
    async def test_outreach_failure_handled_gracefully(self, db) -> None:
        """Pipeline delivery failure doesn't crash the analyzer."""
        mock_pipeline = AsyncMock()
        mock_pipeline.submit = AsyncMock(side_effect=ConnectionError("down"))

        analyzer = CCUpdateAnalyzer(db=db, pipeline=mock_pipeline)

        analysis = {"impact": IMPACT_BREAKING, "summary": "test", "details": ""}
        # Should not raise
        await analyzer._alert(analysis, "1.0.0", "2.0.0")


class TestFallbackAnalysis:
    """Tests for keyword-based fallback when LLM analysis is unavailable."""

    def test_fallback_detects_hook_keyword(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.84", "2.1.85", "Added conditional if field for hooks",
        )
        assert "hooks" in result["summary"]
        assert result["impact"] == IMPACT_INFORMATIONAL

    def test_fallback_detects_breaking_keyword(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.84", "2.1.85", "Breaking: removed --old-flag support",
        )
        assert result["impact"] == IMPACT_ACTION_NEEDED
        assert "breaking changes" in result["summary"]

    def test_fallback_detects_multiple_keywords(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.84", "2.1.85",
            "Added --bare flag, fixed MCP server bug, security patch",
        )
        assert "CLI flags" in result["summary"]
        assert "MCP" in result["summary"]
        assert "security" in result["summary"]

    def test_fallback_empty_changelog(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis("2.1.84", "2.1.85", "")
        assert "LLM analysis unavailable" in result["summary"]
        assert result["details"] == "Changelog not available"

    def test_fallback_preserves_changelog_in_details(self) -> None:
        changelog = "Fixed critical subprocess bug"
        result = CCUpdateAnalyzer._fallback_analysis("2.1.84", "2.1.85", changelog)
        assert result["details"] == changelog


class TestFindingChangelogStorage:
    """Tests that findings always include raw changelog."""

    @pytest.mark.asyncio
    async def test_finding_includes_changelog(self, db) -> None:
        analyzer = CCUpdateAnalyzer(db=db)
        changelog = "## What's changed\n- Added --bare flag"
        analysis = {
            "impact": IMPACT_INFORMATIONAL,
            "summary": "test",
            "details": "test details",
        }
        finding_id = await analyzer._store_finding(
            "2.1.84", "2.1.85", analysis, changelog,
        )
        cursor = await db.execute(
            "SELECT content FROM observations WHERE id = ?", (finding_id,),
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert data["changelog"] == changelog

    @pytest.mark.asyncio
    async def test_finding_without_changelog(self, db) -> None:
        analyzer = CCUpdateAnalyzer(db=db)
        analysis = {
            "impact": IMPACT_INFORMATIONAL,
            "summary": "test",
            "details": "test details",
        }
        finding_id = await analyzer._store_finding("2.1.84", "2.1.85", analysis)
        cursor = await db.execute(
            "SELECT content FROM observations WHERE id = ?", (finding_id,),
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert "changelog" not in data

    @pytest.mark.asyncio
    async def test_finding_truncates_long_changelog(self, db) -> None:
        analyzer = CCUpdateAnalyzer(db=db)
        long_changelog = "x" * 3000
        analysis = {"impact": IMPACT_INFORMATIONAL, "summary": "t", "details": "d"}
        finding_id = await analyzer._store_finding(
            "2.1.84", "2.1.85", analysis, long_changelog,
        )
        cursor = await db.execute(
            "SELECT content FROM observations WHERE id = ?", (finding_id,),
        )
        row = await cursor.fetchone()
        data = json.loads(row[0])
        assert len(data["changelog"]) == 2000


class TestKnowledgeIngestion:
    """Tests for CC update ingestion into knowledge base."""

    @pytest.mark.asyncio
    async def test_ingest_stores_knowledge_unit(self, db) -> None:
        """Happy path: analysis with details creates a knowledge unit."""
        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="qdrant-uuid-123")
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "test-embed-model"

        analyzer = CCUpdateAnalyzer(db=db, memory_store=mock_store)

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="raw log"):
            result = await analyzer.analyze("2.1.85", "2.1.86")

        # Verify MemoryStore.store was called
        mock_store.store.assert_awaited_once()
        call_kwargs = mock_store.store.call_args
        assert call_kwargs.kwargs["collection"] == "knowledge_base"
        assert call_kwargs.kwargs["memory_type"] == "knowledge"
        assert call_kwargs.kwargs["auto_link"] is False
        assert "claude-code" in call_kwargs.kwargs["tags"]

        # Verify knowledge_units row created
        cursor = await db.execute(
            "SELECT * FROM knowledge_units WHERE domain = 'claude-code'",
        )
        row = await cursor.fetchone()
        assert row is not None

        # Verify finding_id still returned (observation not broken)
        assert "finding_id" in result

    @pytest.mark.asyncio
    async def test_ingest_skipped_without_memory_store(self, db) -> None:
        """No memory_store means knowledge ingestion is skipped (observation still stored)."""
        analyzer = CCUpdateAnalyzer(db=db)  # No memory_store

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="log"):
            result = await analyzer.analyze("2.1.85", "2.1.86")

        # Observation still stored
        assert "finding_id" in result
        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        assert count == 1

        # No knowledge unit
        cursor = await db.execute("SELECT COUNT(*) FROM knowledge_units")
        count = (await cursor.fetchone())[0]
        assert count == 0

    @pytest.mark.asyncio
    async def test_ingest_skipped_when_no_details(self, db) -> None:
        """Empty details means nothing worth ingesting."""
        mock_store = AsyncMock()
        analyzer = CCUpdateAnalyzer(db=db, memory_store=mock_store)

        analysis = {"impact": IMPACT_INFORMATIONAL, "summary": "test", "details": ""}
        await analyzer._ingest_to_knowledge("2.1.86", analysis, "")

        mock_store.store.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ingest_failure_does_not_block_observation(self, db) -> None:
        """MemoryStore failure is caught — observation is already stored."""
        mock_store = AsyncMock()
        mock_store.store = AsyncMock(side_effect=RuntimeError("embedding service down"))

        analyzer = CCUpdateAnalyzer(db=db, memory_store=mock_store)

        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock, return_value="log"):
            result = await analyzer.analyze("2.1.85", "2.1.86")

        # Observation was stored before knowledge ingestion attempted
        assert "finding_id" in result
        cursor = await db.execute("SELECT COUNT(*) FROM observations")
        count = (await cursor.fetchone())[0]
        assert count == 1

    @pytest.mark.asyncio
    async def test_ingest_includes_changelog_in_body(self, db) -> None:
        """When changelog differs from details, both are included in body."""
        mock_store = AsyncMock()
        mock_store.store = AsyncMock(return_value="q-id")
        mock_store._embeddings = MagicMock()
        mock_store._embeddings.model_name = "test-model"

        analyzer = CCUpdateAnalyzer(db=db, memory_store=mock_store)

        analysis = {
            "impact": IMPACT_INFORMATIONAL,
            "summary": "CC update",
            "details": "Fixed --bare MCP bug",
        }
        await analyzer._ingest_to_knowledge("2.1.86", analysis, "## Raw\n- bare fix")

        body = mock_store.store.call_args[0][0]
        assert "Fixed --bare MCP bug" in body
        assert "## Raw changelog" in body
        assert "bare fix" in body
