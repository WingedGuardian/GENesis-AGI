"""Live jobs: the ``genesis-job-*`` scopes that ``run`` launched, read from systemd.

The scope IS the ledger entry. Its ``MemoryMax`` is the job's declared RAM
estimate (its reservation) and its ``CPUQuota`` the CPU estimate, so any
session can see every admitted job with no shared file to keep in sync. Jobs
run uncapped (no user manager) are not scopes and are invisible here.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

from genesis.hostmetrics.readings import CGROUP_ROOT, read_container_memory_reclaimable

SCOPE_PREFIX = "genesis-job-"


@dataclass(frozen=True)
class Job:
    unit: str
    reserved: int  # bytes: the scope's MemoryMax, i.e. the declared RAM estimate
    current: int  # bytes in use now, page cache excluded (as container "used" is)
    cpu_quota_pct: float | None  # core-percent, None if unset


def systemd_env() -> dict[str, str]:
    """The environment ``systemd-run``/``systemctl --user`` need to reach the user
    manager; an env-scrubbed caller often lacks both variables."""
    uid = os.getuid()
    env = dict(os.environ)
    # An empty value is a scrubbed one, not a choice: systemd treats "" as an
    # explicit, unusable address rather than discovering the user bus.
    if not env.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
    return env


def _systemctl(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=15,  # a local D-Bus round trip; a hang means no reachable manager
            env=systemd_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _cpu_pct(value: str) -> float | None:
    """``CPUQuotaPerSecUSec`` (e.g. ``500ms``, ``1.500000s``, ``1min 30s``) as core-percent."""
    if value.startswith("infinity"):
        return None
    if " " in value:  # systemd spells 60 s and over as "1min 30s"
        parts = [_cpu_pct(p) for p in value.split()]
        return None if None in parts else sum(parts)
    scale = {"min": 6000.0, "us": 1e-4, "ms": 0.1, "s": 100.0}
    for suffix in ("min", "us", "ms", "s"):
        if value.endswith(suffix):
            try:
                return float(value[: -len(suffix)]) * scale[suffix]
            except ValueError:
                return None
    return None


def _file_cache(control_group: str) -> int | None:
    """The scope's file-LRU bytes, or None when they cannot be read."""
    if not control_group:
        return None
    return read_container_memory_reclaimable(CGROUP_ROOT / control_group.lstrip("/"))


def parse_show(
    text: str, file_cache: Callable[[str], int | None] = _file_cache
) -> list[Job]:
    """Jobs from ``systemctl show -p Id -p MemoryMax -p MemoryCurrent -p
    CPUQuotaPerSecUSec -p ControlGroup`` over several units (blank-line separated).

    A job's page cache is subtracted from its use: container "used" excludes
    reclaimable cache, so a job whose cache fills its cap still holds its full
    reservation against anonymous growth."""
    jobs = []
    for block in text.strip().split("\n\n"):
        props = dict(line.partition("=")[::2] for line in block.splitlines() if "=" in line)
        unit = props.get("Id", "")
        try:
            reserved = int(props.get("MemoryMax", ""))
        except ValueError:
            continue  # "infinity": not one of ours, or no reservation to count
        try:
            current = int(props.get("MemoryCurrent", ""))
        except ValueError:
            current = 0  # "[not set]": started, nothing charged yet
        if unit.startswith(SCOPE_PREFIX):
            cache = file_cache(props.get("ControlGroup", ""))
            # Unknown cache: count the whole reservation (use 0), never MemoryCurrent,
            # which would still include cache that live use excludes.
            current = 0 if cache is None else max(0, current - cache)
            jobs.append(Job(unit, reserved, current, _cpu_pct(props.get("CPUQuotaPerSecUSec", ""))))
    return jobs


def live_jobs() -> list[Job] | None:
    """Running ``genesis-job-*`` scopes, or None if the user manager is unreachable."""
    listing = _systemctl(
        "list-units", f"{SCOPE_PREFIX}*", "--type=scope", "--state=active", "--plain", "--no-legend"
    )
    if listing is None:
        return None
    units = [line.split()[0] for line in listing.splitlines() if line.strip()]
    if not units:
        return []
    shown = _systemctl(
        "show",
        "-p",
        "Id",
        "-p",
        "MemoryMax",
        "-p",
        "MemoryCurrent",
        "-p",
        "CPUQuotaPerSecUSec",
        "-p",
        "ControlGroup",
        *units,
    )
    return None if shown is None else parse_show(shown)  # listed but unreadable: unknown


def reserved_beyond_use(jobs: list[Job]) -> int:
    """Σ max(0, R_i − actual_i): memory the live jobs may still grow into.

    Their actual use is already in the live reading, so only the unused part of
    each reservation is added (actual excludes page cache, as "used" does).
    ``MemoryMax`` caps actual at R, so a job that has not started yet and one
    running below R count the same way.
    """
    return sum(max(0, j.reserved - j.current) for j in jobs)
