"""Tests for morning report generator."""

from unittest.mock import AsyncMock, patch

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
async def test_topic_is_one_report_per_utc_day(db, mock_health, mock_drafter):
    """Governance treats the morning-report topic as the message identity
    (governance._TOPIC_IS_IDENTITY): one topic per UTC date, so a same-day
    resend collapses and the next day's report never does. Pin the format."""
    import re
    from datetime import UTC, datetime

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    before = datetime.now(UTC).strftime("%Y-%m-%d")
    req = await gen.generate()
    after = datetime.now(UTC).strftime("%Y-%m-%d")
    assert re.fullmatch(r"Morning Report — \d{4}-\d{2}-\d{2}", req.topic)
    assert req.topic[-10:] in {before, after}  # robust across a UTC midnight


@pytest.mark.asyncio
async def test_critical_issues_dedupes_subsystem_stale(db, mock_health, mock_drafter):
    """A subsystem already surfaced as a subsystem_stale:<name> alert is NOT listed
    a second time by the heartbeat-staleness block (P2-6 dedup). A subsystem with a
    heartbeat but NO alert (surplus) is still surfaced once."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    alerts = [
        {
            "id": "subsystem_stale:inbox",
            "severity": "WARNING",
            "message": "Subsystem 'inbox' heartbeat overdue — no pulse in 9000s",
        },
    ]
    heartbeats = {
        "inbox": {"status": "overdue", "age_seconds": 9000, "last_seen": "x"},
        "surplus": {"status": "overdue", "age_seconds": 5000, "last_seen": "y"},
    }
    with (
        patch(
            "genesis.mcp.health_mcp._impl_health_alerts",
            new=AsyncMock(return_value=alerts),
        ),
        patch(
            "genesis.mcp.health.manifest._impl_subsystem_heartbeats",
            new=AsyncMock(return_value=heartbeats),
        ),
    ):
        text = await gen._get_critical_issues()

    assert text is not None
    # inbox appears exactly once — via its alert line, NOT duplicated by the hb block.
    assert text.count("'inbox'") == 1
    assert "subsystem_stale:inbox" in text
    # surplus has a heartbeat but no alert → still surfaced once by the hb block.
    assert "'surplus'" in text


@pytest.mark.asyncio
async def test_critical_issues_surfaces_and_dedupes_subsystem_never_started(
    db, mock_health, mock_drafter
):
    """A subsystem_never_started:<name> alert (the new never-started liveness id) is
    surfaced by the morning report (it passes the WARNING+ filter, it is not a
    call_site: alert) and its name is captured into the dedup set so the
    heartbeat-staleness block never lists it a SECOND time. A subsystem with a plain
    overdue heartbeat but no alert is still surfaced once by the hb block."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    alerts = [
        {
            "id": "subsystem_never_started:inbox",
            "severity": "WARNING",
            "message": "Subsystem 'inbox' never started — enabled but no heartbeat since boot",
        },
    ]
    # inbox's heartbeat verdict is "never_started" (mutually exclusive with overdue —
    # never_started requires zero pulses). The hb block only renders "overdue", so the
    # dedup capture is defensive here; assert inbox is NOT double-listed regardless.
    heartbeats = {
        "inbox": {"status": "never_started", "last_seen": None, "reason": "init-failed"},
        "surplus": {"status": "overdue", "age_seconds": 5000, "last_seen": "y"},
    }
    with (
        patch(
            "genesis.mcp.health_mcp._impl_health_alerts",
            new=AsyncMock(return_value=alerts),
        ),
        patch(
            "genesis.mcp.health.manifest._impl_subsystem_heartbeats",
            new=AsyncMock(return_value=heartbeats),
        ),
    ):
        text = await gen._get_critical_issues()

    assert text is not None
    # inbox is surfaced exactly once — via its never_started alert line.
    assert text.count("'inbox'") == 1
    assert "subsystem_never_started:inbox" in text
    # surplus has a heartbeat but no alert → still surfaced once by the hb block.
    assert "'surplus'" in text


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
    section = gen._format_health(
        {
            "cost": {
                "daily_usd": 0.14,
                "monthly_usd": 3.79,
                "budget_status": "UNDER_LIMIT",
                "budget_monthly_limit": 30.0,
                "budget_pct_used": 12.6,  # renders as "13%" via :.0f rounding (pins the format)
                "forecast_monthly_usd": 622.0,  # projection — must NOT appear
                "cost_by_provider": [{"provider": "x", "month_usd": 2.0}],
            },
            "queues": {},
            "infrastructure": {},
            "surplus": {},
            "awareness": {},
            "cc_sessions": {},
        }
    )
    assert "Spend: $3.79 MTD" in section
    assert "13% of $30 cap" in section  # 12.6% → "13%" (.0f); pins the rendered format
    assert "622" not in section  # no projection leaked
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
    section = gen._format_health(
        {
            "cost": {"monthly_usd": 3.79, "budget_status": "unknown"},
            "queues": {},
            "infrastructure": {},
            "surplus": {},
            "awareness": {},
            "cc_sessions": {},
        }
    )
    assert "Spend: $3.79 MTD" in section
    assert "cap" not in section.lower()


