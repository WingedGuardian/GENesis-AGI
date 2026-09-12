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


def _llm(impact: str, summary: str, details: str) -> MagicMock:
    """Build a mock router result carrying an analyzer JSON payload."""
    r = MagicMock()
    r.success = True
    r.content = json.dumps({"impact": impact, "summary": summary, "details": details})
    return r


@pytest.fixture()
async def db():
    """In-memory SQLite with the full production schema.

    Uses ``create_all_tables`` instead of a hand-rolled subset so that schema
    migrations (e.g. the UNIQUE constraint on knowledge_units that powers the
    reference store upsert path) automatically flow into this fixture.
    """
    from genesis.db.schema import create_all_tables

    async with aiosqlite.connect(":memory:") as conn:
        await create_all_tables(conn)
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


class TestChangelogRangeFetch:
    """Range-aware fetch: CHANGELOG.md primary, releases-API single-version fallback."""

    # Newest-first, mirroring the real CHANGELOG.md ordering.
    _MULTI = (
        "## 2.1.220\n\n- fix twenty\n\n"
        "## 2.1.219\n\n- Added Opus 5\n- fix nineteen\n\n"
        "## 2.1.218\n\n- old fix\n"
    )

    @pytest.mark.asyncio
    async def test_range_extracts_all_intervening_releases(self, db) -> None:
        """A multi-release jump analyzes EVERY release in (old, new] — the bug fix."""
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog_md",
                          new_callable=AsyncMock, return_value=self._MULTI):
            result = await analyzer._fetch_changelog("2.1.218", "2.1.220")
        assert "2.1.220" in result
        # The intervening release is NOT skipped (the whole point):
        assert "2.1.219" in result and "Added Opus 5" in result
        assert "2.1.218" not in result  # old bound is exclusive

    @pytest.mark.asyncio
    async def test_range_respects_claude_version_suffix(self, db) -> None:
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog_md",
                          new_callable=AsyncMock, return_value=self._MULTI):
            result = await analyzer._fetch_changelog(
                "2.1.218 (Claude Code)", "2.1.219 (Claude Code)",
            )
        assert "2.1.219" in result
        assert "2.1.220" not in result  # above the new bound

    @pytest.mark.asyncio
    async def test_falls_back_to_release_body_marked_partial(self, db) -> None:
        """CHANGELOG unavailable -> single-version body, LOUDLY marked PARTIAL."""
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog_md",
                          new_callable=AsyncMock, return_value=""), \
             patch.object(analyzer, "_fetch_release_body",
                          new_callable=AsyncMock, return_value="only new notes"):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")
        assert "only new notes" in result
        assert "[PARTIAL]" in result

    @pytest.mark.asyncio
    async def test_fallback_when_range_empty(self, db) -> None:
        """CHANGELOG fetched but no sections in range (equal/downgrade) -> fallback."""
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog_md",
                          new_callable=AsyncMock, return_value=self._MULTI), \
             patch.object(analyzer, "_fetch_release_body",
                          new_callable=AsyncMock, return_value="new only") as rb:
            result = await analyzer._fetch_changelog("2.1.220", "2.1.220")
        rb.assert_awaited_once()
        assert "[PARTIAL]" in result

    @pytest.mark.asyncio
    async def test_returns_empty_when_both_sources_fail(self, db) -> None:
        analyzer = CCUpdateAnalyzer(db=db)
        with patch.object(analyzer, "_fetch_changelog_md",
                          new_callable=AsyncMock, return_value=""), \
             patch.object(analyzer, "_fetch_release_body",
                          new_callable=AsyncMock, return_value=""):
            result = await analyzer._fetch_changelog("1.0.0", "1.1.0")
        assert result == ""


class TestChangelogMdFetch:
    """The raw CHANGELOG.md fetch (curl)."""

    @pytest.mark.asyncio
    async def test_md_fetch_success(self, db) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"## 2.1.5\n- x", b""))
        mock_proc.returncode = 0
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc):
            result = await analyzer._fetch_changelog_md()
        assert "2.1.5" in result

    @pytest.mark.asyncio
    async def test_md_fetch_empty_on_failure(self, db) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"404"))
        mock_proc.returncode = 22
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc):
            assert await analyzer._fetch_changelog_md() == ""

    @pytest.mark.asyncio
    async def test_md_fetch_empty_on_timeout(self, db) -> None:
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc), \
             patch("genesis.recon.cc_update_analyzer.asyncio.wait_for",
                   side_effect=TimeoutError):
            assert await analyzer._fetch_changelog_md() == ""
        mock_proc.kill.assert_called_once()


