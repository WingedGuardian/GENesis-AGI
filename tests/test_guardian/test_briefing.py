"""Tests for Guardian briefing — shared filesystem bridge."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.briefing import (
    BriefingContent,
    _render_briefing_markdown,
    build_dynamic_briefing,
    read_guardian_briefing,
    write_dynamic_guardian_briefing,
    write_guardian_briefing,
)


class TestWriteGuardianBriefing:
    """Test briefing file generation."""

    def test_writes_md_file(self, tmp_path: Path) -> None:
        path = write_guardian_briefing(briefing_dir=tmp_path)
        assert path.exists()
        assert path.name == "guardian_briefing.md"

    def test_writes_json_file(self, tmp_path: Path) -> None:
        write_guardian_briefing(briefing_dir=tmp_path)
        json_path = tmp_path / "guardian_briefing.json"
        assert json_path.exists()
        data = json.loads(json_path.read_text())
        assert "generated_at" in data
        assert "service_baseline" in data

    def test_creates_directory(self, tmp_path: Path) -> None:
        out_dir = tmp_path / "deep" / "nested" / "dir"
        write_guardian_briefing(briefing_dir=out_dir)
        assert out_dir.exists()

    def test_custom_content(self, tmp_path: Path) -> None:
        content = BriefingContent(
            generated_at="2026-04-02T12:00:00Z",
            genesis_version="abc123",
            service_baseline={"test-svc": "test service"},
            notes=["test note"],
        )
        path = write_guardian_briefing(briefing_dir=tmp_path, content=content)
        text = path.read_text()
        assert "abc123" in text
        assert "test-svc" in text
        assert "test note" in text

    def test_default_content_has_baselines(self, tmp_path: Path) -> None:
        path = write_guardian_briefing(briefing_dir=tmp_path)
        text = path.read_text()
        assert "genesis-server" in text
        assert "qdrant" in text
        assert "awareness_tick_interval" in text


class TestReadGuardianBriefing:
    """Test briefing file reading with freshness checks."""

    def test_reads_fresh_file(self, tmp_path: Path) -> None:
        md_path = tmp_path / "guardian_briefing.md"
        md_path.write_text("# Test briefing\nSome content here.")
        result = read_guardian_briefing(md_path, max_age_s=600)
        assert result is not None
        assert "Test briefing" in result

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        result = read_guardian_briefing(tmp_path / "nonexistent.md")
        assert result is None

    def test_returns_none_for_empty_file(self, tmp_path: Path) -> None:
        md_path = tmp_path / "guardian_briefing.md"
        md_path.write_text("")
        result = read_guardian_briefing(md_path)
        assert result is None

    def test_returns_none_for_stale_file(self, tmp_path: Path) -> None:
        md_path = tmp_path / "guardian_briefing.md"
        md_path.write_text("# Old briefing")
        # Backdate the file modification time
        old_time = time.time() - 700  # 700s ago
        import os
        os.utime(md_path, (old_time, old_time))
        result = read_guardian_briefing(md_path, max_age_s=600)
        assert result is None

    def test_respects_custom_max_age(self, tmp_path: Path) -> None:
        md_path = tmp_path / "guardian_briefing.md"
        md_path.write_text("# Recent briefing")
        # File is fresh (just written)
        result = read_guardian_briefing(md_path, max_age_s=1)
        assert result is not None

    def test_whitespace_only_is_empty(self, tmp_path: Path) -> None:
        md_path = tmp_path / "guardian_briefing.md"
        md_path.write_text("   \n\n  ")
        result = read_guardian_briefing(md_path)
        assert result is None


class TestRenderBriefingMarkdown:
    """Test markdown rendering."""

    def test_includes_header(self) -> None:
        content = BriefingContent(generated_at="2026-04-02T12:00:00Z")
        md = _render_briefing_markdown(content)
        assert "Genesis Briefing" in md
        assert "2026-04-02" in md

    def test_renders_service_baseline(self) -> None:
        content = BriefingContent(
            service_baseline={"my-svc": "handles requests"},
        )
        md = _render_briefing_markdown(content)
        assert "**my-svc**" in md
        assert "handles requests" in md

    def test_renders_metric_baselines(self) -> None:
        content = BriefingContent(
            metric_baselines={"cpu_normal": "10-30%"},
        )
        md = _render_briefing_markdown(content)
        assert "cpu_normal" in md
        assert "10-30%" in md

    def test_renders_recent_incidents(self) -> None:
        content = BriefingContent(
            recent_incidents=[{
                "when": "2026-04-01",
                "cause": "OOM kill",
                "resolution": "restarted bridge",
            }],
        )
        md = _render_briefing_markdown(content)
        assert "OOM kill" in md
        assert "restarted bridge" in md

    def test_renders_observations(self) -> None:
        content = BriefingContent(
            active_observations=["Memory trending up over 3 days"],
        )
        md = _render_briefing_markdown(content)
        assert "Memory trending up" in md

    def test_renders_notes(self) -> None:
        content = BriefingContent(
            notes=["tmpfs is 512MB"],
        )
        md = _render_briefing_markdown(content)
        assert "tmpfs is 512MB" in md

    def test_skips_empty_sections(self) -> None:
        content = BriefingContent()
        md = _render_briefing_markdown(content)
        assert "Recent Incidents" not in md
        assert "Active Observations" not in md
        assert "Previously Observed" not in md


class TestBriefingRoundTrip:
    """Test write → read round trip."""

    def test_write_then_read(self, tmp_path: Path) -> None:
        content = BriefingContent(
            generated_at="2026-04-02T12:00:00Z",
            genesis_version="test123",
            notes=["round trip test"],
        )
        md_path = write_guardian_briefing(briefing_dir=tmp_path, content=content)
        result = read_guardian_briefing(md_path, max_age_s=60)
        assert result is not None
        assert "test123" in result
        assert "round trip test" in result

    def test_json_round_trip(self, tmp_path: Path) -> None:
        content = BriefingContent(
            generated_at="2026-04-02T12:00:00Z",
            genesis_version="xyz789",
            service_baseline={"svc1": "desc1"},
            metric_baselines={"mem": "50%"},
        )
        write_guardian_briefing(briefing_dir=tmp_path, content=content)
        json_path = tmp_path / "guardian_briefing.json"
        data = json.loads(json_path.read_text())
        assert data["genesis_version"] == "xyz789"
        assert data["service_baseline"]["svc1"] == "desc1"


def _make_db():
    """Create a mock DB for build_dynamic_briefing tests."""
    return AsyncMock()


# Patch targets — the actual CRUD modules, not the local import names
_OBS = "genesis.db.crud.observations.query_with_total"
_EVENTS = "genesis.db.crud.events.query"
_TICKS_LAST = "genesis.db.crud.awareness_ticks.last_tick"
_TICKS_COUNT = "genesis.db.crud.awareness_ticks.count_in_window_all"
_CC_ACTIVE = "genesis.db.crud.cc_sessions.query_active"


class TestBuildDynamicBriefing:
    """Test dynamic briefing builder."""

    @pytest.mark.asyncio
    async def test_populates_observations(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([
                {"content": "Memory trending up", "source": "infra_forecast", "priority": "high"},
                {"content": "Disk usage stable", "source": "awareness_tick", "priority": "low"},
            ], 2))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        assert len(content.active_observations) == 2
        assert "Memory trending up" in content.active_observations[0]
        assert "[high]" in content.active_observations[0]

    @pytest.mark.asyncio
    async def test_populates_errors(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([], 0))),
            patch(_EVENTS, new=AsyncMock(return_value=[
                {"subsystem": "router", "message": "Provider timeout", "timestamp": "2026-04-03T10:00:00Z"},
            ])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        assert len(content.recent_errors) == 1
        assert content.recent_errors[0]["subsystem"] == "router"

    @pytest.mark.asyncio
    async def test_populates_cc_sessions(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([], 0))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[
                {"session_type": "foreground", "model": "opus", "started_at": "2026-04-03T10:00:00Z", "source_tag": "user"},
            ])),
        ):
            content = await build_dynamic_briefing(db)

        assert len(content.active_cc_sessions) == 1
        assert content.active_cc_sessions[0]["model"] == "opus"

    @pytest.mark.asyncio
    async def test_populates_tick_info(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([], 0))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value={"created_at": "2026-04-03T11:00:00Z"})),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=12)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        assert content.last_tick_at == "2026-04-03T11:00:00Z"
        assert content.tick_count_1h == 12

    @pytest.mark.asyncio
    async def test_handles_empty_db(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([], 0))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        assert content.active_observations == []
        assert content.recent_errors == []
        assert content.active_cc_sessions == []
        assert content.last_tick_at == ""
        # Static baselines should still be present
        assert content.service_baseline
        assert content.notes

    @pytest.mark.asyncio
    async def test_handles_query_failure(self) -> None:
        """One query raising doesn't prevent others from populating."""
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(side_effect=Exception("DB locked"))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value={"created_at": "2026-04-03T11:00:00Z"})),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=5)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        # Observations failed but tick info should still be populated
        assert content.active_observations == []
        assert content.last_tick_at == "2026-04-03T11:00:00Z"
        assert content.tick_count_1h == 5

    @pytest.mark.asyncio
    async def test_truncates_long_observation_content(self) -> None:
        long_text = "x" * 300
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([
                {"content": long_text, "source": "test", "priority": "low"},
            ], 1))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)

        # Observation text should be truncated to 200 chars
        assert len(content.active_observations[0]) < 250


