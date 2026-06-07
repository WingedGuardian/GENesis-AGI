"""session_config tool — set model and/or effort for a conversation session."""

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
        logger.error("session_config model failed for %s: %s", session_id[:8], exc, exc_info=True)
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
        logger.error("session_config effort failed for %s: %s", session_id[:8], exc, exc_info=True)
        return {"error": f"Failed to update session effort: {type(exc).__name__}: {exc}"}


@mcp.tool()
async def session_config(
    session_id: str,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    """Set model and/or effort for a Genesis conversation session.

    Call when the user asks to switch models ('use opus', 'switch to haiku')
    or effort ('think harder', 'low effort', 'max effort'). Both parameters
    are optional — pass only what you want to change.

    Valid models: sonnet, opus, haiku.
    Valid efforts: low, medium, high, xhigh, max.
    Pass the Session ID from your system configuration.
    Changes take effect on the next response.
    """
    import genesis.mcp.health_mcp as health_mcp_mod

    if model is None and effort is None:
        return {"error": "Provide at least one of 'model' or 'effort' to change."}
    if not session_id or not session_id.strip():
        return {"error": "Session ID is required"}

    # Pre-validate both params before any DB writes to prevent partial application
    clean_model: str | None = None
    clean_effort: str | None = None
    if model is not None:
        clean_model = model.lower().strip()
        if clean_model not in _VALID_MODELS:
            return {"error": f"Invalid model '{clean_model}'. Valid: {', '.join(sorted(_VALID_MODELS))}"}
    if effort is not None:
        clean_effort = effort.lower().strip()
        if clean_effort not in _VALID_EFFORTS:
            return {"error": f"Invalid effort '{clean_effort}'. Valid: {', '.join(sorted(_VALID_EFFORTS))}"}

    _service = health_mcp_mod._service
    if _service is None or _service._db is None:
        return {"error": "Database not available"}

    # Single DB call with both params — update_model_effort handles None params
    try:
        from genesis.db.crud import cc_sessions

        updated = await cc_sessions.update_model_effort(
            _service._db, session_id, model=clean_model, effort=clean_effort,
        )
        if not updated:
            return {"error": f"Session '{session_id}' not found"}
    except Exception as exc:
        logger.error("session_config failed for %s: %s", session_id[:8], exc, exc_info=True)
        return {"error": f"Failed to update session: {type(exc).__name__}: {exc}"}

    # Single cache persist after DB succeeds
    _persist_session_config(model=clean_model, effort=clean_effort)

    result: dict = {"success": True, "note": "Takes effect on your next response."}
    if clean_model is not None:
        result["model"] = clean_model
    if clean_effort is not None:
        result["effort"] = clean_effort
    return result