class TestReleaseBodyFallback:
    """The releases-API single-version fallback (_fetch_release_body)."""

    @pytest.mark.asyncio
    async def test_parses_matching_release(self, db) -> None:
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
            result = await analyzer._fetch_release_body("1.1.0")
        assert "Added feature X" in result

    @pytest.mark.asyncio
    async def test_handles_claude_version_format(self, db) -> None:
        releases = json.dumps([{"tag_name": "v2.1.84", "body": "PowerShell tool added"}])
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc):
            result = await analyzer._fetch_release_body("2.1.84 (Claude Code)")
        assert "PowerShell" in result

    @pytest.mark.asyncio
    async def test_empty_on_gh_failure(self, db) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"auth required"))
        mock_proc.returncode = 1
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc):
            assert await analyzer._fetch_release_body("1.1.0") == ""

    @pytest.mark.asyncio
    async def test_empty_when_tag_not_found(self, db) -> None:
        releases = json.dumps([{"tag_name": "v2.0.0", "body": "Different version"}])
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(releases.encode(), b""))
        mock_proc.returncode = 0
        analyzer = CCUpdateAnalyzer(db=db)
        with patch("genesis.recon.cc_update_analyzer.asyncio.create_subprocess_exec",
                   return_value=mock_proc):
            assert await analyzer._fetch_release_body("1.1.0") == ""


class TestSectionExtraction:
    """Pure range extraction from CHANGELOG.md text (no I/O)."""

    _MD = (
        "# Changelog\n\n"
        "## 2.1.245\n\n- glibc fix\n\n"
        "## 2.1.243\n\n- Loops breakdown\n\n"
        "## 2.1.233\n\n- Todo tools removed on newer models\n\n"
        "## 2.1.218\n\n- baseline\n"
    )

    def test_range_is_old_exclusive_new_inclusive(self) -> None:
        r = CCUpdateAnalyzer._extract_sections_in_range(self._MD, "2.1.218", "2.1.245")
        assert "2.1.245" in r and "2.1.243" in r and "2.1.233" in r
        assert "- baseline" not in r  # old (2.1.218) section excluded
        assert "Todo tools removed" in r  # a middle release is present, not skipped

    def test_upper_bound_inclusive_excludes_above(self) -> None:
        r = CCUpdateAnalyzer._extract_sections_in_range(self._MD, "2.1.218", "2.1.233")
        assert "2.1.233" in r
        assert "2.1.243" not in r and "2.1.245" not in r

    def test_skipped_version_numbers_handled(self) -> None:
        # 2.1.244 does not exist; asking up to it still yields 2.1.243 (present).
        r = CCUpdateAnalyzer._extract_sections_in_range(self._MD, "2.1.218", "2.1.244")
        assert "2.1.243" in r and "2.1.245" not in r

    def test_empty_when_no_sections_in_range(self) -> None:
        assert CCUpdateAnalyzer._extract_sections_in_range(self._MD, "2.1.245", "2.1.245") == ""
        assert CCUpdateAnalyzer._extract_sections_in_range(self._MD, "2.1.245", "2.1.243") == ""

    def test_full_range_returned_uncapped(self) -> None:
        # No size cap in extraction — the chunker covers everything downstream.
        big = "".join(
            f"## 2.1.{n}\n\n" + ("x" * 5000) + "\n\n"
            for n in range(309, 299, -1)
        )
        r = CCUpdateAnalyzer._extract_sections_in_range(big, "2.1.299", "2.1.309")
        # every in-range release present; nothing omitted at this layer
        for n in range(300, 310):
            assert f"## 2.1.{n}" in r
        assert "omitted" not in r


