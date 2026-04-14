"""Tests for routing config save/update functionality."""


import pytest
import yaml

from genesis.routing.config import load_config, update_call_site_in_yaml


@pytest.fixture
def config_file(tmp_path):
    """Create a minimal valid routing config YAML."""
    cfg = {
        "retry": {
            "default": {"max_retries": 2, "base_delay_ms": 100},
        },
        "providers": {
            "openrouter_haiku": {"type": "openrouter", "model": "haiku-3.5", "free": True},
            "groq_llama": {"type": "groq", "model": "llama-3.1-8b", "free": True},
            "openrouter_sonnet": {"type": "openrouter", "model": "sonnet-4", "free": False},
        },
        "call_sites": {
            "2_triage": {
                "chain": ["openrouter_haiku", "groq_llama"],
                "default_paid": False,
                "never_pays": True,
            },
            "5_deep_reflection": {
                "chain": ["openrouter_sonnet", "openrouter_haiku"],
                "default_paid": True,
                "never_pays": False,
            },
        },
    }
    path = tmp_path / "model_routing.yaml"
    path.write_text(yaml.dump(cfg, default_flow_style=False))
    return path


def test_update_chain(config_file):
    new_config = update_call_site_in_yaml(
        config_file, "2_triage",
        chain=["groq_llama", "openrouter_haiku"],
    )
    # Chain order should be reversed
    assert list(new_config.call_sites["2_triage"].chain) == ["groq_llama", "openrouter_haiku"]
    # File should be updated
    reloaded = load_config(config_file)
    assert list(reloaded.call_sites["2_triage"].chain) == ["groq_llama", "openrouter_haiku"]


def test_update_creates_backup(config_file):
    # First update creates the local overlay; second update creates a backup of it
    update_call_site_in_yaml(config_file, "2_triage", chain=["groq_llama"])
    update_call_site_in_yaml(config_file, "2_triage", chain=["openrouter_haiku"])
    local_path = config_file.with_name("model_routing.local.yaml")
    bak = local_path.with_suffix(".yaml.bak.1")
    assert bak.exists()


def test_rolling_backups(config_file):
    for i in range(4):
        update_call_site_in_yaml(
            config_file, "2_triage",
            chain=["groq_llama", "openrouter_haiku"] if i % 2 == 0 else ["openrouter_haiku", "groq_llama"],
        )
    local_path = config_file.with_name("model_routing.local.yaml")
    assert local_path.with_suffix(".yaml.bak.1").exists()
    assert local_path.with_suffix(".yaml.bak.2").exists()
    assert local_path.with_suffix(".yaml.bak.3").exists()


def test_reject_empty_chain(config_file):
    with pytest.raises(ValueError, match="at least one provider"):
        update_call_site_in_yaml(config_file, "2_triage", chain=[])


def test_reject_duplicate_providers(config_file):
    with pytest.raises(ValueError, match="duplicate"):
        update_call_site_in_yaml(config_file, "2_triage", chain=["openrouter_haiku", "openrouter_haiku"])


def test_reject_unknown_provider(config_file):
    with pytest.raises(ValueError, match="Unknown provider"):
        update_call_site_in_yaml(config_file, "2_triage", chain=["nonexistent"])


def test_reject_unknown_call_site(config_file):
    with pytest.raises(ValueError, match="Unknown call site"):
        update_call_site_in_yaml(config_file, "99_fake", chain=["groq_llama"])


def test_reject_never_pays_without_free(config_file):
    with pytest.raises(ValueError, match="never_pays.*free provider"):
        update_call_site_in_yaml(
            config_file, "2_triage",
            chain=["openrouter_sonnet"],  # not free
            never_pays=True,
        )


def test_update_default_paid(config_file):
    new_config = update_call_site_in_yaml(config_file, "2_triage", default_paid=True)
    assert new_config.call_sites["2_triage"].default_paid is True


def test_update_never_pays(config_file):
    new_config = update_call_site_in_yaml(config_file, "5_deep_reflection", never_pays=True)
    # Should succeed because openrouter_haiku is free and in the chain
    assert new_config.call_sites["5_deep_reflection"].never_pays is True


