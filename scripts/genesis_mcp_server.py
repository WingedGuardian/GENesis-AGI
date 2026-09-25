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

        from genesis.db.connection import ReadConnectionPool, get_db
        from genesis.env import recall_read_pool_off, session_read_pool_size
        from genesis.mcp.memory_mcp import init
        from genesis.memory.embeddings import EmbeddingProvider
        from genesis.memory.reranker import VoyageReranker
        from genesis.observability.provider_activity import ProviderActivityTracker

        # Long-lived shared connection via SerializedConnection (see _bootstrap_health).
        db = await get_db(_DEFAULT_DB, foreign_keys=False)

        # Read-only recall pool (WS-1 PR-1): reads leave the shared write
        # connection, so this child stops contending for the WAL writer slot on
        # every recall read — fewer convoy participants. Mirrors the server-side
        # wiring in runtime/init/memory.py: same size knob, same kill switch,
        # and failure degrades to read_pool=None (all reads on the shared
        # connection — the pre-pool behavior), never a startup failure.
        read_pool = None
        if not recall_read_pool_off():
            try:
                # session_read_pool_size, NOT recall_read_pool_size: this process
                # is one MCP child PER CC SESSION, so a host-derived size is
                # multiplied by the number of live sessions. The server's pool is
                # the one that carries every session's per-prompt recall.
                pool = ReadConnectionPool(_DEFAULT_DB, size=session_read_pool_size())
                await pool.open()
                read_pool = pool
            except Exception:
                logger.warning(
                    "recall read pool unavailable — reads stay on the shared connection",
                    exc_info=True,
                )
                read_pool = None

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
            # Wire the Voyage reranker into the standalone MCP retriever too, so
            # memory_recall / knowledge_recall rerank here exactly as in the
            # full runtime. Degrades to a no-op without API_KEY_VOYAGE.
            reranker = VoyageReranker()
            init(db=db, qdrant_client=qdrant, embedding_provider=embedding,
                 activity_tracker=tracker, reranker=reranker, read_pool=read_pool)
            clear_mcp_crash("memory")
            yield
        finally:
            # Pool FIRST, then db: an in-flight read that loses the close race
            # gets ReadPoolClosed from acquire() and falls back to the shared
            # connection, which must therefore still be open. Nested so a pool
            # close failure can never leak the db handle.
            try:
                if read_pool is not None:
                    await read_pool.close()
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

    Provides read/write Discord access via bot token for campaign sessions.
    Opens a BEST-EFFORT genesis.db connection (WS5) so send_reply can record
    capability-shadow observations — the server stays fully functional if the
    DB is missing or fails to open (db=None => shadow is a no-op).
    """
    import os

    from genesis.mcp.discord_bot_mcp import init_discord_bot, mcp

    bot_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not bot_token:
        logger.error("DISCORD_BOT_TOKEN not set — discord-bot cannot start")
        return

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        from genesis.db.connection import get_db

        # WS5 capability-shadow DB — opened ONCE for the server's lifetime (never
        # per-call, to avoid a WAL-lock hang on the reply path). NON-fatal: the
        # discord-bot's core job (send_reply) must work even with no shadow DB.
        db = None
        if _DEFAULT_DB.exists():
            try:
                db = await get_db(_DEFAULT_DB, foreign_keys=False)
            except Exception:
                logger.warning(
                    "discord-bot: shadow DB open failed — continuing without shadow",
                    exc_info=True,
                )
        init_discord_bot(bot_token=bot_token, db=db)
        clear_mcp_crash("discord-bot")
        try:
            yield
        finally:
            if db is not None:
                await db.close()

    mcp._lifespan = _lifespan
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

    Also suppresses FastMCP's docket task-queue worker — see
    ``_suppress_docket_worker``. This is the single chokepoint every
    bootstrapper routes through, which is why the suppression lives here
    rather than in each of the five.
    """
    auth_token = transport_kwargs.pop("_auth_token", None)

    if auth_token and transport_kwargs["transport"] != "stdio":
        transport_kwargs["middleware"] = [_bearer_auth_middleware(auth_token)]

    _suppress_docket_worker(mcp_instance)

    mcp_instance.run(**transport_kwargs)


