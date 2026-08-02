"""Tests for the first-run wizard dashboard routes: setup-status + keys/test.

setup-status must derive from PERSISTED state (secrets.env file + identity files
+ marker), never os.environ — so a just-saved value shows before a restart. The
key-test endpoint must not forward a caller-supplied base_url (SSRF guard).
"""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def wiz(tmp_path, monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    import genesis.dashboard.routes.setup as setup_mod

    identity = tmp_path / "identity"
    identity.mkdir()
    secrets_file = tmp_path / "secrets.env"
    marker = tmp_path / "setup-complete"
    monkeypatch.setattr(setup_mod, "_IDENTITY_DIR", identity)
    monkeypatch.setattr(setup_mod, "_SETUP_COMPLETE_MARKER", marker)
    monkeypatch.setattr(setup_mod, "secrets_path", lambda: secrets_file)

    return {
        "client": app.test_client(),
        "identity": identity,
        "secrets": secrets_file,
        "marker": marker,
    }


def test_setup_status_fresh_install(wiz):
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body == {
        "onboarded": False,
        "password_set": False,
        "llm_key_present": False,
        "embedding_key_present": False,
        "identity_set": False,
    }


def test_setup_status_reads_persisted_secrets_not_env(wiz, monkeypatch):
    # A password + LLM key persisted to secrets.env must show as present even
    # though os.environ (stale until restart) does NOT contain them.
    monkeypatch.delenv("API_KEY_ANTHROPIC", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    wiz["secrets"].write_text("DASHBOARD_PASSWORD=pw\nAPI_KEY_ANTHROPIC=sk-xxx\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["password_set"] is True
    assert body["llm_key_present"] is True


def test_setup_status_marker_and_embedding(wiz):
    wiz["marker"].write_text("2026-08-02\n")
    wiz["secrets"].write_text("API_KEY_VOYAGE=vk\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["onboarded"] is True
    assert body["embedding_key_present"] is True


def test_setup_status_identity_set_only_when_differs_from_example(wiz):
    (wiz["identity"] / "USER.md.example").write_text("SEED TEMPLATE\n")
    # identical to example → not customized
    (wiz["identity"] / "USER.md").write_text("SEED TEMPLATE\n")
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["identity_set"] is False
    # customized → set
    (wiz["identity"] / "USER.md").write_text("I am the real user.\n")
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["identity_set"] is True


def test_keys_test_missing_fields_returns_400(wiz):
    resp = wiz["client"].post("/api/genesis/keys/test", json={"provider_type": "anthropic"})
    assert resp.status_code == 400


def test_keys_test_valid(wiz, monkeypatch):
    import genesis.observability.snapshots.api_keys as ak

    async def fake_test(provider_type, key, base_url=None):
        return {"valid": True}

    monkeypatch.setattr(ak, "test_single_key", fake_test)
    resp = wiz["client"].post(
        "/api/genesis/keys/test", json={"provider_type": "anthropic", "key": "sk"}
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"valid": True}


def test_keys_test_ignores_caller_base_url(wiz, monkeypatch):
    """SSRF guard: a caller-supplied base_url must NOT reach test_single_key."""
    import genesis.observability.snapshots.api_keys as ak

    seen = {}

    async def fake_test(provider_type, key, base_url=None):
        seen["base_url"] = base_url
        return {"valid": True}

    monkeypatch.setattr(ak, "test_single_key", fake_test)
    wiz["client"].post(
        "/api/genesis/keys/test",
        json={"provider_type": "zenmux", "key": "z", "base_url": "http://169.254.169.254/"},
    )
    # Endpoint calls test_single_key(provider_type, key) positionally → default.
    assert seen["base_url"] is None
