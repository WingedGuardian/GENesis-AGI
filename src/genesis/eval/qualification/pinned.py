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
from decimal import Decimal, DecimalException
from pathlib import Path

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from genesis.routing.config import load_config_from_string
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.types import RoutingResult

from .accounting import total
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


class LocalFailure(Incomplete):
    """Harness failure, not a candidate verdict."""


def _literal_text(value: str) -> str:
    """Redact environment credentials, including overlapping prefixes."""
    secrets = {
        value
        for name, value in os.environ.items()
        if value
        and (name == KEY_ENV or len(value) >= 8)
        and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def safe_text(value: str) -> str:
    """Redact literal and JSON-escaped credentials in arbitrary diagnostic text."""
    def redact(match):
        token = match.group()
        try:
            decoded = json.loads(token)
        except ValueError:
            return _literal_text(token)
        redacted = _literal_text(decoded)
        return json.dumps(redacted) if redacted != decoded else token

    return _literal_text(re.sub(r'"(?:[^"\\]|\\.)*"', redact, value))


def safe_evidence(value):
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(key): safe_evidence(item) for key, item in value.items()}
    if isinstance(value, list):
        return [safe_evidence(item) for item in value]
    return value


def safe_body(data: bytes) -> str:
    """Redact decoded JSON strings while leaving numeric lexemes untouched."""
    return safe_text(data.decode("utf-8", errors="replace"))


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
        result = load_json(raw)
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
        raise LocalFailure("invalid financial evidence: " + safe_text(str(exc))) from None


class DeferredCloseStream(httpx.AsyncByteStream):
    """Let HTTPX decode/cache the body before releasing its underlying stream."""

    def __init__(self, source):
        self.source = source

    def __aiter__(self):
        return self.source.__aiter__()


class ObservedHTTPHandler(AsyncHTTPHandler):
    """One serialized egress guard, shared by every SDK replacement client.

    The supplied callback must append to Journal before this response hook
    returns. No outer SDK success or cancellable cleanup substitutes for it.
    """

    def __init__(self, expected: dict, key: str, record, *, transport=None):
        self.expected, self.key, self.record = load_json(canonical(expected)), key, record
        self.posts = 0
        self.observed = None
        self.error = None
        self._transport = transport
        super().__init__()

    def create_client(self, **_kwargs):
        return httpx.AsyncClient(
            transport=self._transport,
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
                failure.add_note("response stream cleanup also failed: " + type(exc).__name__)

    def record_response(self, response: httpx.Response, data: bytes):
        raw_body = safe_body(data)
        try:
            raw = financial_json(data, (("usage", "cost"),))
        except LocalFailure as exc:
            self.error = str(exc)
            # Preserve valid identity/content even when cost is absent or invalid.
            try:
                raw = load_json(data)
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
        self.observed = safe_evidence(
            {
                "http_status": response.status_code,
                "raw_body": raw_body,
                "finish_reason": choice.get("finish_reason"),
                "response_model": raw.get("model"),
                "response_provider": raw.get("provider"),
                "generation_id": raw.get("id"),
                "usage": {
                    key: usage[key]
                    for key in ("prompt_tokens", "completion_tokens", "cost")
                    if key in usage
                },
                "content": content if isinstance(content, str) else None,
                "error": self.error,
            }
        )
        # Synchronous durable append: auth/SDK/close may raise after this returns.
        self.record(self.observed)


@contextmanager
def private_logs():
    """Redact SDK and delegate log records and silence LiteLLM's stdout; restore both."""
    original = logging.getLogRecordFactory()

    def redacted(*args, **kwargs):
        record = original(*args, **kwargs)
        record.msg = safe_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = safe_text(record.stack_info)
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
    saved = {name: os.environ.pop(name, None) for name in PRODUCTION_KEY_NAMES}
    os.environ["OPENROUTER_API_KEY"] = key
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


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
            parsed = financial_json(
                response.content,
                tuple(("data", name) for name in ("limit", "limit_remaining", "usage")),
            )
            if "error" in parsed:
                raise LocalFailure("dedicated key lookup returned an error envelope")
            data = parsed["data"]
            if "limit_reset" not in data or data["limit_reset"] is not None:
                raise LocalFailure("dedicated key must explicitly have no limit reset")
            limit, remaining, usage = (
                Decimal(data[name]) for name in ("limit", "limit_remaining", "usage")
            )
            if (
                remaining <= 0
                or remaining < Decimal(required)
                or remaining > limit
                or usage > limit
            ):
                raise LocalFailure(
                    "dedicated key has insufficient or contradictory credit evidence"
                )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, DecimalException) as exc:
            raise LocalFailure(safe_text(str(exc))) from None
    return {name: data[name] for name in ("limit", "limit_remaining", "limit_reset", "usage")}


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
        "association": safe_evidence(association),
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
        evidence.update(model=data["model"], upstream=spec["upstream"], charge=data["total_cost"])
    except (ValueError, KeyError, TypeError) as exc:
        evidence["error"] = safe_text(str(exc))
        # Invalid evidence is still durable; Journal refuses settlement.
        journal.settle(attempt, safe_evidence(evidence))
        raise LocalFailure(evidence["error"]) from None
    journal.settle(attempt, safe_evidence(evidence))
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


