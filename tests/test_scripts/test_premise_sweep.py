"""The premise-sweep harness, and above all its REFUSAL.

`scripts/premise_sweep.py` exists because the method it encodes was already
written down and written-down method is what failed: a session with the
"Measure, Do Not Choose" doctrine loaded still built a harness whose hostile
states were inert. What caught that was a no-op arm behaving unexpectedly, and
that check is mechanical.

So the property under test is not "the tool sweeps". It is that the tool
WITHHOLDS THE RESULTS TABLE when its own controls do not hold. A warning
printed above a table is not the same thing — a reader skims to the table they
came for — which is why every refusal test asserts the table's ABSENCE rather
than the warning's presence.

The second property, added after round 1 of review: the tool must not TRUST THE
SPEC about anything that decides whether the instrument is sound. Every finding
in that round was one instance of it — a control whose polarity the spec
declared, a control run at one point of a multi-axis space, execution outcomes
sharing a namespace with classifications, axis domains and templates and
regexes taken on faith. The tests below are organised by the doctrine's three
clauses rather than by that findings list, because the list was a sample.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SWEEP = Path(__file__).resolve().parents[2] / "scripts" / "premise_sweep.py"


def _spec(**over):
    """A minimal, self-contained spec: one axis, no fixtures, no shell hazards.

    `echo` keeps the cells honest about what is being tested — the harness's
    control logic, not anyone's shell semantics. With only a candidate axis the
    environmental cross product is a single empty cell, which is the degenerate
    case the swept-control logic must still handle.
    """
    spec = {
        "question": "does the harness honour its own controls?",
        "axes": {"arm": ["good", "bad"]},
        "candidate_axis": "arm",
        "cell": "echo {arm}",
        "classify": {"GOOD": "^good", "BAD": "^bad"},
        "predicate": "GOOD",
        "controls": {"oracle": "good", "noop": "bad"},
        "decision_rule": "adoptable iff every cell is GOOD",
        "no_pass_disposition": "abandon the approach and re-open the design",
    }
    spec.update(over)
    return spec


def _two_axis(**over):
    """Candidate axis `arm` x environmental axis `env`, so controls must sweep.

    `env=live` echoes the arm through unchanged; `env=inert` forces `good`
    whatever the arm is, which is a cell where the hazard cannot be reproduced.
    """
    spec = _spec(
        axes={"arm": ["good", "bad"], "env": ["live", "inert"]},
        cell='if [ "{env}" = inert ]; then echo good; else echo {arm}; fi',
    )
    spec.update(over)
    return spec


def _run(tmp_path: Path, spec: dict, *args: str) -> subprocess.CompletedProcess:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SWEEP), str(p), *args], capture_output=True, text=True, timeout=120
    )


# ---------------------------------------------------------------- clause 1:
# enumerate the space, do not pick cases from it


def test_a_sound_spec_prints_the_matrix(tmp_path):
    """Positive control. Without it, a tool that refused EVERYTHING would pass
    every other test in this file."""
    r = _run(tmp_path, _spec())
    assert "=== RESULTS ===" in r.stdout
    assert "CONTROLS HELD" in r.stdout
    assert r.returncode == 1, "the no-op candidate is in the sweep and legitimately fails"
    assert "live cells=2" in r.stdout


def test_the_sweep_is_a_full_cross_product(tmp_path):
    """Axes in, cells enumerated — the author does not get to hand-pick, which
    is how a sweep ends up covering only the cases its author believed in."""
    spec = _spec(
        axes={"a": ["good", "bad"], "b": ["1", "2", "3"]},
        candidate_axis="a",
        cell="echo {a}{b}",
    )
    r = _run(tmp_path, spec)
    assert "live cells=6" in r.stdout, "2 x 3 axes must produce 6 cells, not a chosen subset"


def test_an_axis_with_no_values_is_a_SPEC_error(tmp_path):
    """The vacuous pass: controls pin concrete values so they still hold, the
    cross product is then empty, and the run reports zero non-matches and exits
    0 — a clean-looking result from a sweep that measured nothing."""
    r = _run(tmp_path, _spec(axes={"arm": ["good", "bad"], "extra": []}, cell="echo {arm}{extra}"))
    assert r.returncode == 3
    assert "NON-EMPTY list" in r.stderr


def test_an_axis_given_as_a_bare_string_is_a_SPEC_error(tmp_path):
    """A string is iterable, so `"env": "hostile"` would silently sweep seven
    single-character cells and report them as a real result."""
    r = _run(
        tmp_path, _spec(axes={"arm": ["good", "bad"], "env": "hostile"}, cell="echo {arm}{env}")
    )
    assert r.returncode == 3
    assert "NON-EMPTY list" in r.stderr


def test_an_axis_the_template_never_uses_is_a_SPEC_error(tmp_path):
    """An unsubstituted axis multiplies the cell count without changing what
    runs — the denominator grows while the evidence does not."""
    r = _run(tmp_path, _spec(axes={"arm": ["good", "bad"], "ghost": ["x", "y"]}))
    assert r.returncode == 3
    assert "never substituted" in r.stderr


def test_a_template_placeholder_that_is_not_an_axis_is_a_SPEC_error(tmp_path):
    """It used to raise KeyError mid-sweep. An uncaught exception exits 1, which
    this CLI documents as 'controls held; a real non-match' — so a broken spec
    was recordable as experimental evidence."""
    r = _run(tmp_path, _spec(cell="echo {arm}{nope}"))
    assert r.returncode == 3
    assert "not axes" in r.stderr


# ---------------------------------------------------------------- clause 2:
# pre-register the predicate, the rule, and what happens if nothing passes


@pytest.mark.parametrize("field", ["decision_rule", "no_pass_disposition", "predicate"])
def test_pre_registration_fields_are_REQUIRED_not_merely_recorded(tmp_path, field):
    """The first version printed `(none declared)` and ran anyway. A rule the
    reader can choose after seeing the table is a rationalisation with a
    timestamp, which is precisely what clause 2 exists to stop."""
    spec = _spec()
    del spec[field]
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "missing required field" in r.stderr
    assert "=== RESULTS ===" not in r.stdout


def test_the_decision_rule_is_echoed_and_NOT_graded(tmp_path):
    """Pre-registering the rule is the point; grading against it in code would
    move the judgement into a config file and out of the reader's view."""
    r = _run(tmp_path, _spec(decision_rule="adoptable iff pigs fly"))
    assert "adoptable iff pigs fly" in r.stdout
    assert "does not grade it for you" in r.stdout


