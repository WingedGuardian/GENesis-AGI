"""session_set_model and session_set_effort tools."""

from __future__ import annotations

import logging

from genesis.cc.session_cache import persist_session_config as _persist_session_config
from genesis.mcp.health import mcp  # noqa: E402

logger = logging.getLogger(__name__)

_VALID_MODELS = {"sonnet", "opus", "haiku"}
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


async def _impl_session_set_model(session_id: str, model: str) -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service

    if not session_id or not session_id.strip():
        return {"error": "Session ID is required"}
    model = model.lower().strip()
    if model not in _VALID_MODELS:
        return {"error": f"Invalid model '{model}'. Valid: {', '.join(sorted(_VALID_MODELS))}"}
    if _service is None or _service._db is None:
        return {"error": "Database not available"}
    try:
        from genesis.db.crud import cc_sessions

        updated = await cc_sessions.update_model_effort(
            _service._db, session_id, model=model,
        )
        if not updated:
            return {"error": f"Session '{session_id}' not found"}
        _persist_session_config(model=model)
        return {"success": True, "model": model, "note": "Takes effect on your next response."}
    except Exception as exc:
        logger.error("session_set_model failed for %s: %s", session_id[:8], exc, exc_info=True)
        return {"error": f"Failed to update session model: {type(exc).__name__}: {exc}"}


async def _impl_session_set_effort(session_id: str, effort: str) -> dict:
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service

    if not session_id or not session_id.strip():
        return {"error": "Session ID is required"}
    effort = effort.lower().strip()
    if effort not in _VALID_EFFORTS:
        return {"error": f"Invalid effort '{effort}'. Valid: {', '.join(sorted(_VALID_EFFORTS))}"}
    if _service is None or _service._db is None:
        return {"error": "Database not available"}
    try:
        from genesis.db.crud import cc_sessions

        updated = await cc_sessions.update_model_effort(
            _service._db, session_id, effort=effort,
        )
        if not updated:
            return {"error": f"Session '{session_id}' not found"}
        _persist_session_config(effort=effort)
        return {"success": True, "effort": effort, "note": "Takes effect on your next response."}
    except Exception as exc:
        logger.error("session_set_effort failed for %s: %s", session_id[:8], exc, exc_info=True)
        return {"error": f"Failed to update session effort: {type(exc).__name__}: {exc}"}


@mcp.tool()
async def session_set_model(session_id: str, model: str) -> dict:
    """Switch the model for a Genesis conversation session.

    Call this when the user asks to switch models, e.g. 'switch to opus',
    'use haiku', 'change to sonnet'. Valid models: sonnet, opus, haiku.
    Pass the Session ID from your system configuration.
    The change takes effect on the next response.
    """
    return await _impl_session_set_model(session_id, model)


@mcp.tool()
async def session_set_effort(session_id: str, effort: str) -> dict:
    """Switch the thinking effort for a Genesis conversation session.

    Call this when the user asks to change thinking effort, e.g. 'use high
    thinking', 'think harder', 'low effort', 'max effort'. Valid levels:
    low, medium, high, xhigh, max. Pass the Session ID from your system configuration.
    The change takes effect on the next response.
    """
    return await _impl_session_set_effort(session_id, effort)
