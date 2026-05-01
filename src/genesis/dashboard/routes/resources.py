"""System resources routes — CPU, memory, disk."""

from __future__ import annotations

import logging
import shutil

from flask import jsonify

from genesis.dashboard._blueprint import blueprint

logger = logging.getLogger(__name__)


@blueprint.route("/api/genesis/resources")
def system_resources():
    """Return CPU, memory, and disk usage for the container."""
    result = {}

    # CPU — delta-based from infrastructure snapshot
    try:
        from genesis.observability.snapshots.infrastructure import _collect_cpu_usage

        result["cpu"] = _collect_cpu_usage()
    except Exception:
        logger.error("CPU probe failed", exc_info=True)
        result["cpu"] = {"status": "unavailable", "used_pct": None}

    # Container memory — anon+kernel (non-reclaimable) for status decisions
    try:
        from genesis.autonomy.watchdog import get_container_anon_memory, get_container_memory

        anon_mem = get_container_anon_memory()
        total_mem = get_container_memory()
        if anon_mem and anon_mem[1] > 0:
            anon_kernel, limit = anon_mem
            anon_pct = round(anon_kernel / limit * 100, 1)
            total_pct = round(total_mem[0] / limit * 100, 1) if total_mem and total_mem[1] > 0 else anon_pct
            result["memory"] = {
                "status": "healthy" if anon_pct < 85 else ("degraded" if anon_pct < 95 else "critical"),
                "used_gb": round((total_mem[0] if total_mem else anon_kernel) / (1024**3), 1),
                "total_gb": round(limit / (1024**3), 1),
                "used_pct": total_pct,
                "anon_pct": anon_pct,
            }
        else:
            result["memory"] = {"status": "unavailable"}
    except Exception:
        logger.error("Container memory probe failed", exc_info=True)
        result["memory"] = {"status": "unavailable"}

    # Disk usage
    try:
        usage = shutil.disk_usage("/home/ubuntu")
        result["disk"] = {
            "status": "healthy" if usage.free / usage.total > 0.15 else "degraded",
            "used_gb": round(usage.used / (1024**3), 1),
            "total_gb": round(usage.total / (1024**3), 1),
            "used_pct": round(usage.used / usage.total * 100, 1),
        }
    except OSError:
        result["disk"] = {"status": "unavailable"}

    return jsonify(result)
