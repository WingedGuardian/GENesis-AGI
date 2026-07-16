"""Tests for host-side container capacity grows (PR-C, guardian/grow_capacity.py).

Covers grow_root (grow-only reject, pool-headroom refusal, success+verify) and
set_container_limits (grow-only, host-cap refusal, cpu range, live set), driving
a fake ``run`` (canned incus responses) so no real host is touched.
"""

from __future__ import annotations

from unittest.mock import patch

from genesis.guardian import grow_capacity
from genesis.guardian.config import GuardianConfig
from genesis.guardian.grow_capacity import (
    _parse_incus_size,
    grow_root,
    set_container_limits,
)
from genesis.guardian.pool import StoragePoolStatus


class _FakeRun:
    """Canned incus responses keyed by a substring of the joined argv.

    ``size_after_set`` lets the 'get root size' read return the NEW size only
    once a 'device set' has been issued (models incus applying the resize).
    """

    def __init__(self, responses: dict[str, tuple[int, str, str]], *, size_after_set=None):
        self.responses = responses
        self.size_after_set = size_after_set
        self.set_done = False
        self.calls: list[str] = []

    async def __call__(self, *argv, timeout=None, stdin_data=None):
        joined = " ".join(argv)
        self.calls.append(joined)
        if "device set" in joined:
            self.set_done = True
        if "config set" in joined:
            self.set_done = True
        if (
            "device get" in joined
            and "root size" in joined
            and self.set_done
            and self.size_after_set
        ):
            return (0, self.size_after_set, "")
        for key, resp in self.responses.items():
            if key in joined:
                return resp
        return (1, "", f"unmocked: {joined}")


def _cfg() -> GuardianConfig:
    return GuardianConfig()


def _healthy_pool():
    return StoragePoolStatus(detected=True, data_pct=50.0, metadata_pct=30.0)


def _full_pool():
    return StoragePoolStatus(detected=True, data_pct=90.0, metadata_pct=40.0)


# ── size parsing ────────────────────────────────────────────────────────────


def test_parse_incus_size():
    assert _parse_incus_size("30GB") == 30 * 1000**3
    assert _parse_incus_size("16GiB") == 16 * 1024**3
    assert _parse_incus_size("512MiB") == 512 * 1024**2
    assert _parse_incus_size(str(1024**3)) == 1024**3  # bare bytes
    assert _parse_incus_size("") is None
    assert _parse_incus_size("garbage") is None


# ── grow_root ─────────────────────────────────────────────────────────────


async def test_grow_root_rejects_shrink():
    run = _FakeRun({"device get": (0, "30GB", "")})
    with patch.object(grow_capacity, "measure_storage_pool", return_value=_healthy_pool()):
        res = await grow_root(_cfg(), 20, run=run)  # 20GB < 30GB
    assert res["ok"] is False
    assert "grow-only" in res["error"]
    assert not run.set_done  # never mutated


async def test_grow_root_refuses_full_pool():
    run = _FakeRun({"device get": (0, "30GB", "")})
    with patch.object(grow_capacity, "measure_storage_pool", return_value=_full_pool()):
        res = await grow_root(_cfg(), 40, run=run)
    assert res["ok"] is False
    assert "near-full" in res["error"] or "tier" in res["error"]
    assert not run.set_done


async def test_grow_root_success_and_verify():
    run = _FakeRun(
        {"device get": (0, "30GB", ""), "device set": (0, "", "")}, size_after_set="40GB"
    )
    with patch.object(grow_capacity, "measure_storage_pool", return_value=_healthy_pool()):
        res = await grow_root(_cfg(), 40, run=run)
    assert res["ok"] is True
    assert res["verified"] is True
    assert res["new_size_gb"] == 40
    assert any("device set" in c and "size=40GB" in c for c in run.calls)


async def test_grow_root_read_failure_is_clean_json():
    run = _FakeRun({"device get": (1, "", "no such container")})
    res = await grow_root(_cfg(), 40, run=run)
    assert res["ok"] is False
    assert "cannot read current root size" in res["error"]


# ── set_container_limits ────────────────────────────────────────────────────


