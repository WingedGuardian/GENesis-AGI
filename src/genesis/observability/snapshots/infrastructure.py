"""Infrastructure snapshot — DB, Qdrant, scheduler, disk, container memory, CPU, Ollama."""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.env import ollama_enabled
from genesis.observability.health import (
    probe_db,
    probe_guardian,
    probe_ollama,
    probe_qdrant,
    probe_scheduler,
)
from genesis.observability.types import ProbeStatus
from genesis.routing.types import DegradationLevel, RoutingConfig

if TYPE_CHECKING:
    import aiosqlite

    from genesis.resilience.state import ResilienceStateMachine

logger = logging.getLogger(__name__)

_MEMORY_STAT_PATH = "/sys/fs/cgroup/memory.stat"


def _read_memory_stat() -> dict:
    """Parse cgroup memory.stat into anon/file/kernel breakdown (GiB).

    Returns empty dict if unavailable — callers merge safely via .update().
    """
    try:
        stats: dict[str, int] = {}
        with open(_MEMORY_STAT_PATH) as f:
            for line in f:
                parts = line.split()
                if len(parts) == 2 and parts[0] in ("anon", "file", "kernel"):
                    stats[parts[0]] = int(parts[1])
        if not stats:
            return {}
        to_gb = 1 / (1024**3)
        return {
            "anon_gb": round(stats.get("anon", 0) * to_gb, 2),
            "file_gb": round(stats.get("file", 0) * to_gb, 2),
            "kernel_gb": round(stats.get("kernel", 0) * to_gb, 2),
        }
    except (OSError, ValueError):
        return {}


# Module-level state for delta-based CPU reading (no blocking sleep)
_last_cpu_reading: tuple[int, int, float] | None = None  # (idle, total, monotonic_time)


def _collect_cpu_usage() -> dict:
    """Read CPU usage from /proc/stat delta. No blocking sleep.

    First call stores a baseline and returns None for used_pct.
    Subsequent calls compute delta from the stored baseline.
    """
    global _last_cpu_reading  # noqa: PLW0603
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # Fields: cpu user nice system idle iowait irq softirq steal guest guest_nice
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        now = time.monotonic()

        if _last_cpu_reading is None:
            _last_cpu_reading = (idle, total, now)
            return {"status": "healthy", "used_pct": None, "count": os.cpu_count()}

        prev_idle, prev_total, _prev_time = _last_cpu_reading
        _last_cpu_reading = (idle, total, now)

        delta_idle = idle - prev_idle
        delta_total = total - prev_total
        if delta_total == 0:
            return {"status": "healthy", "used_pct": 0.0, "count": os.cpu_count()}

        used_pct = round((1.0 - delta_idle / delta_total) * 100, 1)
        return {"status": "healthy", "used_pct": used_pct, "count": os.cpu_count()}
    except (OSError, ValueError, IndexError):
        return {"status": "unavailable", "used_pct": None, "count": os.cpu_count()}


