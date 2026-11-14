"""Inert, explicitly constructed shared HTTP transport; no launcher selects it."""

from ._settings import SharedHTTPConfig, require_qualified_stack

__all__ = ["SharedHTTPConfig", "build_http_app", "require_qualified_stack"]


async def build_http_app(server, **options):
    """Qualify the stack before importing its version-specific assembly."""
    require_qualified_stack()
    from ._app import build_http_app as assemble

    return await assemble(server, **options)
