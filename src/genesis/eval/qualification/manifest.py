"""Freeze the corpus, source contracts, intended production payload and cost bound."""

from __future__ import annotations

import hashlib
import subprocess
import unicodedata
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path

from genesis.eval.qualification.contracts import (
    NOVELTY,
    RELEVANCE,
    Sandbox,
    coverage,
    render,
    validate_cases,
    versions,
)
from genesis.eval.qualification.evidence import (
    DEFAULT_CEILING,
    Incomplete,
    currency_sum,
    digest,
    money,
    token_charge,
)
from genesis.eval.qualification.transport import ENDPOINT
from genesis.routing.config import load_config_from_string

ROOT = Path(__file__).resolve().parents[4]
SUPPORTED_CANDIDATES = {
    "openrouter-mimo": "xiaomi/mimo-v2.6-pro",
    "openrouter-deepseek-flash": "deepseek/deepseek-v4.1-flash",
}
SOURCE_FILES = (
    "src/genesis/eval/scorers.py",
    "src/genesis/eval/calibration.py",
    "src/genesis/eval/j9_batch.py",
    "src/genesis/eval/j9_aggregator.py",
    "src/genesis/learning/procedural/extractor.py",
    "src/genesis/learning/procedural/judge.py",
    "src/genesis/learning/procedural/operations.py",
    "src/genesis/learning/procedural/embedding.py",
    "src/genesis/learning/procedural/scoping.py",
    "src/genesis/learning/procedural/validation_gate.py",
    "src/genesis/db/crud/procedural.py",
    "src/genesis/db/connection.py",
    "src/genesis/db/admission.py",
    "src/genesis/db/integrity.py",
    "src/genesis/routing/litellm_delegate.py",
    "config/model_routing.yaml",
)


def source_identity(root: Path) -> dict:
    paths = [
        *SOURCE_FILES,
        *[
            str(p.relative_to(root))
            for folder in (
                "src/genesis/eval/rubrics",
                "src/genesis/eval/qualification",
                "src/genesis/db/schema",
            )
            for p in (root / folder).glob("*.py")
        ],
    ]
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.PIPE
        ).strip()
    except subprocess.CalledProcessError as exc:
        raise Incomplete("source commit identity unavailable") from exc
    return {
        "commit": commit,
        "files": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sorted(paths)},
    }


def routing(root=ROOT):
    # Read the shipped configuration without credentials or local overlays.
    return load_config_from_string(
        (root / "config/model_routing.yaml").read_text(), check_api_keys=False
    )


def candidate_config(provider, config):
    """Bind a new qualification candidate to its exact shipped provider identity."""
    if not isinstance(provider, str) or provider not in SUPPORTED_CANDIDATES:
        raise Incomplete("unsupported qualification candidate")
    cfg = config.providers.get(provider)
    if cfg is None or (
        cfg.name != provider
        or cfg.provider_type != "openrouter"
        or cfg.model_id != SUPPORTED_CANDIDATES[provider]
        or cfg.base_url not in (None, ENDPOINT)
    ):
        raise Incomplete("unexpected qualification candidate configuration")
    return cfg


def validate_numeric_parameters(body: dict):
    """Validate JSON numbers before Python equality can alias booleans.

    OpenRouter's parameter reference defines temperature in [0, 2], top_p in
    [0, 1], positive integer max_tokens and integer seed (no documented bound):
    https://openrouter.ai/docs/api_reference/parameters
    """
    if type(body.get("max_tokens")) is not int or body["max_tokens"] <= 0:
        raise Incomplete("explicit output cap required")
    for key, maximum in (("temperature", 2), ("top_p", 1)):
        if key in body and (type(body[key]) not in (int, float) or not 0 <= body[key] <= maximum):
            raise Incomplete(f"invalid numeric parameter: {key}")
    if "seed" in body and type(body["seed"]) is not int:
        raise Incomplete("invalid integer parameter: seed")
    if "reasoning" in body:
        reasoning = body["reasoning"]
        if not isinstance(reasoning, dict):
            raise Incomplete("reasoning must be a JSON object")
        if "max_tokens" in reasoning and (
            type(reasoning["max_tokens"]) is not int or reasoning["max_tokens"] < 0
        ):
            raise Incomplete("invalid integer parameter: reasoning.max_tokens")


