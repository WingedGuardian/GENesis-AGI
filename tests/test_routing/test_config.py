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
    # lmstudio-30b, github-o3mini, openrouter-deepseek-r1 disabled → 27 enabled providers
    # (30 total - 3 disabled; added openrouter-deepseek-v4, openrouter-gpt55 for
    # executor review gates)
    assert len(cfg.providers) == 27
    assert "lmstudio-30b" not in cfg.providers
    assert "github-o3mini" not in cfg.providers
    assert "openrouter-deepseek-r1" not in cfg.providers
    # Call sites evolve — assert actual count matches config, and lock in
    # a few load-bearing ids rather than chasing the total on every edit.
    assert len(cfg.call_sites) == 43
    assert "background" in cfg.retry_profiles
    assert cfg.call_sites["12_surplus_brainstorm"].never_pays is True
    assert cfg.call_sites["5_deep_reflection"].default_paid is True
    assert cfg.call_sites["36_code_auditor"].never_pays is False
    assert cfg.call_sites["37_infrastructure_monitor"].default_paid is True

    # Phase 6 learning call sites
    assert cfg.call_sites["29_retrospective_triage"].chain == [
        "groq-free", "mistral-large-free", "openrouter-nemo",
    ]
    assert cfg.call_sites["30_triage_calibration"].default_paid is True
    # lmstudio-30b filtered out, only mistral-large-free remains
    assert cfg.call_sites["30_triage_calibration"].chain == ["mistral-large-free"]
    assert cfg.call_sites["31_outcome_classification"].chain == [
        "glm5", "mistral-large-free",
    ]

    # mistral-large-free provider (consolidated from mistral-free + mistral-large)
    ml = cfg.providers["mistral-large-free"]
    assert ml.is_free is True
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


# ---------------------------------------------------------------------------
# F1: ``dispatch`` field parsing on CallSiteConfig.
# ---------------------------------------------------------------------------

_DISPATCH_YAML = """\
providers:
  p:
    type: t
    model: m
    free: true
call_sites:
  default_site:
    chain: [p]
  dual_site:
    chain: [p]
    dispatch: dual
  cli_site:
    chain: [p]
    dispatch: cli
  api_site:
    chain: [p]
    dispatch: api
  legacy_cc_site:
    chain: [p]
    dispatch: cc
  unknown_site:
    chain: [p]
    dispatch: bogus
  upper_site:
    chain: [p]
    dispatch: CLI
"""


def test_dispatch_default_is_dual_when_missing():
    cfg = load_config_from_string(_DISPATCH_YAML)
    assert cfg.call_sites["default_site"].dispatch == "dual"


def test_dispatch_explicit_values_round_trip():
    cfg = load_config_from_string(_DISPATCH_YAML)
    assert cfg.call_sites["dual_site"].dispatch == "dual"
    assert cfg.call_sites["cli_site"].dispatch == "cli"
    assert cfg.call_sites["api_site"].dispatch == "api"


def test_dispatch_legacy_cc_alias_normalises_to_cli():
    """Earlier dashboard code wrote ``dispatch: cc`` before the three
    mode selector landed.  The parser must map that to ``cli`` so old
    yaml files keep working and the runtime router picks up the CLI
    path automatically."""
    cfg = load_config_from_string(_DISPATCH_YAML)
    assert cfg.call_sites["legacy_cc_site"].dispatch == "cli"


def test_dispatch_unknown_falls_back_to_dual():
    """Typos in dispatch must never silently disable the CLI gate.
    Falling back to 'dual' preserves safe default behaviour.

    The behavioural fallback is what matters. A previous version of
    this test also asserted on a caplog WARNING record, but that sniff
    was flaky under the full suite — caplog's logger-name filter
    interacts with other tests' logger configuration. Commit 0ad9567
    removed the same assertion; 3bbae15 re-introduced it; v3.0a3-hf1
    removes it again."""
    cfg = load_config_from_string(_DISPATCH_YAML)
    assert cfg.call_sites["unknown_site"].dispatch == "dual"


def test_dispatch_case_insensitive():
    cfg = load_config_from_string(_DISPATCH_YAML)
    assert cfg.call_sites["upper_site"].dispatch == "cli"