class TestChunking:
    """Context-safe chunking of the concatenated changelog (map step)."""

    def test_packs_sections_under_budget_into_one_chunk(self) -> None:
        cl = "## 2.1.5\n\n- a\n\n## 2.1.4\n\n- b\n\n## 2.1.3\n\n- c"
        chunks = CCUpdateAnalyzer._chunk_changelog(cl, budget=10000, max_chunks=12)
        assert len(chunks) == 1
        assert "2.1.5" in chunks[0] and "2.1.3" in chunks[0]

    def test_splits_on_release_boundaries_when_over_budget(self) -> None:
        cl = "".join(f"## 2.1.{n}\n\n" + ("x" * 5000) + "\n\n" for n in (7, 6, 5))
        chunks = CCUpdateAnalyzer._chunk_changelog(cl, budget=8000, max_chunks=12)
        assert len(chunks) >= 2
        # every section is intact somewhere — no release dropped or split away
        joined = "\n".join(chunks)
        for n in (7, 6, 5):
            assert f"## 2.1.{n}" in joined

    def test_max_chunks_cap_keeps_newest_with_loud_marker(self) -> None:
        cl = "".join(f"## 2.1.{n}\n\n" + ("x" * 5000) + "\n\n" for n in range(20, 0, -1))
        chunks = CCUpdateAnalyzer._chunk_changelog(cl, budget=6000, max_chunks=3)
        assert len(chunks) == 3
        assert "## 2.1.20" in chunks[0]  # newest kept
        assert "omitted" in chunks[-1]   # loud marker, never a silent cut

    def test_no_headers_returns_single_chunk(self) -> None:
        assert CCUpdateAnalyzer._chunk_changelog("[PARTIAL] just notes") == [
            "[PARTIAL] just notes",
        ]

    def test_empty_returns_no_chunks(self) -> None:
        assert CCUpdateAnalyzer._chunk_changelog("   ") == []


class TestRangeAnalysis:
    """Map-reduce analysis: per-chunk classification aggregated by severity."""

    @pytest.mark.asyncio
    async def test_range_analysis_aggregates_max_severity(self, db) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=[
            _llm(IMPACT_INFORMATIONAL, "chunk A", "details A"),
            _llm(IMPACT_BREAKING, "chunk B removed --resume", "details B"),
        ])
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_chunk_changelog",
                          return_value=["chunk1", "chunk2"]):
            out = await analyzer._analyze_range("2.1.3", "2.1.5", "irrelevant")
        assert out["impact"] == IMPACT_BREAKING          # worst chunk wins
        assert router.route_call.await_count == 2        # one LLM call per chunk
        assert "details B" in out["details"]             # merged details retained

    @pytest.mark.asyncio
    async def test_analyze_multichunk_stores_high_priority(self, db) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=[
            _llm(IMPACT_INFORMATIONAL, "A", "dA"),
            _llm(IMPACT_BREAKING, "B", "dB"),
        ])
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_fetch_changelog",
                          new_callable=AsyncMock, return_value="## 2.1.5\n- x"), \
             patch.object(analyzer, "_chunk_changelog",
                          return_value=["c1", "c2"]):
            result = await analyzer.analyze("2.1.3", "2.1.5")
        assert result["impact"] == IMPACT_BREAKING
        cursor = await db.execute(
            "SELECT priority FROM observations WHERE id = ?", (result["finding_id"],),
        )
        assert (await cursor.fetchone())[0] == "high"

    @pytest.mark.asyncio
    async def test_single_chunk_passthrough_verbatim(self, db) -> None:
        """A small (single-chunk) bump keeps the prior single-call behaviour."""
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=_llm(IMPACT_ACTION_NEEDED, "solo", "d"))
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_fetch_changelog",
                          new_callable=AsyncMock, return_value="## 2.1.5\n- x"):
            result = await analyzer.analyze("2.1.4", "2.1.5")
        assert result["impact"] == IMPACT_ACTION_NEEDED
        assert result["summary"] == "solo"           # verbatim, not the rollup form
        assert router.route_call.await_count == 1

    @pytest.mark.asyncio
    async def test_non_dict_llm_payload_falls_back(self, db) -> None:
        """A free model emitting bare-string/array JSON must not crash the reducer."""
        router = AsyncMock()
        bad = MagicMock()
        bad.success = True
        bad.content = json.dumps("done")  # valid JSON, but a str — not an object
        router.route_call = AsyncMock(return_value=bad)
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        # _llm_analyze must return the structured fallback dict, never the str
        out = await analyzer._llm_analyze("2.1.4", "2.1.5", "Fixed a memory leak")
        assert isinstance(out, dict)
        assert out["impact"] == IMPACT_INFORMATIONAL
        # and analyze() end-to-end does not raise on the non-dict payload
        with patch.object(analyzer, "_fetch_changelog",
                          new_callable=AsyncMock, return_value="## 2.1.5\n- x"):
            result = await analyzer.analyze("2.1.4", "2.1.5")
        assert result["impact"] == IMPACT_INFORMATIONAL

    @pytest.mark.asyncio
    async def test_multichunk_summary_rollup_format(self, db) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=[
            _llm(IMPACT_INFORMATIONAL, "A", "dA"),
            _llm(IMPACT_ACTION_NEEDED, "B needs care", "dB"),
        ])
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_chunk_changelog", return_value=["c1", "c2"]):
            out = await analyzer._analyze_range("2.1.3", "2.1.5", "x")
        assert "analyzed across 2 changelog chunks" in out["summary"]
        assert "Highest-severity item: B needs care" in out["summary"]