def effective(parameters: dict, call: dict, provider_params: dict | None):
    if not isinstance(parameters, dict):
        raise Incomplete("missing intended production parameters")
    body = dict(parameters)
    if set(body) - {"temperature", "max_tokens", "top_p", "seed", "reasoning", "provider"}:
        raise Incomplete("unsupported parameters or model fallback")
    validate_numeric_parameters(body)
    policy = body.get("provider", {})
    if (
        not isinstance(policy, dict)
        or set(policy) != {"only", "allow_fallbacks", "require_parameters"}
        or policy.get("allow_fallbacks") is not False
        or policy.get("require_parameters") is not True
    ):
        raise Incomplete("provider fallback must be disabled with required parameters")
    if (
        not isinstance(policy.get("only"), list)
        or len(policy["only"]) != 1
        or not isinstance(policy["only"][0], str)
        or not policy["only"][0].strip()
    ):
        raise Incomplete("exactly one upstream provider must be pinned")
    kwargs = {k: v for k, v in call["kwargs"].items() if k != "chain_offset"}
    if any(body.get(k) != v for k, v in kwargs.items()):
        raise Incomplete("intended production parameters contradict contract defaults")
    delegate = {k: v for k, v in body.items() if k not in ("provider", "reasoning")}
    delegate["extra_body"] = {k: v for k, v in body.items() if k in ("provider", "reasoning")}
    for key, value in (provider_params or {}).items():
        if key == "extra_body":
            delegate[key] = {**value, **delegate[key]}
        else:
            delegate.setdefault(key, value)
    wire = {k: v for k, v in delegate.items() if k != "extra_body"}
    wire.update(delegate["extra_body"])
    if wire != body:
        raise Incomplete("provider defaults introduce unfrozen parameters")
    # OpenRouterConfig.transform_request adds this to production calls. Freeze
    # the actual wire default as well as caller/provider parameters.
    body["usage"] = {"include": True}
    return body, delegate


def pricing_issues(pricing: dict, parameters: dict, model: str, *, check_expiry=True) -> list[str]:
    issues = []
    if not isinstance(pricing, dict):
        return ["missing verified pricing evidence"]
    required = ("input_per_million", "output_per_million", "request_fee")
    try:
        for key in required:
            money(pricing.get(key))
        if type(pricing.get("max_input_tokens")) is not int or pricing["max_input_tokens"] <= 0:
            raise Incomplete("missing input token ceiling")
        expires = datetime.fromisoformat(pricing.get("valid_until", ""))
        if expires.tzinfo is None or (check_expiry and expires <= datetime.now(UTC)):
            raise Incomplete("expired pricing evidence")
    except (ValueError, TypeError):
        issues.append("invalid or expired verified maximum-charge evidence")
    if (
        pricing.get("currency") != "USD"
        or pricing.get("model_id") != model
        or pricing.get("endpoint") != ENDPOINT
        or pricing.get("parameters_hash") != digest(parameters)
    ):
        issues.append("pricing identity/parameter binding missing")
    if pricing.get("verified") is not True or not all(
        isinstance(pricing.get(k), str) and pricing[k].strip()
        for k in ("reviewer", "evidence", "bound_basis")
    ):
        issues.append("verified pricing and token/fee bound need independent evidence")
    return issues


def maximum_charge(pricing: dict, body: dict) -> Decimal:
    return currency_sum(
        (
            pricing["request_fee"],
            token_charge(pricing["input_per_million"], pricing["max_input_tokens"]),
            token_charge(pricing["output_per_million"], body["max_tokens"]),
        )
    )


