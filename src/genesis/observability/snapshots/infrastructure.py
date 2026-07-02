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
    probe_ambient_health,
    probe_db,
    probe_guardian,
    probe_ollama,
    probe_qdrant,
    probe_scheduler,
    probe_wal,
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


def _read_mem_available() -> int | None:
    """Read MemAvailable from /proc/meminfo (bytes).

    MemAvailable is the kernel's estimate of memory available for new
    allocations without swapping. It accounts for reclaimable page cache
    and slab, making it the right metric for OOM risk assessment (unlike
    cgroup memory.current which includes non-reclaimable cache).

    Returns None if unavailable.
    """
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    # Format: "MemAvailable:   22612356 kB"
                    return int(line.split()[1]) * 1024  # kB → bytes
    except (OSError, ValueError, IndexError):
        pass
    return None


_PSI_CPU_PATH = "/sys/fs/cgroup/cpu.pressure"
_PSI_MEMORY_PATH = "/sys/fs/cgroup/memory.pressure"


def _read_psi(path: str) -> dict:
    """Parse a cgroup PSI (pressure stall information) file into some/full avgs.

    PSI is the honest "is contention actually HURTING" metric: `full.avgN` is
    the % of wall-clock over the last N s during which *all* non-idle tasks were
    stalled waiting on this resource. It distinguishes real pressure from benign
    reclaim — e.g. a climbing memory.events 'high' count with memory.pressure
    full.avg=0 is harmless page-cache reclaim, not a problem.

    Returns {} if unavailable (kernel without CONFIG_PSI, missing file). Mirrors
    genesis.guardian.health_signals.parse_psi_content; kept local to avoid
    importing the host-side guardian module into the in-server snapshot path.
    """
    try:
        out: dict[str, float] = {}
        with open(path) as f:
            for line in f:
                parts = line.split()
                if not parts or parts[0] not in ("some", "full"):
                    continue
                prefix = parts[0]
                for part in parts[1:]:
                    key, sep, val = part.partition("=")
                    if sep and key in ("avg10", "avg60", "avg300"):
                        try:
                            out[f"{prefix}_{key}"] = float(val)
                        except ValueError:
                            continue
        return out
    except (OSError, ValueError):
        return {}


# Health-status ranking (higher = worse) for composing multiple signals.
_STATUS_RANK = {"healthy": 0, "unknown": 1, "unavailable": 1, "degraded": 2, "down": 3, "error": 3}


def _worse_status(a: str, b: str) -> str:
    """Return the worse (higher-ranked) of two health statuses."""
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


# Container memory PSI thresholds (full.avg60 %). Sustained memory stall is REAL
# pressure even when anon% is moderate; benign cache reclaim reads ~0.
_MEM_PSI_DEGRADED_PCT = 10.0
_MEM_PSI_ERROR_PCT = 30.0


def memory_status(anon_frac: float, pressure: dict) -> str:
    """Container-memory health = worse of anon% (OOM guard, non-reclaimable) and
    sustained memory PSI (catches reclaim that is actually stalling work).

    anon_frac is a 0..1 fraction of the cgroup limit.
    """
    anon_status = "healthy" if anon_frac < 0.85 else ("degraded" if anon_frac < 0.95 else "down")
    full60 = pressure.get("full_avg60", 0.0)
    psi_status = (
        "down" if full60 >= _MEM_PSI_ERROR_PCT
        else ("degraded" if full60 >= _MEM_PSI_DEGRADED_PCT else "healthy")
    )
    return _worse_status(anon_status, psi_status)


# CPU utilization % thresholds — plain used_pct (NOT loadavg; loadavg counts I/O
# wait and misrepresents CPU). Only SUSTAINED high utilization degrades; the
# normal box hum (~45%) stays healthy.
_CPU_DEGRADED_PCT = 80.0
_CPU_ERROR_PCT = 95.0


def _cpu_status(used_pct: float | None) -> str:
    """Map CPU utilization % → health status. None (baseline/no data) → healthy."""
    if used_pct is None:
        return "healthy"
    if used_pct >= _CPU_ERROR_PCT:
        return "error"
    if used_pct >= _CPU_DEGRADED_PCT:
        return "degraded"
    return "healthy"


# Module-level state for delta-based CPU reading (no blocking sleep)
_last_cpu_reading: tuple[int, int, float] | None = None  # (idle, total, monotonic_time)


