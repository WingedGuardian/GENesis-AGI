"""A one-provider, no-fallback stand-in router for qualification.

Production ``Router`` writes cost rows and walks fallback chains, so it is
unusable here. ``PinnedRouter`` resolves one routing alias, pins one OpenRouter
upstream with ``provider.only``, ``allow_fallbacks: false``,
``require_parameters: true`` and an OpenRouter-enforced ``max_price``, and sends
each request once through the production ``LiteLLMDelegate``. It answers from
the campaign's paid answers first, so a crash or a rescore never pays twice.

Spend is bounded twice: by the credit limit on a DEDICATED OpenRouter key
(checked with ``GET /api/v1/key`` before the first request), and by a request
cap derived from the campaign's own dispatch lines.
"""

from __future__ import annotations

import logging
import os
import traceback
from contextlib import contextmanager
from pathlib import Path

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from genesis.eval.qualification.corpus import route, versions
from genesis.eval.qualification.evidence import Incomplete, digest, load_json
from genesis.routing.config import load_config_from_string
from genesis.routing.litellm_delegate import LiteLLMDelegate
from genesis.routing.types import RoutingResult

ENDPOINT = "https://openrouter.ai/api/v1"
KEY_ENV = "GENESIS_QUALIFICATION_OPENROUTER_KEY"
PRODUCTION_KEY_NAMES = ("API_KEY_OPENROUTER", "OPENROUTER_API_KEY", "OPENROUTER_API_TOKEN")
REPETITIONS = (1, 2, 3)
# Each case and repetition may be sent once more after an interrupted request.
ALLOWED_RESENDS = 1
ROUTES = ("judge", "relevance", "novelty")
ROUTE_KEYS = {"temperature", "max_tokens", "top_p", "seed", "reasoning"}
ANSWER_KEY = ("contract", "version", "case_id", "repetition", "prompt_hash")
ROOT = Path(__file__).resolve().parents[4]


class LocalFailure(Incomplete):
    """The harness failed, not the model. Nothing is scored; a rerun may resend."""


def request_cap(cases: int) -> int:
    return cases * len(REPETITIONS) * (1 + ALLOWED_RESENDS)


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
    return type(value) in (int, float) and value >= low and (high is None or value <= high)


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
        if not isinstance(reasoning, dict) or (
            "max_tokens" in reasoning
            and (type(reasoning["max_tokens"]) is not int or reasoning["max_tokens"] < 0)
        ):
            raise Incomplete(f"{name}: invalid reasoning")
    return params


def safe_text(value: str) -> str:
    """Never persist an environment credential that a response or error echoes."""
    secrets = {
        secret
        for name, secret in os.environ.items()
        if secret
        and len(secret) >= 8
        and any(word in name.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    # Longest first: a prefix replacement must not break a longer match.
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


def safe_evidence(value):
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(k): safe_evidence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_evidence(v) for v in value]
    return value


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
    logging.setLogRecordFactory(redacted)
    litellm.suppress_debug_info = True
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)
        litellm.suppress_debug_info = quiet


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


async def key_status(key: str, transport=None) -> dict:
    with private_logs():
        try:
            async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
                response = await client.get(
                    ENDPOINT + "/key", headers={"Authorization": f"Bearer {key}"}
                )
            data = load_json(response.content).get("data") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise LocalFailure("could not read the dedicated key's limit") from exc
    if not isinstance(data, dict):
        raise LocalFailure("could not read the dedicated key's limit")
    if not _number(data.get("limit"), 0):
        raise LocalFailure("the dedicated key has no credit limit; set one before a run")
    if not _number(data.get("limit_remaining"), 0) or data["limit_remaining"] <= 0:
        raise LocalFailure("the dedicated key's credit limit is exhausted")
    # A resetting limit bounds each period, not the run: the request cap still applies.
    return {k: data.get(k) for k in ("limit", "limit_remaining", "limit_reset", "usage")}