async def infrastructure(
    db: aiosqlite.Connection | None,
    routing_config: RoutingConfig | None,
    learning_scheduler: object | None,
    state_machine: ResilienceStateMachine | None,
) -> dict:
    infra = {}

    if db:
        try:
            result = await probe_db(db)
            infra["genesis.db"] = {
                "status": str(result.status),
                "latency_ms": result.latency_ms,
            }
            _update_memory_axis(state_machine, result.status)
        except Exception as exc:
            infra["genesis.db"] = {"status": "error", "error": str(exc)}
            _update_memory_axis(state_machine, ProbeStatus.DOWN)
    else:
        infra["genesis.db"] = {"status": "error", "error": "no database connection"}

    try:
        result = await probe_qdrant()
        infra["qdrant"] = {
            "status": str(result.status),
            "latency_ms": result.latency_ms,
        }
        _update_embedding_axis(state_machine, result.status)
    except Exception as exc:
        infra["qdrant"] = {"status": "error", "error": str(exc)}
        _update_embedding_axis(state_machine, ProbeStatus.DOWN)

    if learning_scheduler:
        try:
            result = await probe_scheduler(learning_scheduler)
            infra["scheduler"] = {"status": str(result.status)}
        except Exception as exc:
            infra["scheduler"] = {"status": "error", "error": str(exc)}
    elif db:
        try:
            cursor = await db.execute(
                "SELECT MAX(last_run) FROM job_health"
            )
            row = await cursor.fetchone()
            if row and row[0]:
                last_run = datetime.fromisoformat(row[0])
                age_s = (datetime.now(UTC) - last_run).total_seconds()
                if age_s < 600:
                    infra["scheduler"] = {"status": "healthy"}
                else:
                    infra["scheduler"] = {"status": "degraded", "error": f"last job ran {int(age_s)}s ago"}
            else:
                infra["scheduler"] = {"status": "unknown", "error": "no job history"}
        except Exception as exc:
            infra["scheduler"] = {"status": "error", "error": str(exc)}
    else:
        infra["scheduler"] = {"status": "unknown", "error": "no scheduler or DB available"}

    infra["cpu"] = _collect_cpu_usage()

    try:
        usage = shutil.disk_usage("/")
        free_pct = round(usage.free / usage.total * 100, 1)
        infra["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "free_pct": free_pct,
        }
    except OSError as exc:
        infra["disk"] = {"status": "error", "error": str(exc)}

    try:
        from genesis.observability.service_status import collect_tmpfs_usage

        infra["tmpfs"] = collect_tmpfs_usage()
    except (ImportError, OSError) as exc:
        infra["tmpfs"] = {"status": "error", "error": str(exc)}

    try:
        from genesis.observability.service_status import probe_qdrant_collections

        infra["qdrant_collections"] = await probe_qdrant_collections()
    except Exception as exc:
        infra["qdrant_collections"] = {"status": "error", "error": str(exc)}

    try:
        from genesis.autonomy.watchdog import get_container_memory

        mem = get_container_memory()
        if mem and mem[1] > 0:
            current, limit = mem
            pct = current / limit
            mem_info: dict = {
                "status": "healthy" if pct < 0.85 else ("degraded" if pct < 0.95 else "down"),
                "current_gb": round(current / (1024**3), 1),
                "limit_gb": round(limit / (1024**3), 1),
                "used_pct": round(pct * 100, 1),
            }
            # Decompose into anon/file/kernel — "83% memory" is meaningless
            # without knowing what's reclaimable (incident 2026-04-08)
            mem_info.update(_read_memory_stat())
            infra["container_memory"] = mem_info
        else:
            infra["container_memory"] = {"status": "unavailable"}
    except Exception as exc:
        infra["container_memory"] = {"status": "error", "error": str(exc)}

    try:
        result = await probe_guardian()
        infra["guardian"] = {
            "status": str(result.status),
            "latency_ms": result.latency_ms,
        }
        if result.message:
            infra["guardian"]["message"] = result.message
        if result.details:
            infra["guardian"].update(result.details)
    except Exception as exc:
        infra["guardian"] = {"status": "error", "error": str(exc)}

    if ollama_enabled():
        try:
            result = await probe_ollama()
            infra["ollama"] = {
                "status": str(result.status),
                "latency_ms": result.latency_ms,
            }
            if (
                routing_config
                and hasattr(result, "details")
                and isinstance(result.details, dict)
            ):
                actual_models = set(result.details.get("models", []))
                if actual_models:
                    missing = []
                    for name, cfg in routing_config.providers.items():
                        if cfg.provider_type == "ollama" and cfg.model_id not in actual_models:
                            missing.append({"provider": name, "model": cfg.model_id})
                    if missing:
                        infra["ollama"]["missing_models"] = missing
        except (ConnectionError, TimeoutError, OSError) as exc:
            infra["ollama"] = {"status": "error", "error": str(exc)}
        except Exception as exc:
            infra["ollama"] = {"status": "error", "error": str(exc)}

    return infra


def resilience_state(
    breakers,  # CircuitBreakerRegistry | None
    state_machine,  # ResilienceStateMachine | None
) -> str:
    """Compute resilience state from circuit breaker registry."""
    if not breakers:
        return "not configured"
    try:
        level = breakers.compute_degradation_level()
        _update_cloud_axis(state_machine, level)
        return level.value
    except Exception:
        logger.error("Failed to compute degradation level", exc_info=True)
        return "error"


def _update_cloud_axis(state_machine, level: DegradationLevel) -> None:
    if state_machine is None:
        return
    from genesis.resilience.state import CloudStatus

    _map = {
        DegradationLevel.NORMAL: CloudStatus.NORMAL,
        DegradationLevel.FALLBACK: CloudStatus.FALLBACK,
        DegradationLevel.REDUCED: CloudStatus.REDUCED,
        DegradationLevel.ESSENTIAL: CloudStatus.ESSENTIAL,
        DegradationLevel.LOCAL_COMPUTE_DOWN: CloudStatus.OFFLINE,
        DegradationLevel.MEMORY_IMPAIRED: CloudStatus.REDUCED,
    }
    state_machine.update_cloud(_map.get(level, CloudStatus.OFFLINE))


def _update_memory_axis(state_machine, probe_status: ProbeStatus) -> None:
    if state_machine is None:
        return
    from genesis.resilience.state import MemoryStatus

    _map = {
        ProbeStatus.HEALTHY: MemoryStatus.NORMAL,
        ProbeStatus.DEGRADED: MemoryStatus.FTS_ONLY,
        ProbeStatus.DOWN: MemoryStatus.DOWN,
    }
    state_machine.update_memory(_map.get(probe_status, MemoryStatus.DOWN))


def _update_embedding_axis(state_machine, probe_status: ProbeStatus) -> None:
    if state_machine is None:
        return
    from genesis.resilience.state import EmbeddingStatus

    _map = {
        ProbeStatus.HEALTHY: EmbeddingStatus.NORMAL,
        ProbeStatus.DEGRADED: EmbeddingStatus.QUEUED,
        ProbeStatus.DOWN: EmbeddingStatus.UNAVAILABLE,
    }
    state_machine.update_embedding(_map.get(probe_status, EmbeddingStatus.UNAVAILABLE))
