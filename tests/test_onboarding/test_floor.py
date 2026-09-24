"""Unit tests for the live functional floor (``genesis.onboarding.floor``).

The floor is the honest "is this Genesis functional" signal: CC OAuth login AND
≥1 routing-consumed LLM key AND ≥1 embedding key. It is computed live, never a
marker file. ANTHROPIC_API_KEY must NOT count toward the LLM leg (routing has no
``type: anthropic`` provider).
"""

from __future__ import annotations

import json

from genesis.onboarding import floor as floor_mod
from genesis.onboarding.floor import cc_oauth_present, compute_floor

# --------------------------------------------------------------------------
# cc_oauth_present() truth table
# --------------------------------------------------------------------------


def test_cc_oauth_env_token_overrides_everything(monkeypatch, tmp_path):
    # Explicit token env wins even if no credentials file exists anywhere.
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-abc")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is True


def test_cc_oauth_token_file(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tokfile = tmp_path / ".genesis" / "cc_oauth_token.env"
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text("CLAUDE_CODE_OAUTH_TOKEN=tok-from-file\n")
    assert cc_oauth_present() is True


def test_cc_oauth_token_file_empty_value_does_not_count(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path))
    tokfile = tmp_path / ".genesis" / "cc_oauth_token.env"
    tokfile.parent.mkdir(parents=True)
    tokfile.write_text("CLAUDE_CODE_OAUTH_TOKEN=\n")
    assert cc_oauth_present() is False


