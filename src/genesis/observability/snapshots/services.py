"""Services and MCP status snapshot."""

from __future__ import annotations

import asyncio as _aio
import importlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_MCP_CRASH_DIR = Path.home() / ".genesis" / "mcp_crashes"


def _collect_service_status_safe() -> dict:
    """Subprocess-heavy systemd/watchdog collection — safe to run in a worker thread.

    This is the ONLY loop-blocking part of the services snapshot (up to ~7
    ``systemctl`` subprocesses @ 5s each). It touches no shared event-loop state,
    so ``services_async()`` offloads it via ``asyncio.to_thread``.
    """
    try:
        from genesis.observability.service_status import collect_service_status

        return collect_service_status()
    except Exception:
        logger.warning("Failed to collect service status", exc_info=True)
        return {"status": "unknown"}


def _finish_services(result: dict) -> dict:
    """Augment a base service-status dict with host framework + sentinel state.

    MUST run on the event loop. It reads the live ``sentinel.state``, which the
    sentinel dispatcher mutates in place, field-by-field, on the loop; single-
    threaded execution (no ``await`` mid-read) is what makes that read atomic.
    Do NOT offload this to a worker thread — that would open a torn-read window
    (e.g. a fresh ``current_state`` paired with a stale ``last_heartbeat_at``).
    Host-framework ``detect()`` is near-free (empty detector registry, 30s cache),
    so it stays here too rather than warranting its own offload.
    """
    # Deploy-aware: update.sh/restore intentionally stop genesis-server during a
    # deploy. Reuse the SAME signal the watchdog honors (env.update_in_progress —
    # PID-liveness + phase-gated + 4h-bounded, fail-open to False) so the dashboard
    # shows "deploying" instead of a false "degraded/stale". The visible window is
    # the post-restart health_check phase (fresh server, empty sentinel heartbeat →
    # would read stale); the full server-down window is correctly invisible (no
    # server to serve /health).
    try:
        from genesis.env import update_in_progress

        deploying = bool(update_in_progress())
    except Exception:
        deploying = False
    result["deploying"] = deploying

    # Host framework detection (USB mode)
    try:
        status = _get_registry().detect()
        result["host_framework"] = {
            "name": status.name,
            "detected": status.detected,
            "status": status.status,
            "uptime_seconds": status.uptime_seconds,
            "has_restart": status.restart_cmd is not None,
            "details": status.details,
        }
    except Exception:
        logger.warning("Failed to detect host framework", exc_info=True)
        result["host_framework"] = {
            "name": "unknown",
            "detected": False,
            "status": "error",
        }

    # Sentinel — container-side guardian / autonomous CC diagnosis call site.
    # Use GenesisRuntime.peek() so an observability call can never spawn a
    # zombie blank runtime as a side effect (which would mask real bootstrap
    # failures elsewhere). peek() returns None when no runtime exists.
    try:
        from genesis.runtime._core import GenesisRuntime

        rt = GenesisRuntime.peek()
        sentinel = getattr(rt, "_sentinel", None) if rt is not None else None
        if sentinel is None:
            result["sentinel"] = _sentinel_unavailable()
        else:
            state = sentinel.state
            # Compute heartbeat staleness (stale = >10 min since last tick)
            heartbeat = getattr(state, "last_heartbeat_at", "")
            if heartbeat:
                try:
                    age_s = (datetime.now(UTC) - datetime.fromisoformat(heartbeat)).total_seconds()
                except (ValueError, TypeError):
                    age_s = 9999
            else:
                age_s = 9999  # No heartbeat recorded yet — treat as stale
            is_stale = age_s > 600  # 10 min = 2x awareness loop interval

            # Only coerce a HEALTHY sentinel — active states (investigating,
            # escalated, remediating, awaiting_*) are more informative and must
            # not be masked by "stale" or "deploying". During a deploy the fresh
            # server's heartbeat is legitimately empty (not stale): suppress stale
            # and surface "deploying" so the dashboard reads informational, not red.
            reported_state = state.current_state
            if deploying:
                is_stale = False
                if reported_state == "healthy":
                    reported_state = "deploying"
            elif is_stale and reported_state == "healthy":
                reported_state = "stale"

            result["sentinel"] = {
                "enabled": True,
                "current_state": reported_state,
                "is_active": bool(sentinel.is_active),
                "last_trigger_source": state.last_trigger_source or "",
                "last_trigger_reason": state.last_trigger_reason or "",
                "last_dispatch_at": state.last_cc_dispatch_at or "",
                "escalated_count": int(state.escalated_count),
                "staleness_s": int(age_s),
                "is_stale": is_stale,
            }
    except ImportError:
        # In production this import should always succeed; failure here means
        # broken install or PYTHONPATH drift. Loud enough to notice, not loud
        # enough to crash the snapshot.
        logger.warning("Sentinel module not importable — reporting unavailable", exc_info=True)
        result["sentinel"] = _sentinel_unavailable()
    except (AttributeError, TypeError):
        # Schema drift — SentinelStateData / SentinelDispatcher shape changed
        # without updating this snapshot. Loud failure so it gets noticed.
        logger.error("Sentinel snapshot schema drift", exc_info=True)
        result["sentinel"] = _sentinel_unavailable()
    except Exception:
        logger.error("Failed to collect sentinel status", exc_info=True)
        result["sentinel"] = _sentinel_unavailable()

    return result


