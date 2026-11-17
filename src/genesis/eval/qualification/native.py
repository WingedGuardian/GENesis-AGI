"""First-party DeepSeek qualification profile; native usage is not an invoice."""

import os
import re

from . import native_expense, pinned
from .corpus import route
from .evidence import Incomplete, canonical, digest, load_json

ALIAS = MODEL = "deepseek-flash"
KIND = "deepseek_native_v1"
ENDPOINT = "https://api.deepseek.com"
COMPLETION_URL = ENDPOINT + "/chat/completions"
NAMESPACE = "deepseek"
KEY_ENV = "GENESIS_QUALIFICATION_DEEPSEEK_KEY"
PRODUCTION_KEY_NAMES = ("API_KEY_DEEPSEEK", "DEEPSEEK_API_KEY", "DEEPSEEK_API_TOKEN")
ROUTE_KEYS = {"temperature", "top_p", "max_tokens", "thinking"}
FORBIDDEN_IDENTITY = {"backend_model", "receipt_model", "receipt_provider", "receipt_policy"}


def resolve(alias, config=None):
    cfg = (config or pinned.routing()).providers.get(alias)
    if (alias != ALIAS or cfg is None or cfg.provider_type != "deepseek"
            or cfg.model_id != MODEL or cfg.base_url not in (None, ENDPOINT) or cfg.params):
        raise Incomplete("native qualification requires the explicit first-party DeepSeek alias")
    return cfg


def validate_params(params):
    """Freeze the measured non-thinking wire, with no implicit SDK controls."""
    if (not isinstance(params, dict) or set(params) != {"upstream", *pinned.ROUTES}
            or params["upstream"] != NAMESPACE):
        raise Incomplete("invalid native qualification parameters")
    for role in pinned.ROUTES:
        body = params[role]
        cap = 150 if role == "relevance" else 1000
        if (not isinstance(body, dict) or set(body) != ROUTE_KEYS
                or type(body["max_tokens"]) is not int or body["max_tokens"] != cap
                or type(body["temperature"]) not in (int, float) or body["temperature"] != 0
                or type(body["top_p"]) not in (int, float) or body["top_p"] != 1
                or body["thinking"] != {"type": "disabled"}):
            raise Incomplete(f"{role}: native parameters differ from the measured wire")
    return {**params, **{role: {**params[role], "temperature": 0.0, "top_p": 1.0}
                        for role in pinned.ROUTES}}


def request_body(model, params, contract, messages):
    validate_params(params)
    if model != MODEL:
        raise Incomplete("invalid native request model")
    # Normalize both delegate controls and the expected wire explicitly.
    return load_json(canonical({"model": model, "messages": messages, **params[route(contract)],
                                "temperature": 0.0, "top_p": 1.0}))


def validate_binding(binding, cfg, params):
    """Validate native transport identity without fabricating gateway fields."""
    expected = {"transport_kind": KIND, "model": cfg.model_id, "params": validate_params(params),
                "endpoint": ENDPOINT, "generation_namespace": NAMESPACE,
                "credential_mode": "shared_existing", "cost_basis": native_expense.BASIS}
    if (not isinstance(binding, dict) or FORBIDDEN_IDENTITY & binding.keys()
            or any(canonical(binding.get(key)) != canonical(value) for key, value in expected.items())
            or not isinstance(binding.get("provider_identity_evidence"), str)
            or not binding["provider_identity_evidence"].strip()
            or not isinstance(binding.get("credential_fingerprint"), str)
            or re.fullmatch(r"[a-f0-9]{64}", binding["credential_fingerprint"]) is None):
        raise Incomplete("native transport differs from the frozen campaign binding")
    # Local schema adaptation only: the binding stores the API base; expense
    # authority validates a scheduled completion URL. Never persist a fake wire.
    native_expense.validate_spec({**binding, "upstream": NAMESPACE, "endpoint": COMPLETION_URL})


def validate_attempt(spec, binding):
    native_expense.validate_spec(spec)
    expected = {"alias": ALIAS, "transport_kind": KIND, "generation_namespace": NAMESPACE,
                **{key: binding[key] for key in (
                    "model", "credential_mode", "credential_fingerprint",
                    "provider_identity_evidence", "cost_basis", "native_tariff")}}
    if (FORBIDDEN_IDENTITY & spec.keys()
            or any(canonical(spec.get(key)) != canonical(value) for key, value in expected.items())):
        raise Incomplete("schedule differs from the frozen native transport identity")


def validate_study(plan):
    if (plan["max_total_replacements"] != 0 or plan["max_new_attempts"] != 2250
            or plan["histories"] or plan["expense_evidence"] or plan["reuse"] != {ALIAS: {}}):
        raise Incomplete("native_study_requires_fresh_population_without_replacements")
    if len(plan["models"][ALIAS]["manifest"]["attempts"]) != 2250:
        raise Incomplete("native_study_requires_full_population")


def credential(mode="shared_existing"):
    # Globals may already be active; per-call callbacks=[] cannot isolate them.
    pinned.check_callback_isolation()
    if mode != "shared_existing":
        raise pinned.LocalFailure("native qualification requires shared_existing credential mode")
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise pinned.LocalFailure(f"{KEY_ENV} must hold the approved existing key")
    return key


def scoped_credential(key):
    return pinned.scoped_credential(key, aliases=PRODUCTION_KEY_NAMES, target="DEEPSEEK_API_KEY")