@pytest.mark.asyncio
async def test_context_includes_session_topics(db, mock_health, mock_drafter):
    """Session topics from foreground sessions appear in the activity context."""
    await db.execute(
        "INSERT INTO cc_sessions (id, session_type, model, effort, status, "
        "started_at, last_activity_at, topic) VALUES (?, ?, ?, ?, ?, "
        "datetime('now', '-2 hours'), datetime('now', '-1 hour'), ?)",
        ("s1", "foreground", "opus", "high", "completed", "Working on memory supersession chain"),
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
        (
            "fu1",
            "foreground_session",
            "Decide on the migration approach",
            "user_input_needed",
            "pending",
            "medium",
            "user_world",
            created,
        ),
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
        (
            "bg1",
            "background_reflection",
            "sonnet",
            "medium",
            "completed",
            "Internal reflection cycle",
        ),
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
        "cost": {},
        "queues": {},
        "infrastructure": {},
        "surplus": {},
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
    assert (
        _mr_mod._summarize_ci_rollup([{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}]) == "passing"
    )
    assert (
        _mr_mod._summarize_ci_rollup([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}])
        == "pending"
    )
    assert (
        _mr_mod._summarize_ci_rollup([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}])
        == "failing"
    )


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

    await bc.create(
        db,
        id="c1",
        item_key="k1",
        item_title="Dad-joke skill",
        source_file="New Genesis Capabilities.md",
        verdict="build",
    )
    await bc.update(
        db, "c1", outcome="pr_opened", pr_url="https://github.com/o/r/pull/42", branch="task/c1"
    )
    await bc.record_user_decision(db, "c1", user_decision="approved")
    await bc.create(
        db,
        id="c2",
        item_key="k2",
        item_title="Rewrite the kernel",
        source_file="New Genesis Capabilities.md",
        verdict="dont_build",
        verdict_reason="brain-not-body scope",
    )

    async def fake_ci(url, *, actions_degraded=None):
        return "passing"

    monkeypatch.setattr(_mr_mod, "_pr_ci_status", fake_ci)

    async def _not_degraded():
        return False

    # _get_build_lane_section resolves the incident state once before the gather;
    # stub it so the test makes no real githubstatus network call.
    monkeypatch.setattr(_mr_mod, "_github_actions_degraded", _not_degraded)

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


# --- GitHub Actions outage pre-flight (distinguish infra outage from real failure) ---


class _FakeStatusResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeStatusClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    def __init__(self, *, payload=None, exc=None):
        self._payload = payload
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        if self._exc is not None:
            raise self._exc
        return _FakeStatusResp(self._payload)


def _components(*entries):
    return {"components": [dict(e) for e in entries]}


@pytest.mark.asyncio
async def test_github_actions_degraded_component_scoped(monkeypatch):
    # Operational Actions → not degraded.
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(
            payload=_components({"name": "Actions", "status": "operational"}),
        ),
    )
    assert await _mr_mod._github_actions_degraded() is False

    # Degraded Actions → degraded.
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(
            payload=_components({"name": "Actions", "status": "major_outage"}),
        ),
    )
    assert await _mr_mod._github_actions_degraded() is True

    # Scheduled maintenance stalls CI for infra reasons too (Codex P2).
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(
            payload=_components({"name": "Actions", "status": "under_maintenance"}),
        ),
    )
    assert await _mr_mod._github_actions_degraded() is True

    # An UNRELATED component's outage must NOT trip the Actions preflight
    # (the aggregate-indicator approach did — Codex P2).
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(
            payload=_components(
                {"name": "Packages", "status": "major_outage"},
                {"name": "Actions", "status": "operational"},
            ),
        ),
    )
    assert await _mr_mod._github_actions_degraded() is False


