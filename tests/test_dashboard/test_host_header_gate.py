"""The app-level Host-header gate (check_host_header).

A request reaches the dashboard's content routes only when its ``Host`` names
this machine: an IP literal, ``localhost``, the machine's own names, its
Tailscale MagicDNS name, or a name the operator listed in
``GENESIS_DASHBOARD_ALLOWED_HOSTS``. A page that re-points its own DNS name at
the dashboard's address still sends ITS name as ``Host``, so the server refuses
it, while every real way of addressing the box keeps working.

Out of scope by design: ``/v1/*`` (every route carries its own bearer), static
assets (no stored content), and the supervisor's network-open routes
(``_OPEN_FROM_ANY_NETWORK``), which are reachable from anywhere already.
"""

from __future__ import annotations

import json
import logging
import subprocess

import pytest
from flask import Flask, jsonify

from genesis.dashboard import auth as auth_mod
from genesis.dashboard import host_gate as hg


@pytest.fixture()
def gate(monkeypatch, tmp_path):
    """A bare app with only the Host gate, plus stub routes on every scope class."""
    token_file = tmp_path / "internal_api_token"
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: token_file)
    monkeypatch.setattr(auth_mod, "_internal_token_cache", None)
    monkeypatch.delenv("GENESIS_DASHBOARD_HOSTNAME_CHECK", raising=False)
    monkeypatch.delenv("GENESIS_DASHBOARD_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(hg.socket, "gethostname", lambda: "genesis-box")
    monkeypatch.setattr(hg.socket, "getfqdn", lambda: "genesis-box.lan.example")
    # Default: no tailscale. Tests that need a MagicDNS name override this.
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: None)
    hg._reset_cache()

    app = Flask(__name__)
    app.secret_key = "k"
    app.before_request(hg.check_host_header)

    for path, methods in [
        ("/api/genesis/memory/recent", ["GET"]),
        ("/api/genesis/thing", ["GET", "POST"]),
        ("/api/genesis/health", ["GET"]),
        ("/api/genesis/heartbeat", ["GET"]),
        ("/api/genesis/pause", ["GET", "POST"]),
        ("/api/genesis/guardian-dialogue", ["POST"]),
        ("/api/t/tool", ["POST"]),
        ("/ws/terminal", ["GET"]),
        ("/genesis", ["GET"]),
        ("/genesis/monitor", ["GET"]),
        ("/genesis-ui/x.js", ["GET"]),
        ("/v1/voice/x", ["POST"]),
        ("/js/app.js", ["GET"]),
        ("/", ["GET"]),
    ]:
        app.add_url_rule(path, path, lambda: jsonify(ok=True), methods=methods)

    yield app.test_client()
    hg._reset_cache()


def _get(client, path, host, method="GET", **headers):
    if host is not None:
        headers["Host"] = host
    return client.open(path, method=method, headers=headers)


# ── Hosts that name this machine pass ──


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1:5000",
        "127.0.0.1",
        "192.0.2.10:5000",  # LAN IP
        "100.64.1.2:5000",  # Tailscale IP
        "[::1]:5000",
        "[fd7a:115c:a1e0::1]:5000",
        "localhost:5000",
        "LOCALHOST:5000",
        "genesis-box:5000",
        "Genesis-Box:5000",
        "genesis-box.local:5000",
        "genesis-box.lan.example:5000",
    ],
)
def test_own_hosts_pass(gate, host):
    assert _get(gate, "/api/genesis/memory/recent", host).status_code == 200


def test_missing_host_header_passes_and_ignores_server_name(gate):
    # No browser omits Host, so an absent header is a local tool, not a rebinding
    # page. request.host would fall back to SERVER_NAME here; the gate must read
    # the raw header instead, or SERVER_NAME=evil.example would decide.
    app = Flask(__name__)
    with app.test_request_context(
        "/api/genesis/thing", headers={}, environ_base={"SERVER_NAME": "evil.example"}
    ):
        from flask import request

        request.environ.pop("HTTP_HOST", None)
        assert hg.check_host_header() is None


# ── Foreign names are refused on every content route ──


