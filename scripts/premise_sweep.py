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
      "decision_rule": "adoptable iff every hostile cell classifies `real`"
    }

`axes` is swept as a full CROSS PRODUCT on purpose. Hand-picking cells is how a
sweep ends up testing the cases its author already believed in, which is the
failure this exists to prevent — so the tool takes axes and enumerates, rather
than taking a list of cells.

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
    failed = 0
    for combo in combos:
        env = dict(zip(names, combo, strict=True))
        got = _run(spec["cell"].format(**env), rules)
        ok = got == spec["predicate"]
        failed += 0 if ok else 1
        cells = "  ".join(f"{n}={v}" for n, v in env.items())
        lines.append(
            f"  {cells:<{width * 4}}  -> {got}{'' if ok else '   <-- not ' + spec['predicate']}"
        )

    lines += [
        "",
        f"cells={len(combos)}  matching={len(combos) - failed}  NOT-matching={failed}",
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
