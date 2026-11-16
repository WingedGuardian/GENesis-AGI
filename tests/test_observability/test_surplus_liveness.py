"""Tests for the truthful liveness fields on the surplus snapshot.

surplus_status() exposes stalled / last_success_at / stall_reason / liveness_error so
a WEDGED surplus scheduler stops reading green (the idle-proxy `status` hid it). The
verdict is computed robust-by-construction from ONE authoritative source — the
BOOTSTRAPPED runtime's in-memory surplus_dispatch pulse — and EVERY way that read can
be uncertain collapses to liveness_error (dashboard → unknown), never a defaulted-False
that reads green. This enumerates and locks that whole uncertainty class:
  - stale pulse (not paused) -> stalled; recent -> healthy; paused -> not stalled
  - no runtime / unbootstrapped zombie singleton -> unavailable
  - no pulse on record (never started) -> unavailable
  - non-null unparseable pulse (corruption / legacy data) -> unavailable
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.observability.snapshots.surplus import surplus_status
from genesis.runtime._core import GenesisRuntime

_UNSET = object()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def runtime_singleton():
    original = GenesisRuntime._instance
    yield
    GenesisRuntime._instance = original


def _install_runtime(
    *, present=True, bootstrapped=True, paused=False, pulse_min_ago=_UNSET, pulse_raw=_UNSET
):
    """Inject a stub runtime whose in-memory _job_health holds the surplus pulse.

    pulse_min_ago: seed last_success that many minutes before the CALL-TIME clock
        (negative seeds the future). Read `now` HERE, never at import: production
        ages this seed against the live clock, so a module-level capture makes the
        margin the whole suite's runtime instead of this one test's.
    pulse_raw: seed a literal last_success value (e.g. a garbage string).
    neither set: no surplus_dispatch entry at all (never pulsed).
    """
    if not present:
        GenesisRuntime._instance = None
        return
    entry = {}
    if pulse_raw is not _UNSET:
        entry = {"surplus_dispatch": {"last_success": pulse_raw}}
    elif pulse_min_ago is not _UNSET:
        ts = (datetime.now(UTC) - timedelta(minutes=pulse_min_ago)).isoformat()
        entry = {"surplus_dispatch": {"last_success": ts}}
    GenesisRuntime._instance = SimpleNamespace(
        is_bootstrapped=bootstrapped,
        _job_health=entry,
        paused=paused,
        idle_detector=None,
    )


class _FakeQueue:
    async def pending_count(self):
        return 0


def _fake_surplus():
    return SimpleNamespace(
        _dispatch_interval=5,
        _idle_detector=SimpleNamespace(is_idle=lambda: True),
        _queue=_FakeQueue(),
    )


@pytest.mark.asyncio
async def test_stale_pulse_not_paused_is_stalled(db, runtime_singleton):
    """No completed dispatch in 5h, not paused → stalled with a reason."""
    _install_runtime(paused=False, pulse_min_ago=5 * 60)
    r = await surplus_status(db, _fake_surplus())
    assert r["stalled"] is True
    assert r["liveness_error"] is False
    assert r["stall_reason"]
    assert r["last_success_at"] is not None


@pytest.mark.asyncio
async def test_recent_pulse_not_stalled(db, runtime_singleton):
    _install_runtime(paused=False, pulse_min_ago=2)
    r = await surplus_status(db, _fake_surplus())
    assert r["stalled"] is False
    assert r["liveness_error"] is False


@pytest.mark.asyncio
async def test_paused_not_stalled(db, runtime_singleton):
    """A globally-paused surplus legitimately freezes its pulse → never a stall."""
    _install_runtime(paused=True, pulse_min_ago=5 * 60)
    r = await surplus_status(db, _fake_surplus())
    assert r["stalled"] is False
    assert r["liveness_error"] is False


@pytest.mark.asyncio
async def test_no_runtime_is_unavailable(db, runtime_singleton):
    _install_runtime(present=False)
    r = await surplus_status(db, None)
    assert r["liveness_error"] is True
    assert r["stalled"] is False


@pytest.mark.asyncio
async def test_unbootstrapped_zombie_is_unavailable(db, runtime_singleton):
    """A zombie singleton another MCP tool constructed (is_bootstrapped False) must not
    be trusted for pause/pulse — its state would make liveness depend on call order."""
    _install_runtime(bootstrapped=False, paused=False, pulse_min_ago=5 * 60)
    r = await surplus_status(db, _fake_surplus())
    assert r["liveness_error"] is True
    assert r["stalled"] is False


@pytest.mark.asyncio
async def test_never_pulsed_is_unavailable(db, runtime_singleton):
    """No surplus_dispatch entry at all (never started / crashed pre-first-heartbeat) →
    unavailable, not green — the job_never_succeeded alarm doesn't own this."""
    _install_runtime(paused=False)  # no pulse seeded
    r = await surplus_status(db, _fake_surplus())
    assert r["liveness_error"] is True
    assert r["stalled"] is False
    assert r["last_success_at"] is None


