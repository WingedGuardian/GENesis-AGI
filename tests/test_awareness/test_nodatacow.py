"""Tests for the nodatacow drift check (_check_db_nodatacow).

On btrfs, a CoW SQLite DB suffers WAL write-amplification and chronic
fragmentation. The install sets chattr +C on data/; this monitor catches
regressions (a restore/recreate that dropped the flag). The filesystem probe
(ioctl FS_IOC_GETFLAGS) is mocked — CI runners are not btrfs.

The lock-safety tests at the bottom are NOT about nodatacow at all. They exist
because the in-process spelling of this probe was the root cause of repeated
SQLite corruption: see ``loop._nocow_flag``.
"""

from __future__ import annotations

import os
import sqlite3
from unittest.mock import AsyncMock

import pytest

from genesis.awareness import loop


def _locks_held_on(path) -> int:
    """How many POSIX locks THIS process holds on *path*.

    Keyed on (pid, inode), NOT on the ``dev:ino`` string /proc/locks prints.
    On btrfs ``os.stat`` reports a per-subvolume ANONYMOUS device that does not
    match the superblock device the kernel records — MEASURED 2026-09-21: stat
    said st_dev=59 ("00:3b") while /proc/locks printed "00:39" for the very same
    file, while ext4 agreed on both sides. A dev:ino matcher therefore passes on
    ext4 and silently counts ZERO on btrfs, which is the one filesystem where
    the bug these tests guard against actually fires.
    """
    ino = str(os.stat(path).st_ino)
    pid = str(os.getpid())
    n = 0
    with open("/proc/locks") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 8 and parts[4] == pid and parts[5].rsplit(":", 1)[-1] == ino:
                n += 1
    return n


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
    """Stub the nodatacow flag read.

    Deliberately NOT a patch of ``fcntl.ioctl``. That ioctl now runs in a CHILD
    process (``loop._nocow_flag``) precisely so its ``close()`` cannot drop the
    server's POSIX locks on the database, and a monkeypatch in this interpreter
    would not reach the child — it imports its own ``fcntl``. Patching here
    would silently stub nothing and leave the tests reading the real attribute
    off a tmp file, which passes or fails for reasons unrelated to the assertion.
    Patch the seam the parent actually calls.
    """

    async def _flag(_path):
        return bool(flags & loop._FS_NOCOW_FL)

    monkeypatch.setattr(loop, "_nocow_flag", _flag)


def _mock_flag_unreadable(monkeypatch):
    """The probe could not determine the flag — must mean silence, not an alarm."""

    async def _flag(_path):
        return None

    monkeypatch.setattr(loop, "_nocow_flag", _flag)


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
    """A probe failure (weird kernel/fs, spawn failure, timeout) must mean
    silence, not a false alarm. The out-of-process read reports that as None."""
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    _mock_flag_unreadable(monkeypatch)
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


# ── Lock safety — the reason this probe runs out of process ────────────────
#
# POSIX releases EVERY record lock a process holds on a file the moment that
# process closes ANY descriptor to it. SQLite's unix VFS defers its own closes
# to defend against this, but it cannot see a descriptor opened by other code in
# the same process. _check_db_nodatacow runs inside the server, which holds
# SQLite's locks on the database, and it used to open that database directly.
#
# Consequence, MEASURED 2026-09-21 on both ext4 and btrfs with controls: the
# holder's main-file lock went 1 -> 0, the next short-lived opener took an
# exclusive lock, concluded it was the last connection, checkpointed its partial
# view into main and unlinked the sidecars. The server kept writing through
# detached descriptors and integrity_check failed within minutes.


