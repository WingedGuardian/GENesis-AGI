"""Tests for ego dashboard API endpoints."""

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


def _mock_runtime(*, bootstrapped=True, db=True, cadence=True):
    """Build a mock GenesisRuntime."""
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt._db = MagicMock() if db else None
    if cadence:
        mgr = MagicMock()
        mgr.is_running = True
        mgr.is_paused = False
        mgr.current_interval_minutes = 120
        mgr.consecutive_failures = 0
        rt._ego_cadence_manager = mgr
    else:
        rt._ego_cadence_manager = None
    return rt


# ── /api/genesis/ego/cadence ────────────────────────────────────────


class TestEgoCadence:
    def test_not_bootstrapped(self, client):
        rt = _mock_runtime(bootstrapped=False)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/cadence")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["available"] is False

    def test_no_cadence_manager(self, client):
        rt = _mock_runtime(cadence=False)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/cadence")
            data = resp.get_json()
            assert data["available"] is False

    def test_returns_cadence_state(self, client):
        rt = _mock_runtime()
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/cadence")
            data = resp.get_json()
            assert data["available"] is True
            assert data["is_running"] is True
            assert data["is_paused"] is False
            assert data["current_interval_minutes"] == 120
            assert data["consecutive_failures"] == 0


# ── /api/genesis/ego/proposals/all ──────────────────────────────────


class TestEgoProposalsAll:
    def test_not_bootstrapped(self, client):
        rt = _mock_runtime(bootstrapped=False)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/proposals/all")
            assert resp.get_json() == []

    def test_returns_proposals(self, client):
        rt = _mock_runtime()
        proposals = [
            {
                "id": "p1", "action_type": "research", "action_category": "learning",
                "content": "test", "rationale": "why", "confidence": 0.8,
                "urgency": "normal", "alternatives": "", "status": "pending",
                "user_response": None, "cycle_id": "c1", "batch_id": "b1",
                "created_at": "2026-04-20T10:00:00Z", "resolved_at": None,
                "expires_at": "2026-04-21T10:00:00Z",
            },
        ]
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.list_proposals", new_callable=AsyncMock, return_value=proposals),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/proposals/all")
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["id"] == "p1"
            assert data[0]["rationale"] == "why"

    def test_status_filter(self, client):
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.list_proposals", new_callable=AsyncMock, return_value=[]) as mock_list,
        ):
            MockRT.instance.return_value = rt
            client.get("/api/genesis/ego/proposals/all?status=approved&limit=10")
            mock_list.assert_called_once()
            call_kwargs = mock_list.call_args
            assert call_kwargs[1]["status"] == "approved"
            assert call_kwargs[1]["limit"] == 10


# ── /api/genesis/ego/proposals/<id>/resolve ─────────────────────────


class TestEgoProposalResolve:
    def test_not_bootstrapped(self, client):
        rt = _mock_runtime(bootstrapped=False)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved"},
            )
            assert resp.status_code == 503

    def test_invalid_status(self, client):
        rt = _mock_runtime()
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "maybe"},
            )
            assert resp.status_code == 400

    def test_approve_success(self, client):
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True),
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved"},
            )
            data = resp.get_json()
            assert data["ok"] is True
            assert data["status"] == "approved"

    def test_reject_with_reason(self, client):
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True) as mock_resolve,
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "rejected", "response": "not now"},
            )
            data = resp.get_json()
            assert data["ok"] is True
            assert data["status"] == "rejected"
            # Verify the reason was passed through
            call_kwargs = mock_resolve.call_args
            assert call_kwargs[1]["user_response"] == "not now"

    def test_not_found(self, client):
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=False),
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved"},
            )
            assert resp.status_code == 404


# ── /api/genesis/ego/follow-ups ─────────────────────────────────────


class TestEgoFollowUps:
    def test_not_bootstrapped(self, client):
        rt = _mock_runtime(bootstrapped=False)
        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/follow-ups")
            assert resp.get_json() == []

    def test_returns_follow_ups(self, client):
        rt = _mock_runtime()
        items = [
            {
                "id": "f1", "content": "check X", "reason": "ego asked",
                "strategy": "ego_judgment", "status": "pending",
                "priority": "medium", "created_at": "2026-04-20T10:00:00Z",
                "scheduled_at": None,
            },
        ]
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.follow_ups.get_pending", new_callable=AsyncMock, return_value=items),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/follow-ups")
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["id"] == "f1"
            assert data[0]["strategy"] == "ego_judgment"
