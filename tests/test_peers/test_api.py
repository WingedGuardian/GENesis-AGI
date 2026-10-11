"""Flask requests cross the actual runtime loop and scoped auth boundary."""

import asyncio
import secrets
import threading

import pytest
from flask import Flask

from genesis.dashboard.routes.agent_api import MAX_BODY_BYTES, ROOT, agent_api_bp


@pytest.fixture
async def app(registry, monkeypatch):
    loop = asyncio.new_event_loop()
    started = threading.Event()

    def run():
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run)
    thread.start()
    assert started.wait(5)
    app = Flask(__name__)
    app.config.update(GENESIS_EVENT_LOOP=loop, GENESIS_PEER_REGISTRY=registry)
    app.register_blueprint(agent_api_bp)
    try:
        yield app
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        assert not thread.is_alive()


async def configure(registry, monkeypatch):
    import os

    for name in tuple(os.environ):
        if name.startswith("GENESIS_") and name.endswith("_TOKEN"):
            monkeypatch.delenv(name)
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("GENESIS_PEER_MUSE_TOKEN", credential)
    await registry.configure("fallback", service_url="https://genesis.example/v1/agent/a2a")
    await registry.register(
        "muse", same_owner=True, daily_allowance=2, token_name="GENESIS_PEER_MUSE_TOKEN"
    )
    return {"Authorization": "Bearer " + credential}


@pytest.mark.parametrize("path", ["/health", "/.well-known/agent-card.json", "/unknown"])
async def test_disabled_every_path_and_options(app, path):
    client = app.test_client()
    for method in ("GET", "POST", "OPTIONS", "TRACE"):
        response = client.open(ROOT + path, method=method)
        assert response.status_code == 503
        assert response.json["code"] == "not_configured"


async def test_health_auth_and_withheld_card_audit_do_not_disclose(app, registry, monkeypatch, caplog):
    headers = await configure(registry, monkeypatch)
    client = app.test_client()
    assert client.get(ROOT + "/health").status_code == 401
    bad = {"Authorization": "Bearer " + secrets.token_urlsafe(32)}
    assert client.get(ROOT + "/health", headers=bad).status_code == 401
    with caplog.at_level("INFO"):
        response = client.get(ROOT + "/health", headers=headers)
    assert response.status_code == 200 and response.json["task_service_ready"] is False
    assert "GENESIS_PEER_MUSE_TOKEN" in caplog.text
    # Boolean comparison keeps values out of assertion diagnostics.
    assert not bool(headers["Authorization"][7:] in caplog.text)
    card = client.get(
        ROOT + "/.well-known/agent-card.json", headers={**headers, "Host": "evil.example"}
    )
    assert card.status_code == 503
    assert card.json["code"] == "not_ready"
    assert "supportedInterfaces" not in card.json
    assert client.post(ROOT + "/message:send", headers=headers).status_code == 404


class _UnreadableBody:
    """A request body that never ends: any read is a failure of the gate."""

    def __init__(self):
        self.reads = 0

    def _refuse(self, *args, **kwargs):
        self.reads += 1
        raise AssertionError("peer gate read the request body")

    read = readline = readlines = __iter__ = _refuse


def _body_environ(stream, length=None):
    environ = {"wsgi.input": stream, "wsgi.input_terminated": True}
    if length is not None:
        environ["CONTENT_LENGTH"] = str(length)
    return environ


async def test_slow_or_unterminated_body_is_never_read(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    client = app.test_client()
    for method, path, status in (
        ("GET", "/health", 200),
        ("POST", "/message:send", 404),
        ("GET", "/.well-known/agent-card.json", 503),
    ):
        stream = _UnreadableBody()
        response = client.open(
            ROOT + path,
            method=method,
            headers={**headers, "Transfer-Encoding": "chunked"},
            environ_overrides=_body_environ(stream),
        )
        assert response.status_code == status
        assert stream.reads == 0


async def test_declared_oversize_body_refused_from_header_after_auth(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    client = app.test_client()
    stream = _UnreadableBody()
    response = client.open(
        ROOT + "/health",
        headers=headers,
        environ_overrides=_body_environ(stream, MAX_BODY_BYTES + 1),
    )
    assert response.status_code == 413 and response.json["code"] == "body_too_large"
    assert stream.reads == 0
    stream = _UnreadableBody()
    response = client.open(
        ROOT + "/health",
        headers=headers,
        environ_overrides=_body_environ(stream, MAX_BODY_BYTES),
    )
    assert response.status_code == 200 and stream.reads == 0
    # Authorization still precedes the size refusal.
    stream = _UnreadableBody()
    response = client.open(
        ROOT + "/health", environ_overrides=_body_environ(stream, MAX_BODY_BYTES + 1)
    )
    assert response.status_code == 401
    assert stream.reads == 0


async def test_loop_absent_never_falls_back(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    app.config.pop("GENESIS_EVENT_LOOP")
    response = app.test_client().get(ROOT + "/health", headers=headers)
    assert response.status_code == 503 and response.json["code"] == "not_ready"


async def test_agent_card_withheld_until_transport_installed(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    client = app.test_client()
    response = client.get(ROOT + "/.well-known/agent-card.json", headers=headers)
    assert response.status_code == 503
    assert response.json["code"] == "not_ready"
    assert "supportedInterfaces" not in response.json
    assert client.get(ROOT + "/health", headers=headers).status_code == 200
