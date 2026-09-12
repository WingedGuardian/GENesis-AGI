"""Tests for scripts/hooks/gate_demand.py — remedy-coverage semantics.

The storage half of a gate demand lives in review_state.py; this module answers
one question: does a question actually OFFER the declared remedies?

The rule is a BIJECTION, not a count — this question's options are the remedy
set, one each and nothing else. Three counting rules were defeated before it,
each narrower than the last, because the session writes the option text and no
count over those words closes the gap. The cases below include all four attacks
that broke the last of them, and the honest-menu case that it false-blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
import gate_demand as gd  # noqa: E402

REMEDIES = [
    {"key": "redesign", "label": "robust-by-construction redesign"},
    {"key": "narrow", "label": "narrow the scope"},
    {"key": "shelve", "label": "shelve the change"},
]


def _q(*labels: str, question: str = "How should we proceed?") -> dict:
    return {
        "question": question,
        "header": "Cap",
        "multiSelect": False,
        "options": [{"label": lb, "description": ""} for lb in labels],
    }


class TestMissingRemedies:
    def test_a_question_offering_all_three_is_complete(self):
        q = _q("Redesign it", "Narrow the scope", "Shelve it")
        assert gd.missing_remedies([q], REMEDIES) == []

    def test_the_measured_failure_is_caught(self):
        """2026-08-31: the relay dropped 'redesign', invented 'split the PR', and
        added 'ship as-is' — the outcome the cap exists to prevent."""
        q = _q("Split the PR", "Ship as-is", "Narrow the scope", "Shelve it")
        assert gd.missing_remedies([q], REMEDIES) == ["redesign"]

    def test_an_empty_question_list_covers_nothing(self):
        assert gd.missing_remedies([], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_remedies_spread_across_questions_do_not_count(self):
        """Splitting one decision into several questions presents them as
        independent choices, which is the corruption in another shape. ONE
        question must carry the whole set."""
        qs = [_q("Redesign it"), _q("Narrow the scope"), _q("Shelve it")]
        # Reported against the question that covers the MOST, so the message can
        # name what to add rather than restating the whole set.
        assert gd.missing_remedies(qs, REMEDIES) == ["narrow", "shelve"]

    def test_other_questions_may_ride_alongside(self):
        """The standing rule mandates >=2 questions per call, so an unrelated
        question sharing the call must never make the gate refuse."""
        qs = [_q("Yes", "No", question="Unrelated?"), _q("Redesign it", "Narrow it", "Shelve it")]
        assert gd.missing_remedies(qs, REMEDIES) == []

    def test_the_description_does_NOT_carry_a_remedy(self):
        """Superseded expectation, kept inverted rather than deleted.

        Descriptions used to count. They no longer do, and the reason is the
        bypass it enabled: "Ship as-is / neither shelve nor rework" counted as
        offering `shelve`. The cost is real and accepted — a menu that names its
        remedies only in prose is now refused, and the refusal says to put the
        name in the label.
        """
        q = {
            "question": "How?",
            "options": [
                {"label": "Rebuild from the ground up", "description": "a redesign"},
                {"label": "Cut it down", "description": "narrow the change"},
                {"label": "Park it", "description": "shelve for now"},
            ],
        }
        assert gd.missing_remedies([q], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_matching_is_word_bounded_not_substring(self):
        """A slug buried inside a longer word is not the remedy.

        Substring matching would silently widen the gate: 'narrowly' and
        'shelved' would pass, and so would any option that happens to contain
        the letters. The check exists to prove the remedy was OFFERED.
        """
        q = _q("Redesigning", "Narrowly scoped", "Shelved already")
        # 'Redesigning' and 'Shelved' are longer words; 'Narrowly' likewise.
        assert gd.missing_remedies([q], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_matching_is_case_insensitive(self):
        q = _q("REDESIGN", "Narrow", "shelve")
        assert gd.missing_remedies([q], REMEDIES) == []

    def test_malformed_questions_are_ignored_not_crashed_on(self):
        """The payload is harness-shaped and may drift; a shape surprise must
        degrade to 'not covered', never to a traceback in a PreToolUse hook."""
        for bad in ("string", 42, None, {"options": "not a list"}, {"options": [7]}):
            assert gd.missing_remedies([bad], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_no_remedies_means_nothing_is_missing(self):
        assert gd.missing_remedies([_q("x")], []) == []


class TestCoverageIsABijection:
    """One option naming every remedy offers none of them.

    The defect that made the guard useless on its own acceptance case. Testing
    each remedy independently against the shared option pool means a single
    option can satisfy all of them at once — including an option that names them
    in order to DISMISS them, which is the measured corruption verbatim.
    """

    def test_one_option_dismissing_every_remedy_is_not_coverage(self):
        q = {
            "question": "How should we proceed?",
            "options": [
                {
                    "label": "Ship as-is",
                    "description": "rather than redesign, narrow, or shelve",
                },
                {"label": "Keep going", "description": ""},
            ],
        }
        assert gd.missing_remedies([q], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_one_option_listing_every_remedy_is_not_coverage(self):
        """It binds ONE remedy at most, so the others are unoffered and it is
        refused — which is the outcome that matters. (It is not reported as all
        three missing: the option legitimately names `redesign` in its label, so
        that one binds; `narrow` and `shelve` have nowhere left to go.)"""
        assert gd.missing_remedies(
            [_q("redesign narrow shelve — pick later", "Ship as-is")], REMEDIES
        ) == ["narrow", "shelve"]

    def test_an_option_naming_two_remedies_binds_only_one(self):
        """Ambiguity leaves a remedy unoffered — the user cannot pick the other."""
        q = _q("Redesign or narrow it", "Shelve it")
        assert gd.missing_remedies([q], REMEDIES) == ["narrow"]

    def test_the_two_step_bypass_is_closed(self):
        """Adding the options a refusal names must not launder the dismissal.

        Under distinct-option matching the dismissal absorbs one remedy, so this
        menu passed with that remedy never genuinely offered.
        """
        q = _q(
            "Ship as-is",
            "Narrow the scope",
            "Shelve it",
        )
        q["options"][0]["description"] = "rather than redesign, narrow, or shelve"
        assert gd.missing_remedies([q], REMEDIES) == ["redesign"]

    def test_distinct_options_still_pass(self):
        """The control: injectivity must not break the ordinary compliant ask."""
        assert gd.missing_remedies(
            [_q("Redesign it", "Narrow the scope", "Shelve it")], REMEDIES
        ) == []

    def test_an_extra_option_is_REFUSED(self):
        """The acceptance bar, and the case three earlier rules all passed.

        The measured 2026-08-31 relay was three things: a remedy dropped, one
        invented, and "ship as-is" ADDED. Every presence-based rule caught only
        the first, because an extra option is invisible to a test that asks
        whether the remedies are there. This test used to assert the opposite —
        that extra options are fine — with "Something else" standing in for the
        option that makes it a bypass.
        """
        missing = gd.missing_remedies(
            [_q("Redesign it", "Narrow the scope", "Shelve it", "Ship as-is")], REMEDIES
        )
        assert missing, "an unsanctioned option must not pass"
        assert "Ship as-is" in missing[0]

    def test_the_refusal_names_the_option_that_was_not_offered(self):
        missing = gd.missing_remedies(
            [_q("Redesign it", "Narrow the scope", "Shelve it", "Split the PR")], REMEDIES
        )
        assert missing == ["options this gate did not offer: Split the PR"]

    def test_an_option_matching_only_via_its_DESCRIPTION_is_not_an_offer(self):
        """A description is prose ABOUT an option; the label is what is picked.

        Matching descriptions let "Ship as-is / neither shelve nor rework" count
        as offering `shelve` — a mention read as an offer.
        """
        q = {
            "question": "How?",
            "options": [
                {"label": "Redesign it", "description": ""},
                {"label": "Narrow the scope", "description": ""},
                {"label": "Ship as-is", "description": "neither shelve nor rework"},
            ],
        }
        assert gd.missing_remedies([q], REMEDIES) == ["shelve"]

    def test_a_menu_of_pure_negations_offers_nothing(self):
        q = {
            "question": "How?",
            "options": [
                {"label": "Ship as-is", "description": "not a redesign"},
                {"label": "Keep going", "description": "we will not narrow"},
                {"label": "Merge now", "description": "no need to shelve"},
            ],
        }
        assert gd.missing_remedies([q], REMEDIES) == ["redesign", "narrow", "shelve"]

    def test_an_honest_menu_whose_descriptions_mention_siblings_PASSES(self):
        """The false-block direction, which matters as much as the bypass.

        A real menu explains each option by contrast — "rebuild rather than
        narrow the fix". Under exactly-one-mention counting that was refused.
        """
        q = {
            "question": "How?",
            "options": [
                {"label": "Redesign it", "description": "rebuild rather than narrow the fix"},
                {"label": "Narrow the scope", "description": "ship the converging part, shelve the rest"},
                {"label": "Shelve it", "description": ""},
            ],
        }
        assert gd.missing_remedies([q], REMEDIES) == []
