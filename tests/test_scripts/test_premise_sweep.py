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
    control logic, not anyone's shell semantics.
    """
    spec = {
        "question": "does the harness honour its own controls?",
        "axes": {"arm": ["good", "bad"]},
        "cell": "echo {arm}",
        "classify": {"GOOD": "^good", "BAD": "^bad"},
        "predicate": "GOOD",
        "controls": {
            "oracle": {"axes": {"arm": "good"}, "expect": "GOOD"},
            "noop": {"axes": {"arm": "bad"}, "expect": "BAD"},
        },
        "decision_rule": "adoptable iff every cell is GOOD",
    }
    spec.update(over)
    return spec


def _run(tmp_path: Path, spec: dict) -> subprocess.CompletedProcess:
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(_SWEEP), str(p)], capture_output=True, text=True, timeout=120
    )


def test_a_sound_spec_prints_the_matrix(tmp_path):
    """Positive control. Without it, a tool that refused EVERYTHING would pass
    every other test in this file."""
    r = _run(tmp_path, _spec())
    assert "=== RESULTS ===" in r.stdout
    assert "CONTROLS HELD" in r.stdout
    assert r.returncode == 1, "one cell is BAD, so the sweep reports a real non-match"
    assert "cells=2" in r.stdout


def test_a_noop_arm_that_does_not_reproduce_the_hazard_VOIDS_the_run(tmp_path):
    """THE defect this tool was built for, replayed.

    MEASURED 2026-09-20: a CDPATH sweep set its hostile states non-exported, so
    they never reached the child process, the no-op arm passed, and every cell
    reported clean. The conclusion drawn from it — "unset works everywhere" —
    was false and would have shipped.
    """
    spec = _spec()
    spec["controls"]["noop"]["expect"] = "GOOD"  # a no-op arm that cannot fail
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "RUN IS VOID" in r.stdout
    assert "a 'pass' means nothing" in r.stdout
    assert "=== RESULTS ===" not in r.stdout, (
        "the table must be ABSENT, not merely preceded by a warning — a warning "
        "above a table is exactly what a reader skims past"
    )


def test_a_failing_oracle_VOIDS_the_run(tmp_path):
    """The other half of the instrument: a harness that cannot measure the
    known-good case cannot be trusted about the hostile ones either."""
    spec = _spec()
    spec["controls"]["oracle"]["expect"] = "BAD"
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "=== RESULTS ===" not in r.stdout
    assert "cannot even measure the known-good case" in r.stdout


@pytest.mark.parametrize("arm", ["oracle", "noop"])
def test_a_control_arm_may_not_carry_its_own_cell(tmp_path, arm):
    """The design correction that came out of this tool's FIRST real run.

    An arm written as a standalone command can certify a code path the sweep
    never executes. MEASURED: the no-op arm reproduced the CDPATH hazard with a
    literal `cd scripts` while every swept cell resolved `dirname "$0"` to `.`
    inside `bash -c` and never consulted CDPATH — controls held, 8/8 cells
    passed, and the table was meaningless.
    """
    spec = _spec()
    spec["controls"][arm]["cell"] = "echo good"
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "must NOT carry its own `cell`" in r.stdout
    assert "=== RESULTS ===" not in r.stdout


@pytest.mark.parametrize("arm", ["oracle", "noop"])
def test_a_control_arm_must_pin_every_axis(tmp_path, arm):
    """An arm that leaves an axis free is not one cell, so it certifies nothing
    about the cells that vary it."""
    spec = _spec(axes={"arm": ["good", "bad"], "extra": ["x"]}, cell="echo {arm}{extra}")
    spec["classify"] = {"GOOD": "^goodx", "BAD": "^badx"}
    spec["controls"] = {
        "oracle": {"axes": {"arm": "good", "extra": "x"}, "expect": "GOOD"},
        "noop": {"axes": {"arm": "bad", "extra": "x"}, "expect": "BAD"},
    }
    del spec["controls"][arm]["axes"]["extra"]
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "do not cover" in r.stdout
    assert "=== RESULTS ===" not in r.stdout


@pytest.mark.parametrize("arm", ["oracle", "noop"])
def test_both_control_arms_are_mandatory(tmp_path, arm):
    """A spec may not opt out of the check by omitting it — the polarity is
    allowlist, so a sweep without controls does not silently become a sweep
    whose controls trivially hold."""
    spec = _spec()
    del spec["controls"][arm]
    r = _run(tmp_path, spec)
    assert r.returncode == 2
    assert "both arms are required" in r.stdout
    assert "=== RESULTS ===" not in r.stdout


def test_the_sweep_is_a_full_cross_product(tmp_path):
    """Axes in, cells enumerated — the author does not get to hand-pick, which
    is how a sweep ends up covering only the cases its author believed in."""
    spec = _spec(
        axes={"a": ["good", "bad"], "b": ["1", "2", "3"]},
        cell="echo {a}{b}",
        classify={"GOOD": "^good", "BAD": "^bad"},
    )
    spec["controls"] = {
        "oracle": {"axes": {"a": "good", "b": "1"}, "expect": "GOOD"},
        "noop": {"axes": {"a": "bad", "b": "1"}, "expect": "BAD"},
    }
    r = _run(tmp_path, spec)
    assert "cells=6" in r.stdout, "2 x 3 axes must produce 6 cells, not a chosen subset"


def test_the_decision_rule_is_echoed_and_NOT_graded(tmp_path):
    """Pre-registering the rule is the point; grading against it in code would
    move the judgement into a config file and out of the reader's view."""
    r = _run(tmp_path, _spec(decision_rule="adoptable iff pigs fly"))
    assert "adoptable iff pigs fly" in r.stdout
    assert "does not grade it for you" in r.stdout


def test_a_malformed_spec_is_distinguishable_from_a_void_run(tmp_path):
    """Exit 3, not 2: "your spec is broken" and "your instrument is broken" are
    different problems with different fixes, and collapsing them would hide the
    one this tool exists to surface."""
    spec = _spec()
    del spec["predicate"]
    r = _run(tmp_path, spec)
    assert r.returncode == 3
    assert "SPEC ERROR" in r.stderr
