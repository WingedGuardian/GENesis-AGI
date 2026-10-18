"""The terminal WebSocket refuses a handshake that is not same-origin.

``/ws/terminal`` is a bash PTY. flask-sock completes the WebSocket handshake
inside the route wrapper (``Server(request.environ)``), BEFORE the handler body
and its password check run, so a check inside the handler cannot refuse an
upgrade. The guard is an app-level ``before_request`` registered by
``register_terminal_ws``; it answers 403 before any upgrade (RFC 6455 §10.2).

Over plain HTTP (the usual ``http://<ip>:5000`` dashboard), browsers send no
``Sec-Fetch-Site`` (fetch metadata is only sent to potentially trustworthy
URLs), so the ``Origin`` header, which a browser MUST send on a WebSocket
handshake (RFC 6455 §4.1), is the branch that decides most real requests.
"""

from __future__ import annotations

import threading
import time

import pytest
from flask import Flask
from werkzeug.serving import make_server

from genesis.dashboard import auth as auth_mod
from genesis.dashboard.routes import terminal as terminal_mod

pytest.importorskip("flask_sock")
simple_websocket = pytest.importorskip("simple_websocket")


class _FakeSession:
    """Stands in for the real PTY so no bash process is ever spawned."""

    instances: list[_FakeSession] = []

    def __init__(self, *args, **kwargs):
        type(self).instances.append(self)

    def start(self):
        pass

    def read(self, timeout=0.1):
        return None

    def is_alive(self):
        return True

    def write(self, data):
        pass

    def resize(self, rows, cols):
        pass

    def close(self):
        pass


@pytest.fixture()
def app(tmp_path, monkeypatch):
    token_file = tmp_path / "internal_api_token"
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: token_file)
    monkeypatch.setattr(auth_mod, "_internal_token_cache", None)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setattr(terminal_mod, "TerminalSession", _FakeSession)
    _FakeSession.instances = []
    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret-key"
    terminal_mod.register_terminal_ws(flask_app)
    return flask_app


def _upgrade_headers(**extra):
    headers = {
        "Connection": "Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    }
    headers.update(extra)
    return headers


# ── The hook is wired, and keyed on the route that actually dispatches ──


def test_register_installs_the_origin_hook(app):
    assert terminal_mod._refuse_cross_origin_terminal in app.before_request_funcs[None]


def test_terminal_endpoint_name_matches_the_hook(app):
    # The hook keys on the endpoint name; if flask-sock ever renamed it the hook
    # would silently stop matching, so pin the name here.
    rule = next(r for r in app.url_map.iter_rules() if r.rule == "/ws/terminal")
    assert rule.endpoint == terminal_mod._TERMINAL_ENDPOINT


# ── Refusals happen in before_request, before any upgrade ──


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},  # plain HTTP: no fetch metadata, foreign Origin
        {"Origin": "http://localhost:9749"},  # same host, another port: same-site sibling
        {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost:9749"},
        {},  # no origin signal at all: fail closed
    ],
)
@pytest.mark.parametrize("password", [None, "pw"])
def test_cross_origin_handshake_refused(app, monkeypatch, headers, password):
    if password:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)
    resp = app.test_client().get("/ws/terminal", headers=_upgrade_headers(**headers))
    assert resp.status_code == 403
    assert _FakeSession.instances == []


def test_refusal_does_not_depend_on_a_session(app, monkeypatch):
    # A logged-in cookie is attached on a same-site sibling origin (SameSite=Lax),
    # so being authenticated must not let a foreign Origin through.
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    resp = client.get("/ws/terminal", headers=_upgrade_headers(Origin="http://localhost:9749"))
    assert resp.status_code == 403


def test_internal_bearer_is_exempt(app):
    token = auth_mod.get_or_create_internal_api_token()
    resp = app.test_client().get(
        "/ws/terminal",
        headers=_upgrade_headers(
            **{"Authorization": f"Bearer {token}", "Origin": "http://evil.example"}
        ),
    )
    # Passes the hook; the flask test client has no raw socket, so the upgrade
    # itself fails further down. Anything but our 403 proves the hook let it by.
    assert resp.status_code != 403


@pytest.mark.parametrize(
    ("host", "headers"),
    [
        # A browser on plain HTTP: no fetch metadata, Origin carries the port.
        ("192.0.2.10:5000", {"Origin": "http://192.0.2.10:5000"}),
        ("100.64.1.2:5000", {"Origin": "http://100.64.1.2:5000"}),
        # A browser on HTTPS/localhost sends fetch metadata.
        ("localhost:5000", {"Sec-Fetch-Site": "same-origin", "Origin": "http://localhost:5000"}),
    ],
)
def test_browser_same_origin_passes_the_hook(app, host, headers):
    resp = app.test_client().get("/ws/terminal", headers=_upgrade_headers(Host=host, **headers))
    # Past the hook, the flask test client has no raw socket so the upgrade
    # itself fails further down; anything but our 403 proves the hook let it by.
    assert resp.status_code != 403


@pytest.mark.parametrize(
    ("scheme", "origin", "refused"),
    [
        # Over TLS, an http page on the same host is a different origin.
        ("https", "http://dash.example", True),
        ("https", "https://dash.example", False),
        # Plain HTTP as the server sees it (direct, or behind a TLS proxy).
        ("http", "http://dash.example", False),
        ("http", "https://dash.example", False),
        # Not a scheme this server can be.
        ("http", "ftp://dash.example", True),
        ("http", "chrome-extension://dash.example", True),
    ],
)
def test_origin_scheme_must_be_one_this_server_can_be(app, scheme, origin, refused):
    resp = app.test_client().get(
        "/ws/terminal",
        base_url=f"{scheme}://dash.example",
        headers=_upgrade_headers(Origin=origin),
    )
    assert (resp.status_code == 403) is refused


def test_hook_ignores_other_routes(app):
    @app.route("/other")
    def other():
        return "ok"

    assert (
        app.test_client().get("/other", headers={"Origin": "http://evil.example"}).status_code
        == 200
    )


# ── Real handshakes against a real werkzeug server ──


@pytest.fixture()
def served(app):
    srv = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"127.0.0.1:{srv.server_port}"
    srv.shutdown()
    thread.join(timeout=5)


def test_real_handshake_same_origin_connects(served):
    # simple_websocket's client sends Host WITHOUT the port (it hands
    # urlsplit(url).hostname to wsproto), unlike a browser, which includes a
    # non-default port (RFC 9110 §7.2). So match the Host this client sends.
    ws = simple_websocket.Client.connect(
        f"ws://{served}/ws/terminal", headers={"Origin": "http://127.0.0.1"}
    )
    try:
        assert ws.connected
        # The handler builds the session on the server thread after the
        # handshake returns to the client, so wait for it rather than race it.
        deadline = time.monotonic() + 10
        while not _FakeSession.instances and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        ws.close()
    assert len(_FakeSession.instances) == 1


@pytest.mark.parametrize("origin", ["http://evil.example", "http://127.0.0.1:1"])
def test_real_handshake_cross_origin_refused(served, origin):
    # simple_websocket's own ConnectionError, which is NOT the builtin one.
    with pytest.raises(simple_websocket.ConnectionError) as exc:
        simple_websocket.Client.connect(f"ws://{served}/ws/terminal", headers={"Origin": origin})
    assert exc.value.status_code == 403
    assert _FakeSession.instances == []
