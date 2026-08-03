"""Tests for the first-run wizard dashboard routes: setup-status + keys/test.

setup-status must derive from PERSISTED state (secrets.env file + identity files +
the live floor) rather than os.environ — so a just-saved value shows before a
restart. It exposes the live functional floor (cc_oauth / llm_key_present /
embedding_key_present / floor_met). The wizard writes NO ~/.genesis/setup-complete
marker (that endpoint was removed). The key-test endpoint must not forward a
caller-supplied base_url (SSRF guard).
"""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.onboarding import floor as floor_mod


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
    # setup.py reads secrets via the floor module now — patch it there.
    monkeypatch.setattr(floor_mod, "secrets_path", lambda: secrets_file)
    # Default: CC is logged in. Individual tests override for floor-leg coverage.
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: True)

    return {
        "client": app.test_client(),
        "identity": identity,
        "secrets": secrets_file,
        "marker": marker,
        "monkeypatch": monkeypatch,
    }


def test_setup_status_fresh_install(wiz):
    # Fresh box: no keys, no marker, CC not yet logged in.
    wiz["monkeypatch"].setattr(floor_mod, "cc_oauth_present", lambda: False)
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body == {
        "onboarded": False,
        "password_set": False,
        "cc_oauth": False,
        "llm_key_present": False,
        "embedding_key_present": False,
        "floor_met": False,
        "identity_set": False,
    }


def test_setup_status_reads_persisted_secrets_not_env(wiz, monkeypatch):
    # A password + LLM key persisted to secrets.env must show as present even
    # though os.environ (stale until restart) does NOT contain them.
    monkeypatch.delenv("API_KEY_OPENROUTER", raising=False)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    wiz["secrets"].write_text("DASHBOARD_PASSWORD=pw\nAPI_KEY_OPENROUTER=sk-xxx\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["password_set"] is True
    assert body["llm_key_present"] is True


def test_setup_status_floor_met_requires_all_three_legs(wiz):
    # LLM + embedding present AND cc_oauth True → floor met.
    wiz["secrets"].write_text("API_KEY_GROQ=g\nAPI_KEY_VOYAGE=v\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["floor_met"] is True
    # Drop CC login → floor no longer met even with both keys.
    wiz["monkeypatch"].setattr(floor_mod, "cc_oauth_present", lambda: False)
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["floor_met"] is False


def test_setup_status_anthropic_does_not_count_as_llm(wiz):
    # ANTHROPIC_API_KEY is not routing-consumed → must not register as an LLM key.
    wiz["secrets"].write_text("ANTHROPIC_API_KEY=sk-ant\nOPENAI_API_KEY=o\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["llm_key_present"] is False
    assert body["embedding_key_present"] is True  # OPENAI_API_KEY counts for embeddings
    assert body["floor_met"] is False


def test_setup_status_uses_canonical_registry_key_names(wiz):
    # The provider-suffix forms (GOOGLE_API_KEY / OPENAI_API_KEY) are canonical;
    # the API_KEY_* mis-forms must NOT count.
    wiz["secrets"].write_text("GOOGLE_API_KEY=g\nOPENAI_API_KEY=o\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["llm_key_present"] is True  # GOOGLE_API_KEY
    assert body["embedding_key_present"] is True  # OPENAI_API_KEY / GOOGLE_API_KEY
    wiz["secrets"].write_text("API_KEY_GOOGLE=g\n")  # wrong form
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["llm_key_present"] is False


def test_setup_complete_endpoint_removed(wiz):
    # The wizard no longer writes the system marker; the endpoint must not exist.
    resp = wiz["client"].post("/api/genesis/setup-complete")
    assert resp.status_code in (404, 405)
    assert not wiz["marker"].exists()


def test_setup_status_onboarded_reflects_marker_only(wiz):
    # `onboarded` mirrors the bootstrap marker; it is decoupled from floor_met.
    wiz["marker"].write_text("2026-08-03\n")
    wiz["secrets"].write_text("API_KEY_VOYAGE=vk\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["onboarded"] is True
    assert body["embedding_key_present"] is True
    assert body["floor_met"] is False  # marker set but no LLM key → still not functional


def test_setup_status_identity_set_only_when_differs_from_example(wiz):
    (wiz["identity"] / "USER.md.example").write_text("SEED TEMPLATE\n")
    # identical to example → not customized
    (wiz["identity"] / "USER.md").write_text("SEED TEMPLATE\n")
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["identity_set"] is False
    # customized → set
    (wiz["identity"] / "USER.md").write_text("I am the real user.\n")
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["identity_set"] is True


def test_keys_test_missing_fields_returns_400(wiz):
    resp = wiz["client"].post("/api/genesis/keys/test", json={"provider_type": "groq"})
    assert resp.status_code == 400


def test_keys_test_valid(wiz, monkeypatch):
    import genesis.observability.snapshots.api_keys as ak

    async def fake_test(provider_type, key, base_url=None):
        return {"valid": True}

    monkeypatch.setattr(ak, "test_single_key", fake_test)
    resp = wiz["client"].post("/api/genesis/keys/test", json={"provider_type": "groq", "key": "sk"})
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
