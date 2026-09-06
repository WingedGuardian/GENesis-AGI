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


# ── The venv re-exec ──────────────────────────────────────────────────────────
#
# These do NOT use `_run`, because `_run` passes `sys.executable` — under pytest
# that IS the venv interpreter, so it can never exercise the path that broke.
# The bug only appears when gmodel is started by an interpreter OUTSIDE the venv
# that nonetheless RESOLVES to the same real binary, which is the ordinary
# `python3 scripts/gmodel` invocation.


def _fake_repo(tmp_path, secrets_body):
    """A repo-root-shaped dir with a REAL venv, so the re-exec has somewhere to go.

    `.venv` is symlinked as a WHOLE DIRECTORY on purpose: the guard compares
    `sys.prefix` to `<root>/.venv`, and after the exec `sys.prefix` is the real
    venv. Symlinking only `bin/python` would leave the two unequal forever —
    i.e. an infinite exec loop, which is exactly what the sentinel also guards.
    """
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "gmodel").write_bytes(_GMODEL.read_bytes())
    (root / "scripts" / "gmodel").chmod(0o755)
    # Point at the venv THIS test run is using (sys.prefix), not at
    # `_REPO_ROOT/.venv`. A git worktree has no .venv of its own, so the
    # repo-relative form made the load-bearing test SKIP — a silent pass on the
    # one assertion that matters. sys.prefix is a venv wherever pytest runs.
    (root / ".venv").symlink_to(Path(sys.prefix))
    (root / "src").symlink_to(_REPO_ROOT / "src")
    (root / "secrets.env").write_text(secrets_body)
    return root


def _overlay(home, auth_env):
    (home / ".genesis" / "config" / "cc_roster.local.yaml").write_text(
        "models:\n"
        "  test-peer:\n"
        '    anthropic_base_url: "https://example.invalid/api/anthropic"\n'
        f"    auth_env: {auth_env}\n"
        "    model_id: test-peer\n"
        "    failover_order: 1\n"
    )


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
@pytest.mark.skipif(
    sys.prefix == sys.base_prefix, reason="pytest is not running inside a venv"
)
def test_reexecs_into_venv_from_the_base_interpreter(tmp_path, hermetic_home):
    """THE regression. A key that IS configured must not be reported as missing.

    `.venv/bin/python` is a symlink to the base interpreter, so comparing
    `Path(sys.executable).resolve()` against `_VENV_PYTHON.resolve()` compares
    two paths that are EQUAL for both the venv and the system interpreter. The
    guard therefore concluded "already in the venv", skipped the re-exec, ran
    without python-dotenv, silently failed to read secrets.env, and reported
    every peer as `key ✗` — telling the operator that a correctly configured
    route had no credentials. A wrong answer in the shape of a right one.
    """
    root = _fake_repo(tmp_path, "GENESIS_TEST_REEXEC_KEY=zk-from-secrets-env\n")
    _overlay(hermetic_home, "GENESIS_TEST_REEXEC_KEY")

    base = str(Path(sys.executable).resolve())  # the bare `python3` case
    r = subprocess.run(
        [base, str(root / "scripts" / "gmodel"), "--list"],
        capture_output=True, text=True, timeout=60,
        env={"PATH": "/usr/bin:/bin", "HOME": str(hermetic_home)},
    )
    assert r.returncode == 0, r.stderr
    assert "key ✓" in r.stdout, (
        f"peer key read from secrets.env was not seen — re-exec did not happen.\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    assert "key ✗" not in r.stdout


@pytest.mark.skipif(not _GMODEL.is_file(), reason="gmodel launcher not present")
def test_reexec_sentinel_prevents_an_infinite_loop(tmp_path, hermetic_home):
    """A re-exec whose arrival test never becomes true must stop, not spin.

    sys.prefix can legitimately fail to match for a relocated or `--copies`
    venv. One hop is all this ever needs, so the sentinel enforces exactly one —
    and the run must still TERMINATE and say why the keys are unreadable.
    """
    root = _fake_repo(tmp_path, "GENESIS_TEST_REEXEC_KEY=zk-from-secrets-env\n")
    _overlay(hermetic_home, "GENESIS_TEST_REEXEC_KEY")

    base = str(Path(sys.executable).resolve())
    r = subprocess.run(
        [base, str(root / "scripts" / "gmodel"), "--list"],
        capture_output=True, text=True, timeout=60,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(hermetic_home),
            "GENESIS_GMODEL_REEXEC": "1",  # pretend a hop already happened
        },
    )
    assert r.returncode == 0, r.stderr  # terminated, did not loop
    # And it is LOUD about why the key looks missing, instead of implying the
    # operator never configured one.
    assert "python-dotenv is unavailable" in r.stderr
    assert "cannot see your keys" in r.stderr
