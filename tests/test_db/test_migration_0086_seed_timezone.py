"""Migration 0086 — one-time timezone seed (USER_TIMEZONE env → genesis.yaml).

The migration writes a FILE (genesis.yaml), not the DB, so we exercise the frozen
sync helper ``_seed_timezone_into_config`` directly and assert ``up`` is strictly
fail-open (``runtime/init/db.py`` aborts server startup if a migration raises).
``Path.home`` is patched — the seed uses the same path ``genesis.env._local_config``
reads. ``tz.reload`` is neutralized so the best-effort cache refresh can't mutate
the process-global ``_USER_TZ`` across tests.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import pytest
import yaml

_MOD = importlib.import_module("genesis.db.migrations.0086_seed_timezone_config")


def _cfg_path(home: Path) -> Path:
    return home / ".genesis" / "config" / "genesis.yaml"


def _write_cfg(home: Path, data: dict) -> None:
    p = _cfg_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data))


def _read_tz(home: Path):
    p = _cfg_path(home)
    if not p.is_file():
        return None
    return (yaml.safe_load(p.read_text()) or {}).get("timezone")


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    # Keep the best-effort cache refresh from touching the tz singleton.
    monkeypatch.setattr("genesis.util.tz.reload", lambda: None)
    return tmp_path


def test_seeds_when_file_utc_and_env_real(tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"timezone": "UTC", "github": {"user": "x"}})
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "Europe/Paris"
    # Other keys are preserved through the round-trip.
    data = yaml.safe_load(_cfg_path(tmp_path).read_text())
    assert data["github"] == {"user": "x"}


def test_seeds_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "Europe/Paris"


def test_seeds_when_no_timezone_key(tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"github": {"user": "x"}})
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "Europe/Paris"


def test_noop_when_file_has_real_zone(tmp_path, monkeypatch):
    # A real zone in the file is authoritative — the env must NOT clobber it.
    _write_cfg(tmp_path, {"timezone": "America/Chicago"})
    monkeypatch.setenv("USER_TIMEZONE", "Asia/Tokyo")
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "America/Chicago"


def test_noop_when_env_unset(tmp_path):
    _write_cfg(tmp_path, {"timezone": "UTC"})
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "UTC"


def test_noop_when_env_is_utc_case_insensitive(tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"timezone": "UTC"})
    monkeypatch.setenv("USER_TIMEZONE", "utc")
    _MOD._seed_timezone_into_config()
    assert _read_tz(tmp_path) == "UTC"


def test_idempotent(tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"timezone": "UTC"})
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
    _MOD._seed_timezone_into_config()
    _MOD._seed_timezone_into_config()  # file now real → second run is a no-op
    assert _read_tz(tmp_path) == "Europe/Paris"


def test_up_is_fail_open_on_error(monkeypatch):
    # If the seed raises, up() must swallow it — a raising migration aborts startup.
    def _boom() -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(_MOD, "_seed_timezone_into_config", _boom)
    asyncio.run(_MOD.up(None))  # must NOT raise


def test_up_applies_seed(tmp_path, monkeypatch):
    _write_cfg(tmp_path, {"timezone": "UTC"})
    monkeypatch.setenv("USER_TIMEZONE", "Europe/Paris")
    asyncio.run(_MOD.up(None))
    assert _read_tz(tmp_path) == "Europe/Paris"
