"""Every transport claim is subordinate to a distinct scoped credential."""

import asyncio
import os
import secrets

import pytest
from flask import Flask

from genesis.peers.auth import (
    BACKEND_TOKEN,
    PeerRefusal,
    authenticate,
    configuration_warning,
    credential_conflicts,
)


async def prepare(registry, monkeypatch, mode="fallback"):
    # Values stay inside fixtures and never become assertion messages.
    for name in tuple(os.environ):
        if name.startswith("GENESIS_") and name.endswith("_TOKEN"):
            monkeypatch.delenv(name)
    credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("GENESIS_PEER_MUSE_TOKEN", credential)
    monkeypatch.setenv(BACKEND_TOKEN, secrets.token_urlsafe(32))
    await registry.configure(
        mode, "realm" if mode == "sam" else None, "https://genesis.example/v1/agent/a2a"
    )
    await registry.register(
        "muse",
        same_owner=True,
        daily_allowance=3,
        token_name="GENESIS_PEER_MUSE_TOKEN",
        sam_realm="realm",
        sam_node="node",
        principal="principal",
    )
    return credential


async def refused(registry, headers, code, *, probe=False):
    with Flask(__name__).test_request_context(headers=headers):
        with pytest.raises(PeerRefusal) as caught:
            await authenticate(registry, allow_probe=probe)
        assert caught.value.code == code


async def test_disabled_missing_bad_and_non_ascii_fail_closed(registry, monkeypatch):
    await refused(registry, {}, "not_configured")
    credential = await prepare(registry, monkeypatch)
    await refused(registry, {}, "unauthorized")
    await refused(
        registry, {"Authorization": "Bearer " + secrets.token_urlsafe(32)}, "unauthorized"
    )
    monkeypatch.delenv("GENESIS_PEER_MUSE_TOKEN")
    await refused(registry, {"Authorization": "Bearer " + credential}, "not_configured")
    monkeypatch.setenv("GENESIS_PEER_MUSE_TOKEN", "\u00f6")
    await refused(registry, {}, "not_configured")


async def test_fallback_ignores_spoofed_identity_and_revoke_is_immediate(registry, monkeypatch):
    credential = await prepare(registry, monkeypatch)
    headers = {
        "Authorization": "Bearer " + credential,
        "X-Peer-Id": "other",
        "X-Sam-Principal": "other",
    }
    with Flask(__name__).test_request_context(headers=headers):
        identity = await authenticate(registry)
        assert identity.peer["peer_id"] == "muse"
    await registry.revoke("muse")
    await refused(registry, headers, "not_configured")


@pytest.mark.parametrize("mode", ["fallback", "sam"])
async def test_scope_collapse_refuses_activation(registry, monkeypatch, mode):
    credential = await prepare(registry, monkeypatch, mode)
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", credential)
    await refused(registry, {}, "not_configured", probe=True)


async def test_sam_backend_first_pinned_node_and_optional_probe(registry, monkeypatch):
    await prepare(registry, monkeypatch, "sam")
    headers = {"X-Peer-Id": "node", "X-Sam-Principal": "principal"}
    await refused(registry, headers, "unauthorized")
    headers["Authorization"] = "Bearer " + os.environ[BACKEND_TOKEN]
    for key in ("X-Peer-Id", "X-Sam-Principal"):
        altered = {**headers, key: "other"}
        await refused(registry, altered, "unauthorized")
    with Flask(__name__).test_request_context(headers=headers):
        assert (await authenticate(registry)).peer["peer_id"] == "muse"
    probe_headers = {"Authorization": headers["Authorization"]}
    await refused(registry, probe_headers, "unauthorized")
    with Flask(__name__).test_request_context(headers=probe_headers):
        assert (await authenticate(registry, allow_probe=True)).peer is None


async def test_transaction_independence_under_parallel_readers(registry, monkeypatch):
    await prepare(registry, monkeypatch)

    async def read():
        return (await registry.get("muse"))["peer_id"]

    assert await asyncio.gather(*(read() for _ in range(8))) == ["muse"] * 8


async def test_revoked_only_credential_warns_and_refuses(registry, monkeypatch):
    credential = await prepare(registry, monkeypatch)
    assert await configuration_warning(registry) is None
    await registry.revoke("muse")
    assert await configuration_warning(registry) is not None
    await refused(registry, {"Authorization": "Bearer " + credential}, "not_configured")


@pytest.mark.parametrize("configured", [False, True])
async def test_revoked_credential_does_not_mask_active_peer_readiness(
    registry, monkeypatch, configured
):
    await prepare(registry, monkeypatch)
    name = "GENESIS_PEER_OTHER_TOKEN"
    await registry.register("other", same_owner=True, token_name=name)
    credential = secrets.token_urlsafe(32)
    if configured:
        monkeypatch.setenv(name, credential)
    await registry.revoke("muse")
    warning = await configuration_warning(registry)
    assert (warning is None) is configured
    with Flask(__name__).test_request_context(
        headers={"Authorization": "Bearer " + credential}
    ):
        if configured:
            assert (await authenticate(registry)).peer["peer_id"] == "other"
        else:
            with pytest.raises(PeerRefusal) as caught:
                await authenticate(registry)
            assert caught.value.code == "not_configured"


