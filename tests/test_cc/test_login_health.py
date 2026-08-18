"""Tests for container CC login-expiry health + fallback-token injection.

Origin 2026-08-18: the interactive claude.ai OAuth refresh token hard-expires
(routine access-token refresh does NOT extend it) and NOTHING monitored it —
zero readers of refreshTokenExpiresAt anywhere. Background sessions ride the
same credentials, so a lapsed login silently kills autonomy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.cc import login_health


def _write_creds(tmp_path, *, refresh_expires_ms: int | None) -> None:
    oauth: dict = {"accessToken": "at", "refreshToken": "rt"}
    if refresh_expires_ms is not None:
        oauth["refreshTokenExpiresAt"] = refresh_expires_ms
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": oauth}),
    )


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    login_health.reset_probe_cache()
    return tmp_path


def test_refresh_token_expiry_reads_timestamp(tmp_path):
    exp = datetime.now(UTC) + timedelta(days=3)
    _write_creds(tmp_path, refresh_expires_ms=_ms(exp))
    got = login_health.refresh_token_expiry()
    assert got is not None
    assert abs((got - exp).total_seconds()) < 2


def test_refresh_token_expiry_no_signal_cases(tmp_path):
    # Missing file → None (fresh/API-key-only installs stay silent).
    assert login_health.refresh_token_expiry() is None
    # Mid-rewrite garbage → None, never "expired".
    (tmp_path / ".credentials.json").write_text("{not json")
    assert login_health.refresh_token_expiry() is None
    # Present but no expiry field → None.
    _write_creds(tmp_path, refresh_expires_ms=None)
    assert login_health.refresh_token_expiry() is None


@pytest.mark.asyncio
async def test_no_injection_while_login_alive(tmp_path):
    """A future refresh expiry must NEVER probe nor inject — a working login
    is never overridden (credential_bridge honest boundary)."""
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=30)),
    )
    probes = []

    async def probe():
        probes.append(1)
        return True

    env = await login_health.fallback_env_if_login_dead(
        probe=probe,
        token_reader=lambda: "tok-x",
    )
    assert env is None
    assert probes == [], "must not probe while the timestamp says alive"


@pytest.mark.asyncio
async def test_injects_only_on_expired_plus_confirmed_logged_out(tmp_path):
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)),
    )

    async def confirmed_out():
        return True

    env = await login_health.fallback_env_if_login_dead(
        probe=confirmed_out,
        token_reader=lambda: "tok-fallback",
    )
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "tok-fallback"}


@pytest.mark.asyncio
async def test_ambiguous_probe_never_injects(tmp_path):
    """Mirror the guardian rule: never inject on ambiguity."""
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)),
    )

    async def ambiguous():
        return False  # not CONFIRMED logged-out

    env = await login_health.fallback_env_if_login_dead(
        probe=ambiguous,
        token_reader=lambda: "tok-x",
    )
    assert env is None


@pytest.mark.asyncio
async def test_no_token_file_no_injection(tmp_path):
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) - timedelta(hours=1)),
    )

    async def confirmed_out():
        return True

    env = await login_health.fallback_env_if_login_dead(
        probe=confirmed_out,
        token_reader=lambda: None,
    )
    assert env is None


@pytest.mark.asyncio
async def test_awareness_check_alerts_near_expiry_and_resolves(tmp_path, db):
    from genesis.awareness.loop import _check_cc_login_expiry

    # Near expiry (2 days) → one critical observation.
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=2)),
    )
    await _check_cc_login_expiry(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM observations WHERE source='cc_login_monitor' AND resolved_at IS NULL",
    )
    assert len(rows) == 1
    assert "expires" in dict(rows[0])["content"]

    # Duplicate-safe on the next tick.
    await _check_cc_login_expiry(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM observations WHERE source='cc_login_monitor' AND resolved_at IS NULL",
    )
    assert len(rows) == 1

    # User re-logs in (expiry far again) → auto-resolves.
    _write_creds(
        tmp_path,
        refresh_expires_ms=_ms(datetime.now(UTC) + timedelta(days=300)),
    )
    await _check_cc_login_expiry(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM observations WHERE source='cc_login_monitor' AND resolved_at IS NULL",
    )
    assert rows == []


@pytest.mark.asyncio
async def test_awareness_check_silent_without_credentials(tmp_path, db):
    from genesis.awareness.loop import _check_cc_login_expiry

    await _check_cc_login_expiry(db)
    rows = await db.execute_fetchall(
        "SELECT * FROM observations WHERE source='cc_login_monitor'",
    )
    assert rows == []