class PinnedRouter:
    """Adapter router for one frozen, fully funded model campaign.

    ``binding.transports[alias]`` contains model, params, endpoint, common
    gateway generation_namespace and credential_fingerprint. Schedule rows
    also name contract/version/case_id/repetition/prompt_hash and the verified
    receipt-provider display mapping. Preparation and spending approval are
    the later runner's responsibility; this transport cannot derive a bound.
    """

    def __init__(self, campaign, *, alias, params, dispatch, transport=None, config=None):
        self.campaign, self.alias = campaign, alias
        self.config = config or routing()
        self.cfg = resolve(alias, self.config)
        self.params = load_json(canonical(validate_params(params)))
        if self.cfg.params:
            raise LocalFailure("implicit routing parameters are forbidden in qualification")
        self.transport, self.dispatch = transport, dispatch
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
        self.failure = LocalFailure(safe_text(str(error)))
        if case_id in self.served:
            self.campaign.fail(self.served[case_id], str(self.failure))

    def request(self, messages):
        body = self.params[route(self.contract)]
        policy = {
            "only": [self.params["upstream"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": self.params["max_price"],
        }
        return load_json(
            canonical(
                {
                    "model": self.cfg.model_id,
                    "messages": messages,
                    **body,
                    "provider": policy,
                    "usage": {"include": True},
                }
            )
        )

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
            failure = exc if isinstance(exc, LocalFailure) else LocalFailure(safe_text(str(exc)))
            self.local[case_id or "<unmatched>"] = str(failure)
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
        self.failure = LocalFailure(safe_text(str(error)))
        try:
            self.campaign.fail(attempt, str(self.failure) or type(error).__name__)
        except BaseException as append_error:
            # Preserve the original cancellation/call/cleanup exception. Journal
            # poisoning still prevents all subsequent reads and egress.
            error.add_note("failure evidence append failed: " + type(append_error).__name__)

    async def _send(self, attempt, expected):
        secret = dedicated_key()
        if hashlib.sha256(secret.encode()).hexdigest() != self.fingerprint:
            raise LocalFailure("dedicated credential differs from frozen identity")
        info = await key_status(secret, self.transport, required=str(self.required))
        row = self.campaign.attempt(attempt)
        if row.get("reserved_at") is None:
            self.campaign.reserve(attempt)
        self.campaign.append(
            "dispatch",
            attempt=attempt,
            request_hash=row["spec"]["request_hash"],
            key_status=info,
            credential_fingerprint=self.fingerprint,
        )
        self.required = total([self.required, Decimal(row["spec"]["max_charge"]).copy_negate()])

        def record(observation):
            spec = row["spec"]
            observed = {
                **observation,
                "model": observation["response_model"],
                "upstream": spec["upstream"]
                if observation["response_provider"] == spec["receipt_provider"]
                else observation["response_provider"],
            }
            self.campaign.observe(attempt, observed)
            if (
                observation["response_model"] != spec["model"]
                or observation["response_provider"] != spec["receipt_provider"]
                or observation["http_status"] != 200
                or observation["error"]
            ):
                raise LocalFailure("provider observation is not verified pinned-model evidence")

        handler = ObservedHTTPHandler(expected, secret, record, transport=self.transport)
        body = self.params[route(self.contract)]
        flat = {k: v for k, v in body.items() if k != "reasoning"}
        extra = {
            "provider": expected["provider"],
            **({"reasoning": body["reasoning"]} if "reasoning" in body else {}),
        }
        try:
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
