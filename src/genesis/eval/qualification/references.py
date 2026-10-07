"""Declared reference admission and corpus-wide feedback propagation, offline.

Receipts establish which inputs and guidance a reviewer checked. They do not
authenticate reviewers or calibrate a model's self-reported confidence.
"""

from __future__ import annotations

import unicodedata
from collections import Counter

from genesis.eval.qualification.evidence import Incomplete, digest

VERSION = "frontier-assisted-v1"
FRONTIER = "frontier_llm"
NOVELTY = "procedure_novelty"


def nonblank(value):
    return isinstance(value, str) and bool(value.strip())


def identity(value):
    value = unicodedata.normalize("NFC", value.strip()).casefold()
    prefixes = ("openrouter/", "deepinfra/", "nvidia_nim/", "litellm/")
    while value.startswith(prefixes):
        value = value.split("/", 1)[1].strip()
    return value


def validate_policy(policy, contracts):
    if not isinstance(policy, dict) or policy.get("version") != VERSION:
        raise Incomplete("unknown reference policy")
    threshold = policy.get("confidence_threshold", 90)
    if type(threshold) is not int or not 90 <= threshold <= 100:
        raise Incomplete("reference confidence threshold must be an integer from 90 to 100")
    models = policy.get("approved_models")
    if (
        not isinstance(models, list)
        or not models
        or any(not nonblank(m) or m != m.strip() for m in models)
        or any(not identity(m) for m in models)
        or len({identity(m) for m in models}) != len(models)
        or any(identity(m).startswith(("xiaomi/mimo", "deepseek/", "deepseek-ai/")) for m in models)
    ):
        raise Incomplete("approved frontier graders must exclude qualification candidates")
    if not nonblank(policy.get("guidance")):
        raise Incomplete("reference grading guidance is missing")
    feedback = policy.get("feedback")
    if not isinstance(feedback, list):
        raise Incomplete("reference feedback must be a list")
    seen = set()
    for item in feedback:
        if (
            not isinstance(item, dict)
            or not all(nonblank(item.get(k)) for k in ("id", "text", "evidence"))
            or item["id"] in seen
            or not isinstance(item.get("contracts"), list)
            or not item["contracts"]
            or any(not isinstance(c, str) or c not in contracts for c in item["contracts"])
        ):
            raise Incomplete("invalid or duplicate human feedback")
        seen.add(item["id"])


def passed(case):
    """Machine labels never occupy the field reserved for human judgments."""
    provenance = case.get("reference_provenance", {})
    field = "reference_passed" if provenance.get("label_source") == FRONTIER else "user_passed"
    if type(case.get(field)) is not bool:
        raise Incomplete("reference requires a boolean label from its declared source")
    if ("user_passed" if field == "reference_passed" else "reference_passed") in case:
        raise Incomplete("mixed label fields; preserve earlier judgments in reference_history")
    return case[field]


def case_hash(contract, case):
    # The current decision, provenance, sources and all prompt inputs are bound.
    # History is evidence of prior judgments, never the active reference label.
    return digest(
        [
            contract,
            {k: v for k, v in case.items() if k not in ("reference_review", "reference_history")},
        ]
    )


