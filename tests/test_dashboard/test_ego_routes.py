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
        mgr.next_fire_at = "2026-01-01T00:00:00+00:00"
        mgr.source_tag = "user_ego_cycle"
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
        with patch("genesis.runtime.GenesisRuntime") as MockRT, patch(
            "genesis.db.crud.ego.has_pending_cli_approval",
            new=AsyncMock(return_value=False),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/cadence")
            data = resp.get_json()
            assert data["available"] is True
            assert data["is_running"] is True
            assert data["is_paused"] is False
            assert data["current_interval_minutes"] == 120
            assert data["consecutive_failures"] == 0
            # WS-3 additions: honest cadence surface.
            assert data["next_fire_at"] == "2026-01-01T00:00:00+00:00"
            assert data["gated"] is False

    def test_gated_when_approval_pending(self, client):
        rt = _mock_runtime()
        with patch("genesis.runtime.GenesisRuntime") as MockRT, patch(
            "genesis.db.crud.ego.has_pending_cli_approval",
            new=AsyncMock(return_value=True),
        ):
            MockRT.instance.return_value = rt
            resp = client.get("/api/genesis/ego/cadence")
            data = resp.get_json()
            assert data["gated"] is True


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


# ── PR-6a: revision guard + surfacing ───────────────────────────────


class TestEgoRevisionGuard:
    def test_proposals_all_surfaces_revision_fields(self, client):
        """The /all payload (what the resolve card reads) carries revision_num
        + revalidate_at + last_validated_at; absent revision_num defaults to 1."""
        rt = _mock_runtime()
        proposals = [
            {
                "id": "p1", "action_type": "research", "action_category": "learning",
                "content": "c", "rationale": "r", "confidence": 0.8,
                "urgency": "normal", "alternatives": "", "status": "pending",
                "user_response": None, "cycle_id": "c1", "batch_id": "b1",
                "created_at": "2026-04-20T10:00:00Z", "resolved_at": None,
                "expires_at": None, "revision_num": 3,
                "revalidate_at": "2026-05-01T00:00:00Z",
                "last_validated_at": "2026-04-25T00:00:00Z",
            },
            {
                "id": "p2", "action_type": "research", "action_category": "learning",
                "content": "c", "rationale": "r", "confidence": 0.8,
                "urgency": "normal", "alternatives": "", "status": "pending",
                "user_response": None, "cycle_id": "c1", "batch_id": "b1",
                "created_at": "2026-04-20T10:00:00Z", "resolved_at": None,
                "expires_at": None,  # no revision fields → defaults
            },
        ]
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.list_proposals", new_callable=AsyncMock, return_value=proposals),
        ):
            MockRT.instance.return_value = rt
            data = client.get("/api/genesis/ego/proposals/all").get_json()
        assert data[0]["revision_num"] == 3
        assert data[0]["revalidate_at"] == "2026-05-01T00:00:00Z"
        assert data[0]["last_validated_at"] == "2026-04-25T00:00:00Z"
        # Absent → default 1 / None, never KeyError.
        assert data[1]["revision_num"] == 1
        assert data[1]["revalidate_at"] is None
        assert data[1]["last_validated_at"] is None

    def test_resolve_passes_expected_revision(self, client):
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True) as mock_resolve,
            patch("genesis.db.crud.ego.get_proposal", new_callable=AsyncMock, return_value=None),
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved", "revision_num": 2},
            )
        assert resp.get_json()["ok"] is True
        assert mock_resolve.call_args[1]["expected_revision"] == 2

    def test_resolve_without_revision_is_unguarded(self, client):
        """FOOTGUN GUARD: a body without revision_num maps to expected_revision
        None (unguarded, as today) — NEVER a hardcoded 1."""
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=True) as mock_resolve,
            patch("genesis.db.crud.ego.get_proposal", new_callable=AsyncMock, return_value=None),
        ):
            MockRT.instance.return_value = rt
            client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved"},
            )
        assert mock_resolve.call_args[1]["expected_revision"] is None

    def test_resolve_stale_revision_returns_409(self, client):
        """resolve refused + row still pending at a different revision → 409
        stale_revision with the fresh revision, so the client can re-review."""
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=False),
            patch(
                "genesis.db.crud.ego.get_proposal",
                new_callable=AsyncMock,
                return_value={"id": "p1", "status": "pending", "revision_num": 5},
            ),
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved", "revision_num": 2},
            )
        assert resp.status_code == 409
        data = resp.get_json()
        assert data["error"] == "stale_revision"
        assert data["revision_num"] == 5

    def test_resolve_already_resolved_stays_404(self, client):
        """A guard miss where the row is no longer pending (already resolved)
        is a 404, not a false stale-revision 409."""
        rt = _mock_runtime()
        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.ego.resolve_proposal", new_callable=AsyncMock, return_value=False),
            patch(
                "genesis.db.crud.ego.get_proposal",
                new_callable=AsyncMock,
                return_value={"id": "p1", "status": "approved", "revision_num": 2},
            ),
        ):
            MockRT.instance.return_value = rt
            resp = client.post(
                "/api/genesis/ego/proposals/p1/resolve",
                json={"status": "approved", "revision_num": 2},
            )
        assert resp.status_code == 404

