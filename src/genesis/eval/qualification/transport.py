"""Observe the existing delegate's HTTP path without retries or global callbacks."""

from __future__ import annotations

import logging
import os
import traceback
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal

import httpx
import litellm
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

from genesis.eval.qualification.evidence import Incomplete, digest, load_json, money
from genesis.routing.litellm_delegate import LiteLLMDelegate, _resolve_api_key
from genesis.routing.types import RoutingResult

ENDPOINT = "https://openrouter.ai/api/v1"


def qualification_key():
    """Use the production delegate's credential precedence on both network paths."""
    key = _resolve_api_key("openrouter")
    if not key:
        raise Incomplete("an OpenRouter environment credential is required")
    return key


def check_callback_isolation():
    if any(
        getattr(litellm, name, [])
        for name in (
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
    ):
        raise Incomplete("global LiteLLM observers are active; use a fresh isolated CLI process")


def safe_text(value: str) -> str:
    """Do not persist credentials if an error/response echoes an environment key."""
    secrets = {
        secret
        for key, secret in os.environ.items()
        if secret
        and len(secret) >= 8
        and any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
    }
    # Match the whole longest credential before its prefix can destroy the match.
    for secret in sorted(secrets, key=len, reverse=True):
        value = value.replace(secret, "[REDACTED]")
    return value


@contextmanager
def private_logs():
    """Contain credential echoes in fully formatted SDK/delegate log records.

    This CLI is isolated and sequential. Restore the process logging factory on
    every exit; no production logger, handler, or callback is reconfigured.
    """
    original = logging.getLogRecordFactory()

    def redacted_record(*args, **kwargs):
        record = original(*args, **kwargs)
        record.msg = safe_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = safe_text("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = safe_text(record.stack_info)
        return record

    logging.setLogRecordFactory(redacted_record)
    try:
        yield
    finally:
        logging.setLogRecordFactory(original)


def safe_evidence(value):
    """Redact every retained provider string, including object keys and metadata."""
    if isinstance(value, str):
        return safe_text(value)
    if isinstance(value, dict):
        return {safe_text(k): safe_evidence(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe_evidence(v) for v in value]
    return value


def tokens(usage: dict):
    if not isinstance(usage, dict):
        raise Incomplete("missing provider usage")
    for key in ("prompt_tokens", "completion_tokens"):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise Incomplete("invalid provider token usage")


def validate_billing(
    manifest: dict, observation: dict, billing: dict, *, previous_bills=()
) -> Decimal:
    if observation.get("model") != manifest["model_id"]:
        raise Incomplete("missing or mismatched response model")
    generation = observation.get("generation_id")
    if not isinstance(generation, str) or not generation.strip() or "[REDACTED]" in generation:
        raise Incomplete("missing or redacted generation identity")
    tokens(observation.get("usage"))
    if observation.get("request_url") != manifest["endpoint"] + "/chat/completions":
        raise Incomplete("mismatched provider endpoint")
    task = manifest["schedule"][observation["attempt"]]
    expected = {"model": manifest["model_id"], "messages": task["messages"], **task["parameters"]}
    if observation.get("request_hash") != digest(expected):
        raise Incomplete("serialized request differs from frozen configuration")
    if (
        not isinstance(billing, dict)
        or billing.get("id") != generation
        or billing.get("model") != manifest["model_id"]
    ):
        raise Incomplete("billing identity mismatch")
    if billing.get("source") not in ("response.usage.cost", "openrouter.generation"):
        raise Incomplete("unexplained billing source")
    charge = money(billing.get("total_cost"))
    if billing["source"] == "response.usage.cost":
        if charge != money(observation["usage"].get("cost")):
            raise Incomplete("billing differs from observed response")
    elif observation["usage"].get("cost") is not None and charge != money(
        observation["usage"]["cost"]
    ):
        raise Incomplete("conflicting billing evidence")
    for previous in previous_bills:
        if (
            not isinstance(previous, dict)
            or previous.get("source") != "openrouter.generation"
            or previous.get("id") != generation
            or previous.get("model") != manifest["model_id"]
        ):
            continue
        try:
            prior_charge = money(previous.get("total_cost"))
        except Incomplete:
            continue  # An invalid/missing bill cannot establish a known charge.
        if prior_charge != charge:
            raise Incomplete("contradictory known generation charges; reservation retained")
    pricing = manifest["pricing"]
    if observation["usage"]["prompt_tokens"] > pricing["max_input_tokens"]:
        raise Incomplete("input usage exceeded verified bound")
    if observation["usage"]["completion_tokens"] > task["parameters"]["max_tokens"]:
        raise Incomplete("output usage exceeded verified bound")
    return charge


class ObservedHTTPHandler(AsyncHTTPHandler):
    """Per-call httpx hooks are awaited by the delegate's existing transport.

    Verify the serialized body before egress. Read and journal the response
    before returning it to LiteLLM: no queued logging callback can race settlement.
    """

    def __init__(self, journal, attempt: str, *, transport=None):
        self.journal = journal
        self.attempt = attempt
        self.posts = 0
        self.request_hash = None
        self.observed = None
        self.observer_error = None
        self._transport = transport
        super().__init__()

    def create_client(self, **kwargs):
        return httpx.AsyncClient(
            transport=self._transport,
            follow_redirects=False,
            event_hooks={"request": [self.before], "response": [self.after]},
        )

    async def before(self, request: httpx.Request):
        from genesis.eval.qualification.manifest import pricing_issues

        manifest = self.journal.manifest
        issues = pricing_issues(manifest["pricing"], manifest["parameters"], manifest["model_id"])
        if issues:
            raise Incomplete("; ".join(issues))
        self.posts += 1
        task = self.journal.manifest["schedule"][self.attempt]
        expected = {
            "model": self.journal.manifest["model_id"],
            "messages": task["messages"],
            **task["parameters"],
        }
        if request.headers.get("Authorization") != f"Bearer {qualification_key()}":
            raise Incomplete("serialized credential differs from selected provider account")
        body = load_json(await request.aread())
        url = self.journal.manifest["endpoint"] + "/chat/completions"
        if (
            self.posts != 1
            or request.method != "POST"
            or str(request.url) != url
            or body != expected
        ):
            self.observer_error = "serialized_request_mismatch_or_repeated_attempt"
            raise Incomplete(self.observer_error)
        self.request_hash = digest(body)

    async def after(self, response: httpx.Response):
        try:
            raw = load_json(await response.aread(), exact_numbers=True)
            if not isinstance(raw, dict):
                raise Incomplete("nonobject provider response")
            usage = raw.get("usage")
            usage = (
                {
                    key: usage[key]
                    for key in (
                        "prompt_tokens",
                        "completion_tokens",
                        "total_tokens",
                        "cost",
                        "cost_details",
                    )
                    if key in usage
                }
                if isinstance(usage, dict)
                else None
            )
            choices = raw.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            message = choice.get("message") if isinstance(choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            self.observed = safe_evidence(
                {
                    "attempt": self.attempt,
                    "request_hash": self.request_hash,
                    "request_url": str(response.request.url),
                    "http_status": response.status_code,
                    "model": raw.get("model"),
                    "generation_id": raw.get("id"),
                    "usage": usage,
                    "content": content if isinstance(content, str) else None,
                }
            )
            self.journal.append("observation", **self.observed)
        except Exception as exc:
            self.observer_error = type(exc).__name__
            raise


class QualificationRouter:
    """Router-compatible single attempt through LiteLLMDelegate, for one case."""

    def __init__(self, journal, attempt, config, *, transport=None):
        self.journal = journal
        self.attempt = attempt
        self.config = config
        self.transport = transport
        self.called = False

    async def route_call(self, call_site_id, messages, **kwargs):
        check_callback_isolation()
        qualification_key()
        # Router-only controls never enter the delegate or provider payload.
        if kwargs.pop("chain_offset", 0) != 0 or self.called:
            raise Incomplete("qualification prohibits rotation and repeated calls")
        task = self.journal.manifest["schedule"][self.attempt]
        if call_site_id != task["call_site"] or messages != task["messages"]:
            raise Incomplete("contract request differs from frozen prompt")
        if any(task["parameters"].get(k) != v for k, v in kwargs.items()):
            raise Incomplete("contract parameters differ from frozen parameters")
        self.called = True
        self.journal.append("dispatch", attempt=self.attempt)
        handler = ObservedHTTPHandler(self.journal, self.attempt, transport=self.transport)
        provider = self.journal.manifest["provider"]
        model = self.journal.manifest["model_id"]
        try:
            with private_logs():
                result = await LiteLLMDelegate(self.config).call(
                    provider,
                    model,
                    messages,
                    **deepcopy(task["delegate_parameters"]),
                    client=handler,
                    num_retries=0,
                    max_retries=0,
                    success_callback=[],
                    failure_callback=[],
                    caching=False,
                )
            if not result.success or handler.observer_error:
                self.journal.append(
                    "failure",
                    attempt=self.attempt,
                    error=handler.observer_error or "delegate_failure",
                    status_code=result.status_code,
                )
            observation = handler.observed
            if observation is None:
                raise Incomplete("provider response was not observed; reservation retained")
            billing = {
                "source": "response.usage.cost",
                "id": observation["generation_id"],
                "model": observation["model"],
                "total_cost": (observation["usage"] or {}).get("cost"),
            }
            self.journal.append("settle", attempt=self.attempt, billing=billing)
            return RoutingResult(
                success=result.success,
                content=observation["content"],
                call_site_id=call_site_id,
                model_id=observation["model"],
                provider_used=provider,
                attempts=1,
                error="delegate_failure" if not result.success else None,
            )
        except BaseException as exc:
            if (
                self.journal._file is not None
                and "failure" not in self.journal.attempts[self.attempt]
            ):
                self.journal.append("failure", attempt=self.attempt, error=type(exc).__name__)
            raise
        finally:
            await handler.close()


async def reconcile(journal, *, transport=None):
    """GET billing for already observed IDs; never submit another completion."""
    pending = [
        (attempt, record["observation"])
        for attempt, record in journal.attempts.items()
        if "charge" not in record and record.get("observation", {}).get("generation_id")
    ]
    if not pending:
        return
    if any(
        not isinstance(observed["generation_id"], str) or not observed["generation_id"].strip()
        for _, observed in pending
    ):
        raise Incomplete("invalid generation identity; reservation retained")
    if any("[REDACTED]" in observed["generation_id"] for _, observed in pending):
        raise Incomplete("redacted generation identity; reservation retained")
    key = qualification_key()
    with private_logs():
        async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
            for attempt, observed in pending:
                try:
                    response = await client.get(
                        ENDPOINT + "/generation",
                        params={"id": observed["generation_id"]},
                        headers={"Authorization": f"Bearer {key}"},
                    )
                except httpx.HTTPError as exc:
                    raise Incomplete(
                        "generation billing request failed; reservation retained"
                    ) from exc
                if response.status_code != 200:
                    raise Incomplete("generation billing unavailable; reservation retained")
                raw = load_json(response.content, exact_numbers=True)
                if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
                    raise Incomplete("invalid generation billing response; reservation retained")
                data = raw["data"]
                billing = safe_evidence(
                    {
                        "source": "openrouter.generation",
                        "id": data.get("id"),
                        "model": data.get("model"),
                        "total_cost": data.get("total_cost"),
                    }
                )
                journal.append("billing", attempt=attempt, billing=billing)
                journal.append("settle", attempt=attempt, billing=billing)
