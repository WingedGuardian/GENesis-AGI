"""Tests for morning report generator."""

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.content.types import DraftResult, FormatTarget, FormattedContent
from genesis.db.schema import create_all_tables
from genesis.outreach import morning_report as _mr_mod
from genesis.outreach.morning_report import MorningReportGenerator
from genesis.outreach.types import OutreachCategory


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def mock_health():
    health = AsyncMock()
    health.snapshot.return_value = {
        "timestamp": "2026-03-12T07:00:00Z",
        "cost": {
            "daily_usd": 1.23,
            "monthly_usd": 15.0,
            "budget_status": "UNDER_LIMIT",
            "budget_monthly_limit": 30.0,
            "budget_pct_used": 50.0,
        },
        "cc_sessions": {"foreground": 0, "background": {"active": 0}},
        "queues": {"deferred_work": 0, "dead_letters": 0},
        "infrastructure": {
            "genesis.db": {"status": "ok", "latency_ms": 1.2},
            "qdrant": {"status": "ok", "latency_ms": 2.1},
            "disk": {"status": "ok", "free_gb": 50.0},
        },
        "surplus": {"status": "idle", "queue_depth": 2},
    }
    return health


@pytest.fixture
def mock_drafter():
    drafter = AsyncMock()
    drafter.draft.return_value = DraftResult(
        content=FormattedContent(
            text="Good morning. System healthy, $1.23 spent today.",
            target=FormatTarget.GENERIC,
            truncated=False,
            original_length=47,
        ),
        model_used="gemini-free",
        raw_draft="Good morning. System healthy, $1.23 spent today.",
    )
    return drafter


@pytest.mark.asyncio
async def test_generate_returns_outreach_request(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    req = await gen.generate()
    assert req.category == OutreachCategory.DIGEST
    assert req.signal_type == "morning_report"
    assert req.salience_score == 0.0
    assert "morning" in req.topic.lower() or "report" in req.topic.lower()


@pytest.mark.asyncio
async def test_generate_calls_health_snapshot(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    mock_health.snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_generate_calls_drafter(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    mock_drafter.draft.assert_called_once()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "morning" in call_args.topic.lower() or "report" in call_args.topic.lower()


@pytest.mark.asyncio
async def test_system_prompt_includes_next_steps_section(db, mock_health, mock_drafter):
    """The loaded MORNING_REPORT.md system prompt must instruct the LLM to produce
    a 'Next Steps & Blockers' section — so the report highlights what to do, not
    just status (the actionability gap the user flagged)."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert call_args.system_prompt is not None
    assert "Next Steps & Blockers" in call_args.system_prompt


@pytest.mark.asyncio
async def test_generate_includes_health_in_context(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    # Month-to-date spend (grounded against the cap) appears in the context.
    assert "15.00" in call_args.context


@pytest.mark.asyncio
async def test_format_health_cost_line_grounded(db, mock_health, mock_drafter):
    """Cost is ONE neutral grounded line: month-to-date spend against the cap,
    real numbers only — no projection, no daily figure, no spike alarm, no
    provider breakdown. Cost is observability, not control."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health({
        "cost": {
            "daily_usd": 0.14,
            "monthly_usd": 3.79,
            "budget_status": "UNDER_LIMIT",
            "budget_monthly_limit": 30.0,
            "budget_pct_used": 12.6,  # renders as "13%" via :.0f rounding (pins the format)
            "forecast_monthly_usd": 622.0,  # projection — must NOT appear
            "cost_by_provider": [{"provider": "x", "month_usd": 2.0}],
        },
        "queues": {}, "infrastructure": {}, "surplus": {},
        "awareness": {}, "cc_sessions": {},
    })
    assert "Spend: $3.79 MTD" in section
    assert "13% of $30 cap" in section  # 12.6% → "13%" (.0f); pins the rendered format
    assert "622" not in section            # no projection leaked
    assert "today" not in section.lower()  # MTD only — no daily figure
    assert "Top cost drivers" not in section


@pytest.mark.asyncio
async def test_observation_insights_demotes_aged(db, mock_health, mock_drafter):
    """A >3d-old observation is shown demoted and tagged [aged] so a stale write
    doesn't surface as a fresh critical alarm; a recent one is left as-is."""
    from datetime import UTC, datetime, timedelta

    fresh = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    old = (datetime.now(UTC) - timedelta(days=6)).isoformat()
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at) "
        "VALUES ('fresh', 'test', 'quality_drift', 'fresh critical thing', 'critical', ?)",
        (fresh,),
    )
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at) "
        "VALUES ('old', 'test', 'quality_drift', 'old critical thing', 'critical', ?)",
        (old,),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    out = await gen._get_observation_insights()
    assert out is not None
    aged_line = next(line for line in out.splitlines() if "old critical thing" in line)
    fresh_line = next(line for line in out.splitlines() if "fresh critical thing" in line)
    # 6-day-old critical: demoted to high and tagged.
    assert "[aged]" in aged_line
    assert "**high**" in aged_line
    # 2-hour-old critical: unchanged.
    assert "[aged]" not in fresh_line
    assert "**critical**" in fresh_line


