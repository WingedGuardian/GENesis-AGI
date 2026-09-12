"""Smoke tests for the foreground `gmodel` launcher (scripts/gmodel).

Exercised via subprocess with `--print-env` (a no-launch diagnostic) so the tests
never spawn a real `claude`.

HERMETIC BY CONSTRUCTION. These tests subprocess, so conftest's
`_isolate_user_config_dir` fixture does NOT reach the child — it would read a
real `$HOME` and therefore the developer's real `~/.genesis/config/` overlay.
`_run` takes a REQUIRED `home`, with no `Path.home()` default, so a new test
cannot silently reintroduce that leak; every test passes the `hermetic_home`
fixture. The shipped `config/cc_roster.yaml` declares no peers (they are
install-local), so any peer a test needs, it writes into its own overlay.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GMODEL = _REPO_ROOT / "scripts" / "gmodel"


@pytest.fixture
def hermetic_home(tmp_path):
    """An empty HOME with a config dir, so the child reads no real overlay."""
    (tmp_path / ".genesis" / "config").mkdir(parents=True)
    return tmp_path


def _run(args, home, extra_env=None):
    """Run the launcher against an explicit HOME.

    `home` is REQUIRED and has no default on purpose — see the module docstring.
    """
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home)}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_GMODEL), *args],
        capture_output=True, text=True, env=env, timeout=60,
    )


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_list_runs_and_shows_claude(hermetic_home):
    r = _run(["--list"], hermetic_home)
    assert r.returncode == 0, r.stderr
    assert "claude" in r.stdout
    assert "native Max" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_unknown_model_errors(hermetic_home):
    r = _run(["--print-env", "no-such-model"], hermetic_home)
    assert r.returncode == 1
    assert "unknown roster model" in r.stderr


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_native_claude_has_no_routing_and_drops_api_key(hermetic_home):
    # A stray ANTHROPIC_API_KEY must be dropped so native runs on Max, not API.
    r = _run(["--print-env", "claude"], hermetic_home, {"ANTHROPIC_API_KEY": "sk-stray"})
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout
    assert "ANTHROPIC_BASE_URL=<unset>" in r.stdout
    assert "GENESIS_ROSTER_MODEL=<unset>" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_opus_tier_is_native_with_model_flag(hermetic_home):
    r = _run(["--print-env", "opus"], hermetic_home, {"ANTHROPIC_API_KEY": "sk-stray"})
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout
    assert "claude --model opus" in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_peer_routes_and_drops_api_key(hermetic_home):
    """A peer declared in the overlay resolves end-to-end; the stray key is dropped."""
    overlay = hermetic_home / ".genesis" / "config" / "cc_roster.local.yaml"
    overlay.write_text(
        "models:\n"
        "  test-peer:\n"
        '    anthropic_base_url: "https://example.invalid/api/anthropic"\n'
        "    auth_env: GENESIS_TEST_ROSTER_KEY\n"
        "    model_id: test-peer\n"
        "    failover_order: 1\n"
    )
    r = _run(
        ["--print-env", "test-peer"],
        hermetic_home,
        {"ANTHROPIC_API_KEY": "sk-stray", "GENESIS_TEST_ROSTER_KEY": "zk-test"},
    )
    assert r.returncode == 0, r.stderr
    assert "ANTHROPIC_BASE_URL=https://example.invalid/api/anthropic" in r.stdout
    assert "ANTHROPIC_AUTH_TOKEN=<set>" in r.stdout
    assert "ANTHROPIC_MODEL=test-peer" in r.stdout
    assert "ANTHROPIC_API_KEY=<unset>" in r.stdout  # isolation
    assert "GENESIS_ROSTER_MODEL=test-peer" in r.stdout
