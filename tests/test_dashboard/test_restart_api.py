"""Tests for service restart API endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
            mock_run.assert_called_once()
            assert "genesis-bridge.service" in mock_run.call_args[0][0]

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


class TestRestartAgentZero:
    def test_success(self, client):
        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            resp = client.post("/api/genesis/restart/agent-zero")
            assert resp.status_code == 200
            assert resp.get_json()["status"] == "ok"
            assert "agent-zero.service" in mock_run.call_args[0][0]

    def test_get_not_allowed(self, client):
        resp = client.get("/api/genesis/restart/agent-zero")
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

        pending = [{"id": "abc", "action_type": "bet", "status": "pending"}]

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