def blockers(contract, case, policy, version):
    """Return review obligations without manufacturing a label or receipt."""
    issues = []
    provenance = case.get("reference_provenance")
    if not isinstance(provenance, dict):
        return ["ungraded reference"]
    source = provenance.get("label_source")
    if source not in ("human", FRONTIER):
        issues.append("unapproved label source")
    if not nonblank(provenance.get("reviewer")) or provenance.get("rubric_version") != version:
        issues.append("missing reviewer or mismatched contract version")
    if contract == NOVELTY:
        if "expected_target" not in case:
            issues.append("ungraded novelty target")
        elif case["expected_target"] is not None and (
            not isinstance(case["expected_target"], str)
            or not isinstance(case.get("existing"), list)
            or case["expected_target"]
            not in [c.get("id") for c in case["existing"] if isinstance(c, dict)]
        ):
            issues.append("invalid novelty reference target")
    else:
        try:
            passed(case)
        except Incomplete:
            issues.append("missing or conflicting reference label")
    if source == FRONTIER:
        confidence = provenance.get("confidence_percent")
        if type(confidence) is not int or not 0 <= confidence <= 100:
            issues.append("invalid confidence")
        elif confidence < policy.get("confidence_threshold", 90):
            issues.append("confidence below human-review threshold")
        model = provenance.get("model_id")
        if not nonblank(model) or identity(model) not in {
            identity(m) for m in policy["approved_models"]
        }:
            issues.append("unapproved grader model")
        if not all(
            nonblank(provenance.get(k))
            for k in ("model_identity_evidence", "rationale", "evidence")
        ):
            issues.append("missing grader identity or source-backed rationale")
        if provenance.get("evidence_complete") is not True:
            issues.append("missing supporting evidence")
        if provenance.get("uncertainties") != []:
            issues.append("unresolved uncertainty")
    receipt = case.get("reference_review")
    if not isinstance(receipt, dict):
        return issues + ["whole-corpus feedback review missing"]
    if receipt.get("evidence_complete") is not True or receipt.get("uncertainties") != []:
        issues.append("feedback review has missing evidence or unresolved uncertainty")
    if receipt.get("policy_hash") != digest(policy) or receipt.get("case_hash") != case_hash(
        contract, case
    ):
        issues.append("stale guidance, input or decision review")
    applicability = receipt.get("feedback_applicability")
    ids = {f["id"] for f in policy["feedback"]}
    if not isinstance(applicability, dict) or set(applicability) != ids:
        issues.append("feedback applicability scan incomplete")
    else:
        for item in applicability.values():
            if (
                not isinstance(item, dict)
                or item.get("disposition") not in ("regraded", "unaffected")
                or not nonblank(item.get("reason"))
            ):
                issues.append("feedback needs a reasoned regrade or unaffected disposition")
                break
    if not nonblank(receipt.get("reviewer")):
        issues.append("feedback reviewer missing")
    return issues


def admit(contract, cases, policy, version):
    for case in cases:
        issues = blockers(contract, case, policy, version)
        if issues:
            raise Incomplete(f"{contract}: reference review required: {', '.join(issues)}")


def approval_issues(corpus, policy, approval):
    """Real human sign-off on machine-assisted references; no generated approval."""
    labelers = {
        identity(c["reference_provenance"]["reviewer"])
        for cases in corpus.values()
        for c in cases
        if c["reference_provenance"]["label_source"] == FRONTIER
    }
    if (
        not isinstance(approval, dict)
        or approval.get("label_source") != "human"
        or approval.get("approved") is not True
        or approval.get("independent") is not True
        or not nonblank(approval.get("reviewer"))
        or identity(approval["reviewer"]) in labelers
        or not nonblank(approval.get("evidence"))
        or approval.get("policy_hash") != digest(policy)
        or approval.get("corpus_hash") != digest(corpus)
    ):
        return ["human reference approval must bind this corpus and current policy"]
    return []


def summary(corpus, policy):
    return {
        "basis": "frontier-assisted" if policy is not None else "human-only",
        "policy_hash": digest(policy) if policy is not None else None,
        "confidence_threshold": policy.get("confidence_threshold", 90)
        if policy is not None
        else None,
        "sources": dict(
            Counter(
                c["reference_provenance"]["label_source"] for rows in corpus.values() for c in rows
            )
        ),
        "limitations": [
            "Model confidence is self-reported, not measured calibration.",
            "Declared identities, evidence and human approval are not authenticated by this CLI.",
        ],
    }


def review(spec, versions):
    """Offline queue from graded drafts, including cases accepted before new feedback."""
    if not isinstance(spec, dict) or not isinstance(spec.get("cases"), list) or not spec["cases"]:
        raise Incomplete("reference review needs nonempty cases")
    policy = spec.get("reference_policy")
    validate_policy(policy, versions)
    rows, seen = [], set()
    for case in spec["cases"]:
        if (
            not isinstance(case, dict)
            or not nonblank(case.get("id"))
            or not isinstance(case.get("contract"), str)
            or case["contract"] not in versions
            or (case["contract"], case["id"]) in seen
        ):
            raise Incomplete("invalid or duplicate reference identity")
        seen.add((case["contract"], case["id"]))
        issues = blockers(case["contract"], case, policy, versions[case["contract"]])
        human_issues = {
            "confidence below human-review threshold",
            "missing supporting evidence",
            "unresolved uncertainty",
            "feedback review has missing evidence or unresolved uncertainty",
        }
        status = (
            "human_review"
            if human_issues.intersection(issues)
            else "needs_llm_regrade"
            if issues
            else "admitted"
        )
        rows.append(
            {
                "id": case["id"],
                "contract": case["contract"],
                "status": status,
                "issues": issues,
            }
        )
    return {
        "status": "incomplete" if any(r["issues"] for r in rows) else "pass",
        "scope": "reference admission only; prompt/storage validation and qualification are separate",
        "policy_hash": digest(policy),
        "counts": dict(Counter(r["status"] for r in rows)),
        "cases": rows,
    }
