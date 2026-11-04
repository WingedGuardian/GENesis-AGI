"""Review regressions using the real maintenance/manager/relief code and fake Incus."""
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from genesis.guardian.check import _maintain_snapshots
from genesis.guardian.deletion_window import resolve_window
from genesis.guardian.pool_relief import record_action, settled
from genesis.guardian.snapshots import SnapshotManager

from .test_deletion_window import _cfg, _listing, _win
from .test_pool_relief_early import AGED, LIFELINE, PRE, YOUNG, _lvm, _pass, _Snaps
from .test_snapshots import _pool_status, _take_env_mock

T0 = datetime(2026, 6, 2, 6, 59, tzinfo=UTC)
HEALTHY = [f"guardian-202605{d}-000000-healthy" for d in (29, 30, 31)]
PREVIOUS = [f"guardian-2026050{d}-000000-pre-recovery" for d in (1, 2, 3)]


@pytest.fixture(autouse=True)
def window_config(monkeypatch):
    monkeypatch.delenv("GUARDIAN_DELETION_WINDOW", raising=False)
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.setattr("genesis.env._local_config", lambda: {})


class Host:
    """Only Incus/measurement are fake; every deletion must cross the real manager."""
    def __init__(self, names=()):
        self.now = T0
        self.names = list(names)
        self.deletes = []
        self.creates = []
        self.timeout = False
        self.close_on = None
        self.bad_list = False
        self.list_calls = 0
        self.create_refused = False

    async def run(self, *args, **kwargs):
        if args[:3] == ("incus", "snapshot", "list"):
            self.list_calls += 1
            if self.close_on == "list":
                self.now = T0 + timedelta(minutes=2)
            if self.bad_list:
                return 1, "", "listing failed"
            return 0, _listing(*[(n, "2026-05-01T00:00:00Z") for n in self.names]), ""
        if args[:3] == ("incus", "snapshot", "delete"):
            self.deletes.append((args[4], self.now))
            if self.close_on == "delete":
                self.now = T0 + timedelta(minutes=2)
            if self.timeout:
                return -1, "", "timeout"
            self.names.remove(args[4])
            return 0, "", ""
        if args[:3] == ("incus", "snapshot", "create"):
            if self.create_refused:
                return 1, "", "free space in thin pool reached threshold"
            if self.close_on == "create":
                self.now = T0 + timedelta(minutes=2)
            self.creates.append(args[4])
            self.names.append(args[4])
            return 0, "", ""
        return await _take_env_mock("[]", [], [])(*args, **kwargs)

    def clock_type(self):
        host = self
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return host.now.astimezone(tz) if tz else host.now.replace(tzinfo=None)
        return Clock

    async def maintain(self, cfg, *, healthy=False, manager=None):
        with (
            patch("genesis.guardian.check.datetime", self.clock_type()),
            patch("genesis.guardian.snapshots.datetime", self.clock_type()),
            patch("genesis.guardian.snapshots._run_subprocess", self.run),
            patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_pool_status(data=50, meta=20)),
        ):
            mgr = manager or SnapshotManager(cfg)
            await _maintain_snapshots(cfg, mgr, is_healthy=healthy, now=self.now)
            return mgr


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_list", [False, True])
async def test_read_only_maintenance_does_not_inhibit_reserve(tmp_path, failed_list):
    cfg = _cfg(tmp_path)
    host = Host()
    host.bad_list = failed_list
    await host.maintain(cfg)
    assert host.deletes == []
    assert settled(cfg, T0 + timedelta(seconds=30))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(98), now=T0 + timedelta(seconds=30))
    assert out == f"deleted:{PRE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prune", "rotate_superseded", "take", "mark_healthy"])
async def test_timeout_stops_each_deletion_batch(tmp_path, method):
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    host = Host(PREVIOUS if method in ("prune", "take") else HEALTHY)
    host.timeout = True
    mgr = SnapshotManager(cfg)
    with (
        patch("genesis.guardian.snapshots._run_subprocess", host.run),
        patch("genesis.guardian.snapshots.measure_storage_pool", return_value=_pool_status(data=50, meta=20)),
    ):
        await getattr(mgr, method)()
    assert len(host.deletes) == 1
    assert mgr.last_delete_error == "timeout"
    if method == "prune":
        assert not mgr.last_prune_complete


@pytest.mark.asyncio
@pytest.mark.parametrize("during", ["list", "delete", "create"])
async def test_every_routine_delete_uses_the_current_clock(tmp_path, during):
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    host = Host(HEALTHY if during == "create" else PREVIOUS + HEALTHY)
    host.close_on = during
    # Make the initial window pass already complete for the create boundary:
    # this reaches the separate healthy-refresh delete path.
    if during == "create":
        cfg.state_path.mkdir(parents=True, exist_ok=True)
        (cfg.state_path / ".last_window_deletes").write_text(T0.isoformat())
    await host.maintain(cfg, healthy=True)
    assert all(at.hour < 7 for _, at in host.deletes), host.deletes
    assert host.creates  # stopping deletes must not stop the healthy create


