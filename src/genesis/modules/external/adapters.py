"""The one place that maps a config's ``adapter:`` name to its class.

TWO loaders read module YAML — ``runtime/init/modules.py`` for the runtime and
``mcp/health/module_ops.py`` for the MCP tools — and an invariant that holds in
one of two is not an invariant. Before this module existed, only the runtime
loader honoured ``adapter:``; the MCP path built a plain ExternalProgramAdapter
for every external config, so ``module_call`` would have handed back exactly the
object the runtime loader refuses to construct.

Selection lives here rather than in either loader so adding a dialect is one
entry, and neither caller can drift from the other.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def adapter_classes() -> dict[str, type]:
    """Adapter implementations selectable by a config's ``adapter:`` field.

    Built per call and imported lazily so a module package that fails to import
    cannot break loading for every other module at import time.
    """
    from genesis.modules.endpoint.adapter import WindowsEndpointAdapter

    # Named by DIALECT, not by role: the commands WindowsEndpointAdapter builds
    # are PowerShell and its byte budget is a cmd.exe number, so a future POSIX
    # endpoint gets its own entry rather than silently inheriting those.
    return {"windows-endpoint": WindowsEndpointAdapter}


def build_adapter(data: dict, filename: str, config: Any) -> Any | None:
    """Construct the adapter a config asks for, or return None.

    Returns None — meaning "do not register this module" — rather than falling
    back to the base class. A silent fallback yields a module that registers,
    reports healthy, appears in listings and cannot do the one thing its config
    asked for, which is the least debuggable failure available: every surface a
    person would check says the module is fine.
    """
    from genesis.modules.external.adapter import ExternalProgramAdapter

    adapter_name = data.get("adapter")
    if adapter_name:
        known = adapter_classes()
        adapter_cls = known.get(adapter_name)
        if adapter_cls is None:
            logger.error(
                "Module '%s' (%s) requests unknown adapter '%s' — refusing to load it "
                "as a plain external module. Known adapters: %s",
                data.get("name"), filename, adapter_name, sorted(known),
            )
            return None
    else:
        adapter_cls = ExternalProgramAdapter

    try:
        return adapter_cls(config)
    except Exception:
        logger.error(
            "Module '%s' (%s) failed to construct with adapter '%s'",
            data.get("name"), filename, adapter_name or "external", exc_info=True,
        )
        return None
