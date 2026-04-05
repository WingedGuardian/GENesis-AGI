"""Tests for service restart API endpoints."""

from __future__ import annotations

import json
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


class TestRestartBridge:
    def test_success(self, client):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            resp = client.post("/api/genesis/restart/bridge")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "ok"
            # _detect_genesis_service() calls subprocess.run first, then the actual restart
            assert mock_run.call_count == 2
            restart_call = mock_run.call_args_list[1]
            assert "restart" in restart_call[0][0]

    def test_failure(self, client):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Unit not found"

        with patch("subprocess.run", return_value=mock_result):
            resp = client.post("/api/genesis/restart/bridge")
            assert resp.status_code == 500
            assert "Unit not found" in resp.get_json()["message"]

    def test_timeout(self, client):
        import subprocess

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            resp = client.post("/api/genesis/restart/bridge")
            assert resp.status_code == 500
            assert "timed out" in resp.get_json()["message"]

    def test_get_not_allowed(self, client):
        resp = client.get("/api/genesis/restart/bridge")
        assert resp.status_code == 405


class TestPendingApprovals:
    def test_empty_when_not_bootstrapped(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = False
        mock_rt._db = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/approvals")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_returns_pending(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt._db = MagicMock()

        pending = [{
            "id": "abc",
            "action_type": "bet",
            "status": "pending",
            "context": json.dumps({
                "kind": "autonomous_cli_fallback",
                "subsystem": "reflection",
                "action_label": "deep reflection",
                "api_call_site_id": "5_deep_reflection",
            }),
        }]

        with (
            patch("genesis.runtime.GenesisRuntime") as MockRT,
            patch("genesis.db.crud.approval_requests.list_pending", return_value=pending),
        ):
            MockRT.instance.return_value = mock_rt
            resp = client.get("/api/genesis/approvals")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["id"] == "abc"
            assert data[0]["context_data"]["subsystem"] == "reflection"
            assert data[0]["context_data"]["api_call_site_id"] == "5_deep_reflection"

    def test_resolve_requires_gate(self, client):
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt._autonomous_cli_approval_gate = None

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.post("/api/genesis/approvals/abc/resolve", json={"decision": "approved"})

        assert resp.status_code == 503

    def test_resolve_pending_approval(self, client):
        mock_gate = MagicMock()
        mock_gate.resolve_request = AsyncMock(return_value=True)
        mock_rt = MagicMock()
        mock_rt.is_bootstrapped = True
        mock_rt._autonomous_cli_approval_gate = mock_gate

        with patch("genesis.runtime.GenesisRuntime") as MockRT:
            MockRT.instance.return_value = mock_rt
            resp = client.post("/api/genesis/approvals/abc/resolve", json={"decision": "approved"})

        assert resp.status_code == 200
        assert resp.get_json() == {"id": "abc", "status": "approved"}
        mock_gate.resolve_request.assert_called_once_with(
            "abc", decision="approved", resolved_by="dashboard",
        )
