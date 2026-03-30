"""Module registry — manages capability module lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from genesis.modules.base import CapabilityModule

logger = logging.getLogger(__name__)


class ModuleRegistry:
    """Manages capability module lifecycle.

    Modules register with the runtime, subscribe to the Knowledge Pipeline,
    and can be loaded/unloaded without affecting Genesis core.
    """

    def __init__(self) -> None:
        self._modules: dict[str, CapabilityModule] = {}
        self._runtime: Any = None

    def set_runtime(self, runtime: Any) -> None:
        """Set the GenesisRuntime reference for module registration."""
        self._runtime = runtime

    async def load_module(self, module: CapabilityModule) -> None:
        """Register a module with the runtime and pipeline."""
        if module.name in self._modules:
            logger.warning("Module %s already loaded, skipping", module.name)
            return

        if self._runtime is not None:
            await module.register(self._runtime)

        self._modules[module.name] = module
        logger.info("Module %s loaded", module.name)

    async def unload_module(self, name: str) -> None:
        """Deregister a module. Pipeline subscription removed. Nothing breaks."""
        module = self._modules.pop(name, None)
        if module is None:
            logger.warning("Module %s not found", name)
            return

        await module.deregister()
        logger.info("Module %s unloaded", name)

    def get(self, name: str) -> CapabilityModule | None:
        """Get a loaded module by name."""
        return self._modules.get(name)

    def list_modules(self) -> list[str]:
        """Return names of all loaded modules."""
        return list(self._modules.keys())

    def list_enabled(self) -> list[str]:
        """Return names of enabled modules."""
        return [name for name, mod in self._modules.items() if mod.enabled]

    async def unload_all(self) -> None:
        """Unload all modules. For shutdown."""
        for name in list(self._modules):
            await self.unload_module(name)