def _collect_cpu_usage() -> dict:
    """Read CPU usage from /proc/stat delta + CPU pressure (PSI). No blocking sleep.

    First call stores a baseline and returns None for used_pct. Subsequent calls
    compute the delta from the stored baseline. Status derives from used_pct
    (plain CPU utilization %), NOT loadavg — loadavg counts I/O wait and
    misrepresents CPU. cpu.pressure (PSI) is surfaced as the honest "is CPU
    contention actually stalling work" signal.
    """
    global _last_cpu_reading  # noqa: PLW0603
    pressure = _read_psi(_PSI_CPU_PATH)
    count = os.cpu_count()
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()
        # Fields: cpu user nice system idle iowait irq softirq steal guest guest_nice
        idle = int(parts[4])
        total = sum(int(p) for p in parts[1:])
        now = time.monotonic()

        if _last_cpu_reading is None:
            _last_cpu_reading = (idle, total, now)
            return {"status": "healthy", "used_pct": None, "count": count, "pressure": pressure}

        prev_idle, prev_total, _prev_time = _last_cpu_reading
        _last_cpu_reading = (idle, total, now)

        delta_idle = idle - prev_idle
        delta_total = total - prev_total
        if delta_total == 0:
            return {"status": "healthy", "used_pct": 0.0, "count": count, "pressure": pressure}

        used_pct = round((1.0 - delta_idle / delta_total) * 100, 1)
        return {"status": _cpu_status(used_pct), "used_pct": used_pct, "count": count, "pressure": pressure}
    except (OSError, ValueError, IndexError):
        return {"status": "unavailable", "used_pct": None, "count": count, "pressure": pressure}


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

    # WAL size — only meaningful when the DB is connected. Early signal of
    # DB-lock pressure (a long-lived reader pinning an old snapshot bloats the
    # -wal sidecar). Attached to the genesis.db probe so the dashboard surfaces
    # it without a new top-level entry. Skipped when db is None — a "WAL: 0 MB"
    # readout next to a "no database connection" error would mislead operators.
    if db:
        try:
            wal_result = await probe_wal()
            if wal_result.details:
                infra["genesis.db"]["wal_mb"] = wal_result.details.get("wal_mb")
            infra["genesis.db"]["wal_status"] = str(wal_result.status)
        except Exception as exc:
            logger.warning("WAL probe failed: %s", exc)

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
        from genesis.observability.service_status import collect_cc_tmp_usage

        infra["cc_tmp"] = collect_cc_tmp_usage()
    except (ImportError, OSError) as exc:
        infra["cc_tmp"] = {"status": "error", "error": str(exc)}

    try:
        from genesis.observability.cc_slots import enumerate_cc_slots

        infra["cc_slots"] = enumerate_cc_slots()
    except Exception:
        infra["cc_slots"] = []

    try:
        from genesis.observability.service_status import probe_qdrant_collections

        infra["qdrant_collections"] = await probe_qdrant_collections()
    except Exception as exc:
        infra["qdrant_collections"] = {"status": "error", "error": str(exc)}

    try:
        from genesis.autonomy.watchdog import get_container_anon_memory, get_container_memory

        anon_mem = get_container_anon_memory()
        total_mem = get_container_memory()
        if anon_mem and anon_mem[1] > 0:
            anon_kernel, limit = anon_mem
            anon_pct = anon_kernel / limit
            mem_pressure = _read_psi(_PSI_MEMORY_PATH)
            # Status = worse of anon% (OOM guard, non-reclaimable) and sustained
            # memory PSI. Total cgroup usage (memory.current) is shown for
            # reference but NOT used for health — it includes reclaimable page
            # cache that inflates the metric, and whose reclaim is benign.
            mem_info: dict = {
                "status": memory_status(anon_pct, mem_pressure),
                "current_gb": round((total_mem[0] if total_mem else anon_kernel) / (1024**3), 1),
                "limit_gb": round(limit / (1024**3), 1),
                "used_pct": round((total_mem[0] / limit * 100) if total_mem and total_mem[1] > 0 else anon_pct * 100, 1),
                "anon_pct": round(anon_pct * 100, 1),
                "pressure": mem_pressure,
            }
            # MemAvailable from /proc/meminfo — the kernel's estimate of
            # memory available for new allocations without swapping. This is
            # the metric that matters for OOM risk, not used_pct (which
            # includes reclaimable file cache and inflates the number).
            available_bytes = _read_mem_available()
            if available_bytes is not None:
                mem_info["available_gb"] = round(available_bytes / (1024**3), 1)
                # Cap at 100% — on non-namespaced hosts, MemAvailable may
                # exceed the cgroup limit, producing a nonsensical ratio.
                mem_info["available_pct"] = round(min(available_bytes / limit * 100, 100.0), 1)
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

    # Ambient-capture edge bridge (observability only). Omitted entirely when no
    # ambient edge is configured — an absent ambient edge is not a fault.
    try:
        result = await probe_ambient_health()
        if result is not None:
            infra["ambient"] = {
                "status": str(result.status),
                "latency_ms": result.latency_ms,
            }
            if result.message:
                infra["ambient"]["message"] = result.message
            if result.details:
                infra["ambient"].update(result.details)
    except Exception as exc:
        infra["ambient"] = {"status": "error", "error": str(exc)}

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


def resilience_state_detail(
    breakers,  # CircuitBreakerRegistry | None
    state_machine,  # ResilienceStateMachine | None
) -> dict:
    """Compute resilience state with human-readable detail."""
    level = resilience_state(breakers, state_machine)
    summary = ""
    if breakers:
        try:
            down = [
                name for name, cb in breakers._breakers.items()
                if not cb.is_available()
            ]
            if down:
                summary = f"Providers down: {', '.join(sorted(down))}"
        except Exception:
            pass
    return {"level": level, "summary": summary}


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
