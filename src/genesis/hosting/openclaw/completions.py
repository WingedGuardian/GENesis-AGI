"""POST /v1/chat/completions — OpenClaw LLM provider endpoint.

OpenClaw treats Genesis as a custom LLM provider (api: "openai-completions").
It sends a standard OpenAI chat completions request; Genesis runs the message
through ConversationLoop (system prompt, session management, context injection)
and returns an SSE stream.

Protocol notes (confirmed from OpenClaw source/docs):
- ``stream: true`` is hardcoded by OpenClaw — SSE is mandatory
- Full conversation history is sent in ``messages`` each request (stateless
  on OpenClaw's side); Genesis maintains CC session continuity via
  ConversationLoop's DB-backed session management, keyed on
  ``x-openclaw-session-key`` header as ``user_id``
- OpenClaw sends ``max_completion_tokens`` (not ``max_tokens``) — handle both
- Ignore ``tools`` and ``store`` fields — Genesis handles tools via CC
- ``x-openclaw-message-channel`` carries the originating channel name
  (e.g. "whatsapp", "telegram") — logged for observability

Architecture:
  ConversationLoop (created by StandaloneAdapter at startup) runs in the main
  asyncio loop.  Flask threads submit coroutines via
  ``asyncio.run_coroutine_threadsafe()``.  The response is buffered (full CC
  output as a single SSE chunk).  True incremental streaming is deferred to
  Phase 5.1c.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid

from flask import Blueprint, Response, current_app, jsonify, request

from genesis.cc.types import ChannelType

logger = logging.getLogger("genesis.hosting.openclaw")

blueprint = Blueprint("openclaw_completions", __name__)

# Concurrency limiter — prevents unbounded CC subprocess spawning.
# Each request holds the semaphore for the duration of the CC invocation.
_MAX_CONCURRENT = 3
_semaphore = threading.Semaphore(_MAX_CONCURRENT)


@blueprint.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """Handle OpenClaw LLM provider requests.

    Extracts the latest user message, invokes CC via ConversationLoop (with
    full Genesis identity, session management, and context injection), and
    streams the response as OpenAI-format SSE.
    """
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.cc_invoker is None:
        return jsonify({"error": "Genesis not ready", "type": "error"}), 503

    conversation_loop = current_app.config.get("OPENCLAW_CONVERSATION_LOOP")
    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    if conversation_loop is None or event_loop is None:
        # Fallback: ConversationLoop not initialized (e.g., DB unavailable)
        return jsonify({"error": "ConversationLoop not available", "type": "error"}), 503

    data = request.get_json(force=True, silent=True) or {}
    messages = data.get("messages", [])

    # Extract the most recent user turn — OpenClaw sends full history each
    # request, but ConversationLoop already manages its own session history
    # via DB-backed cc_sessions.  We only forward the new message.
    user_message = _extract_last_user_message(messages)
    if not user_message:
        return jsonify({"error": "No user message found in messages array"}), 400

    session_key = request.headers.get("X-Openclaw-Session-Key") or "default"
    channel = request.headers.get("X-Openclaw-Message-Channel") or "openclaw"

    logger.info(
        "OpenClaw message: session=%s channel=%s msg_len=%d",
        session_key[:16], channel, len(user_message),
    )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    return Response(
        _stream_response(
            conversation_loop, event_loop,
            user_message, session_key, completion_id,
        ),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_response(conversation_loop, event_loop, user_message, session_key, completion_id):
    """Generator: invoke ConversationLoop, yield SSE chunks.

    Submits the async handle_message() call to the main asyncio loop via
    run_coroutine_threadsafe(), then blocks on the future result.  This is
    safe because Flask threads have no running event loop.
    """
    if not _semaphore.acquire(timeout=10):
        logger.warning("OpenClaw request rejected — concurrency limit reached")
        created = int(time.time())
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": "genesis",
            "choices": [{
                "index": 0,
                "delta": {"content": "Server is busy. Please try again in a moment."},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        final = {**error_chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"
        return

    try:
        future = asyncio.run_coroutine_threadsafe(
            conversation_loop.handle_message(
                user_message,
                user_id=session_key,
                channel=ChannelType.WEB,
            ),
            event_loop,
        )
        # Block until CC finishes (buffered response)
        response_text = future.result(timeout=300)
    except TimeoutError:
        logger.error("OpenClaw CC invocation timed out for session %s", session_key[:16], exc_info=True)
        response_text = None
    except Exception:
        logger.exception(
            "CC invocation failed for openclaw session %s", session_key[:16],
        )
        response_text = None
    finally:
        _semaphore.release()

    created = int(time.time())

    if response_text is None:
        error_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": "genesis",
            "choices": [{
                "index": 0,
                "delta": {"content": "I encountered an error processing your request."},
                "finish_reason": None,
            }],
        }
        yield f"data: {json.dumps(error_chunk)}\n\n"
        final = {**error_chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final)}\n\n"
        yield "data: [DONE]\n\n"
        return

    logger.info(
        "OpenClaw response: session=%s len=%d",
        session_key[:16], len(response_text),
    )

    # Single content chunk (buffered — incremental streaming is Phase 5.1c)
    content_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "genesis",
        "choices": [{"index": 0, "delta": {"content": response_text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(content_chunk)}\n\n"

    # Final chunk
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "genesis",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


def _extract_last_user_message(messages: list) -> str | None:
    """Extract the text of the most recent user message.

    Handles both string content and OpenAI multimodal content arrays
    (``[{"type": "text", "text": "..."}]``).  Returns None if no valid
    user message is found.
    """
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            # Multimodal: extract first text block
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    if text.strip():
                        return text
    return None
