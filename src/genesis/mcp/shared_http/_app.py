"""Qualified upstream composition; resource construction belongs to bootstrap."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime

from fastmcp.server.auth.middleware import RequireAuthMiddleware
from fastmcp.server.http import StreamableHTTPASGIApp, create_base_app
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import RequestBodyLimitMiddleware, TransportSecuritySettings
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from genesis.caller_context import enter_shared_service
from genesis.db.crud.update_history import last_successful_update
from genesis.db.request_database import RequestDatabase
from genesis.mcp.external_profiles import ExternalProfileMiddleware
from genesis.observability.commit_identity import is_stale
from genesis.observability.mcp_middleware import InstrumentationMiddleware

from ._admission import EnvelopeGuard, ExactAuthority
from ._auth import CallerAuth, authenticated_caller, usable_credentials
from ._catalog import Catalog
from ._execution import SharedInstrumentation, SharedMessages, caller_scope
from ._settings import SharedHTTPConfig

logger = logging.getLogger(__name__)

# Status reports admission while tools may hold every foreground unit; it must
# not wait out the checkout timeout. A miss reports freshness as unknown.
_STATUS_FRESHNESS_WAIT_S = 1.0


async def build_http_app(server, *, config, resolver, database, tracker,
                         loaded_commit, started_at, enabled):
    if not isinstance(config, SharedHTTPConfig) or not isinstance(database, RequestDatabase):
        raise ValueError("Shared MCP requires qualified configuration and request database")
    if database.foreground_limit != config.db_units:
        raise ValueError("Shared foreground database budget does not match configuration")
    # Same rule as initialize_request_service: detached work needs its own budget.
    detached = config.service == "memory" or (tracker is not None and getattr(tracker, "persist_activity", True))
    if detached and database._background_slots is None:
        raise ValueError("Shared detached work requires explicit background budgets")
    if not isinstance(loaded_commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", loaded_commit):
        raise ValueError("Shared MCP requires known loaded commit identity")
    try:
        if datetime.fromisoformat(started_at).utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Shared MCP requires an aware startup timestamp") from None
    if not callable(enabled) or enabled() is not True:
        raise ValueError("Shared MCP cannot start disabled")
    usable_credentials(resolver, config.service)
    instrumentation = [m for m in server.middleware if isinstance(m, InstrumentationMiddleware)]
    if (len(instrumentation) != 1 or instrumentation[0]._db is not database
            or instrumentation[0]._tracker is not tracker or instrumentation[0]._server != config.service
            or any(isinstance(m, (SharedMessages, ExternalProfileMiddleware)) for m in server.middleware)):
        raise ValueError("Shared MCP requires one matching unprofiled resource boundary")
    catalog = await Catalog.capture(server, config.service)
    auth = CallerAuth(resolver, config.service)
    messages = SharedMessages(config, catalog, enabled)
    transport = StreamableHTTPASGIApp(None)

    async def freshness():
        try:
            # Join a tool's unit; status opens its own. An independent checkout
            # inside a one-slot tool unit would wait on itself.
            async with database.unit_of_work():
                row = await last_successful_update(database)
            if row is None or not row[1]:
                return None
            if datetime.fromisoformat(row[0]).utcoffset() is None:
                return None
            return is_stale(loaded_commit, started_at, row[0], row[1])
        except Exception:
            logger.debug("Shared MCP freshness unavailable", exc_info=True)
            return None

    async def status(request):
        caller = authenticated_caller(request.scope)
        with caller_scope(caller, config.service):
            active = enabled() is True
            try:
                async with asyncio.timeout(_STATUS_FRESHNESS_WAIT_S):
                    stale = await freshness()
            except TimeoutError:
                stale = None
            return JSONResponse({
                "service": config.service, "loaded_commit": loaded_commit,
                "started_at": started_at, "transport_schema_version": 1,
                "catalog_revision": catalog.revision(caller, enabled=active),
                "enabled": active, "stale": stale,
                "active_tools": messages.limiter.borrowed_tokens, "busy_refusals": messages.busy,
            })

    @asynccontextmanager
    async def lifespan(app):
        # These two private upstream handles are confined to this assembly.
        # No event store/resumption, protocol patch or second lifespan owner.
        manager = StreamableHTTPSessionManager(
            app=server._mcp_server, event_store=None, json_response=False, stateless=False,
            session_idle_timeout=config.session_idle_timeout_s, max_sessions=config.max_sessions,
            max_request_body_size=config.max_body_bytes,
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        transport.session_manager = manager
        async with manager.run():
            try:
                yield
            finally:
                for session in list(manager._server_instances.values()):
                    try:
                        await session.terminate()
                    except Exception:
                        logger.debug("Shared MCP transport termination failed", exc_info=True)

    app = create_base_app(
        routes=[Route("/mcp", transport, methods=["GET", "POST", "DELETE"]),
                Route("/status", status, methods=["GET", "HEAD"])],
        middleware=[Middleware(ExactAuthority, authority=config.authority, origin=config.origin),
                    *auth.get_middleware(), Middleware(RequireAuthMiddleware, required_scopes=[]),
                    Middleware(RequestBodyLimitMiddleware, max_body_size=config.max_body_bytes),
                    Middleware(EnvelopeGuard)], lifespan=lifespan,
    )
    app.state.fastmcp_server, app.state.fastmcp_auth = server, auth
    app.state.path, app.state.transport_type = "/mcp", "streamable-http"
    app.state.shared_catalog, app.state.shared_messages = catalog, messages
    boundary = SharedInstrumentation(tracker, config.service, database, freshness)
    server.middleware[:] = [messages, *(boundary if m is instrumentation[0] else m for m in server.middleware)]
    # Select the new process mode only after every validation above succeeds.
    enter_shared_service()
    return app
