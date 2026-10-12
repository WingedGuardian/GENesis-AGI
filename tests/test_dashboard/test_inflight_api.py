"""GET /api/genesis/inflight: the server's own account of the work a restart
would cancel, for scripts/deploy_code_only.sh. It names sessions, so it answers
only a dashboard session or the internal bearer."""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard import auth as auth_mod
from genesis.dashboard.api import blueprint
from genesis.util import inflight as inf
from genesis.util.inflight import inflight


@pytest.fixture()
def client(tmp_path, monkeypatch):
    token_file = tmp_path / "internal_api_token"
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: token_file)
    auth_mod._internal_token_cache = None
    inf._items.clear()
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    yield app.test_client()
    auth_mod._internal_token_cache = None
    inf._items.clear()


def test_it_refuses_without_credentials_when_a_password_is_set(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    r = client.get("/api/genesis/inflight")
    assert r.status_code == 403


def test_it_answers_the_internal_bearer_with_the_registered_work(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    token = auth_mod.get_or_create_internal_api_token()
    with inflight("direct_session", "direct_session (research)", item_id="s-1"):
        r = client.get("/api/genesis/inflight", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    (item,) = r.get_json()["items"]
    assert (item["id"], item["kind"], item["label"]) == (
        "s-1",
        "direct_session",
        "direct_session (research)",
    )
    assert isinstance(item["started_at"], float)


def test_an_install_without_a_dashboard_password_still_refuses(client, monkeypatch):
    """No password means nothing can be VERIFIED, not that anyone may read it:
    the report names sessions, so it is a disclosure decision."""
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr("genesis.dashboard.auth.get_dashboard_password", lambda: "")
    with inflight("direct_session", item_id="s-1"):
        r = client.get("/api/genesis/inflight")
    assert r.status_code == 403


def test_a_wrong_bearer_is_refused(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    auth_mod.get_or_create_internal_api_token()
    r = client.get("/api/genesis/inflight", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


def test_nothing_in_flight_is_an_empty_list(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    token = auth_mod.get_or_create_internal_api_token()
    r = client.get("/api/genesis/inflight", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.get_json() == {"items": []}