class TestSemverTuple:
    """Version-string -> numeric tuple."""

    def test_plain(self) -> None:
        assert CCUpdateAnalyzer._semver_tuple("2.1.84") == (2, 1, 84)

    def test_suffix_and_prefix(self) -> None:
        assert CCUpdateAnalyzer._semver_tuple("v2.1.84 (Claude Code)") == (2, 1, 84)

    def test_ordering_numeric_not_lexical(self) -> None:
        assert CCUpdateAnalyzer._semver_tuple("2.1.9") < CCUpdateAnalyzer._semver_tuple("2.1.10")

    def test_empty_on_nonversion(self) -> None:
        assert CCUpdateAnalyzer._semver_tuple("not a version") == ()


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

    def test_fallback_detects_rendering_keywords(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Added alt-screen rendering with scrollback",
        )
        assert "rendering" in result["summary"]
        assert result["impact"] == IMPACT_INFORMATIONAL

    def test_fallback_detects_regression_as_action_needed(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Regression in terminal scrollback behavior",
        )
        assert result["impact"] == IMPACT_ACTION_NEEDED

    def test_fallback_deprecated_stays_informational(self) -> None:
        """'deprecated' alone doesn't trigger action_needed (only breaking/removed/regression do)."""
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Deprecated old flag in favor of new one",
        )
        assert "deprecations" in result["summary"]
        assert result["impact"] == IMPACT_INFORMATIONAL

    def test_fallback_casual_removed_stays_informational(self) -> None:
        """Casual 'removed' (e.g. 'Removed debug log') should NOT escalate to action_needed."""
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Removed unnecessary whitespace in output",
        )
        assert result["impact"] == IMPACT_INFORMATIONAL

    def test_fallback_real_removal_escalates(self) -> None:
        """Phrase-level removal ('removed support', 'no longer') escalates to action_needed."""
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Removed support for --old-flag",
        )
        assert result["impact"] == IMPACT_ACTION_NEEDED

    def test_fallback_deduplicates_labels(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89",
            "Fixed alt-screen flicker, improved scrollback in terminal",
        )
        assert result["summary"].count("rendering") == 1

    def test_fallback_detects_performance_keywords(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Fixed memory leak in long-running sessions",
        )
        assert "performance" in result["summary"]

    def test_fallback_detects_platform_keywords(self) -> None:
        result = CCUpdateAnalyzer._fallback_analysis(
            "2.1.88", "2.1.89", "Fixed tmux scrollback on Linux",
        )
        assert "platform" in result["summary"]

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


class TestHardeningClassA_SizeBounding:
    """Every chunk fed to an LLM call must fit the smallest provider's budget."""

    def test_chunk_budget_fits_smallest_provider_tpm(self) -> None:
        # groq-free (gpt-oss-120b) caps at 8000 TPM; a chunk must leave room for
        # the ~600-token prompt + output + reasoning inflation. ~16k chars ~= 4k tokens.
        from genesis.recon.cc_update_analyzer import _CHUNK_BUDGET

        assert _CHUNK_BUDGET <= 20000

    def test_headerless_body_over_budget_is_length_split(self) -> None:
        # The [PARTIAL] fallback body has no `## X.Y.Z` headers; it must still be
        # bounded, not returned as one oversized chunk.
        body = "[PARTIAL] " + ("word " * 8000)  # ~40k chars, no headers
        chunks = CCUpdateAnalyzer._chunk_changelog(body, budget=16000, max_chunks=12)
        assert len(chunks) >= 2
        assert all(len(c) <= 16000 for c in chunks)

    def test_oversized_single_section_is_length_split(self) -> None:
        # A single release section larger than budget must be split, not emitted whole.
        big_section = "## 2.1.99\n\n" + ("x " * 12000)  # ~24k chars in one section
        chunks = CCUpdateAnalyzer._chunk_changelog(big_section, budget=16000, max_chunks=12)
        assert len(chunks) >= 2
        assert all(len(c) <= 16000 for c in chunks)

    def test_small_changelog_stays_single_chunk(self) -> None:
        # Regression guard: a normal small bump must still be one chunk.
        cl = "## 2.1.5\n\n- a\n\n## 2.1.4\n\n- b"
        chunks = CCUpdateAnalyzer._chunk_changelog(cl, budget=16000, max_chunks=12)
        assert len(chunks) == 1

    def test_maxchunks_marker_keeps_last_chunk_within_budget(self) -> None:
        # When the cap bites and the last kept chunk is already at budget, the loud
        # omission marker must NOT push it over budget (invariant holds on the
        # omission path too).
        sections = "".join(f"## 2.1.{n}\n\n" + ("q" * 15980) + "\n\n" for n in range(30, 0, -1))
        chunks = CCUpdateAnalyzer._chunk_changelog(sections, budget=16000, max_chunks=3)
        assert len(chunks) == 3
        assert "omitted" in chunks[-1]  # loud marker present
        assert all(len(c) <= 16000 for c in chunks)  # and still within budget


