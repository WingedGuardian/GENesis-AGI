"""Tests for guardian pool relief (pool_relief.py): hard-reserve relief that
frees guardian-owned snapshots, and the fail-closed rules around it."""

from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.config import GuardianConfig, StoragePoolConfig
from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.pool_relief import (
    SnapshotInfo,
    check_pool_relief,
    delete_first_allowed,
    effective_relief_mode,
    plan_delete,
    record_action,
    shortfall,
    validate_relief_config,
)
from genesis.guardian.snapshots import HEALTHY_SUFFIX

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
_GB = 1024**3


def _cfg(tmp_path) -> GuardianConfig:
    c = GuardianConfig()
    c.state_dir = str(tmp_path / "state")
    return c


def _lvm(data: float, meta: float = 40.0, **kw) -> StoragePoolStatus:
    base = dict(
        detected=True,
        data_pct=data,
        metadata_pct=meta,
        pool_size_bytes=100 * _GB,
        pool_name="default",
        vg_name="vg0",
        thinpool_lv="IncusThinPool",
    )
    base.update(kw)
    return StoragePoolStatus(**base)


class _Snaps:
    def __init__(self, names: dict[str, datetime] | None = None, *, delete_ok=True, fail=()):
        self.names = dict(names or {})
        self.deleted: list[str] = []
        self.delete_ok = delete_ok
        self.fail = set(fail)
        self.error = "Error: busy"
        self.bypassed: list[str] = []
        self.gone_anyway: set[str] = set()

    async def list_snapshot_meta_strict(self):
        return sorted(self.names.items(), reverse=True)

    async def delete_healthy(self, name, *, healthy_confirmed=False, replaced_by=None):
        assert healthy_confirmed or replaced_by, "relief reached the chokepoint unconfirmed"
        return await self._delete(name)

    async def delete(self, name):
        # Mirrors SnapshotManager.delete: a rollback snapshot is refused here, so
        # relief calling the plain path for one is caught (review: the fake used
        # to accept it and hid exactly that bypass).
        if name.endswith(HEALTHY_SUFFIX):
            self.bypassed.append(name)
            self.last_delete_error = "healthy snapshot: use delete_healthy()"
            return False
        return await self._delete(name)

    async def _delete(self, name):
        self.deleted.append(name)
        ok = self.delete_ok and name not in self.fail
        self.last_delete_error = None if ok else self.error
        if ok or name in self.gone_anyway:
            self.names.pop(name, None)
        return ok


async def _pass(cfg, snaps, status, now=T0, *, measures=None, extra=(), healthy_confirmed=True):
    d = AsyncMock()
    measure = AsyncMock(side_effect=measures) if measures else AsyncMock(return_value=status)
    with ExitStack() as stack:
        stack.enter_context(patch("genesis.guardian.pool.measure_storage_pool", measure))
        for p in extra:
            stack.enter_context(p)
        out = await check_pool_relief(cfg, d, snaps, now=now, healthy_confirmed=healthy_confirmed)
    return out, d


OLD = T0 - timedelta(days=3)


# --- configuration -----------------------------------------------------------


class TestMode:
    def test_live_default(self) -> None:
        assert effective_relief_mode(StoragePoolConfig()) == "live"

    def test_yaml_bare_off_is_off(self) -> None:
        import yaml

        raw = yaml.safe_load("m: off")["m"]
        assert effective_relief_mode(StoragePoolConfig(relief_mode=raw)) == "off"

    def test_invalid_is_alert_only(self) -> None:
        assert effective_relief_mode(StoragePoolConfig(relief_mode="bogus")) == "alert_only"

    def test_kill_switch(self, monkeypatch) -> None:
        monkeypatch.setenv("GUARDIAN_POOL_RELIEF_DISABLED", "1")
        assert effective_relief_mode(StoragePoolConfig()) == "alert_only"


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("storage_pool", "data_high_pct", -1),
        ("storage_pool", "metadata_high_pct", "70"),
        ("storage_pool", "min_reserve_pct", 300),
        ("storage_pool", "min_reserve_pct", "3%"),
        ("storage_pool", "min_reserve_pct", float("nan")),
        ("storage_pool", "min_reserve_pct", True),
        ("storage_pool", "min_meta_reserve_pct", -1),
        ("storage_pool", "realert_hours", 0),
        ("snapshots", "prefix", ""),
        ("snapshots", "prefix", "  "),
    ],
)
def test_validation_rejects(tmp_path, section, key, value) -> None:
    cfg = _cfg(tmp_path)
    assert validate_relief_config(cfg) is None  # control
    setattr(getattr(cfg, section), key, value)
    assert validate_relief_config(cfg) is not None


