"""storage-expand tests — command order, stop-on-failure, and the hard
never-``+100%FREE`` outage-cause regression guard."""

from __future__ import annotations

import pytest

import genesis.guardian.provisioning.expand as expand_mod
from genesis.guardian.config import GuardianConfig
from genesis.guardian.provisioning.expand import _assert_no_full_extend, expand_storage


class FakeRun:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.pvs_out = "/dev/sdb"
        self.lsblk_out = ""  # PKNAME empty → PV is the whole disk
        self.pvresize_rc = 0
        self.vg_free = "34359738368"  # 32 GiB
        self.seg_monitor = "monitored"

    async def __call__(self, *argv, timeout=60.0, stdin_data=None):
        self.calls.append((argv, stdin_data))
        a = list(argv)
        if a[:1] == ["lsblk"]:
            return 0, self.lsblk_out, ""
        if "pvs" in a:
            return 0, self.pvs_out, ""
        if "pvresize" in a:
            return self.pvresize_rc, "", ("" if self.pvresize_rc == 0 else "device busy")
        if "vgs" in a:
            return 0, self.vg_free, ""
        if "lvs" in a:
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
