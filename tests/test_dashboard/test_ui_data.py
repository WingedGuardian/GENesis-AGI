"""Tests for UI data endpoints on the genesis_dashboard blueprint.

These endpoints were formerly on the AZ-only genesis_ui blueprint.
Moving to genesis_dashboard makes them available in standalone mode.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import genesis.dashboard.routes  # noqa: F401 — registers routes on blueprint
from genesis.dashboard._blueprint import blueprint


@pytest.fixture()
def app():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def mock_rt_unbootstrapped():
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.db = None
    return mock_rt


# ── Sessions ─────────────────────────────────────────────────────────


def test_sessions_empty_when_not_bootstrapped(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/sessions")
    assert resp.status_code == 200
    assert resp.get_json() == []


# ── Memory stats ─────────────────────────────────────────────────────


def test_memory_stats_empty_when_not_bootstrapped(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/memory/stats")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["episodic"] == 0
    assert data["procedural"] == 0
    assert data["observations"] == 0


# ── Memory search ─────────────────────────────────────────────────────


def test_memory_search_empty_when_not_bootstrapped(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/memory/search?type=observations")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == []
    assert data["total"] == 0


def test_memory_search_invalid_type_returns_empty(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/memory/search?type=invalid_type")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["results"] == []


# ── Inbox ─────────────────────────────────────────────────────────────


def test_inbox_empty_when_not_bootstrapped(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/inbox")
    assert resp.status_code == 200
    assert resp.get_json() == []


# ── Tasks ─────────────────────────────────────────────────────────────


def test_tasks_empty_when_not_bootstrapped(client, mock_rt_unbootstrapped):
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt_unbootstrapped
        resp = client.get("/api/genesis/ui/tasks")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["jobs"] == []


# ── Routes are on genesis_dashboard ──────────────────────────────────


def test_ui_data_routes_on_dashboard_blueprint(app):
    """Confirm all /api/genesis/ui/* routes are registered on genesis_dashboard."""
    rules = {rule.rule for rule in app.url_map.iter_rules()}
    assert "/api/genesis/ui/sessions" in rules
    assert "/api/genesis/ui/memory/stats" in rules
    assert "/api/genesis/ui/memory/search" in rules
    assert "/api/genesis/ui/inbox" in rules
    assert "/api/genesis/ui/tasks" in rules