class TestWriteDynamicGuardianBriefing:
    """Test the async wrapper that builds + writes."""

    @pytest.mark.asyncio
    async def test_writes_file(self, tmp_path: Path) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=([
                {"content": "Test obs", "source": "test", "priority": "medium"},
            ], 1))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value={"created_at": "2026-04-03T11:00:00Z"})),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=3)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
            patch("genesis.guardian.briefing._CONTAINER_BRIEFING_DIR", tmp_path),
        ):
            await write_dynamic_guardian_briefing(db)

        md_path = tmp_path / "guardian_briefing.md"
        assert md_path.exists()
        text = md_path.read_text()
        assert "Test obs" in text
        assert "Ticks in last hour: 3" in text


class TestRenderDynamicSections:
    """Test markdown rendering of new dynamic fields."""

    def test_renders_tick_info(self) -> None:
        content = BriefingContent(
            last_tick_at="2026-04-03T11:00:00Z",
            tick_count_1h=12,
        )
        md = _render_briefing_markdown(content)
        assert "Awareness Loop Status" in md
        assert "Last tick: 2026-04-03T11:00:00Z" in md
        assert "Ticks in last hour: 12" in md

    def test_renders_cc_sessions(self) -> None:
        content = BriefingContent(
            active_cc_sessions=[
                {"type": "foreground", "model": "opus", "started": "2026-04-03T10:00:00Z", "source": "user"},
            ],
        )
        md = _render_briefing_markdown(content)
        assert "Active CC Sessions" in md
        assert "foreground" in md
        assert "opus" in md

    def test_renders_recent_errors(self) -> None:
        content = BriefingContent(
            recent_errors=[
                {"subsystem": "router", "message": "Provider timeout", "when": "2026-04-03T10:00:00Z"},
            ],
        )
        md = _render_briefing_markdown(content)
        assert "Recent Errors" in md
        assert "**router**" in md
        assert "Provider timeout" in md

    def test_skips_empty_dynamic_sections(self) -> None:
        content = BriefingContent()
        md = _render_briefing_markdown(content)
        assert "Awareness Loop Status" not in md
        assert "Active CC Sessions" not in md
        assert "Recent Errors" not in md


