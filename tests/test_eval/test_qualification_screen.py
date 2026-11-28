"""Synthetic judge-screen strata, fixed selection and early-stop policy."""

from copy import deepcopy

import pytest

from genesis.eval.qualification import screen
from genesis.eval.qualification.__main__ import parser
from genesis.eval.qualification.evidence import Incomplete, digest
from tests.test_eval.qualification_fixtures import small_corpus


@pytest.fixture
def checked_corpus():
    cases = small_corpus(per_class=6)
    plan = {
        name: {case["id"]: {"prompt_hash": digest(case["id"])} for case in rows}
        for name, rows in cases.items()
    }
    return cases, {"complete": True, "corpus_hash": digest(cases), "plan": plan}


def test_selection_reproduces_frozen_rank(checked_corpus):
    cases, checked = checked_corpus
    picked = screen.select(cases, checked)
    assert len(picked) == len({(c.contract, c.case_id) for c in picked}) == 30
    assert picked[0].contract == "j9_relevance"
    assert picked[0].case_id == "relevance-False2"
    for name in screen.CONTRACTS:
        for label in (False, True):
            assert sum((c.contract, c.label) == (name, label) for c in picked) == 5
    report = screen.check(cases, checked, screen.sample_digest(picked))
    assert report["status"] == "pass" and report["selected_cases"] == 30
    assert all(c.case_id not in str(report) for c in picked)


def test_check_rejects_changed_render_or_reference(checked_corpus):
    cases, checked = checked_corpus
    picked = screen.select(cases, checked)
    original = screen.sample_digest(picked)
    changed = deepcopy(checked)
    changed["plan"][picked[0].contract][picked[0].case_id]["prompt_hash"] = "a" * 64
    with pytest.raises(Incomplete, match="sample changed"):
        screen.check(cases, changed, original)
    cases[picked[0].contract].pop()
    with pytest.raises(Incomplete, match="complete admission"):
        screen.check(cases, checked, original)


def test_parser_requires_frozen_selection():
    args = ["screen-check", "--corpus", "corpus", "--temp-root", "tmp"]
    with pytest.raises(SystemExit):
        parser().parse_args(args)
    assert parser().parse_args([*args, "--sample-sha256", "a" * 64]).sample_sha256 == "a" * 64