@pytest.mark.asyncio
async def test_github_actions_degraded_fails_open(monkeypatch):
    # Unreachable / malformed status page must NOT report degraded — never mask
    # a real CI failure as an infra outage.
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(exc=RuntimeError("boom")),
    )
    assert await _mr_mod._github_actions_degraded() is False

    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(payload={}),  # no 'components' key
    )
    assert await _mr_mod._github_actions_degraded() is False

    # Schema-invalid statuses (bool/int/object/unknown string) are NOT outages —
    # the allowlist rejects them (Codex P2: `true`/`1` must not read as degraded).
    for bad in (True, 1, {"level": "major"}, "weird_new_state", None):
        monkeypatch.setattr(
            _mr_mod.httpx,
            "AsyncClient",
            lambda _bad=bad, **kw: _FakeStatusClient(
                payload=_components({"name": "Actions", "status": _bad}),
            ),
        )
        assert await _mr_mod._github_actions_degraded() is False, bad

    # Actions component absent entirely → cannot confirm → fail open.
    monkeypatch.setattr(
        _mr_mod.httpx,
        "AsyncClient",
        lambda **kw: _FakeStatusClient(
            payload=_components({"name": "Packages", "status": "operational"}),
        ),
    )
    assert await _mr_mod._github_actions_degraded() is False


@pytest.mark.asyncio
async def test_pr_ci_status_annotates_on_actions_incident(monkeypatch):
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return '{"statusCheckRollup": [{"conclusion": "FAILURE"}]}'

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)

    async def _degraded():
        return True

    monkeypatch.setattr(_mr_mod, "_github_actions_degraded", _degraded)
    out = await _mr_mod._pr_ci_status("https://x/pull/1")
    assert out is not None and out.startswith("failing")
    assert "GitHub Actions" in out  # incident annotation present


@pytest.mark.asyncio
async def test_pr_ci_status_no_annotation_when_not_degraded(monkeypatch):
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return '{"statusCheckRollup": [{"conclusion": "FAILURE"}]}'

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)

    async def _not_degraded():
        return False

    monkeypatch.setattr(_mr_mod, "_github_actions_degraded", _not_degraded)
    assert await _mr_mod._pr_ci_status("https://x/pull/1") == "failing"


@pytest.mark.asyncio
async def test_pr_ci_status_annotates_pending_branch(monkeypatch):
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return '{"statusCheckRollup": [{"status": "IN_PROGRESS"}]}'

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)

    async def _degraded():
        return True

    monkeypatch.setattr(_mr_mod, "_github_actions_degraded", _degraded)
    out = await _mr_mod._pr_ci_status("https://x/pull/1")
    assert out is not None and out.startswith("pending")
    assert "GitHub Actions" in out


@pytest.mark.asyncio
async def test_pr_ci_status_honors_precomputed_degraded(monkeypatch):
    # When actions_degraded is supplied by the caller, the per-call status check
    # must NOT run (resolved once per report, not per PR).
    import genesis.recon.gh_cli as gh_cli

    async def fake_run_gh(*args, timeout=None):
        return '{"statusCheckRollup": [{"conclusion": "FAILURE"}]}'

    monkeypatch.setattr(gh_cli, "run_gh", fake_run_gh)

    async def _boom():
        raise AssertionError("must not resolve per-call when precomputed")

    monkeypatch.setattr(_mr_mod, "_github_actions_degraded", _boom)

    out = await _mr_mod._pr_ci_status("https://x/pull/1", actions_degraded=True)
    assert out is not None and out.startswith("failing") and "GitHub Actions" in out
    assert await _mr_mod._pr_ci_status("https://x/pull/1", actions_degraded=False) == "failing"


# --- message_queue finding closure (confirm_delivery) ---


