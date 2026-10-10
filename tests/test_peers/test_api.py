"""Flask requests cross the actual runtime loop and scoped auth boundary."""

import asyncio
import io
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


@pytest.mark.parametrize("path", ["/health", "/.well-known/agent-card.json", "/approvals", "/unknown"])
async def test_disabled_every_path_and_options(app, path):
    client = app.test_client()
    for method in ("GET", "POST", "OPTIONS", "TRACE"):
        response = client.open(ROOT + path, method=method)
        assert response.status_code == 503
        assert response.json["code"] == "not_configured"


async def test_health_auth_card_url_and_audit_do_not_disclose(app, registry, monkeypatch, caplog):
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
    assert card.status_code == 200
    assert card.json["supportedInterfaces"][0]["url"] == "https://genesis.example/v1/agent/a2a"
    assert card.json["skills"] == []
    assert (
        card.json["securitySchemes"]["peerBearer"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    )
    assert "peerBearer" in card.json["securityRequirements"][0]["schemes"]
    response = client.post(
        ROOT + "/message:send",
        headers={**headers, "A2A-Version": "1.0"},
        json={
            "message": {"messageId": "probe", "role": "ROLE_USER", "parts": [{"text": "Probe"}]},
            "configuration": {"returnImmediately": True},
        },
    )
    assert response.status_code == 503 and response.json["code"] == "not_ready"


async def test_body_cap_refuses_without_content_length_after_auth(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    client = app.test_client()
    for with_length in (False, True):
        options = {
            "wsgi.input": io.BytesIO(b"x" * (MAX_BODY_BYTES + 1)),
            "wsgi.input_terminated": True,
        }
        if with_length:
            options["CONTENT_LENGTH"] = str(MAX_BODY_BYTES + 1)
        response = client.open(ROOT + "/health", headers=headers, environ_overrides=options)
        assert response.status_code == 413
    assert client.get(ROOT + "/health", data=b"x" * (MAX_BODY_BYTES + 1)).status_code == 401


async def test_loop_absent_never_falls_back(app, registry, monkeypatch):
    headers = await configure(registry, monkeypatch)
    app.config.pop("GENESIS_EVENT_LOOP")
    response = app.test_client().get(ROOT + "/health", headers=headers)
    assert response.status_code == 503 and response.json["code"] == "not_ready"


@pytest.fixture
async def task_app(app, registry, monkeypatch):
    from genesis.db.schema import TABLES
    from genesis.peers.tasks import PeerTasks

    headers = await configure(registry, monkeypatch)
    headers["A2A-Version"] = "1.0"
    await registry.grant("muse", "conversation", "allow")
    async with registry.connection() as db:
        for name in (
            "direct_session_queue",
            "peer_tasks",
            "peer_receipts",
            "peer_daily_admissions",
        ):
            await db.execute(TABLES[name])
        await db.commit()
    app.config["GENESIS_PEER_TASKS"] = PeerTasks(registry)
    return app.test_client(), headers


def task_message():
    return {
        "message": {
            "messageId": "http-one",
            "role": "ROLE_USER",
            "parts": [{"text": "Please help"}],
        },
        "configuration": {"returnImmediately": True},
    }


async def test_owned_task_send_retry_get_list_cancel(task_app):
    client, headers = task_app
    first = client.post(ROOT + "/message:send", headers=headers, json=task_message())
    assert first.status_code == 200 and first.mimetype == "application/a2a+json"
    task_id = first.json["task"]["id"]
    retry = client.post(ROOT + "/message:send", headers=headers, json=task_message())
    assert retry.json["task"]["id"] == task_id
    fetched = client.get(ROOT + "/tasks/" + task_id, headers=headers)
    assert fetched.json["id"] == task_id
    assert fetched.json["status"]["state"] == "TASK_STATE_SUBMITTED"
    listed = client.get(ROOT + "/tasks", headers=headers)
    assert listed.json["tasks"][0]["id"] == task_id and listed.json["pageSize"] == 20
    canceled = client.post(
        ROOT + "/tasks/" + task_id + ":cancel", headers=headers, json={"id": task_id}
    )
    assert canceled.status_code == 200 and canceled.json["status"]["state"] == "TASK_STATE_CANCELED"
    assert client.get(ROOT + "/tasks/unknown", headers=headers).status_code == 404
    assert (
        client.post(
            ROOT + "/tasks/unknown:cancel", headers=headers, json={"id": "unknown"}
        ).status_code
        == 404
    )


async def test_task_routes_keep_auth_first_and_reject_unsupported_parameters(task_app):
    client, headers = task_app
    paths = [
        ("POST", "/message:send"),
        ("GET", "/tasks"),
        ("GET", "/tasks/unknown"),
        ("POST", "/tasks/unknown:cancel"),
    ]
    for method, path in paths:
        assert client.open(ROOT + path, method=method, data=b"bad").status_code == 401
        assert (
            client.open(
                ROOT + path, method=method, data=b"x" * (MAX_BODY_BYTES + 1), headers=headers
            ).status_code
            == 413
        )
    for query in (
        "?pageSize=0",
        "?pageSize=101",
        "?pageSize=bad",
        "?pageToken=bad!",
        "?pageSize=2&pageSize=3",
        "?status=working",
    ):
        assert client.get(ROOT + "/tasks" + query, headers=headers).status_code == 400
    assert (
        client.post(
            ROOT + "/tasks/unknown:cancel", headers=headers, json={"id": "different"}
        ).status_code
        == 400
    )
    missing_version = {name: value for name, value in headers.items() if name != "A2A-Version"}
    assert (
        client.post(
            ROOT + "/message:send", headers=missing_version, json=task_message()
        ).status_code
        == 400
    )
    assert client.get(ROOT + "/tasks", headers=headers).json["tasks"] == []


async def test_finished_task_cancel_uses_sdk_error_and_grant_revocation_hides(task_app, registry):
    client, headers = task_app
    first = client.post(ROOT + "/message:send", headers=headers, json=task_message())
    task_id = first.json["task"]["id"]
    async with registry.transaction() as db:
        await db.execute(
            "UPDATE peer_tasks SET state='completed',slot_reserved=0 WHERE id=?", (task_id,)
        )
    assert (
        client.post(
            ROOT + "/tasks/" + task_id + ":cancel", headers=headers, json={"id": task_id}
        ).status_code
        == 400
    )
    await registry.grant("muse", "conversation", "deny")
    assert client.get(ROOT + "/tasks/" + task_id, headers=headers).status_code == 404
    assert client.get(ROOT + "/tasks", headers=headers).json["tasks"] == []
