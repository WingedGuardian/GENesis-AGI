"""Tests for the app-level /api/genesis network-scope gate (check_api_network_scope).

The dashboard's /api/genesis/* routes serve stored memory, knowledge, session and
observation content to any GET, by design of the mutation gate (reads are open).
This gate scopes them by SOURCE NETWORK instead: loopback and the Tailscale range
are trusted, everything else is refused -- except the few routes a host-side
supervisor probes from outside (health, heartbeat, pause, guardian-dialogue).

``request.remote_addr`` is the TCP peer: no proxy-header trust is installed, so a
caller cannot claim a trusted address through a forwarded-for header.
"""

from __future__ import annotations

import pytest
from flask import Flask, jsonify

from genesis.dashboard import auth as auth_mod

_LAN = "192.0.2.10"  # RFC 5737 documentation address: an untrusted peer
_TAILNET = "100.101.102.103"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "tok")
    auth_mod._internal_token_cache = None
    monkeypatch.delenv("GENESIS_DASHBOARD_TRUSTED_NETWORKS", raising=False)
    monkeypatch.delenv("GENESIS_DASHBOARD_NETWORK_SCOPE", raising=False)
    # No password: proves the network gate does not depend on dashboard auth.
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)

    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    auth_mod.apply_api_mutation_gate(app)

    def ok(**_url_args):
        return jsonify({"ok": True})

    for path in (
        "/api/genesis/knowledge/<unit_id>",
        "/api/genesis/references/list",
        "/api/genesis/memory/recent",
        "/api/genesis/sessions",
        "/api/genesis/observations",
        "/api/genesis/health",
        "/api/genesis/heartbeat",
        "/api/genesis/pause",
        "/api/genesis/healthz",
        "/api/t/some_tool",
        "/v1/thing",
        "/genesis",
    ):
        app.add_url_rule(path, path, ok, methods=["GET", "POST"])
    app.add_url_rule(
        "/api/genesis/guardian-dialogue",
        "gd",
        ok,
        methods=["POST"],
    )
    yield app
    auth_mod._internal_token_cache = None


def _get(app, path, addr, method="GET"):
    c = app.test_client()
    return c.open(path, method=method, environ_base={"REMOTE_ADDR": addr})


@pytest.mark.parametrize(
    "path",
    [
        "/api/genesis/knowledge/abc",
        "/api/genesis/references/list",
        "/api/genesis/memory/recent",
        "/api/genesis/sessions",
        "/api/genesis/observations",
    ],
)
def test_content_routes_refused_from_untrusted_network(app, path):
    assert _get(app, path, _LAN).status_code == 403


@pytest.mark.parametrize(
    "addr",
    [
        "127.0.0.1",
        "127.8.9.10",
        "::1",
        _TAILNET,
        "100.64.0.1",
        "100.127.255.254",
        "fd7a:115c:a1e0::1",
        "::ffff:127.0.0.1",
    ],
)
def test_content_routes_open_to_trusted_sources(app, addr):
    assert _get(app, "/api/genesis/knowledge/abc", addr).status_code == 200


@pytest.mark.parametrize(
    "addr",
    [
        "100.128.0.1",
        "100.63.255.255",
        "192.168.1.5",
        "8.8.8.8",
        "::ffff:192.0.2.10",
        "fe80::1",
        "fd7a:115c:a1e1::1",
    ],
)
def test_just_outside_the_trusted_ranges_is_refused(app, addr):
    """Boundary cells: the CGNAT /10 edges, a sibling ULA, a mapped LAN address."""
    assert _get(app, "/api/genesis/knowledge/abc", addr).status_code == 403


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/genesis/health", "GET"),
        ("/api/genesis/heartbeat", "GET"),
        ("/api/genesis/pause", "GET"),
        ("/api/genesis/guardian-dialogue", "POST"),
    ],
)
def test_supervisor_probe_routes_stay_open_from_anywhere(app, path, method):
    assert _get(app, path, _LAN, method=method).status_code == 200


def test_open_list_is_exact_match_not_prefix(app):
    """A route that merely STARTS with an open name is not open."""
    assert _get(app, "/api/genesis/healthz", _LAN).status_code == 403


@pytest.mark.parametrize("path", ["/v1/thing", "/genesis"])
def test_paths_outside_genesis_api_prefixes_are_not_scoped(app, path):
    """/v1 enforces its own bearer; HTML pages have the login gate."""
    assert _get(app, path, _LAN).status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_tool_api_is_scoped(app, method):
    """/api/t/ returns the same recall content, and its only auth (the mutation
    gate) is a no-op when no dashboard password is set -- as in this fixture."""
    assert _get(app, "/api/t/some_tool", _LAN, method=method).status_code == 403


def test_pause_post_is_not_open_from_anywhere(app):
    """The open list is method-scoped: reading pause state is open, the kill switch is not."""
    assert _get(app, "/api/genesis/pause", _LAN, method="POST").status_code == 403


def test_doubled_slash_does_not_slip_past(app):
    assert _get(app, "//api/genesis/memory/recent", _LAN).status_code != 200


def test_post_to_content_route_is_scoped_too(app):
    assert _get(app, "/api/genesis/memory/recent", _LAN, method="POST").status_code == 403


def test_missing_peer_address_is_refused(app):
    c = app.test_client()
    resp = c.get("/api/genesis/knowledge/abc", environ_base={"REMOTE_ADDR": ""})
    assert resp.status_code == 403


def test_operator_can_replace_trusted_networks(app, monkeypatch):
    monkeypatch.setenv("GENESIS_DASHBOARD_TRUSTED_NETWORKS", "192.0.2.0/24")
    assert _get(app, "/api/genesis/knowledge/abc", _LAN).status_code == 200
    # Replaced, not extended: the default tailnet range is no longer trusted ...
    assert _get(app, "/api/genesis/knowledge/abc", _TAILNET).status_code == 403
    # ... but loopback always is, so the install can never lock itself out.
    assert _get(app, "/api/genesis/knowledge/abc", "127.0.0.1").status_code == 200


def test_unparseable_override_degrades_to_loopback_only(app, monkeypatch):
    """A typo must narrow access, never widen it."""
    monkeypatch.setenv("GENESIS_DASHBOARD_TRUSTED_NETWORKS", "not-a-network, 999.1.1.1/8")
    assert _get(app, "/api/genesis/knowledge/abc", _TAILNET).status_code == 403
    assert _get(app, "/api/genesis/knowledge/abc", _LAN).status_code == 403
    assert _get(app, "/api/genesis/knowledge/abc", "127.0.0.1").status_code == 200


def test_kill_switch_disables_the_gate(app, monkeypatch):
    monkeypatch.setenv("GENESIS_DASHBOARD_NETWORK_SCOPE", "off")
    assert _get(app, "/api/genesis/knowledge/abc", _LAN).status_code == 200


def test_gate_registered_once_on_double_apply(app):
    """apply_api_mutation_gate is idempotent; the scope gate must not stack."""
    auth_mod.apply_api_mutation_gate(app)
    funcs = app.before_request_funcs.get(None, [])
    assert funcs.count(auth_mod.check_api_network_scope) == 1