async def _insert_finding(db, id_: str, content: str = "inbox batch finding"):
    from genesis.db.crud import message_queue as mq_crud

    await mq_crud.create(
        db,
        id=id_,
        source="cc_background",
        target="cc_foreground",
        message_type="finding",
        content=content,
        created_at="2026-03-12T06:00:00+00:00",
        priority="low",
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
async def test_mq_finding_stays_pending_when_delivery_unconfirmed(db, mock_health, mock_drafter):
    # Delivery failed → confirm_delivery never called → finding re-appears
    # in the next report instead of being silently closed.
    await _insert_finding(db, "f2")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()

    row = await _mq_row(db, "f2")
    assert row["responded_at"] is None
    assert gen._pending_mq_ids == ["f2"]


@pytest.mark.asyncio
async def test_untitled_mq_rows_not_rendered_and_not_closed(db, mock_health, mock_drafter):
    # "Untitled" rows are filtered from the report, so closing them would
    # mark items responded that were never shown.
    await _insert_finding(db, "f3", content="Untitled batch artifact")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    await gen.generate()
    await gen.confirm_delivery()

    row = await _mq_row(db, "f3")
    assert row["responded_at"] is None


@pytest.mark.asyncio
async def test_mq_ids_replaced_not_accumulated_across_generates(db, mock_health, mock_drafter):
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
        db,
        id="q1",
        source="cc_background",
        target="user",
        message_type="question",
        content="Need a decision on X",
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
        db,
        title="User career goal",
        category="career",
        priority="high",
    )
    await user_goals.create(
        db,
        title="Ego ops goal",
        category="project",
        origin="genesis_ego",
    )
    paused = await user_goals.create(
        db,
        title="Paused ego goal",
        category="other",
        origin="genesis_ego",
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


# ── Grounding (PR morning-report-grounding) ────────────────────────────────


async def test_report_request_is_verbatim(db, mock_health, mock_drafter):
    """The grounded draft IS the report — the pipeline must not re-draft it
    through the generic drafter (which runs with system_prompt=None)."""
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    request = await gen.generate()
    assert request.verbatim is True


async def test_ground_truth_totals_precede_truncated_lists(db, mock_health, mock_drafter):
    """Authoritative totals render from un-truncated queries; the pending
    list shows 'showing 5 of N' rather than presenting the slice as the
    count."""
    from genesis.db.crud import ego as ego_crud

    for i in range(7):
        await ego_crud.create_proposal(
            db,
            id=f"gt-{i}",
            action_type="t",
            content=f"proposal {i}",
        )
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    context = await gen._assemble_context()

    assert "## Ground Truth" in context
    assert "- Pending ego proposals: 7" in context
    assert "7 pending ego proposal(s)" in context
    assert "(showing 5 of 7)" in context
    # Ground truth section renders before any list section
    assert context.index("## Ground Truth") < context.index("## Pending Items")


async def test_ground_truth_follow_up_counts(db, mock_health, mock_drafter):
    from genesis.db.crud import follow_ups

    for i in range(8):
        await follow_ups.create(
            db,
            id=f"fu-{i}",
            content=f"item {i}",
            source="test",
            strategy="user_input_needed" if i < 3 else "ego_judgment",
            domain="user_world",
        )
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    context = await gen._assemble_context()
    assert "3 awaiting your input, 8 pending total" in context


async def test_protective_fact_tagged(db, mock_health, mock_drafter):
    """A protective mechanism observation carries the [protective: …] tag so
    the drafter cannot invert it into an active risk."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from genesis.db.crud import observations

    await observations.create(
        db,
        id=str(_uuid.uuid4()),
        source="awareness",
        type="infra_posture",
        content="systemd-oomd is active and monitoring memory pressure",
        priority="high",
        created_at=datetime.now(UTC).isoformat(),
    )
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    context = await gen._assemble_context()
    assert "[protective:" in context


async def test_prompt_contract_grounding_rules():
    """MORNING_REPORT.md carries the counts-are-totals + polarity rules."""
    from genesis.outreach.morning_report import MorningReportGenerator as G

    prompt = G._load_system_prompt()
    assert prompt is not None
    assert "Ground Truth" in prompt
    assert "Never derive a total from a list section" in prompt
    assert "Protective facts are never risks" in prompt


async def test_negated_protection_never_tagged_protective(db, mock_health, mock_drafter):
    """The REAL shipping alert class: 'systemd-oomd … is not enforced' is a
    live risk. Tagging it protective would invert it and — via the prompt
    rule — suppress it. Neither negated content nor posture-monitor alerts
    may ever be tagged."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from genesis.db.crud import observations

    now = datetime.now(UTC).isoformat()
    await observations.create(
        db,
        id=str(_uuid.uuid4()),
        source="infra_protection_posture_monitor",
        type="infrastructure_alert",
        content=(
            "systemd-oomd pressure-kill is not enforced for user.slice — "
            "nothing gracefully kills the memory hog before a hard OOM wedge"
        ),
        priority="high",
        created_at=now,
    )
    # Negated phrasing from any other source must also stay untagged.
    await observations.create(
        db,
        id=str(_uuid.uuid4()),
        source="awareness",
        type="infra_posture",
        content="zram is disabled after the unmask sweep",
        priority="high",
        created_at=now,
    )
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    context = await gen._assemble_context()
    assert "[protective:" not in context
    # ...and the alerts themselves still surface
    assert "not enforced" in context
    assert "zram is disabled" in context


async def test_ground_truth_stranded_work_carries_its_denominator_and_freshness(
    db, mock_health, mock_drafter, monkeypatch, tmp_path
):
    """The stranded-work line renders `open of tracked` AND the detector's age.

    Both halves are load-bearing and neither is decoration. Without the
    denominator the number cannot be sized; without the freshness verdict a
    zero from a detector that has never run reads exactly like a clean board —
    which `read_last_run`'s own reader contract says it must never do.
    """
    from genesis.db.crud import zero_drop as zd

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    present = [
        {"branch": "feat/a", "tip_sha": "a" * 40, "ahead_count": 2, "worktree_path": None},
        {"branch": "feat/b", "tip_sha": "b" * 40, "ahead_count": 1, "worktree_path": None},
    ]
    await zd.apply_sweep(db, class_="unpushed_branch", present=present, run_id="r1")
    await zd.ack(db, class_="unpushed_branch", branch="feat/b", reason="kept on purpose")

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    context = await gen._assemble_context()

    assert "Stranded work (zero-drop): 1 open of 2 tracked (1 acked)" in context
    # The detector has not run in this temp home, so the line must SAY the zero
    # is unverified rather than presenting the board as clean.
    assert "detector NEVER RUN" in context


async def test_ground_truth_stranded_work_line_emits_NO_branch_names(
    db, mock_health, mock_drafter, monkeypatch, tmp_path
):
    """Counts only — never untrusted repository text.

    Every other line in this section emits integers, and that is what keeps the
    whole section clear of the redaction concern that keeps `ledger_escalation`
    out of this report entirely. A branch name is attacker-influenced text on a
    path that reaches Telegram; a count cannot leak.
    """
    from genesis.db.crud import zero_drop as zd

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    await zd.apply_sweep(
        db,
        class_="unpushed_branch",
        present=[
            {
                "branch": "feat/SECRET-BRANCH-NAME",
                "tip_sha": "c" * 40,
                "ahead_count": 1,
                "worktree_path": "/home/someone/private/path",
            }
        ],
        run_id="r1",
    )

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    ground_truth = await gen._ground_truth_section()

    assert "Stranded work (zero-drop)" in ground_truth
    assert "SECRET-BRANCH-NAME" not in ground_truth
    assert "private/path" not in ground_truth


async def test_ground_truth_counts_open_questions_and_never_renders_their_text(
    db, mock_health, mock_drafter
):
    """The push surface for parked owner decisions: a count and the oldest age,
    never the question text (free prose a session wrote)."""
    from genesis.db.crud import board

    await db.commit()  # create_all_tables leaves a transaction open; a raise owns its own
    await board.raise_question(
        db, question="PRIVATE-QUESTION-TEXT about something", now="2026-09-01T00:00:00+00:00"
    )
    closed = await board.raise_question(db, question="closed one", now="2026-09-02T00:00:00+00:00")
    await board.close_question(
        db, question_id=closed, status="resolved", resolution="r", now="2026-09-03T00:00:00+00:00"
    )

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    ground_truth = await gen._ground_truth_section()

    assert "- Open questions awaiting your decision: 1 (oldest " in ground_truth
    assert "PRIVATE-QUESTION-TEXT" not in ground_truth


async def test_ground_truth_open_questions_zero_and_unmigrated(
    db, mock_health, mock_drafter, tmp_path
):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    assert (
        "- Open questions awaiting your decision: 0\n" in (await gen._ground_truth_section()) + "\n"
    )

    async with aiosqlite.connect(str(tmp_path / "bare.db")) as bare:
        gen_bare = MorningReportGenerator(mock_health, bare, mock_drafter)
        section = await gen_bare._ground_truth_section()
    assert "- Open questions awaiting your decision: unavailable (store not migrated)" in section


async def test_ground_truth_open_questions_read_failure_is_said_not_silent(
    db, mock_health, mock_drafter, monkeypatch
):
    """A failed read must not look like zero questions: this line is the only
    push surface for parked owner decisions."""
    from genesis.db.crud import board

    async def boom(_db):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(board, "question_summary", boom)
    section = await MorningReportGenerator(mock_health, db, mock_drafter)._ground_truth_section()
    assert "- Open questions awaiting your decision: unavailable (read failed)" in section


async def test_the_stranded_line_SURVIVES_the_whole_store_part_failing(
    db, mock_health, mock_drafter, monkeypatch, tmp_path
):
    """The one line in this section whose own comment forbids silence.

    `items_by_store` is itself guarded, so when that part fails it IS
    `{"status": "unavailable", ...}` and carries no `stranded_work` child at
    all. The old form read `view["items_by_store"].get("stranded_work", {})`,
    got `{}`, found `.get("status")` was None rather than UNAVAILABLE, took the
    happy branch, raised KeyError on `stranded["open"]`, and had it swallowed
    by the section's outer handler. The line VANISHED — in precisely the state
    it exists to announce, and a reader who sees no stranded-work line
    concludes there is no stranded work.

    The guard existed one level up and not one level down. Testing for OK
    rather than for UNAVAILABLE is what makes the check total: it also catches
    STALE and DEGRADED, which the old comparison waved through into a
    real-looking count beside "freshness unknown".
    """
    from genesis.session_awareness import zero_drop_view as V

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    async def _store_is_down(*a, **kw):
        raise RuntimeError("container down")

    monkeypatch.setattr(V, "_items_by_store", _store_is_down)

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    ground_truth = await gen._ground_truth_section()

    assert "Stranded work (zero-drop)" in ground_truth, (
        "the line must be present even when the store cannot be read — its "
        "absence is indistinguishable from a clean board"
    )
    assert "UNAVAILABLE" in ground_truth
    assert "this is not a zero, it is an unread board" in ground_truth


async def test_a_BLIND_detector_is_not_reported_as_a_fresh_count(
    db, mock_health, mock_drafter, monkeypatch, tmp_path
):
    """Fresh and COMPLETE are different claims, and only one was checked.

    A sweep whose GitHub leg failed is recent, its gaps part status is `ok`,
    and its freshness verdict reads `fresh` — so the predicate accepted it and
    the line printed a numeric count followed by "detector fresh" while a whole
    class was frozen and uncounted. The reader is told the board is current and
    not told it is partial.
    """
    from genesis.session_awareness import zero_drop_view as V

    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "home"))

    real = V._gaps

    async def _blind_gaps(conn, *, now, findings_limit):
        out = await real(conn, now=now, findings_limit=findings_limit)
        out["detector"] = {
            **(out.get("detector") or {}),
            "blind": True,
            "stale": False,
            "verdict": "fresh",
            "degraded": {"branches": "ls-remote failed"},
        }
        return out

    monkeypatch.setattr(V, "_gaps", _blind_gaps)

    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    ground_truth = await gen._ground_truth_section()

    assert "Stranded work (zero-drop)" in ground_truth, "the line must still appear"
    assert "detector fresh" not in ground_truth, (
        "a blind detector must not be reported as a fresh count"
    )
    assert "UNAVAILABLE" in ground_truth and "blind" in ground_truth, (
        f"and must name the fault rather than a number: {ground_truth}"
    )


