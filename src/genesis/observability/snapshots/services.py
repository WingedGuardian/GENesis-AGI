"""Services and MCP status snapshot."""

from __future__ import annotations

import asyncio as _aio
import importlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MCP_CRASH_DIR = Path.home() / ".genesis" / "mcp_crashes"


def services() -> dict:
    """Collect systemd service states, watchdog health, and host framework."""
    try:
        from genesis.observability.service_status import collect_service_status

        result = collect_service_status()
    except Exception:
        logger.warning("Failed to collect service status", exc_info=True)
        result = {"status": "unknown"}

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
            result["sentinel"] = {
                "enabled": True,
                "current_state": state.current_state,
                "is_active": bool(sentinel.is_active),
                "last_trigger_source": state.last_trigger_source or "",
                "last_trigger_reason": state.last_trigger_reason or "",
                "last_dispatch_at": state.last_cc_dispatch_at or "",
                "escalated_count": int(state.escalated_count),
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


def _sentinel_unavailable() -> dict:
    return {
        "enabled": False,
        "current_state": "unavailable",
        "is_active": False,
        "last_trigger_source": "",
        "last_trigger_reason": "",
        "last_dispatch_at": "",
        "escalated_count": 0,
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
