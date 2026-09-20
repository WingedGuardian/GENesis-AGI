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

THE SPEC (JSON)
===============
    {
      "question":      "one line: what is actually being decided",
      "axes":          {"remedy": [...], "state": [...], "shell": [...]},
      "cell":          "shell command; {remedy} {state} {shell} are substituted",
      "classify":      {"real": "^/real", "DECOY": "^/decoy"},
      "predicate":     "real",
      "controls": {
        "oracle": {"axes": {"remedy": "...", "state": "...", "shell": "..."}, "expect": "real"},
        "noop":   {"axes": {"remedy": "...", "state": "...", "shell": "..."}, "expect": "DECOY"}
      },
      "proposed_remedy": {"remedy": "unset CDPATH"},
      "decision_rule": "adoptable iff every hostile cell classifies `real`"
    }

`axes` is swept as a full CROSS PRODUCT on purpose. Hand-picking cells is how a
sweep ends up testing the cases its author already believed in, which is the
failure this exists to prevent — so the tool takes axes and enumerates, rather
than taking a list of cells.

A REMEDY MUST BE AN AXIS VALUE
==============================
`proposed_remedy` is a partial axis assignment naming the fix you intend to
recommend. The tool then reports whether the sweep actually covered it, and how
that slice of the table did.

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

Each control arm pins EVERY axis and is substituted into the same ``cell``
template, so it cannot exercise a different code path from the sweep it
certifies. That is not a convenience: it is the one property that makes a
control worth having, and the first real run of this tool failed precisely
because an earlier version let the arms be written as standalone commands.

`decision_rule` is recorded and echoed, never interpreted. Pre-registering it
is the point; having a tool grade against it would just move the judgement call
into a config file. The tool reports; the human decides against what they wrote
down BEFORE they saw the table.

EXIT CODES
    0  controls held and every cell matched `predicate`
    1  controls held; at least one cell did not match (a real result)
    2  CONTROLS FAILED — run is VOID, no matrix printed
    3  the spec itself is malformed