# ── Live facts (issue: report stated stale/mixed facts as current) ─────────


def _health_with(infrastructure=None, queues=None):
    return {
        "cost": {},
        "queues": queues or {},
        "infrastructure": infrastructure or {},
        "surplus": {},
        "awareness": {},
        "cc_sessions": {},
    }


@pytest.mark.asyncio
async def test_format_health_shows_live_pid_budget(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            infrastructure={
                "pids": {
                    "status": "healthy",
                    "current": 980,
                    "max": 2400,
                    "pct": 40.8,
                    "scope": "user-1000.slice",
                    "path": "/sys/fs/cgroup/x",
                }
            }
        )
    )
    assert "PID budget: 41% (980/2400, user-1000.slice)" in section


@pytest.mark.asyncio
async def test_format_health_pid_budget_uncapped(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            infrastructure={
                "pids": {"status": "healthy", "current": 1500, "max": "max", "pct": None}
            }
        )
    )
    pid_line = next(line for line in section.splitlines() if "PID budget" in line)
    assert "1500" in pid_line
    assert "unavailable" not in pid_line
    # The container root can still be capped by the host above our view.
    assert "uncapped" not in pid_line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pids",
    [
        {"status": "error", "error": "boom"},
        {"status": "unavailable"},
        None,  # metric missing entirely
        {"status": "healthy", "current": 5, "max": 10},  # pct missing: malformed
    ],
)
async def test_format_health_pid_budget_unavailable_never_a_number(
    db, mock_health, mock_drafter, pids
):
    infra = {} if pids is None else {"pids": pids}
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(_health_with(infrastructure=infra))
    pid_line = next(line for line in section.splitlines() if "PID budget" in line)
    assert pid_line == "- PID budget: unavailable"