@pytest.mark.asyncio
async def test_format_health_cost_line_without_budget(db, mock_health, mock_drafter):
    """When no budget cap is configured, fall back to a bare MTD spend line."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health({
        "cost": {"monthly_usd": 3.79, "budget_status": "unknown"},
        "queues": {}, "infrastructure": {}, "surplus": {},
        "awareness": {}, "cc_sessions": {},
    })
    assert "Spend: $3.79 MTD" in section
    assert "cap" not in section.lower()


@pytest.mark.asyncio
async def test_context_includes_session_topics(db, mock_health, mock_drafter):
    """Session topics from foreground sessions appear in the activity context."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, effort, status, "
        "started_at, last_activity_at, topic) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-2 hours'), datetime('now', '-1 hour'), ?)",
        ("s1", "foreground", "opus", "high", "completed",
         "Working on memory supersession chain"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "memory supersession" in call_args.context.lower()
    assert "Session topics" in call_args.context


@pytest.mark.asyncio
async def test_context_includes_user_goals(db, mock_health, mock_drafter):
    """Active user goals appear in the activity context for drift detection."""
    await db.execute(
        "INSERT INTO user_goals (id, title, category, priority, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-7 days'), datetime('now'))",
        ("g1", "W2 employment", "career", "high", "active"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "W2 employment" in call_args.context
    assert "Active user goals" in call_args.context


@pytest.mark.asyncio
async def test_follow_up_summary_includes_age(db):
    """User-world follow-ups render with their relative age (C2a).

    Uses a tz-aware ISO created_at to mirror real follow-ups (the crud writes
    ``datetime.now(UTC).isoformat()``); naive timestamps would degrade to
    "unknown age" via _relative_age's TypeError guard.
    """
    from datetime import UTC, datetime, timedelta

    created = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    await db.execute(
        "INSERT INTO follow_ups (id, source, content, strategy, status, "
        "priority, domain, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("fu1", "foreground_session", "Decide on the migration approach",
         "user_input_needed", "pending", "medium", "user_world", created),
    )
    await db.commit()

    gen = MorningReportGenerator.__new__(MorningReportGenerator)
    gen._db = db
    summary = await gen._get_follow_ups_summary()
    assert summary is not None
    assert "Decide on the migration approach" in summary
    assert "20d ago)" in summary


@pytest.mark.asyncio
async def test_background_sessions_excluded_from_topics(db, mock_health, mock_drafter):
    """Background sessions should NOT appear in session topics."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, effort, status, "
        "started_at, last_activity_at, topic) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-2 hours'), datetime('now', '-1 hour'), ?)",
        ("bg1", "background_reflection", "sonnet", "medium", "completed",
         "Internal reflection cycle"),
    )
    await db.commit()

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    call_args = mock_drafter.draft.call_args[0][0]
    assert "Internal reflection cycle" not in call_args.context


@pytest.mark.asyncio
async def test_event_bus_emits_on_section_failure(db, mock_health, mock_drafter):
    """When a section fails, event_bus.emit should be called with WARNING."""
    event_bus = AsyncMock()
    # Make cognitive state query fail
    broken_db = AsyncMock()
    broken_db.execute = AsyncMock(side_effect=RuntimeError("DB gone"))

    # Use real health so _assemble_context reaches the failing DB sections
    mock_health.snapshot.return_value = {
        "cost": {}, "queues": {}, "infrastructure": {}, "surplus": {},
    }

    gen = MorningReportGenerator(mock_health, broken_db, mock_drafter, event_bus=event_bus)
    await gen.generate()

    # Should have emitted warnings for cognitive_state, pending_items, engagement_summary
    assert event_bus.emit.call_count >= 3
    sections_warned = {
        call.kwargs.get("section") or call[1].get("section", "")
        for call in event_bus.emit.call_args_list
    }
    # Check at least some expected sections
    assert len(sections_warned) >= 1


@pytest.mark.asyncio
async def test_no_event_bus_still_works(db, mock_health, mock_drafter):
    """Without event_bus, failures should not crash."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter, event_bus=None)
    req = await gen.generate()
    assert req.category == OutreachCategory.DIGEST


async def _insert_grade(db, subsystem, grade, score, period_end="2026-06-22T00:00:00Z"):
    from genesis.db.crud import j9_eval

    await j9_eval.insert_subsystem_grade(
        db,
        period_start="2026-06-15T00:00:00Z",
        period_end=period_end,
        period_type="weekly",
        subsystem=subsystem,
        grade=grade,
        score=score,
        factors={"f": 1.0},
        sample_count=10,
    )


@pytest.mark.asyncio
async def test_eval_quality_section_surfaces_grades(db, mock_health, mock_drafter):
    """Graded subsystems are surfaced with grade + score, sorted by name."""
    await _insert_grade(db, "memory", "B", 82.0)
    await _insert_grade(db, "ego", "D", 64.0)

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    out = await gen._get_eval_quality_section()

    assert out is not None
    assert "- ego: D (64)" in out
    assert "- memory: B (82)" in out
    # ego sorts before memory
    assert out.index("ego:") < out.index("memory:")


@pytest.mark.asyncio
async def test_eval_quality_section_none_when_no_grades(db, mock_health, mock_drafter):
    """No grades at all → section skipped entirely (returns None)."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    assert await gen._get_eval_quality_section() is None


@pytest.mark.asyncio
async def test_eval_quality_section_omits_ungraded(db, mock_health, mock_drafter):
    """A None grade (cold-start / insufficient data) is omitted, never shown as
    a problem; if it's the only row, the section is skipped. (cognitive_drift is
    excluded at the schema level — the grades table CHECK-constrains subsystem to
    the 5 graded subsystems, so the dark drift dimension never reaches here.)"""
    await _insert_grade(db, "awareness", None, None)  # insufficient data → None
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    assert await gen._get_eval_quality_section() is None

    # With one graded + one ungraded, only the graded one shows.
    await _insert_grade(db, "memory", "A", 91.0)
    out = await gen._get_eval_quality_section()
    assert out is not None
    assert "memory: A (91)" in out
    assert "awareness" not in out


@pytest.mark.asyncio
async def test_eval_quality_section_appears_in_assembled_context(db, mock_health, mock_drafter):
    """Wiring proof (Level-3 data-flow): when grades exist, the section reaches
    the full assembled context that the LLM narrates."""
    await _insert_grade(db, "memory", "B", 82.0)
    gen = MorningReportGenerator(mock_health, db, mock_drafter)

    context = await gen._assemble_context()

    assert "## Cognitive Subsystem Grades" in context
    assert "memory: B (82)" in context


# ── Capability-build lane section ─────────────────────────────────────────


def test_summarize_ci_rollup():
    assert _mr_mod._summarize_ci_rollup([]) == "no checks"
    assert _mr_mod._summarize_ci_rollup(
        [{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "passing"
    assert _mr_mod._summarize_ci_rollup(
        [{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]) == "pending"
    assert _mr_mod._summarize_ci_rollup(
        [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"


def test_format_build_calibration():
    counts = [
        {"verdict": "build", "user_decision": "approved", "count": 2},
        {"verdict": "build", "user_decision": None, "count": 1},
        {"verdict": "dont_build", "user_decision": None, "count": 3},
    ]
    joined = "\n".join(_mr_mod._format_build_calibration(counts))
    assert "build verdicts: 2/2 approved, 1 pending" in joined
    assert "dont_build verdicts: 3 (uncontested" in joined


@pytest.mark.asyncio
async def test_build_lane_section_none_when_empty(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    assert await gen._get_build_lane_section() is None


@pytest.mark.asyncio
async def test_build_lane_section_renders(db, mock_health, mock_drafter, monkeypatch):
    from genesis.db.crud import build_candidates as bc

    await bc.create(db, id="c1", item_key="k1", item_title="Dad-joke skill",
                    source_file="New Genesis Capabilities.md", verdict="build")
    await bc.update(db, "c1", outcome="pr_opened",
                    pr_url="https://github.com/o/r/pull/42", branch="task/c1")
    await bc.record_user_decision(db, "c1", user_decision="approved")
    await bc.create(db, id="c2", item_key="k2", item_title="Rewrite the kernel",
                    source_file="New Genesis Capabilities.md",
                    verdict="dont_build", verdict_reason="brain-not-body scope")

    async def fake_ci(url):
        return "passing"

    monkeypatch.setattr(_mr_mod, "_pr_ci_status", fake_ci)

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = await gen._get_build_lane_section()
    assert section is not None
    assert "https://github.com/o/r/pull/42" in section
    assert "CI passing" in section
    assert "brain-not-body scope" in section
    assert "Calibration" in section


@pytest.mark.asyncio
async def test_pr_ci_status_parses_run_gh(monkeypatch):
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return '{"statusCheckRollup": [{"conclusion": "SUCCESS"}]}'

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)
    assert await _mr_mod._pr_ci_status("https://x/pull/1") == "passing"


@pytest.mark.asyncio
async def test_pr_ci_status_none_on_empty_or_no_url(monkeypatch):
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return ""  # run_gh collapses failure/timeout to ""

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)
    assert await _mr_mod._pr_ci_status("https://x/pull/1") is None
    assert await _mr_mod._pr_ci_status("") is None


# --- message_queue finding closure (confirm_delivery) ---


async def _insert_finding(db, id_: str, content: str = "inbox batch finding"):
    from genesis.db.crud import message_queue as mq_crud

    await mq_crud.create(
        db, id=id_, source="cc_background", target="cc_foreground",
        message_type="finding", content=content,
        created_at="2026-03-12T06:00:00+00:00", priority="low",
    )


async def _mq_row(db, id_: str):
    cursor = await db.execute(
        "SELECT response, responded_at FROM message_queue WHERE id = ?", (id_,)
    )
    return dict(await cursor.fetchone())


@pytest.mark.asyncio
async def test_mq_finding_closed_after_confirm_delivery(db, mock_health, mock_drafter):
    await _insert_finding(db, "f1")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    await gen.confirm_delivery()

    row = await _mq_row(db, "f1")
    assert row["response"] == "surfaced_in_morning_report"
    assert row["responded_at"] is not None
    assert gen._pending_mq_ids == []


@pytest.mark.asyncio
async def test_mq_finding_stays_pending_when_delivery_unconfirmed(
    db, mock_health, mock_drafter
):
    # Delivery failed → confirm_delivery never called → finding re-appears
    # in the next report instead of being silently closed.
    await _insert_finding(db, "f2")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()

    row = await _mq_row(db, "f2")
    assert row["responded_at"] is None
    assert gen._pending_mq_ids == ["f2"]


@pytest.mark.asyncio
async def test_untitled_mq_rows_not_rendered_and_not_closed(
    db, mock_health, mock_drafter
):
    # "Untitled" rows are filtered from the report, so closing them would
    # mark items responded that were never shown.
    await _insert_finding(db, "f3", content="Untitled batch artifact")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    await gen.confirm_delivery()

    row = await _mq_row(db, "f3")
    assert row["responded_at"] is None


@pytest.mark.asyncio
async def test_mq_ids_replaced_not_accumulated_across_generates(
    db, mock_health, mock_drafter
):
    await _insert_finding(db, "f4")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    await gen.generate()  # retry after failed delivery must not duplicate
    assert gen._pending_mq_ids == ["f4"]


@pytest.mark.asyncio
async def test_checkpoint_question_rows_never_closed(db, mock_health, mock_drafter):
    # question/decision/error rows are the checkpoint flow's to answer
    # (CheckpointManager.deliver_response); the report must not close them.
    from genesis.db.crud import message_queue as mq_crud

    await mq_crud.create(
        db, id="q1", source="cc_background", target="user",
        message_type="question", content="Need a decision on X",
        created_at="2026-03-12T06:00:00+00:00",
    )
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    await gen.confirm_delivery()

    row = await _mq_row(db, "q1")
    assert row["responded_at"] is None


@pytest.mark.asyncio
async def test_activity_summary_scopes_goals_by_origin(db, mock_health, mock_drafter):
    """PR-3a: 'Active user goals' excludes ego-owned goals; Genesis's own
    goals appear only as the compact count line (active + paused)."""
    from genesis.db.crud import user_goals

    await user_goals.create(
        db, title="User career goal", category="career", priority="high",
    )
    await user_goals.create(
        db, title="Ego ops goal", category="project", origin="genesis_ego",
    )
    paused = await user_goals.create(
        db, title="Paused ego goal", category="other", origin="genesis_ego",
    )
    await user_goals.update(db, paused, status="paused")

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    summary = await gen._get_activity_summary()

    assert "User career goal" in summary
    assert "Ego ops goal" not in summary
    assert "Genesis's own goals: 1 active, 1 paused" in summary


@pytest.mark.asyncio
async def test_activity_summary_no_ego_goal_line_when_none(db, mock_health, mock_drafter):
    """No ego goals → no count line (the section stays user-pure)."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    summary = await gen._get_activity_summary()
    assert "Genesis's own goals" not in summary


def test_goal_autonomous_action_is_user_visible():
    """Lock: autonomous goal actions surface through the generic morning-
    report observation pipeline — adding the type to INTERNAL_OBS_TYPES
    would silently remove the user's visibility into ego autonomy."""
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES

    assert "goal_autonomous_action" not in INTERNAL_OBS_TYPES
    assert "goal_recommendation" not in INTERNAL_OBS_TYPES
