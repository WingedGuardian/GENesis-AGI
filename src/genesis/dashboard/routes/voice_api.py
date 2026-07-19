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
from datetime import UTC, datetime

from flask import Blueprint, current_app, jsonify, request

from genesis.channels.voice.graduation import validate_envelope
from genesis.channels.voice.transcript_writer import validate_conversation

logger = logging.getLogger("genesis.dashboard.voice_api")

voice_api_bp = Blueprint("voice_api", __name__)

# Latency budget: 5s target + 3s grace for memory recall + router call
_FUTURE_TIMEOUT_SECONDS = 8.0


def _check_voice_token() -> tuple[str, int] | None:
    """Validate Bearer token. Returns (error message, http status) or None if OK.

    Fail-closed: when ``GENESIS_MCP_HTTP_TOKEN`` is not set, every
    ``/v1/voice/*`` route answers 503 — a write surface (``/v1/voice/graduate``)
    shares this token model, so open-by-default is not acceptable even on a
    trusted network. Set the token in ``secrets.env`` to enable the voice API
    (the standalone host logs a boot-time warning when it is missing).
    """
    token = os.environ.get("GENESIS_MCP_HTTP_TOKEN", "")
    if not token:
        return ("voice API disabled: GENESIS_MCP_HTTP_TOKEN not configured", 503)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return ("Missing or invalid Authorization header", 401)

    request_token = auth_header[7:]
    if not hmac.compare_digest(request_token, token):
        return ("Invalid bearer token", 401)

    return None


@voice_api_bp.route("/v1/voice/chat/completions", methods=["POST"])
def voice_chat_completions():
    """Handle voice transcript from HA's conversation agent integration.

    Accepts standard OpenAI chat completions format.  Extracts the last
    user message, routes through VoiceConversationHandler, and returns
    the response in OpenAI format.
    """
    # Auth check
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

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
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

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
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

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
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

    from genesis.channels.voice.genesis_bridge import TOOL_DECLARATIONS
    return jsonify({"tools": TOOL_DECLARATIONS})


# ── Graduation landing (W0 — DARK: quarantine insert only, no consumer) ──
# The voice edge pushes typed graduation events (synthesized claims, never raw
# transcripts) here; they land verbatim in the graduation_events quarantine
# table with disposition='pending'. The W2 policy drainer (separate PR) is the
# only consumer. Bearer auth is REQUIRED — _check_voice_token is fail-closed,
# so an unset token disables this write surface entirely.

# 10s: a single INSERT. The realistic hang is SQLite write-lock contention,
# bounded by busy_timeout=5000 (db/connection.py); 10s = that + runtime-loop
# scheduling headroom. Fast-fail is safe: the edge outbox retries on 503, and
# a timed-out-but-committed insert resolves as 'duplicate' on retry (event_id
# dedup) — effectively-once either way.
_GRADUATE_TIMEOUT_SECONDS = 10.0

# Envelope sanity cap — graduation events are small JSON (claims + metadata);
# app-level MAX_CONTENT_LENGTH is sized for uploads, not this route.
_MAX_GRADUATE_BYTES = 64 * 1024


async def _land_graduation_event(data: dict) -> bool:
    """Insert the validated envelope into quarantine (runs on the runtime loop).

    Returns True if inserted, False on an event_id replay. Raises
    LookupError when the runtime/DB isn't ready — the route maps it to 503
    so the edge outbox retries.
    """
    from genesis.db.crud import graduation_events
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        raise LookupError("Genesis runtime not ready")

    return await graduation_events.insert_event(
        rt.db,
        event_id=data["event_id"],
        schema_version=data["schema_version"],
        type=data["type"],
        source=data["source"],
        occurred_at=data["occurred_at"],
        received_at=datetime.now(UTC).isoformat(),
        payload=data["payload"],
        provenance=data["provenance"],
    )


