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
import json
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
    """Validate Bearer token. Returns error message or None if OK.

    When ``GENESIS_MCP_HTTP_TOKEN`` is not set, requests are allowed
    through without auth (Tailscale-only network, local dev).  Set
    the token in ``secrets.env`` to enforce auth in production.
    """
    token = os.environ.get("GENESIS_MCP_HTTP_TOKEN", "")
    if not token:
        # No token configured — allow through (Tailscale-only network)
        return None

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

    # S2S short-circuit: if the S2S model already handled this request
    # (audio response queued on Wyoming TTS server), return the transcript
    # as-is without making another LLM call.  HA's pipeline still requires
    # a conversation agent response — we give it one, but cheaply.
    from genesis.channels.voice import config as voice_config
    if voice_config.s2s_enabled():
        data = request.get_json(force=True, silent=True) or {}
        messages = data.get("messages", [])
        transcript = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                transcript = msg.get("content", "").strip()
                break
        # In S2S mode, the STT handler already processed the audio through
        # GPT-Realtime and queued the response audio.  Return the transcript
        # text — HA will send it to Wyoming TTS, which will serve the S2S
        # audio instead of synthesizing from this text.
        logger.info("S2S mode: passing through transcript (%d chars)", len(transcript))
        return jsonify({
            "id": "chatcmpl-s2s-passthrough",
            "object": "chat.completion",
            "model": "genesis-voice-s2s",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": transcript or "..."},
                "finish_reason": "stop",
            }],
        })

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

    # Cap transcript length — voice transcripts are typically 1-100 words
    _MAX_TRANSCRIPT_CHARS = 2000
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = transcript[:_MAX_TRANSCRIPT_CHARS]

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


# ── S2S Bridge Tool Dispatch ────────────────────────────────────────
# Called by the voice S2S addon (HA Docker container) to dispatch
# Genesis tool calls triggered by the OpenAI Realtime model.
# Same auth + event loop bridge pattern as voice_chat_completions.

# Tool call timeout: 30s covers memory recall + web search + approval.
# Voice responses should be fast — if a tool call takes >30s, it's hung.
_TOOL_CALL_TIMEOUT_SECONDS = 30.0


@voice_api_bp.route("/v1/voice/tool_call", methods=["POST"])
def voice_tool_call():
    """Dispatch a tool call from the S2S voice bridge addon.

    Expects JSON: ``{"tool_name": "ask_genesis", "arguments": {"query": "..."}}``.
    Returns the tool result as JSON.
    """
    auth_error = _check_voice_token()
    if auth_error:
        return jsonify({"error": auth_error}), 401

    bridge = current_app.config.get("GENESIS_BRIDGE")
    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")

    if bridge is None:
        return jsonify({"error": "Genesis bridge not initialized"}), 503

    if event_loop is None or not event_loop.is_running():
        return jsonify({"error": "Event loop not available"}), 503

    data = request.get_json(force=True, silent=True) or {}
    tool_name = data.get("tool_name", "").strip()
    arguments = data.get("arguments", {})

    if not tool_name:
        return jsonify({"error": "tool_name is required"}), 400

    start = time.monotonic()
    future = asyncio.run_coroutine_threadsafe(
        bridge.handle_tool_call(tool_name, json.dumps(arguments)),
        event_loop,
    )

    try:
        result_json = future.result(timeout=_TOOL_CALL_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        elapsed = time.monotonic() - start
        logger.error("Tool call %s timed out after %.1fs", tool_name, elapsed)
        return jsonify({"error": f"Tool call timed out after {elapsed:.0f}s"}), 504
    except Exception:
        logger.error("Tool call %s failed", tool_name, exc_info=True)
        return jsonify({"error": "Tool call failed"}), 500

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info("Voice tool_call: %s → %dms", tool_name, elapsed_ms)

    # bridge.handle_tool_call returns a JSON string — parse and return
    try:
        return jsonify(json.loads(result_json))
    except (json.JSONDecodeError, TypeError):
        return jsonify({"result": result_json})


@voice_api_bp.route("/v1/voice/system_prompt", methods=["GET"])
def voice_system_prompt():
    """Return the Genesis voice system prompt for the S2S model.

    Called by the voice addon at session start to configure the
    OpenAI Realtime model with Genesis persona + context.
    """
    auth_error = _check_voice_token()
    if auth_error:
        return jsonify({"error": auth_error}), 401

    bridge = current_app.config.get("GENESIS_BRIDGE")
    if bridge is None:
        return jsonify({"error": "Genesis bridge not initialized"}), 503

    try:
        return jsonify({"prompt": bridge.get_system_prompt()})
    except Exception:
        logger.error("Failed to generate system prompt", exc_info=True)
        return jsonify({"error": "Failed to generate system prompt"}), 500


@voice_api_bp.route("/v1/voice/tool_declarations", methods=["GET"])
def voice_tool_declarations():
    """Return the tool declarations for the S2S model session config.

    Called by the voice addon at session start to register Genesis
    tools with the OpenAI Realtime API.
    """
    auth_error = _check_voice_token()
    if auth_error:
        return jsonify({"error": auth_error}), 401

    from genesis.channels.voice.genesis_bridge import TOOL_DECLARATIONS
    return jsonify({"tools": TOOL_DECLARATIONS})