"""

from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# A cell is one shell invocation. Long enough for a real probe (spawning a
# shell, building a fixture tree), short enough that a hung cell cannot wedge a
# sweep of hundreds. Not a correctness bound: a cell that times out is reported
# as `TIMEOUT` and counts as not matching, never as a pass.
_CELL_TIMEOUT_S = 120


def _classify(out: str, rules: dict[str, str]) -> str:
    """First rule whose pattern matches, in declaration order; else `other`.

    Ordered rather than best-match: a sweep's categories usually overlap (an
    output can be both "a path under /real" and "non-empty"), and silently
    preferring one is the kind of hidden decision this tool exists to surface.
    """
    for label, pattern in rules.items():
        if re.search(pattern, out, re.MULTILINE):
            return label
    return "other"


def _run(cell: str, rules: dict[str, str]) -> str:
    try:
        # shell=True is the FEATURE, not an oversight: a probe cell is a shell
        # snippet the author writes to reproduce a hazard, and several of the
        # hazards worth sweeping (CDPATH, IFS, readonly, quoting) only exist
        # under a real shell. The spec is author-authored and runs with the
        # author's own privileges — it is their shell, reached via a file
        # instead of the prompt. It never reads untrusted input, and must not
        # be extended to: a spec fetched from a PR, an issue, or review text
        # would turn this line into remote execution.
        proc = subprocess.run(  # noqa: S602 - author-authored probe, see above
            cell, shell=True, capture_output=True, text=True, timeout=_CELL_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    if proc.returncode != 0 and not proc.stdout.strip():
        # A cell that produced nothing AND failed is an error, not a category.
        # Distinguished from a non-zero exit WITH output, which many probes do
        # legitimately (the thing under test refused, and that refusal is data).
        return "ERR"
    return _classify(proc.stdout, rules)


def _check_controls(spec: dict[str, Any], rules: dict[str, str]) -> list[str]:
    """Return the control failures. Empty list means the instrument is trusted.

    BOTH arms are mandatory and the tool refuses a spec without them. An oracle
    alone proves the harness can succeed; only the no-op proves it can FAIL, and
    a harness that cannot fail is the one that reports everything as fine.

    A CONTROL ARM IS AXIS VALUES, NEVER A COMMAND. It is substituted into the
    SAME ``cell`` template the sweep uses, so the control provably exercises the
    identical code path. An earlier version let the author write each arm as its
    own shell snippet, and that is worthless in the precise case it matters:
    MEASURED on this tool's first real use, the no-op arm reproduced the hazard
    with a literal ``cd scripts`` while every swept cell resolved
    ``dirname "$0"`` to ``.`` inside ``bash -c`` and therefore never consulted
    CDPATH at all. Controls held, eight of eight cells passed, and the whole
    table was meaningless — the same inert-hostile-state defect the tool exists
    to catch, reproduced inside the tool. Deriving the arms from the template
    makes that unrepresentable instead of merely discouraged.
    """
    problems: list[str] = []
    controls = spec.get("controls") or {}
    axis_names = set(spec["axes"])
    for name in ("oracle", "noop"):
        arm = controls.get(name)
        if not arm or "axes" not in arm or "expect" not in arm:
            problems.append(
                f"{name}: MISSING — both arms are required, each as "
                f"{{'axes': {{...}}, 'expect': '...'}}"
            )
            continue
        if "cell" in arm:
            problems.append(
                f"{name}: a control arm must NOT carry its own `cell` — it is "
                f"substituted into the swept template so it cannot drift from it"
            )
            continue
        env = arm["axes"]
        if set(env) != axis_names:
            problems.append(
                f"{name}: axes {sorted(env)} do not cover {sorted(axis_names)} — "
                f"a control must pin EVERY axis, or it is not one cell"
            )
            continue
        got = _run(spec["cell"].format(**env), rules)
        if got != arm["expect"]:
            problems.append(
                f"{name}: expected {arm['expect']!r}, got {got!r} — "
                + (
                    "the harness cannot reproduce the hazard, so a 'pass' means nothing"
                    if name == "noop"
                    else "the harness cannot even measure the known-good case"
                )
            )
    return problems


def _spec_problems(spec: dict[str, Any]) -> list[str]:
    """Structural problems that make the spec unanswerable. Empty list is fine.

    An EMPTY AXIS is here because the cross product of anything with nothing is
    nothing: the controls still run and can hold, the sweep then enumerates zero
    cells, and the tool reports `cells=0 NOT-matching=0` and exits 0 — a
    vacuous pass wearing the grammar of a clean one, which is the single thing
    this tool exists not to print.

    A `proposed_remedy` naming an axis the sweep does not have is a SPEC bug
    rather than an unverified remedy: nothing could ever verify it, and a typo'd
    axis name would otherwise report as `UNVERIFIED` and read as an honest
    measurement gap. Naming a real axis with an unswept VALUE is the opposite —
    that is the case the feature exists to label, so it is not an error.
    """
    problems: list[str] = []
    axes = spec["axes"]
    for name, values in axes.items():
        if not values:
            problems.append(f"axis {name!r} has no values — the sweep would enumerate zero cells")
    remedy = spec.get("proposed_remedy")
    if remedy is None:
        return problems
    if not isinstance(remedy, dict) or not remedy:
        problems.append(
            "proposed_remedy must be a non-empty {axis: value} mapping, "
            "the same shape as a control arm's `axes`"
        )
        return problems
    unknown = sorted(set(remedy) - set(axes))
    if unknown:
        problems.append(
            f"proposed_remedy names axes {unknown}, which the sweep does not "
            f"have ({sorted(axes)}) — nothing could ever verify it"
        )
    return problems


def _remedy_lines(spec: dict[str, Any], rows: list[tuple[dict[str, str], str, bool]]) -> list[str]:
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

    matching = [
        (env, got, ok) for env, got, ok in rows if all(env[k] == v for k, v in remedy.items())
    ]
    missed = [(env, got) for env, got, ok in matching if not ok]
    lines.append(
        f"  MEASURED in {len(matching)} of {len(rows)} cells: "
        f"{len(matching) - len(missed)} matched {spec['predicate']!r}, {len(missed)} did not"
    )
    for env, got in missed:
        cells = "  ".join(f"{n}={v}" for n, v in env.items())
        lines.append(f"    NOT {spec['predicate']}: {cells}  -> {got}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path, help="JSON spec (see this file's docstring)")
    ap.add_argument("--evidence", type=Path, help="write the full run here")
    args = ap.parse_args()

    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any spec problem is the same class
        print(f"SPEC ERROR: {exc}", file=sys.stderr)
        return 3
    missing = [k for k in ("question", "axes", "cell", "classify", "predicate") if k not in spec]
    if missing:
        print(f"SPEC ERROR: missing {missing}", file=sys.stderr)
        return 3
    structural = _spec_problems(spec)
    if structural:
        for p in structural:
            print(f"SPEC ERROR: {p}", file=sys.stderr)
        return 3

    rules: dict[str, str] = spec["classify"]
    lines: list[str] = [
        f"QUESTION      : {spec['question']}",
        f"PREDICATE     : a cell passes iff it classifies {spec['predicate']!r}",
        f"DECISION RULE : {spec.get('decision_rule', '(none declared)')}",
        "",
        "=== INSTRUMENT CONTROL ===",
    ]

    problems = _check_controls(spec, rules)
    for p in problems:
        lines.append(f"  FAIL {p}")
    if problems:
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
    lines += [
        "  oracle arm: as expected",
        "  no-op  arm: reproduced the hazard",
        "  CONTROLS HELD",
        "",
    ]

    names = list(spec["axes"])
    combos = list(itertools.product(*(spec["axes"][n] for n in names)))
    width = max((len(n) for n in names), default=8)
    lines.append("=== RESULTS ===")
    rows: list[tuple[dict[str, str], str, bool]] = []
    failed = 0
    for combo in combos:
        env = dict(zip(names, combo, strict=True))
        got = _run(spec["cell"].format(**env), rules)
        ok = got == spec["predicate"]
        rows.append((env, got, ok))
        failed += 0 if ok else 1
        cells = "  ".join(f"{n}={v}" for n, v in env.items())
        lines.append(
            f"  {cells:<{width * 4}}  -> {got}{'' if ok else '   <-- not ' + spec['predicate']}"
        )

    lines += [
        "",
        f"cells={len(combos)}  matching={len(combos) - failed}  NOT-matching={failed}",
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
