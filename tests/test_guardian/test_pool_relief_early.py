"""Tests for pool relief's EARLY level (measured growth) and its LVM partial
extend: what early relief may and may not take, and the extend's guards."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.config import GuardianConfig
from genesis.guardian.pool import StoragePoolStatus
from genesis.guardian.pool_relief import STATE_FILE, check_pool_relief
from genesis.guardian.pool_runway import HISTORY_FILE, PoolSample, load_history
from genesis.guardian.snapshots import HEALTHY_SUFFIX

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
_GB = 1024**3
_MB = 1024**2
KEY = "default|vg0|IncusThinPool"
YOUNG = T0 - timedelta(hours=6)
AGED = T0 - timedelta(hours=60)
LIFELINE = f"guardian-20260110-060000{HEALTHY_SUFFIX}"
AGED_LIFELINE = f"guardian-20260108-000000{HEALTHY_SUFFIX}"
PRE = "guardian-20260109-000000-pre-recovery"


def _cfg(tmp_path, **pool) -> GuardianConfig:
    c = GuardianConfig()
    c.state_dir = str(tmp_path / "state")
    for k, v in pool.items():
        setattr(c.storage_pool, k, v)
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
        metadata_size_bytes=80 * _MB,
        vg_free_bytes=0,
        thinpool_profile=None,
    )
    base.update(kw)
    return StoragePoolStatus(**base)


def _seed(cfg, *, data_at, meta_at=lambda h: 0.40, hours: float = 12, pool: str = KEY) -> None:
    """Write a 5-minute history ending just before T0."""
    path = cfg.state_path / HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i in range(int(hours * 12)):
        ts = T0 - timedelta(hours=hours) + timedelta(minutes=5 * i)
        h = i / 12.0
        lines.append(
            json.dumps(
                {
                    "ts": ts.isoformat(),
                    "pool": pool,
                    "data_used": data_at(h) * 100 * _GB,
                    "data_size": 100 * _GB,
                    "meta_used": meta_at(h) * 80 * _MB,
                    "meta_size": 80 * _MB,
                }
            )
        )
    path.write_text("\n".join(lines) + "\n")


def _ramp_to(end: float, per_h: float = 0.01, hours: float = 12):
    """Data fraction rising ``per_h`` an hour, reaching ``end`` at T0."""
    return lambda h: end - per_h * (hours - h)


class _Snaps:
    def __init__(self, names: dict[str, datetime], *, holds: float | None = 10 * _GB):
        self.names = dict(names)
        self.deleted: list[str] = []
        self.last_delete_error = None
        self.holds = holds  # what LVM says the healthy snapshots hold alone

    async def lifeline_holds_space(self):
        return self.holds

    async def list_snapshot_meta_strict(self):
        return sorted(self.names.items(), reverse=True)

    async def delete_healthy(self, name, *, healthy_confirmed=False, replaced_by=None):
        assert healthy_confirmed or replaced_by
        self.deleted.append(name)
        self.names.pop(name, None)
        return True

    async def delete(self, name):
        assert not name.endswith(HEALTHY_SUFFIX), "rollback snapshot via the plain path"
        self.deleted.append(name)
        self.names.pop(name, None)
        return True


class _Run:
    """lvextend/vgs fake that grows the pool it was asked about."""

    def __init__(self, *, vgs_rc: int = 0, lvextend_rc: int = 0):
        self.calls: list[tuple] = []
        self.vgs_rc, self.lvextend_rc = vgs_rc, lvextend_rc

    async def __call__(self, *argv, timeout: float = 0, stdin_data=None):  # noqa: ARG002
        self.calls.append(argv)
        if "vgs" in argv:
            return self.vgs_rc, "  4194304\n" if self.vgs_rc == 0 else "", "vgs: boom"
        if "lvextend" in argv:
            return self.lvextend_rc, "", "" if self.lvextend_rc == 0 else "no space"
        raise AssertionError(argv)

    @property
    def extended(self) -> bool:
        return any("lvextend" in c for c in self.calls)


async def _pass(cfg, snaps, status, *, measures=None, run=None, now=T0, healthy=True):
    d = AsyncMock()
    measure = AsyncMock(side_effect=measures) if measures else AsyncMock(return_value=status)
    with patch("genesis.guardian.pool.measure_storage_pool", measure):
        out = await check_pool_relief(
            cfg, d, snaps, now=now, healthy_confirmed=healthy, run=run or _Run(),
        )
    return out, d


def _severities(d) -> list[str]:
    return [c.args[0].severity.name for c in d.send.await_args_list]


def _titles(d) -> list[str]:
    return [c.args[0].title for c in d.send.await_args_list]


def _state(cfg) -> dict:
    path = cfg.state_path / STATE_FILE
    return json.loads(path.read_text()) if path.exists() else {}


# --- the early level --------------------------------------------------------------


@pytest.mark.asyncio
async def test_flat_history_near_full_but_above_the_reserve_does_nothing(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=lambda h: 0.90)
    snaps = _Snaps({LIFELINE: YOUNG, PRE: YOUNG})
    out, d = await _pass(cfg, snaps, _lvm(90.0))
    assert out == "ok" and snaps.deleted == [] and not d.send.await_count


@pytest.mark.asyncio
async def test_early_never_takes_a_young_lifeline(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))  # 1 GB/h into 20 GB free: ~20h runway
    snaps = _Snaps({LIFELINE: YOUNG})
    out, d = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "early_no_target"
    assert snaps.deleted == []
    assert _titles(d) == ["Pool filling — nothing the guardian may free yet"]
    assert d.send.await_args.args[0].severity.name == "WARNING"


@pytest.mark.asyncio
async def test_early_frees_a_pre_recovery_snapshot_first(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, d = await _pass(cfg, snaps, _lvm(80.0))
    assert out == f"deleted:{PRE}" and snaps.deleted == [PRE]
    assert _titles(d) == ["Guardian freed pool space early"]
    assert "data would fill" in d.send.await_args.args[0].body


@pytest.mark.asyncio
async def test_early_takes_a_lifeline_past_its_age_cap(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({AGED_LIFELINE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == f"deleted:{AGED_LIFELINE}"


@pytest.mark.asyncio
async def test_a_zero_age_cap_keeps_even_an_old_lifeline_early(tmp_path) -> None:
    cfg = _cfg(tmp_path, lifeline_max_age_hours=0)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({AGED_LIFELINE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "early_no_target" and snaps.deleted == []


@pytest.mark.asyncio
async def test_the_reserve_still_takes_a_young_lifeline(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    snaps = _Snaps({LIFELINE: YOUNG})
    out, d = await _pass(cfg, snaps, _lvm(98.0))
    assert out == f"deleted:{LIFELINE}"
    assert _titles(d) == ["Guardian freed pool space"]


@pytest.mark.asyncio
async def test_eased_from_reserve_to_early_keeps_the_young_lifeline(tmp_path) -> None:
    # Decided at the reserve (98%); by the pre-delete re-measure it is 95%,
    # which is only EARLY pressure — and a young lifeline is not early's.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.95))
    snaps = _Snaps({LIFELINE: YOUNG})
    out, _ = await _pass(cfg, snaps, _lvm(98.0), measures=[_lvm(98.0), _lvm(95.0)])
    assert out == "eased" and snaps.deleted == []


@pytest.mark.asyncio
async def test_early_alert_only_warns_and_never_acts(tmp_path) -> None:
    cfg = _cfg(tmp_path, relief_mode="alert_only")
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({PRE: AGED})
    out, d = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "alert_only" and snaps.deleted == []
    assert _titles(d) == ["Pool filling — relief is alert-only"]


@pytest.mark.asyncio
async def test_early_horizon_zero_disables_early_relief(tmp_path) -> None:
    cfg = _cfg(tmp_path, early_horizon_hours=0)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({PRE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "ok" and snaps.deleted == []


@pytest.mark.asyncio
async def test_metadata_growth_triggers_early_relief(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=lambda h: 0.5, meta_at=lambda h: 0.60 + 0.01 * h)
    snaps = _Snaps({PRE: AGED})
    out, d = await _pass(cfg, snaps, _lvm(50.0, meta=72.0))
    assert out == f"deleted:{PRE}"
    assert "metadata would fill" in d.send.await_args.args[0].body


@pytest.mark.asyncio
async def test_a_pass_records_the_history(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    out, _ = await _pass(cfg, _Snaps({}), _lvm(50.0))
    assert out == "ok"
    h = load_history(cfg.state_path / HISTORY_FILE)
    assert len(h) == 1 and h[0].pool == KEY
    assert h[0].data_used == pytest.approx(50 * _GB)


@pytest.mark.asyncio
async def test_passes_keep_growing_the_history(tmp_path) -> None:
    # One sample per history interval, so the early level has a rate to read
    # on a live install (the seeded tests alone never prove relief writes it).
    cfg = _cfg(tmp_path)
    for minutes in (0, 1, 6, 12):
        await _pass(cfg, _Snaps({}), _lvm(50.0 + minutes / 10), now=T0 + timedelta(minutes=minutes))
    h = load_history(cfg.state_path / HISTORY_FILE)
    assert [s.ts for s in h] == [T0 + timedelta(minutes=m) for m in (0, 6, 12)]


@pytest.mark.asyncio
async def test_a_broken_history_never_blocks_the_reserve(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    with patch("genesis.guardian.pool_relief.record_sample", side_effect=RuntimeError("boom")):
        out, _ = await _pass(cfg, _Snaps({LIFELINE: YOUNG}), _lvm(98.0))
    assert out == f"deleted:{LIFELINE}"


# --- the LVM partial extend ----------------------------------------------------------


def _stranded(data: float = 85.0, **kw) -> StoragePoolStatus:
    # VG free 6 GiB is under one 20% step of a 100 GiB pool.
    base = dict(
        vg_free_bytes=6 * _GB, metadata_size_bytes=84 * _MB, thinpool_profile="genesis-thinpool",
    )
    base.update(kw)
    return _lvm(data, **base)


def _lvextend_argv(run) -> tuple | None:
    return next((c for c in run.calls if "lvextend" in c), None)


class _Slow(_Run):
    """lvextend that times out (the client gave up)."""

    async def __call__(self, *argv, timeout: float = 0, stdin_data=None):  # noqa: ARG002
        if "lvextend" in argv:
            self.calls.append(argv)
            return -1, "", "timeout"
        return await super().__call__(*argv, timeout=timeout)


@pytest.mark.asyncio
async def test_extend_uses_stranded_vg_space_before_deleting(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85))
    snaps = _Snaps({PRE: AGED})
    run = _Run()
    out, d = await _pass(cfg, snaps, _stranded(), run=run)
    assert out == "extended" and snaps.deleted == []
    assert _lvextend_argv(run)[:4] == ("sudo", "-n", "lvextend", "-L")
    assert _titles(d) == ["Guardian extended the thin pool"]
    assert _state(cfg)["last_action"] == T0.isoformat()


@pytest.mark.asyncio
async def test_extend_follows_autoextends_trigger_not_the_rate(tmp_path) -> None:
    # Review: the permanent extend must not hang on the noisy estimate. A
    # FLAT pool at 85% with stranded VG space is exactly where autoextend
    # would have acted, so it is extended; nothing else is under pressure.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=lambda h: 0.85)
    run = _Run()
    out, d = await _pass(cfg, _Snaps({PRE: AGED}), _stranded(), run=run)
    assert out == "extended"
    assert "autoextend threshold" in d.send.await_args.args[0].body


@pytest.mark.asyncio
async def test_below_the_autoextend_threshold_nothing_extends(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.79))  # a real rate, but under 80%
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({PRE: AGED}), _stranded(data=79.0), run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_once_extended_the_plan_is_spent(tmp_path) -> None:
    # After an extend VG free is down to the keep: no second extend, and no
    # separate cooldown is needed for that.
    cfg = _cfg(tmp_path)
    run = _Run()
    spent = _stranded(vg_free_bytes=512 * _MB)
    out, _ = await _pass(cfg, _Snaps({PRE: AGED}), spent, run=run)
    assert not run.extended and out == "ok"


@pytest.mark.asyncio
async def test_no_extend_while_metadata_is_short(tmp_path) -> None:
    # Codex (round 2, P1): the data-only extend must not spend the pass while
    # metadata is at risk.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85), meta_at=lambda h: 0.60 + 0.02 * h)
    snaps = _Snaps({PRE: AGED})
    run = _Run()
    out, _ = await _pass(cfg, snaps, _stranded(meta=84.0), run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_no_extend_when_metadata_is_at_its_reserve(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    snaps = _Snaps({PRE: AGED})
    run = _Run()
    out, _ = await _pass(cfg, snaps, _stranded(data=98.0, meta=95.0), run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_extend_at_the_reserve_too(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({LIFELINE: YOUNG}), _stranded(data=98.0), run=run)
    assert out == "extended" and run.extended


@pytest.mark.asyncio
async def test_a_failed_read_stamps_nothing_and_falls_through(tmp_path) -> None:
    # Codex (round 1): an unreadable extent size stamped a 24h cooldown and
    # then deleted recovery snapshots instead.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85))
    snaps = _Snaps({PRE: AGED})
    out, _ = await _pass(cfg, snaps, _stranded(), run=_Run(vgs_rc=5))
    assert out == f"deleted:{PRE}"
    assert "extend_backoff" not in _state(cfg)


@pytest.mark.asyncio
async def test_a_failed_lvextend_alerts_backs_off_and_falls_through(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85))
    snaps = _Snaps({PRE: AGED})
    out, d = await _pass(cfg, snaps, _stranded(), run=_Run(lvextend_rc=5))
    assert out == f"deleted:{PRE}"
    assert _titles(d) == [
        "Guardian could not extend the thin pool",
        "Guardian freed pool space early",
    ]
    assert "extend_backoff" in _state(cfg)
    run = _Run()
    await _pass(cfg, _Snaps({}), _stranded(), run=run, now=T0 + timedelta(hours=2))
    assert not run.extended  # backed off, not retried on every settle


@pytest.mark.asyncio
async def test_a_timed_out_lvextend_ends_the_pass(tmp_path) -> None:
    # Review: the extend probably finished; the pass must not also delete.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85))
    snaps = _Snaps({PRE: AGED})
    out, d = await _pass(cfg, snaps, _stranded(), run=_Slow())
    assert out == "extend_indeterminate" and snaps.deleted == []
    assert _titles(d) == ["Guardian extend outcome unknown"]
    assert _severities(d) == ["CRITICAL"]


@pytest.mark.asyncio
async def test_the_extend_is_replanned_right_before_the_mutation(tmp_path) -> None:
    # Review: VG free shrank between the decision and the mutation.
    cfg = _cfg(tmp_path)
    run = _Run()
    out, _ = await _pass(
        cfg, _Snaps({}), _stranded(),
        measures=[_stranded(), _stranded(vg_free_bytes=3 * _GB)], run=run,
    )
    assert out == "extended"
    assert _lvextend_argv(run)[4] == f"+{3 * _GB - 512 * _MB}b"


@pytest.mark.asyncio
async def test_a_profile_withdrawn_before_the_mutation_stops_it(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    run = _Run()
    out, _ = await _pass(
        cfg, _Snaps({}), _stranded(),
        measures=[_stranded(), _stranded(thinpool_profile=None)], run=run,
    )
    assert out == "extend_stopped" and not run.extended


@pytest.mark.asyncio
async def test_a_pool_that_moved_before_the_extend_is_not_extended(tmp_path) -> None:
    # Codex (round 2): re-read the complete pool identity before mutating.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.85))
    moved = _stranded(pool_name="other")
    run = _Run()
    out, _ = await _pass(
        cfg, _Snaps({PRE: AGED}), _stranded(), measures=[_stranded(), moved], run=run,
    )
    assert out == "extend_stopped" and not run.extended
    assert "last_action" not in _state(cfg)


@pytest.mark.asyncio
async def test_alert_only_never_extends(tmp_path) -> None:
    cfg = _cfg(tmp_path, relief_mode="alert_only")
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({}), _stranded(data=98.0), run=run)
    assert out == "alert_only" and not run.extended


@pytest.mark.asyncio
async def test_no_profile_no_extend(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    run = _Run()
    status = _lvm(98.0, vg_free_bytes=6 * _GB, thinpool_profile=None)
    out, _ = await _pass(cfg, _Snaps({LIFELINE: YOUNG}), status, run=run)
    assert not run.extended and out == f"deleted:{LIFELINE}"


# --- review findings on the early level ---------------------------------------------


@pytest.mark.asyncio
async def test_an_aged_lifeline_lvm_says_holds_little_is_kept_early(tmp_path) -> None:
    # Review: delete-first kept this lifeline because LVM measured it holding
    # little; early relief must not delete it anyway, for nothing.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({AGED_LIFELINE: AGED}, holds=None)
    out, _ = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "early_no_target" and snaps.deleted == []


@pytest.mark.asyncio
async def test_on_btrfs_age_alone_decides(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    btrfs = StoragePoolStatus(
        detected=True, pool_used_pct=80.0, pool_size_bytes=100 * _GB, pool_name="default",
    )
    _seed(cfg, data_at=_ramp_to(0.80), pool="default||")
    snaps = _Snaps({AGED_LIFELINE: AGED}, holds=None)
    out, _ = await _pass(cfg, snaps, btrfs)
    assert out == f"deleted:{AGED_LIFELINE}"


@pytest.mark.asyncio
async def test_taking_the_lifeline_early_is_critical(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))
    out, d = await _pass(cfg, _Snaps({AGED_LIFELINE: AGED}), _lvm(80.0))
    assert out == f"deleted:{AGED_LIFELINE}"
    assert _severities(d) == ["CRITICAL"]


@pytest.mark.asyncio
async def test_early_with_the_lifeline_held_for_recovery_says_so(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=_ramp_to(0.80))
    out, d = await _pass(cfg, _Snaps({AGED_LIFELINE: AGED}), _lvm(80.0), healthy=False)
    assert out == "early_no_target"
    assert "kept while the container is not healthy" in d.send.await_args.args[0].body


@pytest.mark.asyncio
async def test_alert_only_early_does_not_mute_the_reserve(tmp_path) -> None:
    cfg = _cfg(tmp_path, relief_mode="alert_only")
    _seed(cfg, data_at=_ramp_to(0.80))
    _, d1 = await _pass(cfg, _Snaps({}), _lvm(80.0))
    _, d2 = await _pass(cfg, _Snaps({}), _lvm(98.0), now=T0 + timedelta(minutes=10))
    assert _titles(d1) == ["Pool filling — relief is alert-only"]
    assert _titles(d2) == ["Pool short of space — relief is alert-only"]


@pytest.mark.asyncio
async def test_an_invalid_early_key_keeps_the_reserve(tmp_path) -> None:
    # Review: a typo in an early-level key used to put ALL of relief into
    # alert-only, the reserve and delete-first included.
    cfg = _cfg(tmp_path, history_sample_interval_s=300.0)
    _seed(cfg, data_at=_ramp_to(0.80))
    snaps = _Snaps({LIFELINE: YOUNG, PRE: AGED})
    out, d = await _pass(cfg, snaps, _lvm(80.0))
    assert out == "ok" and snaps.deleted == []
    assert _titles(d) == ["Early pool relief off"]
    out, _ = await _pass(cfg, snaps, _lvm(98.0), now=T0 + timedelta(minutes=10))
    assert out == f"deleted:{PRE}"


def test_history_sample_type_is_what_relief_writes() -> None:
    # The early tests seed the file by hand; keep them honest about its shape.
    assert set(PoolSample.__dataclass_fields__) == {
        "ts",
        "pool",
        "data_used",
        "data_size",
        "meta_used",
        "meta_size",
    }


def _ledger(cfg) -> list[dict]:
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    return json.loads(ProvisioningLedger(cfg.state_dir)._ledger.read_text())


@pytest.mark.asyncio
async def test_an_issued_extend_is_recorded_in_the_provisioning_ledger(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    await _pass(cfg, _Snaps({}), _stranded())
    (entry,) = _ledger(cfg)
    assert entry["action"] == "pool_extend" and entry["ok"] is True
    assert entry["requested"].startswith("vg0/IncusThinPool: grew")


@pytest.mark.asyncio
async def test_a_timed_out_extend_is_recorded_unverified(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    await _pass(cfg, _Snaps({}), _stranded(), run=_Slow())
    (entry,) = _ledger(cfg)
    assert (entry["action"], entry["ok"], entry["verified"]) == ("pool_extend", False, False)


@pytest.mark.asyncio
async def test_an_extend_stopped_before_the_mutation_is_not_recorded(tmp_path) -> None:
    from genesis.guardian.provisioning.ledger import ProvisioningLedger

    cfg = _cfg(tmp_path)
    await _pass(cfg, _Snaps({}), _stranded(), run=_Run(vgs_rc=5))
    assert not ProvisioningLedger(cfg.state_dir)._ledger.exists()


@pytest.mark.asyncio
async def test_alert_only_says_it_would_extend(tmp_path) -> None:
    cfg = _cfg(tmp_path, relief_mode="alert_only")
    run = _Run()
    out, d = await _pass(cfg, _Snaps({}), _stranded(), run=run)
    assert out == "alert_only" and not run.extended
    assert _titles(d) == ["Pool at LVM's autoextend threshold — relief is alert-only"]


@pytest.mark.asyncio
async def test_a_history_too_short_for_any_rate_is_invalid(tmp_path) -> None:
    cfg = _cfg(tmp_path, history_max_samples=10)
    _, d = await _pass(cfg, _Snaps({}), _lvm(50.0))
    assert _titles(d) == ["Early pool relief off"]


@pytest.mark.asyncio
async def test_an_unreadable_extent_size_is_retried_hourly_not_every_pass(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    await _pass(cfg, _Snaps({}), _stranded(), run=_Run(vgs_rc=5))
    run = _Run()
    await _pass(cfg, _Snaps({}), _stranded(), run=run, now=T0 + timedelta(minutes=10))
    assert run.calls == []
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({}), _stranded(), run=run, now=T0 + timedelta(hours=2))
    assert out == "extended"


@pytest.mark.asyncio
async def test_the_reserve_never_measures_lifeline_evidence(tmp_path) -> None:
    class _Counting(_Snaps):
        asked = 0

        async def lifeline_holds_space(self):
            type(self).asked += 1
            return self.holds

    cfg = _cfg(tmp_path)
    snaps = _Counting({AGED_LIFELINE: AGED})
    out, _ = await _pass(cfg, snaps, _lvm(98.0))
    assert out == f"deleted:{AGED_LIFELINE}" and _Counting.asked == 0


@pytest.mark.asyncio
async def test_the_extend_alert_reports_the_fresh_measurement(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    fresh = _stranded(data=86.5, vg_free_bytes=3 * _GB)
    _, d = await _pass(cfg, _Snaps({}), _stranded(), measures=[_stranded(), fresh])
    body = d.send.await_args.args[0].body
    assert "data 86.5%" in body and "VG free 3.0G" in body


# --- review round 3 ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_metadata_size_refuses_the_extend(tmp_path) -> None:
    # Codex (round 3, P1): an unreadable lv_metadata_size read as 0 and could
    # spend the headroom the metadata LV needs.
    cfg = _cfg(tmp_path)
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({PRE: AGED}), _stranded(data=98.0, metadata_size_bytes=None),
                         run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_a_vanished_extend_plan_under_pressure_still_relieves(tmp_path) -> None:
    # Codex (round 3, P1): the profile disappears while data is at 98%; the
    # extend no longer applies, but the pass must go on to free a snapshot.
    cfg = _cfg(tmp_path)
    run = _Run()
    snaps = _Snaps({PRE: AGED})
    gone = _stranded(data=98.0, thinpool_profile=None)
    out, _ = await _pass(cfg, snaps, _stranded(data=98.0),
                         measures=[_stranded(data=98.0), gone, gone], run=run)
    assert not run.extended and out == f"deleted:{PRE}"


def test_non_lvm_pool_identity_includes_its_source() -> None:
    # Codex (round 3, P2): every btrfs/dir pool keyed as "<name>||", so a pool
    # recreated on other storage shared the old one's history.
    from genesis.guardian.pool_relief import pool_key

    a = StoragePoolStatus(detected=True, pool_used_pct=50.0, pool_name="default",
                          pool_source="/dev/sdb1")
    b = StoragePoolStatus(detected=True, pool_used_pct=50.0, pool_name="default",
                          pool_source="/var/lib/incus/disks/default.img")
    assert pool_key(a) != pool_key(b)
    lvm = _lvm(50.0, pool_source="vg0")
    assert pool_key(lvm) == KEY  # LVM identity unchanged


# --- class audit before round 4 ------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unknown_metadata_pct_refuses_the_extend(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    run = _Run()
    out, _ = await _pass(cfg, _Snaps({PRE: AGED}), _stranded(data=98.0, meta=None), run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_fresh_early_pressure_found_by_the_extend_recheck_relieves_now(tmp_path) -> None:
    # The pass started with an extend only (no pressure); the re-measure shows
    # metadata growing short, so the extend no longer applies and early relief
    # acts on that fresh reading in the same pass.
    cfg = _cfg(tmp_path)
    _seed(cfg, data_at=lambda h: 0.85, meta_at=lambda h: 0.60 + 0.02 * h)
    run = _Run()
    snaps = _Snaps({PRE: AGED})
    calm = _stranded(meta=60.0)
    short = _stranded(meta=84.0)
    out, _ = await _pass(cfg, snaps, calm, measures=[calm, short, short], run=run)
    assert not run.extended and out == f"deleted:{PRE}"


@pytest.mark.asyncio
async def test_a_clock_stepped_back_keeps_the_extend_backoff(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    await _pass(cfg, _Snaps({}), _stranded(), run=_Run(lvextend_rc=5))
    run = _Run()
    await _pass(cfg, _Snaps({}), _stranded(), run=run, now=T0 - timedelta(hours=3))
    assert not run.extended