def test_the_no_pass_disposition_is_shown_above_the_table(tmp_path):
    """It has to be readable before the results are, or it is not pre-registered."""
    r = _run(tmp_path, _spec(no_pass_disposition="drop the feature"))
    assert r.stdout.index("drop the feature") < r.stdout.index("=== RESULTS ===")


def test_a_predicate_no_rule_can_produce_is_a_SPEC_error(tmp_path):
    """A predicate outside the classify labels can never match, so every cell
    fails for a reason that has nothing to do with the question."""
    r = _run(tmp_path, _spec(predicate="NOPE"))
    assert r.returncode == 3
    assert "nothing could ever match it" in r.stderr


def test_an_invalid_regex_is_a_SPEC_error_not_a_crash(tmp_path):
    r = _run(tmp_path, _spec(classify={"GOOD": "^good", "BAD": "([unclosed"}))
    assert r.returncode == 3
    assert "not a valid regex" in r.stderr


# ---------------------------------------------------------------- clause 3:
# control the instrument


def test_a_noop_that_never_reproduces_the_hazard_VOIDS_the_run(tmp_path):
    """THE defect this tool was built for, replayed.

    MEASURED 2026-09-20: a CDPATH sweep set its hostile states non-exported, so
    they never reached the child process, the no-op arm passed, and every cell
    reported clean. The conclusion drawn from it — "unset works everywhere" —
    was false and would have shipped.
    """
    # The placeholder IS substituted and changes nothing — a truer replay of
    # the original defect than omitting it, which the unused-axis check catches.
    r = _run(tmp_path, _spec(cell="echo good  # {arm}"))
    assert r.returncode == 2
    assert "reproduces the hazard nowhere" in r.stdout
    assert "=== RESULTS ===" not in r.stdout, (
        "the table must be ABSENT, not merely preceded by a warning — a warning "
        "above a table is exactly what a reader skims past"
    )


def test_an_oracle_that_fails_in_ONE_environmental_cell_VOIDS_the_run(tmp_path):
    """The round-1 P1: a control run at a single point certifies the instrument
    at that point and nowhere else.

    MEASURED on this tool's own CDPATH run — the no-op arm pinned a relative
    invocation and said CONTROLS HELD while half the table ran under an absolute
    one the arm had never touched. The doctrine's "oracle must score 100%" is a
    claim about the whole space, and only a swept arm can check it.
    """
    spec = _two_axis(
        # env=broken makes even the oracle candidate classify BAD.
        axes={"arm": ["good", "bad"], "env": ["live", "broken"]},
        cell='if [ "{env}" = broken ]; then echo bad; else echo {arm}; fi',
    )
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "oracle FAILED at [env=broken]" in r.stdout
    assert "=== RESULTS ===" not in r.stdout