# --- shortfall + planning ----------------------------------------------------


class TestShortfall:
    def test_room(self) -> None:
        assert shortfall(_lvm(70.0), StoragePoolConfig()) is None

    def test_data_at_reserve_counts(self) -> None:
        """Equality is a shortfall: the reserve is for absorbing one burst."""
        assert "data" in shortfall(_lvm(97.0), StoragePoolConfig())

    def test_metadata_reserve(self) -> None:
        assert "metadata" in shortfall(_lvm(50.0, meta=91.0), StoragePoolConfig())

    def test_non_lvm_uses_pool_used(self) -> None:
        st = StoragePoolStatus(detected=True, pool_used_pct=98.0, pool_name="p")
        assert shortfall(st, StoragePoolConfig()) is not None


def _snap(name, hours_old, healthy):
    return SnapshotInfo(name, T0 - timedelta(hours=hours_old), healthy)


class TestPlanDelete:
    def test_pre_recovery_first_oldest_first(self) -> None:
        snaps = [
            _snap("guardian-new-healthy", 100, True),
            _snap("guardian-b-pre-recovery", 1, False),
            _snap("guardian-a-pre-recovery", 5, False),
        ]
        assert plan_delete(snaps) == "guardian-a-pre-recovery"

    def test_superseded_healthy_before_lifeline(self) -> None:
        snaps = [_snap("guardian-new-healthy", 2, True), _snap("guardian-old-healthy", 30, True)]
        assert plan_delete(snaps) == "guardian-old-healthy"

    def test_lifeline_last_then_nothing(self) -> None:
        assert plan_delete([_snap("guardian-x-healthy", 1, True)]) == "guardian-x-healthy"
        assert plan_delete([]) is None


# --- the pass ------------------------------------------------------------------