def _suppress_docket_worker(mcp_instance) -> None:
    """Stop FastMCP starting a docket task-queue worker we never use.

    MEASURED 2026-09-24: 41 idle MCP server processes burned 1.37 CPU cores
    CONTINUOUSLY, uniformly across all five server types. None of it was
    Genesis code. fastmcp 2.14.6 enters ``_docket_lifespan`` as a sibling of the
    user lifespan (server.py:572-575), builds ``Docket(url="memory://")`` and
    runs ``worker.run_forever()`` (server.py:476). ``memory://`` is fakeredis,
    whose pubsub read is a literal ``await asyncio.sleep(0.01)`` loop that its
    own comment calls a "kludge" (fakeredis/aioredis.py:143-154) — a 100 Hz spin
    for a queue that is always empty.

    Always empty because Genesis registers NOTHING with it: we never pass
    ``tasks=`` to ``FastMCP``, so every tool resolves to ``mode="forbidden"`` and
    fastmcp's own registration loop skips all of them (server.py:418-423).
    Suppressing the worker therefore removes no capability — a claim pinned by
    ``tests/test_mcp/test_docket_worker_suppressed.py``, which fails if any tool
    ever opts in.

    ``_is_mounted`` is the attribute fastmcp's own ``mount()`` sets for exactly
    this purpose (server.py:2714), and server.py:403 is its only read. Upstream
    reached the same conclusion: prefecthq/fastmcp#2887 closed with the
    maintainer noting docket "has been removed as a default in 3.0". This is a
    backport of that decision.

    ⚠ It is a PRIVATE attribute. **Delete this function and its tests when
    Genesis moves to fastmcp 3.x/4.x** — the pin exists so a version bump that
    renames or removes the gate fails loudly in tests instead of silently
    restoring ~1.4 cores of idle burn.

    One reachable protocol delta, stated so nobody re-derives it: the three
    ``tasks/*`` LOOKUP handlers (``fastmcp/server/tasks/protocol.py:70,171,304``)
    return ``INTERNAL_ERROR "Background tasks require Docket"`` instead of
    ``INVALID_PARAMS "Task <id> not found"`` for a bogus taskId. No task can ever
    exist — ``server.py:715`` raises METHOD_NOT_FOUND for a ``forbidden`` tool
    before docket is consulted — so nothing reachable changes. The initialize
    handshake is byte-identical (``get_task_capabilities`` is unconditional).

    Best-effort by design: a failure here costs CPU, never correctness, so it
    must never stop a server booting.

    **Why a pre-state check and not a try/except.** ``FastMCP`` defines no
    ``__slots__``, so ``obj._is_mounted = True`` SUCCEEDS on a version that
    renamed or removed the attribute — it just creates a dead one nobody reads.
    A ``try/except`` there guards the failure that cannot happen and misses the
    one that will: the burn would return silently, on every install, with no
    exception and no log line. Checking that the attribute EXISTS FIRST is what
    makes version drift loud at runtime rather than only in CI.
    """
    sentinel = object()
    if getattr(mcp_instance, "_is_mounted", sentinel) is sentinel:
        logger.warning(
            "fastmcp (%s) no longer exposes _is_mounted; the docket-worker "
            "suppression is INERT and every MCP server will idle-spin at ~4%% "
            "of a CPU core (see tests/test_mcp/test_docket_worker_suppressed.py "
            "and _suppress_docket_worker's docstring)",
            _fastmcp_version(),
        )
        return

    mcp_instance._is_mounted = True


def _fastmcp_version() -> str:
    """fastmcp's version, for the drift warning. Never raises."""
    try:
        import fastmcp

        return getattr(fastmcp, "__version__", "unknown")
    except Exception:  # noqa: BLE001 — a diagnostic must not break startup
        return "unknown"


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
        # SQLite busy_timeout override (must pass the allowlist or a
        # secrets.env-set value would be silently dropped before the
        # setdefault below)
        "GENESIS_DB_BUSY_TIMEOUT_MS",
        # Recall read-pool knobs — _bootstrap_memory honors the same kill
        # switch + size as the server runtime, so a secrets.env-configured
        # value must reach the child too (Codex P2 on #1302)
        # GENESIS_SESSION_READ_POOL_SIZE is the one _bootstrap_memory actually
        # reads now (this process is a per-session child, not the server) —
        # without it here the documented lever is INERT, which is the third
        # instance of this class in this list after #1302 and #1587.
        "GENESIS_RECALL_READ_POOL_OFF",
        "GENESIS_RECALL_READ_POOL_SIZE",
        "GENESIS_SESSION_READ_POOL_SIZE",
        # deliberate() (Model Fusion) timeout budget — the health MCP server hosts the
        # `deliberate` tool, so a secrets.env-set override must reach this child or the
        # documented knob is inert and every call silently stays at the 1000s default
        # (Codex P2 on #1587).
        "GENESIS_DELIBERATE_TIMEOUT_S",
    }
    import os

    from genesis.env import secrets_path

    secrets = secrets_path()
    if secrets.exists():
        from dotenv import dotenv_values

        for key, value in dotenv_values(secrets).items():
            if key in _MCP_VARS and key not in os.environ and value:
                os.environ[key] = value

    # MCP children wait LONGER for the WAL writer slot than the server's 5s
    # default (WS-1 PR-1, follow-up 2d88740d): N child processes race ONE
    # writer slot with no queue fairness, so a child's write loses to any
    # server-side batch that outlasts 5s. 15s lengthens how long a write
    # WAITS — it never shortens or skips work. setdefault AFTER the secrets
    # load so an operator override (env or secrets.env) wins; applied at
    # connect time by get_db/get_raw_db/open_ro_connection via
    # env.db_busy_timeout_ms(). Worst case if the DB is genuinely wedged:
    # 4 attempts x 15s + ~1.75s backoff ≈ 62s for one write episode (the
    # SerializedConnection retry holds its asyncio lock throughout) — accepted:
    # failing that write faster would help nothing.
    os.environ.setdefault("GENESIS_DB_BUSY_TIMEOUT_MS", "15000")

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

    # Snapshot this subprocess's code identity ONCE, before any tool can run, so
    # the stale-code guard can detect a deploy that lands after this start. Must
    # be eager (not lazy-on-first-guarded-call) — see mcp_spawn_identity.
    from genesis.observability.mcp_spawn_identity import capture_spawn_identity

    capture_spawn_identity()

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