def test_cc_oauth_credentials_json_with_structural_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    (cfg / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is True


def test_cc_oauth_credentials_json_without_structural_key(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = tmp_path / "claude-cfg"
    cfg.mkdir()
    # An API-key-mode or otherwise-shaped file lacking claudeAiOauth → not logged in.
    (cfg / ".credentials.json").write_text(json.dumps({"somethingElse": True}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is False


def test_cc_oauth_absent_everywhere(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert cc_oauth_present() is False


def test_cc_oauth_home_credentials_when_no_config_dir(monkeypatch, tmp_path):
    # With CLAUDE_CONFIG_DIR unset, falls back to ~/.claude/.credentials.json.
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = tmp_path / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True)
    creds.write_text(json.dumps({"claudeAiOauth": {}}))
    assert cc_oauth_present() is True


# --------------------------------------------------------------------------
# compute_floor() conjunction + key-name matching
# --------------------------------------------------------------------------


def _oauth(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(floor_mod, "cc_oauth_present", lambda: value)


# These run against the REAL version-controlled config/model_routing.yaml (the LLM
# leg is derived from its enabled providers), so the provider names below reflect the
# actual routing config.


def test_floor_met_when_all_three_legs_present(monkeypatch):
    _oauth(monkeypatch, True)
    f = compute_floor({"API_KEY_OPENROUTER": "x", "API_KEY_DEEPINFRA": "y"})
    assert (f.cc_oauth, f.llm_key_present, f.embedding_key_present, f.floor_met) == (
        True,
        True,
        True,
        True,
    )


def test_floor_unmet_missing_embedding(monkeypatch):
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_OPENROUTER": "x"}).floor_met is False


def test_floor_unmet_missing_llm(monkeypatch):
    _oauth(monkeypatch, True)
    f = compute_floor({"API_KEY_DEEPINFRA": "y"})  # embedding present, no LLM
    assert f.embedding_key_present is True
    assert f.llm_key_present is False
    assert f.floor_met is False


def test_floor_unmet_missing_cc_oauth(monkeypatch):
    _oauth(monkeypatch, False)
    assert compute_floor({"API_KEY_OPENROUTER": "x", "API_KEY_DEEPINFRA": "y"}).floor_met is False


def test_anthropic_key_does_not_satisfy_llm_leg(monkeypatch):
    # No type:anthropic provider in routing → a bare Anthropic key is not usable.
    _oauth(monkeypatch, True)
    f = compute_floor({"ANTHROPIC_API_KEY": "sk-ant-xxx", "API_KEY_DEEPINFRA": "y"})
    assert f.llm_key_present is False
    assert f.floor_met is False


def test_nvidia_only_satisfies_llm_leg(monkeypatch):
    # Regression: an install whose only cloud LLM credential is NVIDIA NIM must count
    # (nvidia_nim is an enabled routing provider).
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_NVIDIA_NIM": "x"}).llm_key_present is True


def test_google_key_counts_for_llm_not_embedding(monkeypatch):
    # google is an enabled LLM provider; there is NO google embedding backend.
    _oauth(monkeypatch, True)
    f = compute_floor({"GOOGLE_API_KEY": "g"})
    assert f.llm_key_present is True
    assert f.embedding_key_present is False
    assert f.floor_met is False


def test_qwen_counts_for_embedding_not_llm(monkeypatch):
    # API_KEY_QWEN drives the dashscope EMBEDDING backend, but the qwen LLM provider
    # (qwen-plus) is in no active call-site chain — so it must NOT satisfy the LLM
    # leg. A Qwen-only install is therefore not floor_met.
    _oauth(monkeypatch, True)
    f = compute_floor({"API_KEY_QWEN": "q"})
    assert f.embedding_key_present is True
    assert f.llm_key_present is False
    assert f.floor_met is False


def test_voyage_does_not_satisfy_embedding(monkeypatch):
    # Voyage is rerank-only — it must not satisfy the embedding leg.
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_VOYAGE": "v"}).embedding_key_present is False


def test_openai_key_declared_but_unchained_does_not_count(monkeypatch):
    # openai is declared+enabled but referenced by no active call-site chain, and
    # there is no OpenAI embedding backend → an OpenAI key satisfies NEITHER leg.
    _oauth(monkeypatch, True)
    f = compute_floor({"OPENAI_API_KEY": "o"})
    assert f.llm_key_present is False
    assert f.embedding_key_present is False


def test_disabled_provider_key_does_not_count(monkeypatch):
    # deepseek is enabled:false in model_routing.yaml → a bare DeepSeek key is not
    # usable, so it must not satisfy the LLM leg.
    _oauth(monkeypatch, True)
    assert compute_floor({"API_KEY_DEEPSEEK": "d"}).llm_key_present is False


def test_sentinel_values_do_not_count(monkeypatch):
    _oauth(monkeypatch, True)
    for bad in ("", "None", "NA", "   "):
        f = compute_floor({"API_KEY_GROQ": bad, "API_KEY_DEEPINFRA": "y"})
        assert f.llm_key_present is False, f"{bad!r} should not count"


def test_config_derivation_only_chain_referenced_cloud_types():
    from genesis.onboarding.floor import _chain_referenced_cloud_provider_types

    types = set(_chain_referenced_cloud_provider_types())
    # In some active call-site chain:
    assert {"openrouter", "groq", "mistral", "nvidia_nim"} <= types
    # Keyless/local excluded even though chain-referenced (lmstudio):
    assert "ollama" not in types and "lmstudio" not in types
    # Declared+enabled but referenced by NO chain → excluded:
    assert "qwen" not in types and "openai" not in types
    assert "xai" not in types and "minimax" not in types
    # Disabled providers are absent from cfg.providers entirely:
    assert "deepseek" not in types and "github" not in types


def test_key_pattern_parity_with_runtime(monkeypatch):
    # The floor's provider_key_present must accept exactly what routing's
    # _resolve_api_key accepts (the three env-var naming patterns).
    from genesis.onboarding.floor import provider_key_present
    from genesis.routing.litellm_delegate import _resolve_api_key

    for ptype, envname in (
        ("groq", "API_KEY_GROQ"),
        ("google", "GOOGLE_API_KEY"),
        ("nvidia_nim", "API_KEY_NVIDIA_NIM"),
        ("openai", "OPENAI_API_KEY"),
    ):
        monkeypatch.setenv(envname, "v")
        assert _resolve_api_key(ptype) == "v"
        assert provider_key_present(ptype, {envname: "v"}) is True
        monkeypatch.delenv(envname)


def test_llm_fallback_when_config_unreadable(monkeypatch, tmp_path):
    from genesis.onboarding.floor import _chain_referenced_cloud_provider_types

    monkeypatch.setattr(floor_mod, "_ROUTING_CONFIG_PATH", tmp_path / "nope.yaml")
    _chain_referenced_cloud_provider_types.cache_clear()
    try:
        _oauth(monkeypatch, True)
        # Static fallback still recognizes a known cloud key.
        assert compute_floor({"API_KEY_GROQ": "x"}).llm_key_present is True
    finally:
        _chain_referenced_cloud_provider_types.cache_clear()


def test_invalidate_provider_type_cache_refreshes_derivation(monkeypatch, tmp_path):
    """A routing hot-reload must be able to refresh the floor's provider-type set
    WITHOUT a server restart — otherwise the ego gate/dashboard keep honoring a
    de-chained provider's key (or rejecting a newly-chained one) until restart."""
    from genesis.onboarding.floor import (
        _chain_referenced_cloud_provider_types,
        invalidate_provider_type_cache,
    )

    _chain_referenced_cloud_provider_types.cache_clear()
    try:
        # Seed the cache from the real config (contains groq etc.).
        assert "groq" in _chain_referenced_cloud_provider_types()

        # Point at a minimal alternate config where only mistral is chained.
        alt = tmp_path / "model_routing.yaml"
        alt.write_text(
            "providers:\n"
            "  only-prov:\n"
            "    type: mistral\n"
            "    model: mistral-small-latest\n"
            "    free: true\n"
            "call_sites:\n"
            "  site_x:\n"
            "    chain: [only-prov]\n"
        )
        monkeypatch.setattr(floor_mod, "_ROUTING_CONFIG_PATH", alt)

        # Still cached: the stale set is served until invalidation.
        assert "groq" in _chain_referenced_cloud_provider_types()

        invalidate_provider_type_cache()
        assert _chain_referenced_cloud_provider_types() == ("mistral",)
    finally:
        _chain_referenced_cloud_provider_types.cache_clear()


def test_as_dict_shape(monkeypatch):
    _oauth(monkeypatch, True)
    d = compute_floor({"API_KEY_GROQ": "x", "API_KEY_DEEPINFRA": "o"}).as_dict()
    assert d == {
        "cc_oauth": True,
        "llm_key_present": True,
        "embedding_key_present": True,
        "floor_met": True,
    }
