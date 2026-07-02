"""Genesis Voice routes — the OPTIONAL-addon gate.

Invariant: a stock install with no ~/.genesis/ambient_remote.yaml must see NOTHING voice —
the enable probe returns false and the page 404s. A present (even malformed) config → enabled.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.secret_key = "test-secret-key"
    return app.test_client()


_CFG = "genesis.observability.ambient_health.load_ambient_remote_config"


# ── /api/genesis/voice/enabled ─────────────────────────────────────────────

def test_enabled_true_when_config_present(client):
    with patch(_CFG, return_value=object()):
        resp = client.get("/api/genesis/voice/enabled")
    assert resp.status_code == 200
    assert resp.get_json() == {"enabled": True}


def test_enabled_false_when_no_config(client):
    with patch(_CFG, return_value=None):
        resp = client.get("/api/genesis/voice/enabled")
    assert resp.status_code == 200
    assert resp.get_json() == {"enabled": False}


def test_enabled_true_when_config_malformed(client):
    from genesis.observability.ambient_health import AmbientRemoteConfigError

    with patch(_CFG, side_effect=AmbientRemoteConfigError("bad yaml")):
        resp = client.get("/api/genesis/voice/enabled")
    assert resp.status_code == 200
    assert resp.get_json() == {"enabled": True}  # present-but-broken is still a voice install


# ── /genesis/voice page (optional-addon 404) ───────────────────────────────

def test_page_404_when_not_configured(client):
    with patch(_CFG, return_value=None):
        resp = client.get("/genesis/voice")
    assert resp.status_code == 404


def test_page_served_when_configured(client):
    with patch(_CFG, return_value=object()):
        resp = client.get("/genesis/voice")
    assert resp.status_code == 200
    assert b"Genesis Voice" in resp.data  # the served template
