"""Explicit, inert shared-MCP forwarder; ordinary registrations stay unchanged."""

from genesis.mcp.shared_http import require_qualified_stack
from genesis.mcp.shared_settings import LOCAL_FRONTEND_STAGES


async def serve_stdio(settings, header_factory, *, local_server=None, stage=None):
    """Serve one frontend after qualification, without initializing a service."""
    require_qualified_stack()
    if (local_server is None) != (stage is None):
        raise ValueError("Local server and stage are required together")
    if stage is not None and (settings.http.service, stage) not in LOCAL_FRONTEND_STAGES:
        raise ValueError("Unsupported local frontend")
    from ._stdio import serve

    await serve(settings, header_factory, local_server=local_server, stage=stage)
