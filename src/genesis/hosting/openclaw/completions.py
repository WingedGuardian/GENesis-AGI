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

Auth: ``Authorization: Bearer <GENESIS_MCP_HTTP_TOKEN>``, enforced per-route via
the shared ``/v1/*`` check. The dashboard's session gate exempts the whole
``/v1/*`` prefix (machine callers have no browser session), so without this the
route would authenticate nobody — and it reaches CC invocation. Configure the
same token on the OpenClaw side as the provider's API key; it is a standard
OpenAI-compatible client, so the header needs no custom code there.

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
from concurrent.futures import wait as futures_wait

from flask import Blueprint, Response, current_app, jsonify, request

from genesis.cc.types import ChannelType
from genesis.dashboard.auth import check_bearer_token
from genesis.hosting.openai_messages import extract_last_user_message

logger = logging.getLogger("genesis.hosting.openclaw")

blueprint = Blueprint("openclaw_completions", __name__)

# Concurrency limiter — prevents unbounded CC subprocess spawning.
# Each request holds the semaphore for the duration of the CC invocation.
_MAX_CONCURRENT = 3
# How long a caller may wait for a result, and how often the wait re-checks
# that the loop which must produce it is still alive. The poll interval is
# what bounds slot occupancy after a shutdown, not the timeout.
_RESULT_TIMEOUT_S = 300
_LIVENESS_POLL_S = 5.0
_semaphore = threading.Semaphore(_MAX_CONCURRENT)


@blueprint.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """Handle OpenClaw LLM provider requests.

    Extracts the latest user message, invokes CC via ConversationLoop (with
    full Genesis identity, session management, and context injection), and
    streams the response as OpenAI-format SSE.
    """
    from genesis.runtime import GenesisRuntime

    # Auth BEFORE any work: this route spawns CC subprocesses, so an
    # unauthenticated caller must not reach the readiness probe, the body
    # parse, or the semaphore.
    denied = check_bearer_token("OpenClaw completions")
    if denied:
        message, status = denied
        return jsonify({"error": message, "type": "error"}), status

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.cc_invoker is None:
        return jsonify({"error": "Genesis not ready", "type": "error"}), 503

    conversation_loop = current_app.config.get("OPENCLAW_CONVERSATION_LOOP")
    if conversation_loop is None:
        # Fallback: ConversationLoop not initialized (e.g., DB unavailable)
        return jsonify({"error": "ConversationLoop not available", "type": "error"}), 503

    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    # READINESS, not existence -- and the two bad states fail differently, so
    # neither is the "500" it is tempting to assume. MEASURED through the real
    # route rather than reasoned about:
    #   stopped, not closed -> run_coroutine_threadsafe returns a PENDING
    #     future, so `future.result(timeout=300)` below blocks for the FULL
    #     five minutes while holding one of only _MAX_CONCURRENT (3) slots.
    #     Three such requests starve the endpoint for everyone else.
    #   closed              -> run_coroutine_threadsafe raises RuntimeError,
    #     but inside _stream_response, whose own `except Exception` runs after
    #     Flask has already committed 200. The caller gets 200 and a generic
    #     apology, never an error status.
    # Both also leave the just-created coroutine un-awaited. is_running() is
    # False for BOTH states, which is why it is the check.
    #
    # No getattr fallback: defaulting a missing is_running to True is a
    # fail-OPEN on the one attribute this gate exists to read. Every asyncio
    # loop has it, and so does a MagicMock, so the default could only ever
    # fire for an object that is not a loop -- exactly the case that must not
    # be waved through.
    if event_loop is None or not event_loop.is_running():
        return jsonify({"error": "event loop not running", "type": "error"}), 503

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


def _result_while_loop_lives(future, event_loop, timeout=_RESULT_TIMEOUT_S, poll=_LIVENESS_POLL_S):
    """``future.result(timeout)``, abandoned early if the loop that must run it stops.

    A plain ``future.result(timeout=300)`` waits the FULL timeout even once the
    loop has stopped and the coroutine can therefore never run -- holding one of
    only ``_MAX_CONCURRENT`` slots for five minutes. Three of those starve the
    endpoint for everyone while nothing is being computed.

    The submission-time check above cannot close this on its own: the loop can
    stop at any point AFTER a successful submission, which no pre-flight can
    see. So the wait is split into short polls and gives up as soon as the loop
    is gone, which is what actually frees the slot.

    Raising RuntimeError rather than returning None deliberately: the caller's
    ``except Exception`` already maps that to the same client-visible answer,
    and keeping the two unready states one kind of failure means a future
    reader cannot handle one and forget the other.
    """
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no result within {timeout}s")

        # futures.wait, NOT future.result(timeout=...). MEASURED on 3.12:
        # concurrent.futures.TimeoutError, asyncio.TimeoutError and builtins
        # TimeoutError are the SAME object, so result() cannot distinguish "the
        # slice expired" from "the coroutine itself raised TimeoutError". An
        # earlier version of this used result() and caught TimeoutError: when
        # the coroutine raised one while the loop was still alive, the liveness
        # branch was skipped, the loop went round, and the now-FINISHED future
        # re-raised instantly -- 29,689 spins in two seconds, a pegged core and
        # a held slot for the whole timeout. Exactly the starvation this
        # function exists to prevent. wait() reports readiness and never
        # re-raises, so the two cases stay separable.
        done, _ = futures_wait([future], timeout=min(poll, remaining))
        if done:
            return future.result()

        if not event_loop.is_running():
            if not future.cancel():
                # cancel() fails only when the future already FINISHED, which
                # here means the result landed between the wait expiring and
                # this call. Discarding it would report an error while the real
                # answer sat in hand.
                return future.result()
            raise RuntimeError("event loop stopped while the request was in flight")


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
        # Re-check readiness HERE, at submission, not only in the view. Flask
        # runs this generator AFTER the view returns, so the preflight check
        # upstream is arbitrarily stale by now and shutdown can land in the
        # gap. Submitting against a stopped loop is the expensive failure:
        # run_coroutine_threadsafe accepts it, returns a PENDING future, and
        # the wait below holds one of _MAX_CONCURRENT slots until it expires.
        if not event_loop.is_running():
            raise RuntimeError("event loop stopped before submission")
        future = asyncio.run_coroutine_threadsafe(
            conversation_loop.handle_message(
                user_message,
                user_id=session_key,
                channel=ChannelType.WEB,
            ),
            event_loop,
        )
        # Block until CC finishes (buffered response)
        response_text = _result_while_loop_lives(future, event_loop)
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


# Moved to genesis.hosting.openai_messages so that any future surface accepting
# the OpenAI request shape reads it through the same parser rather than copying
# this one. Re-exported under the original private name so existing callers and
# tests keep working.
_extract_last_user_message = extract_last_user_message
