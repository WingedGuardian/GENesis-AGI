"""Pinned qualification transport; billing comes from retained gateway evidence.

No estimated delegate cost is a settlement receipt. Interrupted attempts keep
their funded liability and can only be reconciled with GET requests.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sys
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from decimal import Decimal, DecimalException
from pathlib import Path

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from genesis.routing.config import load_config_from_string
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.types import RoutingResult

from .accounting import currency, total
from .corpus import route, versions
from .evidence import Incomplete, canonical, digest, load_json

ENDPOINT = "https://openrouter.ai/api/v1"
KEY_ENV = "GENESIS_QUALIFICATION_OPENROUTER_KEY"
PRODUCTION_KEY_NAMES = ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN")
ROOT = Path(__file__).resolve().parents[4]
ROUTES = ("judge", "relevance", "novelty")
ROUTE_KEYS = {"temperature", "max_tokens", "top_p", "seed", "reasoning"}
# Processing bound, not billing precision: never round evidence to fit it.
# Refusal retains raw evidence and the attempt's funded maximum liability.
MAX_CURRENCY_CHARS = 4096
# The binding limit is the delegate's whole-call wall clock (asyncio.wait_for).
# Every HTTPX client gets the same value, which HTTPX applies per phase
# (connect/read/write/pool), so it never cuts a response off before the wall
# clock does. A response slower than the wall clock leaves a dispatched
# attempt for the existing GET-only recovery path; it is never resent.
COMPLETION_TIMEOUT_S = 120


class LocalFailure(Incomplete):
    """Harness failure, not a candidate verdict."""


_SCOPED_SECRETS = ContextVar("qualification_redaction_secrets", default=frozenset())


def _redaction_values() -> frozenset[str]:
    """Retain known credentials while their SDK aliases are temporarily hidden."""
    return _SCOPED_SECRETS.get() | {
        value
        for name, value in os.environ.items()
        if value
        and (name == KEY_ENV or len(value) >= 8)
        and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }


def _literal_text(value: str) -> str:
    """Redact known credentials, including overlapping prefixes."""
    for secret in sorted(_redaction_values(), key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


# Local processing bounds, not provider limits or billing precision.
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_EVIDENCE_DEPTH = 64
OPAQUE_EVIDENCE = "[REDACTED: unsafe evidence]"
_JSON_PARTS = re.compile(r'["{}\[\]]')


def _json_parts(value):
    """Scan delimiters once; the stdlib consumes disjoint JSON string spans."""
    end = 0
    for match in _JSON_PARTS.finditer(value):
        if match.start() < end:
            continue
        decoded, end = None, match.end()
        if match.group() == '"':
            decoded, end = json.decoder.scanstring(value, end)
        yield match, end, decoded


def external_json(value):
    """Bound external parsing; the existing loader still decides JSON validity."""
    try:
        if len(value) > MAX_EVIDENCE_BYTES:
            raise ValueError
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        if len(text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValueError
        depth = 0
        for match, _, _ in _json_parts(text):
            token = match.group()
            depth += (token in ("{", "[")) - (token in ("}", "]"))
            if depth > MAX_EVIDENCE_DEPTH:
                raise ValueError
        return load_json(text)
    except (ValueError, UnicodeError, TypeError, RecursionError):
        raise LocalFailure("invalid or excessive external JSON") from None


_WIRE_FIELDS = frozenset({
    "id", "model", "object", "created", "choices", "index", "message", "role",
    "content", "finish_reason", "usage", "prompt_tokens", "completion_tokens",
    "total_tokens", "cost", "openrouter_metadata", "requested", "strategy",
    "attempt", "is_byok", "endpoints", "total", "available", "provider", "selected",
    "data", "total_cost", "provider_name", "upstream_id", "error", "code",
    "pipeline", "attempts", "status",
})
_KEY_COLON = re.compile(r"\s*:")


def safe_text(value: str, *, schema_keys: frozenset[str] = frozenset(), _depth=0) -> str:
    """Redact literal and JSON-escaped credentials in arbitrary diagnostic text.

    A JSON object/array is redacted only inside its string tokens, so a numeric
    lexeme that happens to contain a credential's digits stays valid JSON.
    Other text also gets a final literal pass over everything.
    """
    if len(value) > MAX_EVIDENCE_BYTES or _depth > MAX_EVIDENCE_DEPTH:
        return OPAQUE_EVIDENCE
    try:
        return _redacted_text(value, schema_keys, _depth)
    except (ValueError, RecursionError):
        return OPAQUE_EVIDENCE


def _redacted_text(value, schema_keys, depth):
    """Certify string spans without reserializing numeric wire lexemes."""
    try:
        document = isinstance(external_json(value), (dict, list))
    except LocalFailure:
        document = False
    pieces, end = [], 0
    for match, token_end, decoded in _json_parts(value):
        if decoded is None:
            continue
        prefix, token = value[end:match.start()], value[match.start():token_end]
        if "\\" in prefix:
            return OPAQUE_EVIDENCE
        trusted = document and decoded in schema_keys and _KEY_COLON.match(value, token_end)
        # Decoded values remain untrusted text, including another JSON layer.
        # The final literal pass keeps numeric text inside values untrusted.
        redacted = decoded if trusted else _literal_text(safe_text(decoded, _depth=depth + 1))
        pieces.extend((prefix, json.dumps(redacted) if redacted != decoded else token))
        end = token_end
    if "\\" in value[end:]:
        return OPAQUE_EVIDENCE
    strings = "".join((*pieces, value[end:]))
    return strings if document else _literal_text(strings)


def exception_name(error: BaseException) -> str:
    """An exception's name is diagnostic data, not a trusted schema key."""
    try:
        name = safe_text(type(error).__name__)
        return name if name.strip() else "[REDACTED]"
    except BaseException:
        return "[REDACTED]"


