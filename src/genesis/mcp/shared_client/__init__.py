"""Explicit, inert shared-MCP forwarder; ordinary registrations stay unchanged."""

from genesis.mcp.shared_http import require_qualified_stack


async def serve_stdio(settings, header_factory):
    """Serve one frontend after qualification, without initializing a service."""
    require_qualified_stack()
    from ._stdio import serve

    await serve(settings, header_factory)