@pytest.mark.asyncio
async def test_unparseable_pulse_is_unavailable(db, runtime_singleton):
    """A non-null but malformed pulse (corruption / legacy data) is NOT a valid pulse →
    unavailable, never a silent stalled=False that reads green."""
    _install_runtime(paused=False, pulse_raw="not-a-timestamp")
    r = await surplus_status(db, _fake_surplus())
    assert r["liveness_error"] is True
    assert r["stalled"] is False


@pytest.mark.asyncio
async def test_future_pulse_is_unavailable(db, runtime_singleton):
    """A pulse materially in the future (clock stepped backward / corruption) is not a
    confirmable recent success → unavailable, never green from the negative age."""
    _install_runtime(paused=False, pulse_min_ago=-30)  # 30 min in the future
    r = await surplus_status(db, _fake_surplus())
    assert r["liveness_error"] is True
    assert r["stalled"] is False


# ---------------------------------------------------------------------------
# Status-file path (#4072): the standalone health MCP has no runtime, so the
# server carries the same in-memory pulse through status.json. The file's own
# age gates it: a frozen file (server dead, writer failing) must read
# UNAVAILABLE, never green, including when it froze while paused.
# ---------------------------------------------------------------------------


def _iso(minutes_ago: float | None) -> str | None:
    if minutes_ago is None:
        return None
    return (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat()


def _file_pulse(*, written_min_ago=0.1, pulse_min_ago=1.0, paused=False):
    from genesis.observability.snapshots.surplus import StatusFilePulse

    return StatusFilePulse(
        last_success=_iso(pulse_min_ago) if isinstance(pulse_min_ago, (int, float)) else pulse_min_ago,
        paused=paused,
        written_at=_iso(written_min_ago) if isinstance(written_min_ago, (int, float)) else written_min_ago,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kwargs", "liveness_error", "stalled"),
    [
        ("fresh file, recent pulse", {}, False, False),
        ("fresh file, pulse past the stall threshold", {"pulse_min_ago": 600}, False, True),
        ("fresh file, paused with an old pulse", {"pulse_min_ago": 600, "paused": True}, False, False),
        ("stale file, recent pulse", {"written_min_ago": 10}, True, False),
        ("stale file frozen while paused", {"written_min_ago": 10, "paused": True}, True, False),
        ("future-dated file", {"written_min_ago": -30}, True, False),
        ("file without a timestamp", {"written_min_ago": None}, True, False),
        ("unparseable file timestamp", {"written_min_ago": "yesterday"}, True, False),
        ("no pulse (older server)", {"pulse_min_ago": None}, True, False),
        ("non-string pulse", {"pulse_min_ago": 12345}, True, False),
        ("string paused flag", {"paused": "false"}, True, False),
        ("missing paused flag", {"paused": None}, True, False),
    ],
)
async def test_status_file_pulse_cells(db, runtime_singleton, case, kwargs, liveness_error, stalled):
    _install_runtime(present=False)  # the standalone MCP process has no runtime
    pulse = _file_pulse(**kwargs)
    if case == "non-string pulse":
        from dataclasses import replace

        pulse = replace(pulse, last_success=12345)
    r = await surplus_status(db, None, status_file_pulse=pulse)
    assert r["liveness_error"] is liveness_error, case
    assert r["stalled"] is stalled, case
    assert r["liveness_source"] == "status_file", case


@pytest.mark.asyncio
async def test_status_file_path_reports_last_success(db, runtime_singleton):
    _install_runtime(present=False)
    pulse = _file_pulse(pulse_min_ago=2)
    r = await surplus_status(db, None, status_file_pulse=pulse)
    assert r["last_success_at"] == pulse.last_success


@pytest.mark.asyncio
async def test_runtime_path_labels_its_source(db, runtime_singleton):
    _install_runtime(paused=False, pulse_min_ago=1)
    r = await surplus_status(db, _fake_surplus())
    assert r["liveness_error"] is False
    assert r["liveness_source"] == "runtime"