def failure_message(error: BaseException) -> str:
    """Diagnostic formatting must not prevent durable failure recording.

    Catch only while rendering; the call/cleanup caller retains responsibility
    for propagating the original exception, including cancellation.
    """
    try:
        message = safe_text(str(error))
    except BaseException:
        message = ""
    return message if message.strip() else exception_name(error)


def _safe_value(value, depth=0):
    """Redact untrusted values, including arbitrary nested dictionary keys."""
    if depth > 2 * MAX_EVIDENCE_DEPTH:
        raise LocalFailure("evidence nesting exceeds the processing bound")
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(key): _safe_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_value(item, depth + 1) for item in value]
    return value


def safe_evidence(value: dict, depth=0) -> dict:
    """Redact a locally constructed evidence record without renaming its schema.

    Usage, selected response headers and already-sanitized association are
    nested schema records.
    Their field values, and other arbitrary structured values, stay untrusted.
    """
    if depth > 2 * MAX_EVIDENCE_DEPTH:
        raise LocalFailure("evidence nesting exceeds the processing bound")
    result = {}
    for key, item in value.items():
        if key == "raw_body" and isinstance(item, str):
            result[key] = safe_text(item, schema_keys=_WIRE_FIELDS)
        elif key in ("usage", "association", "response_headers") and isinstance(item, dict):
            result[key] = safe_evidence(item, depth + 1)
        else:
            result[key] = _safe_value(item, depth + 1)
    return result


def safe_association(value):
    """Keep association schema names; redact all extra operator-supplied keys."""
    try:
        value = external_json(canonical(value))
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise LocalFailure("association must be finite serializable JSON") from None
    if not isinstance(value, dict):
        return _safe_value(value)
    return {
        key if key in ("attempt", "request_hash", "generation_id", "evidence")
        else safe_text(key): _safe_value(item)
        for key, item in value.items()
    }


def safe_money_evidence(value: dict, money: dict[tuple, str]) -> dict:
    """Redact evidence, keeping validated money fields out of the literal pass.

    ``money`` maps a path to an amount that already passed ``financial_json``.
    A bare decimal string is not a JSON document, so the generic redactor would
    rewrite a credential's digits inside it and Journal could never settle it.
    An amount is re-inserted only when it is still a pure decimal string.
    """
    result = safe_evidence(value)
    for path, amount in money.items():
        try:
            currency(amount)
        except Incomplete:
            continue  # Not proven pure money: keep the redacted form.
        target = result
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = amount
    return result


def safe_body(data: bytes) -> str:
    """Redact decoded JSON strings while leaving numeric lexemes untouched."""
    try:
        external_json(data)
        return safe_text(data.decode("utf-8"), schema_keys=_WIRE_FIELDS)
    except LocalFailure:
        return OPAQUE_EVIDENCE


def fixed_currency(value: Decimal) -> str:
    """Expand a lexical JSON number exactly, before allocating its representation."""
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed():
        raise LocalFailure("billing amount must be a nonnegative finite JSON number")
    if value.is_zero():
        return "0"
    _, digits, exponent = value.as_tuple()
    size = len(digits) + exponent if exponent >= 0 else max(len(digits), 1 - exponent) + 1
    if size > MAX_CURRENCY_CHARS:
        raise LocalFailure("billing amount exceeds the evidence representation limit")
    return format(value, "f")


def financial_json(raw: bytes, paths: tuple[tuple[str, ...], ...]) -> dict:
    """Validate immutable bytes, then normalize only money from lexical Decimals.

    The first parse retains the existing duplicate-key and nonfinite authority.
    Its floats are never used to calculate or normalize a financial field.
    """
    try:
        result = external_json(raw)
        exact = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
        if not isinstance(result, dict):
            raise LocalFailure("provider JSON must be an object")
        for path in paths:
            target, source = result, exact
            for key in path[:-1]:
                target, source = target[key], source[key]
            target[path[-1]] = fixed_currency(source[path[-1]])
        return result
    except (ValueError, UnicodeError, KeyError, TypeError, DecimalException) as exc:
        raise LocalFailure("invalid financial evidence: " + failure_message(exc)) from None


class DeferredCloseStream(httpx.AsyncByteStream):
    """Let HTTPX decode/cache the body before releasing its underlying stream."""

    def __init__(self, source):
        self.source = source

    def __aiter__(self):
        return self.source.__aiter__()


def _selected_routes(raw):
    metadata = raw.get("openrouter_metadata")
    endpoints = metadata.get("endpoints") if isinstance(metadata, dict) else None
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    selected = [item for item in available if isinstance(item, dict) and item.get("selected") is True] \
        if isinstance(available, list) else []
    return metadata, available, selected


def _route_facts(observation):
    """Derive identity from retained bytes; no acknowledgement changes these facts."""
    try:
        raw = external_json(observation["raw_body"])
        if not isinstance(raw, dict):
            raise LocalFailure("routing response is not an object")
    except (KeyError, ValueError, UnicodeError, TypeError) as exc:
        raise LocalFailure("routing response evidence is unavailable") from exc
    return raw, *_selected_routes(raw)