class ObservedHTTPHandler(AsyncHTTPHandler):
    """Check the serialized request before egress; read the response before LiteLLM."""

    def __init__(self, expected: dict, key: str, *, transport=None):
        self.expected, self.key = expected, key
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
            # LiteLLM's client retries a dropped connection once; that would pay twice.
            self.error = "a repeated POST was refused"
            raise LocalFailure(self.error)
        body = load_json(await request.aread())
        if (
            request.method != "POST"
            or str(request.url) != ENDPOINT + "/chat/completions"
            or request.headers.get("Authorization") != f"Bearer {self.key}"
            or body != self.expected
        ):
            self.error = "serialized request differs from the pinned request"
            raise LocalFailure(self.error)

    async def after(self, response: httpx.Response):
        try:
            raw = load_json(await response.aread())
        except ValueError:
            raw = None
        if not isinstance(raw, dict):
            # A garbled body is a transport fault, not the model's answer.
            self.error = "unreadable provider response"
            raw = {}
        choices = raw.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
        finish = choice.get("finish_reason")
        if raw and ("error" in raw or not choices or finish == "error"):
            # An error object (even under HTTP 200) is not the model's verdict.
            self.error = "provider returned an error instead of an answer"
        self.observed = safe_evidence(
            {
                "http_status": response.status_code,
                "finish_reason": finish,
                "response_model": raw.get("model"),
                "response_provider": raw.get("provider"),
                "generation_id": raw.get("id"),
                "usage": {
                    k: usage[k]
                    for k in ("prompt_tokens", "completion_tokens", "cost")
                    if k in usage
                },
                "content": content if isinstance(content, str) else None,
            }
        )