def test_a_control_arm_cannot_declare_its_own_expectation(tmp_path):
    """Polarity is closed BY CONSTRUCTION, not by validation.

    An earlier version let each arm carry `expect` and merely compared against
    it, so a no-op copied from the oracle — both selecting a passing cell, both
    expecting a pass — satisfied the check and printed "reproduced the hazard"
    having reproduced nothing. `expect` no longer exists, so there is no second
    place for the polarity to be wrong.
    """
    spec = _spec()
    spec["controls"] = {"oracle": {"axes": {"arm": "good"}, "expect": "GOOD"}, "noop": "bad"}
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "not a value of the candidate axis" in r.stderr


@pytest.mark.parametrize("arm", ["oracle", "noop"])
def test_a_control_must_name_a_value_the_sweep_actually_runs(tmp_path, arm):
    """A control outside the candidate axis is a code path the sweep never
    executes, which is how a control certifies a hazard nobody measured."""
    spec = _spec()
    spec["controls"][arm] = "not-a-candidate"
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "not a value of the candidate axis" in r.stderr


@pytest.mark.parametrize("arm", ["oracle", "noop"])
def test_both_control_arms_are_mandatory(tmp_path, arm):
    """A spec may not opt out of the check by omitting it — the polarity is
    allowlist, so a sweep without controls does not silently become a sweep
    whose controls trivially hold."""
    spec = _spec()
    del spec["controls"][arm]
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "must be exactly" in r.stderr


def test_a_candidate_axis_that_is_not_an_axis_is_a_SPEC_error(tmp_path):
    r = _run(tmp_path, _spec(candidate_axis="remedy"))
    assert r.returncode == 3
    assert "is not one of the axes" in r.stderr


def test_INERT_cells_are_named_and_excluded_rather_than_counted(tmp_path):
    """A cell where the no-op passes is one where the hazard does not exist, so
    no candidate earns credit for surviving it.

    MEASURED on this tool's own CDPATH run: 4 of 16 cells passed for EVERY
    candidate because an absolute `$0` makes `cd` ignore CDPATH entirely. The
    first version counted them, inflating every candidate's score with cells
    that tested nothing. Voiding the run instead would discard real evidence
    over conditions that were merely irrelevant.
    """
    r = _run(tmp_path, _two_axis())
    assert r.returncode == 1
    assert "INERT — 1 of 2 environmental cell(s) are EXCLUDED" in r.stdout
    assert "env=inert" in r.stdout
    assert "live cells=2" in r.stdout, "2 candidates x 1 live env cell, not 4"
    assert "excluding 1 inert" in r.stdout
    assert "env=inert" not in r.stdout.split("=== RESULTS ===")[1], (
        "an inert cell must not appear as a result row — that is the inflation"
    )


# ---------------------------------------------------------------- execution
# status is not a classification


def test_a_cell_that_prints_then_FAILS_is_not_a_pass(tmp_path):
    """`echo good; exit 42` used to classify as a clean GOOD, so a setup or
    assertion failure occurring AFTER an early result line produced evidence
    that read as success."""
    r = _run(tmp_path, _spec(cell="echo {arm}; exit 42"))
    assert r.returncode == 2, "even the oracle now fails, which correctly voids the run"
    assert "-> ERR" in r.stdout


def test_a_spec_may_DECLARE_a_nonzero_status_as_legitimate_data(tmp_path):
    """Refusing every nonzero exit would break probes whose subject legitimately
    refuses — the refusal IS the data. It must be declared, not assumed."""
    r = _run(tmp_path, _spec(cell="echo {arm}; exit 42", ok_exit_codes=[0, 42]))
    assert r.returncode == 1
    assert "CONTROLS HELD" in r.stdout
    assert "-> ERR" not in r.stdout


@pytest.mark.parametrize("reserved", ["ERR", "TIMEOUT"])
def test_a_spec_may_not_name_a_classification_after_an_execution_outcome(tmp_path, reserved):
    """With one namespace, a spec whose predicate was `ERR` counted every
    crashed command as a pass and exited 0."""
    spec = _spec(classify={"GOOD": "^good", reserved: "^bad"})
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "RESERVED for an execution" in r.stderr


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    """`subprocess.run`'s timeout kills only the direct child.

    MEASURED: a reproduced grandchild kept running and wrote a fixture file
    after the harness had moved on, so a timed-out probe could corrupt the cells
    that followed it. The probe here backgrounds a sleeper that writes a marker;
    if the group is killed, the marker never appears.
    """
    marker = tmp_path / "survivor.txt"
    spec = _spec(cell=f"(sleep 3; echo alive > {marker}) & sleep 3; echo {{arm}}")
    r = _run(tmp_path, spec, "--cell-timeout", "1")
    assert "-> TIMEOUT" in r.stdout
    assert r.returncode == 2, "a harness that only times out cannot measure anything"
    subprocess.run(["sleep", "4"], check=True)
    assert not marker.exists(), "a descendant outlived its cell and wrote to the fixture tree"


