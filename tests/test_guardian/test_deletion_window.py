"""The routine-deletion maintenance window (deletion_window.py) and where it
binds: the healthy rotation's superseded snapshot, prune, take()'s eviction
before a healthy snapshot, and early pool relief. Pressure never waits."""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from genesis.guardian.check import _maintain_snapshots
from genesis.guardian.config import GuardianConfig
from genesis.guardian.deletion_window import (
    DEFAULT_WINDOW,
    DeletionWindow,
    parse_window,
    resolve_window,
)
from genesis.guardian.snapshots import SnapshotManager

from .test_pool_relief_early import (
    AGED,
    LIFELINE,
    PRE,
    YOUNG,
    _lvm,
    _pass,
    _ramp_to,
    _seed,
    _Snaps,
)
from .test_pool_relief_early import T0 as RELIEF_T0
from .test_snapshots import _pool_status, _take_env_mock

BERLIN = ZoneInfo("Europe/Berlin")


@pytest.fixture(autouse=True)
def _window_from_config(monkeypatch):
    # The package conftest pins legacy suites to "any"; these set it explicitly.
    monkeypatch.delenv("GUARDIAN_DELETION_WINDOW", raising=False)


def _cfg(tmp_path, window: str = "05:00-07:00", tz: str = "UTC") -> GuardianConfig:
    c = GuardianConfig()
    c.state_dir = str(tmp_path / "state")
    c.snapshots.deletion_window = window
    c.snapshots.deletion_window_timezone = tz
    return c


def _win(spec: str, tz=UTC, name: str = "UTC") -> DeletionWindow:
    bounds = parse_window(spec)
    return DeletionWindow(bounds[0], bounds[1], tz, name)


# --- window maths and config -------------------------------------------------------


def test_default_window_is_five_to_seven() -> None:
    assert GuardianConfig().snapshots.deletion_window == DEFAULT_WINDOW == "05:00-07:00"


@pytest.mark.parametrize(
    ("at", "inside"),
    [(time(4, 59), False), (time(5, 0), True), (time(6, 59), True), (time(7, 0), False)],
)
def test_boundaries_start_inclusive_end_exclusive(at, inside) -> None:
    now = datetime(2026, 6, 1, at.hour, at.minute, tzinfo=UTC)
    assert _win("05:00-07:00").contains(now) is inside


@pytest.mark.parametrize(
    ("hour", "inside"),
    [(22, False), (23, True), (0, True), (1, True), (2, False)],
)
def test_window_crossing_midnight(hour, inside) -> None:
    w = _win("23:00-02:00")
    now = datetime(2026, 6, 1, hour, 30, tzinfo=UTC)
    assert w.contains(now) is inside
    if inside:
        # The occurrence began at 23:00 — yesterday for the after-midnight part.
        start = w.occurrence_start(now)
        assert start.hour == 23 and (start.day == 1 if hour == 23 else start.day == 31)


def test_window_reads_the_local_wall_clock_across_dst() -> None:
    w = _win("05:00-07:00", BERLIN, "Europe/Berlin")
    # 05:30 local is 03:30 UTC in summer (CEST) and 04:30 UTC in winter (CET).
    assert w.contains(datetime(2026, 7, 1, 3, 30, tzinfo=UTC))
    assert w.contains(datetime(2026, 12, 1, 4, 30, tzinfo=UTC))
    assert not w.contains(datetime(2026, 12, 1, 3, 30, tzinfo=UTC))


def test_a_window_in_the_skipped_dst_hour_does_not_occur_that_day() -> None:
    w = _win("02:00-03:00", BERLIN, "Europe/Berlin")
    day = datetime(2026, 3, 28, 23, 0, tzinfo=UTC)  # 2026-03-29 is spring-forward
    assert not any(w.contains(day + timedelta(minutes=5 * i)) for i in range(12 * 24))
    # The next day it occurs again (catch-up).
    assert w.contains(datetime(2026, 3, 30, 0, 30, tzinfo=UTC))  # 02:30 CEST


def test_the_repeated_dst_hour_is_one_occurrence() -> None:
    w = _win("02:00-03:00", BERLIN, "Europe/Berlin")
    first = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    second = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert w.contains(first) and w.contains(second)
    assert w.occurrence_start(first) == w.occurrence_start(second)