def _response_identity(raw, response_headers):
    """Retain only gateway identity/cache headers and keep contrary IDs separate."""
    headers = {name: response_headers[name] for name in (
        "x-generation-id", "x-openrouter-cache-status"
    ) if name in response_headers}
    body, header = raw.get("id"), headers.get("x-generation-id")
    generation = body if "id" in raw else header
    conflict = body is not None and header is not None and body != header
    _, _, selected = _selected_routes(raw)
    return {
        "response_provider": selected[0].get("provider") if len(selected) == 1 else raw.get("provider"),
        "body_generation_id": body, "header_generation_id": header,
        "generation_id": [body, header] if conflict else generation,
        "response_headers": headers,
        "error": "body and header generation identities conflict" if conflict else None,
    }


def _observed_identity(observation, spec):
    """Keep field-specific conflicting facts unresolved in existing accounting fields.

    Manifest identities remain strings. A list of actual contrary observation
    facts cannot equal that frozen string, including after a late observation.
    """
    try:
        raw, _, _, selected = _route_facts(observation)
    except LocalFailure:
        # Malformed/missing raw evidence must still be durably observed. Legacy
        # receipt-only observations can account expense, never qualify a route.
        return {"model": observation.get("model", observation.get("response_model")),
                "upstream": observation.get("upstream")}
    models = [raw.get("model"), *(item.get("model") for item in selected)]
    providers = [raw.get("provider"), *(item.get("provider") for item in selected)]
    wrong_models = [value for value in models if value is not None and value != spec["model"]]
    wrong_providers = [value for value in providers if value is not None and value != spec["receipt_provider"]]
    return {
        "model": wrong_models or observation.get("response_model", observation.get("model")),
        "upstream": wrong_providers or (
            spec["upstream"] if observation.get("response_provider") == spec["receipt_provider"] else None
        ),
    }


def verify_routing(observation, spec):
    """Qualify the documented gateway route, never infer backend parameter execution."""
    raw, metadata, available, selected = _route_facts(observation)
    headers = observation.get("response_headers", {})
    if not isinstance(headers, dict) or headers.get("x-openrouter-cache-status") not in (None, "MISS"):
        raise LocalFailure("response is not evidence of a fresh gateway completion")
    if (
        not isinstance(metadata, dict)
        or not isinstance(available, list)
        or any(not isinstance(item, dict) or type(item.get("selected")) is not bool for item in available)
        or len(selected) != 1
        or raw.get("model") != spec["model"]
        or metadata.get("requested") != spec["model"]
        or metadata.get("strategy") != "direct"
        or type(metadata.get("attempt")) is not int
        or metadata["attempt"] != 1
        or metadata.get("is_byok") is not False
        or ("pipeline" in metadata and metadata["pipeline"] != [])
    ):
        raise LocalFailure("routing metadata does not verify one unchanged pinned gateway attempt")
    endpoint = selected[0]
    if (
        endpoint.get("model") != spec["model"]
        or endpoint.get("provider") != spec["receipt_provider"]
        or observation.get("response_provider") != endpoint["provider"]
        or ("provider" in raw and raw["provider"] != endpoint["provider"])
    ):
        raise LocalFailure("observed routing identity contradicts the frozen request")
    if "attempts" in metadata:
        attempts = metadata["attempts"]
        if (
            not isinstance(attempts, list) or len(attempts) != 1 or not isinstance(attempts[0], dict)
            or attempts[0].get("model") != spec["model"]
            or attempts[0].get("provider") != spec["receipt_provider"]
            or type(attempts[0].get("status")) is not int or attempts[0]["status"] != 200
        ):
            raise LocalFailure("gateway attempt history contradicts the frozen request")
    generation = raw.get("id")
    header_generation = headers.get("x-generation-id")
    if (
        not isinstance(generation, str) or not generation.strip()
        or observation.get("generation_id") != generation
        or (header_generation is not None and header_generation != generation)
    ):
        raise LocalFailure("routing generation identity is missing or contradictory")