@pytest.mark.asyncio
async def test_timeout_in_window_cannot_resume_deleting_in_healthy_refresh(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    host = Host(PREVIOUS + HEALTHY)
    host.timeout = True
    await host.maintain(cfg, healthy=True)
    assert len(host.deletes) == 1
    assert not (cfg.state_path / ".last_window_deletes").exists()
    assert not settled(cfg, T0 + timedelta(seconds=30))


@pytest.mark.parametrize(("zone", "spec", "now", "expected"), [
    ("Europe/Berlin", "02:00-03:00", "2026-03-28T01:30:00+00:00", "2026-03-30T00:00:00+00:00"),
    ("Europe/Berlin", "02:10-03:30", "2026-03-29T00:30:00+00:00", "2026-03-29T01:00:00+00:00"),
    ("Europe/Berlin", "02:10-02:20", "2026-10-25T00:30:00+00:00", "2026-10-25T01:10:00+00:00"),
    ("Europe/Berlin", "02:10-02:20", "2026-10-25T00:15:00+00:00", "2026-10-26T01:10:00+00:00"),
    ("Australia/Lord_Howe", "02:00-02:20", "2026-10-03T15:00:00+00:00", "2026-10-04T15:00:00+00:00"),
    ("Pacific/Apia", "05:00-07:00", "2011-12-29T15:30:00+00:00", "2011-12-30T15:00:00+00:00"),
    ("Europe/Berlin", "23:00-03:00", "2026-03-29T00:30:00+00:00", "2026-03-29T21:00:00+00:00"),
])
def test_next_open_is_a_real_in_window_instant(zone, spec, now, expected):
    w = _win(spec, ZoneInfo(zone), zone)
    at, expected = datetime.fromisoformat(now), datetime.fromisoformat(expected)
    for supplied in (at, at.astimezone(w.tz)):
        result = w.next_open(supplied).astimezone(UTC)
        assert result == expected
        assert w.contains(result)
        assert w.hours_until_open(supplied) == (0 if w.contains(at) else (expected-at).total_seconds()/3600)


def test_failed_timezone_lookup_is_fallback_even_with_valid_user_config(tmp_path):
    with (
        patch("genesis.env._local_config", return_value={"timezone": "Europe/Berlin"}),
        patch("genesis.env.user_timezone", side_effect=RuntimeError("lookup failed")),
    ):
        w = resolve_window(_cfg(tmp_path, tz=""))
    assert (w.tz_name, w.tz_source) == ("UTC", "fallback")


@pytest.mark.asyncio
async def test_cooldown_stamp_io_cannot_extend_window_authority(tmp_path):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS + HEALTHY)
    def slow_stamp(config, now):
        result = record_action(config, now)
        host.now += timedelta(minutes=2)
        return result
    with patch("genesis.guardian.pool_relief.record_action", slow_stamp):
        await host.maintain(cfg, healthy=True)
    assert host.deletes == []
    assert host.creates
    assert not settled(cfg, T0 + timedelta(seconds=30))  # conservative reservation
    assert not (cfg.state_path / ".last_window_deletes").exists()


@pytest.mark.asyncio
async def test_failed_action_stamp_blocks_deletion_but_not_healthy_create(tmp_path):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS + HEALTHY)
    with patch("genesis.guardian.pool_relief.record_action", return_value=False):
        await host.maintain(cfg, healthy=True)
    assert host.deletes == [] and host.creates
    assert settled(cfg, T0 + timedelta(seconds=30))
    assert not (cfg.state_path / ".last_window_deletes").exists()


@pytest.mark.asyncio
async def test_healthy_retention_checks_time_after_listing(tmp_path):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS + HEALTHY)
    cfg.snapshots.retention = 1
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    (cfg.state_path / ".last_window_deletes").write_text(T0.isoformat())
    host.close_on = "list"
    await host.maintain(cfg, healthy=True)
    assert host.deletes == [] and host.creates


@pytest.mark.asyncio
async def test_read_only_failure_is_paced_without_shared_cooldown(tmp_path):
    cfg, host = _cfg(tmp_path), Host()
    host.now = T0 - timedelta(hours=1)
    host.bad_list = True
    await host.maintain(cfg)
    lists = host.list_calls
    host.now += timedelta(minutes=2)
    await host.maintain(cfg)
    assert host.list_calls == lists
    assert settled(cfg, host.now)
    host.now += timedelta(minutes=4)
    host.bad_list = False
    await host.maintain(cfg)
    assert host.list_calls > lists
    assert (cfg.state_path / ".last_window_deletes").exists()


