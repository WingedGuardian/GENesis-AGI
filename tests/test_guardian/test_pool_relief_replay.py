"""Acceptance replay: the thin-pool exhaustion, re-run through the REAL code.

A fake host models what the incident's host did, and nothing more:

* an LVM thin pool whose used space is the container's own data plus the
  copy-on-write divergence of its OLDEST guardian snapshot, growing ~2.3 GB/day
  (the measured rate) from that snapshot's creation;
* LVM's own behaviour: ``lvcreate`` of a thin snapshot is refused once data%
  exceeds the autoextend threshold (80) and the VG cannot supply a full
  autoextend step; LVM never extends partially;
* incus snapshot create / list / delete, keyed by name.

The real ``SnapshotManager``, ``_maintain_snapshots`` (the daily healthy
rotation) and ``check_pool_pressure`` (relief) run against it on a simulated
clock. The pre-fix code let the pool reach 100%; the bar is that it never does.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.check import _maintain_snapshots
from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool_pressure import check_pool_pressure
from genesis.guardian.snapshots import SnapshotManager

_GiB = 1024**3
_EXTENT = 4 * 1024**2
T0 = datetime(2026, 1, 1, 16, 26, tzinfo=UTC)


class FakeHost:
    def __init__(
        self,
        *,
        size: int,
        base_used: int,
        churn_per_day: int,
        vg_free: int,
        profile: str | None = "genesis-thinpool",
    ) -> None:
        self.now = T0
        self.size = size
        self.base_used = base_used
        self.churn = churn_per_day
        self.vg_free = vg_free
        self.profile = profile
        self.snaps: dict[str, datetime] = {}
        self.max_data_pct = 0.0
        self.creates_refused = 0
        self.extended_bytes = 0

    # -- the pool model -------------------------------------------------
    def used(self) -> float:
        if not self.snaps:
            return self.base_used
        oldest = min(self.snaps.values())
        div = self.churn * (self.now - oldest).total_seconds() / 86400
        return min(self.size, self.base_used + div)

    def data_pct(self) -> float:
        return 100.0 * self.used() / self.size

    # -- subprocess fake --------------------------------------------------
    async def run(self, *argv, timeout: float = 0, stdin_data=None):  # noqa: ARG002
        a = list(argv)
        if a[:2] == ["sudo", "-n"]:
            a = a[2:]
        cmd = a[0]
        if cmd == "incus" and a[1:3] == ["config", "device"]:
            return 0, "default\n", ""
        if cmd == "incus" and a[1] == "storage" and a[2] == "show":
            return 0, "config:\n  lvm.thinpool_name: IncusThinPool\ndriver: lvm\nsource: vg0\n", ""
        if cmd == "incus" and a[1] == "storage" and a[2] == "get":
            return 0, "IncusThinPool\n", ""
        if cmd == "lvs":
            prof = f" {self.profile}" if self.profile else ""
            row = f"  {self.data_pct():.2f}  40.00  {self.size} 88080384 IncusThinPool{prof}\n"
            return 0, row, ""
        if cmd == "vgs":
            if "vg_extent_size" in a:
                return 0, f"  {_EXTENT}\n", ""
            return 0, f"  {self.vg_free}\n", ""
        if cmd == "lvextend":
            grow = int(a[a.index("-L") + 1].lstrip("+").rstrip("b"))
            if grow > self.vg_free:
                return 5, "", "Insufficient free space"
            self.vg_free -= grow
            self.size += grow
            self.extended_bytes += grow
            return 0, "", ""
        if cmd == "incus" and a[1] == "snapshot":
            verb = a[2]
            if verb == "list":
                rows = [
                    {"name": n, "created_at": t.isoformat().replace("+00:00", "Z")}
                    for n, t in self.snaps.items()
                ]
                return 0, json.dumps(rows), ""
            if verb == "delete":
                self.snaps.pop(a[4], None)
                return 0, "", ""
            if verb == "create":
                # LVM's refusal comes from the autoextend profile threshold;
                # without a profile only the guardian's own gate refuses.
                step = self.size * 20 // 100
                if self.profile and self.data_pct() > 80 and self.vg_free < step:
                    self.creates_refused += 1
                    return (
                        1,
                        "",
                        (
                            "Error: Create instance snapshot: Failed to run: lvcreate: "
                            "Cannot create new thin volume, free space in thin pool "
                            "vg0/IncusThinPool reached threshold."
                        ),
                    )
                self.snaps[a[4]] = self.now
                return 0, "", ""
        if cmd == "df":
            return 1, "", "not modelled"
        return 0, "", ""


def _config(tmp_path) -> GuardianConfig:
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "state")
    return cfg


async def _simulate(host: FakeHost, cfg: GuardianConfig, days: float, *, relief: bool) -> AsyncMock:
    dispatcher = AsyncMock()
    snapshots = SnapshotManager(cfg)
    # 15-minute steps (the real tick is 30s): coarse enough to keep this suite
    # fast, fine against a fill that takes days. History samples per step.
    tick = timedelta(minutes=15)
    cfg.storage_pool.history_sample_interval_s = 900
    end = host.now + timedelta(days=days)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return host.now

    with (
        patch("genesis.guardian.pool._run_subprocess", host.run),
        patch("genesis.guardian.snapshots._run_subprocess", host.run),
        patch("genesis.guardian.check.datetime", _Clock),
        patch("genesis.guardian.snapshots.datetime", _Clock),
        patch("genesis.guardian.pool_pressure.datetime", _Clock),
    ):
        # The incident's starting point: one healthy snapshot, just taken.
        await _maintain_snapshots(cfg, snapshots, is_healthy=True, dispatcher=dispatcher)
        while host.now < end:
            host.now += tick
            if relief:
                await check_pool_pressure(cfg, dispatcher, snapshots, now=host.now, run=host.run)
            await _maintain_snapshots(cfg, snapshots, is_healthy=True, dispatcher=dispatcher)
            host.max_data_pct = max(host.max_data_pct, host.data_pct())
    return dispatcher


def _incident_host(**kw) -> FakeHost:
    # 64.9 GiB pool and 3.9 GiB of VG free that LVM's 20% autoextend step
    # could never use, as measured. Container data + churn are calibrated to
    # the measured day-1 point that started the loop: sixteen hours after the
    # last good rotation the pool was already at 81%, over LVM's 80% refusal
    # line, so the next day's create-first rotation was refused. (The measured
    # week AVERAGE was ~2.3 GiB/day; day 1 ran hotter.) With less than this the
    # model never refuses a rotation and the control arm below cannot fail.
    base = dict(
        size=int(64.9 * _GiB) // _EXTENT * _EXTENT,
        base_used=int(49.0 * _GiB),
        churn_per_day=int(3.0 * _GiB),
        vg_free=1003 * _EXTENT,
    )
    base.update(kw)
    return FakeHost(**base)


@pytest.mark.asyncio
async def test_harness_reproduces_the_incident_without_relief(tmp_path) -> None:
    """Control arm: with relief off AND the pre-fix create-first rotation, the
    model must fill the pool — otherwise the other tests prove nothing."""
    host = _incident_host()
    cfg = _config(tmp_path)
    with patch.object(SnapshotManager, "_stale_lifeline", AsyncMock(return_value=None)):
        await _simulate(host, cfg, days=10, relief=False)
    assert host.max_data_pct >= 99.9
    assert host.creates_refused > 0


@pytest.mark.asyncio
async def test_pool_never_fills_with_the_fix(tmp_path) -> None:
    host = _incident_host()
    cfg = _config(tmp_path)
    await _simulate(host, cfg, days=10, relief=True)
    assert host.max_data_pct < 95.0, host.max_data_pct
    # A rollback lifeline exists at the end: the fix frees space without
    # leaving the container permanently unprotected.
    assert any(n.endswith("-healthy") for n in host.snaps)


@pytest.mark.asyncio
async def test_delete_first_rotation_alone_prevents_the_fill(tmp_path) -> None:
    """The root cause, isolated: relief is live (it records the history the
    pressure model needs) but its own actions are disabled, so only the
    delete-first rotation can free anything. The profile stays ON: LVM's
    threshold refusal is what jams create-first rotation."""
    host = _incident_host()
    cfg = _config(tmp_path)
    with (
        patch("genesis.guardian.pool_pressure.plan_delete", return_value=None),
        patch("genesis.guardian.pool_pressure.plan_extend", return_value=None),
    ):
        await _simulate(host, cfg, days=10, relief=True)
    assert host.creates_refused > 0  # guard-the-guard: the jam really happened
    assert host.max_data_pct < 100.0, host.max_data_pct


@pytest.mark.asyncio
async def test_relief_alone_prevents_the_fill(tmp_path) -> None:
    """Relief as an independent layer: rotation stuck create-first (profile
    ON so LVM refuses, as it did)."""
    host = _incident_host()
    cfg = _config(tmp_path)
    with patch.object(SnapshotManager, "_stale_lifeline", AsyncMock(return_value=None)):
        await _simulate(host, cfg, days=10, relief=True)
    assert host.creates_refused > 0  # guard-the-guard: rotation really jammed
    assert host.max_data_pct < 100.0, host.max_data_pct


@pytest.mark.asyncio
async def test_extend_uses_the_stranded_vg_space(tmp_path) -> None:
    """Rotation stuck (so pressure builds): relief's FIRST action is to grow
    the pool into the VG space dmeventd's all-or-nothing autoextend stranded."""
    host = _incident_host()
    cfg = _config(tmp_path)
    with patch.object(SnapshotManager, "_stale_lifeline", AsyncMock(return_value=None)):
        await _simulate(host, cfg, days=4, relief=True)
    assert host.extended_bytes > 3 * _GiB
    assert host.vg_free >= 512 * 1024**2


