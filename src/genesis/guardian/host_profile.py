"""Host-plane profile gather — the guardian's answer to ``host-profile``.

Runs HOST-SIDE (via the gateway verb, ``python -m genesis.guardian
--host-profile``) and emits one JSON blob with three raw sub-dicts. The
container's ``infra_profile.collectors.host`` owns the facts/metrics split —
this module is a dumb, dependency-light data source and must never import
beyond the guardian package (the guardian venv has no full Genesis install;
it runs from ``PYTHONPATH=$INSTALL_DIR/src``).

Every probe is best-effort: a missing tool or unreadable file yields an
omitted/None field, never an exception. Empirically verified against the
live host 2026-07-12: ``incus config show <name>`` exposes ``limits.*`` at
the top-level ``config:`` map without ``--expanded`` or sudo; ``pveversion``
and ``smartctl`` are absent (Proxmox is a layer below the guardian VM);
``measure_storage_pool`` returns real LVM-thin percentages as the
unprivileged guardian user (no sudo).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import platform
import shutil
import socket
from pathlib import Path

from genesis.guardian._subprocess import run_subprocess

logger = logging.getLogger(__name__)

# Bounds a single external probe (incus/systemd-detect-virt/pveversion).
# Failure mode: an incus daemon wedged on a stuck storage pool can hang a
# `config show` indefinitely. 10s per probe keeps one wedged tool bounded
# while staying far above the ~100ms these commands take healthy.
_PROBE_TIMEOUT = 10.0

# measure_storage_pool issues up to ~4 SEQUENTIAL subprocesses (pool-name
# detect, storage show, lvs, vgs), each with its own 10s bound but no
# aggregate cap — a wedged pool could burn ~40s and push the whole gather
# past the gateway's `timeout 45`, SIGKILLing it before any JSON is printed
# (total loss of the healthy sections too). This cap bounds the section so
# the worst case stays inside the gateway budget (review 2026-07-13).
_POOL_TIMEOUT = 30.0


async def _run(*argv: str) -> str | None:
    """Probe stdout on rc=0, else None — thin adapter over the shared
    guardian subprocess helper (timeout kill + OSError → rc=-1 live there)."""
    rc, stdout, _ = await run_subprocess(*argv, timeout=_PROBE_TIMEOUT)
    return stdout if rc == 0 else None


def _read_meminfo() -> dict:
    out: dict = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            if key in ("MemTotal", "MemAvailable"):
                out[key] = int(rest.split()[0])  # kB
    except (OSError, ValueError, IndexError):
        pass
    return out


def _host_system() -> dict:
    """Static-ish host identity + current memory/load readings."""
    section: dict = {}
    mem = _read_meminfo()
    if "MemTotal" in mem:
        section["mem_total_kb"] = mem["MemTotal"]
    if "MemAvailable" in mem:
        section["mem_available_kb"] = mem["MemAvailable"]
    section["nproc"] = os.cpu_count()
    uname = platform.uname()
    section["kernel_release"] = uname.release
    section["architecture"] = uname.machine
    section["hostname"] = socket.gethostname()
    with contextlib.suppress(OSError, ValueError, IndexError):
        section["uptime_seconds"] = float(
            Path("/proc/uptime").read_text().split()[0],
        )
    with contextlib.suppress(OSError, ValueError):
        section["loadavg"] = [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                section["os_pretty_name"] = line.partition("=")[2].strip().strip('"')
                break
    except OSError:
        pass
    return section


async def _host_storage_pool(config) -> dict:
    """The guardian's own pool measurement, verbatim (asdict of the dataclass)."""
    from genesis.guardian.pool import _detect_pool_name, measure_storage_pool, worst_tier

    status = await measure_storage_pool(config)
    section = dataclasses.asdict(status)
    section["tier"] = worst_tier(status, config.storage_pool) if status.detected else "unknown"
    # The incus pool NAME lives in pool.py's detection helper, not in
    # StoragePoolConfig (which is thresholds-only — reading pool_name off it
    # always yielded None; Codex P2 2026-07-13).
    section["pool_name"] = await _detect_pool_name(config)
    return section


