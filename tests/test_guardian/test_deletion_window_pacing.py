"""Deletion-window pacing and the stuck-snapshot path, through the real
maintenance/manager code with only Incus faked.

* A snapshot whose delete DEFINITELY fails (the client did not time out and a
  re-list still shows it) is skipped, so it cannot stop every routine delete
  behind it; an uncertain failure (timeout, failed re-list) still stops.
* Every routine delete waits the settle interval, so a window pass deletes one
  snapshot and continues on a later tick instead of stacking deletions.
"""

from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.check import _maintain_snapshots
from genesis.guardian.pool_relief import settled
from genesis.guardian.snapshots import SnapshotManager

from .test_deletion_window import _cfg
from .test_deletion_window_regressions import HEALTHY, PREVIOUS, T0, Host
from .test_snapshots import _pool_status

SETTLE = timedelta(minutes=5)
OPEN = T0 - timedelta(hours=1, minutes=59)  # 05:00, the window's first minute


@pytest.fixture(autouse=True)
def window_config(monkeypatch):
    monkeypatch.delenv("GUARDIAN_DELETION_WINDOW", raising=False)
    monkeypatch.delenv("USER_TIMEZONE", raising=False)
    monkeypatch.setattr("genesis.env._local_config", lambda: {})


class StuckHost(Host):
    """A Host whose ``stuck`` snapshots refuse deletion with a definite error,
    and whose ``vanish`` snapshots go even though the client reports failure."""

    def __init__(self, names=()):
        super().__init__(names)
        self.stuck: set[str] = set()
        self.vanish: set[str] = set()
        self.relist_fails = False

    async def run(self, *args, **kwargs):
        if args[:3] == ("incus", "snapshot", "list") and self.relist_fails and self.deletes:
            self.list_calls += 1
            return 1, "", "listing failed"
        if args[:3] == ("incus", "snapshot", "delete"):
            if args[4] in self.stuck:
                self.deletes.append((args[4], self.now))
                return 1, "", "Error: snapshot is busy"
            if args[4] in self.vanish:
                self.deletes.append((args[4], self.now))
                self.names.remove(args[4])
                return 1, "", "Error: transient"
        return await super().run(*args, **kwargs)

    async def tick(self, cfg, *, healthy=False, dispatcher=None):
        with (
            patch("genesis.guardian.check.datetime", self.clock_type()),
            patch("genesis.guardian.snapshots.datetime", self.clock_type()),
            patch("genesis.guardian.snapshots._run_subprocess", self.run),
            patch(
                "genesis.guardian.snapshots.measure_storage_pool",
                return_value=_pool_status(data=50, meta=20),
            ),
        ):
            mgr = SnapshotManager(cfg)
            await _maintain_snapshots(
                cfg,
                mgr,
                is_healthy=healthy,
                dispatcher=dispatcher,
                now=self.now,
            )
            return mgr


async def _direct(host, cfg, method):
    mgr = SnapshotManager(cfg)
    with patch("genesis.guardian.snapshots._run_subprocess", host.run):
        result = await getattr(mgr, method)()
    return mgr, result


def _dispatcher():
    d = MagicMock()
    d.send = AsyncMock(return_value=True)
    return d


# --- F1: a definitely-failing snapshot does not block the others --------------------


@pytest.mark.asyncio
async def test_prune_skips_a_snapshot_whose_delete_definitely_fails(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS)
    host.stuck = {PREVIOUS[0]}
    mgr, deleted = await _direct(host, cfg, "prune")
    assert [n for n, _ in host.deletes] == PREVIOUS
    assert deleted == 2 and host.names == [PREVIOUS[0]]
    assert mgr.stuck_deletes == [PREVIOUS[0]]
    assert mgr.last_prune_complete is False


@pytest.mark.asyncio
async def test_rotate_superseded_skips_a_snapshot_whose_delete_definitely_fails(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(HEALTHY)
    host.stuck = {HEALTHY[1]}  # superseded, newest first: HEALTHY[1], HEALTHY[0]
    mgr, deleted = await _direct(host, cfg, "rotate_superseded")
    assert [n for n, _ in host.deletes] == [HEALTHY[1], HEALTHY[0]]
    assert deleted == 1 and HEALTHY[2] in host.names
    assert mgr.stuck_deletes == [HEALTHY[1]]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["prune", "rotate_superseded"])
async def test_a_failed_relist_after_a_failed_delete_still_stops(tmp_path, method):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS if method == "prune" else HEALTHY)
    host.stuck = set(host.names)
    host.relist_fails = True
    mgr, _ = await _direct(host, cfg, method)
    assert len(host.deletes) == 1  # outcome unknown: never stack deletions
    assert mgr.stuck_deletes == []


@pytest.mark.asyncio
async def test_a_failed_delete_whose_snapshot_went_counts_as_deleted(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS)
    host.vanish = {PREVIOUS[0]}
    mgr, deleted = await _direct(host, cfg, "prune")
    assert deleted == 3 and host.names == []
    assert mgr.last_delete_error is None and mgr.last_prune_complete is True


