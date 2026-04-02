"""Tests for routing config loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.routing.config import load_config, load_config_from_string

MINIMAL_YAML = """\
providers:
  local:
    type: ollama
    model: qwen2.5:3b
    free: true
    open_duration_s: 60

call_sites:
  triage:
    chain: [local]

retry:
  default:
    max_retries: 2
    base_delay_ms: 100
    max_delay_ms: 1000
"""


def test_load_minimal():
    cfg = load_config_from_string(MINIMAL_YAML)
    assert "local" in cfg.providers
    assert cfg.providers["local"].is_free is True
    assert "triage" in cfg.call_sites
    assert cfg.call_sites["triage"].chain == ["local"]
    assert cfg.retry_profiles["default"].max_retries == 2


def test_missing_provider_in_chain():
    bad = """\
providers:
  local:
    type: ollama
    model: m
    free: true
call_sites:
  x:
    chain: [local, ghost]
"""
    with pytest.raises(ValueError, match="unknown provider 'ghost'"):
        load_config_from_string(bad)


def test_missing_retry_profile():
    bad = """\
providers:
  local:
    type: ollama
    model: m
    free: true
call_sites:
  x:
    chain: [local]
    retry_profile: nope
"""
    with pytest.raises(ValueError, match="unknown retry profile 'nope'"):
        load_config_from_string(bad)


def test_default_retry_created_when_missing():
    cfg = load_config_from_string("""\
providers:
  a:
    type: t
    model: m
    free: true
call_sites:
  s:
    chain: [a]
""")
    assert "default" in cfg.retry_profiles


def test_env_placeholders_expand(monkeypatch):
    monkeypatch.setenv("OLLAMA_URL", "http://example-ollama:11434")
    cfg = load_config_from_string("""\
providers:
  local:
    type: ollama
    model: qwen2.5:3b
    base_url: ${OLLAMA_URL:-http://localhost:11434}
    free: true
call_sites:
  triage:
    chain: [local]
""")
    assert cfg.providers["local"].base_url == "http://example-ollama:11434"


def test_env_placeholder_default_used_when_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    cfg = load_config_from_string("""\
providers:
  local:
    type: ollama
    model: qwen2.5:3b
    base_url: ${OLLAMA_URL:-http://localhost:11434}
    free: true
call_sites:
  triage:
    chain: [local]
""")
    assert cfg.providers["local"].base_url == "http://localhost:11434"


def test_load_full_yaml(monkeypatch):
    # Ensure deterministic env for provider gating
    monkeypatch.setenv("GENESIS_ENABLE_OLLAMA", "true")
    monkeypatch.setenv("GENESIS_ENABLE_LM_STUDIO", "false")

    path = Path(__file__).resolve().parents[2] / "config" / "model_routing.yaml"
    cfg = load_config(path)
    # lmstudio-30b disabled by default → 21 enabled providers
    # (22 total - 1 disabled lmstudio-30b)
    assert len(cfg.providers) == 21
    assert "lmstudio-30b" not in cfg.providers
    assert len(cfg.call_sites) == 39  # +contingency_*, +cc_update_analysis, +email_triage
    assert "background" in cfg.retry_profiles
    assert cfg.call_sites["12_surplus_brainstorm"].never_pays is True
    assert cfg.call_sites["5_deep_reflection"].default_paid is True
    assert cfg.call_sites["36_code_auditor"].never_pays is False
    assert cfg.call_sites["37_infrastructure_monitor"].default_paid is True

    # Phase 6 learning call sites
    assert cfg.call_sites["29_retrospective_triage"].chain == [
        "ollama-3b", "groq-free", "mistral-free",
    ]
    assert cfg.call_sites["30_triage_calibration"].default_paid is True
    # lmstudio-30b filtered out, only mistral-large remains
    assert cfg.call_sites["30_triage_calibration"].chain == ["mistral-large"]
    assert cfg.call_sites["31_outcome_classification"].chain == [
        "glm5", "mistral-large",
    ]

    # mistral-large provider
    ml = cfg.providers["mistral-large"]
    assert ml.is_free is False
    assert ml.model_id == "mistral-large-latest"


def test_provider_enabled_default():
    """Providers without explicit enabled field default to enabled."""
    cfg = load_config_from_string(MINIMAL_YAML)
    assert cfg.providers["local"].enabled is True


def test_provider_enabled_false_excludes():
    """Providers with enabled: false are excluded from config."""
    cfg = load_config_from_string("""\
providers:
  active:
    type: ollama
    model: m
    free: true
  disabled:
    type: ollama
    model: m
    free: true
    enabled: false
call_sites:
  s:
    chain: [active, disabled]
""")
    assert "active" in cfg.providers
    assert "disabled" not in cfg.providers
    assert cfg.call_sites["s"].chain == ["active"]


def test_provider_enabled_string_parsing():
    """Enabled field parses string values from env var expansion."""
    for false_val in ["false", "False", "0", "no", "off", ""]:
        cfg = load_config_from_string(f"""\
providers:
  p:
    type: t
    model: m
    free: true
    enabled: "{false_val}"
call_sites:
  s:
    chain: [p]
""")
        assert "p" not in cfg.providers, f"'{false_val}' should disable provider"

    for true_val in ["true", "True", "1", "yes", "on"]:
        cfg = load_config_from_string(f"""\
providers:
  p:
    type: t
    model: m
    free: true
    enabled: "{true_val}"
call_sites:
  s:
    chain: [p]
""")
        assert "p" in cfg.providers, f"'{true_val}' should enable provider"


def test_provider_enabled_env_var(monkeypatch):
    """Enabled field works with env var expansion."""
    monkeypatch.setenv("MY_TOGGLE", "false")
    cfg = load_config_from_string("""\
providers:
  gated:
    type: t
    model: m
    free: true
    enabled: ${MY_TOGGLE:-true}
  always:
    type: t
    model: m
    free: true
call_sites:
  s:
    chain: [gated, always]
""")
    assert "gated" not in cfg.providers
    assert "always" in cfg.providers
    assert cfg.call_sites["s"].chain == ["always"]


def test_all_providers_disabled_skips_call_site():
    """Call site with all providers disabled is dropped with warning."""
    cfg = load_config_from_string("""\
providers:
  only:
    type: t
    model: m
    free: true
    enabled: false
call_sites:
  dead:
    chain: [only]
  alive:
    chain: [only]
""")
    assert "dead" not in cfg.call_sites
    assert "alive" not in cfg.call_sites
