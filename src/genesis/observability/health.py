"""Health probes — standalone async functions for infrastructure checks.

Each probe returns a ProbeResult. Clock injection for testing.
Follows the aiohttp pattern from surplus/compute_availability.py.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiosqlite

from genesis.observability.types import ProbeResult, ProbeStatus


async def probe_db(
    db: aiosqlite.Connection,
    *,
    clock=None,
) -> ProbeResult:
    """Probe the SQLite database with a simple query."""
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        async with db.execute("SELECT 1") as cursor:
            await cursor.fetchone()
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="db",
            status=ProbeStatus.HEALTHY,
            latency_ms=round(latency, 2),
            checked_at=_clock().isoformat(),
        )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="db",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=str(exc),
            checked_at=_clock().isoformat(),
        )


async def probe_qdrant(
    url: str | None = None,
    *,
    timeout_s: int = 3,
    clock=None,
) -> ProbeResult:
    """Probe Qdrant's health endpoint."""
    from genesis.env import qdrant_health_url

    resolved_url = url or qdrant_health_url()
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(resolved_url) as resp,
        ):
            latency = (time.monotonic() - start) * 1000
            if resp.status == 200:
                return ProbeResult(
                    name="qdrant",
                    status=ProbeStatus.HEALTHY,
                    latency_ms=round(latency, 2),
                    checked_at=_clock().isoformat(),
                )
            return ProbeResult(
                name="qdrant",
                status=ProbeStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message=f"HTTP {resp.status}",
                checked_at=_clock().isoformat(),
            )
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="qdrant",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=str(exc),
            checked_at=_clock().isoformat(),
        )


async def probe_ollama(
    url: str | None = None,
    *,
    timeout_s: int = 3,
    clock=None,
) -> ProbeResult:
    """Probe Ollama — checks reachability AND extracts available model names.

    Returns ProbeResult with details={"models": [...]} so that
    health_data.py model mismatch detection can compare configured
    models against actually available ones.
    """
    from genesis.env import ollama_tags_url

    resolved_url = url or ollama_tags_url()
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.get(resolved_url) as resp,
        ):
            latency = (time.monotonic() - start) * 1000
            if resp.status == 200:
                models: list[str] = []
                try:
                    data = await resp.json()
                    models = [m["name"] for m in data.get("models", [])]
                except Exception:
                    pass  # Reachable but can't parse — still HEALTHY
                return ProbeResult(
                    name="ollama",
                    status=ProbeStatus.HEALTHY,
                    latency_ms=round(latency, 2),
                    checked_at=_clock().isoformat(),
                    details={"models": models},
                )
            return ProbeResult(
                name="ollama",
                status=ProbeStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message=f"HTTP {resp.status}",
                checked_at=_clock().isoformat(),
            )
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="ollama",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=str(exc),
            checked_at=_clock().isoformat(),
        )


async def probe_scheduler(
    scheduler,
    *,
    name: str = "scheduler",
    clock=None,
) -> ProbeResult:
    """Probe an APScheduler instance. Healthy if running."""
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        running = scheduler.running
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name=name,
            status=ProbeStatus.HEALTHY if running else ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message="" if running else "Scheduler not running",
            checked_at=_clock().isoformat(),
        )
    except Exception as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name=name,
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=str(exc),
            checked_at=_clock().isoformat(),
        )


async def probe_tmp(
    tmp_path: str = "/tmp",
    *,
    warn_pct: float = 80.0,
    critical_pct: float = 90.0,
    clock=None,
) -> ProbeResult:
    """Probe /tmp filesystem usage.

    /tmp is a 512M tmpfs in this container -- filling it kills CC's shell.
    Returns ProbeResult with details={"pct_used": float, "used_mb": float, "total_mb": float}.
    """
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        st = os.statvfs(tmp_path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = (used / total * 100) if total > 0 else 0.0
        latency = (time.monotonic() - start) * 1000

        if pct >= critical_pct:
            status = ProbeStatus.DOWN
            msg = f"/tmp at {pct:.1f}% ({used / (1024*1024):.0f}/{total / (1024*1024):.0f} MB)"
        elif pct >= warn_pct:
            status = ProbeStatus.DEGRADED
            msg = f"/tmp at {pct:.1f}%"
        else:
            status = ProbeStatus.HEALTHY
            msg = ""

        return ProbeResult(
            name="tmp_usage",
            status=status,
            latency_ms=round(latency, 2),
            message=msg,
            checked_at=_clock().isoformat(),
            details={
                "pct_used": round(pct, 1),
                "used_mb": round(used / (1024 * 1024), 1),
                "total_mb": round(total / (1024 * 1024), 1),
            },
        )
    except OSError as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="tmp_usage",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=f"Cannot stat {tmp_path}: {exc}",
            checked_at=_clock().isoformat(),
        )


