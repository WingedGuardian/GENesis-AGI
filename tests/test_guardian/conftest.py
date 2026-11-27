"""Shared fixtures for Guardian tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.guardian.config import GuardianConfig


@pytest.fixture(autouse=True)
def _deletion_window_any(monkeypatch):
    """Guardian suites written before the deletion window characterize the
    ``any`` behaviour (delete when due), which is exactly main's behaviour
    before the window existed — and without this they would depend on the
    wall clock and on the runner's own timezone config. Window tests
    (test_deletion_window.py) delete this override and set the window
    explicitly."""
    monkeypatch.setenv("GUARDIAN_DELETION_WINDOW", "any")


@pytest.fixture(autouse=True)
def _cc_canary_stubbed(request, monkeypatch):
    """run_check runs the recovery-brain auth canary on a healthy tick, and the
    canary makes one real `claude -p` call. Suites that drive run_check are not
    about the canary, so its one real call is stubbed to success here.
    test_cc_canary.py exercises the real call path against a stub binary and
    opts out."""
    if request.module.__name__.endswith("test_cc_canary"):
        return

    async def _ok(_config, _engine):
        return True, "ok (stubbed by tests/test_guardian/conftest.py)"

    monkeypatch.setattr("genesis.guardian.cc_canary._run_once", _ok)


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
