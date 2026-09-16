"""Agent-to-agent connector — ``/v1/agent/*``.

Lets an external personal-agent front-end (any vendor) ask this Genesis
install a question in natural language and get an answer back, over the
tailnet. Genesis stays the brain; the caller stays the body.

A CONVERSATION endpoint, not a tool proxy
-----------------------------------------
The caller asks in prose. The Genesis session that answers already holds the
full MCP catalogue and every existing gate (outreach governance, the approval
gate, autonomy policy). So there is no new allowlist to maintain and no second
place where tool authority is decided.

Exposure
--------
Published through a path-scoped ``tailscale serve`` listener (``/v1/agent`` ->
``127.0.0.1:5000/v1/agent``), never by opening port 5000. That distinction is
load-bearing: port 5000 also carries the OpenClaw route
``/v1/chat/completions``, which has NO route-level auth because ``auth.py``
exempts ``/v1/*`` from the dashboard session check. Publishing the port would
publish that route too.

Auth
----
Bearer token in ``GENESIS_AGENT_API_TOKEN``, fail-closed, via the shared
``_bearer`` helper. Deliberately NOT the voice API's ``GENESIS_MCP_HTTP_TOKEN``
— different trust boundary (an external agent versus the owner's own Home
Assistant), and that token additionally gates a write surface, so one rotation
must not silently re-authorise the other.

The caller is UNTRUSTED, and that is enforced, not assumed
----------------------------------------------------------
* ``ChannelType.AGENT`` carries the request, so every channel-derived trust
  predicate treats it as a gateway rather than the owner.
* ``intent_text=""`` is passed explicitly, so the caller's prose is NEVER
  scanned for slash intents. Without it, ``/model`` and ``/effort`` appearing
  anywhere in a question would be honoured AND persisted to the session row —
  handing an external party a lever over cost and quality that belongs to the
  user. The repo already reasoned this through for the WEB channel when it
  withheld the session-control block; this closes the other road to the same
  lever.
* The message is length-capped before it reaches cognition.

Caller attribution
------------------
``tailscale serve`` proxies to 127.0.0.1, so the socket peer is ALWAYS
localhost and ``request.remote_addr`` can never identify the caller. Identity
is taken from ``Tailscale-User-Login`` in preference to ``X-Forwarded-For``:
serve ``Del()``s the ``Tailscale-User-*`` headers before setting them, so an
incoming value cannot squat there, and it is stable across a device changing
address. Either way this is for logging and origin stamping only — the bearer
token is the authentication, because anything able to reach port 5000 directly
could forge a header.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from genesis.cc.types import ChannelType
from genesis.dashboard.routes._bearer import require_bearer
from genesis.hosting.openai_messages import extract_last_user_message

logger = logging.getLogger("genesis.dashboard.agent_api")

agent_api_bp = Blueprint("agent_api", __name__)

_AUTH_ENV = "GENESIS_AGENT_API_TOKEN"

# Concurrency limiter — each request may spawn a CC invocation, so an
# unbounded caller could otherwise fan out subprocesses without limit.
_MAX_CONCURRENT = 2
_semaphore = threading.Semaphore(_MAX_CONCURRENT)

# Synchronous answer budget. Mirrors the OpenClaw endpoint's 300s: past that
# the caller's own HTTP client has almost certainly given up, so holding the
# request open buys nothing. Genuinely long work belongs on a dispatch/poll
# pair rather than a held connection.
_ANSWER_TIMEOUT_SECONDS = 300.0

# Past this a payload is not a question. Roughly 8k tokens; the point is to
# stop a multi-megabyte body being buffered whole, forwarded whole into a CC
# prompt, and stored.
_MAX_MESSAGE_CHARS = 32_000

# Attribution is a log field ONLY, so it is bounded purely to keep a log line
# sane. It deliberately does NOT reach the session key -- see _conversation_id.
_MAX_CALLER_CHARS = 64

# Hard byte cap applied to the request body BEFORE any parsing. The app-wide
# MAX_CONTENT_LENGTH is 500 MB (sized for knowledge uploads), so without this
# an authenticated caller could have multi-megabyte bodies buffered and parsed
# concurrently -- the _MAX_MESSAGE_CHARS check runs far too late to prevent it,
# and the semaphore is not held yet either. Derivation: a 32,000-character
# message is at most ~128 KB in UTF-8, and callers legitimately send prior turns
# alongside it, so 1 MiB leaves generous room for history while keeping the
# worst case bounded.
_MAX_BODY_BYTES = 1024 * 1024

# Conversation key. The caller NAMES its conversation; nothing is inferred from
# an attribution header, because that would make an unverified value decide
# which stored session a request resumes.
_CONVERSATION_HEADER = "X-Genesis-Conversation-Id"
_DEFAULT_CONVERSATION = "default"
_CONVERSATION_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
_MAX_CONVERSATION_CHARS = 64


def _caller() -> str:
    """Best-effort caller identity for logs and session keying.

    Prefers the tailnet user over the forwarded address: serve clears the
    ``Tailscale-User-*`` headers before setting them, and the identity is
    stable when a device's address changes. Falls back to
    ``X-Forwarded-For``, then to the socket peer (which behind serve is
    always localhost and therefore tells you nothing).
    """
    who = request.headers.get("Tailscale-User-Login", "").strip()
    if not who:
        fwd = request.headers.get("X-Forwarded-For", "")
        who = fwd.split(",")[0].strip() if fwd else ""
    if not who:
        who = request.remote_addr or "unknown"
    # Newlines would otherwise inject a fabricated line into the log.
    return who.replace("\r", " ").replace("\n", " ")[:_MAX_CALLER_CHARS]


def _conversation_id() -> str:
    """Which stored conversation this request continues.

    Taken from an explicit request header, NEVER from the attribution headers.
    Deriving it from ``X-Forwarded-For`` or ``Tailscale-User-Login`` would make
    an unverified value select which persisted session is resumed: two
    unrelated chats from one agent would silently share context, and anything
    able to reach the port directly could pick a session by forging a header.
    The bearer token authenticates ONE principal, so the principal is constant
    and the caller simply names the thread it wants.
    """
    raw = request.headers.get(_CONVERSATION_HEADER, "").strip()
    if not raw:
        return _DEFAULT_CONVERSATION
    safe = _CONVERSATION_ID_RE.sub("-", raw)[:_MAX_CONVERSATION_CHARS]
    return safe or _DEFAULT_CONVERSATION


@agent_api_bp.route("/v1/agent/ping", methods=["GET"])
def agent_ping():
    """Liveness plus a reachability proof for the calling agent.

    Behind the same auth as everything else here. Note this does not make the
    surface invisible to an unauthenticated prober — 503 and 401 are still
    distinguishable, and reveal whether the connector is configured — it only
    withholds the install's identity.
    """
    auth = require_bearer(_AUTH_ENV, "agent connector")
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

    caller = _caller()
    logger.info("agent connector ping from %s", caller)
    return jsonify({
        "status": "ok",
        "service": "genesis-agent-connector",
        "caller": caller,
    })


@agent_api_bp.route("/v1/agent/chat/completions", methods=["POST"])
def agent_chat_completions():
    """Answer one natural-language question from an external agent.

    OpenAI chat-completions shape in and out, so the caller can use an
    off-the-shelf client. Non-streaming: ``stream: true`` is REFUSED rather
    than ignored, because a client that asked for SSE and receives JSON waits
    for frames that never arrive and times out with no diagnostic.
    """
    auth = require_bearer(_AUTH_ENV, "agent connector")
    if auth is not None:
        msg, status = auth
        return jsonify({"error": msg}), status

    caller = _caller()

    # Bound the body BEFORE parsing. Reading one byte past the cap is what
    # distinguishes "at the limit" from "over it" without buffering the rest,
    # and it works for a chunked request too, where Content-Length is absent
    # and so cannot be trusted as the check.
    declared = request.content_length
    if declared is not None and declared > _MAX_BODY_BYTES:
        return jsonify({"error": f"request body too large (max {_MAX_BODY_BYTES} bytes)"}), 413
    raw = request.stream.read(_MAX_BODY_BYTES + 1)
    if len(raw) > _MAX_BODY_BYTES:
        return jsonify({"error": f"request body too large (max {_MAX_BODY_BYTES} bytes)"}), 413

    try:
        payload = json.loads(raw) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return jsonify({"error": "request body is not valid JSON"}), 400

    # A bare list, string or number is valid JSON and has no .get, so without
    # this an authenticated caller turns a client error into a 500 traceback.
    if not isinstance(payload, dict):
        return jsonify({"error": "request body must be a JSON object"}), 400

    if payload.get("stream"):
        return jsonify({
            "error": "streaming is not supported on /v1/agent/chat/completions; "
                     "send stream=false (the answer is returned whole)",
        }), 400

    user_message = extract_last_user_message(payload.get("messages"))
    if not user_message or not user_message.strip():
        return jsonify({"error": "no user message in request"}), 400

    if len(user_message) > _MAX_MESSAGE_CHARS:
        return jsonify({
            "error": f"message too long ({len(user_message)} chars, "
                     f"max {_MAX_MESSAGE_CHARS})",
        }), 413

    conversation_loop = current_app.config.get("GENESIS_CONVERSATION_LOOP")
    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    if conversation_loop is None or event_loop is None:
        logger.warning("agent connector hit before runtime is ready (caller %s)", caller)
        return jsonify({"error": "runtime starting"}), 503

    if not _semaphore.acquire(blocking=False):
        logger.warning("agent connector at capacity, refusing request from %s", caller)
        return jsonify({"error": "server busy, retry shortly"}), 429

    logger.info(
        "agent connector question from %s (%d chars)", caller, len(user_message),
    )
    try:
        future = asyncio.run_coroutine_threadsafe(
            conversation_loop.handle_message(
                user_message,
                user_id=f"agent:{_conversation_id()}",
                channel=ChannelType.AGENT,
                # Never scan an external caller's prose for slash intents.
                intent_text="",
            ),
            event_loop,
        )
        answer = future.result(timeout=_ANSWER_TIMEOUT_SECONDS)
    except TimeoutError:
        # The WAIT ended; the coroutine has not. Cancel it, or the CC
        # invocation keeps running while the finally below frees its slot —
        # which is how a patient caller defeats the cap. Best-effort: once the
        # coroutine has started, cancellation depends on it reaching a suspend
        # point, so this reduces the leak rather than eliminating it.
        future.cancel()
        logger.error(
            "agent connector invocation timed out for caller %s", caller, exc_info=True,
        )
        return jsonify({"error": "timed out producing an answer"}), 504
    except Exception:
        logger.exception("agent connector invocation failed for caller %s", caller)
        return jsonify({"error": "failed to produce an answer"}), 500
    finally:
        _semaphore.release()

    return jsonify({
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "genesis",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer or ""},
            "finish_reason": "stop",
        }],
    })
