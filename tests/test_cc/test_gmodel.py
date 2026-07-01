"""Smoke tests for the foreground `gmodel` launcher (scripts/gmodel).

Exercised via subprocess with `--print-env` (a no-launch diagnostic) so the tests
never spawn a real `claude`. Uses the repo's real config/cc_roster.yaml; the peer
key is supplied via env so no secrets.env is required (CI-safe).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GMODEL = _REPO_ROOT / "scripts" / "gmodel"


def _run(args, extra_env=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_GMODEL), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_list_runs_and_shows_claude():
    r = _run(["--list"])
    assert r.returncode == 0, r.stderr
    assert "claude" in r.stdout
    assert "native Max" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_unknown_model_errors():
    r = _run(["--print-env", "no-such-model"])
    assert r.returncode == 1
    assert "unknown roster model" in r.stderr


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_native_claude_has_no_routing_and_drops_api_key():
    # A stray ANTHROPIC_API_KEY must be dropped so native runs on Max, not API.
    r = _run(["--print-env", "claude"], {"ANTHROPIC_API_KEY": "sk-stray"})
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout
    assert "ANTHROPIC_BASE_URL=<unset>" in r.stdout
    assert "GENESIS_ROSTER_MODEL=<unset>" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_opus_tier_is_native_with_model_flag():
    r = _run(["--print-env", "opus"], {"ANTHROPIC_API_KEY": "sk-stray"})
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout
    assert "claude --model opus" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_peer_routes_and_drops_api_key():
    # Peer key via env (no secrets.env needed). Uses the repo roster's glm-5.2.
    r = _run(
        ["--print-env", "glm-5.2"],
        {"ANTHROPIC_API_KEY": "sk-stray", "ZHIPU_API_KEY": "zk-test"},
    )
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic" in r.stdout
    assert "ANTHROPIC_AUTH_TOKEN=<set>" in r.stdout
    assert "ANTHROPIC_MODEL=glm-5.2" in r.stdout
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout  # isolation
    assert "GENESIS_ROSTER_MODEL=glm-5.2" in r.stdout
