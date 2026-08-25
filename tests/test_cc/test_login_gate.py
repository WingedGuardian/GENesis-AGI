"""Tests for the interactive-slot OAuth decision gate (genesis.cc.login_gate).

The gate is the CLI that scripts/cc-slot.sh runs on slot CREATE to decide
whether to inject the stored setup-token. It reuses
login_health.fallback_env_if_login_dead as the single decision authority, so
these tests exercise the lever branches + the login-dead-conditional path
through that shared gate.

Isolation notes:
- CLAUDE_CONFIG_DIR is pointed at tmp so credentials.json is per-test.
- login_health._TOKEN_FILE is monkeypatched (NOT read_fallback_token), because
  fallback_env_if_login_dead captured read_fallback_token as a default arg at
  import time — patching the file makes the REAL reader read our fixture, which
  is exactly the path the gate takes (it never passes token_reader).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.cc import login_gate, login_health


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _write_creds(config_dir, *, refresh_expires_ms: int | None) -> None:
    oauth: dict = {}
    if refresh_expires_ms is not None:
        oauth["refreshTokenExpiresAt"] = refresh_expires_ms
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": oauth}),
    )


def _write_token(path, *, present: bool) -> None:
    if present:
        path.write_text(
            "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-testtoken\n"
            "GENESIS_CC_TOKEN_CREATED_AT=1700000000\n",
        )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Per-test credentials.json + token file; clear the probe cache."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("GENESIS_CC_SLOT_OAUTH", raising=False)
    token_file = tmp_path / "cc_oauth_token.env"
    monkeypatch.setattr(login_health, "_TOKEN_FILE", token_file)
    login_health.reset_probe_cache()
    return tmp_path, token_file


def _patch_probe(monkeypatch, *, logged_out: bool) -> None:
    async def _probe(cc_path: str = "claude") -> bool:
        return logged_out

    monkeypatch.setattr(login_health, "probe_logged_out", _probe)


# --- conditional mode (the default / chosen model) -------------------------


@pytest.mark.asyncio
async def test_conditional_no_inject_when_login_alive(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=5)))
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)  # even if probe would say out
    assert await login_gate._should_inject() is False


@pytest.mark.asyncio
async def test_conditional_injects_when_expired_and_logged_out(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)))
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._should_inject() is True


@pytest.mark.asyncio
async def test_conditional_no_inject_when_probe_ambiguous(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)))
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=False)  # not confirmed logged-out
    assert await login_gate._should_inject() is False


@pytest.mark.asyncio
async def test_conditional_no_inject_when_no_token(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)))
    _write_token(token_file, present=False)
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._should_inject() is False


# --- off (kill switch) ------------------------------------------------------


@pytest.mark.asyncio
async def test_off_never_injects(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)))
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "off")
    assert await login_gate._should_inject() is False


# --- always (force onto token, bypass login gate) ---------------------------


@pytest.mark.asyncio
async def test_always_injects_regardless_of_live_login(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    # Login is ALIVE — always mode still injects because a token exists.
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=5)))
    _write_token(token_file, present=True)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._should_inject() is True


@pytest.mark.asyncio
async def test_always_no_inject_when_no_token(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=5)))
    _write_token(token_file, present=False)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._should_inject() is False


# --- main() exit codes + fail-closed ---------------------------------------


def test_main_returns_1_when_not_injecting(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=5)))
    _write_token(token_file, present=True)
    assert login_gate.main() == 1


def test_main_returns_0_when_injecting(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)))
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    assert login_gate.main() == 0


def test_main_fails_closed_on_error(_isolated, monkeypatch):
    # Any unexpected error in the decision path must NOT inject (status quo).
    async def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(login_gate, "_should_inject", _boom)
    assert login_gate.main() == 1