@pytest.mark.asyncio
async def test_format_health_splits_deferred_recovery_from_worklist(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            queues={
                "deferred_work": 131,
                "deferred_recovery": 31,
                "deferred_worklist": 100,
                "dead_letters": 0,
            }
        )
    )
    assert "deferred=131" not in section  # the mixed total is not the headline
    assert "recovery backlog=31" in section
    assert "scheduled worklist=100" in section
    assert "not a backlog" in section


@pytest.mark.asyncio
async def test_format_health_deferred_split_names_stalled_overlap(db, mock_health, mock_drafter):
    # A worklist item stalled past a full drain cycle counts in BOTH subsets;
    # the line must say so, from the DIRECT stalled count, never from
    # recovery + worklist - total (three separate queries can disagree).
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            queues={
                "deferred_work": 125,  # R + W - total would say 5
                "deferred_recovery": 30,
                "deferred_worklist": 100,
                "deferred_worklist_stalled": 10,
            }
        )
    )
    assert "recovery backlog=30" in section
    assert "10 stalled worklist" in section
    assert "5 stalled" not in section


@pytest.mark.asyncio
async def test_format_health_deferred_no_stalled_clause_without_direct_count(
    db, mock_health, mock_drafter
):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            queues={"deferred_work": 120, "deferred_recovery": 30, "deferred_worklist": 100}
        )
    )
    assert "recovery backlog=30" in section
    assert "stalled" not in section


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "queues",
    [
        {"status": "error", "error": "boom"},  # the whole queues section failed
        {},  # deferred_work absent
        # the deferred query failed: the producer leaves every split None
        {
            "deferred_work": None,
            "deferred_recovery": None,
            "deferred_worklist": None,
            "deferred_worklist_stalled": None,
        },
        {"deferred_work": 120, "deferred_recovery": 30, "deferred_worklist": None},
        {"deferred_work": 120, "deferred_recovery": None, "deferred_worklist": 90},
    ],
)
async def test_format_health_deferred_unavailable_never_a_zero(
    db, mock_health, mock_drafter, queues
):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(_health_with(queues=queues))
    line = next(x for x in section.splitlines() if "Deferred work" in x)
    assert line == "- Deferred work: unavailable"