def services() -> dict:
    """Collect systemd service states, watchdog health, and host framework.

    Synchronous composition — kept for the standalone/MCP path and direct tests.
    On the event loop, prefer ``services_async()`` (offloads the subprocess work).
    """
    return _finish_services(_collect_service_status_safe())


async def services_async() -> dict:
    """Event-loop-safe services snapshot.

    Offloads ONLY the subprocess-heavy collection to a worker thread, then
    assembles host framework + sentinel on the loop (the sentinel read must stay
    loop-atomic — see ``_finish_services``).
    """
    base = await _aio.to_thread(_collect_service_status_safe)
    return _finish_services(base)


def _sentinel_unavailable() -> dict:
    return {
        "enabled": False,
        "current_state": "unavailable",
        "is_active": False,
        "last_trigger_source": "",
        "last_trigger_reason": "",
        "last_dispatch_at": "",
        "escalated_count": 0,
        "staleness_s": 0,
        "is_stale": False,
    }


# Module-level singleton — lazy init avoids import-time subprocess calls.
_registry = None


def _get_registry():
    global _registry
    if _registry is None:
        from genesis.observability.host_detection import HostDetectorRegistry

        _registry = HostDetectorRegistry()
    return _registry


async def mcp_status() -> dict:
    """Check MCP server registration status by querying tool lists."""
    servers = {}
    mcp_modules = [
        ("health", "genesis.mcp.health_mcp"),
        ("memory", "genesis.mcp.memory_mcp"),
        ("outreach", "genesis.mcp.outreach_mcp"),
        ("recon", "genesis.mcp.recon_mcp"),
    ]
    for name, module_path in mcp_modules:
        try:
            mod = importlib.import_module(module_path)
            mcp_obj = getattr(mod, "mcp", None)
            if mcp_obj is None:
                servers[name] = {"status": "error", "error": "no mcp object"}
                continue
            tools = await _aio.wait_for(mcp_obj.get_tools(), timeout=2.0)
            servers[name] = {
                "status": "up" if tools else "no_tools",
                "tool_count": len(tools),
            }
        except Exception as exc:
            try:
                tm = getattr(mcp_obj, "_tool_manager", None)
                if tm:
                    tool_count = len(getattr(tm, "_tools", {}))
                    servers[name] = {"status": "registered", "tool_count": tool_count}
                else:
                    servers[name] = {"status": "error", "error": str(exc)}
            except Exception:
                servers[name] = {"status": "error", "error": str(exc)}

    # Overlay crash file data — process-level crashes the import check can't see
    # Restrict to known server names (match _VALID_SERVERS in genesis_mcp_server.py)
    _expected = ("health", "memory", "outreach", "recon")
    for srv_name in _expected:
        crash_file = _MCP_CRASH_DIR / f"{srv_name}.json"
        if crash_file.exists():
            try:
                info = json.loads(crash_file.read_text())
                servers[srv_name] = {
                    "status": "crashed",
                    "error": info.get("error", "unknown"),
                    "crashed_at": info.get("timestamp", ""),
                }
            except (json.JSONDecodeError, OSError):
                servers[srv_name] = {"status": "crashed", "error": "unreadable"}

    return servers
