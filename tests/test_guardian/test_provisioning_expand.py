"""storage-expand tests — command order, stop-on-failure, and the hard
never-``+100%FREE`` outage-cause regression guard."""

from __future__ import annotations

import pytest

import genesis.guardian.provisioning.expand as expand_mod
from genesis.guardian.config import GuardianConfig
from genesis.guardian.provisioning.expand import (
    _assert_no_full_extend,
    _resolve_thinpool_lv,
    expand_storage,
)


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.pvs_out = "/dev/sdb"
        self.lsblk_out = ""  # PKNAME empty → PV is the whole disk
        self.pvresize_rc = 0
        self.vg_free = "34359738368"  # 32 GiB
        self.seg_monitor = "monitored"
        # thin-pool LV name resolution knobs
        self.incus_thinpool_name = "IncusThinPool"  # incus lvm.thinpool_name; "" = unset
        self.lvs_lv_name = "IncusThinPool"  # `lvs -o lv_name -S segtype=thin-pool <vg>`
        # btrfs-on-LVM knobs
        self.findmnt_out = "/dev/mapper/vg0-genesis--btrfs--lv"
        self.lvs_backing_out = "  vg0 genesis-btrfs-lv"  # `lvs -o vg_name,lv_name <dev>`
        self.lvextend_rc = 0
        self.btrfs_resize_rc = 0
        # df --output=size --block-size=1 answers, consumed in order (before/after);
        # the last entry repeats once exhausted.
        self.df_sizes = ["322122547200", "330712481792"]  # 300 GiB → 308 GiB

    async def __call__(self, *argv, timeout=60.0, stdin_data=None):
        self.calls.append((argv, stdin_data))
        a = list(argv)
        if a[:1] == ["lsblk"]:
            return 0, self.lsblk_out, ""
        if a[:1] == ["findmnt"]:
            return (0, self.findmnt_out, "") if self.findmnt_out else (1, "", "not found")
        if a[:1] == ["df"]:
            size = self.df_sizes.pop(0) if len(self.df_sizes) > 1 else self.df_sizes[0]
            return 0, f"1B-blocks\n{size}\n", ""
        if a[:2] == ["incus", "storage"] and "lvm.thinpool_name" in a:
            return 0, self.incus_thinpool_name, ""
        if "pvs" in a:
            return 0, self.pvs_out, ""
        if "pvresize" in a:
            return self.pvresize_rc, "", ("" if self.pvresize_rc == 0 else "device busy")
        if "vgs" in a:
            return 0, self.vg_free, ""
        if "lvextend" in a:
            return self.lvextend_rc, "", ("" if self.lvextend_rc == 0 else "insufficient free space")
        if "btrfs" in a:
            return self.btrfs_resize_rc, "", ("" if self.btrfs_resize_rc == 0 else "resize failed")
        if "lvs" in a and "vg_name,lv_name" in a:  # btrfs backing-LV resolution
            return 0, self.lvs_backing_out, ""
        if "lvs" in a and "lv_name" in a:  # thin-pool name resolution
            return 0, self.lvs_lv_name, ""
        if "lvs" in a:  # seg_monitor check
            return 0, self.seg_monitor, ""
        if "tee" in a or "lvchange" in a:
            return 0, "", ""
        return 0, "", ""

    def cmd_index(self, needle: str) -> int:
        for i, (argv, _) in enumerate(self.calls):
            if needle in " ".join(argv):
                return i
        return -1


@pytest.fixture
def _lvm(monkeypatch):
    async def _pool(_cfg):
        return "default"

    async def _vg(_name):
        return "vg0"

    monkeypatch.setattr(expand_mod, "_detect_pool_name", _pool)
    monkeypatch.setattr(expand_mod, "_lvm_source", _vg)


async def test_happy_path_order_and_ok(_lvm):
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is True
    assert res["vg_free_bytes"] == 34359738368
    assert res["monitored"] is True
    # pvs before pvresize before the autoextend profile write.
    assert fake.cmd_index("pvs") < fake.cmd_index("pvresize")
    assert fake.cmd_index("pvresize") < fake.cmd_index("thinpool.profile")


