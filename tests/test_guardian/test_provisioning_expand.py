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

    async def __call__(self, *argv, timeout=60.0, stdin_data=None):
        self.calls.append((argv, stdin_data))
        a = list(argv)
        if a[:1] == ["lsblk"]:
            return 0, self.lsblk_out, ""
        if a[:2] == ["incus", "storage"] and "lvm.thinpool_name" in a:
            return 0, self.incus_thinpool_name, ""
        if "pvs" in a:
            return 0, self.pvs_out, ""
        if "pvresize" in a:
            return self.pvresize_rc, "", ("" if self.pvresize_rc == 0 else "device busy")
        if "vgs" in a:
            return 0, self.vg_free, ""
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


async def test_non_lvm_pool_refused(monkeypatch):
    async def _pool(_cfg):
        return "default"

    async def _vg(_name):
        return None  # not LVM

    monkeypatch.setattr(expand_mod, "_detect_pool_name", _pool)
    monkeypatch.setattr(expand_mod, "_lvm_source", _vg)
    fake = FakeRun()
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is False and "not LVM-thin" in res["error"]
    assert fake.calls == []  # no LVM commands attempted


async def test_vg_free_zero_is_not_ok(_lvm):
    fake = FakeRun()
    fake.vg_free = "0"
    res = await expand_storage(GuardianConfig(), run=fake)
    assert res["ok"] is False
    assert "0 free extents" in res["error"]
