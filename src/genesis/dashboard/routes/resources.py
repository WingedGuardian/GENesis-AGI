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

    # Container memory — cgroup-based
    try:
        from genesis.autonomy.watchdog import get_container_memory

        mem = get_container_memory()
        if mem and mem[1] > 0:
            current, limit = mem
            pct = round(current / limit * 100, 1)
            result["memory"] = {
                "status": "healthy" if pct < 85 else ("degraded" if pct < 95 else "critical"),
                "used_gb": round(current / (1024**3), 1),
                "total_gb": round(limit / (1024**3), 1),
                "used_pct": pct,
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
