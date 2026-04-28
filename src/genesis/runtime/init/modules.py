"""Init function: _init_modules — unified YAML-based module loader.

Module loading has two phases:
1. YAML scan: load all modules declared in config/modules/*.yaml (or local overlay).
2. Auto-discovery: scan genesis/modules/*/module.py for classes with __module_meta__
   that were NOT already loaded from YAML. YAML always takes precedence.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

_MODULES_DIR = Path(__file__).resolve().parents[4] / "config" / "modules"
_LOCAL_MODULES_DIR = Path.home() / ".genesis" / "config" / "modules"


def _import_class(class_path: str):
    """Dynamically import a class from a dotted path like 'genesis.modules.foo.module.FooModule'."""
    module_path, class_name = class_path.rsplit(".", 1)
    mod = importlib.import_module(module_path)
    return getattr(mod, class_name)


async def init(rt: GenesisRuntime) -> None:
    """Initialize ModuleRegistry and load all modules from config/modules/*.yaml."""
    try:
        from genesis.modules.registry import ModuleRegistry

        rt._module_registry = ModuleRegistry()
        rt._module_registry.set_runtime(rt)

        # Phase 1: Load all modules from YAML configs (native + external)
        await _load_modules_from_yaml(rt)

        # Phase 2: Auto-discover native modules with __module_meta__ not covered by YAML
        await _load_discovered_modules(rt)

        # Restore persisted state from DB
        await _restore_module_states(rt)

        count = len(rt._module_registry.list_modules())
        enabled = len(rt._module_registry.list_enabled())
        logger.info("Module registry initialized (%d modules, %d enabled)", count, enabled)

    except ImportError:
        logger.warning("genesis.modules not available")
    except Exception:
        logger.exception("Failed to initialize module registry")


async def _load_modules_from_yaml(rt: GenesisRuntime) -> None:
    """Scan config/modules/*.yaml and ~/.genesis/config/modules/*.yaml for module configs.

    Local overlay (~/.genesis/config/modules/) takes precedence over repo configs
    when the same filename exists in both directories.
    """
    import yaml

    # Collect configs: repo defaults first, then local overlay (local wins on same filename)
    config_files: dict[str, Path] = {}

    if _MODULES_DIR.is_dir():
        for p in sorted(_MODULES_DIR.glob("*.yaml")):
            config_files[p.name] = p
    elif not _LOCAL_MODULES_DIR.is_dir():
        logger.warning("No module config directories found (checked %s and %s)",
                       _MODULES_DIR, _LOCAL_MODULES_DIR)
        return

    if _LOCAL_MODULES_DIR.is_dir():
        for p in sorted(_LOCAL_MODULES_DIR.glob("*.yaml")):
            if p.name in config_files:
                logger.debug("Local module config overrides repo: %s", p.name)
            config_files[p.name] = p

    for yaml_path in sorted(config_files.values(), key=lambda p: p.name):
        try:
            data = yaml.safe_load(yaml_path.read_text())
            if not data or not isinstance(data, dict) or "name" not in data:
                logger.warning("Skipping invalid module config: %s", yaml_path.name)
                continue

            mod_type = data.get("type", "external")

            if mod_type == "native":
                module = _load_native_module(data, yaml_path.name)
            elif mod_type == "external":
                module = _load_external_module(data, yaml_path.name)
            else:
                logger.warning("Unknown module type '%s' in %s, skipping", mod_type, yaml_path.name)
                continue

            if module is not None:
                await rt._module_registry.load_module(module)

        except Exception:
            logger.warning("Failed to load module from %s", yaml_path.name, exc_info=True)


def _discover_native_modules(already_loaded: set[str]) -> list[dict]:
    """Scan genesis/modules/*/module.py for classes with __module_meta__.

    Only returns classes that:
    - Have a ``__module_meta__`` dict attribute with a ``"name"`` key
    - Were defined in the scanned module file (not imported into it)
    - Are not already registered (YAML takes precedence)

    Returns a list of synthetic config dicts suitable for _load_native_module().
    """
    spec = importlib.util.find_spec("genesis.modules")
    if spec is None or not spec.submodule_search_locations:
        logger.debug("Auto-discovery: genesis.modules package not found")
        return []

    modules_dir = Path(list(spec.submodule_search_locations)[0])
    discovered = []

    for module_file in sorted(modules_dir.glob("*/module.py")):
        pkg_name = module_file.parent.name
        dotted = f"genesis.modules.{pkg_name}.module"
        try:
            py_mod = importlib.import_module(dotted)
        except Exception:
            logger.warning("Auto-discovery: failed to import %s", dotted, exc_info=True)
            continue

        for attr_name in dir(py_mod):
            cls = getattr(py_mod, attr_name, None)
            if not isinstance(cls, type):
                continue
            if not hasattr(cls, "__module_meta__"):
                continue
            # Only consider classes defined in THIS file, not imported into it
            if getattr(cls, "__module__", None) != dotted:
                continue

            meta = cls.__module_meta__
            if not isinstance(meta, dict) or "name" not in meta:
                continue

            mod_name = meta["name"]
            if mod_name in already_loaded:
                logger.debug("Auto-discovery: '%s' already loaded from YAML, skipping", mod_name)
                continue

            discovered.append({
                "name": mod_name,
                "type": "native",
                "class": f"{dotted}.{attr_name}",
                "display_name": meta.get("display_name", ""),
                "description": meta.get("description", ""),
                "category": meta.get("category", ""),
                "tags": meta.get("tags", []),
                "version": meta.get("version", ""),
                "enabled": meta.get("enabled", False),
                "research_profile": meta.get("research_profile"),
            })
            logger.info("Auto-discovery: found module '%s' in %s", mod_name, dotted)

    return discovered


async def _load_discovered_modules(rt: GenesisRuntime) -> None:
    """Load auto-discovered native modules not already present from YAML."""
    already_loaded = set(rt._module_registry.list_modules())
    for config in _discover_native_modules(already_loaded):
        try:
            module = _load_native_module(config, "<auto-discovered>")
            if module is not None:
                await rt._module_registry.load_module(module)
        except Exception:
            logger.warning(
                "Auto-discovery: failed to load '%s'", config.get("name"), exc_info=True,
            )


def _load_native_module(data: dict, filename: str):
    """Load a native Python module from its class path."""
    class_path = data.get("class")
    if not class_path:
        logger.warning("Native module in %s missing 'class' field", filename)
        return None

    cls = _import_class(class_path)
    module = cls()

    # Apply identity fields from YAML/meta to module instance attributes (if present)
    for attr in ("description", "display_name", "category", "tags", "version"):
        if data.get(attr) and hasattr(module, f"_{attr}"):
            setattr(module, f"_{attr}", data[attr])

    logger.info("Native module '%s' loaded from %s", data["name"], filename)
    return module


def _load_external_module(data: dict, filename: str):
    """Load an external program module via ExternalProgramAdapter."""
    from genesis.modules.external.adapter import ExternalProgramAdapter
    from genesis.modules.external.config import ProgramConfig

    config = ProgramConfig.from_dict(data)
    adapter = ExternalProgramAdapter(config)
    logger.info("External module '%s' loaded from %s", data["name"], filename)
    return adapter


async def _restore_module_states(rt: GenesisRuntime) -> None:
    """Restore persisted enabled/config state from DB for all loaded modules."""
    if rt._db is None:
        return

    from genesis.modules.persistence import load_all_module_states, save_module_state
    from genesis.runtime._degradation import record_init_degradation

    states = await load_all_module_states(rt._db)

    for mod_name in rt._module_registry.list_modules():
        mod = rt._module_registry.get(mod_name)
        if mod is None:
            continue

        if mod_name in states:
            mod.enabled = states[mod_name]["enabled"]
            config = states[mod_name].get("config", {})
            if config and hasattr(mod, "update_config") and callable(mod.update_config):
                try:
                    mod.update_config(config)
                except Exception as exc:
                    logger.warning("Failed to restore config for module %s", mod_name, exc_info=True)
                    await record_init_degradation(
                        rt._db, rt._event_bus, "modules", f"config_restore:{mod_name}", str(exc),
                    )
            logger.info("Restored module '%s' state: enabled=%s", mod_name, mod.enabled)
        else:
            # New module — seed with YAML default or disabled
            default_enabled = getattr(mod, "_config", None)
            if default_enabled and hasattr(default_enabled, "enabled"):
                mod.enabled = default_enabled.enabled
            else:
                mod.enabled = False
            await save_module_state(rt._db, mod_name, enabled=mod.enabled)
            logger.info("Module '%s' seeded (enabled=%s)", mod_name, mod.enabled)
