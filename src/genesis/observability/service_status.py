"""Service status collector — queries systemd units and watchdog state.

Provides infrastructure-level visibility into the Genesis service stack:
genesis-server (or bridge fallback), watchdog timer, and watchdog health
state. Used by HealthDataService.snapshot() to surface service health in
the dashboard and MCP tools.

All subprocess calls use timeout=5s to avoid blocking the health snapshot.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from genesis.util.systemd import systemctl_env

logger = logging.getLogger(__name__)

_BRIDGE_LOCK = Path.home() / ".genesis" / "bridge.lock"
_WATCHDOG_STATE = Path.home() / ".genesis" / "watchdog_state.json"

# Qdrant collections that must exist for Genesis to function.
_EXPECTED_QDRANT_COLLECTIONS = {"episodic_memory", "knowledge_base"}

_SYSTEMD_UNITS = {
    "watchdog_timer": "genesis-watchdog.timer",
    "tmp_watchgod": "genesis-tmp-watchgod.service",
}

_TMP_WATCHGOD_STATE = Path.home() / ".genesis" / "watchgod_state.json"


def _detect_genesis_service() -> tuple[str, str]:
    """Detect which Genesis service is active.

    Returns (unit_name, display_label) — prefers genesis-server,
    falls back to genesis-bridge for legacy deployments.
    """
    for unit, label in [
        ("genesis-server.service", "Server"),
        ("genesis-bridge.service", "Bridge"),
    ]:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-enabled", unit],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            if result.returncode == 0:
                return unit, label
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    # Nothing enabled — check if either is at least loaded
    for unit, label in [
        ("genesis-server.service", "Server"),
        ("genesis-bridge.service", "Bridge"),
    ]:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", unit],
                capture_output=True, text=True, timeout=5, env=systemctl_env(),
            )
            if result.stdout.strip() == "active":
                return unit, label
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    return "genesis-server.service", "Server"


def query_systemd_unit(unit_name: str) -> dict:
    """Query a systemd user unit's properties. Returns dict or empty on failure."""
    try:
        result = subprocess.run(
            [
                "systemctl", "--user", "show", unit_name,
                "--property=ActiveState,SubState,NRestarts,ExecMainStartTimestamp",
            ],
            capture_output=True, text=True, timeout=5, env=systemctl_env(),
        )
        if result.returncode != 0:
            return {}
        props = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                key, _, val = line.partition("=")
                props[key.strip()] = val.strip()
        return props
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("Failed to query systemd unit %s: %s", unit_name, exc)
        return {}


def _bridge_pid_alive() -> tuple[int | None, bool]:
    """Read bridge PID from lock file and check if process is alive.

    Returns (pid, is_alive). pid is None if lock file missing/unreadable.
    """
    try:
        content = _BRIDGE_LOCK.read_text().strip()
        if not content:
            return None, False
        pid = int(content)
        os.kill(pid, 0)  # signal 0 = existence check
        return pid, True
    except (OSError, ValueError):
        return None, False


def parse_systemd_timestamp(raw: str) -> str | None:
    """Parse systemd's timestamp format into ISO 8601, or None.

    systemd outputs local time with a timezone abbreviation, e.g.
    ``Sat 2026-04-04 20:18:30 EDT``.  Python's ``%Z`` in strptime does
    NOT reliably convert abbreviations to tzinfo — it produces a naive
    datetime.  We use ``dateutil.parser`` when available (handles
    abbreviations correctly), falling back to the system local timezone.
    """
    if not raw or raw == "n/a":
        return None
    try:
        # Strip weekday prefix if present (e.g. "Sat ")
        parts = raw.split(" ", 1)
        if len(parts) == 2 and len(parts[0]) <= 3:
            raw = parts[1]

        # Attempt dateutil first — it handles EDT/EST/PST/etc. correctly
        try:
            from dateutil import parser as _du_parser

            dt = _du_parser.parse(raw)
            if dt.tzinfo is not None:
                return dt.astimezone(UTC).isoformat()
        except Exception:
            pass

        # Fallback: parse date+time, assume system local timezone
        naive = datetime.strptime(raw.rsplit(" ", 1)[0], "%Y-%m-%d %H:%M:%S")
        local_dt = naive.astimezone()  # attaches system local tz
        return local_dt.astimezone(UTC).isoformat()
    except (ValueError, IndexError):
        return None


def compute_uptime_seconds(start_ts: str | None) -> float | None:
    """Compute seconds since a start timestamp."""
    if not start_ts:
        return None
    try:
        started = datetime.fromisoformat(start_ts)
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return round((datetime.now(UTC) - started).total_seconds(), 1)
    except (ValueError, TypeError):
        return None


