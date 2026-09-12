"""Tests for the first-run wizard dashboard routes: setup-status + keys/test.

setup-status must derive from PERSISTED state (secrets.env file + identity files +
the live floor) rather than os.environ — so a just-saved value shows before a
restart. It exposes the live functional floor (cc_oauth / llm_key_present /
embedding_key_present / floor_met). The wizard writes NO ~/.genesis/setup-complete
marker (that endpoint was removed). The key-test endpoint must not forward a
caller-supplied base_url (SSRF guard).
"""

from __future__ import annotations

from types import SimpleNamespace

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
    # readiness reads the RAW secrets.env text (T2 manual parse) via its OWN
    # secrets_path import — patch it too so T2 sees the temp file, not the real box.
    import genesis.onboarding.readiness as readiness_mod

    monkeypatch.setattr(readiness_mod, "secrets_path", lambda: secrets_file)
    # Default: CC is logged in. Individual tests override for floor-leg coverage.
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: True)

    # The route lazy-imports load_ego_config from its source module — patch it there.
    # Default: ego loop OFF (so tier hinges on what each test configures); cadence at the
    # shipped 60 so the enrichment field is deterministic.
    import genesis.ego.config as ego_config_mod

    monkeypatch.setattr(
        ego_config_mod,
        "load_ego_config",
        lambda *a, **k: SimpleNamespace(enabled=False, cadence_minutes=60),
    )

    # The route also lazy-imports the autonomy default-level reader — patch it at the
    # source so the enrichment field is hermetic (not coupled to config/autonomy.yaml).
    import genesis.autonomy.config_read as autonomy_cfg_mod

    monkeypatch.setattr(autonomy_cfg_mod, "read_autonomy_default_level", lambda *a, **k: 1)

    return {
        "client": app.test_client(),
        "identity": identity,
        "secrets": secrets_file,
        "marker": marker,
        "monkeypatch": monkeypatch,
        "ego_config": ego_config_mod,
        "autonomy_cfg": autonomy_cfg_mod,
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
        # readiness fields (additive) — fresh box is Bootstrapped/T0.
        "tier": 0,
        "tier_name": "Bootstrapped",
        "telegram_configured": False,
        "ego_enabled": False,
        # enrichment fields (additive, non-gating) — nothing configured on a fresh box.
        "web_search_keyed_providers": [],
        "voice_configured": False,
        "ego_cadence_minutes": 60,
        "autonomy_level": 1,
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
    # LLM (groq) + embedding (deepinfra) present AND cc_oauth True → floor met.
    wiz["secrets"].write_text("API_KEY_GROQ=g\nAPI_KEY_DEEPINFRA=d\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["floor_met"] is True
    # Drop CC login → floor no longer met even with both keys.
    wiz["monkeypatch"].setattr(floor_mod, "cc_oauth_present", lambda: False)
    assert wiz["client"].get("/api/genesis/setup-status").get_json()["floor_met"] is False


def test_setup_status_anthropic_does_not_count_as_llm(wiz):
    # ANTHROPIC_API_KEY is not routing-consumed → must not register as an LLM key.
    wiz["secrets"].write_text("ANTHROPIC_API_KEY=sk-ant\nAPI_KEY_DEEPINFRA=d\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["llm_key_present"] is False
    assert body["embedding_key_present"] is True  # deepinfra is a real embedding backend
    assert body["floor_met"] is False


def test_setup_status_embedding_leg_only_real_backends(wiz):
    # Only DeepInfra / Qwen are real embedding backends. Voyage (rerank-only) and a
    # bare OpenAI/Google key must NOT satisfy the embedding leg.
    wiz["secrets"].write_text("API_KEY_VOYAGE=v\nOPENAI_API_KEY=o\nGOOGLE_API_KEY=g\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["embedding_key_present"] is False
    assert body["llm_key_present"] is True  # google (gemini-free) is chain-referenced
    # A real embedding backend flips it.
    wiz["secrets"].write_text("API_KEY_QWEN=q\n")
    assert (
        wiz["client"].get("/api/genesis/setup-status").get_json()["embedding_key_present"] is True
    )


def test_setup_complete_endpoint_removed(wiz):
    # The wizard no longer writes the system marker; the endpoint must not exist.
    resp = wiz["client"].post("/api/genesis/setup-complete")
    assert resp.status_code in (404, 405)
    assert not wiz["marker"].exists()


def test_setup_status_onboarded_reflects_marker_only(wiz):
    # `onboarded` mirrors the bootstrap marker; it is decoupled from floor_met.
    wiz["marker"].write_text("2026-08-03\n")
    wiz["secrets"].write_text("API_KEY_DEEPINFRA=dk\n")
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


def _functional_secrets() -> str:
    # cc_oauth is True via the fixture; groq (LLM) + deepinfra (embedding) → floor met.
    return "API_KEY_GROQ=g\nAPI_KEY_DEEPINFRA=d\n"


def test_setup_status_tier2_connected_needs_token_and_allowed_users(wiz):
    # Floor met + a valid Telegram proactive-reach config (token + numeric UID),
    # ego OFF → Connected (T2).
    wiz["secrets"].write_text(
        _functional_secrets() + "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    )
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["floor_met"] is True
    assert body["telegram_configured"] is True
    assert body["ego_enabled"] is False
    assert body["tier"] == 2
    assert body["tier_name"] == "Connected"


def test_setup_status_token_without_allowed_users_stays_tier1(wiz):
    # A bot token but no valid recipient can't proactively reach → NOT Connected.
    wiz["secrets"].write_text(_functional_secrets() + "TELEGRAM_BOT_TOKEN=abc\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["telegram_configured"] is False
    assert body["tier"] == 1


def test_setup_status_tier3_autonomous_needs_ego_and_marker(wiz):
    # Floor + telegram + ego loop enabled + bootstrap marker → Autonomous (T3).
    wiz["secrets"].write_text(
        _functional_secrets() + "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    )
    wiz["monkeypatch"].setattr(
        wiz["ego_config"], "load_ego_config", lambda *a, **k: SimpleNamespace(enabled=True)
    )
    # Without the setup-complete marker the ego cadence can't run → capped at T2,
    # and `onboarded` must not be True alongside an Autonomous tier.
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["ego_enabled"] is True
    assert body["onboarded"] is False
    assert body["tier"] == 2

    # With the marker present, the same config reaches Autonomous.
    wiz["marker"].write_text("2026-08-06\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["onboarded"] is True
    assert body["tier"] == 3
    assert body["tier_name"] == "Autonomous"


def test_setup_status_ego_config_unreadable_is_failsafe_not_500(wiz):
    # An ego config that raises must degrade to ego_enabled=False, NEVER 500 the
    # first-run route.
    def _boom(*a, **k):
        raise RuntimeError("ego.yaml corrupt")

    wiz["monkeypatch"].setattr(wiz["ego_config"], "load_ego_config", _boom)
    wiz["secrets"].write_text(
        _functional_secrets() + "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    )
    resp = wiz["client"].get("/api/genesis/setup-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ego_enabled"] is False
    assert body["tier"] == 2  # capped at Connected without ego
    assert body["ego_cadence_minutes"] == 60  # cadence also fails safe to the default


def test_setup_status_enrichment_fields_reflect_config(wiz):
    # A premium web-search key + deliberate voice opt-in + custom ego cadence + a
    # non-default autonomy level must all surface through the route (non-gating).
    wiz["secrets"].write_text(
        _functional_secrets() + "API_KEY_BRAVE=bk\nVOICE_S2S_PROVIDER=openai\nOPENAI_API_KEY=sk\n"
    )
    wiz["monkeypatch"].setattr(
        wiz["ego_config"],
        "load_ego_config",
        # A fractional cadence must survive the route + jsonify unchanged (not truncated).
        lambda *a, **k: SimpleNamespace(enabled=False, cadence_minutes=45.5),
    )
    wiz["monkeypatch"].setattr(
        wiz["autonomy_cfg"], "read_autonomy_default_level", lambda *a, **k: 2
    )
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["web_search_keyed_providers"] == ["brave"]
    assert body["voice_configured"] is True
    assert body["ego_cadence_minutes"] == 45.5
    assert body["autonomy_level"] == 2
    # Enrichment is non-gating: these do not change the floor/tier.
    assert body["floor_met"] is True


def test_setup_status_overflow_cadence_is_failsafe_not_500(wiz):
    # A user-overridden ego.yaml with `cadence_minutes: .inf` yields a float infinity;
    # int(inf) raises OverflowError. compute_enrichment runs OUTSIDE the route's ego
    # try/except, so this must be caught there (never 500 first-run) and fall back to 60.
    wiz["monkeypatch"].setattr(
        wiz["ego_config"],
        "load_ego_config",
        lambda *a, **k: SimpleNamespace(enabled=True, cadence_minutes=float("inf")),
    )
    wiz["secrets"].write_text(_functional_secrets())
    resp = wiz["client"].get("/api/genesis/setup-status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ego_cadence_minutes"] == 60
    assert body["ego_enabled"] is True  # the bad cadence must not disturb ego/tier


def test_setup_status_openai_llm_key_alone_is_not_voice(wiz):
    # Regression guard: s2s_provider() defaults to "openai", so a bare OPENAI_API_KEY
    # (an LLM key) must NOT read as deliberate voice setup.
    wiz["secrets"].write_text(_functional_secrets() + "OPENAI_API_KEY=sk\n")
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert body["voice_configured"] is False


def test_setup_status_readiness_fields_backward_compatible(wiz):
    # The original floor fields must be unchanged/still present alongside the new ones.
    wiz["secrets"].write_text(_functional_secrets())
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    for key in (
        "onboarded",
        "password_set",
        "cc_oauth",
        "llm_key_present",
        "embedding_key_present",
        "floor_met",
        "identity_set",
    ):
        assert key in body
    for key in ("tier", "tier_name", "telegram_configured", "ego_enabled"):
        assert key in body
    for key in (
        "web_search_keyed_providers",
        "voice_configured",
        "ego_cadence_minutes",
        "autonomy_level",
    ):
        assert key in body


def test_setup_status_floor_computed_once_snapshot_consistent(wiz):
    # Regression (Codex #1318): the route must compute the floor ONCE. If
    # cc_oauth_present flips between calls, a double-computation would let floor_met
    # (payload) and the tier's floor basis disagree (e.g. floor_met=false + tier 3).
    calls = {"n": 0}

    def flaky_oauth():
        calls["n"] += 1
        return calls["n"] == 1  # True on the first read, False on any later read

    wiz["monkeypatch"].setattr(floor_mod, "cc_oauth_present", flaky_oauth)
    wiz["secrets"].write_text(
        _functional_secrets() + "TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_ALLOWED_USERS=12345\n"
    )
    body = wiz["client"].get("/api/genesis/setup-status").get_json()
    assert calls["n"] == 1, "floor must be computed exactly once per request"
    # One snapshot → floor_met and the tier agree (tier>=1 iff floor met).
    assert body["floor_met"] is True
    assert body["tier"] >= 1


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
