"""Services and MCP status snapshot."""

from __future__ import annotations

import asyncio as _aio
import importlib
import logging

logger = logging.getLogger(__name__)


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

    return result


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
    return servers
