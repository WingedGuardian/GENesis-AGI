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
    loop._stuck_nocow_probe = None
    loop._stuck_nocow_probe_since = None
    yield
    loop._last_nocow_alert_at = None
    loop._stuck_nocow_probe = None
    loop._stuck_nocow_probe_since = None


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
    import errno
    import fcntl
    import struct

    _conn, db = _live_wal_holder
    before = _locks_held_on(db)
    if before == 0:
        pytest.skip("no POSIX lock observable on this platform — nothing to assert")

    # The spelling shipped until 2026-09-21, in shape — now wrapped in a `try`.
    #
    # The ioctl is INCIDENTAL here: what drops the lock is the CLOSE, so the
    # `with` block has already performed the thing under test by the time any
    # ioctl error surfaces, and Python closes the handle on the exception path.
    # MEASURED on both tmpfs and ext4: locks 1 -> 0 either way.
    #
    # So the assertion must not be gated on errno taxonomy. An earlier revision
    # allowed only ENOTTY, on the stated grounds that "tmpfs/overlay/NFS answer
    # ENOTTY" — MEASURED FALSE for tmpfs, where shmem implements fileattr_get
    # and the ioctl SUCCEEDS (/dev/shm, kernel 6.8). Worse, a single-value
    # allowlist turns any filesystem answering EOPNOTSUPP/ENOSYS/EINVAL into a
    # hard RED, which is worse than the silent skip it replaced. Tolerate the
    # whole unsupported class; anything else is re-raised rather than swallowed.
    _UNSUPPORTED = {errno.ENOTTY, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}
    try:
        with open(db, "rb") as fh:
            fcntl.ioctl(fh.fileno(), loop._FS_IOC_GETFLAGS, struct.pack("l", 0))
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED:
            raise

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


@pytest.mark.asyncio
async def test_reaping_an_unkillable_child_does_not_hang_the_tick(monkeypatch):
    """The reap is bounded, or the probe's own timeout is worthless.

    SIGKILL cannot complete while a child sits in uninterruptible sleep — which
    is the exact failure mode the probe timeout exists to bound. An unbounded
    ``await proc.wait()`` in the reaper would therefore defeat that timeout and
    hang the awareness tick forever: bounded probe, unbounded reaper.

    Simulated with a child whose wait() never returns, so the assertion is about
    the reaper's structure rather than about provoking real D-state.
    """
    import asyncio
    import time

    class _Unkillable:
        def kill(self):
            pass

        async def wait(self):
            # Deliberately FINITE, and much larger than the bound below. A truly
            # unbounded sleep would make the un-fixed code HANG pytest rather
            # than fail it, which cannot be mutation-verified — a test whose
            # failure mode is "runs forever" tells you nothing on the day it
            # regresses.
            await asyncio.sleep(5)

    monkeypatch.setattr(loop, "_NOCOW_REAP_TIMEOUT_S", 0.05)

    start = time.monotonic()
    await loop._kill_probe(_Unkillable())
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, (
        f"_kill_probe blocked for {elapsed:.1f}s on a child that never exits — "
        "the reap is unbounded and will hang the tick"
    )


@pytest.mark.asyncio
async def test_cancelling_the_probe_stays_cancelled_and_yields_no_verdict(monkeypatch):
    """The invariant asserted at the PRODUCTION CALL SITE, not on the helper.

    This is the second time in this PR that a guarantee was locked one layer
    below where it has to hold, and a stored procedure from this same lineage
    names it: the regression test must assert the invariant at the production
    call site, because a test that exercises only the extracted helper stays
    green when someone reintroduces the hazard next to it in the caller.

    MEASURED against the whole package (447 tests), with `_kill_probe` left
    perfect, these all SURVIVED before this test existed:
      * an extra unbounded `await proc.wait()` beside the `_kill_probe` call
      * deleting `_nocow_flag`'s `except asyncio.CancelledError` block entirely
      * changing that block's `raise` to `return None` — the exact "shrug" the
        docstring forbids
      * widening `_kill_probe`'s `except TimeoutError` to `except BaseException`,
        which swallows cancellation

    So: cancel a REAL probe mid-flight, against a real subprocess, and require
    that the cancellation survives as a cancellation.
    """
    import asyncio
    from pathlib import Path

    # A probe that will not finish on its own, so the cancel lands mid-flight.
    monkeypatch.setattr(loop, "_NOCOW_PROBE_SRC", "import time; time.sleep(60)\n")

    reaped = []
    original_kill = loop._kill_probe

    async def _spy(proc):
        reaped.append(proc)
        await original_kill(proc)

    monkeypatch.setattr(loop, "_kill_probe", _spy)

    task = asyncio.create_task(loop._nocow_flag(Path("/etc/hostname")))
    await asyncio.sleep(0.3)  # let the child actually spawn
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled(), "cancellation was converted into a verdict"

    # The propagation assertion above is NOT enough on its own, and finding that
    # out cost a mutation round: deleting the whole `except CancelledError` block
    # leaves cancellation propagating NATURALLY out of wait_for, so the task is
    # still cancelled and the assertion still passes — while the child leaks.
    # Killing the child is what that handler is FOR, so that is what to assert.
    assert reaped, "cancellation propagated but the child was never reaped"


