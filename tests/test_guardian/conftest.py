"""Shared fixtures for Guardian tests."""

from __future__ import annotations

import json
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
        yield
        return

    from genesis.guardian import cc_canary

    async def _ok(_config, _engine):
        return "ok", "ok (stubbed by tests/test_guardian/conftest.py)"

    # run_check swallows canary exceptions, so a stub that no longer fits
    # _run_once's contract would fail quietly. Record what each run saved
    # and check it after the test instead.
    real_maybe_run = cc_canary.maybe_run_cc_canary
    saved: list[object] = []

    async def _recording(config, *args, **kwargs):
        try:
            await real_maybe_run(config, *args, **kwargs)
        finally:
            path = config.state_path / cc_canary._STATE_FILE
            if path.exists():
                saved.append(json.loads(path.read_text()).get("status"))

    monkeypatch.setattr(cc_canary, "_run_once", _ok)
    monkeypatch.setattr(cc_canary, "maybe_run_cc_canary", _recording)
    yield
    assert all(status == "ok" for status in saved), f"stubbed canary saved {saved}"


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
