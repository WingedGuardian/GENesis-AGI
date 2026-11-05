"""Version-coupled execution boundary for the opt-in shared HTTP manager only.

FastMCP 2.14.6 overrides SDK Server.run without its handler cancellation-finally.
Retain the original server and registries; only the shared manager receives this
adapter. Remove it after an upstream replacement passes the same qualification.

The run-loop structure is adapted from FastMCP 2.14.6 server/low_level.py,
licensed under Apache-2.0 (fastmcp-LICENSE.txt beside this module). Genesis
modifies delegation and adds the SDK-style handler cancellation finalizer.
"""

from contextlib import AsyncExitStack
from importlib.metadata import PackageNotFoundError, version

import anyio
from fastmcp.server.low_level import LowLevelServer, MiddlewareServerSession

_QUALIFIED_PAIRS = frozenset({("2.14.6", "1.30.0"), ("2.14.7", "1.30.0")})
_UPSTREAM_RUN = LowLevelServer.run


class SharedExecutionError(RuntimeError):
    """Shared execution cannot use this dependency pair or server override."""


class SharedExecution:
    def __init__(self, server: LowLevelServer):
        try:
            pair = (version("fastmcp"), version("mcp"))
        except PackageNotFoundError:
            pair = None
        if pair not in _QUALIFIED_PAIRS:
            raise SharedExecutionError(
                "Shared execution requires qualified FastMCP 2.14.6/2.14.7 and MCP 1.30.0"
            )
        if type(server) is not LowLevelServer or getattr(server.run, "__func__", None) is not _UPSTREAM_RUN:
            raise SharedExecutionError("Shared execution requires an unmodified builtin LowLevelServer")
        self.server = server

    def create_initialization_options(self, *args, **kwargs):
        return self.server.create_initialization_options(*args, **kwargs)

    async def run(self, read_stream, write_stream, initialization_options,
                  raise_exceptions=False, stateless=False):
        server = self.server
        async with AsyncExitStack() as stack:
            lifespan_context = await stack.enter_async_context(server.lifespan(server))
            session = await stack.enter_async_context(MiddlewareServerSession(
                server.fastmcp, read_stream, write_stream, initialization_options,
                stateless=stateless,
            ))
            async with anyio.create_task_group() as tg:
                session._subscription_task_group = tg
                try:
                    async for message in session.incoming_messages:
                        tg.start_soon(server._handle_message, message, session,
                                      lifespan_context, raise_exceptions)
                finally:
                    tg.cancel_scope.cancel()
