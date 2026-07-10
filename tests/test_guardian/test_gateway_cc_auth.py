"""Tests for the guardian-gateway.sh `version` verb CC-auth-health fields.

Runs the REAL gateway script in a sandboxed $HOME with a PATH-shim stub
`claude` (responds to both `--version` and `auth status --json`), so the
tri-state `cc_logged_in`, the token presence/age derivation, the 1h probe
cache, and — critically — the no-token-leak invariant get direct coverage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"

_needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None,
    reason="requires bash + python3",
)

SYNTHETIC_TOKEN = "sk-ant-oat01-SYNTHETIC-DO-NOT-USE-abc123"
TEST_SSH_CONNECTION = "10.0.0.42 51234 10.0.0.1 22"


@pytest.fixture
def sandbox(tmp_path):
    """Sandboxed HOME + a stub `claude` on PATH whose auth-status answer is
    driven by $STUB_LOGGED_IN (true/false/garbage/fail)."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    state = home / ".local" / "state" / "genesis-guardian"
    (state / "shared" / "guardian").mkdir(parents=True)

    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    claude = fakebin / "claude"
    claude.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "1.2.3 (Claude Code)"; exit 0; fi\n'
        'if [ "$1" = "auth" ] && [ "$2" = "status" ]; then\n'
        '  case "${STUB_LOGGED_IN:-true}" in\n'
        '    true) echo \'{"loggedIn": true, "email": "priv@example.com"}\'; exit 0;;\n'
        '    false) echo \'{"loggedIn": false}\'; exit 0;;\n'
        '    garbage) echo "not json at all"; exit 0;;\n'
        '    fail) exit 1;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n",
    )
    claude.chmod(0o755)

    return {"home": home, "state": state, "fakebin": fakebin,
            "token_file": state / "shared" / "guardian" / "cc_oauth_token.env"}


def _version(sandbox: dict, logged_in: str = "true") -> dict:
    env = {
        "HOME": str(sandbox["home"]),
        "PATH": f"{sandbox['fakebin']}:/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": "version",
        "SSH_CONNECTION": TEST_SSH_CONNECTION,
        "STUB_LOGGED_IN": logged_in,
    }
    proc = subprocess.run(
        ["bash", str(GATEWAY)], env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _write_token(sandbox: dict, *, created_at: int | None) -> None:
    lines = [f"CLAUDE_CODE_OAUTH_TOKEN={SYNTHETIC_TOKEN}"]
    if created_at is not None:
        lines.append(f"GENESIS_CC_TOKEN_CREATED_AT={created_at}")
    sandbox["token_file"].write_text("\n".join(lines) + "\n")


@_needs_bash
def test_logged_in_true(sandbox):
    v = _version(sandbox, logged_in="true")
    assert v["cc_logged_in"] is True
    assert v["cc_token_present"] is False
    assert v["cc_token_age_days"] == -1


@_needs_bash
def test_logged_in_false(sandbox):
    assert _version(sandbox, logged_in="false")["cc_logged_in"] is False


@_needs_bash
def test_unparseable_auth_status_is_null(sandbox):
    # Non-JSON output → tri-state null (never a false-alarm on old/odd CC).
    assert _version(sandbox, logged_in="garbage")["cc_logged_in"] is None


@_needs_bash
def test_auth_status_failure_is_null(sandbox):
    assert _version(sandbox, logged_in="fail")["cc_logged_in"] is None


@_needs_bash
def test_token_present_with_created_at_age(sandbox):
    _write_token(sandbox, created_at=int(time.time()) - 3 * 86400)
    v = _version(sandbox)
    assert v["cc_token_present"] is True
    assert v["cc_token_age_days"] == 3


@_needs_bash
def test_token_age_mtime_fallback(sandbox):
    # No created_at line → age derived from the file mtime (>= 0).
    _write_token(sandbox, created_at=None)
    v = _version(sandbox)
    assert v["cc_token_present"] is True
    assert v["cc_token_age_days"] >= 0


@_needs_bash
def test_future_dated_created_at_clamps_to_zero(sandbox):
    _write_token(sandbox, created_at=int(time.time()) + 10 * 86400)
    assert _version(sandbox)["cc_token_age_days"] == 0


@_needs_bash
def test_token_value_never_leaks(sandbox):
    _write_token(sandbox, created_at=int(time.time()))
    env = {
        "HOME": str(sandbox["home"]),
        "PATH": f"{sandbox['fakebin']}:/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": "version",
        "SSH_CONNECTION": TEST_SSH_CONNECTION,
        "STUB_LOGGED_IN": "true",
    }
    proc = subprocess.run(
        ["bash", str(GATEWAY)], env=env, capture_output=True, text=True, timeout=60,
    )
    # Neither the token value nor the account email may appear in any output.
    assert SYNTHETIC_TOKEN not in proc.stdout
    assert SYNTHETIC_TOKEN not in proc.stderr
    assert "priv@example.com" not in proc.stdout


@_needs_bash
def test_probe_cache_reuse_and_expiry(sandbox):
    # 1st call logs in true → caches it.
    assert _version(sandbox, logged_in="true")["cc_logged_in"] is True
    cache = sandbox["state"] / "cc_auth_probe.json"
    assert cache.exists()

    # 2nd call: stub now says false, but the fresh cache must be reused → true.
    assert _version(sandbox, logged_in="false")["cc_logged_in"] is True

    # Age the cache past the 1h TTL → the stub is re-probed → false.
    d = json.loads(cache.read_text())
    d["checked_at"] -= 7200
    cache.write_text(json.dumps(d))
    assert _version(sandbox, logged_in="false")["cc_logged_in"] is False


@_needs_bash
def test_corrupt_cache_reprobes(sandbox):
    (sandbox["state"] / "cc_auth_probe.json").write_text("{not json")
    # Garbage cache must not crash the verb; it re-probes.
    assert _version(sandbox, logged_in="true")["cc_logged_in"] is True


@_needs_bash
def test_corrupt_leading_zero_created_at_no_crash(sandbox):
    # A leading-zero created_at must not be read as octal (would error under
    # set -e); the verb still emits valid JSON with a base-10 age.
    sandbox["token_file"].write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-X\n"
        "GENESIS_CC_TOKEN_CREATED_AT=0899999999\n",
    )
    v = _version(sandbox)  # _version asserts returncode 0 + json.loads
    assert v["cc_token_present"] is True
    assert isinstance(v["cc_token_age_days"], int)