_OBS_COUNT = "genesis.db.crud.observations.count_unresolved"


class TestObservationDigestMarker:
    """The 15-row observation digest must announce truncation — a silent
    15-of-N reads as complete while the OLDEST (longest-standing) rows are
    the ones dropped."""

    @staticmethod
    def _page(n, total=None):
        """(rows, total) as ONE snapshot returns them — the shape the digest now
        reads. `total` defaults to the page length, i.e. an untruncated page."""
        rows = [
            {"content": f"obs {i}", "source": "s", "priority": "low"}
            for i in range(n)
        ]
        return rows, (n if total is None else total)

    @pytest.mark.asyncio
    async def test_a_full_read_with_more_beyond_gets_a_marker(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=self._page(15, 42))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        assert len(content.active_observations) == 16
        marker = content.active_observations[-1]
        assert "15 NEWEST of 42" in marker
        assert "27 older" in marker

    @pytest.mark.asyncio
    async def test_a_short_read_gets_no_marker_and_costs_no_second_query(self) -> None:
        """< cap rows = the set is complete, so no marker.

        The old version of this test asserted the separate count "must not even
        run" — an optimisation that only existed because the count WAS a second
        query. It is now a window function inside the page read, so there is no
        second query to skip; the property worth keeping is that the standalone
        counter is not called AT ALL any more, on any path. That is asserted
        here, because a caller that quietly kept using it would reintroduce the
        two-snapshot skew this change removes.
        """
        db = _make_db()
        count = AsyncMock(return_value=999)
        with (
            patch(_OBS, new=AsyncMock(return_value=self._page(3))),
            patch(_OBS_COUNT, new=count),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        assert len(content.active_observations) == 3
        count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exactly_cap_rows_total_equal_no_marker(self) -> None:
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=self._page(15, 15))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        assert len(content.active_observations) == 15

    @pytest.mark.asyncio
    async def test_a_failed_read_reports_nothing_rather_than_a_page_with_no_total(
        self,
    ) -> None:
        """REPLACES "a failed count degrades to an unknown-total marker".

        That state is now unreachable, and removing it is the point rather than
        a side effect: the page and the denominator are one read, so there is no
        way to hold a page whose denominator failed. What can still fail is the
        single read, and the honest answer there is an empty section — the
        previous shape could print 15 rows while silently losing the number that
        says whether 15 was all of them.
        """
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        assert content.active_observations == []