def _parse_incus_limits(config_yaml: str) -> dict:
    """Top-level ``config:`` map ``limits.*`` keys from `incus config show`.

    yaml.safe_load, not a line scan: device-nested ``limits.read/write`` live
    under ``devices:`` — a different top-level map — so they cannot leak in,
    and the parse is immune to incus indentation/quoting changes (a line scan
    hard-coding two-space indents silently returned {} on any format drift —
    review 2026-07-13). Any parse failure degrades to {}.
    """
    import yaml

    try:
        data = yaml.safe_load(config_yaml)
        config_map = (data or {}).get("config") or {}
        return {
            key: str(value)
            for key, value in config_map.items()
            if isinstance(key, str) and key.startswith("limits.")
        }
    except Exception:
        logger.warning("host_profile: incus config parse failed", exc_info=True)
        return {}


async def _host_virt(config) -> dict:
    """Virtualization stack: incus version, our container's cage, nesting."""
    container_name = getattr(config, "container_name", None)

    async def _config_show() -> str | None:
        if not container_name:
            return None
        return await _run("incus", "config", "show", container_name)

    async def _pveversion() -> str | None:
        # Best-effort layer-below probe: absent on a plain VM (Proxmox lives
        # one level down from the guardian).
        if not shutil.which("pveversion"):
            return None
        return await _run("pveversion")

    # Independent read-only probes — run concurrently so the section's worst
    # case is one _PROBE_TIMEOUT, not their sum (gateway budget is 45s total).
    incus_version, config_yaml, detect_virt, pveversion = await asyncio.gather(
        _run("incus", "version"),
        _config_show(),
        _run("systemd-detect-virt"),
        _pveversion(),
    )

    section: dict = {"pve_version": pveversion.strip() if pveversion else None}
    if incus_version:
        for line in incus_version.splitlines():
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "client version":
                section["incus_client_version"] = value.strip()
            elif key == "server version":
                section["incus_server_version"] = value.strip()
    if container_name:
        section["container_name"] = container_name
        if config_yaml:
            section["container_limits"] = _parse_incus_limits(config_yaml)
    if detect_virt:
        section["detect_virt"] = detect_virt.strip()
    section["smartctl_present"] = shutil.which("smartctl") is not None
    return section


async def gather_host_profile(config) -> dict:
    """Gather all three host sections; per-section failures degrade, not raise.

    Sections run concurrently (they share no state) so the gather's worst case
    is its slowest section, not the sum — the gateway kills the process at 45s
    and a SIGKILL loses even the healthy sections' JSON. ``ok`` is False only
    when EVERY section failed: that is indistinguishable from a dead gather,
    so the container degrades the plane instead of rendering three error rows.
    """

    async def _guarded(name: str, awaitable) -> dict:
        try:
            return await awaitable
        except TimeoutError:
            logger.warning("host_profile: %s timed out", name)
            return {"error": f"{name} timed out"}
        except Exception as exc:  # noqa: BLE001 — gather must always emit JSON
            logger.warning("host_profile: %s failed", name, exc_info=True)
            return {"error": repr(exc)}

    async def _system() -> dict:
        return _host_system()

    host_system, host_storage_pool, host_virt = await asyncio.gather(
        _guarded("host_system", _system()),
        _guarded(
            "host_storage_pool",
            asyncio.wait_for(_host_storage_pool(config), _POOL_TIMEOUT),
        ),
        _guarded("host_virt", _host_virt(config)),
    )
    sections = {
        "host_system": host_system,
        "host_storage_pool": host_storage_pool,
        "host_virt": host_virt,
    }
    all_failed = all(set(s) == {"error"} for s in sections.values())
    result: dict = {"ok": not all_failed, "action": "host-profile", **sections}
    if all_failed:
        result["error"] = "all host sections failed"
    return result