@pytest.mark.parametrize(
    "host",
    ["evil.example:5000", "evil.example", "genesis-box.evil.example:5000", "x.local.evil.example"],
)
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/genesis/memory/recent", "GET"),
        ("/api/genesis/thing", "POST"),
        ("/api/t/tool", "POST"),
        ("/ws/terminal", "GET"),
        ("/genesis", "GET"),
        ("/genesis/monitor", "GET"),
    ],
)
def test_foreign_host_refused(gate, host, path, method):
    resp = _get(gate, path, host, method=method)
    assert resp.status_code == 403


def test_refusal_says_how_to_fix(gate):
    api = _get(gate, "/api/genesis/memory/recent", "evil.example:5000")
    assert "GENESIS_DASHBOARD_ALLOWED_HOSTS" in api.get_json()["error"]
    assert "IP address" in api.get_json()["error"]
    page = _get(gate, "/genesis", "evil.example:5000")
    assert page.mimetype == "text/plain"
    assert "GENESIS_DASHBOARD_ALLOWED_HOSTS" in page.get_data(as_text=True)


def test_empty_host_refused(gate):
    assert _get(gate, "/api/genesis/memory/recent", "").status_code == 403


@pytest.mark.parametrize(
    "host",
    [
        "localhost:5000@evil.example",  # userinfo-looking tail after a trusted name
        "localhost:abc",  # non-numeric port
        "1.2.3.4:abc",
        "1.2.3.4:5000:6000",  # two ports
        "localhost:0",  # out-of-range ports
        "local host",
        "[::1",  # unterminated bracket
        "evil.example/x",
    ],
)
def test_malformed_host_refused(gate, host):
    # A Host the gate cannot interpret is refused, never parsed around: the
    # IP/localhost fast path must not trust a prefix of a malformed header.
    assert _get(gate, "/api/genesis/memory/recent", host).status_code == 403


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("Box.Example.:5000", ("box.example", ":5000")),
        ("[FD7A::1]:443", ("fd7a::1", ":443")),
        ("localhost", ("localhost", "")),
        ("localhost:65535", ("localhost", ":65535")),
        ("localhost:65536", None),  # werkzeug's test client cannot send this one
        ("localhost:0", None),
        ("localhost:", None),
        ("[]:5000", None),
        ("a b", None),
    ],
)
def test_split_host(host, expected):
    assert hg._split_host(host) == expected


@pytest.mark.parametrize(
    "host", ["localhost.", "localhost.:5000", "genesis-box.:5000", "127.0.0.1.:5000"]
)
def test_trailing_dot_host_is_normalised(gate, host):
    # A fully-qualified name with DNS's root dot names the same machine; with the
    # dot dropped, "127.0.0.1." is the IP literal (no one can own a name under
    # a numeric top-level label, so this opens nothing a page could serve from).
    assert _get(gate, "/api/genesis/memory/recent", host).status_code == 200


def test_ws_scope_is_terminal_only(gate):
    # Only Genesis's own WebSocket route is gated; a host app's other /ws/*
    # routes (Agent Zero mode) are left alone.
    gate.application.add_url_rule("/ws/other", "/ws/other", lambda: jsonify(ok=True))
    assert _get(gate, "/ws/other", "evil.example").status_code == 200
    assert _get(gate, "/ws/terminal", "evil.example").status_code == 403


# ── Out-of-scope routes are untouched ──


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/v1/voice/x", "POST"),
        ("/js/app.js", "GET"),
        ("/", "GET"),
        ("/genesis-ui/x.js", "GET"),
        ("/api/genesis/health", "GET"),
        ("/api/genesis/heartbeat", "GET"),
        ("/api/genesis/pause", "GET"),
        ("/api/genesis/guardian-dialogue", "POST"),
    ],
)
def test_out_of_scope_routes_ignore_host(gate, path, method):
    assert _get(gate, path, "evil.example:5000", method=method).status_code == 200


def test_supervisor_exemption_is_method_scoped(gate):
    # POST /pause (the kill switch) is not in the open set, so it IS gated.
    assert _get(gate, "/api/genesis/pause", "evil.example:5000", method="POST").status_code == 403


