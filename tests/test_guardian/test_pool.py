"""Tests for host storage-pool monitoring (pure logic)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.guardian import pool as pool_mod
from genesis.guardian.config import StoragePoolConfig
from genesis.guardian.pool import (
    TIER_CRIT,
    TIER_HIGH,
    TIER_OK,
    TIER_WARN,
    PoolBackend,
    StoragePoolStatus,
    ThinPoolReport,
    decide_alert,
    incus_container_lv_name,
    incus_snapshot_lv_name,
    measure_storage_pool,
    parse_lvs_report,
    parse_pool_backend,
    snapshot_only_bytes,
    worst_tier,
)


def _status(data=None, meta=None) -> StoragePoolStatus:
    return StoragePoolStatus(detected=True, data_pct=data, metadata_pct=meta)


class TestWorstTier:
    cfg = StoragePoolConfig()

    def test_ok_when_both_low(self):
        assert worst_tier(_status(50, 40), self.cfg) == TIER_OK

    def test_data_tiers(self):
        assert worst_tier(_status(76, 0), self.cfg) == TIER_WARN
        assert worst_tier(_status(86, 0), self.cfg) == TIER_HIGH
        assert worst_tier(_status(93, 0), self.cfg) == TIER_CRIT

    def test_metadata_alerts_earlier_than_data(self):
        # metadata 72% is HIGH (>=70) while the same 72% data is only WARN.
        assert worst_tier(_status(0, 72), self.cfg) == TIER_HIGH
        assert worst_tier(_status(72, 0), self.cfg) == TIER_OK  # 72 < data_warn 75

    def test_worst_of_the_two_wins(self):
        # data WARN (76) + metadata CRIT (81) → CRIT
        assert worst_tier(_status(76, 81), self.cfg) == TIER_CRIT

    def test_none_percents_are_ok(self):
        assert worst_tier(_status(None, None), self.cfg) == TIER_OK

    def test_pool_used_is_the_fallback_signal(self):
        # Non-LVM backend (btrfs): only pool_used_pct carries signal — it must
        # tier, else a btrfs pool filling to 100% stays OK forever.
        def _btrfs(used):
            return StoragePoolStatus(detected=True, pool_used_pct=used)

        assert worst_tier(_btrfs(50.0), self.cfg) == TIER_OK
        assert worst_tier(_btrfs(76.0), self.cfg) == TIER_WARN
        assert worst_tier(_btrfs(86.0), self.cfg) == TIER_HIGH
        assert worst_tier(_btrfs(93.0), self.cfg) == TIER_CRIT

    def test_pool_used_ignored_when_lvm_percents_present(self):
        # LVM-thin: data%/metadata% stay the sole authority. Even if a used%
        # were present, it must NOT change long-standing behavior.
        s = StoragePoolStatus(
            detected=True, data_pct=50.0, metadata_pct=40.0, pool_used_pct=95.0,
        )
        assert worst_tier(s, self.cfg) == TIER_OK

    def test_pool_used_applies_when_only_one_lvm_percent_missing_is_not_fallback(self):
        # Even ONE present LVM percent keeps the LVM authority (partial lvs
        # output) — fallback engages only when BOTH are absent.
        s = StoragePoolStatus(detected=True, data_pct=50.0, pool_used_pct=95.0)
        assert worst_tier(s, self.cfg) == TIER_OK


def _lvs_json(*rows: dict) -> str:
    """The shape a real `lvs --reportformat json` prints (measured, LVM 2.03)."""
    return json.dumps({"report": [{"lv": list(rows)}]}, indent=2)


def _row(data="73.57", meta="43.51", size="73903636480", name="IncusThinPool") -> dict:
    return {"data_percent": data, "metadata_percent": meta, "lv_size": size, "lv_name": name}


class TestParseLvsReport:
    """Fields are bound by NAME: a blank percent (an inactive LV) must read as
    no signal, never shift the byte size into the data% column."""

    def test_single_row(self):
        assert parse_lvs_report(_lvs_json(_row())) == ThinPoolReport(
            data_pct=73.57, metadata_pct=43.51,
            size_bytes=73_903_636_480, lv_name="IncusThinPool",
        )

    def test_blank_percents_are_none_not_shifted(self):
        rep = parse_lvs_report(_lvs_json(_row(data="", meta="")))
        assert rep.data_pct is None and rep.metadata_pct is None
        assert rep.size_bytes == 73_903_636_480 and rep.lv_name == "IncusThinPool"

    def test_blank_percents_tier_ok_not_crit(self):
        rep = parse_lvs_report(_lvs_json(_row(data="", meta="")))
        status = StoragePoolStatus(
            detected=True, data_pct=rep.data_pct, metadata_pct=rep.metadata_pct,
        )
        assert worst_tier(status, StoragePoolConfig()) == TIER_OK

    def test_two_thin_pools_keep_percents_but_no_identity(self):
        rep = parse_lvs_report(_lvs_json(_row(name="poolA"), _row("10.00", "5.00", "1", "poolB")))
        assert (rep.data_pct, rep.metadata_pct) == (73.57, 43.51)
        assert rep.size_bytes is None and rep.lv_name is None

    @pytest.mark.parametrize("out", [
        "", "\n", "  73.57  43.51  73903636480  IncusThinPool\n", "{}", '{"report": []}',
        '{"report": [{"lv": []}]}', '{"report": [{"lv": ["x"]}]}', "[1, 2]",
    ])
    def test_unexpected_shape_is_no_signal(self, out):
        assert parse_lvs_report(out) == ThinPoolReport()

    def test_garbage_values_are_none(self):
        rep = parse_lvs_report(_lvs_json(_row(data="n/a", meta="nan", size="73.9g", name="  ")))
        assert rep == ThinPoolReport()


class TestMeasureLvmPool:
    """End-to-end of the LVM branch: identity fields reach the status, and a
    VG with two thin pools leaves the LV unnamed (relief then never acts)."""

    @staticmethod
    def _patch(monkeypatch, lvs_out: str, calls: list | None = None, thinpool="IncusThinPool"):
        async def _detect(_config):
            return "default"

        async def _backend(_name):
            return PoolBackend(driver="lvm", source="vg0", vg_name="vg0", thinpool_lv=thinpool)

        async def _run(*a, **k):
            if calls is not None:
                calls.append(a)
            if "lvs" in a:
                assert a[a.index("--reportformat") + 1] == "json"
                return 0, lvs_out, ""
            if "vgs" in a:
                return 0, "  21474836480\n", ""
            return 1, "", "unexpected"

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_backend", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

    @pytest.mark.asyncio
    async def test_identity_reaches_status(self, monkeypatch):
        calls: list[tuple] = []
        self._patch(monkeypatch, _lvs_json(_row()), calls)
        status = await measure_storage_pool(object())
        assert status.detected is True
        assert (status.data_pct, status.metadata_pct) == (73.57, 43.51)
        assert status.pool_size_bytes == 73_903_636_480
        assert (status.pool_name, status.vg_name, status.thinpool_lv) == (
            "default", "vg0", "IncusThinPool",
        )
        assert status.vg_free_bytes == 21_474_836_480
        lvs = next(c for c in calls if "lvs" in c)
        # Bytes, not lvm's default human units — the size is used as a number.
        assert "--units" in lvs and lvs[lvs.index("--units") + 1] == "b"

    @pytest.mark.asyncio
    async def test_declared_thin_pool_is_picked_among_several(self, monkeypatch):
        """Audit: identity is what incus DECLARES (lvm.thinpool_name), not a
        count — a VG with several thin pools measures the right one."""
        self._patch(
            monkeypatch,
            _lvs_json(_row(name="poolA"), _row("10.00", "5.00", "1073741824", "poolB")),
            thinpool="poolB",
        )
        status = await measure_storage_pool(object())
        assert (status.data_pct, status.metadata_pct) == (10.0, 5.0)
        assert status.thinpool_lv == "poolB" and status.pool_size_bytes == 1073741824

    @pytest.mark.asyncio
    async def test_declared_thin_pool_missing_is_no_signal(self, monkeypatch):
        self._patch(
            monkeypatch, _lvs_json(_row(name="poolA"), _row("10.00", "5.00", "1", "poolB")),
            thinpool="IncusThinPool",
        )
        status = await measure_storage_pool(object())
        assert status.thinpool_lv is None and status.data_pct is None and status.metadata_pct is None


class TestParsePoolBackend:
    """`incus storage show` as incus 6.0.0 prints it (measured): source lives
    under config."""

    SHOW = (
        "config:\n  lvm.thinpool_name: IncusThinPool\n  lvm.vg.force_reuse: \"true\"\n"
        "  lvm.vg_name: vg0\n  source: vg0\n  volatile.initial_source: vg0\n"
        "description: \"\"\nname: default\ndriver: lvm\nused_by:\n- /1.0/instances/genesis\n"
        "status: Created\nlocations:\n- none\n"
    )

    def test_measured_host_output(self):
        b = parse_pool_backend(self.SHOW)
        assert b == PoolBackend(
            driver="lvm", source="vg0", vg_name="vg0", thinpool_lv="IncusThinPool", thin=True,
        )

    def test_thick_lvm(self):
        b = parse_pool_backend("driver: lvm\nconfig:\n  source: vg0\n  lvm.use_thinpool: \"false\"\n")
        assert b.thin is False

    def test_loop_file_source_is_not_a_vg(self):
        b = parse_pool_backend("driver: lvm\nconfig:\n  source: /var/lib/incus/disks/p.img\n")
        assert b.vg_name is None

    def test_description_cannot_inject_source(self):
        out = ("driver: lvm\ndescription: |\n  source: evil\nconfig:\n  lvm.vg_name: vg0\n")
        b = parse_pool_backend(out)
        assert b.vg_name == "vg0" and b.source is None

    @pytest.mark.parametrize("out", ["", "::", "- a\n- b\n", "config: {}\n"])
    def test_garbage_is_none(self, out):
        assert parse_pool_backend(out) is None


class TestUnsupportedBackends:
    @pytest.mark.parametrize("backend", [
        PoolBackend(driver="zfs", source="tank/incus"),
        PoolBackend(driver="ceph", source="pool"),
        PoolBackend(driver="lvm", source="vg0", vg_name="vg0", thin=False),
        PoolBackend(driver="lvm", source="/x.img", vg_name=None),
    ])
    @pytest.mark.asyncio
    async def test_not_measured_and_never_df(self, monkeypatch, backend):
        calls: list = []

        async def _detect(_config):
            return "default"

        async def _backend(_name):
            return backend

        async def _run(*a, **k):
            calls.append(a)
            return 0, "      Avail    1B-blocks\n1 100\n", ""

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_backend", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        status = await measure_storage_pool(object())
        assert status.detected is False
        assert not any(c and c[0] == "df" for c in calls)


class TestDecideAlert:
    now = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)

    def test_tier_increase_alerts(self):
        d = decide_alert(TIER_WARN, TIER_OK, None, self.now, 6.0)
        assert d.should_alert and not d.is_resolution

    def test_jump_multiple_tiers_alerts(self):
        d = decide_alert(TIER_CRIT, TIER_OK, None, self.now, 6.0)
        assert d.should_alert and d.tier == TIER_CRIT

    def test_recovery_to_ok_sends_resolution(self):
        d = decide_alert(TIER_OK, TIER_HIGH, self.now - timedelta(hours=1), self.now, 6.0)
        assert d.should_alert and d.is_resolution

    def test_ok_to_ok_silent(self):
        assert not decide_alert(TIER_OK, TIER_OK, None, self.now, 6.0).should_alert

    def test_sustained_realerts_after_interval(self):
        last = self.now - timedelta(hours=7)
        assert decide_alert(TIER_HIGH, TIER_HIGH, last, self.now, 6.0).should_alert

    def test_sustained_silent_within_interval(self):
        last = self.now - timedelta(hours=2)
        assert not decide_alert(TIER_HIGH, TIER_HIGH, last, self.now, 6.0).should_alert

    def test_tier_decrease_still_nonok_is_silent(self):
        # crit → high (still bad) does not spam; caller records new tier.
        assert not decide_alert(TIER_HIGH, TIER_CRIT, self.now, self.now, 6.0).should_alert

    def test_sustained_with_no_prior_time_alerts(self):
        # Defensive: missing last_alert_at shouldn't suppress a live problem.
        assert decide_alert(TIER_WARN, TIER_WARN, None, self.now, 6.0).should_alert


def _df(used: int, size: int, reserved: int = 0) -> str:
    # `df -B1 --output=avail,size` form: header row + one data row. Reserved
    # blocks are neither used nor available.
    return f"      Avail    1B-blocks\n{size - used - reserved} {size}\n"


class TestPoolUsedViaDf:
    """The non-LVM used% signal. `incus storage info` has no machine-readable
    space for an uncapped btrfs-on-LV pool (its `--format json` flag doesn't
    even exist), so the mount is read via df."""

    @pytest.mark.asyncio
    async def test_computes_used_pct(self, monkeypatch):
        async def _run(*a, **k):
            return 0, _df(48_318_382_080, 322_122_547_200), ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        pct = await pool_mod._pool_used_pct_via_df("/mnt/pool")
        assert pct == pytest.approx(15.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_rc_nonzero_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 1, "", "df: no such file or directory"

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/nope") is None

    @pytest.mark.asyncio
    async def test_single_line_output_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 0, "only a header\n", ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None

    @pytest.mark.asyncio
    async def test_nonnumeric_row_is_none(self, monkeypatch):
        async def _run(*a, **k):
            return 0, "Used 1B-blocks\nfoo bar\n", ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None

    @pytest.mark.asyncio
    async def test_zero_size_is_none(self, monkeypatch):
        # Never divide by zero — a 0-size mount yields no signal, not a crash.
        async def _run(*a, **k):
            return 0, _df(0, 0), ""

        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        assert await pool_mod._pool_used_pct_via_df("/mnt/pool") is None


class TestMeasureNonLvmPool:
    """End-to-end of the non-LVM branch of measure_storage_pool: a btrfs pool
    must surface a real pool_used_pct (from df) so worst_tier can tier it."""

    @pytest.mark.asyncio
    async def test_btrfs_pool_populates_used_pct(self, monkeypatch):
        async def _detect(_config):
            return "genesis-btrfs"

        async def _backend(_name):
            return PoolBackend(driver="btrfs", source="/dev/vg/btrfs")  # non-LVM backend

        async def _run(*a, **k):
            return 0, _df(48_318_382_080, 322_122_547_200), ""

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_backend", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

        status = await measure_storage_pool(object())
        assert status.detected is True
        assert status.data_pct is None and status.metadata_pct is None
        assert status.pool_used_pct == pytest.approx(15.0, abs=0.1)
        # The whole point: a measured btrfs pool now tiers.
        assert worst_tier(status, StoragePoolConfig()) == TIER_OK

    @pytest.mark.asyncio
    async def test_btrfs_df_failure_is_not_detected(self, monkeypatch):
        async def _detect(_config):
            return "genesis-btrfs"

        async def _backend(_name):
            return PoolBackend(driver="btrfs", source="/dev/vg/btrfs")

        async def _run(*a, **k):
            return 1, "", "df failed"

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_pool_backend", _backend)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)

        status = await measure_storage_pool(object())
        assert status.detected is False
        assert status.pool_used_pct is None


class TestBackendUnknown:
    """A failed backend lookup must read as UNDETECTED, never as "not LVM":
    the df fallback would then measure the host filesystem, not the pool."""

    @pytest.mark.asyncio
    async def test_storage_show_failure_is_not_detected(self, monkeypatch):
        calls: list[tuple] = []

        async def _detect(_config):
            return "default"

        async def _run(*a, **k):
            calls.append(a)
            if a[:3] == ("incus", "storage", "show"):
                return 1, "", "incus: daemon unreachable"
            return 0, _df(90, 100), ""  # would look 90% full if it were read

        monkeypatch.setattr(pool_mod, "_detect_pool_name", _detect)
        monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
        status = await measure_storage_pool(object())
        assert status.detected is False
        assert not any(c and c[0] == "df" for c in calls)


def _thin(*rows: dict) -> str:
    return json.dumps({"report": [{"lv": list(rows)}]})


def _tlv(name, pct, size, pool="IncusThinPool", segtype="thin"):
    return {
        "lv_name": name, "pool_lv": pool, "segtype": segtype,
        "data_percent": pct, "lv_size": str(size),
    }


def _pool_row(pct, size, name="IncusThinPool"):
    return _tlv(name, pct, size, pool="", segtype="thin-pool")


class TestSnapshotOnlyBytes:
    """The delete-first evidence: pool used minus what live volumes map, from
    ONE lvs read, is a LOWER bound on what the snapshots alone hold."""

    SNAP = incus_snapshot_lv_name("genesis", "guardian-20260926-162814-healthy")
    CT = "containers_genesis"
    G = 1024**3

    def _held(self, *rows, snaps=None):
        return snapshot_only_bytes(_thin(*rows), "IncusThinPool", self.CT, snaps or {self.SNAP})

    def test_lv_names_match_the_measured_incus_layout(self):
        assert self.SNAP == "containers_genesis-guardian--20260926--162814--healthy"
        assert incus_container_lv_name("my-box") == "containers_my--box"

    def test_measured_host_numbers(self):
        # One real read: pool 76.04% of 73903636480; container 74.82% of
        # 70002933760; a 2 GiB custom volume at 17.85%; the snapshot blank.
        held = self._held(
            _pool_row("76.04", 73903636480),
            _tlv(self.CT, "74.82", 70002933760),
            _tlv(self.SNAP, "", 60003713024),
            _tlv("custom_default_genesis--cc--tmp", "17.85", 2147483648),
            _tlv("other_pool_volume", "99.00", 10**12, pool="OtherPool"),
        )
        assert held == pytest.approx(0.7604 * 73903636480 - 0.7482 * 70002933760
                                     - 0.1785 * 2147483648)
        assert 3.0e9 < held < 3.5e9

    def test_new_live_data_is_not_snapshot_held(self):
        held = self._held(
            _pool_row("86.00", 100 * self.G), _tlv(self.CT, "86.00", 100 * self.G),
            _tlv(self.SNAP, "", 1),
        )
        assert held == pytest.approx(0)

    @pytest.mark.parametrize("rows", [
        (),  # empty report
        (_pool_row("90.00", 100 * 1024**3),),  # no container row
        (_pool_row("90.00", 100 * 1024**3), _tlv("containers_genesis", "", 1)),  # blank container
        (_tlv("containers_genesis", "10.00", 100 * 1024**3),),  # no pool row
        (_pool_row("", 100 * 1024**3), _tlv("containers_genesis", "10.00", 100 * 1024**3)),
    ])
    def test_incomplete_report_is_no_evidence(self, rows):
        """Internal review: an empty or anchor-less report used to return the
        WHOLE pool as snapshot-held."""
        assert self._held(*rows) is None

    def test_unattributable_inactive_volume_is_no_evidence(self):
        held = self._held(
            _pool_row("90.00", 100 * self.G),
            _tlv(self.CT, "50.00", 100 * self.G),
            _tlv(self.SNAP, "", 1),
            _tlv("containers_stopped--box", "", 50 * self.G),  # a stopped container
        )
        assert held is None

    @pytest.mark.parametrize("out", ["", "{}", "  12.0  x\n", '{"report": [{"lv": ["x"]}]}'])
    def test_unexpected_shape_is_no_evidence(self, out):
        assert snapshot_only_bytes(out, "IncusThinPool", self.CT, set()) is None


@pytest.mark.asyncio
async def test_reserved_blocks_count_as_unavailable(monkeypatch):
    """Review round 2: ext4 reserved blocks are neither used nor available, so
    used/size read 95% on a filesystem with nothing left to write to."""
    async def _run(*a, **k):
        assert "--output=avail,size" in a
        return 0, _df(95 * 1024**3, 100 * 1024**3, reserved=5 * 1024**3), ""

    monkeypatch.setattr(pool_mod, "_run_subprocess", _run)
    pct = await pool_mod._pool_used_pct_via_df("/mnt/pool")
    assert pct == pytest.approx(100.0)
