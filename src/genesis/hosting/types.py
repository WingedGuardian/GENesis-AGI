"""Host framework adapter protocol.

Defines the interface between Genesis and its host environment. Each host
(standalone, Agent Zero, OpenClaw, etc.) implements this protocol.

Mirrors the HostDetector pattern in genesis.observability.host_detection.types.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from flask import Flask


@runtime_checkable
class HostFrameworkAdapter(Protocol):
    """What Genesis expects from its host environment.

    Standalone mode creates its own Flask app and manages everything.
    Agent Zero mode delegates to AZ's extension system.
    Future hosts (OpenClaw, etc.) implement this same interface.
    """

    @property
    def name(self) -> str:
        """Human-readable name (e.g. 'Standalone', 'Agent Zero')."""
        ...

    async def bootstrap(self) -> None:
        """Initialize GenesisRuntime and host-specific setup.

        Replaces AZ's ``server_startup`` extension hook.  Must call
        ``GenesisRuntime.instance().bootstrap()`` with the appropriate mode.
        """
        ...

    async def serve(self) -> None:
        """Start serving.  Blocks until shutdown signal.

        For standalone: Flask in daemon thread, asyncio in main thread.
        For AZ: no-op (AZ owns the event loop).
        """
        ...

    async def shutdown(self) -> None:
        """Graceful shutdown of all services."""
        ...

    def get_flask_app(self) -> Flask | None:
        """Return the Flask app for blueprint registration.

        Returns ``None`` for headless modes (bridge-only, no dashboard).
        """
        ...