def test_hours_until_open_and_next_open() -> None:
    w = _win("05:00-07:00")
    assert w.hours_until_open(datetime(2026, 6, 1, 12, 0, tzinfo=UTC)) == 17.0
    assert w.hours_until_open(datetime(2026, 6, 1, 5, 30, tzinfo=UTC)) == 0.0
    inside = datetime(2026, 6, 1, 5, 30, tzinfo=UTC)
    assert w.next_open(inside) == datetime(2026, 6, 2, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize("spec", ["05:00-05:00", "25:00-07:00", "5-7", "", None, False, 5])
def test_invalid_window_falls_back_to_the_default(tmp_path, spec) -> None:
    w = resolve_window(_cfg(tmp_path, window=spec))
    assert (w.start, w.end) == (time(5), time(7))


def test_any_disables_the_window(tmp_path) -> None:
    w = resolve_window(_cfg(tmp_path, window="Any"))
    assert w.any_time and w.contains(datetime(2026, 6, 1, 15, tzinfo=UTC))


def test_env_override_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GUARDIAN_DELETION_WINDOW", "any")
    assert resolve_window(_cfg(tmp_path)).any_time


def test_timezone_resolution(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    assert resolve_window(_cfg(tmp_path, tz="Asia/Tokyo")).tz_name == "Asia/Tokyo"
    with patch("genesis.env.user_timezone", return_value="Europe/Berlin"):
        assert resolve_window(_cfg(tmp_path, tz="")).tz_name == "Europe/Berlin"
        # An invalid configured zone falls through to the user timezone.
        assert resolve_window(_cfg(tmp_path, tz="Mars/Base")).tz_name == "Europe/Berlin"
    with patch("genesis.env.user_timezone", side_effect=RuntimeError("no config")):
        w = resolve_window(_cfg(tmp_path, tz=""))
        assert (w.tz_name, w.tz_source) == ("UTC", "fallback")
    # No genesis.yaml timezone and no USER_TIMEZONE: user_timezone()'s UTC is a
    # fallback, and the window says so (disk-status shows it).
    with (
        patch("genesis.env.user_timezone", return_value="UTC"),
        patch("genesis.env._local_config", return_value={}),
    ):
        assert resolve_window(_cfg(tmp_path, tz="")).tz_source == "fallback"
    assert resolve_window(_cfg(tmp_path, tz="UTC")).tz_source == "config"
    # An INVALID genesis.yaml zone is not a source either: user_timezone()
    # falls back to UTC for it.
    with (
        patch("genesis.env.user_timezone", return_value="UTC"),
        patch("genesis.env._local_config", return_value={"timezone": "Amrica/Chicago"}),
    ):
        assert resolve_window(_cfg(tmp_path, tz="")).tz_source == "fallback"


# --- SnapshotManager ----------------------------------------------------------------

_NEW = "guardian-20260703-000000-healthy"
_MID = "guardian-20260702-000000-healthy"
_OLD = "guardian-20260701-000000-healthy"


def _listing(*rows: tuple[str, str], expires: dict[str, str] | None = None) -> str:
    return json.dumps(
        [
            {
                "name": n,
                "created_at": c,
                "expires_at": (expires or {}).get(n, "0001-01-01T00:00:00Z"),
            }
            for n, c in rows
        ]
    )


async def _run_mark(config, existing: str, **kw):
    deletes: list = []
    creates: list = []
    with (
        patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_pool_status(data=50.0, meta=20.0),
        ),
        patch(
            "genesis.guardian.snapshots._run_subprocess", _take_env_mock(existing, deletes, creates)
        ),
    ):
        name = await SnapshotManager(config).mark_healthy(healthy_confirmed=True, **kw)
    return name, deletes


@pytest.mark.asyncio
async def test_deferred_rotation_creates_and_deletes_nothing(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    existing = _listing(
        ("guardian-20260702-120000-pre-recovery", "2026-07-02T12:00:00Z"),
        (_OLD, "2026-07-01T00:00:00Z"),
    )
    name, deletes = await _run_mark(cfg, existing, defer_deletes=True)
    assert name is not None and name.endswith("-healthy")
    # Neither the superseded healthy snapshot nor take()'s retention eviction.
    assert deletes == []


@pytest.mark.asyncio
async def test_a_pool_refusal_while_deferring_still_evicts_first(tmp_path) -> None:
    # Pressure never waits: LVM refusing the create (its thin-pool threshold)
    # retries with take()'s retention eviction, so an expendable snapshot goes
    # before delete-first is weighed — as it did before the window.
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    pre = "guardian-20260702-120000-pre-recovery"
    existing = _listing((pre, "2026-07-02T12:00:00Z"), (_OLD, "2026-07-01T00:00:00Z"))
    deletes: list = []
    inner = _take_env_mock(existing, deletes, [])

    async def lvm_refuses(*args, **kwargs):
        if args[:3] == ("incus", "snapshot", "create"):
            return (1, "", "Cannot create new thin volume, free space in thin pool reached")
        return await inner(*args, **kwargs)

    with (
        patch(
            "genesis.guardian.snapshots.measure_storage_pool",
            return_value=_pool_status(data=50.0, meta=20.0),
        ),
        patch("genesis.guardian.snapshots._run_subprocess", lvm_refuses),
    ):
        name = await SnapshotManager(cfg).mark_healthy(
            healthy_confirmed=True,
            delete_first_allowed=True,
            defer_deletes=True,
        )
    assert name is None
    assert pre in deletes and _OLD not in deletes


@pytest.mark.asyncio
async def test_rotate_superseded_keeps_the_newest(tmp_path) -> None:
    existing = _listing(
        (_MID, "2026-07-02T00:00:00Z"),
        (_NEW, "2026-07-03T00:00:00Z"),
        (_OLD, "2026-07-01T00:00:00Z"),
    )
    deletes: list = []
    mgr = SnapshotManager(_cfg(tmp_path))
    with patch("genesis.guardian.snapshots._run_subprocess", _take_env_mock(existing, deletes, [])):
        assert await mgr.superseded_healthy() == [_MID, _OLD]
        assert await mgr.rotate_superseded() == 2
    assert sorted(deletes) == sorted([_MID, _OLD]) and _NEW not in deletes


@pytest.mark.asyncio
async def test_rotate_superseded_with_one_healthy_deletes_nothing(tmp_path) -> None:
    deletes: list = []
    with patch(
        "genesis.guardian.snapshots._run_subprocess",
        _take_env_mock(_listing((_NEW, "2026-07-03T00:00:00Z")), deletes, []),
    ):
        assert await SnapshotManager(_cfg(tmp_path)).rotate_superseded() == 0
    assert deletes == []


@pytest.mark.asyncio
async def test_rotate_superseded_on_a_failed_list_deletes_nothing(tmp_path) -> None:
    deletes: list = []
    with patch(
        "genesis.guardian.snapshots._run_subprocess", _take_env_mock("not json", deletes, [])
    ):
        assert await SnapshotManager(_cfg(tmp_path)).rotate_superseded() == 0
    assert deletes == []


@pytest.mark.asyncio
async def test_prune_takes_what_incus_would_expire_before_the_next_window(tmp_path) -> None:
    now = datetime.now(UTC)
    soon = "guardian-20260701-000000-pre-recovery"
    later = "guardian-20260702-000000-pre-recovery"
    existing = _listing(
        (later, (now - timedelta(days=1)).isoformat()),
        (soon, (now - timedelta(days=2)).isoformat()),
        expires={
            soon: (now + timedelta(hours=10)).isoformat(),
            later: (now + timedelta(days=12)).isoformat(),
        },
    )
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 5
    deletes: list = []
    with patch("genesis.guardian.snapshots._run_subprocess", _take_env_mock(existing, deletes, [])):
        assert await SnapshotManager(cfg).prune(expires_before=now + timedelta(hours=24)) == 1
    assert deletes == [soon]


@pytest.mark.asyncio
async def test_prune_is_incomplete_when_the_expiry_read_fails(tmp_path) -> None:
    existing = _listing(("guardian-20260702-000000-pre-recovery", "2026-07-02T00:00:00Z"))
    mgr = SnapshotManager(_cfg(tmp_path))
    mgr._expires_at = AsyncMock(return_value=None)
    with patch("genesis.guardian.snapshots._run_subprocess", _take_env_mock(existing, [], [])):
        await mgr.prune(expires_before=datetime.now(UTC))
    assert mgr.last_prune_complete is False


# --- _maintain_snapshots --------------------------------------------------------------


def _mock_snaps(superseded: list[str] | None = None) -> MagicMock:
    s = MagicMock()
    s.enforce_expiry_policy = AsyncMock(return_value=True)
    s.prune = AsyncMock(return_value=0)
    s.rotate_superseded = AsyncMock(return_value=1)
    s.superseded_healthy = AsyncMock(return_value=superseded or [])
    s.mark_healthy = AsyncMock(return_value="guardian-20260601-120000-healthy")
    s.get_latest_healthy = AsyncMock(return_value=None)
    s.last_rotation_note = None
    s.last_rotation_deleted = False
    s.last_prune_complete = True
    return s


OUTSIDE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
INSIDE = datetime(2026, 6, 2, 5, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_outside_the_window_rotation_defers_and_nothing_is_pruned(tmp_path) -> None:
    cfg, s = _cfg(tmp_path), _mock_snaps()
    await _maintain_snapshots(cfg, s, is_healthy=True, now=OUTSIDE)
    s.enforce_expiry_policy.assert_awaited_once()  # not a deletion: stays on its cadence
    s.prune.assert_not_called()
    s.rotate_superseded.assert_not_called()
    assert s.mark_healthy.await_args.kwargs["defer_deletes"] is True


@pytest.mark.asyncio
async def test_inside_the_window_prune_and_rotation_run_once_per_occurrence(tmp_path) -> None:
    cfg, s = _cfg(tmp_path), _mock_snaps()
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE)
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(minutes=30))
    assert s.prune.await_count == 1 and s.rotate_superseded.await_count == 1
    assert s.prune.await_args.kwargs["expires_before"] == datetime(2026, 6, 3, 5, tzinfo=UTC)
    # A missed day is caught up by the next occurrence.
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(days=2))
    assert s.prune.await_count == 2 and s.rotate_superseded.await_count == 2


