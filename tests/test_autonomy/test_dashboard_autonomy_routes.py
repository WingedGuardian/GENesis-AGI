"""Tests for the WS-8 PR-D dashboard autonomy routes (the owner "Activity" tab).

Registration + not-bootstrapped smoke (the cross-event-loop aiosqlite limit makes
full DB-backed route tests impractical here — the flag→demote behaviour is covered
by the CRUD unit tests and the browser E2E, matching the Traces-tab precedent)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import genesis.dashboard.routes.autonomy  # noqa: F401 — registers the routes
from genesis.dashboard._blueprint import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def test_routes_registered():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/genesis/autonomy/grants" in rules
    assert "/api/genesis/autonomy/sends" in rules
    assert "/api/genesis/autonomy/sends/<send_id>/flag" in rules


def test_grants_empty_when_not_bootstrapped(client):
    rt = MagicMock(is_bootstrapped=False, db=None)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.get("/api/genesis/autonomy/grants")
        assert resp.status_code == 200
        assert resp.get_json() == {"grants": []}


def test_sends_empty_when_not_bootstrapped(client):
    rt = MagicMock(is_bootstrapped=False, db=None)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.get("/api/genesis/autonomy/sends")
        assert resp.status_code == 200
        assert resp.get_json() == {"sends": []}


def test_flag_503_when_not_bootstrapped(client):
    rt = MagicMock(is_bootstrapped=False, db=None)
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = rt
        resp = client.post("/api/genesis/autonomy/sends/x/flag")
        assert resp.status_code == 503