class ObservedHTTPHandler(AsyncHTTPHandler):
    """One serialized egress guard, shared by every SDK replacement client.

    The supplied callback must append to Journal before this response hook
    returns. No outer SDK success or cancellable cleanup substitutes for it.
    """

    def __init__(self, expected: dict, key: str, record, *, transport=None, timeout=COMPLETION_TIMEOUT_S):
        self.expected, self.key, self.record = load_json(canonical(expected)), key, record
        self.posts = 0
        self.observed = None
        self.error = None
        self._transport = transport
        super().__init__(timeout=timeout)

    def create_client(self, *, timeout=None, **_kwargs):
        # Every client (initial and AsyncHTTPHandler's replacement after a dropped
        # connection) keeps the delegate's ceiling, never HTTPX's 5-second default.
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=self.timeout if timeout is None else timeout,
            follow_redirects=False,
            event_hooks={"request": [self.before], "response": [self.after]},
        )

    async def before(self, request: httpx.Request):
        self.posts += 1
        if self.posts != 1:
            self.error = self.error or "a repeated completion request was refused"
            raise LocalFailure(self.error)
        body = load_json(await request.aread())
        if (
            request.method != "POST"
            or str(request.url) != ENDPOINT + "/chat/completions"
            or request.headers.get("Authorization") != f"Bearer {self.key}"
            or canonical(body) != canonical(self.expected)
        ):
            self.error = "serialized request differs from the pinned request"
            raise LocalFailure(self.error)
        # Set these after SDK construction, at the final serialized egress hook.
        # SDK caching=False does not control the gateway's response cache.
        request.headers["X-OpenRouter-Metadata"] = "enabled"
        request.headers["X-OpenRouter-Cache"] = "false"

    async def after(self, response: httpx.Response):
        if response.is_closed:
            self.record_response(response, await response.aread())
            return
        stream = response.stream
        response.stream = DeferredCloseStream(stream)
        failure = None
        try:
            self.record_response(response, await response.aread())
        except BaseException as exc:
            failure = exc
            raise
        finally:
            # Partial reads also mark the response closed, so HTTPX's hook-error
            # cleanup cannot close the restored underlying stream a second time.
            await response.aclose()
            response.stream = stream
            try:
                await stream.aclose()
            except BaseException as exc:
                if failure is None:
                    raise
                failure.add_note("response stream cleanup also failed: " + exception_name(exc))

    def record_response(self, response: httpx.Response, data: bytes):
        raw_body = safe_body(data)
        money = {}
        try:
            raw = financial_json(data, (("usage", "cost"),))
            money[("usage", "cost")] = raw["usage"]["cost"]
        except LocalFailure as exc:
            self.error = failure_message(exc)
            # Preserve valid identity/content even when cost is absent or invalid.
            try:
                raw = external_json(data)
            except (ValueError, UnicodeError):
                raw = {}
        raw = raw if isinstance(raw, dict) else {}
        choices = raw.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        if response.status_code != 200 or "error" in raw or not choices:
            self.error = self.error or "provider returned an error instead of an answer"
        if choice.get("finish_reason") == "error" or not isinstance(content, str):
            self.error = self.error or "provider response has no usable answer"
        identity = _response_identity(raw, response.headers)
        self.error = self.error or identity["error"]
        self.observed = safe_money_evidence(
            {
                "http_status": response.status_code,
                "raw_body": raw_body,
                "finish_reason": choice.get("finish_reason"),
                "response_model": raw.get("model"),
                **identity,
                "usage": {
                    key: usage[key]
                    for key in ("prompt_tokens", "completion_tokens", "cost")
                    if key in usage
                },
                "content": content if isinstance(content, str) else None,
                "error": self.error,
            },
            money,
        )
        # Synchronous durable append: auth/SDK/close may raise after this returns.
        self.record(self.observed)


@contextmanager
def private_logs():
    """Redact log records and suppress normal SDK verbosity; restore both."""
    original = logging.getLogRecordFactory()
    # Log calls can originate on SDK threads without the caller's context.
    secrets = _redaction_values()

    def redacted(*args, **kwargs):
        token = _SCOPED_SECRETS.set(_redaction_values() | secrets)
        try:
            return render(*args, **kwargs)
        finally:
            _SCOPED_SECRETS.reset(token)

    def render(*args, **kwargs):
        record = original(*args, **kwargs)
        try:
            record.msg = safe_text(record.getMessage())
            if record.exc_info:
                record.exc_text = safe_text("".join(traceback.format_exception(*record.exc_info)))
            elif record.exc_text:
                record.exc_text = safe_text(record.exc_text)
            if record.stack_info:
                record.stack_info = safe_text(record.stack_info)
        except BaseException:
            # Formatting is diagnostic only, including hostile __str__/repr.
            # Do not retain any partially rendered or unsanitized fallback.
            record.msg = "[REDACTED: unformattable log record]"
            record.exc_text = None
            record.stack_info = None
        finally:
            record.args, record.exc_info = (), None
        return record

    # LiteLLM otherwise PRINTS a help banner and the raw provider error to stdout,
    # which would both corrupt the CLI's one-line JSON and bypass redaction.
    quiet = litellm.suppress_debug_info
    verbose = litellm.set_verbose
    logging.setLogRecordFactory(redacted)
    litellm.suppress_debug_info = True
    litellm.set_verbose = False
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)
        litellm.suppress_debug_info = quiet
        litellm.set_verbose = verbose


def check_callback_isolation():
    # The SDK tests string truthiness, so even "0" enables unredacted stdout.
    if os.environ.get("LITELLM_PRINT_STANDARD_LOGGING_PAYLOAD"):
        raise LocalFailure("LiteLLM payload printing is active; use a fresh CLI process")
    names = (
        "callbacks",
        "input_callback",
        "success_callback",
        "failure_callback",
        "_async_input_callback",
        "_async_success_callback",
        "_async_failure_callback",
        "pre_call_rules",
        "post_call_rules",
    )
    if any(getattr(litellm, name, None) for name in names):
        raise LocalFailure("global LiteLLM observers are active; use a fresh CLI process")


def production_credentials() -> set[str]:
    """Every production OpenRouter key this process can see, env and secrets.env."""
    values = {os.environ.get(name) for name in PRODUCTION_KEY_NAMES}
    try:
        from dotenv import dotenv_values

        from genesis.env import secrets_path

        if secrets_path().is_file():
            stored = dotenv_values(secrets_path())
            values |= {stored.get(name) for name in PRODUCTION_KEY_NAMES}
    except (ImportError, OSError) as exc:
        # Unable to look means unable to rule the production key out.
        raise LocalFailure("cannot read secrets.env to rule out the production key") from exc
    return {v for v in values if v}


@contextmanager
def scoped_credential(key: str):
    """The delegate reads its key from the environment; lend it the dedicated one."""
    saved = {name: os.environ.get(name) for name in PRODUCTION_KEY_NAMES}
    token = _SCOPED_SECRETS.set(_redaction_values())
    try:
        for name in PRODUCTION_KEY_NAMES:
            os.environ.pop(name, None)
        os.environ["OPENROUTER_API_KEY"] = key
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _SCOPED_SECRETS.reset(token)