@pytest.mark.asyncio
async def test_an_incomplete_window_pass_retries_after_the_settle(tmp_path) -> None:
    # A failed listing or delete must not read as "nothing to delete" for the
    # day — but its retry waits pool relief's settle interval (5 min), so
    # retries cannot stack filesystem stalls or hold every tick on timeouts.
    cfg, s = _cfg(tmp_path), _mock_snaps()
    s.last_prune_complete = False
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE)
    s.rotate_superseded.assert_not_called()  # stops at the failed prune
    s.last_prune_complete = True
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(minutes=2))
    assert s.prune.await_count == 1  # still settling
    s.superseded_healthy = AsyncMock(return_value=None)  # the check listing failed
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(minutes=6))
    s.superseded_healthy = AsyncMock(return_value=[])
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(minutes=12))
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE + timedelta(minutes=18))
    assert s.prune.await_count == 3


@pytest.mark.asyncio
async def test_superseded_rotation_waits_for_a_healthy_tick(tmp_path) -> None:
    cfg, s = _cfg(tmp_path), _mock_snaps(superseded=[_OLD])
    await _maintain_snapshots(cfg, s, is_healthy=False, now=INSIDE)
    s.prune.assert_awaited_once()
    s.rotate_superseded.assert_not_called()


@pytest.mark.asyncio
async def test_a_rotation_inside_the_window_deletes_at_once(tmp_path) -> None:
    cfg, s = _cfg(tmp_path), _mock_snaps()
    await _maintain_snapshots(cfg, s, is_healthy=True, now=INSIDE)
    assert s.mark_healthy.await_args.kwargs["defer_deletes"] is False