@pytest.mark.asyncio
async def test_timeout_retries_with_fresh_listing_after_settle(tmp_path):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS)
    host.now = T0 - timedelta(hours=1)
    host.timeout = True
    manager = await host.maintain(cfg)
    assert len(host.deletes) == 1
    host.now += timedelta(minutes=2)
    lists = host.list_calls
    await host.maintain(cfg, manager=manager)
    assert len(host.deletes) == 1 and host.list_calls == lists
    host.now += timedelta(minutes=4)
    host.timeout = False
    await host.maintain(cfg, manager=manager)
    assert host.list_calls > lists and len(host.deletes) == 4
    assert (cfg.state_path / ".last_window_deletes").exists()


@pytest.mark.asyncio
async def test_prior_relief_timeout_blocks_pressure_retry_in_same_tick(tmp_path):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS + HEALTHY)
    cfg.snapshots.retention = 1
    host.create_refused = True
    manager = SnapshotManager(cfg)
    manager.last_delete_error = "timeout"  # earlier pool-relief action this tick
    assert record_action(cfg, host.now)
    await host.maintain(cfg, healthy=True, manager=manager)
    assert host.deletes == []
    assert manager.last_delete_error == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("inside", [False, True])
async def test_pressure_retry_respects_prior_tick_settle_with_fresh_manager(tmp_path, inside):
    cfg, host = _cfg(tmp_path), Host(PREVIOUS + HEALTHY)
    if not inside:
        host.now = T0 + timedelta(hours=2)
    cfg.snapshots.retention = 1
    host.create_refused = True
    assert record_action(cfg, host.now - timedelta(seconds=30))
    await host.maintain(cfg, healthy=True)  # new manager: only durable stamp remembers
    assert host.deletes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("candidates", [[], PREVIOUS])
@pytest.mark.parametrize("stamp_ok", [False, True])
async def test_pressure_retry_outside_window_stamps_only_actual_delete(tmp_path, candidates, stamp_ok):
    cfg, host = _cfg(tmp_path), Host(candidates + HEALTHY)
    cfg.snapshots.retention = 1
    host.now = T0 + timedelta(hours=2)
    host.create_refused = True
    with patch("genesis.guardian.pool_relief.record_action", wraps=record_action) as stamp:
        if not stamp_ok:
            stamp.return_value = False
        await host.maintain(cfg, healthy=True)
    deleted = bool(candidates) and stamp_ok
    assert len(host.deletes) == deleted  # one pressure action before settling
    assert settled(cfg, host.now) is (not deleted)
    assert all(name not in HEALTHY for name, _ in host.deletes)


@pytest.mark.asyncio
async def test_pressure_recheck_evicts_new_expendable_before_lifeline(tmp_path):
    from unittest.mock import AsyncMock

    cfg, host = _cfg(tmp_path), Host(HEALTHY[:1])
    cfg.snapshots.retention = 1
    host.now = T0 + timedelta(hours=2)  # pressure exception, outside window
    host.create_refused = True
    original = host.run
    refused = 0
    async def appearing_candidate(*args, **kwargs):
        nonlocal refused
        result = await original(*args, **kwargs)
        if args[:3] == ("incus", "snapshot", "create"):
            refused += 1
            if refused == 2:
                host.names.append(PREVIOUS[0])
        return result
    host.run = appearing_candidate
    manager = SnapshotManager(cfg)
    manager._stale_lifeline = AsyncMock(return_value=HEALTHY[0])
    manager._lifeline_holds_space = AsyncMock(return_value=2 * 1024**3)
    with patch("genesis.guardian.pool_relief.delete_first_allowed", return_value=True):
        await host.maintain(cfg, healthy=True, manager=manager)
    assert [n for n, _ in host.deletes] == [PREVIOUS[0]]
    assert HEALTHY[0] in host.names


@pytest.mark.asyncio
async def test_post_lifeline_create_cannot_evict_during_its_own_settle(tmp_path):
    from unittest.mock import AsyncMock

    cfg, host = _cfg(tmp_path), Host(PREVIOUS[:1] + HEALTHY[:1])
    cfg.snapshots.retention = 1
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    (cfg.state_path / ".last_window_deletes").write_text(T0.isoformat())
    manager = SnapshotManager(cfg)
    manager.last_gate_measured = True
    manager.safe_to_snapshot = AsyncMock(side_effect=[False, False, True])
    manager._stale_lifeline = AsyncMock(return_value=HEALTHY[0])
    manager._lifeline_holds_space = AsyncMock(return_value=2 * 1024**3)
    with patch("genesis.guardian.pool_relief.delete_first_allowed", return_value=True):
        await host.maintain(cfg, healthy=True, manager=manager)
    assert [n for n, _ in host.deletes] == [HEALTHY[0]]
    assert host.creates and PREVIOUS[0] in host.names
