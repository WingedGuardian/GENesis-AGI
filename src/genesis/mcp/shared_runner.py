"""Explicit shared startup; legacy launch never imports the SDK runner."""

import asyncio
import os
import re
from datetime import datetime
from pathlib import Path


def require_source_checkout(source_root):
    """Refuse a launcher/config root different from this imported checkout."""
    import genesis

    root = Path(source_root).resolve()
    if (root != Path(__file__).resolve().parents[3]
            or Path(genesis.__file__).resolve() != root / "src/genesis/__init__.py"):
        raise ValueError("Shared MCP launcher and imported source differ")
    return root


def run_shared(service, *, source_root, db_path, status_path, cache_dir, enabled,
               setup_routing, setup_health_actions):
    source_root = require_source_checkout(source_root)
    from genesis.mcp.shared_http import require_qualified_stack

    require_qualified_stack()
    from genesis.caller_context import enter_shared_service
    from genesis.observability.mcp_spawn_identity import (
        capture_spawn_identity,
        spawn_at,
        spawn_commit,
    )

    enter_shared_service()
    os.environ.pop("GENESIS_SLOT", None)
    capture_spawn_identity(source_root=source_root, persist_session=False)
    commit, started = spawn_commit(), spawn_at()
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ValueError("Shared MCP requires known loaded commit identity")
    try:
        if datetime.fromisoformat(started).utcoffset() is None:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Shared MCP requires an aware startup timestamp") from None

    from genesis.caller_credentials import CallerResolver
    from genesis.mcp.shared_http._auth import usable_credentials
    from genesis.mcp.shared_settings import load_shared_settings

    settings = load_shared_settings(source_root, service, os.environ)
    if not callable(enabled) or enabled() is not True:
        raise ValueError("Shared MCP cannot start disabled")
    resolver = CallerResolver.load()
    usable_credentials(resolver, service)
    # Import version-specific code only after the qualified-stack admission.
    from genesis.mcp.shared_server import serve_shared

    asyncio.run(serve_shared(settings, resolver=resolver, loaded_commit=commit, started_at=started,
        db_path=db_path, status_path=status_path, cache_dir=cache_dir, enabled=enabled,
        setup_routing=setup_routing, setup_health_actions=setup_health_actions))