class TestHardeningClassB_ChainDistribution:
    """N gathered route_calls must distribute across the chain, not serialize."""

    @pytest.mark.asyncio
    async def test_each_chunk_gets_distinct_chain_offset(self, db) -> None:
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=_llm(IMPACT_INFORMATIONAL, "s", "d"))
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_chunk_changelog", return_value=["c0", "c1", "c2"]):
            await analyzer._analyze_range("2.1.3", "2.1.9", "irrelevant")
        offsets = sorted(
            call.kwargs.get("chain_offset", 0)
            for call in router.route_call.await_args_list
        )
        assert offsets == [0, 1, 2]  # distributed across the chain, not all 0


class TestHardeningClassC_OutputNormalization:
    """Untrusted LLM JSON is normalized to the full contract at one boundary."""

    @pytest.mark.asyncio
    async def test_list_impact_dict_falls_back_no_crash(self, db) -> None:
        # A dict with an unhashable (list) impact must NOT reach the reducer.
        bad = MagicMock()
        bad.success = True
        bad.content = json.dumps({"impact": ["breaking"], "summary": "s", "details": "d"})
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=bad)
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        out = await analyzer._llm_analyze("2.1.4", "2.1.5", "Fixed a leak")
        assert isinstance(out, dict)
        assert out["impact"] in (IMPACT_INFORMATIONAL, IMPACT_ACTION_NEEDED)
        assert isinstance(out["summary"], str) and isinstance(out["details"], str)

    @pytest.mark.asyncio
    async def test_numeric_fields_coerced_to_str(self, db) -> None:
        # Valid impact but non-str summary/details must be coerced, not passed through.
        bad = MagicMock()
        bad.success = True
        bad.content = json.dumps({"impact": IMPACT_BREAKING, "summary": 42, "details": 99})
        router = AsyncMock()
        router.route_call = AsyncMock(return_value=bad)
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        out = await analyzer._llm_analyze("2.1.4", "2.1.5", "x")
        assert out["impact"] == IMPACT_BREAKING
        assert isinstance(out["summary"], str) and isinstance(out["details"], str)

    @pytest.mark.asyncio
    async def test_multichunk_malformed_payload_end_to_end_no_crash(self, db) -> None:
        # The reducer/ingest/alert must survive a malformed chunk payload end to end.
        good = _llm(IMPACT_INFORMATIONAL, "A", "dA")
        bad = MagicMock()
        bad.success = True
        bad.content = json.dumps({"impact": {"x": 1}, "details": [1, 2], "summary": None})
        router = AsyncMock()
        router.route_call = AsyncMock(side_effect=[good, bad])
        analyzer = CCUpdateAnalyzer(db=db, router=router)
        with patch.object(analyzer, "_fetch_changelog", new_callable=AsyncMock,
                          return_value="## 2.1.5\n- x"), \
             patch.object(analyzer, "_chunk_changelog", return_value=["c1", "c2"]):
            result = await analyzer.analyze("2.1.3", "2.1.5")
        assert isinstance(result["impact"], str)
        assert "finding_id" in result

    def test_coerce_returns_none_on_unhashable_impact(self) -> None:
        # An unhashable impact must return None (-> fallback), never raise on the
        # `in frozenset` membership test.
        assert CCUpdateAnalyzer._coerce_analysis({"impact": ["breaking"], "summary": "s"}) is None
        assert CCUpdateAnalyzer._coerce_analysis({"impact": {"x": 1}}) is None
        assert CCUpdateAnalyzer._coerce_analysis("not a dict") is None
        assert CCUpdateAnalyzer._coerce_analysis({"summary": "no impact key"}) is None

    def test_coerce_normalizes_valid_payload_to_str_fields(self) -> None:
        out = CCUpdateAnalyzer._coerce_analysis(
            {"impact": IMPACT_BREAKING, "summary": 42, "details": None},
        )
        assert out == {"impact": IMPACT_BREAKING, "summary": "42", "details": ""}