async def probe_disk(
    mount_path: str = "/",
    *,
    warn_pct: float = 80.0,
    critical_pct: float = 90.0,
    clock=None,
) -> ProbeResult:
    """Probe root filesystem usage via os.statvfs.

    Returns ProbeResult with details={"pct_used": float, "free_gb": float, "total_gb": float}.
    """
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    try:
        st = os.statvfs(mount_path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = (used / total * 100) if total > 0 else 0.0
        latency = (time.monotonic() - start) * 1000

        if pct >= critical_pct:
            status = ProbeStatus.DOWN
            msg = f"Disk at {pct:.1f}% ({free / (1024**3):.1f} GB free)"
        elif pct >= warn_pct:
            status = ProbeStatus.DEGRADED
            msg = f"Disk at {pct:.1f}%"
        else:
            status = ProbeStatus.HEALTHY
            msg = ""

        return ProbeResult(
            name="disk",
            status=status,
            latency_ms=round(latency, 2),
            message=msg,
            checked_at=_clock().isoformat(),
            details={
                "pct_used": round(pct, 1),
                "free_gb": round(free / (1024**3), 2),
                "total_gb": round(total / (1024**3), 2),
            },
        )
    except OSError as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="disk",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=f"Cannot stat {mount_path}: {exc}",
            checked_at=_clock().isoformat(),
        )


async def probe_guardian(
    heartbeat_path: str | Path | None = None,
    *,
    degraded_threshold_s: float = 120.0,
    down_threshold_s: float = 300.0,
    clock=None,
) -> ProbeResult:
    """Probe Guardian health by reading its heartbeat file.

    The Guardian writes ~/.genesis/guardian_heartbeat.json every check cycle.
    Staleness thresholds:
      <120s  → HEALTHY (Guardian running normally)
      120-300s → DEGRADED (Guardian may be delayed)
      >300s  → DOWN (Guardian appears dead)
      missing → unknown status (Guardian never ran)
    """
    _clock = clock or (lambda: datetime.now(UTC))
    start = time.monotonic()
    path = Path(heartbeat_path) if heartbeat_path else Path.home() / ".genesis" / "guardian_heartbeat.json"

    # Check if Genesis is paused — Guardian stops writing heartbeats when
    # Genesis is paused (by design). Report DEGRADED, not DOWN.
    pause_path = path.parent / "paused.json"
    try:
        pause_data = json.loads(pause_path.read_text())
        if pause_data.get("paused"):
            latency = (time.monotonic() - start) * 1000
            return ProbeResult(
                name="guardian",
                status=ProbeStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message="Guardian paused",
                checked_at=_clock().isoformat(),
                details={"paused": True},
            )
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass  # Not paused or file unreadable — proceed with normal check

    try:
        raw = path.read_text()
        data = json.loads(raw)
        latency = (time.monotonic() - start) * 1000

        ts_str = data.get("timestamp", "")
        if not ts_str:
            return ProbeResult(
                name="guardian",
                status=ProbeStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message="heartbeat file missing timestamp",
                checked_at=_clock().isoformat(),
            )

        heartbeat_time = datetime.fromisoformat(ts_str)
        now = _clock()
        staleness_s = (now - heartbeat_time).total_seconds()

        if staleness_s < degraded_threshold_s:
            return ProbeResult(
                name="guardian",
                status=ProbeStatus.HEALTHY,
                latency_ms=round(latency, 2),
                checked_at=now.isoformat(),
                details={"staleness_s": round(staleness_s, 1)},
            )
        if staleness_s < down_threshold_s:
            return ProbeResult(
                name="guardian",
                status=ProbeStatus.DEGRADED,
                latency_ms=round(latency, 2),
                message=f"Guardian heartbeat is {staleness_s:.0f}s stale",
                checked_at=now.isoformat(),
                details={"staleness_s": round(staleness_s, 1)},
            )
        return ProbeResult(
            name="guardian",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message=f"Guardian heartbeat is {staleness_s:.0f}s stale (>{down_threshold_s:.0f}s)",
            checked_at=now.isoformat(),
            details={"staleness_s": round(staleness_s, 1)},
        )

    except FileNotFoundError:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="guardian",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency, 2),
            message="Guardian heartbeat file not found (Guardian not installed)",
            checked_at=_clock().isoformat(),
        )
    except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
        latency = (time.monotonic() - start) * 1000
        return ProbeResult(
            name="guardian",
            status=ProbeStatus.DEGRADED,
            latency_ms=round(latency, 2),
            message=f"Guardian heartbeat file unreadable: {exc}",
            checked_at=_clock().isoformat(),
        )
