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
    # lmstudio-30b, github-o3mini, deepseek-chat disabled → 28 enabled
    # (cerebras disabled 2026-06 — reasoning-only models incompatible;
    # openrouter-deepseek-r1 removed entirely 2026-05-24;
    # nvidia-nim-deepseek + nvidia-nim-kimi added 2026-05-25)
    # 2026-06-23: 28 → 29 after groq-oss-120b added (free reasoning option,
    # not yet wired into any chain).
    # 2026-06-23 (WS2b): 29 → 28 after removing expired openrouter-trinity-free.
    assert len(cfg.providers) == 28
    assert "lmstudio-30b" not in cfg.providers
    assert "github-o3mini" not in cfg.providers
    assert "openrouter-deepseek-r1" not in cfg.providers  # removed from config
    # Call sites evolve — assert actual count matches config, and lock in
    # a few load-bearing ids rather than chasing the total on every edit.
    # 2026-05-10: 44 → 43 after net change (judge added by #304, 2_triage +
    # 7_task_retrospective removed by this PR).
    # 2026-05-12: 43 → 44 after 44_task_premortem added by #334.
    # 2026-05-14: 44 → 45 after 45_intelligence_intake added by #349.
    # 2026-05-15: 45 → 46 after dream_cycle_synthesis added by #359.
    # 2026-05-22: 46 → 47 after models_md_synthesis added by #410.
    # 2026-05-23: 47 → 48 after 40_ego_focus_selection added by #420.
    # 2026-05-23: 48 → 49 after voice_conversation added by #422.
    # 2026-05-24: 49 → 48 after models_md_synthesis removed (converted to CC session dispatch).
    # 2026-05-29: 48 → 49 after dream_cycle_entity_check added (Sprint 2).
    # 2026-06-06: 49 → 51 after dream_cycle_synthesis_challenge + dream_cycle_entity_challenge (immune system PR).
    # 2026-06-20: 51 → 52 after crag_grade added (W-CRAG selective corrective retrieval, PR #711).
    # 2026-06-30: 52 → 53 after 38a_procedure_novelty_llm added (C2b cross-type procedure dedup).
    # 2026-07-01: 53 → 54 after attention_salience added (PR3b L1.5 salience gate).
    # 2026-07-10: 54 → 55 after ambient_arbiter added (WS-C arbiter neural-monitor registration).
    # 2026-07-12: 55 → 56 after 46_infra_annotation added (infrastructure body-schema annotations).
    # 2026-07-13: 56 → 54 after removing orphaned legacy ego sites 7_ego_cycle
    # + 8_ego_compaction (superseded by 7_user/7_genesis_ego_cycle + ephemeral
    # compaction in #26; their model_routing.yaml entries were never cleaned up).
    assert len(cfg.call_sites) == 54
    assert "crag_grade" in cfg.call_sites  # W-CRAG runtime grader (2026-06-20)
    assert "38a_procedure_novelty_llm" in cfg.call_sites  # C2b cross-type dedup (2026-06-30)
    assert "attention_salience" in cfg.call_sites  # PR3b L1.5 salience gate (2026-07-01)
    assert "46_infra_annotation" in cfg.call_sites  # body-schema annotations (2026-07-12)
    # ambient_arbiter: display-only cli site — the detached ambient worker spawns
    # CC directly (model pinned in session_awareness/arbiter.py); empty chain BY
    # DESIGN (no API fallback). First empty-chain cli site — lock the shape.
    assert cfg.call_sites["ambient_arbiter"].dispatch == "cli"
    assert cfg.call_sites["ambient_arbiter"].chain == []
    assert cfg.call_sites["ambient_arbiter"].never_pays is True
    assert "models_md_synthesis" not in cfg.call_sites  # removed 2026-05-24
    assert "2_triage" not in cfg.call_sites  # removed 2026-05-10
    assert "7_task_retrospective" not in cfg.call_sites  # removed 2026-05-10 (duplicate; live one is 43_task_retrospective)
    assert "7_ego_cycle" not in cfg.call_sites  # removed 2026-07-13 (→ 7_user/7_genesis_ego_cycle)
    assert "8_ego_compaction" not in cfg.call_sites  # removed 2026-07-13 (ego went ephemeral; no LLM compaction)
    assert "background" in cfg.retry_profiles
    assert cfg.call_sites["12_surplus_brainstorm"].never_pays is True
    assert cfg.call_sites["5_deep_reflection"].default_paid is True
    assert cfg.call_sites["36_code_auditor"].never_pays is False
    assert cfg.call_sites["37_infrastructure_monitor"].default_paid is True
    # judge: LLM-as-judge eval primitive — free NIM v4-pro first, then paid v4-pro,
    # then v4-flash; paid-by-default
    assert cfg.call_sites["judge"].chain == [
        "nvidia-nim-deepseek", "openrouter-deepseek-v4", "openrouter-deepseek-v4-flash"
    ]
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


# ---------------------------------------------------------------------------
# Per-provider ``params`` (extra litellm kwargs) parsing.
# ---------------------------------------------------------------------------


def test_provider_params_parsed():
    """A provider with a ``params:`` block (e.g. Groq gpt-oss reasoning
    controls) must parse into ProviderConfig.params verbatim. A provider
    without one must get params=None."""
    cfg = load_config_from_string("""\
providers:
  gpt_oss:
    type: groq
    model: openai/gpt-oss-20b
    free: true
    params:
      extra_body:
        include_reasoning: false
        reasoning_effort: low
  plain:
    type: ollama
    model: m
    free: true
call_sites:
  s:
    chain: [gpt_oss, plain]
""")
    assert cfg.providers["gpt_oss"].params == {
        "extra_body": {"include_reasoning": False, "reasoning_effort": "low"},
    }
    assert cfg.providers["plain"].params is None