async def test_profile_targets_real_thinpool_lv_not_pool_name(_lvm):
    """Regression: the pool is 'default' but the LV is 'IncusThinPool' — the
    autoextend profile + monitoring must target vg0/IncusThinPool, NOT vg0/default
    (the bug that left autoextend unarmed after a real grow)."""
    fake = FakeRun()  # incus_thinpool_name = "IncusThinPool"
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["thinpool"] == "IncusThinPool"
    lvchange = [argv for argv, _ in fake.calls if "lvchange" in argv]
    assert lvchange, "expected lvchange calls"
    for argv in lvchange:
        assert "vg0/IncusThinPool" in argv
        assert "vg0/default" not in argv


async def test_thinpool_falls_back_to_lvs_when_incus_silent(_lvm):
    """No incus lvm.thinpool_name → resolve via the single thin-pool LV in the VG."""
    fake = FakeRun()
    fake.incus_thinpool_name = ""  # incus key unset
    fake.lvs_lv_name = "IncusThinPool"
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["thinpool"] == "IncusThinPool"


async def test_thinpool_last_resort_is_pool_name(_lvm):
    """Both sources silent → fall back to the pool name (never worse than old)."""
    fake = FakeRun()
    fake.incus_thinpool_name = ""
    fake.lvs_lv_name = ""  # no single unambiguous LV
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["thinpool"] == "default"


async def test_resolve_thinpool_ambiguous_multiple_pools_falls_back():
    """Two thin pools in the VG is ambiguous → don't guess; fall back to pool_name."""
    async def _run(*argv, timeout=60.0, stdin_data=None):
        if list(argv)[:2] == ["incus", "storage"]:
            return 0, "", ""  # incus silent
        if "lvs" in argv:
            return 0, "poolA\n  poolB", ""  # two thin pools
        return 0, "", ""

    assert await _resolve_thinpool_lv("default", "vg0", _run) == "default"


async def test_never_issues_100pct_free(_lvm):
    fake = FakeRun()
    await expand_storage(GuardianConfig(), run=fake)
    for argv, _ in fake.calls:
        assert "100%FREE" not in " ".join(argv)
        assert "lvextend" not in argv  # no data-LV extend at all


def test_assert_no_full_extend_raises():
    with pytest.raises(RuntimeError):
        _assert_no_full_extend(("lvextend", "-l", "+100%FREE", "vg0/data"))
    # a safe command passes
    _assert_no_full_extend(("pvresize", "/dev/sdb"))


async def test_run_wrapper_blocks_full_extend():
    """Every mutating command in expand_storage goes through _run, which refuses
    a +100%FREE argv at runtime — so no future step can re-create the outage."""
    async def _noop(*_a, **_k):
        return 0, "", ""

    with pytest.raises(RuntimeError):
        await expand_mod._run(_noop, "sudo", "-n", "lvextend", "-l", "+100%FREE", "vg0/data")
    # a benign command passes through the wrapper
    rc, _o, _e = await expand_mod._run(_noop, "sudo", "-n", "pvresize", "/dev/sdb")
    assert rc == 0


async def test_pvresize_failure_stops(_lvm):
    fake = FakeRun()
    fake.pvresize_rc = 1
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is False
    assert "pvresize" in res["error"]
    # stopped before writing the autoextend profile
    assert fake.cmd_index("thinpool.profile") == -1


async def test_unsupported_pool_driver_refused(monkeypatch):
    """Neither LVM nor btrfs (e.g. dir) → refuse without touching anything."""
    async def _pool(_cfg):
        return "default"

    async def _vg(_name):
        return None  # not LVM

    async def _driver(_name):
        return "dir"

    monkeypatch.setattr(expand_mod, "_detect_pool_name", _pool)
    monkeypatch.setattr(expand_mod, "_lvm_source", _vg)
    monkeypatch.setattr(expand_mod, "_detect_pool_driver", _driver)
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is False and "neither LVM-thin nor btrfs-on-LVM" in res["error"]
    assert fake.calls == []  # no storage commands attempted


async def test_vg_free_zero_is_not_ok(_lvm):
    fake = FakeRun()
    fake.vg_free = "0"
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is False
    assert "0 free extents" in res["error"]


