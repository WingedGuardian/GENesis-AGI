"""Synthetic mixed reference admission and propagation; all provider traffic mocked."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from genesis.eval.calibration import _validate_references
from genesis.eval.qualification import corpus, references, run
from genesis.eval.qualification.__main__ import main
from genesis.eval.qualification.evidence import Campaign, Incomplete, digest
from genesis.eval.rubrics import get_rubric
from tests.test_eval.qualification_fixtures import (
    OpenRouter,
    isolate_credentials,
    params,
    small_corpus,
    write_corpus,
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
                "contracts": [corpus.NOVELTY],
            }
        ],
    }


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


def test_default_human_validator_remains_strict(tmp_path):
    guidance, cases = mixed()
    name = next(iter(cases))
    with pytest.raises((ValueError, KeyError)):
        _validate_references(cases[name], get_rubric(name))
    with pytest.raises(Incomplete):
        corpus.load(write_corpus(tmp_path / "legacy", cases))
    loaded = corpus.load(tmp_path / "legacy", reference_policy=guidance)
    assert corpus.counts(loaded) == corpus.counts(small_corpus(novelty=False))
    assert references.summary(loaded, guidance)["sources"] == {"human": 7, references.FRONTIER: 7}


@pytest.mark.parametrize("same_contract", [False, True])
def test_review_identity_is_scoped_to_contract(same_contract):
    guidance, cases = mixed()
    names = [next(n for n in cases if n != corpus.RELEVANCE), corpus.RELEVANCE]
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
            references.review(spec, corpus.versions())
    else:
        assert references.review(spec, corpus.versions())["counts"] == {"admitted": 2}


@pytest.mark.parametrize(
    "change",
    [
        {"confidence_percent": 89},
        {"confidence_percent": True},
        {"confidence_percent": "99"},
        {"confidence_percent": 90.0},
        {"confidence_percent": 101},
        {"confidence_percent": -1},
        {"confidence_percent": 99, "uncertainties": ["Source provenance missing"]},
        {"confidence_percent": 100, "evidence_complete": False},
        {"uncertainties": None},
        {"evidence": ""},
        {"rationale": ""},
        {"model_identity_evidence": ""},
        {"model_id": "unapproved"},
    ],
)
def test_confidence_cannot_override_uncertainty_or_missing_evidence(change):
    guidance, cases = mixed()
    name = next(iter(cases))
    case = cases[name][0]
    case["reference_provenance"].update(change)
    receipt(name, case, guidance)
    with pytest.raises(Incomplete):
        corpus.validate(name, cases[name], reference_policy=guidance)


@pytest.mark.parametrize("threshold", [89, True, 90.0, "90", 101])
def test_invalid_policy_threshold(threshold):
    guidance = policy()
    guidance["confidence_threshold"] = threshold
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, corpus.versions())


@pytest.mark.parametrize(
    "model", ["xiaomi/mimo-v2.6-pro", "deepseek/deepseek-v4.1-flash", "XIAOMI/MIMO-any"]
)
def test_candidate_self_grading_forbidden(model):
    guidance = policy()
    guidance["approved_models"] = [model]
    with pytest.raises(Incomplete):
        references.validate_policy(guidance, corpus.versions())


def test_new_feedback_invalidates_every_accepted_case_including_human_labels():
    guidance, cases = mixed()
    spec = {
        "reference_policy": guidance,
        "cases": [{**c, "contract": name} for name, rows in cases.items() for c in rows],
    }
    # Rebind after adding contract, which is part of the actual case input.
    for case in spec["cases"]:
        receipt(case["contract"], case, guidance)
    assert references.review(spec, corpus.versions())["counts"] == {"admitted": 14}
    guidance["feedback"].append(
        {
            "id": "provenance",
            "text": "Check session and batch provenance",
            "evidence": "Synthetic human correction",
            "contracts": list(corpus.versions()),
        }
    )
    result = references.review(spec, corpus.versions())
    assert result["counts"] == {"needs_llm_regrade": 14}
    assert all("stale guidance, input or decision review" in r["issues"] for r in result["cases"])
    for case in spec["cases"]:
        receipt(case["contract"], case, guidance)
    assert references.review(spec, corpus.versions())["status"] == "pass"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(actual="Changed input"),
        lambda c: c.update(reference_passed=True),
        lambda c: c["reference_provenance"].update(confidence_percent=99),
        lambda c: c["reference_review"].update(feedback_applicability={}),
        lambda c: c["reference_review"]["feedback_applicability"]["when"].update(reason=""),
    ],
)
def test_input_decision_and_applicability_receipts_are_bound(mutate):
    guidance, cases = mixed()
    name = next(iter(cases))
    mutate(cases[name][0])
    with pytest.raises(Incomplete):
        corpus.validate(name, cases[name], reference_policy=guidance)


def test_duplicate_questions_rejected_across_human_and_frontier_sources():
    guidance, cases = mixed()
    name = next(iter(cases))
    cases[name][1]["actual"] = cases[name][0]["actual"]
    receipt(name, cases[name][1], guidance)
    with pytest.raises(Incomplete, match="duplicate grading question"):
        corpus.validate(name, cases[name], reference_policy=guidance)


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


async def test_current_guidance_and_approval_stop_before_router_or_network(tmp_path, monkeypatch):
    guidance, cases = mixed()

    def refuse(*args, **kwargs):
        raise AssertionError("router must not be constructed")

    monkeypatch.setattr(run, "PinnedRouter", refuse)
    with Campaign(tmp_path / "campaign") as campaign:
        for current, approved in [
            (None, approval(cases, guidance)),
            ({**guidance, "guidance": "New human input"}, approval(cases, guidance)),
            (guidance, None),
        ]:
            with pytest.raises(Incomplete):
                await run.qualify(
                    "openrouter-mimo",
                    cases,
                    params(),
                    campaign,
                    tmp_path / "sqlite",
                    reference_policy=guidance,
                    current_reference_policy=current,
                    reference_approval=approved,
                    dispatch=True,
                )
        assert campaign.lines == []


async def test_conflicting_legacy_label_fields_stop_before_router(tmp_path, monkeypatch):
    cases = small_corpus(novelty=False)
    cases[next(iter(cases))][0]["reference_passed"] = True

    def refuse(*args, **kwargs):
        raise AssertionError("conflicting fields must be rejected before dispatch")

    monkeypatch.setattr(run, "PinnedRouter", refuse)
    with Campaign(tmp_path / "campaign") as campaign:
        with pytest.raises(Incomplete, match="mixed label fields"):
            await run.qualify(
                "openrouter-mimo", cases, params(), campaign, tmp_path / "sqlite", dispatch=True
            )
        assert campaign.lines == []


@pytest.mark.parametrize("provenance", [None, [], "human"])
def test_legacy_malformed_provenance_remains_structured_incomplete(provenance):
    cases = small_corpus(novelty=False)
    name = next(iter(cases))
    cases[name][0]["reference_provenance"] = provenance
    with pytest.raises(Incomplete):
        corpus.validate(name, cases[name])


def test_review_queue_keeps_routine_work_with_llm_and_only_escalates_uncertainty():
    guidance, cases = mixed()
    name = next(iter(cases))
    case = {**copy.deepcopy(cases[name][0]), "contract": name}
    receipt(name, case, guidance)
    spec = {"reference_policy": guidance, "cases": [case]}
    assert references.review(spec, corpus.versions())["counts"] == {"admitted": 1}
    case.pop("reference_review")
    assert references.review(spec, corpus.versions())["counts"] == {"needs_llm_regrade": 1}
    case["reference_provenance"]["confidence_percent"] = 89
    assert references.review(spec, corpus.versions())["counts"] == {"human_review": 1}


async def test_mixed_rubrics_and_j9_mock_http_preserve_actual_labels(tmp_path, monkeypatch):
    guidance, cases = mixed()
    isolate_credentials(monkeypatch, tmp_path)
    server = OpenRouter()
    with Campaign(tmp_path / "campaign") as campaign:
        result, records = await run.qualify(
            "openrouter-mimo",
            cases,
            params(),
            campaign,
            tmp_path / "sqlite",
            reference_policy=guidance,
            current_reference_policy=guidance,
            reference_approval=approval(cases, guidance),
            dispatch=True,
            transport=server.transport,
        )
        assert len(server.requests) == 42  # 14 actual cases, three repetitions each
        assert all(
            row["agreed"]
            for reps in records.values()
            for rows in reps.values()
            for row in rows.values()
        )
        assert result["references"]["basis"] == "frontier-assisted"
        assert result["references"]["sources"] == {"human": 7, references.FRONTIER: 7}
        result, _ = await run.qualify(
            "openrouter-mimo",
            cases,
            params(),
            campaign,
            tmp_path / "report",
            reference_policy=guidance,
            dispatch=False,
            transport=server.transport,
        )
        assert len(server.requests) == 42
        assert result["status"] == "incomplete" and result["reference_approval_issues"]


def test_review_cli_offline_and_has_no_dispatch_side_effect(tmp_path, monkeypatch, capsys):
    guidance, cases = mixed()
    spec = {
        "reference_policy": guidance,
        "cases": [{**c, "contract": name} for name, rows in cases.items() for c in rows],
    }
    for case in spec["cases"]:
        receipt(case["contract"], case, guidance)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(spec))

    def refuse(*args, **kwargs):
        raise AssertionError("offline review attempted HTTP")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    assert main(["review-references", "--spec", str(path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["counts"] == {"admitted": 14}
    assert not (tmp_path / "campaign").exists()


def test_novelty_mixed_source_requires_real_target_and_keeps_history():
    guidance = policy()
    cases = small_corpus()[corpus.NOVELTY]
    case = cases[0]
    template = mixed()[1][corpus.RELEVANCE][0]["reference_provenance"]
    case["reference_provenance"] = {
        **copy.deepcopy(template),
        "rubric_version": corpus.versions()[corpus.NOVELTY],
    }
    case["reference_history"] = [{"label_source": "human", "judgment": "ambiguous"}]
    for row in cases:
        receipt(corpus.NOVELTY, row, guidance)
    corpus.validate(corpus.NOVELTY, cases, reference_policy=guidance)
    assert corpus.expected(corpus.NOVELTY, case) is None
    assert case["reference_history"][0]["judgment"] == "ambiguous"
    case["expected_target"] = "missing-candidate"
    receipt(corpus.NOVELTY, case, guidance)
    with pytest.raises(Incomplete, match="invalid novelty reference target"):
        corpus.validate(corpus.NOVELTY, cases, reference_policy=guidance)