async def test_set_limits_rejects_shrink():
    run = _FakeRun({"config get": (0, "16GiB", "")})
    # host MemTotal 21GiB
    with patch.object(grow_capacity, "_read_meminfo", return_value={"MemTotal": 21 * 1024 * 1024}):
        res = await set_container_limits(_cfg(), 8192, None, run=run)  # 8GiB < 16GiB
    assert res["ok"] is False
    assert "grow-only" in res["error"]
    assert not run.set_done


async def test_set_limits_refuses_starving_host():
    run = _FakeRun({"config get": (0, "16GiB", "")})
    # host 21GiB → reserve max(4GiB, 20%*21=4.2GiB)=4.2GiB → cap ~16.8GiB.
    # request 20GiB ≥ cap → refuse.
    with patch.object(grow_capacity, "_read_meminfo", return_value={"MemTotal": 21 * 1024 * 1024}):
        res = await set_container_limits(_cfg(), 20480, None, run=run)
    assert res["ok"] is False
    assert "starve the host" in res["error"] or "exceeds host cap" in res["error"]
    assert not run.set_done


async def test_set_limits_success_memory():
    run = _FakeRun({"config get": (0, "16GiB", ""), "config set": (0, "", "")})
    with patch.object(
        grow_capacity, "_read_meminfo", return_value={"MemTotal": 32 * 1024 * 1024}
    ):  # 32GiB host
        res = await set_container_limits(_cfg(), 20480, None, run=run)  # 20GiB, cap ~25.6GiB
    assert res["ok"] is True
    assert any("limits.memory=20480MiB" in c for c in run.calls)


async def test_set_limits_cpu_range_guard():
    run = _FakeRun({"config get": (0, "16GiB", "")})
    with (
        patch.object(grow_capacity, "_read_meminfo", return_value={"MemTotal": 32 * 1024 * 1024}),
        patch("os.cpu_count", return_value=4),
    ):
        res = await set_container_limits(_cfg(), None, 99, run=run)  # 99 > 4 host cores
    assert res["ok"] is False
    assert "out of range" in res["error"]


async def test_set_limits_meminfo_unreadable():
    run = _FakeRun({})
    with patch.object(grow_capacity, "_read_meminfo", return_value={}):
        res = await set_container_limits(_cfg(), 20480, None, run=run)
    assert res["ok"] is False
    assert "MemTotal" in res["error"]


class _CpuAwareRun:
    """Fake run that answers limits.cpu / limits.memory gets distinctly."""

    def __init__(self, cur_cpu: str, cur_mem: str = "16GiB"):
        self.cur_cpu = cur_cpu
        self.cur_mem = cur_mem
        self.calls: list[str] = []

    async def __call__(self, *argv, timeout=None, stdin_data=None):
        j = " ".join(argv)
        self.calls.append(j)
        if "get" in j and "limits.cpu" in j:
            return (0, self.cur_cpu, "")
        if "get" in j and "limits.memory" in j:
            return (0, self.cur_mem, "")
        return (0, "", "")


async def test_set_limits_cpu_grow_only():
    """Codex P2: cpu must be grow-only — a cpu=4 request must NOT lower cpu=8."""
    run = _CpuAwareRun(cur_cpu="8")
    with patch.object(grow_capacity, "_read_meminfo",
                      return_value={"MemTotal": 64 * 1024 * 1024}), \
         patch("os.cpu_count", return_value=16):
        res = await set_container_limits(_cfg(), None, 4, run=run)
    assert res["ok"] is False
    assert "grow-only" in res["error"]
    assert not any("config set" in c for c in run.calls)  # never mutated


async def test_set_limits_invalid_cpu_does_not_set_memory():
    """Codex P2: a valid-memory + invalid-cpu request must not partially set mem."""
    run = _CpuAwareRun(cur_cpu="2")
    with patch.object(grow_capacity, "_read_meminfo",
                      return_value={"MemTotal": 64 * 1024 * 1024}), \
         patch("os.cpu_count", return_value=4):
        res = await set_container_limits(_cfg(), 20480, 99, run=run)  # cpu 99 > 4 cores
    assert res["ok"] is False
    assert "out of range" in res["error"]
    # up-front validation means NO memory set was issued
    assert not any("config set" in c and "limits.memory" in c for c in run.calls)
