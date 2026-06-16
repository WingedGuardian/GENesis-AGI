#!/usr/bin/env python3
"""Genesis MCP Server — standalone wrapper for CC and HTTP integration.

Launches one of the Genesis MCP servers (health, memory, outreach, recon) as
either a stdio process (for Claude Code via .mcp.json) or an HTTP server
(for external clients like speech models, Home Assistant, or other agents).

Usage:
    # Stdio (default, for CC integration):
    python genesis_mcp_server.py --server health

    # HTTP (for external clients):
    python genesis_mcp_server.py --server health --transport streamable-http --port 8100

Architecture note: mcp.run(transport=...) owns the event loop (via anyio).
Bootstrappers must be synchronous — async DB connections are opened inside
the MCP server's event loop via FastMCP's _lifespan hook.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_VALID_SERVERS = {"health", "memory", "outreach", "recon", "discord-bot"}
_DEFAULT_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_DEFAULT_STATUS = Path.home() / ".genesis" / "status.json"


def _default_db_path() -> Path:
    from genesis.env import genesis_db_path

    return genesis_db_path()


_DEFAULT_DB = _default_db_path()


_VALID_TRANSPORTS = {"stdio", "streamable-http"}

# Default HTTP ports per server (avoids conflicts when running multiple)
_DEFAULT_PORTS = {
    "health": 8100,
    "memory": 8101,
    "outreach": 8102,
    "recon": 8103,
    "discord-bot": 8104,
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genesis MCP Server (standalone)")
    parser.add_argument(
        "--server",
        required=True,
        choices=sorted(_VALID_SERVERS),
        help="Which MCP server to run",
    )
    parser.add_argument(
        "--transport",
        choices=sorted(_VALID_TRANSPORTS),
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP port (default: per-server, 8100-8103)",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Bearer token for HTTP auth (default: GENESIS_MCP_HTTP_TOKEN env var)",
    )
    return parser.parse_args(argv)


def is_genesis_enabled(*, flag_path: Path = _DEFAULT_FLAG) -> bool:
    """Check if Genesis CC context is enabled via flag file."""
    return flag_path.exists()


def _build_transport_kwargs(args: argparse.Namespace) -> dict:
    """Build kwargs dict for mcp.run() from parsed CLI args."""
    kwargs: dict = {"transport": args.transport}
    if args.transport == "streamable-http":
        kwargs["host"] = args.host
        kwargs["port"] = args.port or _DEFAULT_PORTS.get(args.server, 8100)
        kwargs["path"] = "/mcp"
        kwargs["stateless_http"] = True
        kwargs["log_level"] = "warning"
    return kwargs


def _run_disabled_stub(transport_kwargs: dict) -> None:
    """Run a minimal MCP server that reports Genesis context is disabled."""
    from fastmcp import FastMCP

    stub = FastMCP("genesis-disabled")

    @stub.tool()
    async def genesis_status() -> str:
        """Genesis context is disabled. Use /genesis on to enable."""
        return "Genesis context is disabled. Run /genesis on in interactive CC to enable."

    _run_mcp(stub, transport_kwargs)


def _bootstrap_health(transport_kwargs: dict) -> None:
    """Bootstrap and run the health MCP server.

    Uses StandaloneHealthDataService which reads ~/.genesis/status.json
    instead of requiring live GenesisRuntime objects. When the DB exists,
    opens an aiosqlite connection via FastMCP's lifespan hook so that
    heartbeat queries and event-based tools work.
    """
    from genesis.mcp.health_mcp import init_health_mcp, mcp
    from genesis.mcp.standalone_health import StandaloneHealthDataService
    from genesis.observability.provider_activity import ProviderActivityTracker

    if not _DEFAULT_DB.exists():
        # No DB — run without heartbeat/event queries (graceful degradation)
        svc = StandaloneHealthDataService(status_path=_DEFAULT_STATUS, db=None)
        tracker = ProviderActivityTracker()
        init_health_mcp(svc, activity_tracker=tracker)
        clear_mcp_crash("health")
        _run_mcp(mcp, transport_kwargs)
        return

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        from genesis.db.connection import get_db

        # Long-lived shared connection: use the SerializedConnection (get_db) so
        # concurrent tool calls can't interleave-wedge the transaction state.
        # foreign_keys=False preserves the prior raw-connection behavior.
        db = await get_db(_DEFAULT_DB, foreign_keys=False)
        try:

            # Bootstrap standalone router for LLM-dependent tools
            try:
                from genesis.routing.standalone import create_standalone_router
            except ImportError:
                logger.warning("genesis.routing.standalone not available", exc_info=True)
            else:
                create_standalone_router()

            svc = StandaloneHealthDataService(
                status_path=_DEFAULT_STATUS,
                db=db,
            )
            tracker = ProviderActivityTracker()
            tracker.set_db(db)
            init_health_mcp(svc, activity_tracker=tracker)

            # Wire direct session tools with DB-only access.
            # Standalone MCP enqueues to direct_session_queue;
            # the Genesis server's poll loop handles dispatch.
            # Ensure queue table exists (standalone doesn't call db.init()).
            try:
                from genesis.db.schema import INDEXES, TABLES

                await db.execute(TABLES["direct_session_queue"])
                for idx_ddl in INDEXES:
                    if "direct_session_queue" in idx_ddl:
                        await db.execute(idx_ddl)
                await db.commit()

                from genesis.mcp.health.direct_session_tools import (
                    init_direct_session_tools,
                )

                init_direct_session_tools(db=db)
            except Exception:
                logger.warning(
                    "Direct session tools not available in standalone MCP",
                    exc_info=True,
                )

            # Wire campaign tools with DB-only access.
            # Standalone MCP provides read/update access to campaigns;
            # trigger and schedule hot-reload require the main server.
            try:
                from genesis.mcp.health.campaign_tools import init_campaign_tools

                init_campaign_tools(runner=None, db=db)
            except Exception:
                logger.warning(
                    "Campaign tools not available in standalone MCP",
                    exc_info=True,
                )

            clear_mcp_crash("health")
            yield
        finally:
            from genesis.mcp.health.browser import async_cleanup as _browser_cleanup
            await _browser_cleanup()
            await db.close()

    mcp._lifespan = _lifespan
    _run_mcp(mcp, transport_kwargs)


def _bootstrap_memory(transport_kwargs: dict) -> None:
    """Bootstrap and run the memory MCP server.

    Requires Qdrant + Ollama for embeddings. Opens aiosqlite connection
    inside the MCP event loop via FastMCP's lifespan hook.
    """
    from genesis.env import qdrant_url
    from genesis.mcp.memory_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        from qdrant_client import QdrantClient

        from genesis.db.connection import get_db
        from genesis.mcp.memory_mcp import init
        from genesis.memory.embeddings import EmbeddingProvider
        from genesis.observability.provider_activity import ProviderActivityTracker

        # Long-lived shared connection via SerializedConnection (see _bootstrap_health).
        db = await get_db(_DEFAULT_DB, foreign_keys=False)

        try:

            # Bootstrap standalone router for LLM-dependent tools
            try:
                from genesis.routing.standalone import create_standalone_router
            except ImportError:
                logger.warning("genesis.routing.standalone not available", exc_info=True)
            else:
                create_standalone_router()

            qdrant = QdrantClient(url=qdrant_url(), timeout=5)
            embedding = EmbeddingProvider()
            # The activity tracker enables InstrumentationMiddleware, which also
            # runs the per-call commit/rollback boundary that releases read
            # snapshots (WS-15 follow-up). Without a tracker the middleware — and
            # thus the boundary — never attaches.
            tracker = ProviderActivityTracker()
            tracker.set_db(db)
            init(db=db, qdrant_client=qdrant, embedding_provider=embedding,
                 activity_tracker=tracker)
            clear_mcp_crash("memory")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — memory MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    _run_mcp(mcp, transport_kwargs)


def _bootstrap_recon(transport_kwargs: dict) -> None:
    """Bootstrap and run the recon MCP server."""
    from genesis.mcp.recon_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        from genesis.db.connection import get_db
        from genesis.mcp.recon_mcp import init_recon_mcp
        from genesis.observability.provider_activity import ProviderActivityTracker

        # Long-lived shared connection via SerializedConnection (see _bootstrap_health).
        db = await get_db(_DEFAULT_DB, foreign_keys=False)

        try:

            # Bootstrap standalone router for LLM-dependent tools
            try:
                from genesis.routing.standalone import create_standalone_router
            except ImportError:
                logger.warning("genesis.routing.standalone not available", exc_info=True)
            else:
                create_standalone_router()

            # Tracker enables InstrumentationMiddleware + its read-snapshot
            # boundary (WS-15 follow-up) — see _bootstrap_memory.
            tracker = ProviderActivityTracker()
            tracker.set_db(db)
            init_recon_mcp(db=db, activity_tracker=tracker)
            clear_mcp_crash("recon")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — recon MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    _run_mcp(mcp, transport_kwargs)


def _bootstrap_outreach(transport_kwargs: dict) -> None:
    """Bootstrap and run the outreach MCP server.

    In standalone mode, pipeline/engagement/config are None — outreach_send
    returns 'not initialized'. Read-only tools (outreach_queue, outreach_digest,
    outreach_engagement) work with just DB.
    """
    from genesis.mcp.outreach_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        from genesis.db.connection import get_db
        from genesis.mcp.outreach_mcp import init_outreach_mcp
        from genesis.observability.provider_activity import ProviderActivityTracker

        # Long-lived shared connection via SerializedConnection (see _bootstrap_health).
        db = await get_db(_DEFAULT_DB, foreign_keys=False)

        try:
            # Standalone: pipeline=None means outreach_send returns "not initialized".
            # DB-only tools (outreach_queue, outreach_digest, outreach_engagement) work.
            # Bootstrap standalone router for LLM-dependent tools
            try:
                from genesis.routing.standalone import create_standalone_router
            except ImportError:
                logger.warning("genesis.routing.standalone not available", exc_info=True)
            else:
                create_standalone_router()

            # Tracker enables InstrumentationMiddleware + its read-snapshot
            # boundary (WS-15 follow-up) — see _bootstrap_memory.
            tracker = ProviderActivityTracker()
            tracker.set_db(db)
            init_outreach_mcp(
                pipeline=None, engagement=None, config=None, db=db,
                activity_tracker=tracker,
            )
            clear_mcp_crash("outreach")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — outreach MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    _run_mcp(mcp, transport_kwargs)


def _bootstrap_discord_bot(transport_kwargs: dict) -> None:
    """Bootstrap and run the discord-bot MCP server.

    Provides read/write Discord access via bot token for campaign
    sessions. No DB connection needed — fully stateless.
    """
    import os

    from genesis.mcp.discord_bot_mcp import init_discord_bot, mcp

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        logger.error("DISCORD_BOT_TOKEN not set — discord-bot cannot start")
        return

    init_discord_bot(bot_token=bot_token)
    clear_mcp_crash("discord-bot")
    _run_mcp(mcp, transport_kwargs)


_BOOTSTRAPPERS = {
    "health": _bootstrap_health,
    "memory": _bootstrap_memory,
    "outreach": _bootstrap_outreach,
    "recon": _bootstrap_recon,
    "discord-bot": _bootstrap_discord_bot,
}


def _run_mcp(mcp_instance, transport_kwargs: dict) -> None:
    """Run an MCP server with the configured transport.

    For stdio: delegates directly to mcp.run().
    For HTTP with auth: injects a raw ASGI auth wrapper via FastMCP's
    middleware parameter, then delegates to mcp.run() so lifespan
    handling works correctly.
    """
    auth_token = transport_kwargs.pop("_auth_token", None)

    if auth_token and transport_kwargs["transport"] != "stdio":
        transport_kwargs["middleware"] = [_bearer_auth_middleware(auth_token)]

    mcp_instance.run(**transport_kwargs)


def _bearer_auth_middleware(expected_token: str):
    """Create a raw ASGI middleware for bearer token auth.

    Returns a Starlette Middleware wrapping a pure-ASGI class so SSE
    streaming responses pass through without buffering (unlike
    BaseHTTPMiddleware which breaks text/event-stream).
    """
    import hmac
    import json as _json

    from starlette.middleware import Middleware

    _token = expected_token

    class _AuthGuard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] not in ("http", "websocket"):
                return await self.app(scope, receive, send)

            headers = dict(scope.get("headers", []))
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], _token):
                return await self.app(scope, receive, send)

            if scope["type"] == "http":
                body = _json.dumps({"error": "Unauthorized"}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return

            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4001})
                return

    return Middleware(_AuthGuard)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING,
        format="%(name)s %(levelname)s: %(message)s",
        stream=sys.stderr,  # MCP uses stdout for protocol; logs go to stderr
    )

    # Load infrastructure URLs and provider API keys from secrets.env.
    # MCP servers need these to reach Ollama, Qdrant, and embedding APIs.
    # Principle of least privilege: only load vars the MCP servers actually use.
    # Without the API keys, EmbeddingProvider gets zero backends and silently degrades.
    _MCP_VARS = {
        # Infrastructure
        "OLLAMA_URL", "QDRANT_URL", "GENESIS_DB_PATH", "GENESIS_CC_PROJECT_ID",
        # Browser (CDP remote backend)
        "GENESIS_CDP_URL",
        # Embedding providers (required for memory MCP)
        "API_KEY_DEEPINFRA", "API_KEY_QWEN",
        # LLM providers (used by recon/outreach MCP tools)
        "GOOGLE_API_KEY", "API_KEY_GROQ", "API_KEY_MISTRAL", "API_KEY_OPENROUTER",
        "API_KEY_DEEPSEEK",
        # Ollama config
        "GENESIS_ENABLE_OLLAMA", "OLLAMA_EMBEDDING_MODEL",
        # HTTP transport auth
        "GENESIS_MCP_HTTP_TOKEN",
        # Discord bot (used by discord-bot MCP server)
        "DISCORD_BOT_TOKEN",
    }
    import os

    from genesis.env import secrets_path

    secrets = secrets_path()
    if secrets.exists():
        from dotenv import dotenv_values

        for key, value in dotenv_values(secrets).items():
            if key in _MCP_VARS and key not in os.environ and value:
                os.environ[key] = value

    transport_kwargs = _build_transport_kwargs(args)

    # HTTP transport: validate auth token is configured
    if args.transport == "streamable-http":
        token = args.auth_token or os.environ.get("GENESIS_MCP_HTTP_TOKEN", "")
        if not token:
            logger.error(
                "HTTP transport requires auth token. Set GENESIS_MCP_HTTP_TOKEN "
                "env var or pass --auth-token."
            )
            sys.exit(1)
        transport_kwargs["_auth_token"] = token
        logger.warning(
            "Starting %s MCP server on http://%s:%d/mcp",
            args.server,
            transport_kwargs["host"],
            transport_kwargs["port"],
        )

    if not is_genesis_enabled():
        _run_disabled_stub(transport_kwargs)
        return

    bootstrapper = _BOOTSTRAPPERS[args.server]
    try:
        bootstrapper(transport_kwargs)
    except Exception:
        _record_mcp_crash(args.server)
        raise


# ── MCP crash reporting ───────────────────────────────────────────────
# Per-server crash files under ~/.genesis/mcp_crashes/<server>.json.
# Written on crash, cleared on successful lifespan init.
# Read by SessionStart hook + health snapshot to surface failures loudly.

_MCP_CRASH_DIR = Path.home() / ".genesis" / "mcp_crashes"


def _record_mcp_crash(server_name: str) -> None:
    """Write crash info so SessionStart hook and health snapshot can report it."""
    import json
    import traceback
    from datetime import UTC, datetime

    try:
        _MCP_CRASH_DIR.mkdir(parents=True, exist_ok=True)
        crash_file = _MCP_CRASH_DIR / f"{server_name}.json"
        tb = traceback.format_exc()
        crash_file.write_text(json.dumps({
            "server": server_name,
            "error": tb.splitlines()[-1] if tb.strip() else "unknown",
            "traceback": "\n".join(tb.splitlines()[-15:]),
            "timestamp": datetime.now(UTC).isoformat(),
        }, indent=2))
    except Exception:
        pass  # Best-effort — don't mask the original crash


def clear_mcp_crash(server_name: str) -> None:
    """Remove crash file after successful startup."""
    try:
        crash_file = _MCP_CRASH_DIR / f"{server_name}.json"
        if crash_file.exists():
            crash_file.unlink()
    except Exception:
        pass


if __name__ == "__main__":
    main()
