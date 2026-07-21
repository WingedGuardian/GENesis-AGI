"""Tests for the calibration_status MCP tool (WS-2 P3 cell surface).

The hard requirement under test: thin/unknown cells render escalation
phrasing, NEVER a bare percentage (design §3.4/§4.3).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest

from genesis.db.crud import calibration_cells as cc_crud
from genesis.db.schema import create_all_tables

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _cell(**overrides) -> dict:
    base = {
        "domain": "outreach.general",
        "action_class": "outreach_send",
        "metric": "reply_received",
        "provenance": "stated",
        "window_days": 90,
        "n": 40,
        "n_mechanical": 40,
        "base_rate": 0.60,
        "mean_confidence": 0.85,
        "brier": 0.2,
        "reliability": 0.05,
        "resolution": 0.01,
        "uncertainty": 0.24,
        "ece": 0.2,
        "shrunk_estimate": 0.62,
        "status": "ok",
    }
    return {**base, **overrides}


async def _run(db, **kwargs):
    svc = MagicMock()
    svc._db = db
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.calibration_status import _impl_calibration_status

        return await _impl_calibration_status(**kwargs)


@pytest.mark.asyncio
async def test_unavailable_without_db():
    svc = MagicMock()
    svc._db = None
    with patch("genesis.mcp.health_mcp._service", svc):
        from genesis.mcp.health.calibration_status import _impl_calibration_status

        result = await _impl_calibration_status()
    assert result["status"] == "unavailable"


@pytest.mark.asyncio
async def test_no_data_on_empty_table(db):
    result = await _run(db)
    assert result["status"] == "no_data"
    assert "grading pass" in result["message"]


@pytest.mark.asyncio
async def test_ok_cells_render_percentages(db):
    await cc_crud.replace_cells(db, [_cell()], now=NOW)
    result = await _run(db)
    assert result["status"] == "ok"
    assert result["status_counts"] == {"ok": 1, "thin": 0, "unknown": 0}
    (line,) = result["cells_readable"]
    assert "says ~85%" in line and "right 62%" in line and "n=40" in line


@pytest.mark.asyncio
async def test_thin_and_unknown_render_escalation_never_bare_percent(db):
    await cc_crud.replace_cells(
        db,
        [
            _cell(domain="task.deploy", n=14, n_mechanical=14, status="thin"),
            _cell(domain="build", n=3, n_mechanical=3, status="unknown"),
        ],
        now=NOW,
    )
    result = await _run(db)
    by_domain = {line.split("/", 1)[0]: line for line in result["cells_readable"]}
    thin_line = by_domain["task.deploy"]
    unknown_line = by_domain["build"]
    assert "thin sample" in thin_line and "n=14" in thin_line
    assert "calibration unknown" in unknown_line and "escalate to user" in unknown_line
    # the hard requirement: no percentage figure anywhere in those lines
    for line in (thin_line, unknown_line):
        assert re.search(r"\d+%", line) is None, f"bare percentage leaked: {line}"
    # ...and neither is ranked as over/underconfident
    assert result["overconfident_domains"] == []
    assert result["underconfident_domains"] == []


@pytest.mark.asyncio
async def test_over_and_underconfident_ranking_ok_cells_only(db):
    await cc_crud.replace_cells(
        db,
        [
            _cell(domain="outreach.hot", mean_confidence=0.9, shrunk_estimate=0.5),
            _cell(domain="outreach.shy", mean_confidence=0.4, shrunk_estimate=0.7),
            _cell(domain="outreach.fine", mean_confidence=0.6, shrunk_estimate=0.58),
            _cell(
                domain="outreach.thin",
                mean_confidence=0.99,
                shrunk_estimate=0.1,
                n=12,
                status="thin",
            ),
        ],
        now=NOW,
    )
    result = await _run(db)
    over = [d["domain"] for d in result["overconfident_domains"]]
    under = [d["domain"] for d in result["underconfident_domains"]]
    assert over == ["outreach.hot"]  # thin excluded despite its huge gap
    assert under == ["outreach.shy"]


@pytest.mark.asyncio
async def test_domain_filter_and_history(db):
    await cc_crud.replace_cells(
        db,
        [_cell(), _cell(domain="task.deploy", action_class="task_execution", metric="completed")],
        now=NOW,
    )
    await cc_crud.append_history(db, [_cell()], now=NOW)
    result = await _run(db, domain="outreach", include_history=True)
    assert result["cell_count"] == 1
    assert result["cells"][0]["domain"] == "outreach.general"
    assert len(result["history"]) == 1
    # history without a domain filter is refused, not unbounded
    result_all = await _run(db, include_history=True)
    assert result_all["history"] == []
    assert "requires a domain" in result_all["history_note"]


@pytest.mark.asyncio
async def test_tool_lane_cells_render_observed_base_rate(db):
    await cc_crud.replace_cells(
        db,
        [
            _cell(
                domain="tool.Edit",
                action_class="tool_call",
                metric="success_rate",
                provenance="policy_prior",
                n=50,
                n_mechanical=50,
                base_rate=0.9,
                mean_confidence=None,
                brier=None,
                reliability=None,
                resolution=None,
                uncertainty=None,
                ece=None,
                shrunk_estimate=None,
                status="ok",
            )
        ],
        now=NOW,
    )
    result = await _run(db)
    (line,) = result["cells_readable"]
    assert "base rate 90%" in line and "observed only" in line


# ── WS-2 P4: earn-back evidence stream ──────────────────────────────────────


async def _seed_autonomy(db, *, category="direct_session", current=2, earned=4):
    await db.execute(
        "INSERT INTO autonomy_state (id, category, current_level, earned_level,"
        " updated_at) VALUES (?, ?, ?, ?, ?)",
        (f"as-{category}", category, current, earned, NOW.isoformat()),
    )
    await db.commit()


async def _seed_events(db, category, *, successes=0, corrections=0):
    for i in range(successes):
        await db.execute(
            "INSERT INTO autonomy_events (id, category, kind, occurred_at)"
            " VALUES (?, ?, 'success', ?)",
            (f"ev-s-{category}-{i}", category, NOW.isoformat()),
        )
    for i in range(corrections):
        await db.execute(
            "INSERT INTO autonomy_events (id, category, kind, occurred_at)"
            " VALUES (?, ?, 'correction', ?)",
            (f"ev-c-{category}-{i}", category, NOW.isoformat()),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_earnback_empty_when_nothing_demoted(db):
    await cc_crud.replace_cells(db, [_cell()], now=NOW)
    await _seed_autonomy(db, current=4, earned=4)  # not demoted
    result = await _run(db)
    assert result["earnback"] == {"demoted_categories": []}


@pytest.mark.asyncio
async def test_earnback_surfaces_demoted_category_with_windowed_evidence(db):
    await cc_crud.replace_cells(db, [_cell()], now=NOW)
    await _seed_autonomy(db, current=2, earned=4)
    await _seed_events(db, "direct_session", successes=30, corrections=1)
    result = await _run(db)
    (entry,) = result["earnback"]["demoted_categories"]
    assert entry["category"] == "direct_session"
    assert entry["current_level"] == 2
    assert entry["earned_level"] == 4
    assert entry["window_successes"] == 30
    assert entry["window_corrections"] == 1
    assert 0.0 < entry["posterior"] <= 1.0
    assert isinstance(entry["evidence_supports_earned"], bool)


@pytest.mark.asyncio
async def test_earnback_no_evidence_reads_zero_counts(db):
    await cc_crud.replace_cells(db, [_cell()], now=NOW)
    await _seed_autonomy(db, current=1, earned=3)
    result = await _run(db)
    (entry,) = result["earnback"]["demoted_categories"]
    assert entry["window_successes"] == 0
    assert entry["window_corrections"] == 0
    assert entry["evidence_supports_earned"] is False


@pytest.mark.asyncio
async def test_earnback_failure_degrades_to_unavailable(db):
    await cc_crud.replace_cells(db, [_cell()], now=NOW)
    await db.execute("DROP TABLE autonomy_state")
    await db.commit()
    result = await _run(db)
    assert result["earnback"]["demoted_categories"] == []
    assert result["earnback"]["unavailable"] is True