@voice_api_bp.route("/v1/voice/graduate", methods=["POST"])
def voice_graduate():
    """Land a graduation event from the voice edge (W0 quarantine landing).

    Responses (spec §4.9): 200 ``{"status": "accepted"}`` only after the
    INSERT commits; 200 ``{"status": "duplicate"}`` on an event_id replay
    (edge treats both as delivered); 400 ``{"status": "rejected", "errors":
    [...]}`` on envelope validation failure. One-way boundary — no core data
    ever returns.

    Auth + validation run synchronously (same pattern as the sibling voice
    routes) so fail-closed answers correctly even while the runtime loop is
    still starting; only the DB insert dispatches to the runtime loop.
    """
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

    # Measure the actual body, not the Content-Length header — a chunked
    # request has no header and would bypass a content_length check; get_json
    # below reuses this cached read.
    if len(request.get_data(cache=True)) > _MAX_GRADUATE_BYTES:
        return jsonify(
            {"status": "rejected", "errors": ["envelope exceeds 64KB"]}
        ), 400

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify(
            {"status": "rejected", "errors": ["body must be a JSON object"]}
        ), 400
    errors = validate_envelope(data)
    if errors:
        return jsonify({"status": "rejected", "errors": errors}), 400

    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    if event_loop is None or not event_loop.is_running():
        return jsonify({"error": "Event loop not available"}), 503

    future = asyncio.run_coroutine_threadsafe(
        _land_graduation_event(data), event_loop
    )
    try:
        inserted = future.result(timeout=_GRADUATE_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        logger.error(
            "Voice graduate timed out after %.0fs for event %s",
            _GRADUATE_TIMEOUT_SECONDS, data["event_id"][:16],
        )
        return jsonify({"error": "graduate landing timed out"}), 503
    except LookupError:
        return jsonify({"error": "Genesis runtime not ready"}), 503
    except Exception:
        logger.error(
            "Voice graduate failed for event %s", data["event_id"][:16],
            exc_info=True,
        )
        return jsonify({"error": "graduate landing failed"}), 500

    status = "accepted" if inserted else "duplicate"
    logger.info(
        "Voice graduate: %s event %s from %s",
        status, data["event_id"][:16], data["source"][:32],
    )
    return jsonify({"status": status})


# ── Conversation transcript landing (W0.5 — s2s extraction parity) ──
# The edge s2s bridge posts its FULL cumulative turn list here on client
# disconnect (and may re-post on replays/double-fires). The transcript writer
# appends only new turns to a per-session CC-format JSONL that the memory
# extraction job mines like any other channel — replacing the legacy
# "one-blob memory_store" landing entirely.

# 10s: a bounded file append + at most two SQLite writes — the same bound
# class as /v1/voice/graduate (busy_timeout=5000 + loop scheduling headroom).
# Fast-fail is safe: the edge re-posts its cumulative list, and the
# line-count reconciliation makes any replay idempotent.
_CONVERSATION_TIMEOUT_SECONDS = 10.0

# Cumulative turn lists are bigger than graduation envelopes (largest blob
# observed in the wild: ~25KB after weeks of accumulation) — 256KB is ~10x
# headroom while still refusing pathological bodies.
_MAX_CONVERSATION_BYTES = 256 * 1024


async def _land_conversation(data: dict) -> int:
    """Reconcile the cumulative turn list (runs on the runtime loop).

    Returns the number of newly appended messages. Raises LookupError when
    the runtime/DB isn't ready — mapped to 503 so the edge retries.
    """
    from genesis.channels.voice.transcript_writer import get_shared_writer
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        raise LookupError("Genesis runtime not ready")

    writer = await get_shared_writer(rt.db)
    return await writer.sync_cumulative(data["session_id"], data["turns"])


@voice_api_bp.route("/v1/voice/conversation", methods=["POST"])
def voice_conversation():
    """Land a voice conversation's cumulative turn list from the edge bridge.

    Body: ``{"session_id": str, "satellite_id": str?, "turns": [{"role":
    "user"|"assistant", "text": str}, ...]}`` where ``turns`` is the full
    cumulative list for the session. Contract: the producer must regenerate
    ``session_id`` whenever its turn cache resets (a shorter-than-known list
    appends nothing and logs a warning server-side).

    Responses: 200 ``{"status": "ok", "appended": N}`` after the transcript
    append is durable; 400 ``{"status": "rejected", "errors": [...]}`` on
    validation failure; 503 while the runtime is not ready (edge retries).
    """
    auth = _check_voice_token()
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

    # Measure the actual body, not the Content-Length header (chunked-safe);
    # get_json below reuses this cached read.
    if len(request.get_data(cache=True)) > _MAX_CONVERSATION_BYTES:
        return jsonify({"status": "rejected", "errors": ["body exceeds 256KB"]}), 400

    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict):
        return jsonify({"status": "rejected", "errors": ["body must be a JSON object"]}), 400
    errors = validate_conversation(data)
    if errors:
        return jsonify({"status": "rejected", "errors": errors}), 400

    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    if event_loop is None or not event_loop.is_running():
        return jsonify({"error": "Event loop not available"}), 503

    future = asyncio.run_coroutine_threadsafe(_land_conversation(data), event_loop)
    try:
        appended = future.result(timeout=_CONVERSATION_TIMEOUT_SECONDS)
    except TimeoutError:
        future.cancel()
        logger.error(
            "Voice conversation landing timed out after %.0fs for session %s",
            _CONVERSATION_TIMEOUT_SECONDS,
            data["session_id"][:32],
        )
        return jsonify({"error": "conversation landing timed out"}), 503
    except LookupError:
        return jsonify({"error": "Genesis runtime not ready"}), 503
    except Exception:
        logger.error(
            "Voice conversation landing failed for session %s",
            data["session_id"][:32],
            exc_info=True,
        )
        return jsonify({"error": "conversation landing failed"}), 500

    logger.info(
        "Voice conversation: %d message(s) appended for session %s",
        appended,
        data["session_id"][:32],
    )
    return jsonify({"status": "ok", "appended": appended})
