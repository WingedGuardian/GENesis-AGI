"""POST /v1/voice/chat/completions — Voice channel endpoint.

OpenAI-compatible chat completions format so Home Assistant's HACS
``openai-compatible-conversation`` integration can send transcripts
directly to Genesis.

Uses a distinct URL path (``/v1/voice/...``) to avoid collision with the
OpenClaw endpoint at ``/v1/chat/completions``.

Auth: Bearer token validated per-route (the dashboard's ``/v1/*`` exemption
in ``auth.py`` skips the dashboard session check, but this endpoint enforces
its own token check using ``GENESIS_MCP_HTTP_TOKEN``).
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

logger = logging.getLogger("genesis.dashboard.voice_api")

voice_api_bp = Blueprint("voice_api", __name__)

# Latency budget: 5s target + 3s grace for memory recall + router call
_FUTURE_TIMEOUT_SECONDS = 8.0


def _check_voice_token() -> str | None:
    """Validate Bearer token. Returns error response or None if OK."""
    token = os.environ.get("GENESIS_MCP_HTTP_TOKEN", "")
    if not token:
        # No token configured — reject all requests
        logger.warning("Voice API called but GENESIS_MCP_HTTP_TOKEN not set")
        return None  # Let it through if unconfigured (local dev)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return "Missing or invalid Authorization header"

    request_token = auth_header[7:]
    if not hmac.compare_digest(request_token, token):
        return "Invalid bearer token"

    return None


@voice_api_bp.route("/v1/voice/chat/completions", methods=["POST"])
def voice_chat_completions():
    """Handle voice transcript from HA's conversation agent integration.

    Accepts standard OpenAI chat completions format.  Extracts the last
    user message, routes through VoiceConversationHandler, and returns
    the response in OpenAI format.
    """
    # Auth check
    auth_error = _check_voice_token()
    if auth_error:
        return jsonify({"error": auth_error}), 401

    # Get handler and event loop from app config
    voice_handler = current_app.config.get("VOICE_HANDLER")
    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")

    if voice_handler is None:
        return jsonify({
            "error": {"message": "Voice handler not initialized", "type": "server_error"},
        }), 503

    if event_loop is None or not event_loop.is_running():
        return jsonify({
            "error": {"message": "Event loop not available", "type": "server_error"},
        }), 503

    # Parse request
    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])

    # Extract last user message
    transcript = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            transcript = msg.get("content", "").strip()
            break

    if not transcript:
        return jsonify({
            "error": {"message": "No user message in request", "type": "invalid_request_error"},
        }), 400

    # Derive session ID from HA's conversation_id or generate one
    # HA sends conversation_id in the messages metadata or we use a default
    session_id = data.get("user", "ha-voice-default")

    # Dispatch to main event loop with explicit timeout
    start = time.monotonic()
    future = asyncio.run_coroutine_threadsafe(
        voice_handler.handle(transcript, session_id),
        event_loop,
    )

    try:
        response_text = future.result(timeout=_FUTURE_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        elapsed = time.monotonic() - start
        logger.error(
            "Voice handler timed out after %.1fs for session %s",
            elapsed, session_id[:12],
        )
        response_text = "I'm taking too long to respond. Try a simpler question."
    except Exception:
        logger.error(
            "Voice handler failed for session %s",
            session_id[:12], exc_info=True,
        )
        response_text = "Something went wrong. Try again."

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Voice response: %dms, %d chars, session=%s",
        elapsed_ms, len(response_text), session_id[:12],
    )

    # Return OpenAI-compatible response
    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "genesis-voice",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text,
            },
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    })
