"""The defect corpus's report: break a gate on purpose, run each rung of its tests,
and say which rung noticed.

A rung is a set of test files. Each (defect, rung) is one ordinary `Case`, so the
sweep runs them unchanged, each in its own copy. An escape is a finding and exits 0.
Exit 1 means the run itself could not be trusted. The guarantees are in
`mutation_sweep.py`'s docstring.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from mutation_case import (
    ABORTED,
    BIT,
    Case,
    Result,
    _decode,
    _str_list,
    _validate,
    apply_edits,
    cases_from_json,
)

_DEFECT_KEYS = frozenset(
    {
        "label",
        "why",
        "aim",
        "tests",
        "path",
        "validator",
        "anchor",
        "replacement",
        "edits",
        "python",
        "pytest_args",
        "env",
        "requires",
    }
)

#: What the table can and cannot say, printed with it.
_LIMIT = (
    '"caught_by" means the rung failed on the defect and passed either side of it; a '
    'flaky test can still read as one, so its tests are listed. "never opened" means '
    "the rung did not load the file. Opened is not exercised: a file every rung loads "
    "at import reads as opened whatever the tests do with it."
)


@dataclass(frozen=True)
class _Defect:
    label: str
    aim: str
    rungs: tuple[str, ...]


@dataclass
class Report:
    """Which rung noticed which defect, gathered as results arrive so a stop or a
    red baseline still has something to print."""

    defects: list[_Defect]
    #: By `id(case)`, as `sweep` keys its selectors: a `Case` holds a dict, so it
    #: does not hash.
    owners: dict[int, tuple[str, str]]
    #: Every rung in the manifest, with its files.
    rungs: dict[str, tuple[str, ...]]
    results: dict[tuple[str, str], Result] = field(default_factory=dict)
    started: tuple[str, str] | None = None
    refused: str | None = None

    def add(self, result: Result) -> None:
        self.results[self.owners[id(result.case)]] = result

    def start(self, case: Case) -> None:
        self.started = self.owners[id(case)]

    def _untrusted(self, result: Result) -> bool:
        return result.outcome == ABORTED and not result.never_opened

    def exit_code(self) -> int:
        return 1 if any(self._untrusted(r) for r in self.results.values()) else 0

    def _row(self, defect: _Defect) -> list[str]:
        got = {r: self.results.get((defect.label, r)) for r in defect.rungs}
        caught = [r for r, res in got.items() if res and res.outcome == BIT]
        never = [r for r, res in got.items() if res and res.never_opened]
        aborted = [r for r, res in got.items() if res and self._untrusted(res)]
        missing = [r for r, res in got.items() if res is None]

        if caught:
            head = f"caught_by: {', '.join(caught)}"
            if defect.aim not in caught:
                head += "  [off aim]"
        elif aborted:
            head = "undetermined"
        elif missing:
            head = "not finished"
        else:
            head = "ESCAPED"
        parts = [f"{defect.label} (aim: {defect.aim})", head]
        if never:
            parts.append(f"never opened: {', '.join(never)}")
        if aborted:
            parts.append(f"aborted: {', '.join(aborted)}")
        if unrun := [r for r in self.rungs if r not in defect.rungs]:
            parts.append(f"not run: {', '.join(unrun)}")
        lines = ["  ".join(parts)]
        for rung in caught:
            for name in _failed_tests(got[rung].stdout):
                lines.append(f"    {rung}: {name}")
        for rung in aborted:
            lines.append(f"    {rung}: {got[rung].detail}")
        return lines

    def table(self) -> str:
        complete = all((d.label, r) in self.results for d in self.defects for r in d.rungs)
        lines = ["", "RUNG REPORT", _LIMIT]
        if not complete:
            lines.append(
                "PARTIAL: the run ended before every rung ran. A defect marked `not "
                "finished` has no verdict, and no rung is judged."
            )
        if self.refused and self.started:
            _, rung = self.started
            lines.append(
                f"Stopped at rung `{rung}`, files: {', '.join(self.rungs[rung])}. A rung's "
                "baseline must pass with nothing skipped or xfailed: fix the test, or move "
                "it out of the rung."
            )
        for defect in self.defects:
            lines.extend(self._row(defect))
        if complete and len(self.defects) >= 2:
            rungs = dict.fromkeys(r for d in self.defects for r in d.rungs)
            for rung in rungs:
                judged = [res for (_, r), res in self.results.items() if r == rung]
                if not any(res.outcome == BIT or self._untrusted(res) for res in judged):
                    lines.append(f"rung `{rung}`: caught nothing")
        return "\n".join(lines)

    def publish(self) -> None:
        text = self.table()
        print(text, flush=True)
        if path := os.environ.get("GITHUB_STEP_SUMMARY"):
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(f"```\n{text.strip()}\n```\n")
            except OSError as exc:
                print(f"mutation-rungs: could not write the job summary: {exc}", file=sys.stderr)


def _failed_tests(stdout: str) -> list[str]:
    """The ids in pytest's short summary: `FAILED <id> - <why>`. Only below its
    heading, because a test's captured output can carry a line that looks the same."""
    _, heading, summary = stdout.rpartition("short test summary info")
    return [
        line.removeprefix("FAILED ").split(" - ", 1)[0]
        for line in summary.splitlines()
        if heading and line.startswith("FAILED ")
    ]