def test_noop_update_no_backup(config_file):
    """No-op update (all params None) should not create backups."""
    config = update_call_site_in_yaml(config_file, "2_triage")
    assert config is not None
    # No backup should be created for no-op
    assert not config_file.with_suffix(".yaml.bak.1").exists()


def test_dispatch_api_mode_clears_cc_fields(config_file):
    """dispatch='api' writes the mode and strips cc_model/cc_position.

    cc_model / cc_position are stored in the local overlay yaml (the
    dashboard route reads them via yaml.safe_load after merging).
    Assertions round-trip through the local overlay file.
    """
    import yaml as _y
    local_path = config_file.with_name("model_routing.local.yaml")
    # Seed with cc_model first
    update_call_site_in_yaml(
        config_file, "5_deep_reflection",
        chain=["openrouter_sonnet"], cc_model="Sonnet", cc_position=0,
    )
    # Now switch to forced API mode
    new_config = update_call_site_in_yaml(
        config_file, "5_deep_reflection", dispatch="api",
    )
    assert new_config.call_sites["5_deep_reflection"].dispatch == "api"
    raw = _y.safe_load(local_path.read_text())
    site = raw["call_sites"]["5_deep_reflection"]
    assert site.get("dispatch") == "api"
    assert "cc_model" not in site
    assert "cc_position" not in site


def test_dispatch_cli_mode_preserves_cc_model(config_file):
    """dispatch='cli' forces CLI execution while preserving cc_model."""
    import yaml as _y
    local_path = config_file.with_name("model_routing.local.yaml")
    new_config = update_call_site_in_yaml(
        config_file, "5_deep_reflection",
        chain=["openrouter_sonnet"], cc_model="Opus", dispatch="cli",
    )
    assert new_config.call_sites["5_deep_reflection"].dispatch == "cli"
    raw = _y.safe_load(local_path.read_text())
    site = raw["call_sites"]["5_deep_reflection"]
    assert site.get("dispatch") == "cli"
    assert site.get("cc_model") == "Opus"


def test_dispatch_dual_mode_is_auto(config_file):
    """dispatch='dual' stores the explicit auto mode."""
    import yaml as _y
    local_path = config_file.with_name("model_routing.local.yaml")
    new_config = update_call_site_in_yaml(
        config_file, "5_deep_reflection",
        chain=["openrouter_sonnet"], cc_model="Sonnet", dispatch="dual",
    )
    assert new_config.call_sites["5_deep_reflection"].dispatch == "dual"
    raw = _y.safe_load(local_path.read_text())
    site = raw["call_sites"]["5_deep_reflection"]
    assert site.get("dispatch") == "dual"
    assert site.get("cc_model") == "Sonnet"


def test_dispatch_invalid_value_rejected(config_file):
    """Invalid dispatch values raise ValueError before the file is touched."""
    with pytest.raises(ValueError, match="Invalid dispatch mode"):
        update_call_site_in_yaml(config_file, "5_deep_reflection", dispatch="bogus")


def test_atomic_write_cleans_up_on_failure(config_file, monkeypatch):
    """If validation fails, no overlay file is written and base is untouched."""
    from genesis.routing import config as config_mod

    # Corrupt the validation by monkeypatching _parse to fail
    original_parse = config_mod._parse

    def bad_parse(raw):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(config_mod, "_parse", bad_parse)

    with pytest.raises(ValueError, match="validation"):
        update_call_site_in_yaml(config_file, "2_triage", chain=["groq_llama"])

    # No overlay file should have been created (validation failed before write)
    local_path = config_file.with_name("model_routing.local.yaml")
    assert not local_path.exists()
    assert not local_path.with_suffix(".yaml.new").exists()
    # Original base file should be untouched — restore parse and reload
    monkeypatch.setattr(config_mod, "_parse", original_parse)
    reloaded = load_config(config_file)
    assert list(reloaded.call_sites["2_triage"].chain) == ["openrouter_haiku", "groq_llama"]