@pytest.mark.asyncio
async def test_the_timeout_path_is_bounded_at_the_call_site(monkeypatch):
    """The timeout handler, not the helper — a separate path from cancellation.

    MEASURED as a surviving mutant before this test existed: an extra unbounded
    `await proc.wait()` placed NEXT TO the bounded `_kill_probe` call, inside
    `_nocow_flag`'s timeout handler, kept the suite green. A perfect helper does
    not make its caller correct.
    """
    import asyncio
    import time
    from pathlib import Path

    monkeypatch.setattr(loop, "_NOCOW_PROBE_SRC", "import time; time.sleep(60)\n")
    monkeypatch.setattr(loop, "_NOCOW_PROBE_TIMEOUT_S", 0.2)
    monkeypatch.setattr(loop, "_NOCOW_REAP_TIMEOUT_S", 0.2)

    start = time.monotonic()
    result = await asyncio.wait_for(loop._nocow_flag(Path("/etc/hostname")), timeout=20)
    elapsed = time.monotonic() - start

    assert result is None, "a timed-out probe must report None, not a verdict"
    assert elapsed < 5.0, (
        f"the timeout path took {elapsed:.1f}s against a 0.2s probe bound and a "
        "0.2s reap bound — something on the call site waits unbounded"
    )


# ---------------------------------------------------------------------------
# At most ONE un-reaped probe child, ever.
#
# Review finding (Devin, PR #2226): _kill_probe abandons a child that survives
# SIGKILL — correct, because waiting on an uninterruptible child would defeat
# the very timeout it serves. But abandoning is not bounding. The check runs
# hourly, so a filesystem wedged for a week leaves ~168 un-reaped children and
# as many asyncio child-watcher registrations. The bound has to live in the
# CALLER, which is what these tests pin.
# ---------------------------------------------------------------------------


class _WedgedProc:
    """A probe child that ignores SIGKILL until explicitly released.

    Stands in for a child in uninterruptible sleep — the one state where
    ``kill()`` is accepted and the process still does not exit. Nothing in the
    test suite can produce a real D-state process (it needs a wedged
    filesystem), so this models the contract the code depends on: ``kill()``
    returns, and ``returncode`` stays None.
    """

    def __init__(self, pid: int = 424242) -> None:
        import asyncio as _asyncio

        self.pid = pid
        self.returncode = None
        self.kills = 0
        self._released = _asyncio.Event()

    def kill(self) -> None:
        self.kills += 1

    async def wait(self):
        await self._released.wait()
        return self.returncode

    async def communicate(self):
        await self._released.wait()
        return b"", b""

    def release(self, code: int = 0) -> None:
        """The child finally exits. Mirrors asyncio's child watcher setting
        ``returncode`` with nobody awaiting — which is what lets the latch clear
        itself without a timer."""
        self.returncode = code
        self._released.set()


class _KillableProc(_WedgedProc):
    """A child that DOES die on SIGKILL — the ordinary timeout case.

    This is the discriminating control. If the latch fired for every timeout
    rather than only for children that survive the kill, a single slow probe
    would disable the check until the next restart.
    """

    def kill(self) -> None:
        self.kills += 1
        self.release(-9)


