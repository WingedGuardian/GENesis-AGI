"""Calculated native token tariffs, distinct from provider monetary receipts."""

import re
from datetime import datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal, localcontext

from .accounting import currency, total
from .evidence import Incomplete, digest, load_json

BASIS = "native_tariff_v1"
ENDPOINT = "https://api.deepseek.com/chat/completions"
PRO = "deepseek-v4-pro"
PRO_KIND = "deepseek_native_pro_v1"
COUNTERS = {"prompt_tokens", "completion_tokens", "total_tokens",
            "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"}
RATES = ("prompt_cache_hit", "prompt_cache_miss", "completion")


def _closed(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Incomplete("invalid native expense schema")


def validate_spec(spec: dict) -> None:
    """Validate the frozen, self-contained native tariff authority."""
    model, kind = spec.get("model"), spec.get("transport_kind")
    legacy_flash = model == "deepseek-flash" and kind != PRO_KIND
    screen_pro = model == PRO and kind == PRO_KIND and spec.get("alias") == PRO
    if (spec.get("cost_basis") != BASIS or not (legacy_flash or screen_pro)
            or spec.get("upstream") != "deepseek" or spec.get("endpoint") != ENDPOINT
            or spec.get("generation_namespace") != "deepseek"):
        raise Incomplete("invalid native expense basis")
    tariff = spec.get("native_tariff")
    _closed(tariff, ("version", "currency", "unit_tokens", "provider", "model", "selection",
                     "rates", "pricing_evidence"))
    if (type(tariff["version"]) is not int or tariff["version"] != 1
            or type(tariff["unit_tokens"]) is not int or tariff["unit_tokens"] != 1000000
            or tariff["currency"] != "USD" or tariff["provider"] != "deepseek"
            or tariff["model"] != spec["model"] or tariff["selection"] != "published_tier_interval"):
        raise Incomplete("invalid native tariff authority")
    _closed(tariff["rates"], ("off_peak", "peak"))
    for rates in tariff["rates"].values():
        _closed(rates, RATES)
        if any(currency(rate) <= 0 for rate in rates.values()):
            raise Incomplete("invalid native tariff rate")
    if any(currency(tariff["rates"]["peak"][key]) < currency(tariff["rates"]["off_peak"][key])
           for key in RATES):
        raise Incomplete("inverted native tariff interval")
    evidence = tariff["pricing_evidence"]
    _closed(evidence, ("url", "sha256", "observed_at"))
    if (evidence["url"] != "https://api-docs.deepseek.com/quick_start/pricing/"
            or not isinstance(evidence["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", evidence["sha256"]) is None
            or not isinstance(evidence["observed_at"], str) or not evidence["observed_at"].strip()):
        raise Incomplete("invalid native pricing evidence")
    try:
        observed = datetime.fromisoformat(evidence["observed_at"])
    except ValueError:
        raise Incomplete("invalid native pricing timestamp") from None
    if observed.utcoffset() != timedelta(0):
        raise Incomplete("native pricing timestamp must be UTC")


def generation_claims(observation: dict) -> list:
    """Read retained native identities, including contradictory and malformed claims."""
    values = [observation.get(key) for key in (
        "generation_id", "body_generation_id", "header_generation_id", "response_generation_id")]
    headers = observation.get("response_headers")
    if isinstance(headers, dict):
        values.append(headers.get("x-generation-id"))
    try:
        raw = load_json(observation.get("raw_body", ""))
    except (ValueError, TypeError):
        return values
    if isinstance(raw, dict):
        values.append(raw.get("id"))
    return values


def _counts(usage):
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
                                           for key in COUNTERS):
        raise Incomplete("invalid native token counts")
    if (usage["prompt_tokens"] != usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
            or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]):
        raise Incomplete("inconsistent native token counts")
    return {key: usage[key] for key in COUNTERS}