# ── btrfs-on-LVM substrate ─────────────────────────────────────────────────

_GIB = 1024 ** 3


@pytest.fixture
def _btrfs(monkeypatch):
    async def _pool(_cfg):
        return "genesis-btrfs"

    async def _vg(_name):
        return None  # not an LVM pool

    async def _driver(_name):
        return "btrfs"

    monkeypatch.setattr(expand_mod, "_detect_pool_name", _pool)
    monkeypatch.setattr(expand_mod, "_lvm_source", _vg)
    monkeypatch.setattr(expand_mod, "_detect_pool_driver", _driver)


_MOUNT = "/var/lib/incus/storage-pools/genesis-btrfs"


async def test_btrfs_happy_path_order_and_ok(_btrfs):
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=8)
    assert res["ok"] is True
    assert res["driver"] == "btrfs"
    assert res["vg"] == "vg0" and res["lv"] == "genesis-btrfs-lv"
    assert res["fs_size_bytes"] == 330712481792  # the post-resize re-read
    # resolve → pvresize → lvextend → btrfs resize, strictly in that order.
    assert fake.cmd_index("findmnt") < fake.cmd_index("pvresize")
    assert fake.cmd_index("pvresize") < fake.cmd_index("lvextend")
    assert fake.cmd_index("lvextend") < fake.cmd_index("resize max")
    # the online resize targets the MOUNTPOINT, not the device
    resize = next(argv for argv, _ in fake.calls if "resize" in argv)
    assert _MOUNT in resize
    # explicit byte count: 8 GiB requested < 32 GiB free → exactly 8 GiB
    lvextend = next(argv for argv, _ in fake.calls if "lvextend" in argv)
    assert f"+{8 * _GIB}b" in lvextend
    assert "vg0/genesis-btrfs-lv" in lvextend
    # never any thin-pool machinery on the btrfs path
    joined = " ".join(" ".join(argv) for argv, _ in fake.calls)
    assert "thinpool.profile" not in joined
    assert "lvchange" not in joined
    assert "100%FREE" not in joined


async def test_btrfs_standalone_absorbs_vg_free(_btrfs):
    """No add_gib (standalone storage-expand retry) → absorb the VG free space."""
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is True
    lvextend = next(argv for argv, _ in fake.calls if "lvextend" in argv)
    assert f"+{32 * _GIB}b" in lvextend  # vg_free = 32 GiB


async def test_btrfs_clamps_extend_to_vg_free(_btrfs):
    """Requested 64G but only 32G of extents exist (PV metadata / extent
    rounding eats a sliver of every grow) → clamp, don't fail."""
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=64)
    assert res["ok"] is True
    lvextend = next(argv for argv, _ in fake.calls if "lvextend" in argv)
    assert f"+{32 * _GIB}b" in lvextend


async def test_btrfs_no_vg_free_refuses_extend(_btrfs):
    fake = FakeRun()
    fake.vg_free = "0"
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=8)
    assert res["ok"] is False
    assert "no free space to absorb" in res["error"]
    assert fake.cmd_index("lvextend") == -1  # never attempted


async def test_btrfs_lvextend_failure_stops(_btrfs):
    fake = FakeRun()
    fake.lvextend_rc = 1
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=8)
    assert res["ok"] is False
    assert "lvextend" in res["error"]
    assert fake.cmd_index("resize max") == -1  # stopped before the fs resize


async def test_btrfs_unresolved_backing_refuses(_btrfs):
    fake = FakeRun()
    fake.findmnt_out = ""  # findmnt fails
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=8)
    assert res["ok"] is False
    assert "could not resolve" in res["error"]
    assert fake.cmd_index("pvresize") == -1  # nothing mutated


async def test_btrfs_fs_did_not_grow_is_not_ok(_btrfs):
    """Commands all exit 0 but the re-read shows no growth → honest failure."""
    fake = FakeRun()
    fake.df_sizes = ["322122547200", "322122547200"]  # identical before/after
    res = await expand_storage(GuardianConfig(), run=fake, add_gib=8)
    assert res["ok"] is False
    assert "did not grow" in res["error"]
