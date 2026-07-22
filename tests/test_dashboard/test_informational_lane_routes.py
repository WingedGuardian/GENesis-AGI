"""E2E (HTTP-layer) tests for the informational-proposal lane on the ego routes.

Drives the real Flask route handlers with the DB layer patched to return a mix
of approval + acknowledge-only (j9/gauntlet) pending rows, and asserts the split.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


def _rt():
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt._db = MagicMock()
    return rt


def _row(id_, action_type, ego_source, status="pending"):
    return {
        "id": id_,
        "action_type": action_type,
        "action_category": "",
        "content": f"content-{id_}",
        "rationale": "why",
        "confidence": 0.8,
        "urgency": "normal",
        "status": status,
        "created_at": "2026-07-22T10:00:00Z",
        "expires_at": None,
        "rank": None,
        "execution_plan": None,
        "recurring": 0,
        "ego_source": ego_source,
        "realist_verdict": None,
    }


# One real approval proposal + two acknowledge-only eval rows.
_MIXED = [
    _row("real1", "investigate", "genesis_ego_cycle"),
    _row("j9a", "j9_regression", "j9_eval"),
    _row("gaunt", "gauntlet_regression", "gauntlet"),
]


class TestEgoProposalsExcludesInformational:
    def test_proposals_endpoint_hides_informational(self, client):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.ego.list_pending_proposals",
                new_callable=AsyncMock,
                return_value=list(_MIXED),
            ),
        ):
            MockRT.instance.return_value = _rt()
            resp = client.get("/api/genesis/ego/proposals")
            data = resp.get_json()
            ids = [p["id"] for p in data]
            assert ids == ["real1"]  # eval rows excluded from approval list


class TestEgoInformationalEndpoint:
    def test_informational_endpoint_returns_only_eval(self, client):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.ego.list_pending_proposals",
                new_callable=AsyncMock,
                return_value=list(_MIXED),
            ),
        ):
            MockRT.instance.return_value = _rt()
            resp = client.get("/api/genesis/ego/informational")
            data = resp.get_json()
            ids = sorted(p["id"] for p in data)
            assert ids == ["gaunt", "j9a"]

    def test_informational_empty_when_not_bootstrapped(self, client):
        rt = _rt()
        rt.is_bootstrapped = False
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/informational")
            assert resp.get_json() == []


class TestEgoStatusCounts:
    def test_pending_count_excludes_informational(self, client):
        rt = _rt()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.ego.list_pending_proposals",
                new_callable=AsyncMock,
                return_value=list(_MIXED),
            ),
            patch("genesis.db.crud.ego.daily_ego_cost", new_callable=AsyncMock, return_value=0.0),
            patch("genesis.db.crud.ego.get_state", new_callable=AsyncMock, return_value=""),
            patch(
                "genesis.db.crud.ego.list_recent_cycles", new_callable=AsyncMock, return_value=[]
            ),
            patch("genesis.db.crud.ego.count_uncompacted", new_callable=AsyncMock, return_value=0),
            patch(
                "genesis.db.crud.ego.daily_dispatch_cost", new_callable=AsyncMock, return_value=0.0
            ),
            patch(
                "genesis.db.crud.ego.rolling_daily_ego_cost",
                new_callable=AsyncMock,
                return_value=0.0,
            ),
            patch(
                "genesis.db.crud.ego.has_pending_cli_approval",
                new_callable=AsyncMock,
                return_value=False,
            ),
        ):
            rt._ego_cadence_manager = None
            rt._genesis_ego_cadence_manager = None
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/status")
            data = resp.get_json()
            # 3 pending rows in, but only the 1 approval item counts as pending.
            assert data["pending_proposals"] == 1
            assert data["informational_count"] == 2
