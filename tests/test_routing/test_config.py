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
    # (32 total - 4 disabled; added openrouter-deepseek-v4, openrouter-gpt55 for
    # executor review gates)
    assert len(cfg.providers) == 28
    assert "lmstudio-30b" not in cfg.providers
    assert "github-o3mini" not in cfg.providers
    assert "openrouter-deepseek-r1" not in cfg.providers
    # Call sites evolve — assert actual count matches config, and lock in
    # a few load-bearing ids rather than chasing the total on every edit.
    # 2026-05-10: 44 → 43 after net change (judge added by #304, 2_triage +
    # 7_task_retrospective removed by this PR).
    # 2026-05-12: 43 → 44 after 44_task_premortem added by #334.
    # 2026-05-14: 44 → 45 after 45_intelligence_intake added by #349.
    assert len(cfg.call_sites) == 45
    assert "2_triage" not in cfg.call_sites  # removed 2026-05-10
    assert "7_task_retrospective" not in cfg.call_sites  # removed 2026-05-10 (duplicate; live one is 43_task_retrospective)
    assert "background" in cfg.retry_profiles
    assert cfg.call_sites["12_surplus_brainstorm"].never_pays is True
    assert cfg.call_sites["5_deep_reflection"].default_paid is True
    assert cfg.call_sites["36_code_auditor"].never_pays is False
    assert cfg.call_sites["37_infrastructure_monitor"].default_paid is True
    # judge: LLM-as-judge eval primitive — single-model chain, paid-by-default
    assert cfg.call_sites["judge"].chain == ["openrouter-deepseek-v4"]
    assert cfg.call_sites["judge"].default_paid is True
    assert cfg.call_sites["judge"].dispatch == "api"

    # Phase 6 learning call sites
    assert cfg.call_sites["29_retrospective_triage"].chain == [
        "groq-free", "mistral-large-free", "openrouter-nemo",
    ]
    assert cfg.call_sites["30_triage_calibration"].default_paid is True
    # lmstudio-30b filtered out, only mistral-large-free remains
    assert cfg.call_sites["30_triage_calibration"].chain == ["mistral-large-free"]
    assert cfg.call_sites["31_outcome_classification"].chain == [
        "glm51", "mistral-large-free",
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
# Keyless providers stay registered as down (2026-05-10).
# ---------------------------------------------------------------------------


def test_keyless_provider_stays_registered_as_down(monkeypatch):
    """A provider whose API key env var is unset must stay in cfg.providers
    with has_api_key=False, NOT get filtered out at load time. The call
    site that references it must also remain visible in cfg.call_sites
    with its full chain intact.

    This is the normal install state for partial API-key configuration —
    the router treats keyless providers as down (same as a tripped CB),
    and the snapshot surfaces them on the neural monitor so the user can
    see what they need to add to enable the call site.
    """
    # Override the conftest autouse patch that forces has_api_key=True.
    # We want the real check to run so we can verify the load-time
    # marking. Local types (ollama) bypass the env check entirely.
    from unittest.mock import patch

    def _real_has_api_key(cfg):
        return cfg.provider_type in {"ollama", "lmstudio"}

    with patch(
        "genesis.observability.snapshots.api_keys.has_api_key",
        side_effect=_real_has_api_key,
    ):
        cfg = load_config_from_string("""\
providers:
  keyless:
    type: anthropic
    model: claude-haiku
    free: false
  keyed:
    type: ollama
    model: qwen2.5:3b
    free: true
call_sites:
  site:
    chain: [keyless, keyed]
""", check_api_keys=True)

    assert "keyless" in cfg.providers
    assert cfg.providers["keyless"].has_api_key is False
    # Local providers always have keys
    assert cfg.providers["keyed"].has_api_key is True
    # Call site stays visible with full chain
    assert "site" in cfg.call_sites
    assert cfg.call_sites["site"].chain == ["keyless", "keyed"]


def test_mixed_disabled_and_keyless_chain():
    """A chain mixing `enabled: false` (filtered out) + keyless (kept as
    down) must keep the chain reference for the keyless provider while
    dropping the explicitly-disabled one. This is the only regression
    path where the two filters could interact incorrectly.
    """
    from unittest.mock import patch

    def _has_key(cfg):
        return cfg.provider_type == "ollama"

    with patch(
        "genesis.observability.snapshots.api_keys.has_api_key",
        side_effect=_has_key,
    ):
        cfg = load_config_from_string("""\
providers:
  off:
    type: anthropic
    model: m
    free: false
    enabled: false
  keyless:
    type: zenmux
    model: m
    free: false
  keyed:
    type: ollama
    model: m
    free: true
call_sites:
  s:
    chain: [off, keyless, keyed]
""", check_api_keys=True)

    # `off` is removed from cfg.providers (explicit enabled:false)
    assert "off" not in cfg.providers
    # keyless stays registered with has_api_key=False
    assert cfg.providers["keyless"].has_api_key is False
    assert cfg.providers["keyed"].has_api_key is True
    # Chain has `off` filtered out, keyless preserved
    assert cfg.call_sites["s"].chain == ["keyless", "keyed"]


def test_keyless_chain_preserves_site():
    """When a site's whole chain is keyless, the site stays visible —
    no silent drop. The snapshot will mark it 'disabled' so the user
    knows it's not running, but it's reachable in the dashboard.
    """
    from unittest.mock import patch

    # All providers come back as keyless
    with patch("genesis.observability.snapshots.api_keys.has_api_key", return_value=False):
        cfg = load_config_from_string("""\
providers:
  p1:
    type: anthropic
    model: claude-sonnet
    free: false
  p2:
    type: zenmux
    model: glm-4.5
    free: false
call_sites:
  only_keyless:
    chain: [p1, p2]
""", check_api_keys=True)

    assert "only_keyless" in cfg.call_sites
    assert cfg.call_sites["only_keyless"].chain == ["p1", "p2"]
    assert all(not cfg.providers[p].has_api_key for p in ["p1", "p2"])


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
