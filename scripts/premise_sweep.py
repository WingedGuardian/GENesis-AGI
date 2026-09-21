#!/usr/bin/env python3
"""Sweep a design question across its axes, with instrument controls that can VOID the run.

WHY THIS IS A TOOL AND NOT A DOCUMENT
=====================================
The method it encodes was already written down — "Measure, Do Not Choose" in the
genesis-development skill — and written-down method is exactly what failed. On
2026-09-20 a session with that doctrine loaded still (a) recommended a remedy it
had not measured, (b) built a harness whose hostile states were inert, and
(c) came within one message of reporting the resulting numbers as fact.

What caught it was not the doctrine. It was a NO-OP ARM that was supposed to
fail and didn't. That check is mechanical, so it belongs in code.

The load-bearing behaviour here is a REFUSAL: when the controls do not hold,
this tool prints no results matrix at all. Not a warning above the numbers — no
numbers. A warning is something a reader can skim past on the way to the table
they wanted; an absent table is not. Every other feature is convenience.

THE DOCTRINE IT ENCODES, CLAUSE BY CLAUSE
=========================================
`SKILL.md` "Measure, Do Not Choose" has three clauses, and this file exists to
make each one mechanical rather than remembered:

1. *Enumerate the space; do not pick cases from it.* -> `axes` are swept as a
   full cross product. The author supplies dimensions, never cells.
2. *Pre-register the predicate, the decision rule, and what you will do if
   nothing passes.* -> all three are REQUIRED spec fields, and a spec missing
   any of them is malformed. Recording them but letting the run proceed without
   them was the first version's gap: a rule chosen after seeing the table is not
   a pre-registration, it is a rationalisation with a timestamp.
3. *Control the instrument: an ORACLE arm that must score 100% and a NO-OP arm
   that must fail.* -> see "CONTROLS" below. "Score 100%" is across the
   environmental cells, not at one point, which is the whole difficulty.

THE SPEC (JSON)
===============
    {
      "question":       "one line: what is actually being decided",
      "axes":           {"remedy": [...], "state": [...], "shell": [...]},
      "candidate_axis": "remedy",
      "cell":           "shell command; {remedy} {state} {shell} are substituted",
      "classify":       {"real": "^/real", "DECOY": "^/decoy"},
      "predicate":      "real",
      "controls":       {"oracle": "unset CDPATH", "noop": "true"},
      "ok_exit_codes":  [0],
      "proposed_remedy": {"remedy": "unset CDPATH"},
      "decision_rule":  "adoptable iff every hostile cell classifies `real`",
      "no_pass_disposition": "if nothing passes, the call site moves off `cd` entirely"
    }

One axis is the CANDIDATE axis — the dimension whose values are the things being
compared (the remedies). Every other axis is ENVIRONMENTAL: the conditions each
candidate must survive. The distinction is what makes a control checkable.

CONTROLS
========
Each arm names a CANDIDATE VALUE and nothing else. The tool sweeps it across the
full environmental cross product, using the same `cell` template as the run.

- The **oracle** is a candidate known to work. It must classify `predicate` in
  EVERY environmental cell. One failure voids the run: an instrument that cannot
  measure the known-good case has nothing to say about the hostile ones.
- The **no-op** is a candidate known NOT to work — typically a literal no-op. It
  must NOT classify `predicate`. Where it does, the hazard is not reproduced in
  that cell, and the cell is INERT.

NEITHER ARM DECLARES AN EXPECTATION. An earlier version let the spec write
`expect` per arm and merely compared against it, which meant a no-op copied from
the oracle (both selecting a passing cell, both expecting a pass) satisfied the
check and printed "reproduced the hazard" having reproduced nothing — the
harness's central safeguard defeated by a copy-paste. Validating that
`oracle.expect == predicate and noop.expect != predicate` would have closed it;
deleting `expect` closes it by construction, and there is then no second place
for the polarity to be wrong.

INERT CELLS ARE NAMED, NOT VOIDED
=================================
A cell where the no-op does not fail is one where the hazard does not exist, so
no candidate can be credited for surviving it. Those cells are EXCLUDED from the
denominator and listed. Voiding the whole run instead would discard real
evidence over conditions that were simply irrelevant; silently counting them —
what the first version did — inflates every candidate's score with cells that
tested nothing. MEASURED on this tool's own CDPATH run: 4 of 16 cells passed for
every candidate because an absolute `$0` makes `cd` ignore CDPATH entirely.
If EVERY cell is inert the run is void, because then the harness reproduces
nothing anywhere.

A REMEDY MUST BE AN AXIS VALUE
==============================
`proposed_remedy` is a partial axis assignment naming the fix you intend to
recommend. The tool reports whether the sweep actually covered it, and how that
slice of the table did.

It exists because of an asymmetry nothing else catches. A premise check
MEASURES its finding and ASSERTS its remedy: the protocol demands evidence for
the finding, the fix arrives as a bonus, and nobody grades it. MEASURED n=3 on
2026-09-20 — a `return "unknown"` that would have shipped a no-op, a `./`-prefix
recommendation that breaks on an absolute `$0`, and a parser recommendation
made before checking the parser could read that input. In all three the FINDING
was sound, which is exactly what lends the bad remedy its credibility.

A declared remedy whose values were never swept is labelled `UNVERIFIED`; it
does not void the run. The instrument is sound and the finding is real — only
the fix is unmeasured, and destroying good evidence over a separate claim would
teach authors to omit the field rather than declare it. For the same reason the
exit code is untouched: "did the cells match?" and "is the remedy measured?"
are independent, and folding them into one integer would force a lie whenever
they disagree. Omitting the field is reported too, as a stated absence rather
than a silent one.

THE LIMIT, because the tool must not oversell itself: this checks that the
remedy was SWEPT, never that the axes were the right axes. Nothing here can
tell you a hazard is missing from the table. MEASURED on the run that produced
this feature — the CDPATH spec's `operand` axis did not carry the `./` prefix
at all (so the recommendation was UNVERIFIED), and once it did, the sweep still
had no axis for an absolute `$0`, which is the case the prefix actually breaks
on. Adding it turned four cells red. The controls certify the instrument; the
axis set remains a judgement, and it is the one this tool leaves with you.

EXECUTION STATUS IS NOT A CLASSIFICATION
========================================
What happened to the PROCESS (ran / failed / timed out) is tracked separately
from what its output MEANS. Collapsing them into one string let a spec whose
`predicate` was `ERR` count every crashed command as a pass, and let a cell that
printed a result and then exited nonzero be read as a clean success. A cell
matches only when it RAN (an accepted exit status) and its output classifies as
`predicate`. Declare `ok_exit_codes` when a nonzero status is legitimate data.

CELLS MUST BE IDEMPOTENT, AND THE TOOL CANNOT ENFORCE IT
A cell is a shell snippet, so the harness cannot sandbox one without becoming
a different tool. The controls run before the result cells and in the same
environment, so a cell that mutates shared state -- a file outside its own
mktemp dir, an exported variable, a running service -- can change the answer
of the cells that follow it. Write cells that set up and tear down their own
state. The harness reaps each cell's process group on every exit path, which
stops a lingering DESCENDANT from writing into the next cell, but it cannot
undo a write that already happened.

EXIT CODES
    0  controls held and every live cell matched `predicate`
    1  controls held; at least one live cell did not match (a real result)
    2  CONTROLS FAILED — run is VOID, no matrix printed
    3  the spec itself is malformed
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

# A cell is one shell invocation. Long enough for a real probe (spawning a
# shell, building a fixture tree), short enough that a hung cell cannot wedge a
# sweep of hundreds. Not a correctness bound: a cell that times out is reported
# as `TIMEOUT` and counts as not matching, never as a pass.
_CELL_TIMEOUT_S = 120

#: Process outcomes. These are NOT classification labels and a spec may not use
#: them as one — see "EXECUTION STATUS IS NOT A CLASSIFICATION".
_RAN = "ran"
_ERR = "ERR"
_TIMEOUT = "TIMEOUT"
_TRUNC = "TRUNCATED"
_UNDECODABLE = "UNDECODABLE"
#: The label `_classify` returns when NO rule matched. Reserved for the same
#: reason the execution outcomes are: a spec could otherwise declare `other`
#: as a classification AND name it as the predicate, at which point output
#: matching none of the author's own rules SATISFIES the thing being measured.
#: The fallback means "unclassified", and unclassified can never be a finding.
_OTHER = "other"
_RESERVED_LABELS = frozenset({_ERR, _TIMEOUT, _TRUNC, _UNDECODABLE, _OTHER})

#: Hard cap on one cell's captured stdout. A probe is expected to print a line
#: or two; anything past this is a runaway. Unbounded capture is not merely
#: untidy — `yes` in a cell fills memory until the OOM killer takes the sweep,
#: or on a swapless host the whole machine, and the victim gets no error. The
#: reader stops at the cap and kills the process group rather than growing.
_MAX_CELL_OUTPUT = 1 << 20
#: How long to wait for the drain thread after the child is gone. It only ever
#: has a closed pipe left to notice, so this is a safety net, not a budget.
_READER_JOIN_S = 5.0

_REQUIRED_KEYS = (
    "question",
    "axes",
    "candidate_axis",
    "cell",
    "classify",
    "predicate",
    "controls",
    "decision_rule",
    "no_pass_disposition",
)


def _reap_group(pgid: int) -> None:
    """SIGKILL a process group by pgid, tolerating a group that is already gone.

    The `pgid > 1` guard is the house `process_kill_safety` procedure, and it
    is not defensive noise: signalling process-group ONE is equivalent to
    signalling every process this user owns -- the whole container, from a
    harness that was only trying to clean up after one probe. A mock whose pid
    was never set reports exactly that value on Python 3.12, so the guard is
    what stands between a test double and the box.
    """
    if pgid <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the cell's whole process group. pgid > 1 is CHECKED, not assumed.

    `killpg(1, ...)` is `kill(-1, ...)` — every process this user owns, which on
    a container is the session running the sweep. `start_new_session=True` makes
    pgid == the child pid so the guard should be unreachable; an unreachable
    branch in front of a whole-container kill is worth its two lines anyway.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
        pgid = os.getpgid(proc.pid)
        if pgid > 1:
            os.killpg(pgid, signal.SIGKILL)
        else:
            proc.kill()


class _BoundedReader(threading.Thread):
    """Drain a pipe into a CAPPED buffer, discarding nothing silently.

    A dedicated thread rather than `communicate()`: the cap has to be enforced
    WHILE the child runs, not after it exits, because the failure being
    prevented is the buffer growing without limit. On passing the cap it stops
    accumulating and KILLS the process group immediately rather than letting a
    firehose burn the whole timeout budget — a runaway probe is already a
    broken probe, and there is nothing to learn from the next 120 seconds of it.
    """

    def __init__(self, pipe, proc: subprocess.Popen) -> None:
        super().__init__(daemon=True)
        self._pipe = pipe
        self._proc = proc
        self._chunks: list[str] = []
        self._size = 0
        self.truncated = False
        self.undecodable = False

    def run(self) -> None:
        try:
            while chunk := self._pipe.read(65536):
                if self._size < _MAX_CELL_OUTPUT:
                    self._chunks.append(chunk)
                    self._size += len(chunk)
                    continue
                if not self.truncated:
                    self.truncated = True
                    _kill_group(self._proc)
        except UnicodeDecodeError:
            # NOT swallowed with the rest, and the ordering is the whole point:
            # UnicodeDecodeError IS a ValueError, so the broad handler below
            # used to catch it, keep the valid PREFIX, and hand that prefix to
            # `_classify` as though it were the entire output. A rule that
            # would have matched past the bad bytes was then silently missed --
            # the same silent-truncation failure the byte cap exists to make
            # loud, arriving through a different door.
            self.undecodable = True
            _kill_group(self._proc)
        except (ValueError, OSError):
            # The pipe was closed under us by the timeout kill. Whatever was
            # read before that is still what the cell produced.
            pass
        finally:
            with contextlib.suppress(Exception):
                self._pipe.close()

    def text(self) -> str:
        return "".join(self._chunks)


def _classify(out: str, rules: dict[str, str]) -> str:
    """First rule whose pattern matches, in declaration order; else `other`.

    Ordered rather than best-match: a sweep's categories usually overlap (an
    output can be both "a path under /real" and "non-empty"), and silently
    preferring one is the kind of hidden decision this tool exists to surface.
    """
    for label, pattern in rules.items():
        if re.search(pattern, out, re.MULTILINE):
            return label
    return _OTHER


def _run(
    cell: str,
    rules: dict[str, str],
    ok_exits: frozenset[int],
    timeout_s: float = _CELL_TIMEOUT_S,
) -> tuple[str, str]:
    """Run one cell. Returns (status, label); `label` is meaningful only when ran.

    The cell is started in its OWN PROCESS GROUP so a timeout can kill the whole
    tree. `subprocess.run`'s own timeout kills only the direct child, and a
    shell cell that spawns descendants leaves them running — MEASURED: a
    reproduced child kept executing and wrote a fixture file after the harness
    had moved on to the next cell, so a timed-out probe could corrupt the cells
    that followed it.
    """
    # shell=True is the FEATURE, not an oversight: a probe cell is a shell
    # snippet the author writes to reproduce a hazard, and several of the
    # hazards worth sweeping (CDPATH, IFS, readonly, quoting) only exist
    # under a real shell. The spec is author-authored and runs with the
    # author's own privileges — it is their shell, reached via a file
    # instead of the prompt. It never reads untrusted input, and must not
    # be extended to: a spec fetched from a PR, an issue, or review text
    # would turn this line into remote execution.
    proc = subprocess.Popen(  # noqa: S602 - author-authored probe, see above
        cell,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    # `start_new_session=True` above makes the child a process-group LEADER,
    # so its pgid IS its pid. Captured here while the child is certainly
    # alive, because `os.getpgid` cannot answer once it has been reaped -- and
    # the descendants that outlive it are exactly what needs reaping.
    pgid = proc.pid
    reader = _BoundedReader(proc.stdout, proc)
    reader.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            timed_out = True
        reader.join(_READER_JOIN_S)
        if reader.undecodable:
            # Leads for the same reason TRUNCATED does: the output cannot be
            # classified soundly, and which way it broke is the actionable part.
            return _UNDECODABLE, ""
        # TRUNCATED is reported ahead of TIMEOUT. A firehose cell is killed BY the
        # reader, so it can present as either depending on which noticed first, and
        # "your probe emits unbounded output" is the actionable one — the timeout is
        # a consequence of it, not an independent fact.
        if reader.truncated:
            # A probe that emits more than the cap is a broken probe, and its
            # output CANNOT be classified soundly: `_classify` returns the first
            # rule matching anywhere in the text, so a rule that would have matched
            # past the cap is silently missed. Reported as an outcome, never as a
            # category — the same reason ERR and TIMEOUT are not classifications.
            return _TRUNC, ""
        if timed_out:
            return _TIMEOUT, ""
        out = reader.text()
        if proc.returncode not in ok_exits:
            # A nonzero exit is an ERROR, not a category — even when the cell
            # printed something first. `echo good; exit 42` used to classify as a
            # clean GOOD, so a setup failure occurring AFTER an early result line
            # produced evidence that read as success. A spec that legitimately
            # expects a nonzero status says so in `ok_exit_codes`.
            return _ERR, ""
        return _RAN, _classify(out, rules)
    finally:
        # EVERY exit path reaps the group, not only the timeout. Before this,
        # `_kill_group` ran solely under `TimeoutExpired`, so an interrupt --
        # or any other exception out of `wait` -- left the whole tree running;
        # and a cell that exited NORMALLY still left backgrounded descendants
        # behind, free to write into the cells that followed it. The kill uses
        # the captured pgid rather than a fresh `getpgid`, which the reaped
        # child can no longer answer.
        _reap_group(pgid)


def _matches(result: tuple[str, str], predicate: str) -> bool:
    """A cell matches only when it RAN and its output classifies as `predicate`."""
    return result[0] == _RAN and result[1] == predicate


def _render(result: tuple[str, str]) -> str:
    return result[1] if result[0] == _RAN else result[0]


def _spec_problems(spec: Any) -> list[str]:
    """Everything that makes the spec unanswerable. Empty list means well-formed.

    Validation is BY VALUE, not by key presence. The first version checked only
    that keys existed, so a complete-but-malformed spec reached execution and
    crashed — and an uncaught exception exits 1, which this CLI documents as
    "controls held; a real non-match". A broken spec was therefore recordable as
    experimental evidence, which is the failure mode this whole tool is against.
    """
    problems: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    missing = [k for k in _REQUIRED_KEYS if k not in spec]
    # PRESENT is not the same as SUPPLIED. `"decision_rule": ""` satisfies a
    # presence check and then prints as an empty DECISION RULE line above a
    # real matrix, which is pre-registration in form and nothing in substance.
    blank = [
        k
        for k in ("question", "decision_rule", "no_pass_disposition", "predicate", "cell")
        if k not in missing and (not isinstance(spec[k], str) or not spec[k].strip())
    ]
    if blank:
        problems.append(f"field(s) present but empty or non-string: {sorted(blank)}")
    if missing:
        problems.append(f"missing required field(s): {missing}")
        # `decision_rule` and `no_pass_disposition` are required, not optional
        # with a printed "(none declared)". Pre-registration that the tool will
        # proceed without is not pre-registration: the reader can pick a rule
        # after seeing the table, which is the exact rationalisation clause 2 of
        # the doctrine exists to prevent.
        return problems

    # TYPES BEFORE SEMANTICS, and this ordering is the fix rather than an
    # extra check. Every test below performs an OPERATION on a field -- a
    # membership test, `re.compile`, a set difference -- and an operation on
    # the wrong type does not return a problem STRING, it RAISES. MEASURED: a
    # list-valued `candidate_axis` raised TypeError from the membership test,
    # because a list is unhashable. This CLI documents its exit 1 as "controls
    # held; a real non-match", so a malformed spec crashed its way into being
    # recordable as experimental evidence -- the one outcome the whole tool
    # exists to prevent. Returning EARLY is the load-bearing part: a semantic
    # check on a value of the wrong type has no defined answer to report.
    mistyped: list[str] = []
    if not isinstance(spec["candidate_axis"], str):
        mistyped.append("candidate_axis must be a STRING naming one of the axes")
    if not isinstance(spec["classify"], dict) or not spec["classify"]:
        mistyped.append("classify must be a non-empty {label: regex} mapping")
    elif bad := sorted(
        repr(k)
        for k, v in spec["classify"].items()
        if not isinstance(k, str) or not isinstance(v, str)
    ):
        mistyped.append(
            f"classify must map a string label to a string regex; malformed entries: {bad}"
        )
    if not isinstance(spec.get("ok_exit_codes", []), list):
        mistyped.append("ok_exit_codes must be a LIST of integers")
    if mistyped:
        return problems + mistyped

    axes = spec["axes"]
    if not isinstance(axes, dict) or not axes:
        # `problems +` and not a bare list: the emptiness checks above have
        # already found real faults, and returning only this one would hide
        # them behind the first structural complaint.
        return problems + ["axes must be a non-empty {name: [values]} mapping"]
    for name, values in axes.items():
        # A bare string is iterable, so a spec writing `"state": "hostile"`
        # would sweep eight single-character cells without complaint.
        if not isinstance(values, list) or not values:
            problems.append(f"axis {name!r} must be a NON-EMPTY list of values")
        elif not all(isinstance(v, str) for v in values):
            problems.append(f"axis {name!r} has non-string values")
        elif len(set(values)) != len(values):
            # A repeated value runs the same cell twice and counts it twice,
            # so it silently WEIGHTS one arm of the sweep against the others
            # while the cell count still reads as the size of the space.
            dupes = sorted({v for v in values if values.count(v) > 1})
            problems.append(
                f"axis {name!r} repeats value(s) {dupes} — a repeated value "
                f"is the same cell counted twice, which weights the result "
                f"without changing what was measured"
            )

    cand = spec["candidate_axis"]
    if cand not in axes:
        problems.append(f"candidate_axis {cand!r} is not one of the axes {sorted(axes)}")

    classify = spec["classify"]
    if not isinstance(classify, dict) or not classify:
        problems.append("classify must be a non-empty {label: regex} mapping")
    else:
        for label, pattern in classify.items():
            if label in _RESERVED_LABELS:
                problems.append(
                    f"classify label {label!r} is RESERVED for an execution "
                    f"outcome — a spec may not name a classification after one"
                )
            try:
                re.compile(pattern)
            except re.error as exc:
                problems.append(f"classify[{label!r}] is not a valid regex: {exc}")
        if spec["predicate"] not in classify:
            problems.append(
                f"predicate {spec['predicate']!r} is not one of the classify "
                f"labels {sorted(classify)} — nothing could ever match it"
            )

    if isinstance(axes, dict) and all(isinstance(v, list) for v in axes.values()):
        # An unknown {placeholder} used to raise KeyError mid-sweep; an axis
        # never referenced by the template is swept but changes nothing, which
        # silently multiplies the cell count without measuring anything.
        try:
            fields = {f for _, f, _, _ in __import__("string").Formatter().parse(spec["cell"]) if f}
        except (ValueError, TypeError) as exc:
            problems.append(f"cell is not a valid format template: {exc}")
        else:
            if unknown := sorted(fields - set(axes)):
                problems.append(f"cell references {unknown}, which are not axes")
            if unused := sorted(set(axes) - fields):
                problems.append(
                    f"axes {unused} are never substituted into `cell` — they "
                    f"multiply the cell count without changing what runs"
                )

    controls = spec["controls"]
    if not isinstance(controls, dict) or set(controls) != {"oracle", "noop"}:
        problems.append("controls must be exactly {'oracle': <value>, 'noop': <value>}")
    elif cand in axes and isinstance(axes.get(cand), list):
        for arm in ("oracle", "noop"):
            if controls[arm] not in axes[cand]:
                problems.append(
                    f"controls.{arm} = {controls[arm]!r} is not a value of the "
                    f"candidate axis {cand!r} — a control must be substituted "
                    f"into the same template as the sweep"
                )

    for code in spec.get("ok_exit_codes", [0]):
        if not isinstance(code, int):
            problems.append(f"ok_exit_codes contains a non-integer: {code!r}")

    remedy = spec.get("proposed_remedy")
    if remedy is not None:
        if not isinstance(remedy, dict) or not remedy:
            problems.append(
                "proposed_remedy must be a non-empty {axis: value} mapping, "
                "naming the fix you intend to recommend"
            )
        elif unknown_axes := sorted(set(remedy) - set(axes)):
            problems.append(
                f"proposed_remedy names axes {unknown_axes}, which the sweep "
                f"does not have ({sorted(axes)}) — nothing could ever verify it"
            )
    return problems


def _env_cells(spec: dict[str, Any]) -> list[dict[str, str]]:
    """The cross product of the ENVIRONMENTAL axes — every axis but the candidate."""
    names = [n for n in spec["axes"] if n != spec["candidate_axis"]]
    if not names:
        return [{}]
    return [
        dict(zip(names, combo, strict=True))
        for combo in itertools.product(*(spec["axes"][n] for n in names))
    ]


def _check_controls(
    spec: dict[str, Any],
    rules: dict[str, str],
    ok_exits: frozenset[int],
    timeout_s: float,
) -> tuple[list[str], list[dict[str, str]]]:
    """Run both arms across every environmental cell. Returns (problems, inert).

    Each arm is swept rather than run once. A control executed at a single point
    certifies the instrument at that point and nowhere else — MEASURED on this
    tool's own CDPATH run, where a no-op arm pinned to a relative invocation
    said CONTROLS HELD while half the table ran under an absolute one the arm
    had never touched. The doctrine's "oracle must score 100%" is a statement
    about the whole space, and only a swept arm can check it.
    """
    problems: list[str] = []
    inert: list[dict[str, str]] = []
    cand = spec["candidate_axis"]
    template = spec["cell"]
    predicate = spec["predicate"]

    for env in _env_cells(spec):
        where = "  ".join(f"{k}={v}" for k, v in env.items()) or "(no environmental axes)"
        noop = _run(
            template.format(**{cand: spec["controls"]["noop"]}, **env), rules, ok_exits, timeout_s
        )
        # The no-op has to have RUN before its verdict means anything. Any
        # non-RAN outcome -- ERR, TIMEOUT, TRUNCATED, UNDECODABLE -- fails
        # `_matches` for the wrong reason, so a CRASHED no-op used to read as
        # "the arm correctly did not match" and certify the instrument. This
        # arm exists to prove the harness can FAIL; a broken arm proves only
        # that it can break.
        if noop[0] != _RAN:
            problems.append(
                f"no-op arm did not RUN at [{where}] -> {_render(noop)}: the "
                f"arm that must prove the harness can fail did not execute, "
                f"so its non-match is an accident rather than a measurement"
            )
            continue
        if _matches(noop, predicate):
            # The hazard does not exist here, so no candidate earns credit for
            # surviving it. Named and excluded rather than voided or counted.
            inert.append(env)
            continue
        oracle = _run(
            template.format(**{cand: spec["controls"]["oracle"]}, **env), rules, ok_exits, timeout_s
        )
        if not _matches(oracle, predicate):
            problems.append(
                f"oracle FAILED at [{where}] -> {_render(oracle)}: the harness "
                f"cannot measure the known-good candidate here, so nothing it "
                f"says about the hostile ones is worth reading"
            )

    if inert and len(inert) == len(_env_cells(spec)):
        problems.append(
            "the no-op candidate passes in EVERY environmental cell — the "
            "harness reproduces the hazard nowhere, so a 'pass' means nothing"
        )
    return problems, inert


def _remedy_lines(
    spec: dict[str, Any], rows: list[tuple[dict[str, str], tuple[str, str], bool]]
) -> list[str]:
    """Report whether the fix the author intends to recommend was actually swept.

    Printed AFTER the matrix on purpose. The refusal above works by withholding
    the table, which cannot apply here — the table is legitimately real. What is
    available instead is position: the reader who came for the table reads the
    table, and this is the next thing under it.
    """
    remedy = spec.get("proposed_remedy")
    lines = ["", "=== PROPOSED REMEDY ==="]
    if remedy is None:
        lines += [
            "  none declared.",
            "  Any fix recommended off this table is UNMEASURED unless it is one of",
            "  the swept values above. Declaring `proposed_remedy` gets that checked",
            "  instead of remembered.",
        ]
        return lines

    shown = "  ".join(f"{k}={v}" for k, v in remedy.items())
    lines.append(f"  {shown}")
    unswept = {k: v for k, v in remedy.items() if v not in spec["axes"][k]}
    if unswept:
        for k, v in unswept.items():
            lines.append(
                f"  UNVERIFIED — {k}={v!r} is not among the swept values {spec['axes'][k]}"
            )
        lines += [
            "  The sweep says nothing about this fix. Do not recommend it in the",
            "  grammar of the finding: the finding is measured, this is not.",
        ]
        return lines

    matching = [r for r in rows if all(r[0][k] == v for k, v in remedy.items())]
    if not matching:
        # Every declared value can be a real axis value and still select NO
        # live cell -- when the remedy pins an environmental value whose cells
        # the controls all classified inert. "MEASURED in 0 of N live cells"
        # is then a measurement claim resting on nothing, and it reads as a
        # pass because nothing failed. The distinction this tool exists for is
        # exactly measured-versus-asserted, so an empty slice is UNVERIFIED.
        return lines + [
            "  UNVERIFIED: the remedy selects NO live cell — every cell it "
            "names was excluded as inert, so the sweep never exercised it.",
        ]
    missed = [(env, got) for env, got, ok in matching if not ok]
    lines.append(
        f"  MEASURED in {len(matching)} of {len(rows)} live cells: "
        f"{len(matching) - len(missed)} matched {spec['predicate']!r}, {len(missed)} did not"
    )
    for env, got in missed:
        cells = "  ".join(f"{n}={v}" for n, v in env.items())
        lines.append(f"    NOT {spec['predicate']}: {cells}  -> {_render(got)}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path, help="JSON spec (see this file's docstring)")
    ap.add_argument("--evidence", type=Path, help="write the full run here")
    ap.add_argument(
        "--cell-timeout",
        type=float,
        default=_CELL_TIMEOUT_S,
        help=f"seconds before one cell is killed and reported TIMEOUT (default {_CELL_TIMEOUT_S})",
    )
    args = ap.parse_args()

    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any spec problem is the same class
        print(f"SPEC ERROR: {exc}", file=sys.stderr)
        return 3
    problems = _spec_problems(spec)
    if problems:
        for p in problems:
            print(f"SPEC ERROR: {p}", file=sys.stderr)
        return 3

    rules: dict[str, str] = spec["classify"]
    ok_exits = frozenset(spec.get("ok_exit_codes", [0]))
    predicate = spec["predicate"]
    lines: list[str] = [
        f"QUESTION      : {spec['question']}",
        f"PREDICATE     : a cell passes iff it RAN and classifies {predicate!r}",
        f"DECISION RULE : {spec['decision_rule']}",
        f"IF NONE PASS  : {spec['no_pass_disposition']}",
        "",
        "=== INSTRUMENT CONTROL ===",
    ]

    control_problems, inert = _check_controls(spec, rules, ok_exits, args.cell_timeout)
    for p in control_problems:
        lines.append(f"  FAIL {p}")
    if control_problems:
        # THE REFUSAL. No matrix, deliberately — see the module docstring.
        lines += [
            "",
            "RUN IS VOID. The results are NOT printed, because a matrix from an",
            "instrument that failed its own control is worse than no matrix: it",
            "reads exactly like a real one. Fix the harness and re-run.",
        ]
        out = "\n".join(lines)
        print(out)
        if args.evidence:
            args.evidence.write_text(out + "\n", encoding="utf-8")
        return 2

    env_cells = _env_cells(spec)
    live = [e for e in env_cells if e not in inert]
    lines += [
        f"  oracle {spec['controls']['oracle']!r}: matched {predicate!r} in all "
        f"{len(live)} live environmental cell(s)",
        f"  no-op  {spec['controls']['noop']!r}: reproduced the hazard in all "
        f"{len(live)} live environmental cell(s)",
        "  CONTROLS HELD",
    ]
    if inert:
        lines += [
            "",
            f"  INERT — {len(inert)} of {len(env_cells)} environmental cell(s) are "
            f"EXCLUDED: the no-op candidate passes there, so the hazard does not",
            "  exist in them and no candidate earns credit for surviving them.",
        ]
        lines += [f"    {'  '.join(f'{k}={v}' for k, v in e.items())}" for e in inert]
    lines.append("")

    cand = spec["candidate_axis"]
    lines.append("=== RESULTS ===")
    rows: list[tuple[dict[str, str], tuple[str, str], bool]] = []
    failed = 0
    for value in spec["axes"][cand]:
        # The NO-OP is a value of the candidate axis, so it is swept here
        # like any other -- and by construction it does not match, because
        # `_check_controls` has just REQUIRED that of every live cell. Counting
        # it as a failure made `failed` at least 1 on every sound run, so the
        # documented "0 = every cell matched" exit was unreachable whenever the
        # instrument was working. Its row stays in the table, because the
        # negative control is evidence a reader should see; it is labelled and
        # left out of the count.
        is_control = value == spec["controls"]["noop"]
        for env in live:
            full = {cand: value, **env}
            got = _run(spec["cell"].format(**full), rules, ok_exits, args.cell_timeout)
            ok = _matches(got, predicate)
            rows.append((full, got, ok))
            if not is_control:
                failed += 0 if ok else 1
            cells = "  ".join(f"{n}={v}" for n, v in full.items())
            if is_control:
                note = "   <-- no-op CONTROL, not counted"
            else:
                note = "" if ok else "   <-- not " + predicate
            lines.append(f"  {cells}  -> {_render(got)}{note}")

    # `matching` is counted, not DERIVED from `len(rows) - failed`. Once the
    # no-op control stopped counting as a failure, that subtraction silently
    # credited the control's own non-match as a match -- MEASURED on a live
    # run that printed `matching=2` over a table containing exactly one match.
    # The three numbers now describe disjoint sets, and the control row is
    # named so the reader can see why they do not sum to the row count.
    controls_shown = sum(1 for full, _got, _ok in rows if full[cand] == spec["controls"]["noop"])
    matched = sum(1 for full, _got, ok in rows if ok and full[cand] != spec["controls"]["noop"])
    lines += [
        "",
        f"live cells={len(rows)}  matching={matched}  NOT-matching={failed}"
        + (
            f"  ({controls_shown} no-op control row(s) shown, not counted)"
            if controls_shown
            else ""
        )
        + (f"  (excluding {len(inert)} inert environmental cell(s))" if inert else ""),
    ]
    lines += _remedy_lines(spec, rows)
    lines += [
        "",
        "Grade this against the DECISION RULE above — the one written before the",
        "table existed. The tool does not grade it for you on purpose.",
    ]
    out = "\n".join(lines)
    print(out)
    if args.evidence:
        args.evidence.write_text(out + "\n", encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