def reviewer_identity(value: str) -> str:
    """Compare declared identities across case, whitespace and canonical Unicode."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", value.strip()).casefold())


def approval_issues(cases, approval) -> list[str]:
    labelers = {reviewer_identity(c["reference_provenance"]["reviewer"]) for c in cases}
    if not isinstance(approval, dict) or (
        approval.get("approved") is not True
        or approval.get("independent") is not True
        or approval.get("corpus_hash") != digest(cases)
        or not isinstance(approval.get("reviewer"), str)
        or not approval["reviewer"].strip()
        or reviewer_identity(approval["reviewer"]) in labelers
        or not isinstance(approval.get("evidence"), str)
        or not approval["evidence"].strip()
    ):
        return ["independent reference approval is missing"]
    return []


def validate_manifest(manifest: dict):
    if not isinstance(manifest, dict):
        raise Incomplete("manifest must be a JSON object")
    if (
        manifest.get("format") != "genesis.qualification.v1"
        or not isinstance(manifest.get("provider"), str)
        or manifest["provider"] not in SUPPORTED_CANDIDATES
        or manifest.get("endpoint") != ENDPOINT
    ):
        raise Incomplete("invalid campaign identity")
    model = manifest.get("model_id")
    # Earlier MiMo v1 campaigns accepted the xiaomi/mimo-* family. Preserve
    # frozen historical reports; prepare and live preflight enforce exact IDs.
    if not isinstance(model, str) or (
        not model.startswith("xiaomi/mimo-")
        if manifest["provider"] == "openrouter-mimo"
        else model != SUPPORTED_CANDIDATES[manifest["provider"]]
    ):
        raise Incomplete("invalid frozen qualification candidate identity")
    if money(manifest.get("ceiling_usd")) <= 0:
        raise Incomplete("campaign ceiling must be positive")
    for key in ("parameters", "provider_config", "source", "libraries"):
        if not isinstance(manifest.get(key), dict):
            raise Incomplete("invalid frozen object field")
    if manifest["provider_config"].get("params") is not None and not isinstance(
        manifest["provider_config"]["params"], dict
    ):
        raise Incomplete("invalid frozen provider parameters")
    # Reopening historical evidence must not depend on today's rubric registry.
    # Strict production reference validation runs at prepare and execute only.
    contracts = manifest.get("contracts")
    if (
        not isinstance(contracts, dict)
        or not {NOVELTY, RELEVANCE} <= contracts.keys()
        or not all(
            isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in contracts.items()
        )
    ):
        raise Incomplete("invalid frozen contract inventory")
    validate_frozen_corpus(manifest, contracts)
    validate_frozen_schedule(manifest)


def validate_frozen_corpus(manifest, contracts):
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise Incomplete("missing frozen corpus")
    seen = set()
    for case in cases:
        if not isinstance(case, dict):
            raise Incomplete("frozen case must be a JSON object")
        name = case.get("contract")
        provenance = case.get("reference_provenance", {})
        if (
            not isinstance(case.get("id"), str)
            or not case["id"].strip()
            or case["id"] in seen
            or not isinstance(name, str)
            or name not in contracts
            or not isinstance(provenance, dict)
            or provenance.get("rubric_version") != contracts[name]
            or provenance.get("label_source") != "human"
            or not isinstance(provenance.get("reviewer"), str)
            or not provenance["reviewer"].strip()
            or (name != NOVELTY and type(case.get("user_passed")) is not bool)
            or (name == NOVELTY and "expected_target" not in case)
        ):
            raise Incomplete("invalid frozen reference identity or label")
        seen.add(case["id"])
    if manifest["corpus_hash"] != digest(manifest["cases"]):
        raise Incomplete("corpus hash mismatch")


def validate_frozen_schedule(manifest):
    if not isinstance(manifest.get("schedule"), dict):
        raise Incomplete("frozen schedule must be a JSON object")
    expected_keys = set()
    for index, case in enumerate(manifest["cases"]):
        for repetition in (1, 2, 3):
            key = digest([case["contract"], case["id"], repetition])
            expected_keys.add(key)
            task = manifest["schedule"].get(key, {})
            if not isinstance(task, dict) or not isinstance(task.get("kwargs"), dict):
                raise Incomplete("frozen task must be a JSON object")
            if any(
                task.get(k) != v
                for k, v in {
                    "case_index": index,
                    "repetition": repetition,
                    "contract": case["contract"],
                    "case_id": case["id"],
                }.items()
            ) or task.get("prompt_hash") != digest(task.get("messages")):
                raise Incomplete("invalid frozen schedule")
    if set(manifest["schedule"]) != expected_keys:
        raise Incomplete("missing or additional scheduled attempts")
    if (
        not isinstance(manifest.get("order"), list)
        or len(manifest["order"]) != len(expected_keys)
        or any(not isinstance(key, str) for key in manifest["order"])
        or set(manifest["order"]) != expected_keys
    ):
        raise Incomplete("invalid frozen schedule order")


def frozen_issues(manifest: dict) -> list[str]:
    validate_manifest(manifest)
    cases, pricing = manifest["cases"], manifest["pricing"]
    issues = coverage(cases, names=manifest["contracts"]) + approval_issues(
        cases, manifest["reference_approval"]
    )
    price_issues = pricing_issues(
        pricing, manifest["parameters"], manifest["model_id"], check_expiry=False
    )
    issues.extend(price_issues)
    if not price_issues:
        if pricing.get("corpus_hash") != manifest["corpus_hash"]:
            issues.append("input token bound is not bound to this corpus")
        charges = []
        for task in manifest["schedule"].values():
            route = (
                "novelty"
                if task["contract"] == NOVELTY
                else "relevance"
                if task["contract"] == RELEVANCE
                else "judge"
            )
            body, delegate = effective(
                manifest["parameters"].get(route), task, manifest["provider_config"]["params"]
            )
            if task["parameters"] != body or task["delegate_parameters"] != delegate:
                raise Incomplete("frozen parameter mismatch")
            maximum = maximum_charge(pricing, body)
            if money(task["maximum_usd"]) != maximum or maximum <= 0:
                raise Incomplete("invalid maximum charge")
            charges.append(maximum)
        total = currency_sum(charges)
        if money(manifest["maximum_campaign_usd"]) != total:
            raise Incomplete("campaign cost bound mismatch")
        ceiling = money(manifest["ceiling_usd"])
        if total > ceiling:
            issues.append(
                f"complete protocol maximum ${total} exceeds ${ceiling} by ${total - ceiling}"
            )
    return sorted(set(issues))


async def prepare(spec: dict, *, temp_root: Path, root=ROOT) -> dict:
    if not isinstance(spec, dict):
        raise Incomplete("spec must be a JSON object")
    ceiling = money(spec.get("ceiling_usd", DEFAULT_CEILING))
    if ceiling <= 0:
        raise Incomplete("campaign ceiling must be positive")
    cases = spec.get("cases")
    validate_cases(cases)
    provider = spec.get("provider", "openrouter-mimo")
    config = routing(root)
    cfg = candidate_config(provider, config)
    parameters = spec.get("parameters", {})
    pricing = spec.get("pricing", {})
    if not isinstance(parameters, dict) or not isinstance(pricing, dict):
        raise Incomplete("parameters and pricing must be JSON objects")
    issues = coverage(cases) + pricing_issues(pricing, parameters, cfg.model_id)
    approval = spec.get("reference_approval", {})
    issues.extend(approval_issues(cases, approval))
    if pricing.get("corpus_hash") != digest(cases):
        issues.append("input token bound is not bound to this corpus")
    schedule = {}
    questions = set()
    async with Sandbox(temp_root) as sandbox:
        for index, case in enumerate(cases):
            call = await render(case, sandbox, provider=provider, model=cfg.model_id)
            marker = (case["contract"], call["prompt_hash"])
            if marker in questions:
                raise Incomplete("duplicate rendered contract question")
            questions.add(marker)
            route = (
                "novelty"
                if case["contract"] == NOVELTY
                else "relevance"
                if case["contract"] == RELEVANCE
                else "judge"
            )
            body, delegate = effective(parameters.get(route), call, cfg.params)
            maximum = None
            if not pricing_issues(pricing, parameters, cfg.model_id):
                maximum = maximum_charge(pricing, body)
                if maximum <= 0:
                    issues.append("maximum charge must be positive")
            for repetition in (1, 2, 3):
                key = digest([case["contract"], case["id"], repetition])
                schedule[key] = {
                    **call,
                    "case_index": index,
                    "contract": case["contract"],
                    "case_id": case["id"],
                    "repetition": repetition,
                    "parameters": body,
                    "delegate_parameters": delegate,
                    "maximum_usd": str(maximum) if maximum is not None else None,
                }
    total = (
        currency_sum(t["maximum_usd"] for t in schedule.values())
        if all(t["maximum_usd"] is not None for t in schedule.values())
        else None
    )
    if total is not None and total > ceiling:
        issues.append(
            f"complete protocol maximum ${total} exceeds ${ceiling} by ${total - ceiling}"
        )
    return {
        "format": "genesis.qualification.v1",
        "ceiling_usd": str(ceiling),
        "source": source_identity(root),
        "libraries": {name: version(name) for name in ("litellm", "httpx")},
        "contracts": versions(),
        "provider": provider,
        "model_id": cfg.model_id,
        "endpoint": ENDPOINT,
        "provider_config": asdict(cfg),
        "corpus_hash": digest(cases),
        "cases": cases,
        "parameters": parameters,
        "pricing": pricing,
        "reference_approval": approval,
        "schedule": schedule,
        "order": list(schedule),
        "maximum_campaign_usd": str(total) if total is not None else None,
        "preflight_issues": sorted(set(issues)),
    }


def preflight(manifest: dict, *, root=ROOT, check_expiry=True) -> list[str]:
    issues = frozen_issues(manifest)
    cfg = candidate_config(manifest["provider"], routing(root))
    if manifest["model_id"] != cfg.model_id:
        raise Incomplete("unsupported qualification candidate identity for execution")
    validate_cases(manifest["cases"])
    if manifest["contracts"] != versions():
        issues.append("contract inventory changed; prepare a new campaign")
    if manifest["source"] != source_identity(root):
        issues.append("source identity changed; prepare a new campaign")
    issues.extend(
        pricing_issues(
            manifest["pricing"],
            manifest["parameters"],
            manifest["model_id"],
            check_expiry=check_expiry,
        )
    )
    if manifest["libraries"] != {name: version(name) for name in ("litellm", "httpx")}:
        issues.append("transport library version changed")
    if manifest["provider_config"] != asdict(cfg):
        issues.append("provider configuration changed")
    return sorted(set(issues))