# ---------------------------------------------------------------- the remedy


def test_a_swept_remedy_reports_its_own_slice_of_the_table(tmp_path):
    """The measured case: the fix the author intends to recommend is an axis
    value, so the tool can say how it did rather than taking their word."""
    r = _run(tmp_path, _spec(proposed_remedy={"arm": "good"}))
    assert "=== PROPOSED REMEDY ===" in r.stdout
    assert "MEASURED in 1 of 2 live cells: 1 matched 'GOOD', 0 did not" in r.stdout
    assert "UNVERIFIED" not in r.stdout


def test_a_swept_remedy_that_FAILS_names_the_cell_it_failed_in(tmp_path):
    """The `./`-prefix case, replayed.

    MEASURED 2026-09-20: a premise check recommended prefixing `./` to a path
    that could be absolute, producing `.//abs/path`. The finding it rode on was
    sound, which is exactly what made the remedy credible. Had the remedy been
    an axis value, the absolute-path cell would have printed a non-match — so
    the tool must not summarise the slice to a count and hide which cell broke.
    """
    r = _run(tmp_path, _spec(proposed_remedy={"arm": "bad"}))
    assert "MEASURED in 1 of 2 live cells: 0 matched 'GOOD', 1 did not" in r.stdout
    assert "NOT GOOD: arm=bad" in r.stdout


def test_an_UNSWEPT_remedy_is_labelled_but_does_NOT_void_the_run(tmp_path):
    """The asymmetry this feature exists for, and the limit of the response.

    The instrument is sound and the finding is real — only the fix is
    unmeasured. Voiding would destroy good evidence over a separate claim and
    would teach authors to omit the field rather than declare it, so the table
    stays and the exit code is untouched.
    """
    r = _run(tmp_path, _spec(proposed_remedy={"arm": "prefix-with-dot-slash"}))
    assert "UNVERIFIED" in r.stdout
    assert "is not among the swept values" in r.stdout
    assert "=== RESULTS ===" in r.stdout, "the sweep's own finding survives an unmeasured remedy"
    assert r.returncode == 1, (
        "the exit code answers 'did the cells match?', which an unverified "
        "remedy does not change — folding both into one integer would force a "
        "lie whenever they disagree"
    )


def test_declaring_NO_remedy_is_reported_as_a_stated_absence(tmp_path):
    """The tool cannot know you have a fix in mind, so the omission is the
    escape hatch. It can at least make the omission visible instead of letting
    silence read as 'nothing to check here'."""
    r = _run(tmp_path, _spec())
    assert "none declared" in r.stdout
    assert "UNMEASURED unless it is one of" in r.stdout


def test_a_void_run_reports_no_remedy_verdict_either(tmp_path):
    """A verdict from a void instrument is the same lie as a matrix from one."""
    r = _run(tmp_path, _spec(cell="echo good  # {arm}", proposed_remedy={"arm": "good"}))
    assert r.returncode == 2
    assert "=== PROPOSED REMEDY ===" not in r.stdout


def test_a_remedy_naming_an_axis_that_does_not_exist_is_a_SPEC_error(tmp_path):
    """Exit 3, not `UNVERIFIED`: nothing could ever verify it, and a typo'd axis
    name reported as UNVERIFIED would read as an honest measurement gap."""
    r = _run(tmp_path, _spec(proposed_remedy={"remdy": "good"}))
    assert r.returncode == 3
    assert "which the sweep does not have" in r.stderr


@pytest.mark.parametrize("bad", ["good", {}, []])
def test_a_remedy_that_is_not_a_nonempty_mapping_is_a_SPEC_error(tmp_path, bad):
    """A bare string would have to be matched against every axis at once, and
    guessing which one the author meant is the ambiguity this tool refuses."""
    r = _run(tmp_path, _spec(proposed_remedy=bad))
    assert r.returncode == 3
    assert "non-empty {axis: value} mapping" in r.stderr


# ---------------------------------------------------------------- framing


def test_a_malformed_spec_is_distinguishable_from_a_void_run(tmp_path):
    """Exit 3, not 2: "your spec is broken" and "your instrument is broken" are
    different problems with different fixes, and collapsing them would hide the
    one this tool exists to surface."""
    r = _run(tmp_path, _spec(axes={"arm": []}))
    assert r.returncode == 3
    assert "SPEC ERROR" in r.stderr


def test_a_spec_that_is_not_an_object_is_rejected_without_a_traceback(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(_SWEEP), str(p)], capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 3
    assert "Traceback" not in r.stderr