@pytest.mark.asyncio
async def test_a_roomy_pool_keeps_its_lifeline(tmp_path) -> None:
    """Negative control: plenty of room, normal churn — relief never fires,
    the lifeline rotates daily, nothing is extended."""
    host = _incident_host(size=200 * _GiB // _EXTENT * _EXTENT, vg_free=50 * _GiB)
    cfg = _config(tmp_path)
    dispatcher = await _simulate(host, cfg, days=5, relief=True)
    assert host.extended_bytes == 0
    assert host.creates_refused == 0
    assert len([n for n in host.snaps if n.endswith("-healthy")]) == 1
    dispatcher.send.assert_not_called()


def _freed(dispatcher: AsyncMock) -> list[str]:
    return [
        c.args[0].title
        for c in dispatcher.send.await_args_list
        if "freed" in c.args[0].title or "extended" in c.args[0].title
    ]


@pytest.mark.parametrize(
    ("data_pct", "profile"),
    [
        (87.1, None),  # above the guardian's own 85% gate
        (81.7, "genesis-thinpool"),  # above LVM's 80% refusal line
    ],
)
@pytest.mark.asyncio
async def test_stable_but_full_pool_keeps_its_lifeline(tmp_path, data_pct, profile) -> None:
    """Negative control the review asked for: a pool that is FULL but barely
    growing refuses every rotation — and must still keep its rollback
    snapshot, because deleting it frees nothing. (An earlier revision deleted
    it here, measured by review.)"""
    size = int(64.9 * _GiB) // _EXTENT * _EXTENT
    host = _incident_host(
        base_used=int(size * data_pct / 100),
        churn_per_day=int(0.05 * _GiB),
        vg_free=0,
        profile=profile,
    )
    # The lifeline already exists (taken when the pool had room); the pool
    # then sat full. Without this seed the first create is refused and there
    # is never a lifeline to keep — a vacuous pass.
    seeded = "guardian-20251231-162600-healthy"
    host.snaps[seeded] = T0 - timedelta(hours=1)
    cfg = _config(tmp_path)
    dispatcher = await _simulate(host, cfg, days=5, relief=True)
    assert host.creates_refused > 0 or data_pct >= 85  # rotations really were refused
    assert seeded in host.snaps, host.snaps
    assert _freed(dispatcher) == []
    # Refusals are loud but throttled (realert_hours), not hourly.
    refused = [c for c in dispatcher.send.await_args_list if "NOT refreshed" in c.args[0].title]
    assert 1 <= len(refused) <= 5 * 24 / cfg.storage_pool.realert_hours + 1
