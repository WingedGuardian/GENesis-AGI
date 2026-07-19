"""Container collectors against fixture proc/sys/etc trees (the injectable-root seam)."""

from __future__ import annotations

import json

import pytest

from genesis.infra_profile.collectors import container as _container
from genesis.infra_profile.collectors.container import (
    _keepconf_on_route_link,
    _networkd_manages_link,
    _networkd_route_iface,
    collect_cpu,
    collect_kernel,
    collect_memory,
    collect_network,
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

    # systemd applies drop-ins lexicographically, LAST assignment wins: an
    # operator zz-override reverting to auto disables the policy — the fact
    # must not report a protection that is no longer effective.
    dropins.joinpath("zz-local.conf").write_text(
        "[Slice]\nManagedOOMMemoryPressure=auto\n",
    )
    result = await collect_memory(proc_root=proc_root, sys_root=sys_root, etc_root=tmp_path)
    assert result.facts["oomd_user_slice_kill"] is False


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


# ── network resilience facts (KeepConfiguration + watchdog) ──────────────────


def _keepconf_dir(etc_root):
    d = etc_root / "systemd/network/10-netplan-eth0.network.d"
    d.mkdir(parents=True)
    return d


async def test_network_keep_configuration_fact(tmp_path):
    # no drop-in dir at all -> unprotected
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is False

    # KeepConfiguration=no does NOT count as protection
    d = _keepconf_dir(tmp_path)
    d.joinpath("genesis-keep-config.conf").write_text("[Network]\nKeepConfiguration=no\n")
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is False

    # =true (any non-"no" value) counts, whitespace-tolerant, comments ignored
    d.joinpath("genesis-keep-config.conf").write_text(
        "[Network]\n# KeepConfiguration=no\nKeepConfiguration = true\n"
    )
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is True

    # last-assignment-wins WITHIN the link's own drop-in dir: a later zz-*.conf
    # reverting to `no` disables THIS link's protection — the fact must not lie.
    d.joinpath("zz-off.conf").write_text("[Network]\nKeepConfiguration=no\n")
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is False


async def test_network_keep_configuration_scoped_per_network_file(tmp_path):
    # Drop-ins are scoped per .network file, not globally: a `no` on one
    # interface must not mask protection on another, and last-assignment-wins is
    # evaluated within each dir independently (Codex P2 — cross-interface bleed).
    net = tmp_path / "systemd/network"
    (net / "10-eth0.network.d").mkdir(parents=True)
    (net / "20-eth1.network.d").mkdir(parents=True)
    # eth1 (later-sorting) explicitly off; eth0 protected -> overall True.
    (net / "10-eth0.network.d/genesis-keep-config.conf").write_text(
        "[Network]\nKeepConfiguration=true\n"
    )
    (net / "20-eth1.network.d/other.conf").write_text("[Network]\nKeepConfiguration=no\n")
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is True

    # remove the protected link -> only the `no` link remains -> False
    (net / "10-eth0.network.d/genesis-keep-config.conf").unlink()
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_keep_configuration"] is False


async def test_network_watchdog_installed_fact(tmp_path):
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["network_watchdog_installed"] is False

    timer = tmp_path / "systemd/system/genesis-network-watchdog.timer"
    timer.parent.mkdir(parents=True)
    timer.write_text("[Timer]\nOnUnitActiveSec=2min\n")
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["network_watchdog_installed"] is True


async def test_network_watchdog_metrics_from_run_state(tmp_path):
    run = tmp_path / "run"
    run.mkdir()

    # absent telemetry file -> no metric key (no drift churn)
    result = await collect_network(etc_root=tmp_path, run_root=run)
    assert "watchdog" not in result.metrics

    # valid telemetry -> parsed into METRICS, never facts
    (run / "genesis-network-watchdog.json").write_text(
        '{"last_check": 100, "last_heal": 90, "last_trigger": "failed-link:eth0",'
        ' "heal_count": 2, "last_action": "healed"}'
    )
    result = await collect_network(etc_root=tmp_path, run_root=run)
    assert result.metrics["watchdog"]["heal_count"] == 2
    assert result.metrics["watchdog"]["last_trigger"] == "failed-link:eth0"
    assert "watchdog" not in result.facts

    # malformed JSON -> key omitted, section does not fail
    (run / "genesis-network-watchdog.json").write_text("{not json")
    result = await collect_network(etc_root=tmp_path, run_root=run)
    assert "watchdog" not in result.metrics
    assert result.status == STATUS_OK


async def test_network_resilience_facts_are_deterministic(tmp_path):
    """The new facts must not churn the section hash across identical reads."""
    from genesis.infra_profile.hashing import section_hash

    d = _keepconf_dir(tmp_path)
    d.joinpath("genesis-keep-config.conf").write_text("[Network]\nKeepConfiguration=true\n")
    first = await collect_network(etc_root=tmp_path)
    second = await collect_network(etc_root=tmp_path)
    assert first.facts["networkd_keep_configuration"] is True
    assert section_hash(first.facts) == section_hash(second.facts)


# ── networkd default-route management gate (network-posture applicability) ───


@pytest.mark.parametrize(
    "networkctl_json, dev, expected",
    [
        # networkd owns the default-route link → the two protections apply
        ('{"Interfaces": [{"Name": "eth0", "AdministrativeState": "configured"}]}', "eth0", True),
        # NetworkManager / foreign owns it → not applicable, stay silent
        ('{"Interfaces": [{"Name": "eth0", "AdministrativeState": "unmanaged"}]}', "eth0", False),
        # default-route dev is not one of networkd's links → not applicable
        ('{"Interfaces": [{"Name": "eth1", "AdministrativeState": "configured"}]}', "eth0", False),
        # networkctl absent / networkd not running (_run_cmd → None) → suppress
        (None, "eth0", False),
        # no default route resolved → nothing to correlate on
        ('{"Interfaces": [{"Name": "eth0", "AdministrativeState": "configured"}]}', None, False),
        # malformed / wrong-shape JSON must fail safe, never raise
        ("not json", "eth0", False),
        ("[]", "eth0", False),
        ('{"Interfaces": null}', "eth0", False),
        ("", "eth0", False),
    ],
)
def test_networkd_manages_link(networkctl_json, dev, expected):
    assert _networkd_manages_link(networkctl_json, dev) is expected


def test_networkd_route_iface_selects_by_name():
    payload = json.dumps(
        {
            "Interfaces": [
                {"Name": "lo", "AdministrativeState": "unmanaged"},
                {"Name": "eth0", "AdministrativeState": "configured", "NetworkFile": "/x.network"},
            ]
        }
    )
    assert _networkd_route_iface(payload, "eth0")["NetworkFile"] == "/x.network"
    assert _networkd_route_iface(payload, "eth9") is None
    assert _networkd_route_iface(None, "eth0") is None
    assert _networkd_route_iface("not json", "eth0") is None
    assert _networkd_route_iface("[]", "eth0") is None  # wrong shape → None, no raise


def test_keepconf_on_route_link_is_scoped_to_that_unit(tmp_path):
    # P2 #1: KeepConfiguration must be verified on the DEFAULT-ROUTE link's own
    # drop-in dir — a protected *other* link must not count.
    net = tmp_path / "systemd/network"
    (net / "10-eth0.network.d").mkdir(parents=True)
    (net / "20-eth1.network.d").mkdir(parents=True)
    (net / "20-eth1.network.d/keep.conf").write_text("[Network]\nKeepConfiguration=true\n")
    nf = "/run/systemd/network/10-eth0.network"  # the default-route link
    # eth1 is protected, eth0 is not → route-scoped verdict is False.
    assert _keepconf_on_route_link(tmp_path, nf) is False
    # protect eth0 itself → True.
    (net / "10-eth0.network.d/keep.conf").write_text("[Network]\nKeepConfiguration=true\n")
    assert _keepconf_on_route_link(tmp_path, nf) is True
    # no unit (networkd not managing / not reported) → False.
    assert _keepconf_on_route_link(tmp_path, None) is False


def _fake_run_cmd(
    *,
    admin_state="configured",
    network_file="/run/systemd/network/10-netplan-eth0.network",
    watchdog_enabled=True,
):
    """Fake _run_cmd covering the three commands collect_network shells out to."""

    async def _fake(*argv, **_kw):
        if argv and argv[0] == "ip" and "route" in argv:
            return json.dumps([{"dev": "eth0", "gateway": "10.0.0.1"}])
        if argv and argv[0] == "networkctl":
            iface = {"Name": "eth0", "AdministrativeState": admin_state}
            if network_file is not None:
                iface["NetworkFile"] = network_file
            return json.dumps({"Interfaces": [iface]})
        if argv and argv[0] == "systemctl" and "is-enabled" in argv:
            # real _run_cmd returns None on non-zero rc (disabled/masked/absent)
            return "enabled" if watchdog_enabled else None
        return None  # ip -j addr etc. → harmless None

    return _fake


async def test_collect_network_effective_facts_all_present(tmp_path, monkeypatch):
    # networkd owns eth0; its drop-in has KeepConfiguration; watchdog enabled.
    (tmp_path / "systemd/network/10-netplan-eth0.network.d").mkdir(parents=True)
    (tmp_path / "systemd/network/10-netplan-eth0.network.d/keep.conf").write_text(
        "[Network]\nKeepConfiguration=true\n"
    )
    monkeypatch.setattr(_container, "_run_cmd", _fake_run_cmd())
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["default_route_dev"] == "eth0"
    assert result.facts["networkd_manages_default_route"] is True
    assert result.facts["networkd_default_route_keepconfig"] is True
    assert result.facts["network_watchdog_enabled"] is True


async def test_collect_network_effective_facts_all_missing(tmp_path, monkeypatch):
    # networkd owns eth0 but NO KeepConfiguration drop-in and watchdog disabled.
    monkeypatch.setattr(_container, "_run_cmd", _fake_run_cmd(watchdog_enabled=False))
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_manages_default_route"] is True
    assert result.facts["networkd_default_route_keepconfig"] is False
    assert result.facts["network_watchdog_enabled"] is False


async def test_collect_network_suppresses_when_networkmanager(tmp_path, monkeypatch):
    # Unmanaged default route → gate False, and the scoped keepconfig fact is
    # False (no NetworkFile credited) regardless of any drop-ins present.
    monkeypatch.setattr(_container, "_run_cmd", _fake_run_cmd(admin_state="unmanaged"))
    result = await collect_network(etc_root=tmp_path)
    assert result.facts["networkd_manages_default_route"] is False
    assert result.facts["networkd_default_route_keepconfig"] is False


# ── cc-tmp isolation (blast-radius split) ──────────────────────────────────


async def test_storage_cc_tmp_not_isolated_same_fs(proc_root, sys_root, tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["cc_tmp_isolated"] is False  # shares a device with its parent
    assert "cc_tmp" in result.metrics  # df headroom label present


async def test_storage_cc_tmp_isolated_own_device(proc_root, sys_root, tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))

    class _S:
        def __init__(self, dev):
            self.st_dev = dev

    def fake_stat(path, *a, **k):
        return _S(1 if str(path).endswith("cc-tmp") else 2)

    monkeypatch.setattr(_container.os, "stat", fake_stat)
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["cc_tmp_isolated"] is True


async def test_storage_cc_tmp_fact_absent_when_missing(proc_root, sys_root, tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".genesis").mkdir(parents=True)  # cc-tmp does not exist
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert "cc_tmp_isolated" not in result.facts
