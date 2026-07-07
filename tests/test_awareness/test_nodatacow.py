"""Tests for the nodatacow drift check (_check_db_nodatacow).

On btrfs, a CoW SQLite DB suffers WAL write-amplification and chronic
fragmentation. The install sets chattr +C on data/; this monitor catches
regressions (a restore/recreate that dropped the flag). The filesystem probe
(ioctl FS_IOC_GETFLAGS) is mocked — CI runners are not btrfs.
"""
from __future__ import annotations

import struct
from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


@pytest.fixture(autouse=True)
def _reset_cooldown():
    loop._last_nocow_alert_at = None
    yield
    loop._last_nocow_alert_at = None


@pytest.fixture
def _db_file(tmp_path, monkeypatch):
    db_path = tmp_path / "genesis.db"
    db_path.write_bytes(b"sqlite")
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db_path)
    return db_path


def _mock_flags(monkeypatch, flags: int):
    monkeypatch.setattr(
        "fcntl.ioctl", lambda _fd, _op, _buf: struct.pack("l", flags)
    )


@pytest.mark.asyncio
async def test_alerts_when_cow_on_btrfs(_db_file, monkeypatch):
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flags(monkeypatch, 0)  # no FS_NOCOW_FL
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_called_once()
    assert spy.call_args.kwargs["priority"] == "high"
    assert spy.call_args.kwargs["type"] == "infrastructure_alert"
    assert spy.call_args.kwargs["source"] == "nodatacow_monitor"


@pytest.mark.asyncio
async def test_no_alert_when_nocow_set(_db_file, monkeypatch):
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flags(monkeypatch, loop._FS_NOCOW_FL)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_non_btrfs_is_exempt(_db_file, monkeypatch):
    """The flag is meaningless on ext4/tmpfs — never alert there."""
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "ext4")
    _mock_flags(monkeypatch, 0)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_one_alert_per_day(_db_file, monkeypatch):
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flags(monkeypatch, 0)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())
    await loop._check_db_nodatacow(object())

    spy.assert_called_once()  # second call suppressed by the 24h cooldown


@pytest.mark.asyncio
async def test_probe_failure_never_alerts(_db_file, monkeypatch):
    """An ioctl error (weird kernel/fs) must mean silence, not a false alarm."""
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")

    def _boom(_fd, _op, _buf):
        raise OSError("ioctl unsupported")

    monkeypatch.setattr("fcntl.ioctl", _boom)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_missing_db_never_alerts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "genesis.env.genesis_db_path", lambda: tmp_path / "absent.db"
    )
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_no_db_handle_skips_write(_db_file, monkeypatch):
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flags(monkeypatch, 0)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(None)

    spy.assert_not_called()


@pytest.mark.asyncio
async def test_first_alert_fires_on_fresh_boot_small_monotonic(_db_file, monkeypatch):
    """Same monotonic-since-boot regression class as the WAL alert: the None
    sentinel must fire the first alert even at ~5s uptime."""
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flags(monkeypatch, 0)
    monkeypatch.setattr(loop.time, "monotonic", lambda: 5.0)
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())

    spy.assert_called_once()


def test_fs_type_for_smoke():
    """Real /proc/mounts parse: '/' resolves to some fs type; a bogus path
    still resolves via its containing mount (never raises)."""
    assert loop._fs_type_for("/") is not None
    loop._fs_type_for("/definitely/not/a/real/path")  # must not raise
