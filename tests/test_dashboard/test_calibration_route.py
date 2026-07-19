"""Tests for /api/genesis/calibration (WS-2 P3 dashboard surface).

Split per the cc-sessions route-test precedent: the route wiring (guard, lane
validation) is pinned synchronously with a mocked runtime; the data collection
is tested by awaiting ``_collect_calibration`` directly against a real
in-memory DB (``_async_route`` owns its own event loop and cannot run inside
an async test's loop). The aggregation math itself is unit-tested in
test_ledger/test_cells.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.dashboard.routes.calibration import _collect_calibration
from genesis.db.crud import calibration_cells as cc_crud
from genesis.db.schema import create_all_tables

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _rt(db=None, *, bootstrapped=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt.db = db
    return rt


def _cell(**overrides) -> dict:
    base = {
        "domain": "outreach.general",
        "action_class": "outreach_send",
        "metric": "reply_received",
        "provenance": "stated",
        "window_days": 90,
        "n": 40,
        "n_mechanical": 40,
        "base_rate": 0.6,
        "mean_confidence": 0.85,
        "brier": 0.2,
        "shrunk_estimate": 0.62,
        "status": "ok",
    }
    return {**base, **overrides}


async def _seed(db):
    await cc_crud.replace_cells(
        db,
        [
            _cell(),
            _cell(provenance="policy_prior", mean_confidence=None),
            _cell(window_days=0, n=12, status="thin"),
        ],
        now=NOW,
    )
    # two graded rows for the summary shares: one mechanical, one llm_fallback
    for pid, resolver, status in (
        ("p-1", "mechanical", "resolved"),
        ("p-2", "llm_fallback", "fuzzy_resolved"),
    ):
        await db.execute(
            "INSERT INTO ledger_predictions (id, action_class, subject_ref_type,"
            " subject_ref_id, domain, metric, confidence, deadline_at, provenance,"
            " predictor, status, outcome_value, resolved_at, resolver)"
            " VALUES (?, 'outreach_send', 'outreach', ?, 'outreach.general',"
            " 'reply_received', 0.5, '2026-07-18T00:00:00+00:00', 'stated', 't',"
            " ?, 1, '2026-07-18T12:00:00+00:00', ?)",
            (pid, pid, status, resolver),
        )
    await db.commit()


# ── route wiring (sync, mocked runtime) ──────────────────────────────────────


def test_503_when_not_bootstrapped(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(bootstrapped=False)
        resp = client.get("/api/genesis/calibration")
    assert resp.status_code == 503


def test_400_on_invalid_lane(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(db=MagicMock())
        resp = client.get("/api/genesis/calibration?lane=vibes")
    assert resp.status_code == 400


# ── data collection (async, real DB) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cells_and_summary_default_stated_90d(db):
    await _seed(db)
    data = await _collect_calibration(db, lane="stated", window=90)
    assert [c["domain"] for c in data["cells"]] == ["outreach.general"]
    assert data["cells"][0]["provenance"] == "stated"
    summary = data["summary"]
    assert summary["ok"] == 1 and summary["thin"] == 0
    assert summary["graded_total"] == 2
    assert summary["mechanical_share"] == pytest.approx(0.5)
    assert summary["fallback_share"] == pytest.approx(0.5)
    assert summary["last_computed_at"] is not None


@pytest.mark.asyncio
async def test_lane_and_window_filters(db):
    await _seed(db)
    prior = await _collect_calibration(db, lane="policy_prior", window=90)
    alltime = await _collect_calibration(db, lane="stated", window=0)
    assert [c["provenance"] for c in prior["cells"]] == ["policy_prior"]
    assert [c["window_days"] for c in alltime["cells"]] == [0]
    assert alltime["cells"][0]["status"] == "thin"


@pytest.mark.asyncio
async def test_empty_state_summary(db):
    data = await _collect_calibration(db, lane="stated", window=90)
    assert data["cells"] == []
    assert data["summary"]["graded_total"] == 0
    assert data["summary"]["mechanical_share"] is None
    assert data["summary"]["last_computed_at"] is None


def test_400_on_invalid_window(client):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(db=MagicMock())
        assert client.get("/api/genesis/calibration?window=45").status_code == 400
        # Flask type=int would silently default non-numeric input — raw parse must 400
        assert client.get("/api/genesis/calibration?window=abc").status_code == 400
