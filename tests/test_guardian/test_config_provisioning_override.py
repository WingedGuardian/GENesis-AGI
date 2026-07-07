"""Tests for the state-dir provisioning override (configure-provisioning verb).

The override lets `configure-provisioning` land host-specific provisioning config
in the guardian state_dir — outside the git checkout — so it survives guardian
redeploys without editing the tracked guardian.yaml.
"""
from __future__ import annotations

import pytest
import yaml

from genesis.guardian.config import (
    ProvisioningConfig,
    load_config,
    write_provisioning_override,
)


def _guardian_yaml(tmp_path, state_dir):
    p = tmp_path / "guardian.yaml"
    p.write_text(f'container_name: genesis\nstate_dir: "{state_dir}"\n')
    return p


def test_write_and_merge_roundtrip(tmp_path):
    state = tmp_path / "state"
    cfg_path = _guardian_yaml(tmp_path, state)
    dest = write_provisioning_override(str(state), {
        "enabled": "true", "api_host": "10.0.0.5", "api_port": "8006",
        "node": "proxmox", "vmid": "500", "target_disk": "scsi1",
        "storage": "local-lvm", "verify_tls": "false",
        "require_recent_backup": "false",
    })
    assert dest.exists()
    written = yaml.safe_load(dest.read_text())["provisioning"]
    assert written["enabled"] is True          # str -> bool coercion
    assert written["vmid"] == 500              # str -> int coercion
    assert written["verify_tls"] is False
    assert written["api_host"] == "10.0.0.5"

    cfg = load_config(cfg_path)
    assert cfg.provisioning.enabled is True
    assert cfg.provisioning.vmid == 500
    assert cfg.provisioning.target_disk == "scsi1"
    assert cfg.provisioning.verify_tls is False
    assert cfg.provisioning.require_recent_backup is False


def test_absent_override_is_noop(tmp_path):
    cfg_path = _guardian_yaml(tmp_path, tmp_path / "state")
    cfg = load_config(cfg_path)
    assert cfg.provisioning == ProvisioningConfig()  # defaults, disabled


def test_unknown_field_raises(tmp_path):
    with pytest.raises(ValueError, match="unknown provisioning field"):
        write_provisioning_override(str(tmp_path / "s"), {"bogus": "x"})


def test_partial_override_merges_onto_defaults(tmp_path):
    state = tmp_path / "state"
    cfg_path = _guardian_yaml(tmp_path, state)
    write_provisioning_override(str(state), {"enabled": "true", "vmid": "300"})
    cfg = load_config(cfg_path)
    assert cfg.provisioning.enabled is True
    assert cfg.provisioning.vmid == 300
    # untouched fields keep their defaults
    assert cfg.provisioning.api_port == 8006
    assert cfg.provisioning.target_disk == "scsi1"


def test_env_kill_switch_wins_over_override(tmp_path, monkeypatch):
    state = tmp_path / "state"
    cfg_path = _guardian_yaml(tmp_path, state)
    write_provisioning_override(str(state), {"enabled": "true"})
    monkeypatch.setenv("GUARDIAN_PROVISIONING_ENABLED", "0")
    cfg = load_config(cfg_path)
    assert cfg.provisioning.enabled is False  # env override runs last and wins


def test_unreadable_override_is_silent(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "provisioning.local.yaml").write_text(":::not: valid: yaml:::\n  - [")
    cfg_path = _guardian_yaml(tmp_path, state)
    cfg = load_config(cfg_path)  # must not raise
    assert cfg.provisioning.enabled is False  # fell back to defaults


def test_native_yaml_types_accepted(tmp_path):
    """The loader must also accept an override written with real YAML types
    (not just the CLI's key=value strings)."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "provisioning.local.yaml").write_text(
        yaml.safe_dump({"provisioning": {"enabled": True, "vmid": 42}})
    )
    cfg_path = _guardian_yaml(tmp_path, state)
    cfg = load_config(cfg_path)
    assert cfg.provisioning.enabled is True
    assert cfg.provisioning.vmid == 42