def delegate_kwargs(params, contract, expected):
    return {"api_base": ENDPOINT, "extra_body": {"thinking": params[route(contract)]["thinking"]},
            **{key: value for key, value in params[route(contract)].items() if key != "thinking"},
            "temperature": 0.0, "top_p": 1.0}


def observed_identity(observation, spec):
    return {}  # Native parser already derives identity from the actual response and URLs.


def _verify_controls(raw, spec):
    """Reject reported features outside the frozen non-thinking token-only wire."""
    usage = raw["usage"]  # Financial validation precedes this check in both callers.
    cap = 150 if route(spec["contract"]) == "relevance" else 1000
    if (usage["completion_tokens"] > cap
            or any(source.get("reasoning_tokens", 0) != 0 or "safety_tokens" in source
                   for source in (usage, usage.get("completion_tokens_details", {})))):
        raise pinned.LocalFailure("native response contradicts frozen controls")
    choices = raw.get("choices")
    for choice in choices if isinstance(choices, list) else ():
        message = choice.get("message") if isinstance(choice, dict) else None
        if isinstance(message, dict) and (
            message.get("reasoning_content") not in (None, "") or "title" in message
            or any(message.get(key) not in (None, [])
                   for key in ("tool_calls", "server_tool_calls", "annotations"))
        ):
            raise pinned.LocalFailure("native response contradicts frozen controls")


def verify_observation(observation, spec):
    """Verify actual native origin and answer identity, without a weights claim."""
    native_expense.observation_interval(spec, observation)
    raw = pinned.external_json(observation["raw_body"])
    _verify_controls(raw, spec)
    choices = raw.get("choices")
    choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else {}
    choice = choice if isinstance(choice, dict) else {}
    message = choice.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    fingerprint = raw.get("system_fingerprint")
    headers = observation.get("response_headers")
    if not isinstance(headers, dict):
        raise pinned.LocalFailure("invalid native response headers")
    header_id = headers.get("x-generation-id")
    if (observation.get("error") or observation.get("response_model") != raw.get("model")
            or observation.get("body_generation_id") != raw.get("id")
            or (header_id is not None and header_id != raw.get("id"))
            or observation.get("system_fingerprint") != fingerprint
            or (fingerprint is not None and not isinstance(fingerprint, str))
            or not isinstance(content, str) or observation.get("content") != content
            or message.get("refusal") not in (None, "")
            or message.get("role") != "assistant"
            or type(choice.get("index")) is not int or choice["index"] != 0
            or choice.get("finish_reason") not in ("stop", "length")
            or observation.get("finish_reason") != choice.get("finish_reason")):
        raise pinned.LocalFailure("native response is not verified answer evidence")


class HTTPHandler(pinned.ObservedHTTPHandler):
    """Reuse the one-POST observer/close lifecycle with native response parsing."""

    completion_url = COMPLETION_URL
    request_headers = {}

    def record_response(self, response, data):
        try:
            raw = pinned.external_json(data)
        except (ValueError, UnicodeError):
            raw = {}
            self.error = "invalid native response JSON"
        raw = raw if isinstance(raw, dict) else {}
        choices = raw.get("choices")
        choice = choices[0] if isinstance(choices, list) and len(choices) == 1 else {}
        choice = choice if isinstance(choice, dict) else {}
        message = choice.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if (response.status_code != 200 or raw.get("error") or not isinstance(content, str)
                or choice.get("finish_reason") not in ("stop", "length")):
            self.error = self.error or "native response has no usable answer"
        usage = raw.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        request_url, response_url = str(response.request.url), str(response.url)
        self.observed = pinned.safe_evidence({
            "http_status": response.status_code, "request_url": request_url,
            "response_url": response_url,
            "model": raw.get("model"), "response_model": raw.get("model"),
            "upstream": NAMESPACE if request_url == response_url == COMPLETION_URL else None,
            "generation_id": raw.get("id"), "body_generation_id": raw.get("id"),
            "header_generation_id": response.headers.get("x-generation-id"),
            "response_headers": {key: response.headers[key] for key in ("x-generation-id",)
                                 if key in response.headers},
            "system_fingerprint": raw.get("system_fingerprint"),
            "raw_body": pinned.safe_body(data),
            "native_usage": {key: usage[key] for key in native_expense.COUNTERS if key in usage},
            "finish_reason": choice.get("finish_reason"), "content": content,
            "error": self.error,
        })
        # Local association is trusted; a numeric key inside its hash must not
        # corrupt the request binding. URLs are restored only after exact match.
        self.observed["request_hash"] = digest(self.expected)
        for field, actual in (("request_url", request_url), ("response_url", response_url)):
            if actual == COMPLETION_URL:
                self.observed[field] = COMPLETION_URL
        self.record(self.observed)


async def finalize(journal, attempt, *, transport=None, credential_mode="shared_existing"):
    """Append calculation evidence only; never make an account/generation GET."""
    row = journal.attempt(attempt)
    if len(row["observations"]) != 1 or row.get("lookups"):
        raise pinned.LocalFailure("native calculation requires one observed completion")
    evidence = native_expense.billing_evidence(row["spec"], row["observations"][0])
    _verify_controls(pinned.external_json(row["observations"][0]["raw_body"]), row["spec"])
    receipts = row.get("receipts", [])
    if not isinstance(receipts, list) or any(receipt != evidence for receipt in receipts):
        raise pinned.LocalFailure("native calculation contradicts retained billing evidence")
    if receipts:
        return evidence
    journal.settle(attempt, evidence)
    return evidence


finalize_on_failure = True
