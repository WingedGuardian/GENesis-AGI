"""Container-plane fact collectors.

Every collector takes injectable filesystem roots (``proc_root``, ``sys_root``,
``etc_root``) defaulting to the live paths — the test seam: tests point them at
fixture trees instead of mocking ``open``.

Facts vs metrics discipline (see ``types.py``): facts must be deterministic
across runs on an unchanged system. Lists are emitted in a defined order
(mounts by mountpoint, interfaces by name, flags sorted) so the section hash
only moves when the system actually changed.

Subprocess timeout rationale: these are raw subprocess calls inside a
boot-path task with no external watchdog — a hung ``systemctl`` would hold the
refresh lock forever. All the commands here complete in well under a second on
a healthy system; 15s tolerates heavy load while still releasing the lock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
from pathlib import Path

from genesis.infra_profile.types import SectionResult

logger = logging.getLogger(__name__)

_CMD_TIMEOUT = 15.0

# Filesystem types worth recording as real mounts. Kernel plumbing
# (proc/sysfs/cgroup2/…) is noise; tmpfs is deliberately IN — tmpfs sizes
# (/tmp!) are a known incident class.
_MOUNT_FSTYPES = {
    "ext4",
    "ext3",
    "ext2",
    "xfs",
    "btrfs",
    "zfs",
    "f2fs",
    "vfat",
    "tmpfs",
    "nfs",
    "nfs4",
    "9p",
    "virtiofs",
    "cifs",
    "squashfs",
    "fuse.sshfs",
}

# Sysctls that have each bitten container installs; a fixed set keeps the
# facts hash stable.
_SYSCTLS = (
    "vm/swappiness",
    "vm/overcommit_memory",
    "fs/inotify/max_user_watches",
    "fs/inotify/max_user_instances",
    "fs/file-max",
    "kernel/pid_max",
)


async def _run_cmd(*argv: str, timeout: float = _CMD_TIMEOUT) -> str | None:
    """Run a command, return stripped stdout, or None on any failure."""
    if shutil.which(argv[0]) is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        logger.warning("infra_profile command timed out: %s", " ".join(argv))
        return None
    except OSError as exc:
        logger.debug("infra_profile command failed to start (%s): %s", argv[0], exc)
        return None
    if proc.returncode != 0:
        return None
    return stdout.decode(errors="replace").strip()


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


# Local twins of runtime/cgroup.py helpers, NOT imports: `import
# genesis.runtime.cgroup` executes genesis/runtime/__init__ and drags the full
# GenesisRuntime graph into whatever process collects — including the
# lightweight genesis-health MCP server on `infrastructure_profile
# (refresh=true)`. MCP-process RSS is a known OOM class on this install
# (review 2026-07-12, finder B). Keep these in sync with runtime/cgroup.py.


def _read_cgroup_memory_max(sys_root: Path) -> int | None:
    raw = _read(sys_root / "fs/cgroup/memory.max")
    if raw is None or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_cgroup_memory_swap_max(sys_root: Path) -> int | str | None:
    """Unlike memory.max (where "max" collapses to None = no limit), the
    swap knob keeps its raw tri-state: "0" IS the incident state (memory
    spikes thrash instead of swapping — the 2026-07 wedge series), "max"
    is the healthy state, None means unreadable/cgroup-v1."""
    raw = _read(sys_root / "fs/cgroup/memory.swap.max")
    if raw is None or raw == "max":
        return raw
    try:
        return int(raw)
    except ValueError:
        return None


def _oomd_user_slice_kill(etc_root: Path) -> bool:
    """Whether a systemd-oomd pressure-kill policy is configured for
    user.slice (ManagedOOMMemoryPressure=kill in any user.slice.d drop-in,
    e.g. the genesis-oomd.conf that scripts/lib/memory_resilience.sh lays
    down). Config-plane deliberately: the runtime signal candidate (the
    /run/systemd/io.systemd.ManagedOOM socket) was probed live and predates
    protection — it means "oomd installed", not "policy configured"."""
    dropin_dir = etc_root / "systemd/system/user.slice.d"
    if not dropin_dir.is_dir():
        return False
    # systemd semantics, not grep semantics: drop-ins apply in lexicographic
    # order and the LAST assignment wins (a later zz-local.conf reverting to
    # `auto` disables the policy — first-match would report a lie), and unit
    # files have no inline comments (only full-line # / ; lines).
    effective: str | None = None
    for conf in sorted(dropin_dir.glob("*.conf")):
        raw = _read(conf) or ""
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(("#", ";")):
                continue
            key, sep, value = stripped.partition("=")
            if sep and key.strip() == "ManagedOOMMemoryPressure":
                effective = value.strip()
    return effective == "kill"


def _networkd_keep_configuration(etc_root: Path) -> bool:
    """True when at least one systemd-networkd link has an effective
    KeepConfiguration policy (any value other than "no") — the
    genesis-keep-config.conf that scripts/lib/network_resilience.sh lays down so
    a networkd failure RETAINS the address instead of dropping it (the 2026-07
    eth0 wedge). Scoped the way systemd scopes drop-ins: last-assignment-wins
    WITHIN each `.network.d` directory (a later zz-*.conf reverting to `auto`/
    `no` disables that link), then OR'd across links — an unrelated drop-in on
    a *different* interface can neither fake protection nor mask it. Config-
    plane: scans our world-readable /etc drop-ins (the /run render is root-only).
    Comments are full-line only (unit files have no inline comments)."""
    net_dir = etc_root / "systemd/network"
    if not net_dir.is_dir():
        return False
    for dropin_dir in sorted(net_dir.glob("*.network.d")):
        if not dropin_dir.is_dir():
            continue
        effective: str | None = None
        for conf in sorted(dropin_dir.glob("*.conf")):
            raw = _read(conf) or ""
            for line in raw.splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", ";")):
                    continue
                key, sep, value = stripped.partition("=")
                if sep and key.strip() == "KeepConfiguration":
                    effective = value.strip()
        if effective is not None and effective.lower() != "no":
            return True
    return False


def _networkd_manages_link(networkctl_json: str | None, dev: str | None) -> bool:
    """True when the RUNNING systemd-networkd daemon reports interface ``dev`` as
    ``AdministrativeState == "configured"`` — i.e. networkd owns that link via a
    .network file. This is the applicability gate for the network-resilience
    posture: the KeepConfiguration + watchdog protections only matter when
    networkd manages the box's primary connectivity. On a NetworkManager install
    the daemon either is not running (``networkctl`` fails → None reaches here)
    or reports the link ``unmanaged`` (foreign/NM-managed) — either way False, so
    the posture check stays silent instead of false-alarming.

    Any doubt returns False (no output, unparseable JSON, dev absent/None, link
    not "configured"): a false-negative is re-checked next collection; a
    false-positive would nag a NetworkManager box that can't act on it. Queried
    live rather than reading /run/systemd/netif/links/<idx>, which is marked "do
    not parse" and — under the unit's RuntimeDirectoryPreserve=yes — survives a
    networkd stop, so its presence would NOT prove networkd is the manager."""
    if not networkctl_json or not dev:
        return False
    try:
        interfaces = json.loads(networkctl_json).get("Interfaces") or []
    except (ValueError, TypeError, AttributeError):
        return False
    for iface in interfaces:
        if isinstance(iface, dict) and iface.get("Name") == dev:
            return iface.get("AdministrativeState") == "configured"
    return False


def _read_watchdog_state(run_root: Path) -> dict | None:
    """Parse the network watchdog's /run telemetry (last_check / last_heal /
    last_trigger / heal_count / last_action). Returns None when absent or
    malformed — it is a METRIC, so a missing or garbage file just omits the key
    rather than failing the section."""
    raw = _read(run_root / "genesis-network-watchdog.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _detect_root_device(proc_root: Path) -> str | None:
    """Block device major:minor for the root filesystem (via mountinfo)."""
    raw = _read(proc_root / "self/mountinfo") or ""
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) >= 10 and fields[4] == "/":
            return fields[2]
    return None


# ── os ───────────────────────────────────────────────────────────────────


async def collect_os(etc_root: Path = Path("/etc")) -> SectionResult:
    """Distro, architecture, hostname."""
    facts: dict = {"architecture": platform.machine(), "hostname": socket.gethostname()}
    os_release = _read(etc_root / "os-release") or ""
    for line in os_release.splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "VERSION_ID", "PRETTY_NAME"):
            facts[key.lower()] = value.strip('"')
    return SectionResult(name="os", facts=facts)


# ── virt ─────────────────────────────────────────────────────────────────


async def collect_virt() -> SectionResult:
    """Virtualization nesting as visible from inside the container."""
    facts: dict = {
        "container": await _run_cmd("systemd-detect-virt", "--container"),
        "vm": await _run_cmd("systemd-detect-virt", "--vm"),
        "kvm_device": Path("/dev/kvm").exists(),
    }
    return SectionResult(name="virt", facts=facts)


# ── cpu ──────────────────────────────────────────────────────────────────


async def collect_cpu(
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
) -> SectionResult:
    """CPU model, flags, vulnerability mitigations, governor; steal% as metric."""
    facts: dict = {}
    metrics: dict = {}

    cpuinfo = _read(proc_root / "cpuinfo") or ""
    count = 0
    for line in cpuinfo.splitlines():
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if key == "processor":
            count += 1
        elif key == "model name" and "model" not in facts:
            facts["model"] = value
        elif key == "flags" and "flags" not in facts:
            facts["flags"] = sorted(value.split())
    facts["count"] = count

    vulns: dict[str, str] = {}
    vuln_dir = sys_root / "devices/system/cpu/vulnerabilities"
    if vuln_dir.is_dir():
        for entry in sorted(vuln_dir.iterdir()):
            value = _read(entry)
            if value is not None:
                vulns[entry.name] = value
    facts["vulnerabilities"] = vulns

    # Truthiness, not `is not None`: _read returns "" for an empty file
    # (cpufreq-less guests), and an empty string must not become a hashed
    # fact baseline (review 2026-07-12).
    governor = _read(
        sys_root / "devices/system/cpu/cpu0/cpufreq/scaling_governor",
    )
    if governor:
        facts["governor"] = governor

    numa_nodes = sys_root / "devices/system/node"
    if numa_nodes.is_dir():
        facts["numa_nodes"] = len([p for p in numa_nodes.iterdir() if p.name.startswith("node")])

    # Steal time — volatile, but persistent nonzero steal is the VPS-contention
    # tell, so it belongs in the rendered doc (as a metric, never hashed).
    stat = _read(proc_root / "stat") or ""
    for line in stat.splitlines():
        if line.startswith("cpu "):
            fields = line.split()
            if len(fields) > 8:
                total = sum(int(f) for f in fields[1:11] if f.isdigit())
                steal = int(fields[8])
                if total > 0:
                    metrics["steal_pct"] = round(100.0 * steal / total, 2)
            break

    return SectionResult(name="cpu", facts=facts, metrics=metrics)


# ── memory ───────────────────────────────────────────────────────────────


async def collect_memory(
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
    etc_root: Path = Path("/etc"),
) -> SectionResult:
    """Cgroup limit, swap/zram config, oomd policy, THP; availability as metrics."""
    facts: dict = {}
    metrics: dict = {}

    facts["cgroup_memory_max"] = _read_cgroup_memory_max(sys_root)
    facts["cgroup_memory_swap_max"] = _read_cgroup_memory_swap_max(sys_root)
    facts["oomd_user_slice_kill"] = _oomd_user_slice_kill(etc_root)

    meminfo = _read(proc_root / "meminfo") or ""
    mem: dict[str, int] = {}
    for line in meminfo.splitlines():
        key, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            mem[key.strip()] = int(parts[0]) * 1024
    facts["mem_total"] = mem.get("MemTotal")
    facts["swap_total"] = mem.get("SwapTotal")
    metrics["mem_available"] = mem.get("MemAvailable")
    metrics["swap_free"] = mem.get("SwapFree")

    facts["zram"] = (
        any(p.name.startswith("zram") for p in (sys_root / "block").iterdir())
        if (sys_root / "block").is_dir()
        else False
    )

    thp = _read(sys_root / "kernel/mm/transparent_hugepage/enabled")
    if thp:  # truthiness — "" from an empty file must not become a fact
        facts["transparent_hugepage"] = thp

    return SectionResult(name="memory", facts=facts, metrics=metrics)


# ── storage ──────────────────────────────────────────────────────────────


async def collect_storage(
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
) -> SectionResult:
    """Mount table + IO scheduler as facts; df/inode headroom as metrics."""
    facts: dict = {}
    metrics: dict = {}

    mounts: list[dict] = []
    raw = _read(proc_root / "mounts") or ""
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, mountpoint, fstype, options = fields[0], fields[1], fields[2], fields[3]
        if fstype not in _MOUNT_FSTYPES:
            continue
        mounts.append(
            {
                "mountpoint": mountpoint,
                "source": source,
                "fstype": fstype,
                "options": sorted(options.split(",")),
            },
        )
    mounts.sort(key=lambda m: m["mountpoint"])
    facts["mounts"] = mounts

    root_dev = _detect_root_device(proc_root)
    facts["root_device"] = root_dev
    if root_dev:
        # root_dev is usually a PARTITION's major:minor, but queue/scheduler
        # exists only on the whole disk — so match the disk's own dev file OR
        # any of its partition subdirs (review 2026-07-12). Best-effort:
        # virtual devices have neither.
        for disk_dir in sorted(sys_root.glob("block/*")):
            devs = [_read(disk_dir / "dev")]
            devs += [_read(p) for p in disk_dir.glob(f"{disk_dir.name}*/dev")]
            if root_dev in devs:
                facts["io_scheduler"] = _read(disk_dir / "queue/scheduler")
                break

    # /tmp is a MEASUREMENT TARGET here (small tmpfs = known incident class),
    # not a temp-file location.
    for label, path in (("root", "/"), ("home", str(Path.home())), ("tmp", "/tmp")):  # noqa: S108
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            metrics[label] = {
                "total_bytes": total,
                "free_bytes": free,
                "pct_used": round((total - free) / total * 100, 1) if total else 0.0,
                "inodes_free": st.f_favail,
            }
        except OSError:
            continue

    return SectionResult(name="storage", facts=facts, metrics=metrics)


# ── kernel ───────────────────────────────────────────────────────────────


async def collect_kernel(
    proc_root: Path = Path("/proc"),
    sys_root: Path = Path("/sys"),
) -> SectionResult:
    """Kernel version + the sysctls that bite containers; entropy as metric."""
    facts: dict = {
        "release": platform.release(),
        "version": platform.version(),
    }
    metrics: dict = {}

    sysctls: dict[str, str | None] = {}
    for key in _SYSCTLS:
        sysctls[key.replace("/", ".")] = _read(proc_root / "sys" / key)
    facts["sysctls"] = sysctls

    facts["cgroup_pids_max"] = _read(sys_root / "fs/cgroup/pids.max")

    entropy = _read(proc_root / "sys/kernel/random/entropy_avail")
    if entropy is not None:
        metrics["entropy_avail"] = int(entropy)

    return SectionResult(name="kernel", facts=facts, metrics=metrics)


# ── network ──────────────────────────────────────────────────────────────


async def collect_network(
    etc_root: Path = Path("/etc"),
    run_root: Path = Path("/run"),
) -> SectionResult:
    """Interfaces/MTU/addresses, DNS, default route, tailscale presence, plus
    the network-resilience posture (KeepConfiguration + watchdog).

    Addresses are FACTS by design: an IP change on this plane is a real
    identity event worth a drift observation, not noise.
    """
    facts: dict = {}
    metrics: dict = {}

    # Addresses and gateway are METRICS, not facts: under DHCP/bridged
    # networking they can change on every container recreation, and hashing
    # them would spam drift observations + burn annotation calls per restart
    # on portable installs (review 2026-07-12). Interface NAMES/MTU are the
    # stable topology and stay facts.
    ip_json = await _run_cmd("ip", "-j", "addr")
    interfaces: list[dict] = []
    addresses: dict[str, list[str]] = {}
    if ip_json:
        try:
            for iface in json.loads(ip_json):
                name = iface.get("ifname")
                addresses[name] = sorted(
                    a.get("local", "")
                    for a in iface.get("addr_info", [])
                    if a.get("scope") == "global"
                )
                interfaces.append({"name": name, "mtu": iface.get("mtu")})
        except (ValueError, KeyError, TypeError):
            logger.debug("infra_profile: cannot parse `ip -j addr` output")
    interfaces.sort(key=lambda i: i["name"] or "")
    facts["interfaces"] = interfaces
    facts["tailscale"] = any(i["name"] == "tailscale0" for i in interfaces)
    metrics["addresses"] = addresses

    resolv = _read(etc_root / "resolv.conf") or ""
    facts["nameservers"] = [
        line.split()[1]
        for line in resolv.splitlines()
        if line.startswith("nameserver") and len(line.split()) > 1
    ]

    route = await _run_cmd("ip", "-j", "route", "show", "default")
    if route:
        try:
            default = json.loads(route)
            if default:
                facts["default_route_dev"] = default[0].get("dev")
                metrics["default_gateway"] = default[0].get("gateway")
        except (ValueError, IndexError, TypeError):
            pass

    # Resilience posture (config-plane facts — see docs/reference/network-
    # resilience.md). Deterministic file reads, so they belong in facts: the
    # annotation layer flags an install that lacks either protection.
    facts["networkd_keep_configuration"] = _networkd_keep_configuration(etc_root)
    facts["network_watchdog_installed"] = (
        etc_root / "systemd/system/genesis-network-watchdog.timer"
    ).exists()
    # Applicability gate for the two facts above: are they even relevant here?
    # The posture check only asserts a network protection is MISSING when
    # networkd manages the default-route link — otherwise (NetworkManager /
    # foreign-managed) the protections don't apply and staying silent is
    # correct. Live daemon query; any doubt → False → silent (see the helper).
    facts["networkd_manages_default_route"] = _networkd_manages_link(
        await _run_cmd("networkctl", "--json=short", "list"),
        facts.get("default_route_dev"),
    )

    # Watchdog heal telemetry is volatile (heal_count/timestamps move), so it is
    # a METRIC — never hashed. Absent file → key omitted (no drift churn).
    watchdog = _read_watchdog_state(run_root)
    if watchdog is not None:
        metrics["watchdog"] = watchdog

    return SectionResult(name="network", facts=facts, metrics=metrics)


# ── systemd ──────────────────────────────────────────────────────────────


async def collect_systemd() -> SectionResult:
    """genesis-* user units: the LIST + enablement are facts; states are metrics."""
    facts: dict = {}
    metrics: dict = {}

    listing = await _run_cmd(
        "systemctl",
        "--user",
        "list-unit-files",
        "genesis-*",
        "--no-legend",
        "--plain",
    )
    units: list[dict] = []
    if listing:
        for line in listing.splitlines():
            fields = line.split()
            if len(fields) >= 2:
                units.append({"unit": fields[0], "enabled": fields[1]})
    units.sort(key=lambda u: u["unit"])
    facts["units"] = units

    states = await _run_cmd(
        "systemctl",
        "--user",
        "list-units",
        "genesis-*",
        "--all",
        "--no-legend",
        "--plain",
    )
    unit_states: dict[str, str] = {}
    if states:
        for line in states.splitlines():
            fields = line.split()
            if len(fields) >= 4:
                unit_states[fields[0]] = f"{fields[2]}/{fields[3]}"
    metrics["states"] = unit_states

    return SectionResult(name="systemd", facts=facts, metrics=metrics)


# ── versions ─────────────────────────────────────────────────────────────


async def collect_versions() -> SectionResult:
    """Tool versions. Facts: a version change (CC update, node bump) is a signal."""

    facts: dict = {
        "python": platform.python_version(),
        "sqlite_library": sqlite3.sqlite_version,
    }
    probes = (
        ("node", ("node", "--version")),
        ("claude_cli", ("claude", "--version")),
        ("git", ("git", "--version")),
        ("ruff", ("ruff", "--version")),
    )
    # Independent spawns — gather so one slow tool doesn't serialize the rest.
    outputs = await asyncio.gather(*(_run_cmd(*argv) for _, argv in probes))
    for (key, _), out in zip(probes, outputs, strict=True):
        if out is not None:
            facts[key] = out.splitlines()[0]
    return SectionResult(name="versions", facts=facts)


# ── limits ───────────────────────────────────────────────────────────────


async def collect_limits(proc_root: Path = Path("/proc")) -> SectionResult:
    """Process ulimits + the genesis-server unit's file-descriptor limit."""
    facts: dict = {}

    raw = _read(proc_root / "self/limits") or ""
    limits: dict[str, dict] = {}
    for line in raw.splitlines():
        if line.startswith(("Max open files", "Max processes")):
            fields = line.split()
            # "Max open files  1024  524288  files" — soft, hard are the two
            # numeric fields before the unit.
            nums = [f for f in fields if f.isdigit() or f == "unlimited"]
            if len(nums) >= 2:
                key = "open_files" if "open" in line else "processes"
                limits[key] = {"soft": nums[0], "hard": nums[1]}
    facts["process_limits"] = limits

    unit_nofile = await _run_cmd(
        "systemctl",
        "--user",
        "show",
        "genesis-server",
        "-p",
        "LimitNOFILE",
    )
    if unit_nofile:
        facts["genesis_server_nofile"] = unit_nofile.partition("=")[2]

    return SectionResult(name="limits", facts=facts)


# ── time ─────────────────────────────────────────────────────────────────


async def collect_time() -> SectionResult:
    """Timezone + NTP config as facts; sync state as a metric (flaps at boot)."""
    facts: dict = {}
    metrics: dict = {}

    out = await _run_cmd("timedatectl", "show")
    if out:
        props = dict(line.partition("=")[::2] for line in out.splitlines() if "=" in line)
        facts["timezone"] = props.get("Timezone")
        facts["ntp_enabled"] = props.get("NTP")
        metrics["ntp_synchronized"] = props.get("NTPSynchronized")
    return SectionResult(name="time", facts=facts, metrics=metrics)
