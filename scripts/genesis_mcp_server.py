#!/usr/bin/env python3
"""Genesis MCP Server — standalone stdio wrapper for CC integration.

Launches one of the Genesis MCP servers (health, memory, recon) as a stdio
process that Claude Code can connect to via .mcp.json auto-discovery.

Usage:
    python genesis_mcp_server.py --server health|memory|outreach|recon

Architecture note: mcp.run(transport="stdio") owns the event loop (via anyio).
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

_VALID_SERVERS = {"health", "memory", "outreach", "recon"}
_DEFAULT_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_DEFAULT_STATUS = Path.home() / ".genesis" / "status.json"


def _default_db_path() -> Path:
    from genesis.env import genesis_db_path

    return genesis_db_path()


_DEFAULT_DB = _default_db_path()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Genesis MCP Server (standalone)")
    parser.add_argument(
        "--server",
        required=True,
        choices=sorted(_VALID_SERVERS),
        help="Which MCP server to run",
    )
    return parser.parse_args(argv)


def is_genesis_enabled(*, flag_path: Path = _DEFAULT_FLAG) -> bool:
    """Check if Genesis CC context is enabled via flag file."""
    return flag_path.exists()


def _run_disabled_stub() -> None:
    """Run a minimal MCP server that reports Genesis context is disabled."""
    from fastmcp import FastMCP

    stub = FastMCP("genesis-disabled")

    @stub.tool()
    async def genesis_status() -> str:
        """Genesis context is disabled. Use /genesis on to enable."""
        return "Genesis context is disabled. Run /genesis on in interactive CC to enable."

    stub.run(transport="stdio")


def _bootstrap_health() -> None:
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
        mcp.run(transport="stdio")
        return

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        import aiosqlite

        from genesis.db.connection import BUSY_TIMEOUT_MS

        db = await aiosqlite.connect(str(_DEFAULT_DB))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        try:
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
                from genesis.db.schema import TABLES

                await db.execute(TABLES["direct_session_queue"])
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dsq_status_created "
                    "ON direct_session_queue(status, created_at)"
                )
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

            clear_mcp_crash("health")
            yield
        finally:
            from genesis.mcp.health.browser import async_cleanup as _browser_cleanup
            await _browser_cleanup()
            await db.close()

    mcp._lifespan = _lifespan
    mcp.run(transport="stdio")


def _bootstrap_memory() -> None:
    """Bootstrap and run the memory MCP server.

    Requires Qdrant + Ollama for embeddings. Opens aiosqlite connection
    inside the MCP event loop via FastMCP's lifespan hook.
    """
    from genesis.env import qdrant_url
    from genesis.mcp.memory_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        import aiosqlite
        from qdrant_client import QdrantClient

        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.mcp.memory_mcp import init
        from genesis.memory.embeddings import EmbeddingProvider

        db = await aiosqlite.connect(str(_DEFAULT_DB))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

        try:
            qdrant = QdrantClient(url=qdrant_url(), timeout=5)
            embedding = EmbeddingProvider()
            init(db=db, qdrant_client=qdrant, embedding_provider=embedding)
            clear_mcp_crash("memory")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — memory MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    mcp.run(transport="stdio")


def _bootstrap_recon() -> None:
    """Bootstrap and run the recon MCP server."""
    from genesis.mcp.recon_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        import aiosqlite

        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.mcp.recon_mcp import init_recon_mcp

        db = await aiosqlite.connect(str(_DEFAULT_DB))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

        try:
            init_recon_mcp(db=db)
            clear_mcp_crash("recon")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — recon MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    mcp.run(transport="stdio")


def _bootstrap_outreach() -> None:
    """Bootstrap and run the outreach MCP server.

    In standalone mode, pipeline/engagement/config are None — outreach_send
    returns 'not initialized'. Read-only tools (outreach_queue, outreach_digest,
    outreach_engagement) work with just DB.
    """
    from genesis.mcp.outreach_mcp import mcp

    @asynccontextmanager
    async def _lifespan(server) -> AsyncIterator[None]:
        import aiosqlite

        from genesis.db.connection import BUSY_TIMEOUT_MS
        from genesis.mcp.outreach_mcp import init_outreach_mcp

        db = await aiosqlite.connect(str(_DEFAULT_DB))
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

        try:
            # Standalone: pipeline=None means outreach_send returns "not initialized".
            # DB-only tools (outreach_queue, outreach_digest, outreach_engagement) work.
            init_outreach_mcp(pipeline=None, engagement=None, config=None, db=db)
            clear_mcp_crash("outreach")
            yield
        finally:
            await db.close()

    if not _DEFAULT_DB.exists():
        logger.error("DB not found at %s — outreach MCP cannot start", _DEFAULT_DB)
        return

    mcp._lifespan = _lifespan
    mcp.run(transport="stdio")


_BOOTSTRAPPERS = {
    "health": _bootstrap_health,
    "memory": _bootstrap_memory,
    "outreach": _bootstrap_outreach,
    "recon": _bootstrap_recon,
}


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
        # Embedding providers (required for memory MCP)
        "API_KEY_DEEPINFRA", "API_KEY_QWEN",
        # LLM providers (used by recon/outreach MCP tools)
        "GOOGLE_API_KEY", "API_KEY_GROQ", "API_KEY_MISTRAL", "API_KEY_OPENROUTER",
        "API_KEY_DEEPSEEK",
        # Ollama config
        "GENESIS_ENABLE_OLLAMA", "OLLAMA_EMBEDDING_MODEL",
    }
    from genesis.env import secrets_path

    secrets = secrets_path()
    if secrets.exists():
        import os

        from dotenv import dotenv_values

        for key, value in dotenv_values(secrets).items():
            if key in _MCP_VARS and key not in os.environ and value:
                os.environ[key] = value

    if not is_genesis_enabled():
        _run_disabled_stub()
        return

    bootstrapper = _BOOTSTRAPPERS[args.server]
    try:
        bootstrapper()
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