class TestTheDigestReadsOneSnapshot:
    """The page and its denominator must come from ONE read.

    Two awaited queries are two snapshots of a WAL database with concurrent
    writers, and the marker relates them. MEASURED as a defect class rather than
    a hypothetical — both skews below are reachable on a live install:

    * an INSERT between the reads: the page is the 15 newest as of the FIRST
      snapshot, the count is larger, and the briefing says "N older ones exist
      beyond this digest" about a row that is the NEWEST one;
    * a RESOLVE of a row that is ON the page: the count falls to the page length
      and the marker vanishes, while unresolved rows outside the page remain.

    The second one is the worse of the two — a recovery briefing that has
    silently dropped the longest-standing problems is exactly the failure the
    marker was added to prevent.
    """

    @pytest.mark.asyncio
    async def test_the_standalone_counter_is_never_called(self) -> None:
        """The structural lock. Any path that still reaches `count_unresolved`
        has re-created the second snapshot, whatever the marker looks like."""
        db = _make_db()
        count = AsyncMock(return_value=999)
        page = AsyncMock(return_value=(
            [{"content": f"o{i}", "source": "s", "priority": "low"} for i in range(15)],
            42,
        ))
        with (
            patch(_OBS, new=page),
            patch(_OBS_COUNT, new=count),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        count.assert_not_awaited()
        assert page.await_count == 1, "one snapshot means exactly one read"
        assert "15 NEWEST of 42" in content.active_observations[-1]

    @pytest.mark.asyncio
    async def test_the_marker_is_decided_by_the_totals_the_same_read_returned(
        self,
    ) -> None:
        """A total EQUAL to the page length is a complete set, even at the cap —
        and the old shape could not tell that apart from a stale count, because
        the two numbers came from different moments."""
        db = _make_db()
        with (
            patch(_OBS, new=AsyncMock(return_value=(
                [{"content": f"o{i}", "source": "s", "priority": "low"} for i in range(15)],
                15,
            ))),
            patch(_EVENTS, new=AsyncMock(return_value=[])),
            patch(_TICKS_LAST, new=AsyncMock(return_value=None)),
            patch(_TICKS_COUNT, new=AsyncMock(return_value=0)),
            patch(_CC_ACTIVE, new=AsyncMock(return_value=[])),
        ):
            content = await build_dynamic_briefing(db)
        assert len(content.active_observations) == 15
        assert not any("older ones exist" in line for line in content.active_observations)