@pytest.mark.asyncio
async def test_superseded_snapshots_piling_up_alert(tmp_path) -> None:
    cfg, s = _cfg(tmp_path), _mock_snaps(superseded=[_MID, _OLD])
    d = MagicMock()
    d.send = AsyncMock(return_value=True)
    await _maintain_snapshots(cfg, s, is_healthy=True, dispatcher=d, now=OUTSIDE)
    titles = [c.args[0].title for c in d.send.await_args_list]
    assert titles == ["Superseded healthy snapshots are piling up"]


@pytest.mark.asyncio
async def test_any_time_keeps_the_old_calls_exactly(tmp_path) -> None:
    """Characterization: "any" makes the calls main made before the window."""
    cfg, s = _cfg(tmp_path, window="any"), _mock_snaps()
    await _maintain_snapshots(cfg, s, is_healthy=True, now=OUTSIDE)
    s.prune.assert_awaited_once_with()
    s.rotate_superseded.assert_not_called()
    s.superseded_healthy.assert_not_called()
    assert "defer_deletes" not in s.mark_healthy.await_args.kwargs


# --- pool relief ----------------------------------------------------------------------
# RELIEF_T0 is 12:00 UTC: the 05:00-07:00 UTC window opens in 17h.


@pytest.mark.asyncio
async def test_early_relief_waits_when_the_pool_outlasts_the_wait(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    # 0.5%/h into 20% free: full in ~40h, the 3% reserve in ~34h > 17h.
    _seed(cfg, data_at=_ramp_to(0.80, per_h=0.005))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, d = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "early_deferred_to_window" and snaps.deleted == []
    assert [c.args[0].title for c in d.send.await_args_list] == [
        "Pool filling — early relief waits for the deletion window"
    ]


@pytest.mark.asyncio
async def test_early_relief_judges_the_reserve_not_full(tmp_path) -> None:
    # 1%/h into 19% free: full in ~19h (beyond the 17h wait) but the 3% reserve
    # in ~16h — waiting would hand the delete to reserve relief, out of window.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.81))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(81.0))
    assert out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_early_relief_acts_when_the_pool_fills_before_the_window(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80, per_h=0.02))  # ~10h runway < 17h
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_early_relief_inside_the_window_acts(tmp_path) -> None:
    cfg = _cfg(tmp_path, tz="Etc/GMT+7")  # 12:00 UTC is 05:00 there
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_reserve_relief_never_waits(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    # Data at its reserve with no measured growth, metadata growing slowly (its
    # reserve ~hundreds of hours away): RESERVE relief must not wait on that
    # long axis. Only the early level ever waits.
    _seed(cfg, data_at=lambda h: 0.98, meta_at=_ramp_to(0.40, per_h=0.001))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(98.0), now=RELIEF_T0)
    assert out == f"deleted:{PRE}"
