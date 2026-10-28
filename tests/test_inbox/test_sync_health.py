"""Inbox sync staleness alert (#3183): genesis.inbox.sync_health.

The pure ``assess`` is checked against every heartbeat state; the async check is
driven against a real in-memory observations table with the config and the
heartbeat path patched, so the raise / dedup / resolve cycle is the real CRUD.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.db.crud import observations as obs
from genesis.db.schema._tables import TABLES
from genesis.inbox import sync_health
from genesis.inbox.sync_health import STALE_AFTER, assess

_NOW = datetime(2026, 10, 28, 18, 0, tzinfo=UTC)
_REMOTE = "dropbox:Apps/vault"


def _hb(last_ok: str, held: int = 0) -> str:
    return json.dumps({"last_run": "2026-10-28T17:59:00Z", "last_ok": last_ok, "held": held})


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_unconfigured_sync_is_silent_whatever_the_heartbeat():
    assert assess("", None, _NOW) == []
    assert assess("", "garbage", _NOW) == []


def test_fresh_heartbeat_is_healthy():
    assert assess(_REMOTE, _hb(_iso(_NOW - timedelta(minutes=5))), _NOW) == []


def test_missing_heartbeat_is_stale():
    [p] = assess(_REMOTE, None, _NOW)
    assert p.key == "stale" and p.priority == "high"
    assert "never completed" in p.message


def test_old_last_ok_is_stale():
    [p] = assess(_REMOTE, _hb(_iso(_NOW - STALE_AFTER - timedelta(minutes=1))), _NOW)
    assert p.key == "stale"
    assert "seen-in-vault" in p.message  # the restore warning travels with the alert


def test_exactly_at_the_window_is_not_stale():
    assert assess(_REMOTE, _hb(_iso(_NOW - STALE_AFTER)), _NOW) == []


def test_empty_last_ok_is_stale():
    [p] = assess(_REMOTE, _hb(""), _NOW)
    assert p.key == "stale" and "ever" in p.message


def test_unreadable_heartbeat_is_reported_not_ignored():
    for text in ("not json", "[1, 2]", '{"held": "x"}'):
        [p] = assess(_REMOTE, text, _NOW)
        assert p.key == "unreadable", text


def test_held_responses_raise_a_separate_alert():
    problems = assess(_REMOTE, _hb(_iso(_NOW), held=2), _NOW)
    assert [p.key for p in problems] == ["held"]
    assert problems[0].priority == "medium" and "2 response" in problems[0].message


def test_stale_and_held_together():
    problems = assess(_REMOTE, _hb(_iso(_NOW - timedelta(hours=2)), held=1), _NOW)
    assert sorted(p.key for p in problems) == ["held", "stale"]


# --- the async check against a real observations table ------------------------


@pytest.fixture
async def obs_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(TABLES["observations"])
    await db.commit()
    yield db
    await db.close()


def _patch(monkeypatch, tmp_path: Path, remote: str) -> Path:
    hb = tmp_path / "inbox_sync.json"
    monkeypatch.setattr("genesis.inbox.sync_config.heartbeat_path", lambda: hb)
    monkeypatch.setattr(
        "genesis.inbox.sync_config.load_sync_settings", lambda: (remote, tmp_path / "inbox")
    )
    return hb


async def _open_alerts(db) -> list[dict]:
    cur = await db.execute(
        "SELECT content, priority FROM observations "
        "WHERE source = ? AND type = 'infrastructure_alert' AND resolved = 0",
        (sync_health.SOURCE,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def test_stale_alert_is_raised_once_and_resolved_when_fresh(obs_db, monkeypatch, tmp_path):
    hb = _patch(monkeypatch, tmp_path, _REMOTE)
    hb.write_text(_hb("2026-01-01T00:00:00Z"))

    await sync_health.check_inbox_sync_health(obs_db)
    await sync_health.check_inbox_sync_health(obs_db)  # second tick: no duplicate
    alerts = await _open_alerts(obs_db)
    assert len(alerts) == 1 and alerts[0]["priority"] == "high"

    hb.write_text(_hb(_iso(datetime.now(UTC))))
    await sync_health.check_inbox_sync_health(obs_db)
    assert await _open_alerts(obs_db) == []


async def test_unconfigured_check_writes_nothing(obs_db, monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, "")
    await sync_health.check_inbox_sync_health(obs_db)
    assert await _open_alerts(obs_db) == []


async def test_held_alert_resolves_without_touching_a_stale_one(obs_db, monkeypatch, tmp_path):
    hb = _patch(monkeypatch, tmp_path, _REMOTE)
    hb.write_text(_hb("2026-01-01T00:00:00Z", held=1))
    await sync_health.check_inbox_sync_health(obs_db)
    assert len(await _open_alerts(obs_db)) == 2

    hb.write_text(_hb("2026-01-01T00:00:00Z", held=0))  # still stale, no longer held
    await sync_health.check_inbox_sync_health(obs_db)
    alerts = await _open_alerts(obs_db)
    assert len(alerts) == 1 and alerts[0]["priority"] == "high"


async def test_symlinked_heartbeat_is_treated_as_missing(obs_db, monkeypatch, tmp_path):
    hb = _patch(monkeypatch, tmp_path, _REMOTE)
    real = tmp_path / "elsewhere.json"
    real.write_text(_hb(_iso(datetime.now(UTC))))
    hb.symlink_to(real)
    await sync_health.check_inbox_sync_health(obs_db)
    [alert] = await _open_alerts(obs_db)
    assert "never completed" in alert["content"]


async def test_check_never_raises_into_the_tick(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, _REMOTE)

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(obs, "create", boom)
    await sync_health.check_inbox_sync_health(object())  # must not raise
    await sync_health.check_inbox_sync_health(None)


async def test_unreadable_config_raises_an_alert_and_clears_when_fixed(obs_db, monkeypatch, tmp_path):
    """A broken overlay stops the sync; the check must say so, not skip (B1)."""
    hb = tmp_path / "inbox_sync.json"
    monkeypatch.setattr("genesis.inbox.sync_config.heartbeat_path", lambda: hb)

    def broken():
        raise ValueError("overlay is not valid YAML")

    monkeypatch.setattr("genesis.inbox.sync_config.load_sync_settings", broken)
    await sync_health.check_inbox_sync_health(obs_db)
    [alert] = await _open_alerts(obs_db)
    assert alert["priority"] == "high" and "cannot be read" in alert["content"]

    monkeypatch.setattr(
        "genesis.inbox.sync_config.load_sync_settings", lambda: (_REMOTE, tmp_path / "inbox")
    )
    hb.write_text(_hb(_iso(datetime.now(UTC))))
    await sync_health.check_inbox_sync_health(obs_db)
    assert await _open_alerts(obs_db) == []