def _budget_paths(raw: bytes) -> tuple[tuple, ...]:
    """Every returned budget remainder, which must parse as nonnegative money.

    OpenRouter's GET /key documents ``budgets`` (KeyBudget list) and
    ``effective_budget`` (KeyBudget or null), each with ``remaining_usd``.
    Absent fields add nothing; any present, malformed one refuses.
    """
    try:
        data = external_json(raw)["data"]
    except (ValueError, UnicodeError, KeyError, TypeError):
        return ()  # financial_json reports the malformed envelope itself
    if not isinstance(data, dict):
        return ()
    paths = []
    if data.get("effective_budget") is not None:
        if not isinstance(data["effective_budget"], dict):
            raise LocalFailure("effective_budget is not a budget object")
        paths.append(("data", "effective_budget", "remaining_usd"))
    if "budgets" in data:
        budgets = data["budgets"]
        if not isinstance(budgets, list) or not all(isinstance(item, dict) for item in budgets):
            raise LocalFailure("budgets is not a list of budget objects")
        paths.extend(("data", "budgets", index, "remaining_usd") for index in range(len(budgets)))
    for path in tuple(paths):
        budget = _at(data, path[1:-1])
        if "limit_usd" in budget or "spend_usd" in budget:
            # A partial pair is malformed; financial_json rejects its missing
            # partner instead of silently ignoring a supplied constraint.
            paths.extend(path[:-1] + (name,) for name in ("limit_usd", "spend_usd"))
    return tuple(paths)


def _at(data, path):
    for key in path:
        data = data[key]
    return data


async def key_status(key: str, transport=None, *, required="0") -> dict:
    """Read an explicit nonreset limit; a snapshot is not spending approval."""
    with private_logs():
        try:
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
                response = await client.get(
                    ENDPOINT + "/key", headers={"Authorization": f"Bearer {key}"}
                )
            if response.status_code != 200:
                raise LocalFailure("could not read the dedicated key's limit")
            budgets = _budget_paths(response.content)
            parsed = financial_json(
                response.content,
                (("data", "limit"), ("data", "limit_remaining"), ("data", "usage"), *budgets),
            )
            if "error" in parsed:
                raise LocalFailure("dedicated key lookup returned an error envelope")
            data = parsed["data"]
            if "limit_reset" not in data or data["limit_reset"] is not None:
                raise LocalFailure("dedicated key must explicitly have no limit reset")
            limit, remaining, usage = (
                Decimal(data[name]) for name in ("limit", "limit_remaining", "usage")
            )
            # limit/limit_remaining describe only the key's own limit; a member,
            # workspace or guardrail budget can block requests first.
            budget_headroom = [
                {
                    "limit_usd": _at(data, path[1:]),
                    "spend_usd": _at(data, path[1:-1] + ("spend_usd",)),
                }
                for path in budgets if path[-1] == "limit_usd"
            ]
            for budget in budget_headroom:
                budget["available"] = format(total([
                    Decimal(budget["limit_usd"]), Decimal(budget["spend_usd"]).copy_negate(),
                ]), "f")
            # Snapshots need not be exactly equal or atomic. Never admit more
            # than any reported or independently derived headroom supports.
            available = min([
                remaining, total([limit, usage.copy_negate()]),
                *(Decimal(_at(data, path[1:])) for path in budgets if path[-1] == "remaining_usd"),
                *(Decimal(item["available"]) for item in budget_headroom),
            ])
            if (
                available <= 0
                or available < Decimal(required)
                or remaining > limit
                or usage > limit
            ):
                raise LocalFailure(
                    "dedicated key has insufficient or contradictory credit evidence"
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, DecimalException) as exc:
            raise LocalFailure(failure_message(exc)) from None
    info = {name: data[name] for name in ("limit", "limit_remaining", "limit_reset", "usage")}
    # The budget remainders that decided dispatch, as returned (null stays null).
    if "effective_budget" in data:
        effective = data["effective_budget"]
        info["effective_budget_remaining_usd"] = None if effective is None else effective["remaining_usd"]
    if "budgets" in data:
        info["budgets_remaining_usd"] = [item["remaining_usd"] for item in data["budgets"]]
    info["available"] = format(available, "f")
    if budget_headroom:
        info["budget_headroom"] = budget_headroom
    return info


def dedicated_key() -> str:
    check_callback_isolation()
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise LocalFailure(f"{KEY_ENV} must hold a dedicated credit-limited key")
    if key in production_credentials():
        raise LocalFailure("refusing a production OpenRouter key")
    return key


