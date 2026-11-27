"""Owned source snapshots and synthetic receipts for verification writer tests."""

from dataclasses import replace
from threading import get_ident

from genesis.session_awareness import pr_completeness, pr_verification_sources
from genesis.session_awareness.pr_evidence import parse_evidence

MERGE_SHA = "deadbeef1234" + "0" * 28
_REAL_ASSESSMENT = pr_completeness.assess_completeness
WRITER_BODY = "The timer fires hourly."


def writer_source(repo, pr, body=WRITER_BODY):
    return pr_verification_sources.PrSourceSnapshot(
        repo, pr, MERGE_SHA, "b" * 40, "c" * 40, body, "", [], "2026-01-01T00:00:00Z",
    )


def simple_receipt(raw):
    """Bind the ordinary one-claim, one-control transport fixture."""
    source = writer_source(raw["repo"], raw["pr"])
    inventory = pr_completeness.ReviewInventory(
        policy_version=1,
        source=pr_completeness.source_binding(source),
        items=[{
            "id": "timer",
            "statement": WRITER_BODY,
            "sources": [{"kind": "body", "start": 1, "quote": WRITER_BODY}],
        }],
        files=[],
    )
    return raw | {"completeness": {
        "policy_version": 1,
        "review_id": "synthetic-timer-review",
        "review_execution_ref": "fixture-only:no-model-ran",
        "reviewer_kind": "fresh_context_agent",
        "completed_at": "2026-01-01T00:00:00Z",
        "inventory": inventory.model_dump(mode="json"),
        "inventory_sha256": pr_completeness.inventory_sha256(inventory),
        "evidence_sha256": pr_completeness.evidence_sha256(parse_evidence(raw)),
        "coverage": [{"item": "timer", "claims": [0], "gaps": list(range(len(raw["gaps"])))}],
        "claim_reviews": [{
            "kind": "runtime", "claim": 0, "controls": [0], "contrast": "The unarmed timer fails.",
        }],
        "findings": [],
        "complete": True,
    }}


def stub_legacy_writer_review(monkeypatch):
    # Verdict, schema and SQL tests deliberately vary the document in isolation.
    # The receipt matrix and subprocess fixtures exercise the real assessor.
    monkeypatch.setattr(pr_verification_sources, "acquire_sources", writer_source)
    monkeypatch.setattr(
        pr_completeness,
        "assess_completeness",
        lambda doc, source: pr_completeness.CompletenessAssessment(True, "owned fixture", "verified"),
    )


def use_real_receipt_assessment(monkeypatch):
    caller_thread = get_ident()

    def assess(doc, snapshot):
        assert get_ident() != caller_thread, "full receipt validation blocked the writer event loop"
        return _REAL_ASSESSMENT(doc, snapshot)

    monkeypatch.setattr(pr_completeness, "assess_completeness", assess)


CLOSE_CASES = {
    "verified": None,
    "missing": "independent completeness review is missing",
    "failed": "independent review is incomplete",
    "stale": "source, inventory or evidence digest is stale",
    "new-body": "E2E coverage is incomplete",
    "new-merge": "merge identity differs",
    "unavailable": "Cannot obtain current PR sources",
}


def reviewed_close(tmp_path, monkeypatch, case):
    from tests.test_session_awareness.test_pr_completeness import refresh, reviewed

    doc, source = reviewed(tmp_path)
    source = replace(source, merge_commit=MERGE_SHA)
    # A collision of the accepted short prefix must still refuse a changed merge.
    doc["merge_commit"] = MERGE_SHA[:12]
    doc["completeness"]["inventory"]["source"] = pr_completeness.source_binding(source).model_dump(mode="json")
    if case == "failed":
        doc["completeness"]["complete"] = False
    refresh(doc)
    if case == "missing":
        del doc["completeness"]

    def acquire(repo, pr):
        assert (repo, pr) == (source.repo, source.pr)
        if case == "unavailable":
            raise pr_verification_sources.SourceError("owned acquisition timeout")
        if case == "new-body":
            return replace(source, body=source.body + "\nE2E: measure another promised behaviour")
        if case == "new-merge":
            return replace(source, merge_commit=MERGE_SHA[:-1] + "1")
        if case == "stale":
            return replace(source, head="f" * 40)
        return source

    monkeypatch.setattr(pr_verification_sources, "acquire_sources", acquire)
    use_real_receipt_assessment(monkeypatch)
    return doc, source
