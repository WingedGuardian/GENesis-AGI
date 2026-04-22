"""MCP tools for spawning and monitoring directed background CC sessions.

Follows the ``task_tools.py`` pattern: module-level state, init function
for runtime wiring, ``_impl_*`` functions (testable without FastMCP),
and ``@mcp.tool()`` decorated public wrappers.

Sessions are dispatched via a DB queue (``direct_session_queue`` table).
The MCP tool enqueues the request; the Genesis server's poll loop in
``runtime/init/direct_session.py`` claims and dispatches to
``DirectSessionRunner.spawn()``.  This decouples the session lifecycle
from the MCP server process — sessions outlive the calling CC session.
"""

from __future__ import annotations

import contextlib
import json
import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

# Module-level state (wired at runtime via init_direct_session_tools)
_runner = None
_db = None


def init_direct_session_tools(*, db=None, runner=None) -> None:
    """Wire direct session tools.

    In runtime mode: both db and runner are provided. The poll loop
    in runtime/init/direct_session.py handles dispatch.
    In standalone MCP mode: only db is provided. Items are enqueued
    for the Genesis server's poll loop to pick up.
    """
    global _runner, _db
    _db = db
    _runner = runner
    logger.info(
        "Direct session MCP tools wired (db=%s, runner=%s)",
        db is not None,
        runner is not None,
    )


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
    """Enqueue a directed background CC session for dispatch."""
    if _db is None:
        return {"error": "Direct session tools not initialized (no DB)"}

    from genesis.cc.direct_session import VALID_PROFILES
    from genesis.cc.types import CCModel, EffortLevel

    if profile not in VALID_PROFILES:
        return {
            "error": f"Invalid profile '{profile}'. "
            f"Must be one of: {', '.join(sorted(VALID_PROFILES))}",
        }

    model_lower = model.lower()
    try:
        CCModel(model_lower)
    except ValueError:
        return {"error": f"Invalid model '{model}'. Must be one of: sonnet, opus, haiku"}

    effort_lower = effort.lower()
    try:
        EffortLevel(effort_lower)
    except ValueError:
        return {"error": f"Invalid effort '{effort}'. Must be one of: low, medium, high, max"}

    try:
        from genesis.db.crud import direct_session_queue as dsq

        queue_id = await dsq.enqueue(
            _db,
            prompt=prompt,
            profile=profile,
            model=model_lower,
            effort=effort_lower,
            timeout_s=min(timeout_minutes * 60, 3600),  # cap at 1 hour
            notify=notify,
            caller_context="mcp_tool",
        )
        return {
            "queue_id": queue_id,
            "status": "queued",
            "profile": profile,
            "message": (
                f"Session queued with '{profile}' profile. "
                "The Genesis server will dispatch it within seconds. "
                "You'll be notified via Telegram when it completes."
            ),
        }
    except Exception as exc:
        logger.error("direct_session_run failed", exc_info=True)
        return {"error": f"Failed to enqueue session: {exc}"}


async def _get_db():
    """Get a DB connection — prefer module-level, fall back to health service."""
    if _db is not None:
        return _db
    import genesis.mcp.health as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return None
    return getattr(svc, "_db", None)


async def _impl_direct_session_status(lookup_id: str) -> dict:
    """Check status of a direct session by queue_id or session_id."""
    db = await _get_db()
    if db is None:
        return {"error": "Database not available"}

    # If it looks like a queue_id, look up the queue row first
    if lookup_id.startswith("dsq-"):
        from genesis.db.crud import direct_session_queue as dsq

        q_row = await dsq.get_by_id(db, lookup_id)
        if q_row is None:
            return {"error": f"Queue item {lookup_id} not found"}

        payload = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            payload = json.loads(q_row.get("payload_json", "{}"))

        result = {
            "queue_id": lookup_id,
            "queue_status": q_row["status"],
            "created_at": q_row["created_at"],
            "profile": payload.get("profile"),
            "model": payload.get("model"),
        }

        # If dispatched, also include session details
        if q_row.get("session_id"):
            result["session_id"] = q_row["session_id"]
            session_info = await _lookup_session(db, q_row["session_id"])
            if session_info:
                result.update(session_info)
        elif q_row["status"] == "failed":
            result["error"] = q_row.get("error_message")

        return result

    # Otherwise treat as session_id (backward compat)
    session_info = await _lookup_session(db, lookup_id)
    if session_info is None:
        return {"error": f"Session {lookup_id} not found"}
    return {"session_id": lookup_id, **session_info}


async def _lookup_session(db, session_id: str) -> dict | None:
    """Look up session details from cc_sessions."""
    from genesis.db.crud import cc_sessions

    row = await cc_sessions.get_by_id(db, session_id)
    if row is None:
        return None

    metadata = {}
    if row.get("metadata"):
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            metadata = json.loads(row["metadata"])

    return {
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
    db = await _get_db()
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

    # Include pending queue items
    pending_count = 0
    try:
        from genesis.db.crud import direct_session_queue as dsq

        pending_count = await dsq.count_pending(db)
    except Exception:
        pass

    return {
        "sessions": sessions,
        "count": len(sessions),
        "active_now": active_count,
        "queued": pending_count,
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

    The session is queued and dispatched by the Genesis server, so it
    outlives this MCP session. Results are reported via Telegram.

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
async def direct_session_status(lookup_id: str) -> dict:
    """Check the status and results of a direct background session.

    Accepts either a queue_id (dsq-...) from direct_session_run or a
    session_id. Returns status, cost, duration, output preview, tools
    used, and any errors.

    Args:
        lookup_id: Queue ID or session ID from direct_session_run
    """
    return await _impl_direct_session_status(lookup_id)


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
