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
import os
import re
import signal
import subprocess
import sys
import time
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
    assert r.returncode == 0, (
        "the documented `0 = every live cell matched` must be REACHABLE. The "
        "no-op is required to be a candidate-axis value AND required by the "
        "controls not to match, so counting it made exit 1 the only possible "
        "outcome of a sound run — a status that is constant carries no "
        "information at all."
    )
    assert "live cells=2" in r.stdout
    assert "no-op CONTROL, not counted" in r.stdout, (
        "excluded from the COUNT, not from the TABLE — the negative control is "
        "evidence the reader should see, and dropping the row to fix the "
        "count would hide it"
    )


def test_a_REAL_candidate_failing_still_exits_1(tmp_path):
    """The other half of the exit contract, and the reason the four baselines
    above could move safely.

    Excluding the no-op from the count buys a reachable 0 — and a change that
    simply stopped counting anything would buy the same 0 while destroying the
    signal. This spec adds a THIRD candidate that is not a control and does
    not match, so 1 has to come back.
    """
    r = _run(
        tmp_path,
        _spec(
            axes={"arm": ["good", "bad", "alsobad"]},
            classify={"GOOD": "^good", "BAD": "^bad|^alsobad"},
        ),
    )
    assert r.returncode == 1, (
        "a non-control candidate did not match, which is precisely what exit "
        "1 is documented to mean"
    )
    assert "no-op CONTROL, not counted" in r.stdout
    assert "alsobad" in r.stdout


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
    assert r.returncode == 0, "no real candidate fails here; see the exit-contract test"
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
    assert r.returncode == 0, "no real candidate fails here; see the exit-contract test"
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
    assert r.returncode == 0, (
        "the exit code answers 'did the cells match?', which an unverified "
        "remedy does not change — folding both into one integer would force a "
        "lie whenever they disagree. The baseline is 0 rather than 1 only "
        "because the no-op control no longer counts as a failing cell; the "
        "property under test is that the remedy verdict did not move it."
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


# ---------------------------------------------------------------- a runaway
# cell must not take the harness (or the host) down with it


def test_a_runaway_cell_is_bounded_and_never_classified(tmp_path):
    """Unbounded capture is an OOM, not an untidiness.

    `yes` in a cell fills the capture buffer until the OOM killer takes the
    sweep — and on a swapless host, the machine, where the victim process is
    not necessarily the guilty one and gets no error. The reader stops at the
    cap and the cell is reported TRUNCATED.

    TRUNCATED is an outcome, never a category: `_classify` returns the first
    rule matching anywhere in the text, so a rule that would have matched past
    the cap is silently missed. Classifying a clipped stream would be a
    confident wrong answer, which is the one thing this tool may not produce.
    """
    spec = _spec(cell="yes good  # {arm}")
    r = _run(tmp_path, spec, "--cell-timeout", "10")
    assert r.returncode == 2, "the oracle cannot be measured, so the run is void"
    assert "TRUNCATED" in r.stdout
    assert "=== RESULTS ===" not in r.stdout


#: Measuring the sweep's peak memory needs a process whose child-rusage counter
#: STARTS AT ZERO. ``resource.getrusage(RUSAGE_CHILDREN).ru_maxrss`` is a monotonic
#: HIGH-WATER MARK over every child the calling process has ever reaped, so read
#: from the pytest process it reports the largest child ANY test in the session
#: spawned. MEASURED: after one unrelated 400 MiB child the counter stays at
#: 400 MiB for every small child that follows — it never falls back. That is not
#: hypothetical here: this test passed in a targeted run and failed in CI at
#: 1,005,096 KiB, attributed to a sweep that actually peaks near 16 MiB.
#: This wrapper is a FRESH process, so its counter covers the sweep alone — and it
#: still sees the sweep's own descendants, which is exactly what the bound is about
#: (MEASURED: a wrapper reports a grandchild's 400 MiB, not just a child's).
#: The peak goes to a FILE rather than a stream because the sweep writes to both.
_RSS_WRAPPER = (
    "import resource,subprocess,sys;"
    "peak=sys.argv[1];"
    "r=subprocess.run([sys.executable]+sys.argv[2:],capture_output=True,text=True);"
    "sys.stdout.write(r.stdout);sys.stderr.write(r.stderr);"
    "open(peak,'w').write(str(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss))"
)


def test_the_bound_holds_in_MEMORY_not_just_in_the_report(tmp_path):
    """The report saying TRUNCATED proves nothing about what was retained.

    A version that buffered everything and merely LABELLED it truncated would
    pass the test above while still being the OOM. This measures the peak RSS of
    the sweep process itself across a cell emitting far more than the cap.

    THE THRESHOLD IS DERIVED, NOT PICKED. The sweep peaks at 16,384-16,652 KiB on
    this cell (MEASURED, 3 runs), so 300 MiB leaves ~18x headroom for a slower or
    differently-configured runner. A version that retained the stream would hold
    the whole ``yes`` firehose for the cell's lifetime — orders of magnitude above
    the line, which is what makes the exact threshold uncritical in both
    directions. See ``_RSS_WRAPPER`` for why the reading cannot be taken here.
    """
    spec = _spec(cell="yes good  # {arm}")
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    peak_file = tmp_path / "peak_kb"
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _RSS_WRAPPER,
            str(peak_file),
            str(_SWEEP),
            str(p),
            "--cell-timeout",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert peak_file.exists(), (
        f"the RSS wrapper never wrote its reading, so nothing was measured; "
        f"wrapper stderr: {proc.stderr[-2000:]}"
    )
    peak_kb = int(peak_file.read_text())
    assert "TRUNCATED" in proc.stdout
    assert peak_kb < 300_000, (
        f"sweep peak RSS {peak_kb} KiB — a 1 MiB cap should not need "
        f"hundreds of MiB, so output is being retained past the bound"
    )


@pytest.mark.parametrize("field", ["decision_rule", "no_pass_disposition", "question"])
def test_a_pre_registration_field_that_is_EMPTY_is_a_SPEC_error(tmp_path, field):
    """Present is not supplied. An empty decision rule satisfies a presence
    check and prints as an empty DECISION RULE line above a real matrix —
    pre-registration in form and nothing in substance."""
    r = _run(tmp_path, _spec(**{field: "   "}))
    assert r.returncode == 3
    assert "present but empty" in r.stderr


# ------------------------------------------------- the spec is validated BY
# TYPE before any semantic test operates on it


def test_the_summary_counts_describe_DISJOINT_sets_that_sum_to_the_rows(tmp_path):
    """`matching` used to be DERIVED as `len(rows) - failed`.

    Once the no-op control stopped counting as a failure that subtraction
    silently credited the control's own non-match as a match — MEASURED on a
    live run that printed `matching=2` over a table containing exactly one
    match. A derived count agrees with its inputs by construction and so can
    never disagree loudly; this asserts the three numbers against the table
    they summarise instead.
    """
    r = _run(tmp_path, _two_axis())
    summary = next(ln for ln in r.stdout.splitlines() if ln.startswith("live cells="))
    total = int(re.search(r"live cells=(\d+)", summary).group(1))
    matching = int(re.search(r"matching=(\d+)", summary).group(1))
    not_matching = int(re.search(r"NOT-matching=(\d+)", summary).group(1))
    controls = int(re.search(r"\((\d+) no-op control row\(s\)", summary).group(1))
    assert matching + not_matching + controls == total, summary
    body = r.stdout.split("=== RESULTS ===")[1].split("live cells=")[0]
    rows = [ln for ln in body.splitlines() if "->" in ln]
    assert len(rows) == total, f"{total} claimed, {len(rows)} rows printed"


def test_a_MISTYPED_field_is_a_SPEC_error_not_a_crash(tmp_path):
    """Types before semantics, and the ordering is the fix.

    Every later check performs an OPERATION on a field — a membership test,
    `re.compile`, a set difference — and an operation on the wrong type does
    not return a problem string, it RAISES. A list-valued `candidate_axis`
    raised TypeError from `cand not in axes`, because a list is unhashable.
    This CLI documents exit 1 as "controls held; a real non-match", so a
    malformed spec crashed its way into being recordable as experimental
    evidence — the one outcome the whole tool exists to prevent.
    """
    r = _run(tmp_path, _spec(candidate_axis=["arm"]))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "candidate_axis must be a STRING" in r.stderr


def test_a_MISTYPED_classify_entry_is_a_SPEC_error_not_a_crash(tmp_path):
    """The same class one field over: a non-string pattern reaches
    `re.compile`, which raises TypeError rather than `re.error`, so the
    invalid-regex handler never sees it."""
    r = _run(tmp_path, _spec(classify={"GOOD": ["^good"]}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "string label to a string regex" in r.stderr


def test_a_REPEATED_axis_value_is_a_SPEC_error(tmp_path):
    """A repeated value runs the same cell twice and counts it twice, so it
    weights one arm of the sweep while the cell count still reads as the size
    of the space."""
    r = _run(tmp_path, _spec(axes={"arm": ["good", "good", "bad"]}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "repeats value(s)" in r.stderr


# ------------------------------------------- an execution outcome can never
# be a classification, and `other` is one too


def test_the_implicit_other_label_cannot_be_the_predicate(tmp_path):
    """`_classify` returns `other` when NO rule matched.

    Left unreserved, a spec could declare an `other` regex AND name `other`
    as its predicate — at which point output matching none of the author's
    own rules SATISFIES the thing being measured. The fallback means
    "unclassified", and unclassified can never be the finding.
    """
    r = _run(tmp_path, _spec(classify={"GOOD": "^good", "other": "^bad"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "RESERVED for an execution" in r.stderr


def test_a_CRASHED_no_op_does_not_certify_the_instrument(tmp_path):
    """The no-op arm exists to prove the harness can FAIL.

    Any non-RAN outcome fails `_matches` for the wrong reason, so a no-op
    that ERRORED used to read as "the arm correctly did not match" and
    certify the controls. A broken arm proves only that it can break.
    """
    r = _run(
        tmp_path,
        _spec(cell='if [ "{arm}" = bad ]; then exit 99; else echo good; fi'),
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "no-op arm did not RUN" in r.stdout + r.stderr
    assert "=== RESULTS ===" not in r.stdout, "a void run prints no matrix"


def test_output_that_cannot_be_DECODED_is_an_outcome_not_a_silent_prefix(tmp_path):
    """UnicodeDecodeError IS a ValueError.

    So the reader's broad handler caught it, kept the valid PREFIX, and handed
    that prefix to `_classify` as though it were the whole output — a rule
    that would have matched past the bad bytes was silently missed. That is
    the same silent-truncation failure the byte cap exists to make loud,
    arriving through a different door.
    """
    r = _run(tmp_path, _spec(cell="printf '{arm}\\n'; printf '\\377\\376'"))
    assert "UNDECODABLE" in r.stdout + r.stderr, r.stdout + r.stderr


# ------------------------------------------------------- process hygiene and
# the honesty of the remedy slice


def test_a_cell_that_exits_NORMALLY_still_has_its_descendants_reaped(tmp_path):
    """Before this, `_kill_group` ran only under `TimeoutExpired`.

    A cell that exited normally left its backgrounded descendants running,
    free to write into the cells that followed it — which is the corruption
    the timeout kill was added to prevent, reached by the ordinary path
    instead of the exceptional one.
    """
    pidfile = tmp_path / "descendant.pid"
    # The descendant must NOT inherit stdout. With it open the reader is still
    # running at its deadline, so the cell resolves UNFINISHED and is reaped by
    # THAT branch -- never by the `finally` this test is about. MEASURED: the
    # first version of this fixture passed with the `finally` body replaced by
    # `pass`, so it pinned nothing. Closing the descendant's stdio lets the
    # cell reach RAN, which is the path the reap is for.
    spec = _spec(cell=f"echo {{arm}}; (sleep 45 >/dev/null 2>&1 <&-) & echo $! > {pidfile}")
    r = _run(tmp_path, spec)
    assert "CONTROLS HELD" in r.stdout, (
        "guard-the-guard: the cell must reach RAN. If it resolves UNFINISHED "
        "the descendant is reaped by that branch instead, and this test stops "
        "exercising the `finally` its docstring is about"
    )
    assert pidfile.exists(), "guard-the-guard: the cell never recorded a descendant"
    pid = int(pidfile.read_text().strip())
    assert pid > 1, "guard-the-guard: a pid of 1 or 0 would make the check meaningless"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return  # reaped, which is the property
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)  # do not leak it out of the test either
    raise AssertionError(f"descendant {pid} survived the sweep")


def test_a_remedy_selecting_only_INERT_cells_is_UNVERIFIED_not_measured_in_zero(tmp_path):
    """Every declared value can be a real axis value and still select NO live
    cell, when the remedy pins an environmental value the controls excluded.

    The remedy names the CANDIDATE axis as well, because one that names only
    environmental values is now a spec error in its own right — it would
    select every candidate at once, the no-op control included, and call the
    aggregate a measurement.

    "MEASURED in 0 of N live cells" is then a measurement claim resting on
    nothing, and it reads as a pass because nothing failed. The distinction
    this tool exists for is measured-versus-asserted.
    """
    r = _run(tmp_path, _two_axis(proposed_remedy={"arm": "good", "env": "inert"}))
    assert "UNVERIFIED: the remedy selects NO live cell" in r.stdout, r.stdout
    assert "MEASURED in 0" not in r.stdout


# ----------------------------------------------- round 4: the things that made
# the tool LIE, as opposed to the things that merely handle bad input badly


def test_the_ORACLE_runs_even_in_a_cell_that_would_be_excluded_as_inert(tmp_path):
    """Excluding a cell is a finding about the HAZARD. It says nothing about
    the harness, and the two were being decided by one test.

    The no-op used to be checked first and `continue` past the oracle, so a
    cell where the no-op passes AND the known-good oracle fails was filed as
    inert while the run printed CONTROLS HELD — the instrument certifying
    itself in a cell where it demonstrably does not work. 'The oracle scores
    100%' is a claim about the whole space, so a cell the oracle never
    entered cannot be part of that evidence.
    """
    # env=broken: the no-op's own value prints GOOD (so the cell looks inert),
    # but the ORACLE prints nothing a rule matches, so the harness is dead here.
    spec = _spec(
        axes={"arm": ["good", "bad"], "env": ["live", "broken"]},
        cell=(
            'if [ "{env}" = broken ]; then '
            '  if [ "{arm}" = bad ]; then echo good; else echo WRECKED; fi; '
            "else echo {arm}; fi"
        ),
    )
    r = _run(tmp_path, spec)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "oracle FAILED" in r.stdout + r.stderr
    assert "excluded as inert" in r.stdout + r.stderr
    assert "CONTROLS HELD" not in r.stdout


def test_a_JSON_boolean_is_not_an_accepted_EXIT_CODE(tmp_path):
    """`bool` is a SUBCLASS of `int`, so `isinstance(True, int)` is True and
    the type gate waved `[true]` straight through.

    `True == 1`, so the set then accepted exit status 1 as success: an oracle
    that printed the predicate and exited 1 could certify the instrument and
    produce an exit-0 report. This is the type gate's own class, recurring
    because the gate was written against the obvious reading of `isinstance`.
    """
    r = _run(tmp_path, _spec(ok_exit_codes=[True]))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "a JSON boolean is not an exit status" in r.stderr


def test_a_remedy_must_name_the_CANDIDATE_axis(tmp_path):
    """A remedy is a claim about a candidate.

    One naming only environmental values selects every candidate in those
    cells — the no-op control included — and the report then aggregates them
    under a single verdict and calls it MEASURED.
    """
    r = _run(tmp_path, _two_axis(proposed_remedy={"env": "live"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "does not assign the candidate axis" in r.stderr


def test_a_NON_FINITE_cell_timeout_is_refused(tmp_path):
    """`type=float` accepts `inf` and `nan`, and with either `wait()` never
    reaches a deadline — so a hung cell wedges the sweep while the CLI's own
    help promises the cell will be killed. A promise the flag cannot keep is
    the same class as a status that cannot occur."""
    for bad in ("inf", "nan", "0"):
        r = _run(tmp_path, _spec(), "--cell-timeout", bad)
        assert r.returncode == 3, f"{bad}: {r.stdout}{r.stderr}"
        assert "finite positive number of seconds" in r.stderr


def test_a_USAGE_error_is_not_reported_as_a_failed_CONTROL(tmp_path):
    """Status 2 is documented as CONTROLS FAILED — a real experimental
    outcome — and 3 as malformed input.

    argparse exits 2 on a usage error by default, which made a non-numeric
    timeout indistinguishable from a sweep whose instrument failed its own
    control. That is the one distinction these exit codes exist to carry.
    """
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec()), encoding="utf-8")
    r = subprocess.run(
        [sys.executable, str(_SWEEP), str(spec_path), "--cell-timeout", "not-a-number"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 3, r.stdout + r.stderr
    assert "CONTROLS FAILED" not in r.stdout


def test_the_EVIDENCE_file_is_REPLACED_rather_than_truncated_in_place(tmp_path):
    """`write_text` truncates before it writes, so an interrupted write over a
    previous run's evidence destroyed it and left a short file that still
    reads as a complete report.

    Asserted on the INODE, which is the observable difference between the two
    implementations and the reason this test is not vacuous: writing over a
    path in place keeps the inode, while writing a sibling and renaming makes
    a new one (MEASURED both ways). An earlier version of this test checked
    only that the content was replaced and no temp file was left behind —
    both of which the truncating version also satisfies, so it passed against
    the unfixed code and proved nothing.

    What it still does NOT prove is the crash path itself: that the previous
    evidence survives a signal mid-write. Reaching that needs a race this
    suite should not run. The inode is the mechanism that makes the crash
    path safe, so it is the honest thing to pin.
    """
    ev = tmp_path / "evidence.txt"
    r1 = _run(tmp_path, _spec(), "--evidence", str(ev))
    assert "=== RESULTS ===" in ev.read_text(encoding="utf-8"), r1.stdout
    first_inode = os.stat(ev).st_ino
    _run(tmp_path, _two_axis(), "--evidence", str(ev))
    assert os.stat(ev).st_ino != first_inode, (
        "the evidence file was written over IN PLACE — there is a window in "
        "which the previous run's report is already destroyed and the new one "
        "is not yet written, and a short file from that window still reads as "
        "a complete report"
    )
    assert "=== RESULTS ===" in ev.read_text(encoding="utf-8")
    leftovers = [p.name for p in tmp_path.iterdir() if ".partial." in p.name]
    assert not leftovers, f"temporary evidence files left behind: {leftovers}"


def test_a_reader_still_RUNNING_at_its_deadline_yields_no_classification(tmp_path):
    """The shell exits, but a background descendant keeps stdout open.

    `reader.join()` then returns with the reader alive, and what is buffered
    is a PREFIX of unknown completeness. Classifying it reads the first
    matching rule in a partial text, so a cell that prints the predicate and
    then contradicts itself passes — the same silent-truncation class as the
    byte cap and the decode error, and it gets the same treatment: an
    outcome, not a classification.

    The no-op arm reaches this first, and a no-op that did not RUN cannot
    certify the instrument, so the run is VOID rather than a matrix.
    """
    r = _run(tmp_path, _spec(cell="echo {arm}; (sleep 30) &"))
    assert r.returncode == 2, r.stdout + r.stderr
    assert "UNFINISHED" in r.stdout + r.stderr, r.stdout + r.stderr
    assert "=== RESULTS ===" not in r.stdout


# ------------------------------------- from the mandated fresh-context audit


def test_a_control_arm_that_CONTRADICTS_the_banner_voids_the_run(tmp_path):
    """The BLOCKER, and it was the tool's founding defect reproduced inside it.

    The control arms are swept TWICE — once by `_check_controls`, which prints
    the banner, and again as ordinary candidate rows. Nothing compared the two,
    so a cell carrying state from the first pass could print `CONTROLS HELD`
    directly above a no-op row classified as the predicate. Once the control
    row stopped counting as a failure, it did that while exiting 0: "a no-op
    that passes, every cell clean", which is the one result this tool exists
    to make impossible.

    The harness is already holding both measurements; it only has to notice
    they disagree. Voiding is the honest response, because WHICH reading is
    true is exactly what it can no longer tell.
    """
    marker = tmp_path / "seen"
    spec = _spec(cell=f"if [ -f {marker} ]; then echo good; else touch {marker}; echo {{arm}}; fi")
    r = _run(tmp_path, spec)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "RUN IS VOID" in r.stdout
    assert "behaved DIFFERENTLY in the results" in r.stdout
    assert "no-op did NOT match in the control sweep" in r.stdout


def test_a_SOUND_spec_is_not_caught_by_the_drift_check(tmp_path):
    """The guard-the-guard. A drift check that voided everything would satisfy
    the test above perfectly while destroying the tool."""
    r = _run(tmp_path, _spec())
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RUN IS VOID" not in r.stdout


@pytest.mark.parametrize(
    "cell",
    [
        pytest.param("echo {arm} {}", id="auto-numbered-field"),
        pytest.param("echo {arm:{arm}}", id="nested-format-spec"),
        pytest.param("echo {arm:d}", id="type-code-the-value-cannot-satisfy"),
        pytest.param("echo {arm!z}", id="unknown-conversion"),
    ],
)
def test_a_template_that_PARSES_but_cannot_be_SUBSTITUTED_is_a_SPEC_error(tmp_path, cell):
    """Reading the template's fields is not the same question as formatting it.

    Each of these parses, so the field scan accepted them, and each then threw
    out of `.format()` mid-sweep as an uncaught exception — which exits 1, the
    status this CLI documents as a real non-match. Asking the real formatter
    the real question is both shorter and complete.
    """
    r = _run(tmp_path, _spec(cell=cell))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "cannot be substituted" in r.stderr or "not a valid format" in r.stderr


def test_a_HARNESS_crash_does_not_masquerade_as_a_real_result(tmp_path):
    """A bare `sys.exit(main())` makes exit 1 the catch-all, because that is
    what CPython returns for an uncaught exception — and 1 is documented here
    as "controls held; a real non-match".

    So any harness bug read as an experimental finding. MEASURED reachable
    through an `--evidence` path that cannot be written, which turned both a
    sound run (0) and a VOID run (2) into 1. Exit 4 says the tool failed and
    that nothing above it is a measurement.
    """
    r = _run(tmp_path, _spec(), "--evidence", "/proc/cannot/exist/report.txt")
    assert r.returncode == 4, r.stdout + r.stderr
    assert "HARNESS ERROR" in r.stderr


def test_the_RAN_status_cannot_be_borrowed_as_a_label(tmp_path):
    """`_RAN` sat under a comment saying a spec may not use these as labels,
    and was the one status missing from the reserved set. No falsehood today,
    because the status never renders — but an unenforced rule in a comment is
    the shape every other sentinel in this file was reserved to prevent."""
    r = _run(tmp_path, _spec(classify={"GOOD": "^good", "ran": "^bad"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "RESERVED for an execution" in r.stderr


def test_an_EMPTY_ok_exit_codes_is_a_SPEC_error_not_a_broken_instrument(tmp_path):
    """With no accepted status every cell becomes ERR, and the run then reports
    2 — the instrument is broken — when what is broken is the spec. The two
    exit codes exist to tell those apart."""
    r = _run(tmp_path, _spec(ok_exit_codes=[]))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "no exit status could ever count as RAN" in r.stderr


def test_the_two_CONTROL_arms_must_differ(tmp_path):
    """One value cannot both demonstrate the harness works and demonstrate it
    can fail. Left unchecked the run still voids, but with a message describing
    the wrong problem — which costs the reader the actual diagnosis."""
    r = _run(tmp_path, _spec(controls={"oracle": "good", "noop": "good"}))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "are the same value" in r.stderr