def _load_watchdog_state() -> dict:
    """Load watchdog state file. Returns default dict on failure."""
    try:
        if _WATCHDOG_STATE.exists():
            data = json.loads(_WATCHDOG_STATE.read_text())
            return {
                "consecutive_failures": data.get("consecutive_failures", 0),
                "last_reason": data.get("last_reason"),
                "next_attempt_after": data.get("next_attempt_after"),
                "last_check_at": data.get("last_check_at"),
            }
    except (json.JSONDecodeError, OSError):
        logger.warning("Failed to read watchdog state from %s", _WATCHDOG_STATE, exc_info=True)
    return {
        "consecutive_failures": 0,
        "last_reason": None,
        "next_attempt_after": None,
        "last_check_at": None,
    }


async def probe_qdrant_collections(
    url: str | None = None,
    *,
    timeout_s: int = 5,
) -> dict:
    """Check expected Qdrant collections and their point counts.

    Returns {"status": ..., "collections": [{"name": ..., "points": N}], "missing": [...]}
    """
    import aiohttp

    if url is None:
        from genesis.env import qdrant_collections_url

        url = qdrant_collections_url()

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return {"status": "error", "message": f"HTTP {resp.status}"}
                data = await resp.json()

            existing_names = {c["name"] for c in data.get("result", {}).get("collections", [])}
            missing = sorted(_EXPECTED_QDRANT_COLLECTIONS - existing_names)

            # Fetch point counts per collection
            collections = []
            total_points = 0
            for name in sorted(existing_names):
                points = 0
                try:
                    base = url.rsplit("/collections", 1)[0]
                    async with session.get(f"{base}/collections/{name}") as info_resp:
                        if info_resp.status == 200:
                            info = (await info_resp.json()).get("result", {})
                            points = info.get("points_count", 0)
                except Exception:
                    pass  # point count is best-effort
                collections.append({"name": name, "points": points})
                total_points += points

            return {
                "status": "healthy" if not missing else "degraded",
                "collections": collections,
                "missing": missing,
                "total_points": total_points,
            }
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def collect_service_status() -> dict:
    """Collect status of all Genesis systemd services and watchdog state.

    Returns a dict suitable for inclusion in HealthDataService.snapshot().
    """
    result = {}

    # Genesis service (auto-detect: server or bridge)
    svc_unit, svc_label = _detect_genesis_service()
    svc_props = query_systemd_unit(svc_unit)
    bridge_pid, pid_alive = _bridge_pid_alive()
    start_ts = parse_systemd_timestamp(svc_props.get("ExecMainStartTimestamp", ""))
    result["bridge"] = {
        "active_state": svc_props.get("ActiveState", "unknown"),
        "sub_state": svc_props.get("SubState", "unknown"),
        "pid": bridge_pid,
        "pid_alive": pid_alive,
        "uptime_seconds": compute_uptime_seconds(start_ts),
        "restart_count": int(svc_props.get("NRestarts", 0)),
        "service_label": svc_label,
        "service_unit": svc_unit,
    }

    # Watchdog timer
    timer_props = query_systemd_unit(_SYSTEMD_UNITS["watchdog_timer"])
    result["watchdog_timer"] = {
        "active_state": timer_props.get("ActiveState", "unknown"),
        "sub_state": timer_props.get("SubState", "unknown"),
    }

    # Watchdog state (backoff counters, last check)
    wd_state = _load_watchdog_state()
    in_backoff = False
    if wd_state["next_attempt_after"]:
        import time
        in_backoff = time.time() < wd_state["next_attempt_after"]
    result["watchdog"] = {
        "consecutive_failures": wd_state["consecutive_failures"],
        "last_reason": wd_state["last_reason"],
        "in_backoff": in_backoff,
        "last_check_at": wd_state.get("last_check_at"),
    }

    # Tmp watchgod service
    watchgod_props = query_systemd_unit(_SYSTEMD_UNITS["tmp_watchgod"])
    result["tmp_watchgod"] = {
        "active_state": watchgod_props.get("ActiveState", "unknown"),
        "sub_state": watchgod_props.get("SubState", "unknown"),
    }

    return result


def collect_cc_tmp_usage() -> dict:
    """Read watchgod state file for CC temp usage. Returns dict with tier, usage, budget."""
    try:
        if not _TMP_WATCHGOD_STATE.exists():
            return {"status": "unavailable", "error": "watchgod not running"}
        data = json.loads(_TMP_WATCHGOD_STATE.read_text())
        cc = data.get("cc_tmp", {})
        sys_tmp = data.get("system_tmp", {})
        return {
            "cc_tier": cc.get("tier", "unknown"),
            "cc_used_mb": cc.get("used_mb", 0),
            "cc_budget_mb": cc.get("budget_mb", 500),
            "cc_sacred_mb": cc.get("sacred_mb", 150),
            "sys_tier": sys_tmp.get("tier", "unknown"),
            "sys_used_pct": sys_tmp.get("used_pct", 0),
            "poll_at": data.get("poll_at", ""),
        }
    except (json.JSONDecodeError, OSError):
        return {"status": "error", "error": "cannot read watchgod state"}

