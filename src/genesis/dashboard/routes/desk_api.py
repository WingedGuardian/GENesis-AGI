"""POST /v1/desk/chat/completions — the desktop assistant's brain.

A desktop assistant (a separate, third-party product running on the operator's
own machine) speaks plain OpenAI chat-completions to whatever ``base_url`` it is
configured with. Pointing that at this endpoint makes Genesis the brain behind
it: every turn routes through ``ModelRouter``, so provider choice, cost tracking,
circuit breakers and observability are Genesis's, and the desktop side holds no
model credential of its own.

Distinct from its two neighbours on purpose:

- ``/v1/chat/completions`` (OpenClaw) runs a full ``ConversationLoop`` — a Claude
  Code subprocess per request. Correct for a chat channel, far too heavy for a
  per-utterance desk brain.
- ``/v1/voice/*`` serves one specific edge client with its own tool protocol.

TWO LANES, because the caller's turns are not all the same shape. Desk turns must
follow long instructions exactly (the desktop side triggers its own tools by
emitting an exact control tag, and a near-miss silently fires nothing), while
phone turns are composed mid-call where latency is felt directly. The lane comes
from ``X-Genesis-Lane``, or from a ``-fast``/``-primary`` suffix on ``model`` when a
proxy strips unknown headers — a header alone fails silently and invisibly.

Deliberately NOT a recall path. The caller builds its own system prompt from its
own vault, and Genesis memory reaches it through the explicit ask-Genesis tool,
not by injection here — two context builders fighting over one prompt is how
both end up wrong. That is also why ``suppress_dead_letter`` is set: a dead-letter
row would persist the caller's vault-derived prompt in ``genesis.db`` for 72h and
re-dispatch it against a paid chain, for a reply nobody will ever read.

KNOWN CONTRACT LIMITS, stated so they are a contract rather than a surprise:
streaming is refused outright; ``role: "tool"`` and function/tool-call turns are
refused (the router has no tool protocol); and IMAGE content is refused, because
Genesis's routing layer carries no multimodal support at all — answering a
"what am I looking at" turn from the text alone would be a confident answer about
something never seen, which is worse than an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid

from flask import Blueprint, current_app, jsonify, request

from genesis.dashboard.auth import check_bearer_token

logger = logging.getLogger("genesis.dashboard.desk_api")

desk_api_bp = Blueprint("desk_api", __name__)

# Lane -> routing call site. Both are configured in config/model_routing.yaml.
_LANES = {
    "primary": "desk_primary",
    "fast": "desk_fast",
}
_DEFAULT_LANE = "primary"

# The caller's own client gives up at 120s. Failing INSIDE that window is what
# lets it receive a parseable error it can speak, instead of a raw socket
# timeout that reads to the operator as a dead hang.
_ROUTER_TIMEOUT_SECONDS = 110.0

# The delegate's per-attempt default is 120s — LONGER than this endpoint's whole
# budget, so one slow provider would consume the wall and the 2nd and 3rd links
# of the chain would never be tried. Fallback resilience is the entire reason
# for routing through ModelRouter rather than calling a provider directly.
#
# "3 x 30s < 110s" is the arithmetic for three ATTEMPTS, and a retry profile
# multiplies attempts per provider — so the bound has to hold for the retried
# case, not the happy one.
#
# Note what does NOT threaten it, because the obvious worry is the wrong one: a
# per-attempt TIMEOUT is classified TIMEOUT and fails fast without retrying
# (routing/router.py, the fast-fail category set), precisely so a timeout cannot
# be multiplied. The case that does spend the budget is a provider failing
# SLOWLY without timing out — a 503 returned at ~25s is TRANSIENT, and under
# ``user_facing`` (max_retries: 2) that is three goes at the same dead provider
# before the chain advances.
#
# Both desk call sites therefore use ``desk_interactive``, which drops the
# transient retry too: for an interactive turn the next provider is worth more
# than another go at the one that just failed, and the budget can buy one or the
# other. The walk is then bounded by chain length alone —
# 3 providers x 1 attempt x 30s = 90s, inside 110s, with the remainder absorbing
# the router's own overhead and any rate-gate wait.
_PER_ATTEMPT_TIMEOUT_SECONDS = 30.0

# How long to wait before re-checking that the loop is still alive. Short enough
# that a shutdown answers in about a second rather than at the far end of the
# wall; long enough to cost nothing on the happy path, where the future is
# pending at this point regardless.
_LIVENESS_RECHECK_SECONDS = 1.0

# Reject, never truncate. The largest legitimate request is a call-context answer
# (~9KB of material plus prompts) carried with conversation history; 256KB is
# ample headroom for that and still refuses a body that would be held whole in
# memory. A request over this is not a value we accept — it is not trimmed to fit.
_MAX_BODY_BYTES = 256 * 1024

# Per the OpenAI shape the caller speaks; also accepted as ``max_completion_tokens``.
_DEFAULT_MAX_TOKENS = 400
_MAX_MAX_TOKENS = 8192

# One desktop client, occasionally two lanes at once. The bound is not about this
# endpoint's own cost: provider rate gates serialize process-wide, and the chains
# here are shared with dozens of Genesis call sites, so an unbounded desk client
# queues Genesis's own triage and reflection behind it. Each request also holds a
# Flask thread on the same app that serves the dashboard and health probes.
_MAX_CONCURRENT = 4
_semaphore = threading.Semaphore(_MAX_CONCURRENT)


def _err(message: str, status: int, kind: str = "invalid_request_error"):
    """Every refusal is an ``error`` object with NO ``choices`` key.

    A caller parsing ``choices[0].message.content`` then raises instead of
    speaking the error aloud as though it were the answer.
    """
    return jsonify({"error": {"message": message, "type": kind}}), status


def _lane_call_site(data: dict) -> tuple[str, str]:
    """(lane, call_site) from the ``model`` field or the lane header.

    ``model`` is checked FIRST: every OpenAI client sends it and no proxy strips
    it, whereas an unknown header can be dropped in transit — and a dropped
    header fails silently, with the phone lane simply never engaging. A generic
    ``model`` (the ordinary case) falls through to the header.

    An unknown or absent lane resolves to PRIMARY, the capable one. That direction
    is deliberate: silently serving an unrecognised lane from the fast chain
    would degrade instruction-following with no error anywhere, which is the
    failure that is hardest to notice from the desktop side.
    """
    model = (data.get("model") or "").strip().lower()
    for name in _LANES:
        if model == name or model.endswith(f"-{name}"):
            return name, _LANES[name]
    raw = (request.headers.get("X-Genesis-Lane") or "").strip().lower()
    lane = raw if raw in _LANES else _DEFAULT_LANE
    return lane, _LANES[lane]


def _messages_from(data: dict) -> tuple[list[dict], str | None]:
    """Validate the OpenAI ``messages`` array. Returns (messages, error)."""
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        return [], "messages must be a non-empty array"

    clean: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            return [], "each message must be an object"
        role = m.get("role")
        if role in ("tool", "function"):
            return [], (
                "tool/function messages are not supported on this endpoint — "
                "it routes text completions and carries no tool protocol"
            )
        if role not in ("system", "user", "assistant"):
            return [], f"unsupported message role: {role!r}"
        content = m.get("content")
        if content is None:
            # The ordinary shape of an assistant turn that only carried
            # tool_calls. Degrade it to an empty turn rather than 400 the whole
            # request: the caller sends full history, and one such turn in it
            # must not kill an unrelated question.
            content = ""
        if isinstance(content, list):
            # Validate EVERY block before flattening any of it. The earlier
            # predicate let two shapes through silently: a block with no `type`
            # at all, and a scalar — neither matched the rejection test, and
            # neither matched the join filter either, so both were DISCARDED
            # and a materially incomplete prompt was routed as though whole.
            # A third shape, {"type": "text", "text": 1}, passed both and then
            # raised inside str.join as a 500 on a parseable request.
            parts: list[str] = []
            for b in content:
                if not isinstance(b, dict):
                    return [], "each content block must be an object"
                kind = b.get("type")
                if kind != "text":
                    return [], (
                        "image and other non-text content is not supported on "
                        "this endpoint — Genesis's routing layer is text-only, "
                        "and answering from the text alone would describe "
                        "something never seen"
                    )
                text = b.get("text")
                if not isinstance(text, str):
                    return [], "a text block's 'text' must be a string"
                parts.append(text)
            content = " ".join(parts).strip()
        if not isinstance(content, str):
            return [], "message content must be a string or a text block array"
        clean.append({"role": role, "content": content})

    if not any(m["role"] != "system" for m in clean):
        return [], "messages must contain at least one non-system turn"
    return clean, None


def _sampling_from(data: dict) -> tuple[dict, str | None]:
    """``max_tokens`` / ``temperature`` for the router. Returns (kwargs, error).

    ``route_call`` forwards ``**kwargs`` to the delegate — two existing Genesis
    call sites already pass ``max_tokens`` this way. Dropping it would leave
    output length to whichever provider answered, which is exactly how a turn
    comes back empty.
    """
    raw = data.get("max_tokens")
    if raw is None:
        raw = data.get("max_completion_tokens")
    if raw is None:
        raw = _DEFAULT_MAX_TOKENS
    # bool is a subclass of int, so True would otherwise arrive as 1 — a
    # silently accepted nonsense value rather than a refused one. Strings and
    # fractions are refused for the same reason: int("4") and int(4.7) both
    # succeed and both mean the caller asked for something it did not get.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return {}, "max_tokens must be an integer"
    if isinstance(raw, float) and not raw.is_integer():
        return {}, "max_tokens must be a whole number"
    try:
        # A backstop only, now that the guards above run first: inf and nan are
        # both non-integral floats and are refused there, so nothing that could
        # raise OverflowError reaches int() any more. Kept because an uncaught
        # one here would be a 500 where a 400 belongs, and the guards above are
        # easier to loosen than this is to remember.
        max_tokens = int(raw)
    except (TypeError, ValueError, OverflowError):
        return {}, "max_tokens must be an integer"
    if max_tokens < 1:
        return {}, "max_tokens must be at least 1"
    kwargs: dict = {"max_tokens": min(max_tokens, _MAX_MAX_TOKENS)}

    temperature = data.get("temperature")
    if temperature is not None:
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool):
            return {}, "temperature must be a number"
        kwargs["temperature"] = float(temperature)
    return kwargs, None


@desk_api_bp.route("/v1/desk/chat/completions", methods=["POST"])
def desk_chat_completions():
    """Route one desktop-assistant turn through Genesis and answer OpenAI-shaped."""
    denied = check_bearer_token("desk brain API")
    if denied:
        message, status = denied
        return _err(message, status)

    # Two checks, and the second is the one that actually bounds memory.
    #
    # The header is advisory: content_length is None under
    # Transfer-Encoding: chunked, and `or 0` would compare 0 > cap and wave an
    # arbitrarily large body straight through. So it is only a cheap early
    # refusal for a caller that declares an oversized body honestly.
    #
    # The real bound is the READ. An earlier version called
    # request.get_data(), which MATERIALISES the whole stream and only then
    # compares its length — so a chunked body was held in memory in full before
    # being rejected, and the only live ceiling was the app-wide 500 MiB
    # MAX_CONTENT_LENGTH. Several concurrent authenticated requests could each
    # allocate toward that and exhaust the process that also serves the
    # dashboard. Reading cap+1 bytes bounds the allocation itself: enough to
    # know the body is too big, never more.
    #
    # The trade, stated because this endpoint otherwise promises a parseable
    # error over a dead socket: leaving an oversized CHUNKED body undrained can
    # surface to the client as a connection reset instead of the 413 (Werkzeug
    # documents this for the limited stream). Memory is worth more than the
    # status code on a request already being refused, and a caller sending
    # megabytes to a 256KB endpoint learns the same thing either way.
    if (request.content_length or 0) > _MAX_BODY_BYTES:
        return _err(f"request body exceeds {_MAX_BODY_BYTES} bytes", 413)
    body = request.stream.read(_MAX_BODY_BYTES + 1)
    if len(body) > _MAX_BODY_BYTES:
        return _err(f"request body exceeds {_MAX_BODY_BYTES} bytes", 413)

    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    router = getattr(rt, "router", None)
    if not rt.is_bootstrapped or router is None:
        return _err("Genesis router not available", 503, "server_error")

    event_loop = current_app.config.get("GENESIS_EVENT_LOOP")
    if event_loop is None or not event_loop.is_running():
        return _err("Event loop not available", 503, "server_error")

    # Parsed from the bounded read above, not from request.get_json(): the
    # stream has already been consumed, and re-reading it would yield nothing.
    try:
        data = json.loads(body) if body.strip() else {}
    except ValueError:
        return _err("request body is not valid JSON", 400)
    # A list or a bare scalar used to become {} here, and the caller was then
    # told its messages array was missing — an accurate-sounding error about
    # the wrong thing. Say what is actually wrong.
    if not isinstance(data, dict):
        return _err("request body must be a JSON object", 400)
    if data.get("stream"):
        return _err("streaming is not supported on this endpoint", 400)

    messages, err = _messages_from(data)
    if err:
        return _err(err, 400)
    sampling, err = _sampling_from(data)
    if err:
        return _err(err, 400)

    lane, call_site = _lane_call_site(data)

    if not _semaphore.acquire(timeout=5):
        logger.warning("Desk %s lane rejected — concurrency limit reached", lane)
        return _err("server busy, try again shortly", 503, "server_error")
    start = time.monotonic()
    try:
        coro = router.route_call(
            call_site_id=call_site,
            messages=messages,
            suppress_dead_letter=True,
            timeout=_PER_ATTEMPT_TIMEOUT_SECONDS,
            **sampling,
        )
        try:
            future = asyncio.run_coroutine_threadsafe(coro, event_loop)
        except RuntimeError:
            # The loop was running at the readiness check and is closed by the
            # time we submit — an ordinary shutdown race. run_coroutine_threadsafe
            # raises BEFORE the handler below, so without this the caller gets
            # Flask's unstructured 500 and the coroutine is never awaited.
            # Close it explicitly: an un-awaited coroutine is a warning at best
            # and a leak at worst.
            coro.close()
            logger.warning("Desk %s lane: event loop closed during submission", lane)
            return _err("Event loop not available", 503, "server_error")
        # Wait in two steps, because a STOPPED loop is indistinguishable from a
        # slow provider if you only wait once. `run_coroutine_threadsafe`
        # succeeds against a stopped-but-not-closed loop and returns a future
        # that will never resolve, so a single 110s wait hangs the request for
        # the whole wall, holds a semaphore slot and a Flask thread the entire
        # time, and then blames the router in a 504. A short first wait costs
        # nothing on the happy path (the future is pending either way) and turns
        # a shutdown into a 1s structured 503 instead of a 110s lie.
        try:
            result = future.result(timeout=_LIVENESS_RECHECK_SECONDS)
        except TimeoutError:
            if not event_loop.is_running():
                future.cancel()
                logger.warning("Desk %s lane: event loop stopped mid-request", lane)
                return _err("Event loop not available", 503, "server_error")
            try:
                remaining = _ROUTER_TIMEOUT_SECONDS - _LIVENESS_RECHECK_SECONDS
                result = future.result(timeout=remaining)
            except TimeoutError:
                future.cancel()
                elapsed = time.monotonic() - start
                # Re-check once more: the loop can stop during the long wait too.
                if not event_loop.is_running():
                    logger.warning("Desk %s lane: event loop stopped mid-request", lane)
                    return _err("Event loop not available", 503, "server_error")
                logger.error("Desk %s lane timed out after %.1fs", lane, elapsed)
                return _err(f"router timed out after {elapsed:.0f}s", 504, "server_error")
            except Exception:
                logger.error("Desk %s lane raised", lane, exc_info=True)
                return _err("router call failed", 500, "server_error")
        except Exception:
            logger.error("Desk %s lane raised", lane, exc_info=True)
            return _err("router call failed", 500, "server_error")
    finally:
        _semaphore.release()

    # EMPTY content is a failure too, not a short answer. A provider can return
    # success with content None or "" — a refusal, a content filter, or a
    # reasoning model that spent the whole budget thinking — and the caller would
    # speak that as silence rather than retrying or saying anything.
    content = (result.content or "").strip() if result.success else ""
    if not result.success or not content:
        logger.error(
            "Desk %s lane produced no answer: success=%s provider=%s error=%s",
            lane,
            result.success,
            result.provider_used,
            result.error,
        )
        return _err(
            f"no provider answered: {result.error or 'empty completion'}",
            502,
            "server_error",
        )

    elapsed_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "Desk %s lane: %s/%s → %dms (%d msgs)",
        lane,
        result.provider_used,
        result.model_id,
        elapsed_ms,
        len(messages),
    )

    return jsonify(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            # The model that ACTUALLY answered, not the one the caller asked for:
            # the caller logs this, and a lane name there would hide which
            # provider served the turn.
            "model": result.model_id or call_site,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    # A CONSTANT, and knowingly so: no truncation signal survives
                    # the routing layer (neither CallResult nor RoutingResult
                    # carries finish_reason), so the caller's retry-on-truncation
                    # path cannot fire. Plumbing it is tracked separately; this
                    # comment exists so "stop" is not read as a deliberate claim.
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": result.input_tokens,
                "completion_tokens": result.output_tokens,
                "total_tokens": result.input_tokens + result.output_tokens,
            },
        }
    )