async def reconcile(journal, attempt: str, *, transport=None, association=None):
    """Append GET evidence without sending a completion, including on recovery.

    Frozen schedule rows carry ``receipt_provider`` and
    ``provider_identity_evidence`` in addition to Journal's required identity.
    Receipt-only recovery requires separately verified request association.
    Supplying that association is an operator assertion, never a timestamp guess.
    """
    row = journal.attempt(attempt)
    spec = row["spec"]
    display = spec.get("receipt_provider")
    identity = spec.get("provider_identity_evidence")
    if (
        not row["dispatched"]
        or spec["endpoint"] != ENDPOINT + "/chat/completions"
        or spec["generation_namespace"] != "openrouter"
        or not isinstance(display, str)
        or not display.strip()
        or not isinstance(identity, str)
        or not identity.strip()
    ):
        raise LocalFailure("reconciliation requires frozen gateway and provider identity evidence")
    ids = [obs.get("generation_id") for obs in row["observations"]]
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise LocalFailure("observed generation identity is missing or contradictory")
    generations = set(ids)
    if row["observations"]:
        if (
            len(generations) != 1
            or not isinstance(next(iter(generations)), str)
            or not next(iter(generations)).strip()
        ):
            raise LocalFailure("observed generation identity is missing or contradictory")
        generation = next(iter(generations))
    else:
        if (
            not isinstance(association, dict)
            or association.get("attempt") != attempt
            or association.get("request_hash") != spec["request_hash"]
            or not isinstance(association.get("evidence"), str)
            or not association["evidence"].strip()
            or not isinstance(association.get("generation_id"), str)
            or not association["generation_id"].strip()
        ):
            raise LocalFailure(
                "receipt-only recovery needs independently verified attempt association"
            )
        generation = association["generation_id"]
    # Validate the serializer's whole input domain before doing any GET work.
    association_evidence = safe_association(association)
    key = dedicated_key()
    if hashlib.sha256(key.encode()).hexdigest() != spec.get("credential_fingerprint"):
        raise LocalFailure("reconciliation credential differs from frozen identity")
    with private_logs():
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            response = await client.get(
                ENDPOINT + "/generation",
                params={"id": generation},
                headers={"Authorization": f"Bearer {key}"},
            )
    evidence = {
        "generation_id": generation,
        "attempt": attempt,
        "request_hash": spec["request_hash"],
        "http_status": response.status_code,
        "raw_body": safe_body(response.content),
        "association": association_evidence,
        "provider_identity_evidence": identity,
    }
    try:
        if response.status_code != 200:
            raise LocalFailure("billing GET failed")
        envelope = financial_json(response.content, (("data", "total_cost"),))
        if "error" in envelope:
            raise LocalFailure("billing GET returned an error envelope")
        data = envelope["data"]
        evidence.update(
            response_generation_id=data.get("id"),
            response_model=data.get("model"),
            response_provider=data.get("provider_name"),
            upstream_id=data.get("upstream_id"),
        )
        if (
            data.get("id") != generation
            or data.get("model") != spec["model"]
            or data.get("provider_name") != display
        ):
            raise LocalFailure("billing GET identity differs from frozen request")
        for observation in row["observations"]:
            identity_fields = _observed_identity(observation, spec)
            if any(value is not None and value != spec[name] for name, value in identity_fields.items()):
                raise LocalFailure("retained routing identity contradicts billing settlement")
        evidence.update(model=data["model"], upstream=spec["upstream"], charge=data["total_cost"])
    except (ValueError, KeyError, TypeError) as exc:
        evidence["error"] = failure_message(exc)
        # Invalid evidence is still durable; Journal refuses settlement.
        journal.settle(attempt, safe_evidence(evidence))
        raise LocalFailure(evidence["error"]) from None
    # charge was set last inside the try, only after financial_json validated it.
    journal.settle(attempt, safe_money_evidence(evidence, {("charge",): evidence["charge"]}))
    return evidence


def routing(root: Path = ROOT):
    """The shipped routing config, without credentials or local overlays."""
    return load_config_from_string(
        (root / "config/model_routing.yaml").read_text(), check_api_keys=False
    )


def resolve(alias: str, config=None):
    cfg = (config or routing()).providers.get(alias)
    if cfg is None or cfg.provider_type != "openrouter" or cfg.base_url not in (None, ENDPOINT):
        raise Incomplete(f"{alias!r} is not an OpenRouter alias in the shipped routing config")
    return cfg


def _number(value, low, high=None) -> bool:
    try:
        return (
            type(value) in (int, float)
            and math.isfinite(value)
            and value >= low
            and (high is None or value <= high)
        )
    except OverflowError:
        return False


def validate_params(params) -> dict:
    """``{upstream, max_price: {prompt, completion}, judge, relevance, novelty}``.

    Ranges are OpenRouter's documented ones: temperature [0, 2], top_p [0, 1],
    positive integer max_tokens, integer seed
    (https://openrouter.ai/docs/api_reference/parameters). ``max_price`` is USD
    per million tokens (https://openrouter.ai/docs/features/provider-routing).
    """
    if not isinstance(params, dict) or set(params) != {"upstream", "max_price", *ROUTES}:
        raise Incomplete("params need exactly: upstream, max_price, judge, relevance, novelty")
    if not isinstance(params["upstream"], str) or not params["upstream"].strip():
        raise Incomplete("upstream must name one OpenRouter provider")
    price = params["max_price"]
    if (
        not isinstance(price, dict)
        or set(price) != {"prompt", "completion"}
        or not all(_number(v, 0) for v in price.values())
    ):
        raise Incomplete("max_price needs non-negative prompt and completion USD per million")
    for name in ROUTES:
        body = params[name]
        if not isinstance(body, dict) or set(body) - ROUTE_KEYS:
            raise Incomplete(f"{name}: unsupported parameters")
        if type(body.get("max_tokens")) is not int or body["max_tokens"] <= 0:
            raise Incomplete(f"{name}: an explicit positive max_tokens is required")
        for key, high in (("temperature", 2), ("top_p", 1)):
            if key in body and not _number(body[key], 0, high):
                raise Incomplete(f"{name}: invalid {key}")
        if "seed" in body and type(body["seed"]) is not int:
            raise Incomplete(f"{name}: invalid seed")
        reasoning = body.get("reasoning", {})
        if (
            not isinstance(reasoning, dict)
            or set(reasoning) - {"enabled", "exclude", "effort", "max_tokens"}
            or any(
                type(reasoning[key]) is not bool
                for key in ("enabled", "exclude")
                if key in reasoning
            )
            or (
                "effort" in reasoning
                and reasoning["effort"]
                not in ("max", "xhigh", "high", "medium", "low", "minimal", "none")
            )
            or (
                "max_tokens" in reasoning
                and (type(reasoning["max_tokens"]) is not int or reasoning["max_tokens"] < 0)
            )
            or {"effort", "max_tokens"} <= set(reasoning)
        ):
            raise Incomplete(f"{name}: invalid reasoning")
    return params


