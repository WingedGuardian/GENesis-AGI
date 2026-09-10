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


def test_pid_ceiling_effective_ok(sys_root):
    # Reflects the EFFECTIVE cgroup pids.max (systemd resolves the configured %
    # AND any set-property override into it) vs the container root budget — NOT a
    # drop-in string-match, so a runtime override is judged correctly.
    from genesis.infra_profile.collectors.container import _pid_ceiling_effective_ok

    cg = sys_root / "fs/cgroup"
    slice_dir = cg / "user.slice/user-4242.slice"
    slice_dir.mkdir(parents=True)

    # root=4000 (fixture writes pids.max=15000 → overwrite for determinism)
    cg.joinpath("pids.max").write_text("4000\n")

    # raised to 60% (2400) → well above the 33% default → ok
    slice_dir.joinpath("pids.max").write_text("2400\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is True

    # a DELIBERATE lower operator override (40% = 1600) is a conscious choice, not
    # an unprovisioned box — must read as ok, not falsely nagged (Codex P2 regress).
    slice_dir.joinpath("pids.max").write_text("1600\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is True

    # just above the stock default + rounding margin (35% = 1400) → still ok
    slice_dir.joinpath("pids.max").write_text("1400\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is True

    # still on systemd's stock 33% default (1320) → NOT ok (the defect we surface)
    slice_dir.joinpath("pids.max").write_text("1320\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is False

    # no sub-cap on the slice → ok (inherits the root budget)
    slice_dir.joinpath("pids.max").write_text("max\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is True

    # no container root cap → the % default is huge, not a risk → ok
    slice_dir.joinpath("pids.max").write_text("1320\n")
    cg.joinpath("pids.max").write_text("max\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is True

    # root unreadable → can't determine → None (posture check stays silent)
    cg.joinpath("pids.max").unlink()
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is None

    # slice file absent → None
    slice_dir.joinpath("pids.max").unlink()
    cg.joinpath("pids.max").write_text("4000\n")
    assert _pid_ceiling_effective_ok(sys_root, uid=4242) is None


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


def _write_marker(home, **fields):
    (home / ".genesis" / "state").mkdir(parents=True, exist_ok=True)
    (home / ".genesis" / "state" / "cc_tmp_apply.json").write_text(json.dumps(fields))


def _fresh_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


async def test_storage_cc_tmp_apply_volatile_fields_are_metrics_not_facts(
    proc_root, sys_root, tmp_path, monkeypatch
):
    # The raw reason/timestamp are volatile (change as CC sessions come/go) → they
    # MUST live in metrics (never hashed), not facts, or they spam infra drift.
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    _write_marker(home, last_attempt_at=_fresh_now(), last_reason="live-cc", claude_procs=5)
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    # derived + freshness-bounded → FACT
    assert result.facts["cc_tmp_apply_blocked_on_cc"] is True
    # raw volatile fields → METRICS, and NOT duplicated into facts
    assert result.metrics["cc_tmp_apply_last_reason"] == "live-cc"
    assert "cc_tmp_apply_last_attempt_at" in result.metrics
    assert "cc_tmp_apply_last_reason" not in result.facts
    assert "cc_tmp_apply_last_attempt_at" not in result.facts


async def test_storage_cc_tmp_apply_stale_marker_not_converging(
    proc_root, sys_root, tmp_path, monkeypatch
):
    # A live-cc marker whose attempt is OLD (dead/disabled timer, lock/write
    # failure) must NOT read as converging — the freshness bound flips it.
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    _write_marker(home, last_attempt_at="2020-01-01T00:00:00+00:00", last_reason="live-cc")
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["cc_tmp_apply_blocked_on_cc"] is False


async def test_storage_cc_tmp_apply_terminal_reason_not_blocked(
    proc_root, sys_root, tmp_path, monkeypatch
):
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    _write_marker(home, last_attempt_at=_fresh_now(), last_reason="unsupported-pool")
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert result.facts["cc_tmp_apply_blocked_on_cc"] is False  # not live-cc
    assert result.metrics["cc_tmp_apply_last_reason"] == "unsupported-pool"


async def test_storage_cc_tmp_apply_marker_absent_silent(
    proc_root, sys_root, tmp_path, monkeypatch
):
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)  # no state/ marker
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert "cc_tmp_apply_blocked_on_cc" not in result.facts
    assert "cc_tmp_apply_last_reason" not in result.metrics


async def test_storage_cc_tmp_apply_marker_corrupt_silent(
    proc_root, sys_root, tmp_path, monkeypatch
):
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    (home / ".genesis" / "state").mkdir(parents=True)
    (home / ".genesis" / "state" / "cc_tmp_apply.json").write_text("not valid json {{{")
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    result = await collect_storage(proc_root=proc_root, sys_root=sys_root)  # must not raise
    assert "cc_tmp_apply_blocked_on_cc" not in result.facts


async def test_storage_marker_does_not_churn_facts_hash(proc_root, sys_root, tmp_path, monkeypatch):
    # Cluster-A regression guard: the SAME converging state sampled at two
    # different times (different last_attempt_at) must produce IDENTICAL facts, so
    # section_hash is stable and no spurious infrastructure_drift fires — only the
    # metrics differ.
    home = tmp_path / "h"
    (home / ".genesis" / "cc-tmp").mkdir(parents=True)
    monkeypatch.setattr(_container.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(_container, "_cc_tmp_marker_fresh", lambda _at: True)
    _write_marker(home, last_attempt_at="2026-08-09T10:00:00+00:00", last_reason="live-cc")
    first = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    _write_marker(home, last_attempt_at="2026-08-09T10:02:00+00:00", last_reason="live-cc")
    second = await collect_storage(proc_root=proc_root, sys_root=sys_root)
    assert first.facts == second.facts  # hashed surface stable across marker churn
    assert (
        first.metrics["cc_tmp_apply_last_attempt_at"]
        != second.metrics["cc_tmp_apply_last_attempt_at"]
    )


def test_cc_tmp_marker_fresh_helper():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    assert _container._cc_tmp_marker_fresh((now - timedelta(minutes=30)).isoformat()) is True
    assert _container._cc_tmp_marker_fresh((now - timedelta(hours=12)).isoformat()) is False
    assert _container._cc_tmp_marker_fresh("2020-01-01T00:00:00+00:00") is False
    assert _container._cc_tmp_marker_fresh("not-a-timestamp") is False
    assert _container._cc_tmp_marker_fresh(None) is False
    # a naive (tz-less) timestamp is treated as UTC, not crashed
    assert _container._cc_tmp_marker_fresh(now.replace(tzinfo=None).isoformat()) is True


def _oom_proc(tmp_path, pids: dict[str, str], *, owners: dict[str, str] | None = None):
    """A minimal /proc root holding oom_score_adj (and cgroup) for the given pids.

    ``owners`` maps pid -> the unit whose cgroup that pid sits in. Defaults to the
    pid owning whatever unit the test names, which is the healthy case; pass it
    explicitly to simulate a RECYCLED pid now owned by something else.
    """
    root = tmp_path / "proc"
    root.mkdir(parents=True, exist_ok=True)
    for pid, adj in pids.items():
        d = root / pid
        d.mkdir(parents=True, exist_ok=True)
        (d / "oom_score_adj").write_text(adj + "\n")
        owner = (owners or {}).get(pid, "genesis-server.service")
        (d / "cgroup").write_text(
            f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{owner}\n"
        )
    return root


def _fake_systemctl(units: dict[str, tuple[str, str]]):
    """Fake _run_cmd over `systemctl`: units maps unit -> (MainPID, declared).

    Returns Key=Value output deliberately — systemd emits properties in ITS OWN
    order, so a --value form would let positional parsing pair the wrong numbers.
    """

    async def _run(*argv: str, timeout: float = 0):
        if "list-units" in argv:
            return "\n".join(f"{u} loaded active running x" for u in units)
        if "show" in argv:
            unit = argv[argv.index("show") + 1]
            if unit not in units:
                return None
            pid, declared = units[unit]
            return f"MainPID={pid}\nOOMScoreAdjust={declared}"
        return None

    return _run


async def test_oom_score_adj_detects_a_real_divergence(tmp_path, monkeypatch):
    """The measured defect: declared -500, effective 100, silently.

    systemd keeps reporting the DECLARED value, so this is the only surface that
    can see it.
    """
    from genesis.infra_profile.collectors import container

    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({"genesis-server.service": ("275329", "-500")}),
    )
    proc = _oom_proc(tmp_path, {"275329": "100"})
    assert await container._oom_score_adj_declared_ok(proc) is False


async def test_oom_score_adj_silent_when_everything_agrees(tmp_path, monkeypatch):
    """DIRECTION CONTROL. Units that declare nothing are reported by systemd as
    200 and RUN at 200 — measured 6/6 on a live install — so they must not nag."""
    from genesis.infra_profile.collectors import container

    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({
            "genesis-server.service": ("275329", "100"),
            "genesis-tmp-watchgod.service": ("380", "200"),
        }),
    )
    proc = _oom_proc(
        tmp_path, {"275329": "100", "380": "200"},
        owners={"275329": "genesis-server.service", "380": "genesis-tmp-watchgod.service"},
    )
    assert await container._oom_score_adj_declared_ok(proc) is True


async def test_oom_score_adj_inspects_managed_units_not_the_refresh_process(
    tmp_path, monkeypatch,
):
    """It must NOT read /proc/self.

    `refresh()` is shared by the server, the CLI and a separate health-MCP
    process. An earlier version sampled whichever process happened to refresh, so
    a CLI or MCP refresh returned None — and because the posture check treats
    None as silent, that could AUTO-RESOLVE a standing divergence alert.
    Here the refreshing process's own pid is absent from the fake /proc while a
    managed unit diverges: reading self would yield None, not False.
    """
    import os

    from genesis.infra_profile.collectors import container

    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({"genesis-server.service": ("275329", "-500")}),
    )
    proc = _oom_proc(tmp_path, {"275329": "100"})
    assert not (proc / str(os.getpid())).exists()  # self is deliberately absent
    assert not (proc / "self").exists()
    assert await container._oom_score_adj_declared_ok(proc) is False


async def test_oom_score_adj_silent_when_nothing_checkable(tmp_path, monkeypatch):
    """None (silent) whenever no judgement is possible — the explicit-False-only
    contract the posture check depends on."""
    from genesis.infra_profile.collectors import container

    proc = _oom_proc(tmp_path, {})

    # No systemctl at all (or the listing failed).
    async def _none(*argv: str, timeout: float = 0):
        return None

    monkeypatch.setattr(container, "_run_cmd", _none)
    assert await container._oom_score_adj_declared_ok(proc) is None

    # A unit that is not running has no effective value to compare.
    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({"genesis-backup.service": ("0", "200")}),
    )
    assert await container._oom_score_adj_declared_ok(proc) is None

    # Running, declared, but /proc entry unreadable (raced with exit).
    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({"genesis-server.service": ("999999", "100")}),
    )
    assert await container._oom_score_adj_declared_ok(proc) is None


# ── the CLASS: no user unit may declare an unachievable OOM score ─────────────


def test_no_repo_written_user_unit_declares_a_negative_oom_score():
    """A user-manager unit can NEVER apply a negative oom_score_adj.

    Lowering below the inherited oom_score_adj_min of 0 needs CAP_SYS_RESOURCE,
    which a systemd USER manager does not hold, and the write fails SILENTLY —
    no journal entry, no start failure, while `systemctl show` keeps reporting
    the declared value. So a negative declaration in any unit this repo writes
    into ~/.config/systemd/user is dead on arrival by construction.

    This guardrail exists because scoping the runtime alert to `genesis-*`
    units missed two real cases found in review: `qdrant.service` (written
    inline by install.sh, a HARD dependency of the server, with no
    template-sync path to heal it) and `agent-zero.service` (rendered from a
    template edited in this very change, yet outside the glob). Checking the
    SOURCE closes the class wherever the value is written, independent of which
    units the runtime check happens to enumerate.

    Deliberately excludes host/system units: `config/genesis-guardian.service`
    runs as a SYSTEM unit where the constraint does not apply, and its 0 is a
    considered choice ("the Guardian is NOT expendable").
    """
    import re
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    offenders: list[str] = []
    # Everything that writes a USER unit: the rendered templates plus the
    # inline heredocs in the installer/bootstrap.
    candidates = [
        *(repo / "scripts" / "systemd").glob("*.service.template"),
        repo / "scripts" / "install.sh",
        repo / "scripts" / "bootstrap.sh",
    ]
    for path in candidates:
        if not path.exists():
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # explanatory prose, not a declaration
            m = re.match(r"^OOMScoreAdjust=(-?\d+)$", stripped)
            if m and int(m.group(1)) < 0:
                offenders.append(f"{path.relative_to(repo)}:{lineno} -> {stripped}")
    assert not offenders, (
        "a user unit declares a negative OOMScoreAdjust, which the user manager "
        "will refuse SILENTLY (the value reads back correct while the kernel "
        "ignores it). Use an achievable non-negative value:\n  "
        + "\n  ".join(offenders)
    )


def test_managed_unit_patterns_cover_every_user_unit_the_repo_writes():
    """``_MANAGED_UNIT_PATTERNS`` must match every user unit this repo installs.

    Stated as PARITY between two sets, derived from the repo, rather than as a
    check for the three names that happen to be listed today. Mutation testing is
    what forced this shape: narrowing the list back to ``genesis-*.service`` alone
    broke nothing, because the source guardrail above only covers the WRITE side —
    it proves no unit DECLARES an unachievable value, and says nothing about
    whether the runtime alert would ever LOOK at that unit. Both halves are needed:
    one stops the bad value being written, this one stops the watcher going blind.

    So a newly added managed unit now fails here until it is enumerated, instead of
    silently sitting outside the alert the way ``agent-zero.service`` and
    ``qdrant.service`` did.
    """
    import fnmatch
    import re
    from pathlib import Path

    from genesis.infra_profile.collectors import container

    repo = Path(__file__).resolve().parents[2]

    expected: set[str] = set()
    # Rendered templates — the install/bootstrap loops write each of these.
    for tpl in (repo / "scripts" / "systemd").glob("*.service.template"):
        expected.add(tpl.name.removesuffix(".template"))
    # Inline heredocs writing straight into the user unit dir.
    for script in ("install.sh", "bootstrap.sh"):
        path = repo / "scripts" / script
        if not path.exists():
            continue
        for m in re.finditer(r'\$SYSTEMD_USER_DIR/([A-Za-z0-9@._-]+\.service)', path.read_text()):
            expected.add(m.group(1))

    assert expected, "found no user units — the derivation is stale, not the code"

    unmatched = sorted(
        unit for unit in expected
        if not any(fnmatch.fnmatch(unit, pat) for pat in container._MANAGED_UNIT_PATTERNS)
    )
    assert not unmatched, (
        "user unit(s) this repo writes are NOT covered by _MANAGED_UNIT_PATTERNS, so "
        "the declared-vs-effective OOM alert can never see them:\n  "
        + "\n  ".join(unmatched)
        + "\nAdd a pattern, or the next regression in one of these is invisible."
    )


async def test_oom_score_adj_ignores_a_recycled_pid(tmp_path, monkeypatch):
    """A recycled pid must not fabricate a divergence.

    Between `systemctl show` and the /proc read, a unit's main process can exit
    and its pid be reused by something unrelated — pid churn here is high. Judging
    a stranger's adj would raise a standing high-priority alert that persists to
    the next profile refresh (a same-key repeat is cooldown-suppressed while the
    WRONG fact is what got persisted), and self-heal a day later — baffling rather
    than obvious. The pid's cgroup is the discriminator.
    """
    from genesis.infra_profile.collectors import container

    monkeypatch.setattr(
        container, "_run_cmd",
        _fake_systemctl({"genesis-server.service": ("275329", "100")}),
    )
    # The pid now belongs to an unrelated CC session scope, and its adj (500)
    # differs from the unit's declaration (100) — a naive compare returns False.
    proc = _oom_proc(tmp_path, {"275329": "500"}, owners={"275329": "session-c99.scope"})
    assert await container._oom_score_adj_declared_ok(proc) is None
