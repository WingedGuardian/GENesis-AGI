"""The close-time date refusal and the completeness prompts (#4180).

Measured basis (2026-11-14): of 128 defects independent verifiers named in
first-round evidence documents, a deterministic rule could catch only a few. The
one refusal here reads a typed field (an event-gated date that has already come);
the prompts are questions for the validator and never change a verdict.
"""

from __future__ import annotations

from datetime import date

import pytest

from genesis.session_awareness import pr_evidence as pe

TODAY = date(2026, 11, 14)


def _doc(claims=None, gaps=(), tracking=(), controls=("a control",)) -> pe.EvidenceDocument:
    return pe.parse_evidence(
        {
            "repo": "owner/repo",
            "pr": 7,
            "merge_commit": "deadbeef1234",
            "deploy": {"method": "content", "established": True, "detail": "present"},
            "claims": claims
            or [
                {
                    "claim": "it works",
                    "verdict": "pass",
                    "tier": "MEASURED",
                    "measurement": "ran x.py: ok",
                }
            ],
            "controls": list(controls),
            "gaps": [dict(g) for g in gaps],
            "findings": [],
            "tracking": [dict(t) for t in tracking],
        }
    )


def _gate(trigger: str) -> dict:
    return {
        "kind": "event_gated",
        "detail": "needs the weekly run",
        "trigger": trigger,
        "recheck": "gh run list",
    }


def _decision(doc):
    d = pe.decide(doc, park=False, asserted=None, note=None)
    if isinstance(d, pe.Refusal):  # an open verdict needs a note
        d = pe.decide(doc, park=False, asserted=None, note="open")
    assert not isinstance(d, pe.Refusal)
    return d


# ---- the date refusal ------------------------------------------------------


@pytest.mark.parametrize("trigger", ["2026-11-13", "2026-11-14"])
def test_a_closing_document_with_a_date_gate_already_come_is_refused(trigger):
    doc = _doc(gaps=[_gate(trigger)])
    decision = _decision(doc)
    assert decision.closing
    refusal = pe.check_event_dates(doc, decision.closing, TODAY)
    assert isinstance(refusal, pe.Refusal) and refusal.code == 2
    assert f"gaps/0 (trigger {trigger})" in refusal.message
    assert "first date on which it will have" in refusal.message


def test_a_future_date_gate_is_not_refused():
    doc = _doc(gaps=[_gate("2026-11-15")])
    assert pe.check_event_dates(doc, _decision(doc).closing, TODAY) is None


def test_a_named_event_trigger_is_never_refused():
    doc = _doc(gaps=[_gate("the next weekly run")])
    assert pe.check_event_dates(doc, _decision(doc).closing, TODAY) is None


def test_a_verdict_that_keeps_the_row_open_is_not_refused():
    failing = [
        {
            "claim": "it works",
            "verdict": "fail",
            "tier": "MEASURED",
            "measurement": "ran x.py: broke",
        }
    ]
    doc = _doc(claims=failing, gaps=[_gate("2026-11-01")])
    decision = _decision(doc)
    assert not decision.closing
    assert pe.check_event_dates(doc, decision.closing, TODAY) is None


def test_every_due_gate_is_named_not_only_the_first():
    doc = _doc(gaps=[_gate("2026-11-01"), _gate("2026-12-01"), _gate("2026-11-10")])
    refusal = pe.check_event_dates(doc, _decision(doc).closing, TODAY)
    assert "gaps/0 (trigger 2026-11-01)" in refusal.message
    assert "gaps/2 (trigger 2026-11-10)" in refusal.message
    assert "gaps/1" not in refusal.message


# ---- the prompts -------------------------------------------------------------


def test_a_clean_document_raises_no_prompt():
    assert pe.completeness_prompts(_doc()) == []


def test_every_method_gap_is_asked_whether_its_step_is_reachable():
    doc = _doc(
        gaps=[
            {"kind": "method", "detail": "a"},
            {"kind": "owner_only", "detail": "b"},
            {"kind": "method", "detail": "c"},
        ]
    )
    prompts = pe.completeness_prompts(doc)
    assert [p.split(" ")[0] for p in prompts] == ["gaps/0", "gaps/2"]
    assert all("not_yet_done" in p for p in prompts)


@pytest.mark.parametrize(
    ("claim", "measurement", "asked"),
    [
        ("output is otherwise unchanged", "ran it: same lines", True),
        ("the old path still works", "ran it: exit 0", True),
        ("it no longer leaks", "ran the probe: 0 rows", True),
        ("output is otherwise unchanged", "on the parent tree and the merge: identical", False),
        ("the old path still works", "before the change and after: exit 0", False),
        ("it adds a flag", "ran it: exit 0", False),
        ("the stillness check fires", "ran it: fired", False),
    ],
)
def test_an_unchanged_claim_is_asked_for_its_before_arm(claim, measurement, asked):
    doc = _doc(
        claims=[{"claim": claim, "verdict": "pass", "tier": "MEASURED", "measurement": measurement}]
    )
    prompts = pe.completeness_prompts(doc)
    assert any(p.startswith("claims/0 says something is unchanged") for p in prompts) is asked


@pytest.mark.parametrize(
    ("measurement", "asked"),
    [
        ("tests/a/test_one.py::test_x and tests/b/test_two.py::test_y pass", True),
        ("test_one.py::test_x and test_one.py::test_z pass", False),
        ("test_one::test_x and test_two::test_y pass", True),
        ("the probe ran", False),
    ],
)
def test_a_claim_citing_two_test_modules_is_asked_whether_it_folds_two(measurement, asked):
    doc = _doc(
        claims=[
            {"claim": "it works", "verdict": "pass", "tier": "MEASURED", "measurement": measurement}
        ]
    )
    prompts = pe.completeness_prompts(doc)
    assert any("two or more test modules" in p for p in prompts) is asked


@pytest.mark.parametrize(
    ("rationale", "asked"),
    [
        ("6/6 parallel CI passes cover it", True),
        ("passed 4 of 4 runs", True),
        ("3 of 5 runs passed, so it is flaky; tracked", False),
        ("the step needs the owner's device", False),
    ],
)
def test_an_accepted_limit_resting_on_a_pass_count_is_questioned(rationale, asked):
    doc = _doc(
        gaps=[{"kind": "method", "detail": "only CI"}],
        tracking=[{"kind": "accepted_limit", "target": "gaps/0", "rationale": rationale}],
    )
    prompts = pe.completeness_prompts(doc)
    assert any(p.startswith("tracking/0 accepts a limit on a pass count") for p in prompts) is asked


def test_prompts_never_change_the_derived_verdict():
    plain = _doc(controls=("a control",))
    prompted = _doc(
        claims=[
            {
                "claim": "otherwise unchanged",
                "verdict": "pass",
                "tier": "MEASURED",
                "measurement": "ran",
            }
        ],
        controls=("a control",),
    )
    assert pe.completeness_prompts(prompted)
    assert _decision(plain).verdict == _decision(prompted).verdict