def _rungs(doc: dict) -> dict[str, tuple[str, ...]]:
    rungs = doc.get("rungs")
    if not isinstance(rungs, dict) or not rungs:
        raise ValueError("the manifest must have a `rungs` object: {name: [test file, ...]}")
    out = {}
    for name, files in rungs.items():
        if not (isinstance(files, list) and files and all(isinstance(f, str) for f in files)):
            raise ValueError(f"rung {name!r} must be a non-empty list of test files")
        for each in files:
            # Only the first file of a rung reaches `_copy_selector`'s refusal, so a
            # later one would otherwise run the caller's copy, or read as an option.
            if each.startswith("-") or Path(each).is_absolute() or ".." in Path(each).parts:
                raise ValueError(f"rung {name!r}: {each!r} must be a path inside the repository")
        out[name] = tuple(files)
    return out


def rung_cases(doc: dict, repo: Path) -> tuple[list[Case], Report]:
    """One `Case` per (defect, rung), through `cases_from_json` so a field is judged
    one way in both modes. Raises ValueError naming what is wrong."""
    rungs = _rungs(doc)
    entries = doc.get("cases")
    if not isinstance(entries, list) or not entries:
        raise ValueError("the manifest has no cases, and an empty report would read as clean")
    expanded, owners_in_order, defects, seen = [], [], [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"each case must be an object, got {entry!r}")
        label = entry.get("label")
        if not (isinstance(label, str) and label.strip()):
            raise ValueError(f"each case needs a non-empty string `label`, got {label!r}")
        if label in seen:
            raise ValueError(f"case {label!r} is listed twice")
        seen.add(label)
        if unknown := set(entry) - _DEFECT_KEYS:
            raise ValueError(f"case {label!r}: unknown key(s) {sorted(unknown)}")
        narrowed = entry.get("tests", {})
        if not isinstance(narrowed, dict):
            raise ValueError(f"case {label!r}: `tests` must map a rung to a selector")
        if unknown := set(narrowed) - set(rungs):
            raise ValueError(f"case {label!r}: `tests` names unknown rung(s) {sorted(unknown)}")
        run = tuple(narrowed) or tuple(rungs)
        aim = entry.get("aim")
        if aim not in run:
            raise ValueError(f"case {label!r}: `aim` {aim!r} is not a rung it runs: {list(run)}")
        defects.append(_Defect(label, aim, run))
        common = {k: v for k, v in entry.items() if k not in ("aim", "tests")}
        extra = _str_list(entry.get("pytest_args", []), f"case {label!r} `pytest_args`")
        for rung in run:
            files = (narrowed[rung],) if rung in narrowed else rungs[rung]
            expanded.append(
                {
                    **common,
                    "label": f"{label} [{rung}]",
                    "test": files[0],
                    "pytest_args": [*files[1:], *extra],
                }
            )
            owners_in_order.append((label, rung))
    cases = cases_from_json({"cases": expanded}, repo)
    return cases, Report(
        defects, {id(c): owner for c, owner in zip(cases, owners_in_order, strict=True)}, rungs
    )


def check_anchors(doc: dict, repo: Path) -> int:
    """Every defect still applies and every rung's file exists, without running a
    test. The cheap half of the report: it is what a gate PR can break."""
    cases, report = rung_cases(doc, repo)
    problems = [
        f"rung `{rung}`: test file {name} does not exist"
        for rung, files in report.rungs.items()
        for name in files
        if not (repo / name.split("::", 1)[0]).is_file()
    ]
    judged = set()
    for case in cases:
        label, _ = report.owners[id(case)]
        if not (repo / case.test.split("::", 1)[0]).is_file():
            problems.append(f"{label}: test file {case.test} does not exist")
        if label in judged:
            continue
        judged.add(label)
        try:
            text, _encoding = _decode(case, case.path.read_bytes())
        except (OSError, ValueError) as exc:
            problems.append(f"{label}: {exc}")
            continue
        mutated, why = apply_edits(text, case.edits)
        if why is None:
            why = _validate(case, mutated)
        if why:
            problems.append(f"{label}: {why}")
    for problem in dict.fromkeys(problems):
        print(f"  {problem}")
    print(f"  {len(judged)} defect(s) checked, {len(set(problems))} problem(s)")
    return 1 if problems else 0
