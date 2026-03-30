"""Shared fixtures for Guardian tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.guardian.config import GuardianConfig


@pytest.fixture
def guardian_config() -> GuardianConfig:
    """Default Guardian config for testing."""
    return GuardianConfig()


@pytest.fixture
def config_yaml(tmp_path: Path) -> Path:
    """Write a test guardian.yaml and return its path."""
    yaml_content = """
container_name: test-genesis
container_ip: "10.0.0.1"
health_api_port: 5555
check_interval_s: 15

probes:
  probe_timeout_s: 5
  ping_count: 2

confirmation:
  recheck_delay_s: 10
  max_recheck_attempts: 2
  required_failed_signals: 1

cc:
  enabled: false
  model: haiku

snapshots:
  retention: 3
"""
    p = tmp_path / "guardian.yaml"
    p.write_text(yaml_content)
    return p


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Temp directory for state files."""
    d = tmp_path / "guardian-state"
    d.mkdir()
    return d