def observation_interval(spec: dict, observation: dict) -> tuple[Decimal, Decimal]:
    """Derive unrounded tariff amounts from the actual native response usage."""
    validate_spec(spec)
    if (not isinstance(observation, dict) or type(observation.get("http_status")) is not int
            or observation["http_status"] != 200
            or any(observation.get(key) != spec[target] for key, target in (
                ("model", "model"), ("upstream", "upstream"), ("request_hash", "request_hash"),
                ("request_url", "endpoint"), ("response_url", "endpoint")))):
        raise Incomplete("invalid native financial identity")
    try:
        raw = load_json(observation.get("raw_body", ""))
    except (ValueError, TypeError):
        raise Incomplete("invalid native response JSON") from None
    generation = observation.get("generation_id")
    if (not isinstance(raw, dict) or "error" in raw or raw.get("object") != "chat.completion"
            or not isinstance(generation, str) or not generation.strip()
            or raw.get("id") != generation or raw.get("model") != spec["model"]):
        raise Incomplete("invalid native response identity")
    headers = observation.get("response_headers", {})
    if not isinstance(headers, dict):
        raise Incomplete("invalid native response headers")
    claims = [(observation.get(key), generation) for key in (
        "body_generation_id", "header_generation_id", "response_generation_id")]
    claims.extend(((observation.get("response_model"), spec["model"]),
                   (observation.get("response_provider"), spec["upstream"]),
                   (headers.get("x-generation-id"), generation)))
    if any(value is not None and (not isinstance(value, str) or value != expected)
           for value, expected in claims):
        raise Incomplete("contradictory native financial identity")
    normalized = observation.get("native_usage")
    _closed(normalized, COUNTERS)
    usage = raw.get("usage")
    counts = _counts(usage)
    if _counts(normalized) != counts:
        raise Incomplete("native usage changed")
    details = usage.get("prompt_tokens_details", {})
    if not isinstance(details, dict):
        raise Incomplete("invalid native cache details")
    if "cached_tokens" in details and (type(details["cached_tokens"]) is not int
                                       or details["cached_tokens"] != counts["prompt_cache_hit_tokens"]):
        raise Incomplete("inconsistent native cache details")
    completion = usage.get("completion_tokens_details", {})
    if not isinstance(completion, dict):
        raise Incomplete("invalid native completion details")
    for source in (usage, completion):
        for key in ("reasoning_tokens", "safety_tokens"):
            if key in source and (type(source[key]) is not int
                                  or not 0 <= source[key] <= counts["completion_tokens"]):
                raise Incomplete("invalid native completion breakdown")
    for source in (raw, usage):
        tools = source.get("server_tool_use", {})
        if not isinstance(tools, dict) or any(type(v) is not int or v != 0 for v in tools.values()):
            raise Incomplete("unsupported native tool expense")
    amounts = []
    for rates in spec["native_tariff"]["rates"].values():
        factors = [(counts[key + "_tokens"] if key != "completion" else counts["completion_tokens"],
                    currency(rates[key])) for key in RATES]
        with localcontext(Context(Emin=MIN_EMIN, Emax=MAX_EMAX)) as context:
            context.prec = max(28, max(len(str(n)) + len(rate.as_tuple().digits)
                                       + abs(rate.as_tuple().exponent) for n, rate in factors) + 8)
            numerator = total(Decimal(n) * rate for n, rate in factors)
            context.prec = max(context.prec, len(numerator.as_tuple().digits))
            amounts.append(numerator / Decimal(1000000))
    # Tier order is explicit; canonical JSON sorts object keys during journal replay.
    by_tier = dict(zip(spec["native_tariff"]["rates"], amounts, strict=True))
    return by_tier["off_peak"], by_tier["peak"]


def billing_evidence(spec: dict, observation: dict) -> dict:
    """Build a calculation binding; this is neither an invoice nor a GET receipt."""
    lower, upper = observation_interval(spec, observation)
    return {"cost_basis": BASIS, "attempt": spec["id"], "request_hash": spec["request_hash"],
            "generation_id": observation["generation_id"], "model": spec["model"],
            "upstream": spec["upstream"], "observation_sha256": digest(observation),
            "tariff_sha256": digest(spec["native_tariff"]),
            "calculated_lower": format(lower, "f"), "calculated_upper": format(upper, "f")}


def interval(row: dict) -> tuple[Decimal, Decimal] | None:
    """Read only complete, consistent native computations, without repairing events."""
    try:
        if (len(row["observations"]) != 1 or row.get("lookups", []) != [] or not row["receipts"]
                or not isinstance(row["receipts"], list)):
            return None
        expected = billing_evidence(row["spec"], row["observations"][0])
        if any(not isinstance(receipt, dict) or receipt != expected for receipt in row["receipts"]):
            return None
        return currency(expected["calculated_lower"]), currency(expected["calculated_upper"])
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return None
