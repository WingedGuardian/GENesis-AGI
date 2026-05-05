"""Module operations MCP tool — execute operations on external modules.

The MCP server runs standalone (no GenesisRuntime). This tool reads module
YAML configs directly and creates IPC adapters to communicate with external
programs. Native modules are not accessible via this path — they run inside
the genesis-server process.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_MODULES_DIR = Path(__file__).resolve().parents[4] / "config" / "modules"
_LOCAL_MODULES_DIR = Path.home() / ".genesis" / "config" / "modules"

# Cache of loaded configs + IPC adapters (lazy, per-process lifetime)
_adapters: dict | None = None


def _reset_adapter_cache() -> None:
    """Clear the cached external-module adapter dict.

    For test isolation only. The production ``_get_adapters()`` helper
    lazily loads YAML configs from ``_MODULES_DIR`` once per process
    and caches the result in the module-level ``_adapters`` global.
    Tests that monkeypatch ``_MODULES_DIR`` (or that need to verify
    the load path) must call this first, or they get stale state from
    whichever test loaded the cache earliest.

    No production callsite needs this — IPC adapter reloads require a
    process restart. Tests only.
    """
    global _adapters
    _adapters = None


def _get_adapters() -> dict:
    """Lazily load external module configs and create adapters."""
    global _adapters
    if _adapters is not None:
        return _adapters

    from genesis.modules.external.adapter import ExternalProgramAdapter
    from genesis.modules.external.config import ProgramConfig

    _adapters = {}

    # Collect configs: repo defaults first, local overlay wins on same filename
    # (mirrors genesis.runtime.init.modules._load_modules_from_yaml)
    config_files: dict[str, Path] = {}
    for d in (_MODULES_DIR, _LOCAL_MODULES_DIR):
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                config_files[p.name] = p

    for yaml_path in sorted(config_files.values(), key=lambda p: p.name):
        try:
            data = yaml.safe_load(yaml_path.read_text())
            if not data or data.get("type") != "external":
                continue
            config = ProgramConfig.from_dict(data)
            adapter = ExternalProgramAdapter(config)
            _adapters[config.name] = adapter
        except Exception:
            logger.warning("Failed to load module config from %s", yaml_path.name, exc_info=True)

    return _adapters


async def _ensure_adapter_started(adapter) -> None:
    """Start the IPC adapter and mark healthy if not already running.

    Each adapter type owns its readiness semantics via ``needs_start``:
    HTTP needs a client, stdio needs a process, SSH is always ready.
    """
    if adapter.ipc.needs_start:
        await adapter.ipc.start()
    # In standalone MCP context, register() never ran. Mark healthy
    # if health check passes so execute_operation doesn't reject.
    if not adapter.healthy:
        await adapter.check_health()


async def _impl_module_call(
    module_name: str,
    operation: str,
    params: dict | None = None,
) -> dict:
    """Execute an operation on an external module."""
    adapters = _get_adapters()

    if not module_name or not module_name.strip():
        return {"error": "module_name is required"}
    module_name = module_name.strip()

    if module_name not in adapters:
        available = list(adapters.keys())
        return {"error": f"Module '{module_name}' not found", "available_modules": available}

    adapter = adapters[module_name]

    if not operation or not operation.strip():
        ops = adapter.list_operations()
        return {
            "error": "operation is required",
            "available_operations": {
                name: op.get("description", "") for name, op in ops.items()
            },
        }

    await _ensure_adapter_started(adapter)
    return await adapter.execute_operation(operation.strip(), params)


async def _impl_module_list() -> dict:
    """List available external modules and their operations."""
    adapters = _get_adapters()
    result = {}
    for name, adapter in adapters.items():
        ops = adapter.list_operations()
        ipc = adapter.config.ipc
        entry: dict = {
            "description": adapter.config.description,
            "enabled": adapter.enabled,
            "ipc_method": ipc.method,
            "operations": {
                op_name: op.get("description", "") for op_name, op in ops.items()
            },
        }
        if ipc.method == "ssh":
            entry["ipc_host"] = ipc.ssh_host
        else:
            entry["ipc_url"] = ipc.url
        result[name] = entry
    return result


@mcp.tool()
async def module_call(
    module_name: str,
    operation: str,
    params: dict | None = None,
) -> dict:
    """Execute an operation on an external module.

    Looks up the module by name, finds the operation in its manifest,
    and executes via IPC (HTTP, stdio, or SSH). Pass params as a dict
    for query parameters, request body fields, or SSH dispatch options.

    Call with just module_name (no operation) to see available operations.

    Examples:
        module_call("My Module", "list_items", {"min_score": 80})
        module_call("My Module", "status")
        module_call("Remote Agent", "dispatch", {
            "prompt": "Analyze the latest data",
            "model": "sonnet", "effort": "high"
        })
        module_call("Remote Agent", "version")
    """
    return await _impl_module_call(module_name, operation, params)


@mcp.tool()
async def module_list() -> dict:
    """List all external modules and their available operations.

    Returns module names, descriptions, IPC details, and operation manifests.
    Use this to discover what modules are available and what you can do with them.
    """
    return await _impl_module_list()