class TestPass:
    @pytest.mark.asyncio
    async def test_room_does_nothing(self, tmp_path) -> None:
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(70.0))
        assert out == "ok" and snaps.deleted == []
        d.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_short_deletes_one_and_alerts(self, tmp_path) -> None:
        snaps = _Snaps(
            {
                "guardian-20260101-000000-pre-recovery": OLD,
                "guardian-20260102-000000-healthy": OLD,
            }
        )
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == "deleted:guardian-20260101-000000-pre-recovery"
        assert snaps.deleted == ["guardian-20260101-000000-pre-recovery"]
        assert "freed" in d.send.await_args.args[0].title

    @pytest.mark.asyncio
    async def test_settles_between_deletes(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        snaps = _Snaps(
            {
                "guardian-20260101-000000-pre-recovery": OLD,
                "guardian-20260102-000000-pre-recovery": OLD + timedelta(hours=1),
            }
        )
        o1, _ = await _pass(cfg, snaps, _lvm(98.0), T0)
        o2, _ = await _pass(cfg, snaps, _lvm(98.0), T0 + timedelta(seconds=30))
        o3, _ = await _pass(cfg, snaps, _lvm(98.0), T0 + timedelta(minutes=6))
        assert o1.startswith("deleted:") and o2 == "settling" and o3.startswith("deleted:")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        [
            StoragePoolStatus(detected=False),
            _lvm(98.0, thinpool_lv=None),  # several thin pools in the VG
            _lvm(98.0, pool_name=None),
        ],
    )
    async def test_undetected_or_ambiguous_never_acts(self, tmp_path, status) -> None:
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        out, _ = await _pass(_cfg(tmp_path), snaps, status)
        assert out in ("unmeasured", "ambiguous_pool") and snaps.deleted == []

    @pytest.mark.asyncio
    async def test_pool_changed_before_the_act(self, tmp_path) -> None:
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        out, _ = await _pass(
            _cfg(tmp_path),
            snaps,
            None,
            measures=[_lvm(98.0), _lvm(50.0, pool_name="other")],
        )
        assert out == "pool_changed" and snaps.deleted == []

    @pytest.mark.asyncio
    async def test_unwritable_state_stops_the_delete(self, tmp_path) -> None:
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        out, _ = await _pass(
            _cfg(tmp_path),
            snaps,
            _lvm(98.0),
            extra=[patch("genesis.guardian.pool_relief._save_state", return_value=False)],
        )
        assert out == "state_unwritable" and snaps.deleted == []

    @pytest.mark.asyncio
    async def test_invalid_config_alert_only_once(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.storage_pool.min_reserve_pct = 300
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        o1, d1 = await _pass(cfg, snaps, _lvm(98.0))
        o2, d2 = await _pass(cfg, snaps, _lvm(98.0), T0 + timedelta(minutes=1))
        assert o1 == o2 == "invalid_config" and snaps.deleted == []
        assert d1.send.await_count == 1 and d2.send.await_count == 0

    @pytest.mark.asyncio
    async def test_alert_only_never_deletes(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.storage_pool.relief_mode = "alert_only"
        snaps = _Snaps({"guardian-20260101-000000-pre-recovery": OLD})
        out, d = await _pass(cfg, snaps, _lvm(98.0))
        assert out == "alert_only" and snaps.deleted == []
        assert "alert-only" in d.send.await_args.args[0].title

    @pytest.mark.asyncio
    async def test_list_failure_is_not_an_empty_list(self, tmp_path) -> None:
        snaps = _Snaps({})
        snaps.list_snapshot_meta_strict = AsyncMock(return_value=None)
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == "list_failed"
        assert "cannot list" in d.send.await_args.args[0].title

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("writable", "expected"), [(True, 1), (False, 0)])
    async def test_no_target_alert_is_throttled_and_persisted(
        self,
        tmp_path,
        writable,
        expected,
    ) -> None:
        """Once per realert window — and never when its stamp cannot persist
        (else every 30s tick)."""
        cfg = _cfg(tmp_path)
        extra = (
            []
            if writable
            else [
                patch("genesis.guardian.pool_relief._save_state", return_value=False),
            ]
        )
        sends = 0
        for i in range(5):
            out, d = await _pass(
                cfg,
                _Snaps({}),
                _lvm(98.0),
                T0 + timedelta(seconds=30 * i),
                extra=extra,
            )
            assert out == "no_target"
            sends += d.send.await_count
        assert sends == expected


# --- delete-first permission ---------------------------------------------------


class TestDeleteFirstAllowed:
    def test_live_and_idle(self, tmp_path) -> None:
        assert delete_first_allowed(_cfg(tmp_path), T0) is True

    @pytest.mark.parametrize("mode", ["alert_only", False])
    def test_brake_modes(self, tmp_path, mode) -> None:
        cfg = _cfg(tmp_path)
        cfg.storage_pool.relief_mode = mode
        assert delete_first_allowed(cfg, T0) is False

    def test_kill_switch(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("GUARDIAN_POOL_RELIEF_DISABLED", "1")
        assert delete_first_allowed(_cfg(tmp_path), T0) is False

    def test_invalid_config(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.snapshots.prefix = ""
        assert delete_first_allowed(cfg, T0) is False

    def test_not_while_relief_settles(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        cfg.state_path.mkdir(parents=True)
        (cfg.state_path / "pool_relief_state.json").write_text(
            json.dumps({"last_action": T0.isoformat()})
        )
        assert delete_first_allowed(cfg, T0 + timedelta(minutes=1)) is False
        assert delete_first_allowed(cfg, T0 + timedelta(minutes=6)) is True


class TestFallThroughAndCannotAct:
    """Review findings: one undeletable snapshot must not pin relief, and a
    relief that can never act must say so (not stay silent at INFO)."""

    @pytest.mark.asyncio
    async def test_failed_delete_falls_through_to_the_next(self, tmp_path) -> None:
        pre = "guardian-20260101-000000-pre-recovery"
        pre2 = "guardian-20260102-000000-pre-recovery"
        life = "guardian-20260105-000000-healthy"
        snaps = _Snaps({pre: OLD, pre2: OLD + timedelta(days=1), life: T0 - timedelta(days=1)},
                       fail={pre})
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"deleted:{pre2}"
        assert snaps.deleted == [pre, pre2]  # tried in plan order, stopped at one success
        assert f"deleting {pre} failed first" in d.send.await_args.args[0].body

    @pytest.mark.asyncio
    async def test_failure_never_falls_through_to_the_lifeline(self, tmp_path) -> None:
        """Audit: the earlier failure may be the daemon still deleting it (a
        re-delete while one is in flight fails with a non-timeout error), so the
        space may already be coming back — never take the lifeline on top."""
        pre = "guardian-20260101-000000-pre-recovery"
        life = "guardian-20260105-000000-healthy"
        snaps = _Snaps({pre: OLD, life: T0 - timedelta(days=1)}, fail={pre})
        out, _ = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"delete_failed:{pre}"
        assert snaps.deleted == [pre] and life in snaps.names

    @pytest.mark.asyncio
    async def test_all_deletes_failing_alerts_once_per_hour(self, tmp_path) -> None:
        a, b = "guardian-20260101-000000-pre-recovery", "guardian-20260105-000000-healthy"
        cfg = _cfg(tmp_path)
        snaps = _Snaps({a: OLD, b: T0 - timedelta(days=1)}, delete_ok=False)
        out, d = await _pass(cfg, snaps, _lvm(98.0))
        assert out == f"delete_failed:{a}"  # stops before the lifeline
        assert b not in snaps.deleted
        assert d.send.await_count == 1
        out2, d2 = await _pass(cfg, snaps, _lvm(98.0), now=T0 + timedelta(minutes=6))
        assert out2.startswith("delete_failed") and d2.send.await_count == 0

    @pytest.mark.asyncio
    async def test_superseded_healthy_is_not_called_the_lifeline(self, tmp_path) -> None:
        old = "guardian-20260101-000000-healthy"
        new = "guardian-20260105-000000-healthy"
        snaps = _Snaps({old: OLD, new: T0 - timedelta(days=1)})
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"deleted:{old}"
        assert "rollback lifeline" not in d.send.await_args.args[0].body

    @pytest.mark.parametrize("status", [
        StoragePoolStatus(detected=False, detail="lvs failed"),
        _lvm(98.0, thinpool_lv=None),
    ])
    @pytest.mark.asyncio
    async def test_cannot_act_alerts_only_once_it_persists(self, tmp_path, status) -> None:
        cfg = _cfg(tmp_path)
        snaps = _Snaps({"guardian-20260101-000000-healthy": OLD})
        _, d1 = await _pass(cfg, snaps, status)
        _, d2 = await _pass(cfg, snaps, status, now=T0 + timedelta(minutes=30))
        assert d1.send.await_count == 0 and d2.send.await_count == 0  # a blip never pages
        _, d3 = await _pass(cfg, snaps, status, now=T0 + timedelta(hours=1, minutes=1))
        assert d3.send.await_count == 1
        assert d3.send.await_args.args[0].title == "Pool relief cannot act"
        _, d4 = await _pass(cfg, snaps, status, now=T0 + timedelta(hours=5))
        assert d4.send.await_count == 0  # daily throttle
        assert snaps.deleted == []

    @pytest.mark.asyncio
    async def test_a_good_measurement_resets_the_clock(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        snaps = _Snaps({})
        bad = StoragePoolStatus(detected=False)
        await _pass(cfg, snaps, bad)
        await _pass(cfg, snaps, _lvm(50.0), now=T0 + timedelta(minutes=50))
        _, d = await _pass(cfg, snaps, bad, now=T0 + timedelta(hours=1, minutes=10))
        assert d.send.await_count == 0  # the outage restarted, not an hour old

    def test_record_action_holds_back_delete_first_and_relief(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        assert delete_first_allowed(cfg, T0)
        assert record_action(cfg, T0)
        assert not delete_first_allowed(cfg, T0 + timedelta(minutes=1))
        assert delete_first_allowed(cfg, T0 + timedelta(minutes=6))


class TestReviewRound1:
    """External review round 1 on the core PR."""

    @pytest.mark.parametrize("status", [
        # metadata-only reading from an unnamed pool (several thin pools)
        _lvm(None, meta=95.0, thinpool_lv=None),
        _lvm(98.0, thinpool_lv=None),
    ])
    @pytest.mark.asyncio
    async def test_unnamed_pool_never_acts_whatever_figures(self, tmp_path, status) -> None:
        snaps = _Snaps({"guardian-20260101-000000-healthy": OLD})
        out, _ = await _pass(_cfg(tmp_path), snaps, status)
        assert out == "ambiguous_pool" and snaps.deleted == []

    @pytest.mark.asyncio
    async def test_signal_less_measurement_is_not_ok(self, tmp_path) -> None:
        cfg = _cfg(tmp_path)
        blank = _lvm(None, meta=None)
        snaps = _Snaps({"guardian-20260101-000000-healthy": OLD})
        out, _ = await _pass(cfg, snaps, blank)
        assert out == "no_signal"
        _, d = await _pass(cfg, snaps, blank, now=T0 + timedelta(hours=2))
        assert d.send.await_args.args[0].title == "Pool relief cannot act"

    @pytest.mark.parametrize("value", ["false", "no", 0, None])
    @pytest.mark.asyncio
    async def test_non_bool_enabled_never_acts(self, tmp_path, value) -> None:
        cfg = _cfg(tmp_path)
        cfg.storage_pool.enabled = value
        snaps = _Snaps({"guardian-20260101-000000-healthy": OLD})
        out, _ = await _pass(cfg, snaps, _lvm(98.0))
        assert out in ("invalid_config", "disabled") and snaps.deleted == []
        assert not delete_first_allowed(cfg)

    @pytest.mark.asyncio
    async def test_delete_timeout_stops_the_pass(self, tmp_path) -> None:
        """The client timing out says nothing about the daemon: never go on to
        the next snapshot (possibly the lifeline) in the same pass."""
        pre = "guardian-20260101-000000-pre-recovery"
        life = "guardian-20260105-000000-healthy"
        snaps = _Snaps({pre: OLD, life: T0 - timedelta(days=1)}, fail={pre})
        snaps.error = "timeout"
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"delete_indeterminate:{pre}"
        assert snaps.deleted == [pre]
        assert d.send.await_args.args[0].title == "Guardian delete outcome unknown"

    @pytest.mark.asyncio
    async def test_failed_delete_that_happened_anyway_counts_once(self, tmp_path) -> None:
        pre = "guardian-20260101-000000-pre-recovery"
        life = "guardian-20260105-000000-healthy"
        snaps = _Snaps({pre: OLD, life: T0 - timedelta(days=1)}, fail={pre})
        snaps.error = "timeout"
        snaps.gone_anyway = {pre}
        out, _ = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"deleted:{pre}" and snaps.deleted == [pre]



class TestHealthyOnlyAfterTheProbe:
    """Review round 3: relief runs before this tick's health probe too, and a
    container that failed since the last tick still reads HEALTHY there. So a
    rollback snapshot is a relief target only on a probe-confirmed HEALTHY
    tick (the post-cycle pass)."""

    LIFE = "guardian-20260105-000000-healthy"
    OLDH = "guardian-20260103-000000-healthy"
    PRE = "guardian-20260101-000000-pre-recovery"

    @pytest.mark.asyncio
    async def test_pre_probe_pass_frees_only_non_healthy(self, tmp_path) -> None:
        snaps = _Snaps({self.PRE: OLD, self.OLDH: OLD, self.LIFE: T0 - timedelta(days=1)})
        out, _ = await _pass(_cfg(tmp_path), snaps, _lvm(98.0), healthy_confirmed=False)
        assert out == f"deleted:{self.PRE}"

    @pytest.mark.asyncio
    async def test_pre_probe_pass_defers_silently_when_only_healthy_left(self, tmp_path) -> None:
        snaps = _Snaps({self.OLDH: OLD, self.LIFE: T0 - timedelta(days=1)})
        d = AsyncMock()
        with patch("genesis.guardian.pool.measure_storage_pool", AsyncMock(return_value=_lvm(98.0))):
            out = await check_pool_relief(
                _cfg(tmp_path), d, snaps, now=T0,
                healthy_confirmed=False, alert_when_deferred=False,
            )
        assert out == "healthy_deferred" and snaps.deleted == [] and d.send.await_count == 0

    @pytest.mark.asyncio
    async def test_unhealthy_post_probe_pass_keeps_them_and_alerts(self, tmp_path) -> None:
        snaps = _Snaps({self.LIFE: T0 - timedelta(days=1)})
        out, d = await _pass(_cfg(tmp_path), snaps, _lvm(98.0), healthy_confirmed=False)
        assert out == "lifeline_protected" and snaps.deleted == []
        assert "kept for recovery" in d.send.await_args.args[0].title

    @pytest.mark.asyncio
    async def test_confirmed_healthy_frees_superseded_then_lifeline(self, tmp_path) -> None:
        snaps = _Snaps({self.OLDH: OLD, self.LIFE: T0 - timedelta(days=1)})
        out, _ = await _pass(_cfg(tmp_path), snaps, _lvm(98.0))
        assert out == f"deleted:{self.OLDH}"




def test_timezone_less_stamps_are_due_not_a_crash(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    (cfg.state_path / "pool_relief_state.json").write_text(
        json.dumps({"last_action": "2026-01-10T11:59:00", "cannot_act_since": "2026-01-10T10:00:00"})
    )
    assert delete_first_allowed(cfg, T0) is True


@pytest.mark.asyncio
async def test_timezone_less_cannot_act_stamp_restarts_the_clock(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.state_path.mkdir(parents=True, exist_ok=True)
    (cfg.state_path / "pool_relief_state.json").write_text(
        json.dumps({"cannot_act_since": "2026-01-10T10:00:00"})
    )
    out, d = await _pass(cfg, _Snaps({}), StoragePoolStatus(detected=False))
    assert out == "unmeasured" and d.send.await_count == 0
