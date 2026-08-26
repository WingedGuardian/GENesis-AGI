"""Tests for the interactive-slot OAuth decision gate (genesis.cc.login_gate).

The gate is a faithful mirror of CCInvoker's fallback contract for interactive
slots. `_decide()` returns the human notice string when the slot should inject,
or None otherwise; `main()` maps that to exit 0/1 and prints the notice.

Isolation notes:
- CLAUDE_CONFIG_DIR → tmp so credentials.json is per-test.
- login_health._TOKEN_FILE is monkeypatched (NOT read_fallback_token), because
  fallback_env_if_login_dead captured read_fallback_token as a default arg at
  import time — patching the file makes the REAL reader read our fixture, which
  is exactly the path the gate takes (it never passes token_reader).
- competing-auth env vars are cleared so the runner's own env can't skip the gate.
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


def _write_token(path, *, present: bool, age_days: float = 1.0) -> None:
    """Write the token file with a now-relative created_at (avoids a wall-clock
    time-bomb): age_days<365 = fresh, >365 = stale."""
    if present:
        created = int((datetime.now(UTC) - timedelta(days=age_days)).timestamp())
        path.write_text(
            f"CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat-testtoken\n"
            f"GENESIS_CC_TOKEN_CREATED_AT={created}\n",
        )


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    # Isolate credentials.json by patching the resolver directly — NOT by setting
    # CLAUDE_CONFIG_DIR, which the gate now (correctly) treats as competing-auth.
    creds = tmp_path / ".credentials.json"
    monkeypatch.setattr(login_health, "credentials_path", lambda: creds)
    monkeypatch.delenv("GENESIS_CC_SLOT_OAUTH", raising=False)
    for name in login_gate._COMPETING_AUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    token_file = tmp_path / "cc_oauth_token.env"
    monkeypatch.setattr(login_health, "_TOKEN_FILE", token_file)
    login_health.reset_probe_cache()
    return tmp_path, token_file


def _patch_probe(monkeypatch, *, logged_out: bool) -> None:
    async def _probe(cc_path: str = "claude") -> bool:
        return logged_out

    monkeypatch.setattr(login_health, "probe_logged_out", _probe)


def _expired(days: int = 1):
    return _ms(datetime.now(UTC) - timedelta(hours=days * 24))


def _future(days: int = 5):
    return _ms(datetime.now(UTC) + timedelta(days=days))


# --- conditional mode (the default / chosen model) -------------------------


@pytest.mark.asyncio
async def test_conditional_no_inject_when_login_alive(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_future())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._decide() is None


@pytest.mark.asyncio
async def test_conditional_injects_when_expired_and_logged_out(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._decide() == login_gate._NOTICE_CONDITIONAL


@pytest.mark.asyncio
async def test_conditional_no_inject_when_probe_ambiguous(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=False)
    assert await login_gate._decide() is None


@pytest.mark.asyncio
async def test_conditional_no_inject_when_no_token(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=False)
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._decide() is None


@pytest.mark.asyncio
async def test_conditional_no_inject_when_token_stale(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True, age_days=400)  # past ~1yr life
    _patch_probe(monkeypatch, logged_out=True)
    assert await login_gate._decide() is None


def test_conditional_notice_directs_restart_not_just_login():
    # After a conditional inject, CLAUDE_CODE_OAUTH_TOKEN overrides /login for the
    # process lifetime, so /login alone can't restore connectors — the notice MUST
    # tell the user to restart the slot (parity with the always notice).
    assert "restart" in login_gate._NOTICE_CONDITIONAL
    assert "restart" in login_gate._NOTICE_ALWAYS


# --- off / unknown lever (fail-closed) --------------------------------------


@pytest.mark.asyncio
async def test_off_never_injects(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "off")
    assert await login_gate._decide() is None


@pytest.mark.asyncio
async def test_unknown_lever_fails_closed(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "of")  # typo of "off"
    assert await login_gate._decide() is None


# --- competing-auth exclusion (invoker parity) ------------------------------


@pytest.mark.parametrize("var", login_gate._COMPETING_AUTH_ENV)
@pytest.mark.asyncio
async def test_competing_auth_blocks_injection(_isolated, monkeypatch, var):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    monkeypatch.setenv(var, "something")
    # Even in always mode (which bypasses the login gate), a competing auth blocks.
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._decide() is None


# --- always mode ------------------------------------------------------------


@pytest.mark.asyncio
async def test_always_injects_regardless_of_live_login(_isolated, monkeypatch):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_future())  # login ALIVE
    _write_token(token_file, present=True)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._decide() == login_gate._NOTICE_ALWAYS


@pytest.mark.asyncio
async def test_always_refuses_stale_token(_isolated, monkeypatch, capsys):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_future())
    _write_token(token_file, present=True, age_days=400)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._decide() is None
    # always mode must EXPLAIN why it did not inject (parity with other branches).
    assert "past its ~1-year life" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_always_no_inject_when_no_token(_isolated, monkeypatch, capsys):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_future())
    _write_token(token_file, present=False)
    monkeypatch.setenv("GENESIS_CC_SLOT_OAUTH", "always")
    assert await login_gate._decide() is None
    assert "no setup-token is stored" in capsys.readouterr().err


# --- main() exit codes + notice on stdout + fail-closed ---------------------


def test_main_returns_1_when_not_injecting(_isolated, capsys):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_future())
    _write_token(token_file, present=True)
    assert login_gate.main() == 1
    assert capsys.readouterr().out == ""  # nothing on stdout when not injecting


def test_main_returns_0_and_prints_notice_when_injecting(_isolated, monkeypatch, capsys):
    config_dir, token_file = _isolated
    _write_creds(config_dir, refresh_expires_ms=_expired())
    _write_token(token_file, present=True)
    _patch_probe(monkeypatch, logged_out=True)
    assert login_gate.main() == 0
    assert login_gate._NOTICE_CONDITIONAL in capsys.readouterr().out


def test_main_fails_closed_on_error(_isolated, monkeypatch):
    async def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(login_gate, "_decide", _boom)
    assert login_gate.main() == 1