def test_internal_bearer_passes(gate):
    token = auth_mod.get_or_create_internal_api_token()
    resp = _get(
        gate,
        "/api/genesis/thing",
        "evil.example:5000",
        method="POST",
        Authorization=f"Bearer {token}",
    )
    assert resp.status_code == 200


def test_wrong_bearer_still_refused(gate):
    auth_mod.get_or_create_internal_api_token()
    resp = _get(gate, "/api/genesis/thing", "evil.example:5000", Authorization="Bearer nope")
    assert resp.status_code == 403


# ── Operator allowlist and kill switch ──


def test_env_allowlist(gate, monkeypatch):
    monkeypatch.setenv("GENESIS_DASHBOARD_ALLOWED_HOSTS", " Dash.Example. , ,.home.example")
    hg._reset_cache()
    assert _get(gate, "/genesis", "dash.example:5000").status_code == 200
    assert _get(gate, "/genesis", "box.home.example").status_code == 200  # leading dot = suffix
    assert _get(gate, "/genesis", "home.example.evil.example").status_code == 403


def test_invalid_allowlist_entry_is_skipped_not_fatal(gate, monkeypatch, caplog):
    # werkzeug's matcher raises on an entry it cannot IDNA-encode ("a..b" has an
    # empty label); one bad entry must not turn every request into a 500.
    monkeypatch.setenv("GENESIS_DASHBOARD_ALLOWED_HOSTS", "a..b, myname.example")
    with caplog.at_level(logging.WARNING, logger=hg.logger.name):
        assert _get(gate, "/genesis", "myname.example").status_code == 200
        assert _get(gate, "/genesis", "genesis-box").status_code == 200
        assert _get(gate, "/genesis", "evil.example").status_code == 403
    assert any("ignoring invalid entry" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("val", ["off", "OFF", "0", "false", "no"])
def test_kill_switch(gate, monkeypatch, val):
    monkeypatch.setenv("GENESIS_DASHBOARD_HOSTNAME_CHECK", val)
    assert _get(gate, "/api/genesis/memory/recent", "evil.example:5000").status_code == 200


def test_unknown_kill_switch_value_keeps_gate_on(gate, monkeypatch):
    monkeypatch.setenv("GENESIS_DASHBOARD_HOSTNAME_CHECK", "maybe")
    assert _get(gate, "/api/genesis/memory/recent", "evil.example:5000").status_code == 403


# ── Tailscale MagicDNS name ──


def _ts_json(dnsname):
    return json.dumps({"Self": {"DNSName": dnsname}})


def test_magicdns_full_and_short_name_pass(gate, monkeypatch):
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: _ts_json("tsbox.tail0000.ts.net."))
    hg._reset_cache()
    assert (
        _get(gate, "/genesis", "tsbox.tail0000.ts.net").status_code == 200
    )  # trailing dot stripped
    assert _get(gate, "/genesis", "TSBOX.tail0000.ts.net:5000").status_code == 200
    assert _get(gate, "/genesis", "tsbox:5000").status_code == 200
    assert _get(gate, "/genesis", "other.tail0000.ts.net").status_code == 403


def test_ip_access_never_spawns_tailscale(gate, monkeypatch):
    calls = []
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: calls.append(1))
    hg._reset_cache()
    for host in ("127.0.0.1:5000", "[::1]:5000", "localhost", "genesis-box"):
        _get(gate, "/genesis", host)
    assert calls == []


def test_magicdns_success_is_cached(gate, monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return _ts_json("box.tail0000.ts.net.")

    monkeypatch.setattr(hg, "_run_tailscale_status", fake)
    hg._reset_cache()
    for _ in range(3):
        _get(gate, "/genesis", "evil.example")
    assert calls == [1]


def test_magicdns_failure_retries_after_interval(gate, monkeypatch):
    calls = []
    clock = [1000.0]
    monkeypatch.setattr(hg.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: calls.append(1))
    hg._reset_cache()
    _get(gate, "/genesis", "evil.example")
    _get(gate, "/genesis", "evil.example")
    assert len(calls) == 1  # failure remembered
    clock[0] += hg._MAGICDNS_RETRY_SECONDS + 1
    _get(gate, "/genesis", "evil.example")
    assert len(calls) == 2


@pytest.mark.parametrize(
    "payload", [None, "not json", "[]", '{"Self": {}}', '{"Self": {"DNSName": 7}}']
)
def test_bad_tailscale_output_is_not_fatal(gate, monkeypatch, payload):
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: payload)
    hg._reset_cache()
    assert _get(gate, "/genesis", "evil.example").status_code == 403
    assert _get(gate, "/genesis", "127.0.0.1").status_code == 200