@pytest.mark.asyncio
async def test_a_stuck_snapshot_does_not_stop_the_window_and_is_named_after_it(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.snapshots.retention = 1
    host = StuckHost(PREVIOUS + HEALTHY)
    host.stuck = {PREVIOUS[0]}
    host.now = OPEN
    d = _dispatcher()
    while host.now < T0:
        await host.tick(cfg, healthy=True, dispatcher=d)
        host.now += timedelta(minutes=1)
    # Everything routine went except the stuck one; the newest healthy remains.
    healthy_left = [n for n in host.names if n.endswith("-healthy")]
    assert [n for n in host.names if n.endswith("-pre-recovery")] == [PREVIOUS[0]]
    assert healthy_left == host.creates[-1:]
    assert all(n not in host.names for n in HEALTHY)
    # Skipping the settle after a stuck attempt covers ONE real delete, never two.
    real = [at for name, at in host.deletes if name != PREVIOUS[0]]
    assert all(b - a >= SETTLE for a, b in zip(real, real[1:], strict=False))
    assert not d.send.await_args_list  # nothing said while the window is open
    # The first tick after the window names it, once (throttled).
    host.now = T0 + timedelta(hours=2)
    await host.tick(cfg, healthy=True, dispatcher=d)
    host.now += timedelta(minutes=1)
    await host.tick(cfg, healthy=True, dispatcher=d)
    stuck_alerts = [c.args[0] for c in d.send.await_args_list if PREVIOUS[0] in c.args[0].body]
    assert len(stuck_alerts) == 1
    assert stuck_alerts[0].severity.name == "WARNING"


@pytest.mark.asyncio
async def test_a_snapshot_deleted_later_in_the_window_is_not_reported(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS[:1])
    host.stuck = {PREVIOUS[0]}
    host.now = OPEN
    d = _dispatcher()
    await host.tick(cfg, dispatcher=d)
    host.stuck = set()
    host.now += SETTLE + timedelta(minutes=1)
    await host.tick(cfg, dispatcher=d)
    assert host.names == []
    host.now = T0 + timedelta(hours=2)
    await host.tick(cfg, dispatcher=d)
    assert not d.send.await_args_list


@pytest.mark.asyncio
async def test_an_unwritable_stuck_record_never_aborts_the_tick(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS[:1])
    host.stuck = {PREVIOUS[0]}
    host.now = OPEN
    await host.tick(cfg)
    assert (cfg.state_path / ".window_stuck").exists()
    host.now = T0 + timedelta(hours=2)
    with patch.object(Path, "unlink", side_effect=PermissionError("read-only state dir")):
        await host.tick(cfg, dispatcher=_dispatcher())  # must not raise


# --- F2: routine deletes are spaced by the settle interval --------------------------


@pytest.mark.asyncio
async def test_routine_deletes_in_one_window_are_spaced_by_the_settle(tmp_path):
    cfg = _cfg(tmp_path)
    host = StuckHost(PREVIOUS)
    host.now = OPEN
    await host.tick(cfg)
    assert len(host.deletes) == 1  # never back-to-back
    assert not (cfg.state_path / ".last_window_deletes").exists()  # incomplete
    while host.now < T0:
        host.now += timedelta(minutes=1)
        await host.tick(cfg)
    times = [at for _, at in host.deletes]
    assert len(times) == 3 and host.names == []
    assert all(b - a >= SETTLE for a, b in zip(times, times[1:], strict=False))
    assert (cfg.state_path / ".last_window_deletes").exists()
    assert not settled(cfg, times[-1] + timedelta(seconds=30))


# --- N2: an invalid configured timezone is said out loud ----------------------------


@pytest.mark.asyncio
async def test_an_invalid_window_timezone_alerts_once(tmp_path):
    cfg = _cfg(tmp_path, tz="Not/AZone")
    host = StuckHost()
    host.now = T0 + timedelta(hours=2)
    d = _dispatcher()
    await host.tick(cfg, dispatcher=d)
    host.now += timedelta(minutes=1)
    await host.tick(cfg, dispatcher=d)
    alerts = [c.args[0] for c in d.send.await_args_list]
    assert len(alerts) == 1 and "Not/AZone" in alerts[0].body
    assert alerts[0].severity.name == "WARNING"


@pytest.mark.asyncio
async def test_a_valid_window_timezone_says_nothing(tmp_path):
    cfg = _cfg(tmp_path, tz="Europe/Berlin")
    host = StuckHost()
    host.now = T0 + timedelta(hours=2)
    d = _dispatcher()
    await host.tick(cfg, dispatcher=d)
    assert not d.send.await_args_list


# --- N3: the host recovery agent leaves its snapshot to the window ------------------


def test_recovery_prompt_does_not_delete_its_snapshot():
    prompt = (Path(__file__).parents[2] / "config" / "guardian-claude.md").read_text()
    assert "incus snapshot delete" not in prompt
    assert "deletion window" in prompt
