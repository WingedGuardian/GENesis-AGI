"""Tests for the app-level /api mutation auth gate (check_api_mutation_auth).

The gate is registered as an APP-level before_request so it covers every
blueprint uniformly. These tests register the gate on a bare Flask app with
plain routes (NOT the dashboard blueprint) — which is itself the proof that the
gate fires regardless of blueprint (the outreach_api coverage case).
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify

from genesis.dashboard import auth as auth_mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # Isolate the internal-token file and reset the in-process cache so each test
    # generates/reads its own token.
    token_file = tmp_path / "internal_api_token"
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: token_file)
    auth_mod._internal_token_cache = None

    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    app.before_request(auth_mod.check_api_mutation_auth)

    @app.route("/api/genesis/thing", methods=["GET", "POST"])
    def thing():
        return jsonify({"ok": True})

    @app.route("/api/genesis/auth/login", methods=["POST"])
    def login_stub():
        return jsonify({"ok": True})

    @app.route("/api/t/mytool", methods=["POST"])
    def tool_stub():
        return jsonify({"ok": True})

    @app.route("/api/host/native", methods=["POST"])
    def host_native_stub():
        return jsonify({"ok": True})

    @app.route("/v1/voice/thing", methods=["POST"])
    def v1_stub():
        return jsonify({"ok": True})

    yield app.test_client()
    auth_mod._internal_token_cache = None


def _pw(monkeypatch, val="pw"):
    monkeypatch.setenv("DASHBOARD_PASSWORD", val)


def test_no_password_mutation_open(client, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert client.post("/api/genesis/thing").status_code == 200


def test_password_blocks_mutation_without_credential(client, monkeypatch):
    _pw(monkeypatch)
    assert client.post("/api/genesis/thing").status_code == 401


def test_password_leaves_get_open(client, monkeypatch):
    _pw(monkeypatch)
    assert client.get("/api/genesis/thing").status_code == 200


def test_valid_session_cookie_passes(client, monkeypatch):
    _pw(monkeypatch)
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    # A real same-origin browser fetch always carries Sec-Fetch-Site: same-origin.
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 200


def test_valid_internal_bearer_passes(client, monkeypatch):
    _pw(monkeypatch)
    token = auth_mod.get_or_create_internal_api_token()
    resp = client.post("/api/genesis/thing", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


def test_wrong_bearer_blocked(client, monkeypatch):
    _pw(monkeypatch)
    resp = client.post("/api/genesis/thing", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_auth_endpoints_exempt(client, monkeypatch):
    _pw(monkeypatch)
    assert client.post("/api/genesis/auth/login").status_code == 200


def test_v1_surface_exempt(client, monkeypatch):
    _pw(monkeypatch)
    assert client.post("/v1/voice/thing").status_code == 200


def test_tool_api_prefix_is_gated(client, monkeypatch):
    # /api/t/* is a Genesis-owned mutation surface → gated.
    _pw(monkeypatch)
    assert client.post("/api/t/mytool").status_code == 401


def test_non_genesis_api_route_is_left_open(client, monkeypatch):
    # A co-hosting framework's OWN /api/* route (not Genesis-owned) must NOT be
    # gated, even with a password set — else enabling the password would break the
    # host's unrelated API in Agent Zero mode.
    _pw(monkeypatch)
    assert client.post("/api/host/native").status_code == 200


def test_kill_switch_disables_gate(client, monkeypatch):
    _pw(monkeypatch)
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "off")
    assert client.post("/api/genesis/thing").status_code == 200


def test_apply_gate_idempotent_and_wires(tmp_path, monkeypatch):
    """The shared helper mints the token, wires the gate ONCE, and actually gates."""
    token_file = tmp_path / "internal_api_token"
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: token_file)
    auth_mod._internal_token_cache = None
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")

    app = Flask(__name__)
    app.secret_key = "k"

    @app.route("/api/genesis/thing", methods=["POST"])
    def thing():
        return jsonify({"ok": True})

    auth_mod.apply_api_mutation_gate(app)
    auth_mod.apply_api_mutation_gate(app)  # idempotent — must NOT stack the hook
    assert getattr(app, "_genesis_api_mutation_gate_applied", False) is True
    hooks = app.before_request_funcs.get(None, [])
    assert hooks.count(auth_mod.check_api_mutation_auth) == 1
    assert app.test_client().post("/api/genesis/thing").status_code == 401  # actually gates
    auth_mod._internal_token_cache = None


# ── CSRF: cookie-authed mutations must be same-origin (bearer path is immune) ──
#
# SameSite=Lax attaches the session cookie on same-SITE requests (a sibling origin
# on another port/subdomain of the dashboard host), so a cookie alone is not proof
# of same-origin intent. These assert the Sec-Fetch-Site (primary) + Origin
# (fallback) same-origin check, fail-closed when neither signal is present.


def _authed(client):
    with client.session_transaction() as sess:
        sess["authenticated"] = True


def test_cookie_same_origin_sec_fetch_passes(client, monkeypatch):
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 200


def test_cookie_sec_fetch_none_passes(client, monkeypatch):
    # Sec-Fetch-Site: none = a direct user action (typed URL / bookmark), not a
    # cross-origin ride — treated as safe per the OWASP fetch-metadata policy.
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "none"})
    assert resp.status_code == 200


def test_cookie_same_site_sec_fetch_blocked(client, monkeypatch):
    # The core threat: a same-site sibling origin riding the SameSite=Lax cookie.
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 403


def test_cookie_cross_site_sec_fetch_blocked(client, monkeypatch):
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403


def test_cookie_no_headers_fail_closed(client, monkeypatch):
    # Neither Sec-Fetch-Site nor Origin — a cookie-authed mutation with no
    # same-origin signal is refused (a real machine caller uses the bearer).
    _pw(monkeypatch)
    _authed(client)
    assert client.post("/api/genesis/thing").status_code == 403


def test_cookie_origin_fallback_matches_host(client, monkeypatch):
    # No Sec-Fetch-Site (old browser) → Origin host must match the request Host.
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Origin": "http://localhost"})
    assert resp.status_code == 200


def test_cookie_origin_fallback_mismatch_blocked(client, monkeypatch):
    _pw(monkeypatch)
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


def test_bearer_immune_to_cross_origin(client, monkeypatch):
    # A valid internal bearer is CSRF-immune — origin headers must not block it.
    _pw(monkeypatch)
    token = auth_mod.get_or_create_internal_api_token()
    resp = client.post(
        "/api/genesis/thing",
        headers={"Authorization": f"Bearer {token}", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 200


def test_csrf_not_enforced_when_kill_switch_off(client, monkeypatch):
    _pw(monkeypatch)
    monkeypatch.setenv("GENESIS_DASHBOARD_API_AUTH", "off")
    _authed(client)
    resp = client.post("/api/genesis/thing", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 200


def test_agent_zero_adapter_applies_mutation_gate(monkeypatch):
    """Regression: the Agent Zero host mounts the dashboard/outreach blueprints, so it
    MUST apply the mutation gate too (else /api mutations are unauthed in AZ mode)."""
    import genesis.dashboard.heartbeat as hb_mod
    from genesis.hosting.agent_zero.adapter import AgentZeroAdapter

    # Neutralize the heartbeat thread the dashboard registration would start.
    monkeypatch.setattr(hb_mod.DashboardHeartbeat, "start", lambda self: None)
    seen = {}
    monkeypatch.setattr(
        auth_mod, "apply_api_mutation_gate", lambda app: seen.setdefault("app", app)
    )

    app = Flask(__name__)
    AgentZeroAdapter().register_blueprints(app)
    assert seen.get("app") is app  # the gate was applied to AZ's app
