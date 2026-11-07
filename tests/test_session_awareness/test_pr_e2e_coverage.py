"""Declaration identities and existing verdict policy, with no semantic matching."""

import pytest

from genesis.session_awareness import e2e_declaration as e2e
from genesis.session_awareness import pr_evidence as pe
from tests.test_session_awareness.test_pr_evidence import GAP, _doc, _raw

STEP = "restart the timer and measure the next scheduled run"
OTHER = "check the owner device after the next scheduled run"


@pytest.mark.parametrize("body,steps", [
    ("", []),
    ("E2E: none — documentation only", []),
    ("E2E: <one-line plan for the post-merge verification>", []),
    (f"- **e2e**: {STEP}\r\n", [STEP]),
    (f"```text\nE2E: {OTHER}\n```\nE2E: {STEP}", [STEP]),
    (f"<!--\nE2E: {OTHER}\n-->\nE2E: {STEP}", [STEP]),
    (f"E2E: {STEP}\nE2E: {OTHER}", [STEP, OTHER]),
    (f"E2E: {STEP}\nE2E: none — documentation only", [STEP]),
    (f"E2E: {STEP}\nE2E: {STEP}", [STEP]),
])
def test_all_visible_declarations_use_the_existing_parser(body, steps):
    assert e2e.e2e_steps(body) == steps


@pytest.mark.parametrize("mapping", [
    {"step": STEP}, {"step": STEP, "claim": 0, "gap": 0},
    {"step": STEP, "claim": True}, {"step": STEP, "claim": "0"},
    {"step": STEP, "claim": 0.0}, {"step": STEP, "claim": -1},
    {"step": STEP, "claim": 1}, {"step": STEP, "gap": 0},
    {"step": " ", "claim": 0}, {"step": STEP, "claims": [0]},
    {"step": STEP, "claim": None}, {"step": STEP, "claim": 0, "extra": 1},
])
def test_mapping_is_strict_and_targets_must_exist(mapping):
    with pytest.raises(pe.EvidenceError, match="e2e"):
        pe.parse_evidence(_raw(e2e=[mapping]))


def test_old_documents_parse_but_cannot_close_over_an_unmapped_plan():
    doc = _doc()
    assert doc.e2e == []
    refusal = pe.check_e2e_coverage(doc, f"E2E: {STEP}")
    assert refusal.code == 2 and STEP in refusal.message
    assert pe.check_e2e_coverage(doc, "E2E: none — documentation only") is None


@pytest.mark.parametrize("suffix", [chr(i) for i in range(0x110000) if chr(i).isspace()])
def test_copying_a_declaration_with_unicode_whitespace_can_close(suffix):
    body = f"E2E: {STEP}{suffix}"
    parsed = e2e.parse_e2e(body)["text"]
    doc = _doc(e2e=[{"step": parsed, "claim": 0}])
    assert pe.check_e2e_coverage(doc, body) is None


def test_visible_values_are_not_interpreted_as_markdown_a_second_time():
    body = "E2E: <!<!-- example -->-- restart and measure timer"
    declaration = e2e.parse_e2e(body)
    assert declaration["kind"] == "plan"
    assert e2e.e2e_steps(body) == [declaration["text"]]
    refusal = pe.check_e2e_coverage(_doc(), body)
    assert refusal is not None and declaration["text"] in refusal.message
    assert pe.check_e2e_coverage(_doc(e2e=[{"step": declaration["text"], "claim": 0}]), body) is None


@pytest.mark.parametrize("mapped", [STEP, OTHER])
def test_every_declaration_requires_coverage_and_a_later_none_cannot_erase_it(mapped):
    doc = _doc(e2e=[{"step": mapped, "claim": 0}])
    refusal = pe.check_e2e_coverage(doc, f"E2E: {STEP}\nE2E: {OTHER}\nE2E: none — docs only")
    assert (OTHER if mapped == STEP else STEP) in refusal.message


def test_matching_is_exact_and_stale_mappings_refuse():
    doc = _doc(e2e=[{"step": OTHER, "claim": 0}])
    refusal = pe.check_e2e_coverage(doc, f"E2E: {STEP}")
    assert STEP in refusal.message and OTHER in refusal.message
    assert "not a declared E2E step" in pe.check_e2e_coverage(doc, "").message


@pytest.mark.parametrize("kind", pe.GAP_KINDS)
def test_mapping_gap_keeps_its_existing_verdict_and_event_recheck(kind):
    gap = GAP[kind]
    doc = _doc(gaps=[gap], e2e=[{"step": STEP, "gap": 0}])
    assert pe.check_e2e_coverage(doc, f"E2E: {STEP}") is None
    assert pe.derive(doc).verdict == (
        "not-yet-done" if kind == "not_yet_done" else "pass-with-measured-gaps"
    )
    assert bool(pe.rechecks_for(doc)) == (kind == "event_gated")


@pytest.mark.parametrize("verdict,tier,expected", [
    ("pass", "MEASURED", "pass-mechanical"),
    ("pass", "READ", "pass-mechanical"),
    ("pass", "INFERRED", "cannot-verify"),
    ("unverified", "NOT_VERIFIABLE_HERE", "cannot-verify"),
    ("fail", "MEASURED", "fail-intent"),
    ("fail", "INFERRED", "cannot-verify"),
])
def test_mapping_never_promotes_claim_tiers(verdict, tier, expected):
    doc = _doc(claims=[(verdict, tier)], e2e=[{"step": STEP, "claim": 0}])
    assert pe.check_e2e_coverage(doc, f"E2E: {STEP}") is None
    assert pe.derive(doc).verdict == expected