def _spawn_returning(monkeypatch, *procs):
    """Patch the spawn seam; return a list that records each spawn."""
    import asyncio as _asyncio

    spawned: list = []
    queue = list(procs)

    async def _fake_exec(*_a, **_kw):
        proc = queue.pop(0) if queue else _WedgedProc()
        spawned.append(proc)
        return proc

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(loop, "_NOCOW_PROBE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(loop, "_NOCOW_REAP_TIMEOUT_S", 0.02)
    return spawned


@pytest.mark.asyncio
async def test_a_wedged_probe_is_not_spawned_beside(tmp_path, monkeypatch):
    """THE ACCEPTANCE BAR. Hour two must not add a second stuck child."""
    from pathlib import Path

    wedged = _WedgedProc()
    spawned = _spawn_returning(monkeypatch, wedged, _WedgedProc(pid=999999))

    first = await loop._nocow_flag(Path(tmp_path / "genesis.db"))
    assert first is None
    assert len(spawned) == 1
    assert wedged.kills == 1, "the first probe must still be SIGKILLed"
    assert loop._stuck_nocow_probe is wedged, "the survivor must be recorded"

    second = await loop._nocow_flag(Path(tmp_path / "genesis.db"))

    assert second is None, "declining to probe reports 'could not tell'"
    assert len(spawned) == 1, (
        f"a second probe was spawned beside a child that never exited "
        f"({len(spawned)} spawns) — this is the per-hour accumulation"
    )


@pytest.mark.asyncio
async def test_the_latch_clears_once_the_stuck_child_exits(tmp_path, monkeypatch):
    """Self-healing, with no timer and no cleanup path: the next call clears it."""
    from pathlib import Path

    wedged = _WedgedProc()
    healthy = _KillableProc(pid=555)
    spawned = _spawn_returning(monkeypatch, wedged, healthy)

    await loop._nocow_flag(Path(tmp_path / "genesis.db"))
    assert loop._stuck_nocow_probe is wedged

    wedged.release(0)  # the filesystem un-wedges; the watcher sets returncode

    await loop._nocow_flag(Path(tmp_path / "genesis.db"))

    assert len(spawned) == 2, "probing must resume once the survivor is gone"
    assert loop._stuck_nocow_probe is not wedged


@pytest.mark.asyncio
async def test_a_timeout_whose_child_died_does_not_latch(tmp_path, monkeypatch):
    """THE CONTROL. A latch that fires on every timeout would be a worse bug
    than the one being fixed: one slow probe would disable the check until the
    process restarts. Only a child that survives SIGKILL may latch."""
    from pathlib import Path

    killable = _KillableProc()
    spawned = _spawn_returning(monkeypatch, killable, _KillableProc(pid=2))

    await loop._nocow_flag(Path(tmp_path / "genesis.db"))
    assert killable.kills == 1
    assert killable.returncode is not None, "fixture must model a collected child"
    assert loop._stuck_nocow_probe is None, (
        "a child that DIED on SIGKILL is not a survivor and must not latch"
    )

    await loop._nocow_flag(Path(tmp_path / "genesis.db"))
    assert len(spawned) == 2, "an ordinary timeout must not suppress later checks"


@pytest.mark.asyncio
async def test_cancellation_also_records_a_surviving_child(tmp_path, monkeypatch):
    """Shutdown takes the same abandon path, so it needs the same bound.

    Without this, a cancelled tick would leak a survivor that nothing tracks —
    and the next probe would spawn beside it.
    """
    import asyncio as _asyncio
    from pathlib import Path

    wedged = _WedgedProc()
    _spawn_returning(monkeypatch, wedged)

    task = _asyncio.ensure_future(loop._nocow_flag(Path(tmp_path / "genesis.db")))
    await _asyncio.sleep(0)  # let it reach the await on communicate()
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task

    assert wedged.kills == 1, "cancellation must still SIGKILL the child"
    assert loop._stuck_nocow_probe is wedged, (
        "a child abandoned by CANCELLATION accumulates exactly like one "
        "abandoned by timeout"
    )


def _slow_kill_seam(monkeypatch):
    """Replace `_kill_probe` with one that parks inside the reap.

    `_kill_probe` awaits, which makes it a cancellation point — the whole
    subject of these two tests. Parking there lets a cancellation be delivered
    at exactly that await rather than hoping to hit a real 0.02s window.
    """
    import asyncio as _asyncio

    reap_entered = _asyncio.Event()

    async def _slow_kill(proc):
        proc.kill()
        reap_entered.set()
        await _asyncio.sleep(3600)

    monkeypatch.setattr(loop, "_kill_probe", _slow_kill)
    return reap_entered


@pytest.mark.asyncio
async def test_timeout_path_records_before_the_cancellable_reap(
    tmp_path, monkeypatch
):
    """Record BEFORE the cancellable await, not after — the TIMEOUT path.

    An earlier revision assigned the latch after `_kill_probe`. Review
    reproduced the consequence by execution: cancel the tick mid-reap and
    CancelledError propagates past the assignment, leaving the child killed but
    UNTRACKED, so the next hourly check spawns beside it. That is the exact
    accumulation this feature exists to prevent, reachable through the
    feature's own cleanup path.

    NOTE FOR WHOEVER EDITS THIS: an earlier version of this test claimed to
    cover the CANCELLATION path and did not — it reaches `_kill_probe` via the
    timeout, so mutating the cancellation path left it green. Mutation caught
    that; reading it did not. The two paths need the two tests below.
    """
    import asyncio as _asyncio
    from pathlib import Path

    wedged = _WedgedProc()
    _spawn_returning(monkeypatch, wedged)
    reap_entered = _slow_kill_seam(monkeypatch)

    task = _asyncio.ensure_future(loop._nocow_flag(Path(tmp_path / "genesis.db")))
    await _asyncio.wait_for(reap_entered.wait(), timeout=5)  # timeout fired
    task.cancel()
    with pytest.raises(_asyncio.CancelledError):
        await task

    assert loop._stuck_nocow_probe is wedged, (
        "a cancellation inside the timeout path's reap left the killed child "
        "untracked — the next check will spawn beside it"
    )


@pytest.mark.asyncio
async def test_cancel_path_records_before_the_cancellable_reap(
    tmp_path, monkeypatch
):
    """Same ordering, on the CANCELLATION path — where it is the ordinary case.

    This handler runs *because* something cancelled us, so a second
    cancellation arriving while it reaps is not exotic: it is what a shutdown
    that stops waiting looks like. Getting here needs the first cancel to land
    on `communicate()` BEFORE the timeout fires, which is why this test cancels
    immediately instead of waiting for the reap like the one above.
    """
    import asyncio as _asyncio
    from pathlib import Path

    wedged = _WedgedProc()
    _spawn_returning(monkeypatch, wedged)
    monkeypatch.setattr(loop, "_NOCOW_PROBE_TIMEOUT_S", 30)  # timeout must NOT win
    reap_entered = _slow_kill_seam(monkeypatch)

    task = _asyncio.ensure_future(loop._nocow_flag(Path(tmp_path / "genesis.db")))
    await _asyncio.sleep(0)  # park on communicate()
    task.cancel()  # -> CancelledError handler -> the reap
    await _asyncio.wait_for(reap_entered.wait(), timeout=5)
    task.cancel()  # second cancel, delivered INSIDE the reap
    with pytest.raises(_asyncio.CancelledError):
        await task

    assert loop._stuck_nocow_probe is wedged, (
        "a cancellation inside the cancel path's reap left the killed child "
        "untracked"
    )


@pytest.mark.asyncio
async def test_the_caller_stops_spawning_too(_db_file, monkeypatch):
    """THE GUARANTEE AT THE CALL SITE, not one layer below it.

    Every other test here drives `_nocow_flag` directly. That is the mistake
    this file already records twice (see the docstrings above on asserting a
    guarantee at the helper rather than at its caller) and which a BLOCKER on
    the parent PR caught once — a mutant that moved the check into
    `_check_db_nodatacow` kept eleven helper-level tests green while dropping
    the real guarantee. `_check_db_nodatacow` is what the awareness tick calls,
    so the bound has to hold THERE.
    """
    monkeypatch.setattr(loop, "_fs_type_for", lambda _p: "btrfs")
    wedged = _WedgedProc()
    spawned = _spawn_returning(monkeypatch, wedged, _WedgedProc(pid=777))
    spy = AsyncMock()
    monkeypatch.setattr(loop.observations, "create", spy)

    await loop._check_db_nodatacow(object())  # hour one: probe wedges
    await loop._check_db_nodatacow(object())  # hour two: must not spawn again

    assert len(spawned) == 1, (
        f"the production call site spawned {len(spawned)} probes across two "
        "ticks while the first child was still alive"
    )
    spy.assert_not_called(), "an undeterminable flag must never raise an alert"


@pytest.mark.asyncio
async def test_a_held_latch_is_announced_at_warning_not_debug(
    tmp_path, monkeypatch, caplog
):
    """A silently disabled health check is the failure mode, not a quiet success.

    While the latch holds, this monitor reports "could not tell" every hour and
    raises nothing. The shipped level is INFO, so a debug line would make that
    invisible for as long as it lasts — which can be until the server restarts.
    """
    import logging
    from pathlib import Path

    wedged = _WedgedProc(pid=31337)
    _spawn_returning(monkeypatch, wedged)

    await loop._nocow_flag(Path(tmp_path / "genesis.db"))
    assert loop._stuck_nocow_probe is wedged

    with caplog.at_level(logging.WARNING, logger=loop.logger.name):
        await loop._nocow_flag(Path(tmp_path / "genesis.db"))

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "declining to probe must be visible at the shipped log level"
    assert "31337" in warnings[0].getMessage(), "name the pid that is wedged"
