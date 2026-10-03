"""Freeze the corpus, source contracts, intended production payload and cost bound."""

from __future__ import annotations

import hashlib
import subprocess
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
    CEILING,
    Incomplete,
    currency_sum,
    digest,
    money,
    token_charge,
)
from genesis.eval.qualification.transport import ENDPOINT
from genesis.routing.config import load_config_from_string

ROOT = Path(__file__).resolve().parents[4]
SOURCE_FILES = (
    "src/genesis/eval/scorers.py",
    "src/genesis/eval/calibration.py",
    "src/genesis/eval/j9_batch.py",
    "src/genesis/learning/procedural/extractor.py",
    "src/genesis/learning/procedural/judge.py",
    "src/genesis/learning/procedural/operations.py",
    "src/genesis/learning/procedural/embedding.py",
    "src/genesis/learning/procedural/scoping.py",
    "src/genesis/learning/procedural/validation_gate.py",
    "src/genesis/db/crud/procedural.py",
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
    return {
        "commit": subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in sorted(paths)},
    }


def routing(root=ROOT):
    # Read the shipped configuration without credentials or local overlays.
    return load_config_from_string(
        (root / "config/model_routing.yaml").read_text(), check_api_keys=False
    )


def effective(parameters: dict, call: dict, provider_params: dict | None):
    if not isinstance(parameters, dict):
        raise Incomplete("missing intended production parameters")
    body = dict(parameters)
    if set(body) - {"temperature", "max_tokens", "top_p", "seed", "reasoning", "provider"}:
        raise Incomplete("unsupported parameters or model fallback")
    if type(body.get("max_tokens")) is not int or body["max_tokens"] <= 0:
        raise Incomplete("explicit output cap required")
    policy = body.get("provider", {})
    if (
        set(policy) != {"only", "allow_fallbacks", "require_parameters"}
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


def approval_issues(cases, approval) -> list[str]:
    labelers = {c["reference_provenance"]["reviewer"] for c in cases}
    if not isinstance(approval, dict) or (
        approval.get("approved") is not True
        or approval.get("independent") is not True
        or approval.get("corpus_hash") != digest(cases)
        or not isinstance(approval.get("reviewer"), str)
        or not approval["reviewer"].strip()
        or approval["reviewer"] in labelers
        or not isinstance(approval.get("evidence"), str)
        or not approval["evidence"].strip()
    ):
        return ["independent reference approval is missing"]
    return []


def validate_manifest(manifest: dict):
    if (
        manifest.get("format") != "genesis.qualification.v1"
        or manifest.get("ceiling_usd") != "5"
        or manifest.get("provider") != "openrouter-mimo"
        or manifest.get("endpoint") != ENDPOINT
    ):
        raise Incomplete("invalid campaign identity")
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
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise Incomplete("missing frozen corpus")
    seen = set()
    for case in cases:
        name = case.get("contract")
        provenance = case.get("reference_provenance", {})
        if (
            not isinstance(case.get("id"), str)
            or not case["id"].strip()
            or case["id"] in seen
            or name not in contracts
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
    expected_keys = set()
    for index, case in enumerate(manifest["cases"]):
        for repetition in (1, 2, 3):
            key = digest([case["contract"], case["id"], repetition])
            expected_keys.add(key)
            task = manifest["schedule"].get(key, {})
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
        if total > CEILING:
            issues.append(f"complete protocol maximum ${total} exceeds $5 by ${total - CEILING}")
    return sorted(set(issues))


async def prepare(spec: dict, *, temp_root: Path, root=ROOT) -> dict:
    cases = spec.get("cases")
    validate_cases(cases)
    provider = spec.get("provider", "openrouter-mimo")
    if provider != "openrouter-mimo":
        raise Incomplete("this campaign authorizes MiMo only; DeepSeek spend is zero")
    config = routing(root)
    cfg = config.providers[provider]
    if (
        cfg.provider_type != "openrouter"
        or not cfg.model_id.startswith("xiaomi/mimo-")
        or cfg.base_url not in (None, ENDPOINT)
    ):
        raise Incomplete("unexpected MiMo provider identity")
    parameters = spec.get("parameters", {})
    pricing = spec.get("pricing", {})
    issues = coverage(cases) + pricing_issues(pricing, parameters, cfg.model_id)
    approval = spec.get("reference_approval", {})
    issues.extend(approval_issues(cases, approval))
    if pricing.get("corpus_hash") != digest(cases):
        issues.append("input token bound is not bound to this corpus")
    schedule = {}
    questions = set()
    async with Sandbox(temp_root) as sandbox:
        for index, case in enumerate(cases):
            call = await render(case, sandbox)
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
    if total is not None and total > CEILING:
        issues.append(f"complete protocol maximum ${total} exceeds $5 by ${total - CEILING}")
    return {
        "format": "genesis.qualification.v1",
        "ceiling_usd": "5",
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


def preflight(manifest: dict, *, root=ROOT) -> list[str]:
    issues = frozen_issues(manifest)
    validate_cases(manifest["cases"])
    if manifest["contracts"] != versions():
        issues.append("contract inventory changed; prepare a new campaign")
    if manifest["source"] != source_identity(root):
        issues.append("source identity changed; prepare a new campaign")
    issues.extend(pricing_issues(manifest["pricing"], manifest["parameters"], manifest["model_id"]))
    if manifest["libraries"] != {name: version(name) for name in ("litellm", "httpx")}:
        issues.append("transport library version changed")
    if manifest["provider_config"] != asdict(routing(root).providers[manifest["provider"]]):
        issues.append("provider configuration changed")
    return sorted(set(issues))
