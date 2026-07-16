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


# ── vzdump slice: new backup fields + float coercion ──────────────────────


def test_backup_fields_have_generic_defaults():
    """Nothing install-specific: empty backup_storage falls back to `storage`
    at the consumer; caps/multiplier/wall-bound are generic defaults."""
    pc = ProvisioningConfig()
    assert pc.backup_storage == ""
    assert pc.backup_keep_last == 2
    assert pc.max_backups_per_week == 2
    assert pc.backup_size_multiplier == 1.0
    assert pc.vzdump_timeout_s == 7200


def test_float_field_coerces_through_override(tmp_path):
    """configure-provisioning sends strings — float fields must coerce (the
    pre-existing coercer only knew bool/int/str)."""
    state = tmp_path / "state"
    dest = write_provisioning_override(str(state), {
        "backup_storage": "backup",
        "backup_keep_last": "3",
        "backup_size_multiplier": "0.7",
        "vzdump_timeout_s": "10800",
    })
    written = yaml.safe_load(dest.read_text())["provisioning"]
    assert written["backup_storage"] == "backup"
    assert written["backup_keep_last"] == 3
    assert written["backup_size_multiplier"] == pytest.approx(0.7)
    assert written["vzdump_timeout_s"] == 10800


def test_bad_float_value_raises_value_error(tmp_path):
    state = tmp_path / "state"
    with pytest.raises(ValueError):
        write_provisioning_override(str(state), {"backup_size_multiplier": "fast"})