@pytest.mark.parametrize("mode", ["fallback", "sam"])
@pytest.mark.parametrize("password_kind", ["same", "padded", "distinct", "empty", "unicode"])
async def test_dashboard_password_cannot_be_peer_authority(registry, monkeypatch, mode, password_kind):
    await prepare(registry, monkeypatch, mode)
    name = "GENESIS_PEER_MUSE_TOKEN" if mode == "fallback" else BACKEND_TOKEN
    value = os.environ[name]
    passwords = {
        "same": value,
        "padded": "  " + value + "  ",
        "distinct": secrets.token_urlsafe(32),
        "empty": "   ",
        "unicode": "\N{SNOWMAN}",
    }
    monkeypatch.setenv("DASHBOARD_PASSWORD", passwords[password_kind])
    collision = password_kind in {"same", "padded"}
    assert credential_conflicts((name,)) is collision
    if collision:
        assert await configuration_warning(registry) is not None
        await refused(registry, {"Authorization": "Bearer " + value}, "not_configured", probe=True)
        await registry.revoke("muse")
        assert credential_conflicts((name,))
    else:
        assert await configuration_warning(registry) is None
        with Flask(__name__).test_request_context(headers={"Authorization": "Bearer " + value}):
            identity = await authenticate(registry, allow_probe=True)
        assert identity.credential_name == name


@pytest.mark.parametrize("mode", ["fallback", "sam"])
@pytest.mark.parametrize(
    "key_kind", ["active", "fallback", "bytes", "distinct", "unicode", "empty", "no_fallback"]
)
async def test_loaded_signing_keys_cannot_be_peer_authority(registry, monkeypatch, mode, key_kind):
    await prepare(registry, monkeypatch, mode)
    name = "GENESIS_PEER_MUSE_TOKEN" if mode == "fallback" else BACKEND_TOKEN
    value = os.environ[name]
    app = Flask(__name__)
    app.secret_key = secrets.token_urlsafe(32)
    app.config["SECRET_KEY_FALLBACKS"] = None
    if key_kind == "active":
        app.secret_key = value
    elif key_kind == "fallback":
        app.config["SECRET_KEY_FALLBACKS"] = [value]
    elif key_kind == "bytes":
        app.secret_key = value.encode("ascii")
    elif key_kind == "unicode":
        app.secret_key = secrets.token_urlsafe(32) + chr(0x2603)
        app.config["SECRET_KEY_FALLBACKS"] = ["別の鍵"]
    elif key_kind == "empty":
        app.secret_key = None
    elif key_kind == "distinct":
        app.config["SECRET_KEY_FALLBACKS"] = [secrets.token_urlsafe(32)]
    collision = key_kind in {"active", "fallback", "bytes"}
    with app.test_request_context(headers={"Authorization": "Bearer " + value}):
        assert credential_conflicts((name,)) is collision
        if collision:
            with pytest.raises(PeerRefusal) as caught:
                await authenticate(registry, allow_probe=True)
            assert caught.value.code == "not_configured"
        else:
            identity = await authenticate(registry, allow_probe=True)
            assert identity.credential_name == name
    # Standalone boot runs without an implicit Flask context.
    assert (await configuration_warning(registry, app=app) is not None) is collision


@pytest.mark.parametrize("persisted", [False, True])
async def test_signing_collision_uses_loaded_key_not_file(registry, monkeypatch, tmp_path, persisted):
    from pathlib import Path

    from genesis.dashboard.auth import get_or_create_secret_key

    await prepare(registry, monkeypatch)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    key_file = tmp_path / ".genesis" / "flask_secret_key"
    if persisted:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(secrets.token_hex(32))
    app = Flask(__name__)
    app.secret_key = get_or_create_secret_key()
    monkeypatch.setenv("GENESIS_PEER_MUSE_TOKEN", app.secret_key)
    key_file.write_text(secrets.token_hex(32))
    assert await configuration_warning(registry, app=app) is not None
    with app.test_request_context(headers={"Authorization": "Bearer " + app.secret_key}):
        with pytest.raises(PeerRefusal) as caught:
            await authenticate(registry)
        assert caught.value.code == "not_configured"
    # A file changed to a peer value does not replace the live app key either.
    distinct = secrets.token_urlsafe(32)
    monkeypatch.setenv("GENESIS_PEER_MUSE_TOKEN", distinct)
    key_file.write_text(distinct)
    assert await configuration_warning(registry, app=app) is None
    with app.test_request_context(headers={"Authorization": "Bearer " + distinct}):
        identity = await authenticate(registry)
    assert identity.credential_name == "GENESIS_PEER_MUSE_TOKEN"


async def test_sam_backend_probe_readiness_survives_peer_revocation(registry, monkeypatch):
    await prepare(registry, monkeypatch, "sam")
    await registry.revoke("muse")
    assert await configuration_warning(registry) is None
    with Flask(__name__).test_request_context(
        headers={"Authorization": "Bearer " + os.environ[BACKEND_TOKEN]}
    ):
        assert (await authenticate(registry, allow_probe=True)).peer is None