class PinnedRouter:
    """Duck-types ``Router.route_call`` for ``LLMJudgeScorer`` and the J9/novelty paths.

    ``bind`` names the contract and repetition before each pass. Rubric passes
    pass ``prompts`` (prompt hash -> case id) because ``run_calibration`` does
    not tell the router which case it is grading.
    """

    def __init__(self, campaign, *, alias, params, cap, dispatch, transport=None, config=None):
        self.campaign = campaign
        self.config = config or routing()
        self.cfg = resolve(alias, self.config)
        self.params = validate_params(params)
        self.scope = {"alias": alias, "model": self.cfg.model_id, "params": digest(self.params)}
        self.cap, self.dispatch, self.transport = cap, dispatch, transport
        self.answers = {
            self._key(line): line
            for line in campaign.lines
            if line["kind"] == "answer" and self.in_scope(line)
        }
        self.dispatched = sum(
            line["kind"] == "dispatch" and self.in_scope(line) for line in campaign.lines
        )
        self.failure: LocalFailure | None = None
        self.key = self.key_info = None
        self.local: dict[str, str] = {}
        self.served: set[str] = set()

    def in_scope(self, line) -> bool:
        return all(line.get(k) == v for k, v in self.scope.items())

    @staticmethod
    def _key(line):
        return tuple(line.get(k) for k in ANSWER_KEY)

    def bind(self, contract, repetition, *, case_id=None, prompts=None):
        self.contract, self.repetition = contract, repetition
        self.case_id, self.prompts = case_id, prompts or {}
        self.calls = []
        self.local = {}
        self.served = set()

    async def route_call(self, call_site_id, messages, **kwargs):
        prompt_hash = digest(messages)
        case_id = self.case_id or self.prompts.get(prompt_hash, "<unmatched prompt>")
        try:
            if case_id == "<unmatched prompt>":
                raise LocalFailure("prompt matches no corpus case")
            line = await self._answer(messages, prompt_hash, case_id, kwargs)
        except LocalFailure as exc:
            self.local[case_id] = str(exc)
            raise
        except Exception as exc:
            # The callers swallow exceptions; never let one read as a model error.
            # Callers discard the exception, so its (redacted) message travels here.
            detail = safe_text(str(exc))
            failure = LocalFailure(f"harness error: {type(exc).__name__}: {detail}")
            self.failure = self.failure or failure
            self.local[case_id] = str(failure)
            raise failure from exc
        self.served.add(case_id)
        self.calls.append((messages, line["content"]))
        return RoutingResult(
            success=True,
            call_site_id=call_site_id,
            provider_used=self.scope["alias"],
            model_id=line["response_model"],
            content=line["content"],
            attempts=1,
        )

    async def _answer(self, messages, prompt_hash, case_id, kwargs):
        if kwargs.pop("chain_offset", 0):
            raise LocalFailure("qualification prohibits chain rotation")
        configured = self.params[route(self.contract)]
        for name, value in kwargs.items():
            if name not in ROUTE_KEYS or configured.get(name, value) != value:
                raise LocalFailure(f"call-site parameter {name!r} contradicts the params file")
        key = (self.contract, versions()[self.contract], case_id, self.repetition, prompt_hash)
        line = self.answers.get(key)
        fresh = line is None
        if fresh:
            if not self.dispatch:
                raise LocalFailure("unanswered")
            if self.failure is not None:
                raise self.failure
            try:
                line = await self._send(messages, {**configured, **kwargs}, key)
            except LocalFailure as exc:
                self.failure = exc
                raise
        served = (line["response_model"], line["response_provider"])
        if served[0] != self.cfg.model_id or served[1] not in (None, self.params["upstream"]):
            # Paid and kept, but not evidence about the pinned model; stop sending.
            exc = LocalFailure("the served model or upstream differs from the pinned identity")
            if fresh:
                self.failure = exc
            raise exc
        return line

    async def _send(self, messages, body, key):
        if self.dispatched >= self.cap:
            raise LocalFailure(f"request cap of {self.cap} reached")
        secret = await self._credential()
        fields = dict(zip(ANSWER_KEY, key, strict=True))
        line_fields = {**self.scope, **fields, "upstream": self.params["upstream"]}
        policy = {
            "only": [self.params["upstream"]],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": self.params["max_price"],
        }
        flat = {k: v for k, v in body.items() if k != "reasoning"}
        extra = {
            "provider": policy,
            **({"reasoning": body["reasoning"]} if "reasoning" in body else {}),
        }
        # OpenRouter's LiteLLM transform adds usage accounting to every request.
        expected = {"model": self.cfg.model_id, "messages": messages, **flat, **extra}
        expected["usage"] = {"include": True}
        handler = ObservedHTTPHandler(expected, secret, transport=self.transport)
        self.campaign.append("dispatch", **line_fields)
        self.dispatched += 1
        try:
            with private_logs(), scoped_credential(secret):
                await LiteLLMDelegate(self.config).call(
                    self.scope["alias"],
                    self.cfg.model_id,
                    messages,
                    client=handler,
                    num_retries=0,
                    max_retries=0,
                    success_callback=[],
                    failure_callback=[],
                    caching=False,
                    extra_body=extra,
                    **flat,
                )
        except BaseException as exc:
            self._settle(handler, line_fields, key, error=type(exc).__name__)
            if not isinstance(exc, Exception):
                raise  # cancellation and interpreter exits keep their meaning
            raise LocalFailure("the request raised before it settled") from exc
        finally:
            await handler.close()
        line = self._settle(handler, line_fields, key)
        if line is None:
            raise LocalFailure(f"no answer was observed (HTTP {self._status(handler)})")
        return line

    @staticmethod
    def _status(handler):
        return handler.observed["http_status"] if handler.observed else None

    def _settle(self, handler, fields, key, *, error=None):
        """Keep any answer the provider returned, even if the call raised after it."""
        if not handler.error and self._status(handler) == 200:
            line = self.campaign.append("answer", **fields, **handler.observed)
            self.answers[key] = line
            return line
        reason = handler.error or error or "no_answer"
        self.campaign.append("failure", **fields, error=reason, status=self._status(handler))
        return None

    async def _credential(self) -> str:
        if self.key is None:
            check_callback_isolation()
            key = os.environ.get(KEY_ENV, "")
            if not key:
                raise LocalFailure(f"{KEY_ENV} must hold a dedicated credit-limited key")
            if key in production_credentials():
                raise LocalFailure("refusing the production OpenRouter key: no limit bounds it")
            self.key_info = await key_status(key, self.transport)
            self.key = key
        return self.key
