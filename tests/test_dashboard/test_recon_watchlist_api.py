"""Tests for the dashboard recon-watchlist routes (list/add/disable/remove).

The store functions are mocked — these cover routing, the auth gate on
mutations, and error→422 mapping.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint

_WL = "genesis.recon.watchlist"
_AUTH = "genesis.dashboard.routes.recon.is_authenticated"


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.secret_key = "test-secret-key"
    return app.test_client()


@pytest.fixture(autouse=True)
def _authed():
    with patch(_AUTH, return_value=True):
        yield


def test_list_returns_entries(client):
    entries = [{"repo": "a/b", "source": "base", "disabled": False}]
    with patch(f"{_WL}.list_entries", return_value=entries):
        resp = client.get("/api/genesis/recon/watchlist")
    assert resp.status_code == 200
    assert resp.get_json()["entries"] == entries


def test_list_survives_store_error(client):
    with patch(f"{_WL}.list_entries", side_effect=RuntimeError("boom")):
        resp = client.get("/api/genesis/recon/watchlist")
    assert resp.status_code == 200 and resp.get_json()["entries"] == []


def test_add_success(client):
    with patch(f"{_WL}.add_repo", return_value={"ok": True, "repo": "a/b"}) as add:
        resp = client.post("/api/genesis/recon/watchlist",
                           json={"name": "X", "repo": "a/b",
                                 "track": ["releases"], "priority": "low"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert add.call_args.args[0]["repo"] == "a/b"


def test_add_validation_error_maps_422(client):
    with patch(f"{_WL}.add_repo", return_value={"error": "bad"}):
        resp = client.post("/api/genesis/recon/watchlist", json={"repo": "bad"})
    assert resp.status_code == 422


def test_disable_success(client):
    with patch(f"{_WL}.set_base_disabled",
               return_value={"ok": True, "repo": "a/b", "disabled": True}) as fn:
        resp = client.post("/api/genesis/recon/watchlist/disable",
                           json={"repo": "a/b", "disabled": True})
    assert resp.status_code == 200
    assert fn.call_args.args == ("a/b", True)


def test_remove_success(client):
    with patch(f"{_WL}.remove_overlay_repo",
               return_value={"ok": True, "repo": "a/b"}) as fn:
        resp = client.delete("/api/genesis/recon/watchlist", json={"repo": "a/b"})
    assert resp.status_code == 200
    assert fn.call_args.args == ("a/b",)


def test_remove_base_error_maps_422(client):
    with patch(f"{_WL}.remove_overlay_repo", return_value={"error": "base only"}):
        resp = client.delete("/api/genesis/recon/watchlist", json={"repo": "x/y"})
    assert resp.status_code == 422


# ── auth gate on mutations ────────────────────────────────────────────
@pytest.mark.parametrize("method,path", [
    ("post", "/api/genesis/recon/watchlist"),
    ("post", "/api/genesis/recon/watchlist/disable"),
    ("delete", "/api/genesis/recon/watchlist"),
])
def test_mutations_require_auth(client, method, path):
    with (
        patch(_AUTH, return_value=False),
        patch(f"{_WL}.add_repo") as add,
        patch(f"{_WL}.set_base_disabled") as dis,
        patch(f"{_WL}.remove_overlay_repo") as rem,
    ):
        resp = getattr(client, method)(path, json={"repo": "a/b"})
    assert resp.status_code == 401
    add.assert_not_called()
    dis.assert_not_called()
    rem.assert_not_called()


def test_list_does_not_require_auth(client):
    with (
        patch(_AUTH, return_value=False),
        patch(f"{_WL}.list_entries", return_value=[]),
    ):
        resp = client.get("/api/genesis/recon/watchlist")
    assert resp.status_code == 200
