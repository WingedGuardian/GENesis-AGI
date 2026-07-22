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
import os

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
    roster_model: str | None = None,
    deliver_to_origin: bool = False,
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
        return {
            "error": f"Invalid model '{model}'. Must be one of: "
            f"{', '.join(m.value for m in CCModel)}"
        }

    effort_lower = effort.lower()
    try:
        EffortLevel(effort_lower)
    except ValueError:
        return {"error": f"Invalid effort '{effort}'. Must be one of: low, medium, high, xhigh, max"}

    # Early validation for an explicit roster_model so the caller gets immediate
    # feedback on a typo (key-presence is re-checked at dispatch = fail-loud).
    if roster_model is not None:
        from genesis.cc import roster as _roster

        if _roster.resolve(roster_model) is None:
            known = ", ".join((_roster.load_roster().get("models") or {}).keys())
            return {"error": f"Unknown roster_model '{roster_model}'. Known: {known}"}

    # deliver_to_origin: capture the foreground origin so the server can route
    # the result back to the calling conversation. GENESIS_SESSION_ID is the
    # foreground cc_sessions row id, set by the CCInvoker and inherited into this
    # MCP child process. Only foreground/interactive sessions can reach this tool
    # (it is in the background disallow list), so this id is always a foreground
    # origin — never a background session (which would be circular).
    origin_session_id = None
    delivery_mode = None
    if deliver_to_origin:
        origin_session_id = os.environ.get("GENESIS_SESSION_ID") or None
        if origin_session_id:
            delivery_mode = "result"
        else:
            logger.warning(
                "direct_session_run(deliver_to_origin=True) with no "
                "GENESIS_SESSION_ID in env — falling back to default notification "
                "(no origin delivery)"
            )

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
            roster_model=roster_model,
            origin_session_id=origin_session_id,
            delivery_mode=delivery_mode,
        )
        return {
            "queue_id": queue_id,
            "status": "queued",
            "profile": profile,
            "message": (
                f"Session queued with '{profile}' profile. "
                "The Genesis server will dispatch it within seconds. "
                + (
                    "The result will be delivered back to this conversation "
                    "when it completes."
                    if delivery_mode == "result"
                    else "You'll get a Telegram alert if it fails (successful "
                    "runs are silent — poll direct_session_status for output)."
                )
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
        "transcript_path": metadata.get("transcript_path"),
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
            LIMIT ?""",  # noqa: S608 - literal SQL fragments; values bound as parameters
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
    roster_model: str | None = None,
    deliver_to_origin: bool = False,
) -> dict:
    """Spawn a directed background CC session with profile-based tool restrictions.

    The session is queued and dispatched by the Genesis server, so it
    outlives this MCP session. By default the session is FIRE-AND-FORGET:
    successful runs are silent (poll ``direct_session_status`` for output) and
    only failures raise a Telegram alert. Set ``deliver_to_origin=True`` to have
    the terminal outcome (success AND failure) delivered back to THIS
    conversation when it finishes — use this whenever you hand off long work from
    a channel and tell the user you'll report back.

    Profiles control what the session can do:
    - observe: Read everything, change nothing. Browser viewing, memory reads, web search.
    - research: Observe + memory/observation writes + follow-ups. Stores findings for future use.
    - interact: Full browser interaction + memory writes + outreach_send + follow-ups.
      Use for workflows that operate external platforms and communicate with the user.

    All profiles block: Bash, Edit, Write, task_submit, settings_update.

    Args:
        prompt: The full instructions for the background session
        profile: Safety profile (observe, interact, research)
        model: LLM model (sonnet, opus, haiku, fable)
        effort: Effort level (low, medium, high, xhigh, max)
        timeout_minutes: Max runtime in minutes (default 15, max 60)
        notify: Send a Telegram alert if the session FAILS (successful runs are
            silent unless deliver_to_origin is set). Has no effect when
            deliver_to_origin=True.
        deliver_to_origin: Deliver the terminal outcome (success and failure)
            back to the conversation this tool was called from. Requires a
            foreground channel session as the caller.
        roster_model: Optionally run this session on a specific roster model
            (e.g. "glm-5.2") instead of the default — intentional model
            selection. Omit/None to use the active default. Fails the session
            if the named model is unknown or its API key is not configured.
    """
    return await _impl_direct_session_run(
        prompt,
        profile,
        model,
        effort,
        timeout_minutes,
        notify,
        roster_model,
        deliver_to_origin,
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
