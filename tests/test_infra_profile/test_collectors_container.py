"""Container collectors against fixture proc/sys/etc trees (the injectable-root seam)."""

from __future__ import annotations

import pytest

from genesis.infra_profile.collectors.container import (
    collect_cpu,
    collect_kernel,
    collect_memory,
    collect_os,
    collect_storage,
)
from genesis.infra_profile.types import STATUS_OK


@pytest.fixture
def proc_root(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "cpuinfo").write_text(
        "processor\t: 0\n"
        "model name\t: Intel(R) Xeon(R) CPU E5-2670 v2 @ 2.50GHz\n"
        "flags\t\t: sse4_2 avx fpu\n"
        "\n"
        "processor\t: 1\n"
        "model name\t: Intel(R) Xeon(R) CPU E5-2670 v2 @ 2.50GHz\n"
        "flags\t\t: sse4_2 avx fpu\n",
    )
    (proc / "stat").write_text(
        "cpu  100 0 50 800 10 0 5 35 0 0\n",
    )
    (proc / "meminfo").write_text(
        "MemTotal:       16384000 kB\n"
        "MemAvailable:    8192000 kB\n"
        "SwapTotal:       2097152 kB\n"
        "SwapFree:        2097152 kB\n",
    )
    (proc / "mounts").write_text(
        "sysfs /sys sysfs rw,nosuid 0 0\n"
        "/dev/sda2 / ext4 rw,relatime,discard 0 0\n"
        "tmpfs /tmp tmpfs rw,size=524288k 0 0\n"
        "/dev/sda1 /home ext4 rw,noatime 0 0\n",
    )
    sys_kernel = proc / "sys"
    (sys_kernel / "vm").mkdir(parents=True)
    (sys_kernel / "vm" / "swappiness").write_text("60\n")
    (sys_kernel / "kernel" / "random").mkdir(parents=True)
    (sys_kernel / "kernel" / "random" / "entropy_avail").write_text("3754\n")
    return proc


@pytest.fixture
def sys_root(tmp_path):
    sys = tmp_path / "sys"
    vuln = sys / "devices/system/cpu/vulnerabilities"
    vuln.mkdir(parents=True)
    (vuln / "meltdown").write_text("Mitigation: PTI\n")
    (vuln / "spectre_v2").write_text("Vulnerable\n")
    cpufreq = sys / "devices/system/cpu/cpu0/cpufreq"
    cpufreq.mkdir(parents=True)
    (cpufreq / "scaling_governor").write_text("powersave\n")
    (sys / "block").mkdir()
    (sys / "kernel/mm/transparent_hugepage").mkdir(parents=True)
    (sys / "kernel/mm/transparent_hugepage" / "enabled").write_text(
        "always [madvise] never\n",
    )
    (sys / "fs/cgroup").mkdir(parents=True)
    (sys / "fs/cgroup" / "pids.max").write_text("15000\n")
    return sys


async def test_cpu_facts(proc_root, sys_root):
    result = await collect_cpu(proc_root=proc_root, sys_root=sys_root)
    assert result.status == STATUS_OK
    assert result.facts["count"] == 2
    assert "Xeon" in result.facts["model"]
    assert result.facts["flags"] == ["avx", "fpu", "sse4_2"]  # sorted
    assert result.facts["vulnerabilities"]["meltdown"] == "Mitigation: PTI"
    assert result.facts["governor"] == "powersave"
    # steal ticks present → steal_pct metric computed, never a fact
    assert "steal_pct" in result.metrics
    assert "steal_pct" not in result.facts


async def test_memory_facts(proc_root, sys_root):
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["mem_total"] == 16384000 * 1024
    assert result.facts["swap_total"] == 2097152 * 1024
    assert result.facts["transparent_hugepage"] == "always [madvise] never"
    # volatile values are metrics
    assert result.metrics["mem_available"] == 8192000 * 1024
    assert "mem_available" not in result.facts


async def test_memory_swap_max_tristate(proc_root, sys_root, tmp_path):
    # "max" (healthy) survives as the string; "0" (the 2026-07 wedge state)
    # as an int; an absent file (cgroup v1) as None. All three are facts —
    # the 0/max flip is exactly the drift the body schema exists to catch.
    cg = sys_root / "fs/cgroup"
    cg.joinpath("memory.swap.max").write_text("max\n")
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["cgroup_memory_swap_max"] == "max"

    cg.joinpath("memory.swap.max").write_text("0\n")
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["cgroup_memory_swap_max"] == 0

    cg.joinpath("memory.swap.max").unlink()
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["cgroup_memory_swap_max"] is None


async def test_oomd_policy_fact_from_dropins(proc_root, sys_root, tmp_path):
    dropins = tmp_path / "systemd/system/user.slice.d"

    # no drop-in dir at all -> unprotected
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["oomd_user_slice_kill"] is False

    # a commented-out or auto policy does not count
    dropins.mkdir(parents=True)
    dropins.joinpath("genesis-oomd.conf").write_text(
        "[Slice]\n# ManagedOOMMemoryPressure=kill\nManagedOOMMemoryPressure=auto\n",
    )
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["oomd_user_slice_kill"] is False

    # the real policy (whitespace-tolerant) counts
    dropins.joinpath("genesis-oomd.conf").write_text(
        "[Slice]\nManagedOOMMemoryPressure = kill\nManagedOOMMemoryPressureLimit=60%\n",
    )
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["oomd_user_slice_kill"] is True


async def test_storage_mounts_sorted_and_filtered(proc_root, sys_root):
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    mounts = result.facts["mounts"]
    # sysfs filtered out; sorted by mountpoint; options sorted
    assert [m["mountpoint"] for m in mounts] == ["/", "/home", "/tmp"]
    root = mounts[0]
    assert root["fstype"] == "ext4"
    assert root["options"] == sorted(root["options"])


async def test_kernel_sysctls(proc_root, sys_root):
    result = await collect_kernel(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["sysctls"]["vm.swappiness"] == "60"
    # missing sysctls present as None (stable key set — no hash churn)
    assert result.facts["sysctls"]["fs.file-max"] is None
    assert result.facts["cgroup_pids_max"] == "15000"
    assert result.metrics["entropy_avail"] == 3754


async def test_os_facts(tmp_path):
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "os-release").write_text(
        'ID=ubuntu\nVERSION_ID="24.04"\nPRETTY_NAME="Ubuntu 24.04.2 LTS"\n',
    )
    result = await collect_os(etc_root=etc)
    assert result.facts["id"] == "ubuntu"
    assert result.facts["version_id"] == "24.04"
    assert result.facts["hostname"]


async def test_collector_determinism(proc_root, sys_root):
    """Same tree twice → identical facts (the anti-churn contract)."""
    from genesis.infra_profile.hashing import section_hash

    first = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    second = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert section_hash(first.facts) == section_hash(second.facts)
