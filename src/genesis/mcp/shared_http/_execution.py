"""Validated message context and nonblocking tool admission around real units."""

from contextlib import contextmanager, nullcontext

import anyio
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_request
from fastmcp.server.middleware import Middleware
from mcp import MCPError

from genesis.caller_context import request_caller
from genesis.mcp.external_profiles import ExternalProfile, current_external_profile
from genesis.observability.mcp_middleware import InstrumentationMiddleware
from genesis.observability.provider_activity import suppress_activity_persistence
from genesis.observability.session_context import session_scope

from ._auth import authenticated_caller

BUSY_MESSAGE = "Shared MCP service busy"


@contextmanager
def caller_scope(caller, service):
    profile = ExternalProfile(caller.profile) if caller.profile is not None else None
    token = current_external_profile.set(profile)
    try:
        suppression = suppress_activity_persistence() if service == "health" and profile == ExternalProfile.VALIDATOR else nullcontext()
        with request_caller(caller), session_scope(caller.session_id), suppression:
            yield
    finally:
        current_external_profile.reset(token)


class SharedInstrumentation(InstrumentationMiddleware):
    """Keep existing units/guard modes; a shared service recomputes freshness."""

    def __init__(self, tracker, service, db, freshness):
        super().__init__(tracker, service, db=db)
        self._shared_freshness = freshness

    async def _is_stale(self):
        return await self._shared_freshness() is True

    async def _enforce_freshness(self, tool_name):
        try:
            await super()._enforce_freshness(tool_name)
        except ToolError:
            # A fresh client cannot replace code in a long-lived shared service.
            raise ToolError("Shared MCP service is stale; reconnect after the service updates") from None


class SharedMessages(Middleware):
    def __init__(self, config, catalog, enabled):
        self.service, self.catalog, self.enabled = config.service, catalog, enabled
        self.limiter = anyio.CapacityLimiter(config.tool_concurrency)
        self.busy = 0

    async def on_message(self, context, call_next):
        caller = authenticated_caller(get_http_request().scope)
        with caller_scope(caller, self.service):
            return await call_next(context)

    async def on_list_tools(self, context, call_next):
        caller = authenticated_caller(get_http_request().scope)
        tools = await call_next(context)
        enabled = self.enabled() is True
        return [tool for tool in tools
                if self.catalog.allowed(tool.name, caller, enabled=enabled) and self.catalog.binds(tool.name, tool)]

    async def on_call_tool(self, context, call_next):
        # Use validated metadata, never a lost-scope/default CallerContext as
        # evidence of a forwarder role. Discovery/cancel never borrow a token.
        caller = authenticated_caller(get_http_request().scope)
        name = context.message.name
        if (not self.catalog.allowed(name, caller, enabled=self.enabled() is True)
                or not self.catalog.binds(name, await context.fastmcp_context.fastmcp.get_tool(name))):
            raise ToolError("Tool unavailable in this shared service")
        token = object()
        try:
            self.limiter.acquire_on_behalf_of_nowait(token)
        except anyio.WouldBlock:
            self.busy += 1
            raise MCPError(code=-32000, message=BUSY_MESSAGE) from None
        try:
            return await call_next(context)
        finally:
            self.limiter.release_on_behalf_of(token)
