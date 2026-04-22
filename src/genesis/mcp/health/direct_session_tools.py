"""MCP tools for spawning and monitoring directed background CC sessions.

Follows the ``task_tools.py`` pattern: module-level state, init function
for runtime wiring, ``_impl_*`` functions (testable without FastMCP),
and ``@mcp.tool()`` decorated public wrappers.
"""

from __future__ import annotations

import contextlib
import json
import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Module-level state (wired at runtime via init_direct_session_tools)
_runner = None


def init_direct_session_tools(runner) -> None:
    """Wire the DirectSessionRunner. Called from runtime init."""
    global _runner
    _runner = runner
    logger.info("Direct session MCP tools wired")


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_direct_session_run(
    prompt: str,
    profile: str = "observe",
    model: str = "sonnet",
    effort: str = "high",
    timeout_minutes: int = 15,
    notify: bool = True,
) -> dict:
    """Spawn a directed background CC session."""
    if _runner is None:
        return {"error": "Direct session runner not initialized"}

    from genesis.cc.direct_session import VALID_PROFILES
    from genesis.cc.types import CCModel, EffortLevel

    if profile not in VALID_PROFILES:
        return {
            "error": f"Invalid profile '{profile}'. "
            f"Must be one of: {', '.join(sorted(VALID_PROFILES))}",
        }

    model_upper = model.upper()
    if model_upper not in CCModel.__members__:
        return {"error": f"Invalid model '{model}'. Must be one of: sonnet, opus, haiku"}

    effort_upper = effort.upper()
    if effort_upper not in EffortLevel.__members__:
        return {"error": f"Invalid effort '{effort}'. Must be one of: low, medium, high, max"}

    try:
        from genesis.cc.direct_session import DirectSessionRequest

        request = DirectSessionRequest(
            prompt=prompt,
            profile=profile,
            model=CCModel(model_upper),
            effort=EffortLevel(effort_upper),
            timeout_s=min(timeout_minutes * 60, 3600),  # cap at 1 hour
            notify=notify,
            caller_context="mcp_tool",
        )
        session_id = await _runner.spawn(request)
        return {
            "session_id": session_id,
            "status": "spawned",
            "profile": profile,
            "message": (
                f"Background session started with '{profile}' profile. "
                "You'll be notified via Telegram when it completes."
            ),
        }
    except Exception as exc:
        logger.error("direct_session_run failed", exc_info=True)
        return {"error": f"Failed to spawn session: {exc}"}


async def _impl_direct_session_status(session_id: str) -> dict:
    """Check status of a direct session."""
    import genesis.mcp.health as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return {"error": "Health service not available"}

    db = getattr(svc, "_db", None)
    if db is None:
        return {"error": "Database not available"}

    from genesis.db.crud import cc_sessions

    row = await cc_sessions.get_by_id(db, session_id)
    if row is None:
        return {"error": f"Session {session_id} not found"}

    metadata = {}
    if row.get("metadata"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            metadata = json.loads(row["metadata"])

    return {
        "session_id": session_id,
        "status": row.get("status", "unknown"),
        "source_tag": row.get("source_tag"),
        "model": row.get("model"),
        "effort": row.get("effort"),
        "started_at": row.get("started_at"),
        "cost_usd": row.get("cost_usd", 0),
        "input_tokens": row.get("input_tokens", 0),
        "output_tokens": row.get("output_tokens", 0),
        "profile": metadata.get("profile"),
        "output_preview": (metadata.get("output_text") or "")[:500],
        "tools_summary": metadata.get("tools_summary", {}),
        "error": metadata.get("error"),
        "duration_s": metadata.get("duration_s"),
        "caller_context": metadata.get("caller_context"),
    }


async def _impl_direct_session_list(
    include_completed: bool = True,
    limit: int = 20,
) -> dict:
    """List recent direct sessions."""
    import genesis.mcp.health as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return {"error": "Health service not available"}

    db = getattr(svc, "_db", None)
    if db is None:
        return {"error": "Database not available"}

    status_clause = "" if include_completed else "AND status = 'active'"

    cursor = await db.execute(
        f"""SELECT id, status, model, effort, source_tag,
                   started_at, cost_usd, metadata
            FROM cc_sessions
            WHERE source_tag = 'direct_session'
            {status_clause}
            ORDER BY started_at DESC
            LIMIT ?""",
        (limit,),
    )
    rows = await cursor.fetchall()

    sessions = []
    for row in rows:
        r = dict(row)
        metadata = {}
        if r.get("metadata"):
            with contextlib.suppress(json.JSONDecodeError, TypeError):
                metadata = json.loads(r["metadata"])
        sessions.append({
            "session_id": r["id"],
            "status": r["status"],
            "model": r["model"],
            "profile": metadata.get("profile", "unknown"),
            "started_at": r["started_at"],
            "cost_usd": r.get("cost_usd", 0),
            "duration_s": metadata.get("duration_s"),
            "error": metadata.get("error"),
            "tools_summary": metadata.get("tools_summary", {}),
        })

    active_count = _runner.active_count() if _runner else 0

    return {
        "sessions": sessions,
        "count": len(sessions),
        "active_now": active_count,
    }


# ---------------------------------------------------------------------------
# MCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def direct_session_run(
    prompt: str,
    profile: str = "observe",
    model: str = "sonnet",
    effort: str = "high",
    timeout_minutes: int = 15,
    notify: bool = True,
) -> dict:
    """Spawn a directed background CC session with profile-based tool restrictions.

    The session runs independently and reports results via Telegram.
    Returns immediately with a session_id for tracking.

    Profiles control what the session can do:
    - observe: Read everything, change nothing. Browser viewing, memory reads, web search.
    - interact: Observe + browser clicks/fills. For social media, web tasks.
    - research: Observe + memory/observation writes. Stores findings for future use.

    All profiles block: Bash, Edit, Write, outreach_send, task_submit, settings_update.

    Args:
        prompt: The full instructions for the background session
        profile: Safety profile (observe, interact, research)
        model: LLM model (sonnet, opus, haiku)
        effort: Effort level (low, medium, high, max)
        timeout_minutes: Max runtime in minutes (default 15, max 60)
        notify: Send Telegram notification on completion/failure
    """
    return await _impl_direct_session_run(
        prompt, profile, model, effort, timeout_minutes, notify,
    )


@mcp.tool()
async def direct_session_status(session_id: str) -> dict:
    """Check the status and results of a direct background session.

    Returns status, cost, duration, output preview, tools used, and any errors.

    Args:
        session_id: Session ID from direct_session_run
    """
    return await _impl_direct_session_status(session_id)


@mcp.tool()
async def direct_session_list(
    include_completed: bool = True,
    limit: int = 20,
) -> dict:
    """List recent direct background sessions.

    Args:
        include_completed: Include finished sessions (default true)
        limit: Max results (default 20)
    """
    return await _impl_direct_session_list(include_completed, limit)
