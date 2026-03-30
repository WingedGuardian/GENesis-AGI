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
    update_call_site_in_yaml(config_file, "2_triage", chain=["groq_llama"])
    bak = config_file.with_suffix(".yaml.bak.1")
    assert bak.exists()


def test_rolling_backups(config_file):
    for i in range(4):
        update_call_site_in_yaml(
            config_file, "2_triage",
            chain=["groq_llama", "openrouter_haiku"] if i % 2 == 0 else ["openrouter_haiku", "groq_llama"],
        )
    assert config_file.with_suffix(".yaml.bak.1").exists()
    assert config_file.with_suffix(".yaml.bak.2").exists()
    assert config_file.with_suffix(".yaml.bak.3").exists()


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


def test_atomic_write_cleans_up_on_failure(config_file, monkeypatch):
    """If validation fails after writing .new, the .new file should be cleaned up."""
    # Corrupt the validation by monkeypatching load_config to fail
    original = load_config

    def bad_load(path):
        if str(path).endswith(".new"):
            raise RuntimeError("simulated parse failure")
        return original(path)

    monkeypatch.setattr("genesis.routing.config.load_config", bad_load)

    with pytest.raises(ValueError, match="validation"):
        update_call_site_in_yaml(config_file, "2_triage", chain=["groq_llama"])

    # .new file should be cleaned up
    assert not config_file.with_suffix(".yaml.new").exists()
    # Original file should be untouched
    reloaded = original(config_file)
    assert list(reloaded.call_sites["2_triage"].chain) == ["openrouter_haiku", "groq_llama"]