@pytest.mark.asyncio
async def test_format_health_deferred_query_failed_says_unavailable(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            queues={"deferred_work": None, "deferred_recovery": None, "deferred_worklist": 0}
        )
    )
    assert "Deferred work: unavailable" in section


async def _insert_obs(db, oid, otype, content, priority="high", surfaced_count=0):
    from datetime import UTC, datetime

    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, created_at, "
        "surfaced_count) VALUES (?, 'test', ?, ?, ?, ?, ?)",
        (oid, otype, content, priority, datetime.now(UTC).isoformat(), surfaced_count),
    )
    await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_priority", ["low", "high"])
@pytest.mark.parametrize(
    "otype",
    [
        "gap_clustering",
        "wing_audit",
        "self_unblock",
        "memory_audit",
        "procedure_audit",
        "prompt_effectiveness_review",
        "quarantined_reflection",
    ],
)
async def test_reflection_audit_rows_absent_from_report_sections(
    db, mock_health, mock_drafter, otype, audit_priority
):
    # Real self-audit rows are written at "low" (surplus/intake.py); "low" is the
    # case the activity list renders today. "high" covers the other sections.
    await _insert_obs(
        db, "audit-row", otype, "AUDIT-ROW-MARKER drift text", priority=audit_priority
    )
    await _insert_obs(
        db,
        "audit-standing",
        otype,
        "AUDIT-STANDING-MARKER",
        priority=audit_priority,
        surfaced_count=5,
    )
    await _insert_obs(db, "real-row", "bug_identified", "REAL-ROW-MARKER")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)

    activity = await gen._get_activity_summary()
    assert "AUDIT-ROW-MARKER" not in activity
    assert "REAL-ROW-MARKER" in activity
    # Still accounted for, as internal: never silently vanished.
    assert "Genesis internal: 2 items" in activity

    insights = await gen._get_observation_insights()
    assert insights is not None
    assert "AUDIT-ROW-MARKER" not in insights
    assert "REAL-ROW-MARKER" in insights
    assert "audit-row" not in gen._pending_surface_ids

    standing = await gen._get_standing_items()
    assert standing is None or "AUDIT-STANDING-MARKER" not in standing

    context = await gen._assemble_context()
    assert "AUDIT-ROW-MARKER" not in context
    assert "AUDIT-STANDING-MARKER" not in context