@pytest.fixture
def _live_wal_holder(tmp_path):
    """A real WAL connection holding a real POSIX lock, plus its db path."""
    db = tmp_path / "genesis.db"
    conn = sqlite3.connect(str(db))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x)")
    conn.execute("INSERT INTO t VALUES(1)")
    conn.commit()
    try:
        yield conn, db
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_flag_probe_does_not_drop_the_holders_lock(_live_wal_holder):
    """The acceptance bar: reading the flag must not disarm SQLite.

    Skips rather than passing vacuously when no lock is observable — a green run
    that measured nothing is worse here than an honest skip, because the entire
    assertion is "a number did not change".
    """
    _conn, db = _live_wal_holder
    before = _locks_held_on(db)
    if before == 0:
        pytest.skip("no POSIX lock observable on this platform — nothing to assert")

    result = await loop._nocow_flag(db)

    assert result in (True, False, None)  # never raises into the tick
    assert _locks_held_on(db) == before, (
        "reading the nodatacow flag dropped the holder's POSIX lock — this is the "
        "corruption root cause, not a style issue; keep the ioctl out of process"
    )


@pytest.mark.asyncio
async def test_the_inprocess_spelling_would_have_dropped_it(_live_wal_holder):
    """VERIFY-RED for the test above.

    Reproduces the ORIGINAL defective spelling and asserts that it DOES drop the
    lock. If this ever stops dropping, the guarantee asserted above has become
    untestable on this platform and its passing is no longer evidence.
    """
    import fcntl
    import struct

    _conn, db = _live_wal_holder
    before = _locks_held_on(db)
    if before == 0:
        pytest.skip("no POSIX lock observable on this platform — nothing to assert")

    # The spelling shipped until 2026-09-21, verbatim in shape.
    with open(db, "rb") as fh:
        fcntl.ioctl(fh.fileno(), loop._FS_IOC_GETFLAGS, struct.pack("l", 0))

    assert _locks_held_on(db) < before, (
        "the in-process open no longer drops the lock, so the fix above is not "
        "being exercised — re-derive the guarantee before trusting it"
    )


@pytest.mark.asyncio
async def test_tick_entry_point_does_not_drop_the_holders_lock(_live_wal_holder, monkeypatch):
    """The guarantee asserted at the CALLER, which is what production runs.

    Asserting it on `_nocow_flag` alone is not enough, and that gap is not
    hypothetical: an adversarial review reintroduced the full corruption defect
    by adding an innocuous-looking header check to `_check_db_nodatacow` —

        with open(db_path, "rb") as fh:
            if not fh.read(16).startswith(b"SQLite format 3") and False:
                return
        flag = await _nocow_flag(db_path)

    — which leaves the `_nocow_flag` seam completely intact. The suite stayed
    green at 11 passed while the holder's lock went 1 -> 0 through the real
    entry point. The docstring on `_check_db_nodatacow` warns against exactly
    this, and a warning nothing enforces is a comment.

    So: assert on the function the tick actually calls, not on its helper.
    """
    _conn, db = _live_wal_holder
    before = _locks_held_on(db)
    if before == 0:
        pytest.skip("no POSIX lock observable on this platform — nothing to assert")

    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: db)

    # db=None: we are measuring the probe path, not the observation write.
    await loop._check_db_nodatacow(None)

    assert _locks_held_on(db) == before, (
        "the awareness tick's own call path dropped the holder's POSIX lock — "
        "something in _check_db_nodatacow opened the database in-process again"
    )


@pytest.mark.asyncio
async def test_spawn_failure_reports_none_rather_than_raising(tmp_path, monkeypatch):
    """A broken interpreter path must be None, not an exception.

    The contract is ``bool | None``. Previously a bad ``sys.executable`` raised
    FileNotFoundError out of the probe and only the caller's blanket ``except``
    made it look like silence — so the declared contract was false and the
    mocked probe-failure test never exercised this path at all.
    """
    db = tmp_path / "genesis.db"
    db.write_bytes(b"sqlite")
    monkeypatch.setattr(loop.sys, "executable", str(tmp_path / "no-such-interpreter"))

    assert await loop._nocow_flag(db) is None
