"""ACE-style provenance for skill_evolution: every mutation records *why*.

The cognitive_file_modifications ledger previously stored only
{skill_name, change_size, confidence}, dropping the triggering signals
(failure patterns + the SkillReport trend/usage that prompted the refinement).
"""

from __future__ import annotations

from genesis.learning.skills.applicator import SkillApplicator
from genesis.learning.skills.refiner import SkillRefiner
from genesis.learning.skills.types import (
    ChangeSize,
    SkillProposal,
    SkillReport,
    SkillTrend,
)


def _declining_report() -> SkillReport:
    return SkillReport(
        skill_name="demo",
        usage_count=12,
        success_count=6,
        failure_count=6,
        success_rate=0.5,
        baseline_success_rate=0.8,
        failure_patterns=["timeout", "truncation"],
        trend=SkillTrend.DECLINING,
    )


def test_skill_proposal_provenance_defaults_to_none():
    proposal = SkillProposal(
        skill_name="s",
        proposed_content="c",
        rationale="r",
        change_size=ChangeSize.MINOR,
    )
    assert proposal.provenance_trace is None


def test_build_provenance_summarizes_the_trigger_signals():
    trace = SkillRefiner()._build_provenance(_declining_report())
    # The trace must capture *why* this skill was picked for refinement.
    assert "declining" in trace.lower()
    assert "timeout" in trace
    assert "12" in trace  # usage count
    assert "50" in trace or "0.5" in trace  # success rate


def test_parse_response_attaches_provenance_when_supplied():
    text = (
        '{"proposed_content": "x", "rationale": "y", "change_size": "minor", '
        '"confidence": 0.9, "failure_patterns_addressed": ["timeout"]}'
    )
    proposal = SkillRefiner()._parse_response(
        "demo", text, provenance_trace="usage=12 trend=declining"
    )
    assert proposal is not None
    assert proposal.provenance_trace == "usage=12 trend=declining"


def test_parse_response_provenance_is_optional_backward_compatible():
    # Existing callers pass only (skill_name, text) — must still work.
    text = '{"proposed_content": "x", "rationale": "y", "change_size": "minor"}'
    proposal = SkillRefiner()._parse_response("demo", text)
    assert proposal is not None
    assert proposal.provenance_trace is None


def test_modification_metadata_includes_provenance_and_failure_patterns():
    proposal = SkillProposal(
        skill_name="s",
        proposed_content="c",
        rationale="r",
        change_size=ChangeSize.MINOR,
        confidence=0.8,
        failure_patterns_addressed=["timeout"],
        provenance_trace="usage=12 trend=declining",
    )
    metadata = SkillApplicator._build_modification_metadata(proposal)
    assert metadata["skill_name"] == "s"
    assert metadata["change_size"] == "minor"
    assert metadata["confidence"] == 0.8
    assert metadata["provenance_trace"] == "usage=12 trend=declining"
    assert metadata["failure_patterns_addressed"] == ["timeout"]