@pytest.mark.asyncio
async def test_contradiction_rows_stay_in_report(db, mock_health, mock_drafter):
    # Contradictions are written for human judgment; the report must still show them.
    await _insert_obs(db, "contra-row", "contradiction", "CONTRA-ROW-MARKER")
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    insights = await gen._get_observation_insights()
    assert insights is not None
    assert "CONTRA-ROW-MARKER" in insights


def test_report_internal_set_does_not_widen_the_shared_set():
    # The report-local exclusion must not leak into INTERNAL_OBS_TYPES, which
    # the dashboard observations panel also reads (gap_clustering stays visible
    # there on purpose).
    from genesis.db.crud.observations import INTERNAL_OBS_TYPES

    assert "gap_clustering" not in INTERNAL_OBS_TYPES
    assert "contradiction" not in INTERNAL_OBS_TYPES
    assert set(_mr_mod._REPORT_INTERNAL_OBS_TYPES) >= INTERNAL_OBS_TYPES


@pytest.mark.asyncio
async def test_failed_stalled_count_keeps_the_deferred_figures(db, mock_health, mock_drafter):
    # Only the stalled count failed (its own error): the three good figures
    # still render, without a stalled clause. The report keys on None only,
    # never on error strings.
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        _health_with(
            queues={
                "deferred_work": 120,
                "deferred_recovery": 30,
                "deferred_worklist": 100,
                "deferred_worklist_stalled": None,
                "errors": ["deferred_worklist_stalled: query failed"],
            }
        )
    )
    line = next(x for x in section.splitlines() if "Deferred work" in x)
    assert "recovery backlog=30" in line
    assert "scheduled worklist=100" in line
    assert "stalled" not in line


def _health_line(section: str, label: str) -> str:
    return next(x for x in section.splitlines() if x.startswith(f"- {label}"))


_ERROR_SECTION = {"status": "error", "error": "boom"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("health", "label", "figures"),
    [
        # Every figure _format_health prints from a snapshot section.
        (
            {"queues": _ERROR_SECTION},
            "Queues",
            ["dead_letters=unavailable", "pending_embeddings=unavailable"],
        ),
        (
            {"queues": {"dead_letters": None, "pending_embeddings": None}},
            "Queues",
            ["dead_letters=unavailable", "pending_embeddings=unavailable"],
        ),
        ({"surplus": _ERROR_SECTION}, "Surplus", ["queue_depth=unavailable"]),
        ({"surplus": {"status": "idle"}}, "Surplus", ["queue_depth=unavailable"]),
        ({"awareness": _ERROR_SECTION}, "Awareness", ["ticks_24h=unavailable"]),
        ({"awareness": {"status": "unknown"}}, "Awareness", ["ticks_24h=unavailable"]),
        (
            {"cc_sessions": _ERROR_SECTION},
            "CC Sessions",
            ["foreground=unavailable", "background=unavailable", "failed_24h=unavailable"],
        ),
        (
            # The cc_sessions snapshot's own no-DB shape: statuses, no counts.
            {
                "cc_sessions": {
                    "foreground": {"status": "unknown"},
                    "background": {"status": "unknown"},
                }
            },
            "CC Sessions",
            ["foreground=unavailable", "background=unavailable", "failed_24h=unavailable"],
        ),
    ],
)
async def test_health_figures_never_fabricate_a_zero_or_none(
    db, mock_health, mock_drafter, health, label, figures
):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health({**_health_with(), **health})
    line = _health_line(section, label)
    for fig in figures:
        assert fig in line
    assert "None" not in section
    assert "=0" not in line


@pytest.mark.asyncio
async def test_health_figures_render_real_values(db, mock_health, mock_drafter):
    gen = MorningReportGenerator(mock_health, db, mock_drafter)
    section = gen._format_health(
        {
            **_health_with(
                queues={
                    "dead_letters": 0,
                    "pending_embeddings": 150,
                    "deferred_work": 0,
                    "deferred_recovery": 0,
                    "deferred_worklist": 0,
                }
            ),
            "surplus": {"status": "idle", "queue_depth": 2},
            "awareness": {"ticks_24h": 288},
            "cc_sessions": {
                "foreground": {"status": "healthy", "active": 2},
                "background": {"status": "error", "active": 1},  # budget read failed only
                "failed_24h": 3,
            },
        }
    )
    assert "dead_letters=0, pending_embeddings=150" in section
    assert "queue_depth=2" in section
    assert "ticks_24h=288" in section
    assert "foreground=2, background=1, failed_24h=3" in section
    assert "Embedding queue elevated**: 150 pending" in section
