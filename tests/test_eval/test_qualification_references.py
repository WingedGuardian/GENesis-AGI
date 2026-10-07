"""Offline reference admission/feedback; original stage-two regression identities."""

import copy

import pytest

from genesis.eval.qualification import references
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_reference_fixtures import (
    NOVELTY,
    RELEVANCE,
    small_corpus,
    versions,
)


def policy():
    return {
        "version": references.VERSION,
        "confidence_threshold": 90,
        "approved_models": ["synthetic-frontier"],
        "guidance": "Use each registered contract and verify supporting evidence.",
        "feedback": [
            {
                "id": "when",
                "text": "Check when a procedure applies, not only its steps.",
                "evidence": "Synthetic human message",
                "contracts": [NOVELTY],
            }
        ],
    }


@pytest.mark.parametrize(
    "model",
    [
        "openrouter/xiaomi/mimo-v2.6-pro",
        "OPENROUTER/XIAOMI/MIMO-V2.6-PRO",
        "deepinfra/xiaomi/mimo-v2.6-pro",
        "nvidia_nim/deepseek-ai/deepseek-v4.1-flash",
        "litellm/openrouter/deepseek/deepseek-v4.1-flash",
        "openrouter/deepseek/deepseek-v4-pro",
    ],
)
def test_gateway_prefixes_do_not_admit_candidate_graders(model):
    guidance = policy()
    guidance["approved_models"] = [model]
    with pytest.raises(Incomplete, match="exclude qualification candidates"):
        references.validate_policy(guidance, versions())


def test_normalized_grader_duplicates_are_rejected():
    guidance = policy()
    guidance["approved_models"] = ["synthetic-frontier", "openrouter/synthetic-frontier"]
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, versions())


@pytest.mark.parametrize("model", ["openrouter/", "litellm/openrouter/"])
def test_gateway_without_a_model_is_rejected(model):
    guidance = policy()
    guidance["approved_models"] = [model]
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, versions())


def test_identity_normalizes_gateway_without_removing_vendor():
    assert references.identity("litellm/openrouter/openai/gpt-6") == "openai/gpt-6"
    assert references.identity("openrouter/xiaomi/mimo-v2.6-pro") == "xiaomi/mimo-v2.6-pro"


def receipt(name, case, guidance):
    case["reference_review"] = {
        "reviewer": "synthetic-frontier-reviewer",
        "evidence_complete": True,
        "uncertainties": [],
        "policy_hash": digest(guidance),
        "case_hash": references.case_hash(name, case),
        "feedback_applicability": {
            f["id"]: {
                "disposition": "regraded" if name in f["contracts"] else "unaffected",
                "reason": "Synthetic applicability assessment against this case.",
            }
            for f in guidance["feedback"]
        },
    }


def mixed():
    guidance, cases = policy(), small_corpus(novelty=False)
    for name, rows in cases.items():
        case = rows[0]
        case["reference_passed"] = case.pop("user_passed")
        case["reference_provenance"].update(
            label_source=references.FRONTIER,
            reviewer="synthetic-frontier-reviewer",
            model_id="synthetic-frontier",
            model_identity_evidence="Synthetic provider-reported identity",
            confidence_percent=90,
            rationale="Synthetic source proves this fails the stated contract.",
            evidence="Synthetic supporting source",
            evidence_complete=True,
            uncertainties=[],
        )
        for case in rows:
            receipt(name, case, guidance)
    return guidance, cases


def approval(cases, guidance):
    return {
        "label_source": "human",
        "reviewer": "synthetic-owner",
        "approved": True,
        "independent": True,
        "evidence": "Synthetic human approval only",
        "policy_hash": digest(guidance),
        "corpus_hash": digest(cases),
    }


@pytest.mark.parametrize("same_contract", [False, True])
def test_review_identity_is_scoped_to_contract(same_contract):
    guidance, cases = mixed()
    names = [next(n for n in cases if n != RELEVANCE), RELEVANCE]
    if same_contract:
        names[1] = names[0]
    rows = []
    for name in names:
        case = copy.deepcopy(cases[name][0])
        case["id"] = "shared-case-id"
        case["contract"] = name
        receipt(name, case, guidance)
        rows.append(case)
    spec = {"cases": rows, "reference_policy": guidance}
    if same_contract:
        with pytest.raises(Incomplete, match="duplicate reference identity"):
            references.review(spec, versions())
    else:
        assert references.review(spec, versions())["counts"] == {"admitted": 2}


@pytest.mark.parametrize("threshold", [89, True, 90.0, "90", 101])
def test_invalid_policy_threshold(threshold):
    guidance = policy()
    guidance["confidence_threshold"] = threshold
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, versions())


@pytest.mark.parametrize(
    "model", ["xiaomi/mimo-v2.6-pro", "deepseek/deepseek-v4.1-flash", "XIAOMI/MIMO-any"]
)
def test_candidate_self_grading_forbidden(model):
    guidance = policy()
    guidance["approved_models"] = [model]
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, versions())


def test_new_feedback_invalidates_every_accepted_case_including_human_labels():
    guidance, cases = mixed()
    spec = {
        "reference_policy": guidance,
        "cases": [{**c, "contract": name} for name, rows in cases.items() for c in rows],
    }
    # Rebind after adding contract, which is part of the actual case input.
    for case in spec["cases"]:
        receipt(case["contract"], case, guidance)
    assert references.review(spec, versions())["counts"] == {"admitted": 14}
    guidance["feedback"].append(
        {
            "id": "provenance",
            "text": "Check session and batch provenance",
            "evidence": "Synthetic human correction",
            "contracts": list(versions()),
        }
    )
    result = references.review(spec, versions())
    assert result["counts"] == {"needs_llm_regrade": 14}
    assert all("stale guidance, input or decision review" in r["issues"] for r in result["cases"])
    for case in spec["cases"]:
        receipt(case["contract"], case, guidance)
    assert references.review(spec, versions())["status"] == "pass"


def test_human_corpus_approval_binds_policy_and_corpus():
    guidance, cases = mixed()
    approved = approval(cases, guidance)
    assert references.approval_issues(cases, guidance, approved) == []
    for key, value in [
        ("reviewer", " SYNTHETIC-FRONTIER-REVIEWER "),
        ("label_source", references.FRONTIER),
        ("approved", False),
        ("policy_hash", "old"),
        ("corpus_hash", "old"),
    ]:
        assert references.approval_issues(cases, guidance, {**approved, key: value})


def test_review_queue_keeps_routine_work_with_llm_and_only_escalates_uncertainty():
    guidance, cases = mixed()
    name = next(iter(cases))
    case = {**copy.deepcopy(cases[name][0]), "contract": name}
    receipt(name, case, guidance)
    spec = {"reference_policy": guidance, "cases": [case]}
    assert references.review(spec, versions())["counts"] == {"admitted": 1}
    case.pop("reference_review")
    assert references.review(spec, versions())["counts"] == {"needs_llm_regrade": 1}
    case["reference_provenance"]["confidence_percent"] = 89
    assert references.review(spec, versions())["counts"] == {"human_review": 1}
