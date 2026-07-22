"""E2E (HTTP-layer) tests for the ego Directives & Goals visibility routes.

Drives the real Flask route handlers with the DB/CRUD layer patched, asserting
the payload shape (active/resolved split, user/own-goal split), the
not-bootstrapped guards, and the retire (resolve) contract incl. 400/404/503.
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


def _rt(bootstrapped=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt._db = MagicMock() if bootstrapped else None
    return rt


def _drow(id_, ego_target, status, resolved_at=None):
    return {
        "id": id_,
        "content": f"directive {id_}",
        "priority": "high",
        "source": "user",
        "ego_target": ego_target,
        "status": status,
        "created_at": "2026-06-08T00:00:00+00:00",
        "resolved_at": resolved_at,
        "resolution": None,
        "reaffirm_count": 0,
        "last_reaffirmed_at": None,
    }


def _grow(id_, origin):
    return {
        "id": id_,
        "title": f"goal {id_}",
        "description": "d",
        "category": "project",
        "priority": "high",
        "status": "active",
        "timeline": None,
        "confidence": 0.5,
        "goal_type": "milestone",
        "origin": origin,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-01T00:00:00+00:00",
    }


def _list_directives_side_effect(db, *, statuses=("active",), limit=20, **_kw):
    if "active" in statuses:
        return [_drow("c60", "genesis_ego", "active"), _drow("u1", "user_ego", "active")]
    return [_drow("old", "user_ego", "completed", resolved_at="2026-07-19T00:00:00+00:00")]


class TestEgoDirectives:
    def test_active_and_resolved_split(self, client):
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.ego.list_directives",
                new=AsyncMock(side_effect=_list_directives_side_effect),
            ),
        ):
            MockRT.instance.return_value = _rt()
            data = client.get("/api/genesis/ego/directives").get_json()
            assert [d["id"] for d in data["active"]] == ["c60", "u1"]
            assert [d["id"] for d in data["resolved"]] == ["old"]
            # payload projection carries priority/target/reaffirm for the panel
            assert data["active"][0]["ego_target"] == "genesis_ego"
            assert data["active"][0]["reaffirm_count"] == 0

    def test_empty_when_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _rt(bootstrapped=False)
            data = client.get("/api/genesis/ego/directives").get_json()
            assert data == {"active": [], "resolved": []}


def _goals_by_origin(by_origin):
    """Build a list_active side_effect returning per-origin rows (the route
    now queries each lane separately, so a busy lane can't starve the other)."""

    def _side_effect(db, *, limit=20, origin=None):
        return list(by_origin.get(origin, []))

    return _side_effect


class TestEgoGoals:
    def test_split_by_origin(self, client):
        side = _goals_by_origin(
            {
                "user": [_grow("g1", "user"), _grow("g3", "user")],
                "genesis_ego": [_grow("g2", "genesis_ego")],
            }
        )
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.user_goals.list_active",
                new=AsyncMock(side_effect=side),
            ),
        ):
            MockRT.instance.return_value = _rt()
            data = client.get("/api/genesis/ego/goals").get_json()
            assert [g["id"] for g in data["user"]] == ["g1", "g3"]
            assert [g["id"] for g in data["genesis_ego"]] == ["g2"]

    def test_empty_own_goal_lane_renders(self, client):
        # fresh-install state: only user goals, empty own-goal lane
        side = _goals_by_origin({"user": [_grow("g1", "user")], "genesis_ego": []})
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.user_goals.list_active", new=AsyncMock(side_effect=side)),
        ):
            MockRT.instance.return_value = _rt()
            data = client.get("/api/genesis/ego/goals").get_json()
            assert data["genesis_ego"] == []
            assert len(data["user"]) == 1

    def test_empty_when_not_bootstrapped(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _rt(bootstrapped=False)
            data = client.get("/api/genesis/ego/goals").get_json()
            assert data == {"user": [], "genesis_ego": []}


class TestEgoDirectiveResolve:
    def test_retire_happy_path(self, client):
        resolve = AsyncMock(return_value=True)
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_directive", new=resolve),
        ):
            MockRT.instance.return_value = _rt()
            resp = client.post("/api/genesis/ego/directives/c60/resolve", json={})
            assert resp.status_code == 200
            assert resp.get_json() == {"ok": True, "id": "c60", "status": "cancelled"}
            # default retire uses status='cancelled' + a resolution note
            _args, kwargs = resolve.call_args
            assert kwargs["status"] == "cancelled"
            assert kwargs["resolution"] == "Retired via dashboard"

    def test_completed_status_passthrough(self, client):
        resolve = AsyncMock(return_value=True)
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_directive", new=resolve),
        ):
            MockRT.instance.return_value = _rt()
            resp = client.post(
                "/api/genesis/ego/directives/c60/resolve",
                json={"status": "completed", "resolution": "done"},
            )
            assert resp.status_code == 200
            _args, kwargs = resolve.call_args
            assert kwargs["status"] == "completed"
            assert kwargs["resolution"] == "done"

    def test_not_found_or_decision_row_is_404(self, client):
        # resolve_directive returns False for a missing/already-resolved/decision row
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch(
                "genesis.db.crud.ego.resolve_directive",
                new=AsyncMock(return_value=False),
            ),
        ):
            MockRT.instance.return_value = _rt()
            resp = client.post("/api/genesis/ego/directives/nope/resolve", json={})
            assert resp.status_code == 404
            assert resp.get_json()["ok"] is False

    def test_bad_status_is_400(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _rt()
            resp = client.post(
                "/api/genesis/ego/directives/c60/resolve",
                json={"status": "approved"},
            )
            assert resp.status_code == 400

    def test_not_bootstrapped_is_503(self, client):
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = _rt(bootstrapped=False)
            resp = client.post("/api/genesis/ego/directives/c60/resolve", json={})
            assert resp.status_code == 503