def test_run_tailscale_status_handles_missing_binary(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(hg.subprocess, "run", boom)
    assert hg._run_tailscale_status() is None


def test_run_tailscale_status_handles_timeout(monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="tailscale", timeout=k.get("timeout"))

    monkeypatch.setattr(hg.subprocess, "run", slow)
    assert hg._run_tailscale_status() is None


# ── Refusals are observable, but not a log flood ──


def test_refusal_logged_once_per_interval(gate, caplog, monkeypatch):
    clock = [5000.0]
    monkeypatch.setattr(hg.time, "monotonic", lambda: clock[0])
    hg._reset_cache()
    with caplog.at_level(logging.WARNING, logger=hg.logger.name):
        for _ in range(5):
            _get(gate, "/genesis", "evil.example:5000")
    refused = [r for r in caplog.records if "Host" in r.getMessage()]
    assert len(refused) == 1
    assert "evil.example:5000" in refused[0].getMessage()


# ── Registration ──


def test_apply_dashboard_gates_registers_host_gate_first_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "t")
    monkeypatch.setattr(auth_mod, "_internal_token_cache", None)
    app = Flask(__name__)
    auth_mod.apply_dashboard_gates(app)
    auth_mod.apply_dashboard_gates(app)
    funcs = app.before_request_funcs[None]
    assert funcs[0] is hg.check_host_header
    assert funcs.count(hg.check_host_header) == 1
    assert funcs.count(auth_mod.check_api_network_scope) == 1


def test_standalone_app_carries_the_host_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "t")
    monkeypatch.setattr(auth_mod, "_internal_token_cache", None)
    monkeypatch.setattr(auth_mod, "get_or_create_secret_key", lambda: "k")
    from genesis.hosting.standalone import StandaloneAdapter

    app = StandaloneAdapter(host="127.0.0.1", port=0)._create_flask_app()
    assert hg.check_host_header in app.before_request_funcs[None]


def test_agent_zero_adapter_applies_dashboard_gates(monkeypatch):
    import genesis.dashboard.heartbeat as hb_mod
    from genesis.hosting.agent_zero.adapter import AgentZeroAdapter

    monkeypatch.setattr(hb_mod.DashboardHeartbeat, "start", lambda self: None)
    seen = {}
    monkeypatch.setattr(auth_mod, "apply_dashboard_gates", lambda app: seen.setdefault("app", app))
    app = Flask(__name__)
    AgentZeroAdapter().register_blueprints(app)
    assert seen.get("app") is app


# ── Names from the OS or Tailscale that werkzeug cannot encode ──


def test_unencodable_os_hostname_does_not_break_the_gate(gate, monkeypatch):
    # "a..b" has an empty label: werkzeug's matcher would raise UnicodeError on
    # it for every request. It is dropped; the gate keeps answering.
    monkeypatch.setattr(hg.socket, "gethostname", lambda: "a..b")
    monkeypatch.setattr(hg.socket, "getfqdn", lambda: "a..b")
    hg._reset_cache()
    assert _get(gate, "/genesis", "evil.example").status_code == 403
    assert _get(gate, "/genesis", "localhost:5000").status_code == 200


def test_unencodable_magicdns_name_does_not_break_the_gate(gate, monkeypatch):
    monkeypatch.setattr(hg, "_run_tailscale_status", lambda: _ts_json("a..b.tail0000.ts.net."))
    hg._reset_cache()
    assert _get(gate, "/genesis", "evil.example").status_code == 403
    assert _get(gate, "/genesis", "genesis-box").status_code == 200
