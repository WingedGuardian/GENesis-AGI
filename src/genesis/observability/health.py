"""Health probes — standalone async functions for infrastructure checks.

Each probe returns a ProbeResult. Clock injection for testing.
Follows the aiohttp pattern from surplus/compute_availability.py.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import aiosqlite

from genesis.observability.types import ProbeResult, ProbeStatus

logger = logging.getLogger(__name__)

# Sentinel for probe_guardian's guardian_remote parameter.
# Allows callers to explicitly pass None (skip remote) vs not passing
# anything (auto-load from config).
_GUARDIAN_REMOTE_UNSET = object()

# Lazy-loaded GuardianRemote from config file (loaded once, cached).
_guardian_remote_from_config: object | None = None
_guardian_remote_config_checked: bool = False

# SSH probe cache: {host_ip: (monotonic_timestamp, ProbeResult)}
_guardian_ssh_cache: dict[str, tuple[float, ProbeResult]] = {}
_GUARDIAN_SSH_TTL = 60.0


def _load_guardian_remote_from_config() -> object | None:
    """Lazy-load a GuardianRemote from ~/.genesis/guardian_remote.yaml.

    Cached at module level — reads the file at most once per process lifetime.
    Returns None if config doesn't exist or is incomplete.
    """
    global _guardian_remote_from_config, _guardian_remote_config_checked
    if _guardian_remote_config_checked:
        return _guardian_remote_from_config
    _guardian_remote_config_checked = True

    config_path = Path.home() / ".genesis" / "guardian_remote.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        config = yaml.safe_load(config_path.read_text()) or {}
        host_ip = config.get("host_ip", "")
        host_user = config.get("host_user", "")
        ssh_key = config.get("ssh_key", "")
        if host_ip and host_user:
            from genesis.guardian.remote import GuardianRemote

            _guardian_remote_from_config = GuardianRemote(
                host_ip=host_ip,
                host_user=host_user,
                key_path=ssh_key or "~/.ssh/genesis_guardian_ed25519",
            )
            logger.debug("Loaded guardian remote from config: %s@%s", host_user, host_ip)
    except Exception:
        logger.warning("Failed to load guardian remote config", exc_info=True)
    return _guardian_remote_from_config


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
    guardian_remote=_GUARDIAN_REMOTE_UNSET,
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

    When the heartbeat file is missing (Guardian runs on a remote host),
    falls back to SSH probe via guardian_remote. If guardian_remote is
    _GUARDIAN_REMOTE_UNSET (default), auto-loads from guardian_remote.yaml.
    Pass guardian_remote=None to skip SSH fallback entirely (useful in tests).
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

        # Resolve remote: auto-load from config if sentinel, skip if None
        remote = guardian_remote
        if remote is _GUARDIAN_REMOTE_UNSET:
            remote = _load_guardian_remote_from_config()

        if remote is not None:
            return await _probe_guardian_ssh(remote, latency, _clock)

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


async def collect_probe_results(
    db=None,
    *,
    scheduler=None,
    guardian_remote=_GUARDIAN_REMOTE_UNSET,
) -> dict[str, ProbeResult]:
    """Run all infrastructure probes and return results keyed by probe name.

    Used by the remediation registry to check which probes are failing.
    Each probe runs independently — a failure in one does not block others.
    """
    import asyncio

    results: dict[str, ProbeResult] = {}

    async def _safe(name: str, coro) -> None:
        try:
            results[name] = await asyncio.wait_for(coro, timeout=10.0)
        except Exception as exc:
            results[name] = ProbeResult(
                name=name,
                status=ProbeStatus.DOWN,
                latency_ms=0,
                message=f"Probe error: {exc}",
                checked_at=datetime.now(UTC).isoformat(),
            )

    from genesis.env import ollama_enabled

    tasks = [
        _safe("qdrant", probe_qdrant()),
        *([] if not ollama_enabled() else [_safe("ollama", probe_ollama())]),
        _safe("tmp_usage", probe_tmp()),
        _safe("disk", probe_disk()),
        _safe("guardian", probe_guardian(guardian_remote=guardian_remote)),
    ]
    if db is not None:
        tasks.append(_safe("db", probe_db(db)))
    if scheduler is not None:
        tasks.append(_safe("awareness_tick", probe_scheduler(scheduler)))

    await asyncio.gather(*tasks)
    return results


async def _probe_guardian_ssh(remote, latency_ms: float, clock) -> ProbeResult:
    """Probe Guardian health via SSH with a TTL cache.

    Maps remote.status() current_state to ProbeStatus:
      running → HEALTHY
      paused  → DEGRADED
      anything else (unreachable, unknown) → DOWN
    """
    _clock = clock or (lambda: datetime.now(UTC))
    host_ip = getattr(remote, "host_ip", "unknown")

    # Check TTL cache
    now = time.monotonic()
    cached = _guardian_ssh_cache.get(host_ip)
    if cached is not None:
        cache_time, cache_result = cached
        if (now - cache_time) < _GUARDIAN_SSH_TTL:
            return cache_result

    try:
        status_data = await remote.status()
        state = status_data.get("current_state", "unknown")

        state_map = {
            "running": (ProbeStatus.HEALTHY, "Guardian running on remote host"),
            "paused": (ProbeStatus.DEGRADED, "Guardian paused on remote host"),
        }
        probe_status, message = state_map.get(
            state, (ProbeStatus.DOWN, f"Guardian remote state: {state}")
        )
        result = ProbeResult(
            name="guardian",
            status=probe_status,
            latency_ms=round(latency_ms, 2),
            message=message,
            checked_at=_clock().isoformat(),
            details={"remote": True, "current_state": state},
        )
    except Exception:
        logger.warning("Guardian SSH probe failed", exc_info=True)
        result = ProbeResult(
            name="guardian",
            status=ProbeStatus.DOWN,
            latency_ms=round(latency_ms, 2),
            message="Guardian remote probe failed (SSH error)",
            checked_at=_clock().isoformat(),
            details={"remote": True, "ssh_error": True},
        )

    # Update cache
    _guardian_ssh_cache[host_ip] = (now, result)
    return result