def request_body(model, params, contract, messages):
    """One canonical wire constructor for scheduling and actual dispatch."""
    return load_json(canonical({
        "model": model, "messages": messages, **params[route(contract)],
        "provider": {
            "only": [params["upstream"]], "allow_fallbacks": False,
            "require_parameters": True, "max_price": params["max_price"],
        },
        "usage": {"include": True},
    }))


class PinnedRouter:
    """Adapter router for one frozen, fully funded model campaign.

    ``binding.transports[alias]`` contains model, params, endpoint, common
    gateway generation_namespace and credential_fingerprint. Schedule rows
    also name contract/version/case_id/repetition/prompt_hash and the verified
    receipt-provider display mapping. Preparation and spending approval are
    the later runner's responsibility; this transport cannot derive a bound.
    """

    def __init__(self, campaign, *, alias, params, dispatch, transport=None, config=None,
                 dispatch_admission=None):
        self.campaign, self.alias = campaign, alias
        self.config = config or routing()
        self.cfg = resolve(alias, self.config)
        self.params = load_json(canonical(validate_params(params)))
        if self.cfg.params:
            raise LocalFailure("implicit routing parameters are forbidden in qualification")
        self.transport, self.dispatch = transport, dispatch
        self.dispatch_admission = dispatch_admission
        # One detached snapshot at construction, never a full campaign copy per call.
        state = campaign.state
        self.manifest = state.manifest
        transports = self.manifest["binding"].get("transports")
        binding = transports.get(alias) if isinstance(transports, dict) else None
        expected = {
            "model": self.cfg.model_id,
            "params": self.params,
            "endpoint": ENDPOINT,
            "generation_namespace": "openrouter",
        }
        if not isinstance(binding, dict) or any(
            canonical(binding.get(k)) != canonical(v) for k, v in expected.items()
        ):
            raise LocalFailure("transport differs from the frozen campaign binding")
        fingerprint = binding.get("credential_fingerprint")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(c not in "0123456789abcdef" for c in fingerprint)
        ):
            raise LocalFailure("campaign must freeze a dedicated credential fingerprint")
        self.fingerprint = fingerprint
        self.schedule = {}
        maxima = []
        for attempt, row in state.attempts.items():
            spec = row["spec"]
            if (
                spec["alias"] != alias
                or spec["model"] != self.cfg.model_id
                or spec["upstream"] != self.params["upstream"]
                or spec["endpoint"] != ENDPOINT + "/chat/completions"
                or spec["generation_namespace"] != "openrouter"
                or spec.get("credential_fingerprint") != self.fingerprint
                or not isinstance(spec.get("receipt_provider"), str)
                or not spec["receipt_provider"].strip()
                or not isinstance(spec.get("provider_identity_evidence"), str)
                or not spec["provider_identity_evidence"].strip()
            ):
                raise LocalFailure("schedule differs from the frozen transport identity")
            coordinate = tuple(
                spec.get(k) for k in ("contract", "version", "case_id", "repetition", "prompt_hash")
            )
            contract, version, case_id, repetition, prompt_hash = coordinate
            if (
                not isinstance(contract, str)
                or contract not in versions()
                or versions().get(contract) != version
                or not isinstance(case_id, str)
                or not case_id.strip()
                or type(repetition) is not int
                or repetition not in (1, 2, 3)
                or not isinstance(prompt_hash, str)
                or len(prompt_hash) != 64
                or any(c not in "0123456789abcdef" for c in prompt_hash)
                or coordinate in self.schedule
            ):
                raise LocalFailure("invalid or duplicate frozen attempt coordinate")
            self.schedule[coordinate] = attempt
            if not row["dispatched"]:
                maxima.append(Decimal(spec["max_charge"]))
        self.required = total(maxima)
        self.failure = None
        self.calls, self.local, self.served = [], {}, {}

    def bind(self, contract, repetition, *, case_id=None, prompts=None):
        if contract not in versions() or type(repetition) is not int or repetition not in (1, 2, 3):
            raise LocalFailure("unknown contract or repetition")
        self.contract, self.repetition = contract, repetition
        self.case_id, self.prompts = case_id, dict(prompts or {})
        self.calls, self.local, self.served = [], {}, {}

    def stop_processing(self, case_id, error):
        message = failure_message(error)
        self.failure = LocalFailure(message)
        if case_id in self.served:
            self.campaign.fail(self.served[case_id], message)

    def request(self, messages):
        return request_body(self.cfg.model_id, self.params, self.contract, messages)

    async def route_call(self, call_site_id, messages, **kwargs):
        case_id = self.case_id or self.prompts.get(digest(messages))
        try:
            if not isinstance(case_id, str):
                raise LocalFailure("prompt matches no corpus case")
            if kwargs.pop("chain_offset", 0):
                raise LocalFailure("qualification prohibits chain rotation")
            configured = self.params[route(self.contract)]
            if any(
                name not in configured or canonical(value) != canonical(configured[name])
                for name, value in kwargs.items()
            ):
                raise LocalFailure("call-site parameters contradict the frozen configuration")
            coordinate = (
                self.contract,
                versions()[self.contract],
                case_id,
                self.repetition,
                digest(messages),
            )
            attempt = self.schedule.get(coordinate)
            if attempt is None:
                raise LocalFailure("unscheduled request")
            expected = self.request(messages)
            row = self.campaign.attempt(attempt)
            if digest(expected) != row["spec"]["request_hash"]:
                raise LocalFailure("request differs from frozen schedule")
            self.campaign.ready()
            if row["dispatched"]:
                if len(row["observations"]) != 1 or row["totals"][1] != 0:
                    raise LocalFailure("attempt has no verified retained answer; never resend")
                answer = row["observations"][0]
            else:
                if not self.dispatch:
                    raise LocalFailure("unanswered")
                answer = await self._send(attempt, expected)
            spec = row["spec"]
            verify_routing(answer, spec)
            if (
                answer.get("model") != spec["model"]
                or answer.get("upstream") != spec["upstream"]
                or answer.get("response_provider") != spec["receipt_provider"]
                or answer.get("http_status") != 200
                or answer.get("error")
                or not isinstance(answer.get("content"), str)
            ):
                raise LocalFailure("retained response is not verified evidence of the pinned model")
        except Exception as exc:
            failure = exc if isinstance(exc, LocalFailure) else LocalFailure(failure_message(exc))
            self.local[case_id or "<unmatched>"] = failure_message(failure)
            self.failure = failure
            if failure is exc:
                raise
            # The original exception may echo a credential; callers can print
            # this exception without exposing an unredacted traceback cause.
            raise failure from None
        self.served[case_id] = attempt
        self.calls.append((messages, answer["content"]))
        return RoutingResult(
            success=True,
            call_site_id=call_site_id,
            provider_used=self.alias,
            model_id=self.cfg.model_id,
            content=answer["content"],
            attempts=1,
        )

    def _failure(self, attempt, error):
        message = failure_message(error)
        self.failure = LocalFailure(message)
        try:
            self.campaign.fail(attempt, message)
        except BaseException as append_error:
            # Preserve the original cancellation/call/cleanup exception. Journal
            # poisoning still prevents all subsequent reads and egress.
            error.add_note("failure evidence append failed: " + exception_name(append_error))

    async def _send(self, attempt, expected):
        secret = dedicated_key()
        if hashlib.sha256(secret.encode()).hexdigest() != self.fingerprint:
            raise LocalFailure("dedicated credential differs from frozen identity")
        info = await key_status(secret, self.transport, required=str(self.required))
        if self.dispatch_admission is not None:
            # Check the exact validated observation before owning a client or
            # reserving/dispatching. Nested metadata remains detached evidence.
            await self.dispatch_admission(self, attempt, deepcopy(info))
        row = self.campaign.attempt(attempt)
        def record(observation):
            spec = row["spec"]
            observed = {
                **observation,
                **_observed_identity(observation, spec),
            }
            self.campaign.observe(attempt, observed)
            verify_routing(observed, spec)
            if (
                observation["response_model"] != spec["model"]
                or observation["response_provider"] != spec["receipt_provider"]
                or observation["http_status"] != 200
                or observation["error"]
            ):
                raise LocalFailure("provider observation is not verified pinned-model evidence")

        body = self.params[route(self.contract)]
        flat = {k: v for k, v in body.items() if k != "reasoning"}
        extra = {
            "provider": expected["provider"],
            **({"reasoning": body["reasoning"]} if "reasoning" in body else {}),
        }
        # Local client setup must succeed before an irreversible dispatch event.
        handler = ObservedHTTPHandler(expected, secret, record, transport=self.transport)
        dispatched = False
        try:
            if row.get("reserved_at") is None:
                self.campaign.reserve(attempt)
            self.campaign.append(
                "dispatch",
                attempt=attempt,
                request_hash=row["spec"]["request_hash"],
                key_status=info,
                credential_fingerprint=self.fingerprint,
            )
            dispatched = True
            self.required = total([self.required, Decimal(row["spec"]["max_charge"]).copy_negate()])
            try:
                with private_logs(), scoped_credential(secret):
                    result = await LiteLLMDelegate(self.config).call(
                        self.alias,
                        self.cfg.model_id,
                        expected["messages"],
                        client=handler,
                        num_retries=0,
                        max_retries=0,
                        success_callback=[],
                        failure_callback=[],
                        caching=False,
                        timeout=COMPLETION_TIMEOUT_S,
                        extra_body=extra,
                        **flat,
                    )
                if (
                    getattr(result, "success", None) is not True
                    or handler.error
                    or handler.observed is None
                ):
                    raise LocalFailure(
                        handler.error
                        or safe_text(str(getattr(result, "error", "invalid delegate result")))
                    )
            except BaseException as exc:
                self._failure(attempt, exc)
                raise
        finally:
            active = sys.exception()
            try:
                await handler.close()
            except BaseException as cleanup:
                if dispatched:
                    self._failure(attempt, cleanup)
                if active is None or not isinstance(cleanup, Exception):
                    raise
        try:
            await reconcile(self.campaign, attempt, transport=self.transport)
        except BaseException as exc:
            self._failure(attempt, exc)
            raise
        self.campaign.ready()
        return self.campaign.attempt(attempt)["observations"][0]
