"""What a mutation sweep judges: its cases, how a pytest run reads, and how a
result is reported. Nothing here writes a file, and the one process it runs is
`bash -n`, in `_validate`. The guarantees are in `mutation_sweep.py`'s docstring."""

from __future__ import annotations

import io
import re
import subprocess
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

#: Outcomes. `ABORTED` is deliberately NOT a synonym for "survived" -- conflating
#: them is how a sweep that never ran reports a clean bill of health.
BIT = "BIT"
SURVIVED = "SURVIVED"
ABORTED = "ABORTED"


@dataclass(frozen=True)
class Edit:
    """One anchored replacement. A case may carry several, applied together."""

    anchor: str
    replacement: str

    def __post_init__(self) -> None:
        if not self.anchor:
            raise ValueError("edit has an empty anchor, which matches everywhere")
        if self.anchor == self.replacement:
            raise ValueError("edit is a no-op")


#: How a mutated file proves it is still valid. REQUIRED per case, and `none`
#: must carry a reason. Silently skipping the check for a file type we cannot
#: parse is the trap: an invalid mutation breaks pytest COLLECTION, and the
#: nonzero exit then reads as a successful RED -- the exact false positive the
#: postcondition exists to prevent. Raised by the session that mutates systemd
#: `.service.template` files, for which no validator exists at all (
#: `systemd-analyze verify` needs a rendered unit, not one carrying placeholders).
VALIDATORS = {
    "python": lambda text, path: compile(text, str(path), "exec"),
    "bash": None,  # handled out-of-process; see _validate
    "none": None,  # requires a reason
}


@dataclass(frozen=True)
class Case:
    """One mutation and the test that must notice it.

    ``why`` is required, not decorative: a case whose author cannot say which
    property it breaks is usually a behaviourally-null edit, and that is the
    failure mode a GREEN result cannot distinguish from a vacuous test.

    The runner fields are PER CASE, not module-global. That is the measured
    reason sessions forked their harnesses rather than adding cases to them: one
    sweep can need a probe venv with `--noconftest` for an engine-backed test and
    the production interpreter for a test that needs the full import tree, and a
    single global runner cannot express both.
    """

    label: str
    path: Path
    test: str
    why: str
    validator: str
    anchor: str | None = None
    replacement: str | None = None
    edits: tuple[Edit, ...] = ()
    #: Overrides the sweep default when set.
    python: str | None = None
    pytest_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    #: Shared live state this case touches (a running engine, a real socket).
    #: The pytest lock does not model these -- two sessions mutating against the
    #: same live engine stomp each other and nothing notices. Declared here so a
    #: reader can serialise on it; a case that names a resource ABORTS LOUDLY
    #: when it is absent rather than being skipped, because a skipped case and a
    #: killed one look identical in a summary line.
    requires: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.why.strip():
            raise ValueError(f"case {self.label!r}: `why` is required")
        if not self.validator.strip():
            raise ValueError(
                f"case {self.label!r}: `validator` is required -- one of "
                f"{sorted(VALIDATORS)}, and `none` must carry a reason "
                f"(e.g. 'none: a systemd template has no validator')"
            )
        kind, _, reason = self.validator.partition(":")
        kind = kind.strip()
        if kind not in VALIDATORS:
            raise ValueError(f"case {self.label!r}: unknown validator {kind!r}")
        # The reason is the point, so an empty one ("none:", "none:   ") is the
        # same omission as no colon at all.
        if kind == "none" and not reason.strip():
            raise ValueError(f"case {self.label!r}: `none` must say WHY there is no validator")
        # The single-edit shorthand is a PAIR. Half of it is a malformed case,
        # never a weaker one: a `replacement` with no `anchor` used to be dropped
        # silently (a multi-edit case then ran with one edit missing), and an
        # `anchor` with no `replacement` became a deletion via `or ""`. An
        # explicit empty replacement is still a deliberate deletion.
        if (self.anchor is None) != (self.replacement is None):
            raise ValueError(
                f"case {self.label!r}: `anchor` and `replacement` must be given "
                "together (use `edits` for more than one)"
            )
        if self.anchor is not None:
            object.__setattr__(self, "edits", (Edit(self.anchor, self.replacement),) + self.edits)
        if not self.edits:
            raise ValueError(f"case {self.label!r}: no edits")


@dataclass
class Result:
    case: Case
    outcome: str
    detail: str = ""
    stdout: str = ""
    never_opened: bool = False


@dataclass
class Sweep:
    results: list[Result] = field(default_factory=list)

    @property
    def bit(self) -> list[Result]:
        return [r for r in self.results if r.outcome == BIT]

    @property
    def survived(self) -> list[Result]:
        return [r for r in self.results if r.outcome == SURVIVED]

    @property
    def aborted(self) -> list[Result]:
        return [r for r in self.results if r.outcome == ABORTED]

    @property
    def clean(self) -> bool:
        """Every case bit, and nothing aborted.

        An abort is NOT a pass. A sweep that could not run half its cases has
        established nothing about them.
        """
        return bool(self.results) and not self.survived and not self.aborted


#: READING PYTEST'S SUMMARY. Every verdict this module reaches about a test run
#: goes through `parse_pytest_summary` and nothing else. The reader it replaced
#: classified the summary by SUBSTRING, and that one generator produced two
#: review findings: a case-sensitive "error" let "1 Error" through, and
#: "1 xfailed" CONTAINS "failed" -- so a strict-xfail target baselined as a
#: result, the mutation made it pass, pytest reported the strict XPASS as
#: "1 failed", and the sweep scored BIT on a test that never passed. A grammar
#: closes the class; a longer word list would not.
#:
#: The grammar is pytest's own, READ from `_pytest/terminal.py` (pytest 9.0.3; unchanged
#: in 9.2.0, the version CI pins in .github/test-tools/requirements.txt):
#: `summary_stats` joins the parts with ", " and appends
#: " in {format_session_duration}" ("0.12s", or "75.00s (0:01:15)" past a
#: minute); `_build_normal_summary_stats_line` renders each part as
#: "%d %s" % pluralize(count, key) over KNOWN_TYPES plus any plugin's own key,
#: or the single part "no tests ran"; `pluralize` changes exactly two keys,
#: error -> errors and warnings -> warning/warnings. At verbosity >= 0 the line
#: is centred in "=" separators; with --color=yes it carries SGR codes; at -qq
#: (verbosity < -1) `summary_stats` returns before writing it at all.
_ANSI_SGR = re.compile(r"\x1b\[[0-9;]*m")
_PART = r"\d+ [a-z]+(?: [a-z]+)*"
_SUMMARY_LINE = re.compile(
    r"(?:=+ )?"
    rf"(?P<body>no tests ran|{_PART}(?:, {_PART})*)"
    r" in \d+(?:\.\d+)?s(?: \([^()]+\))?"
    r"(?: =+)?"
)
#: pytest's two pluralised keys, folded back to one name each.
_KEY_ALIASES = {"errors": "error", "warning": "warnings"}
#: Keys that ride along with a verdict without changing it: a sibling the
#: selection excluded, a warning, a subtest that passed.
_INCIDENTAL = frozenset({"deselected", "warnings", "subtests passed"})


def parse_pytest_summary(stdout: str) -> dict[str, int] | None:
    """The outcome counts from pytest's FINAL summary line, by exact keyword.

    Returns ``{}`` for "no tests ran" -- readable, and empty -- and None when no
    line in ``stdout`` is a whole summary line. None is never scored: every
    caller ABORTS on it. The last matching line wins, because the summary is the
    last thing pytest writes and a run prints plenty above it (a short-summary
    "FAILED t.py::x - ..." line, an "ERROR <nodeid>" report) that must not be
    read as one. A line must match the grammar end to end: "1 Error in 0.2s" is
    not a line pytest can write, so it is unreadable rather than a near miss. A
    repeated key is refused the same way -- pytest renders each key once.
    """
    for raw in reversed(_ANSI_SGR.sub("", stdout).splitlines()):
        m = _SUMMARY_LINE.fullmatch(raw.strip())
        if not m:
            continue
        if m["body"] == "no tests ran":
            return {}
        counts: dict[str, int] = {}
        for part in m["body"].split(", "):
            n, key = part.split(" ", 1)
            key = _KEY_ALIASES.get(key, key)
            if key in counts:
                return None
            counts[key] = int(n)
        return counts
    return None


def _render_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{n} {k}" for k, n in counts.items()) or "no tests ran"


def baseline_problem(test: str, returncode: int, stdout: str) -> str | None:
    """None when the baseline run proves the TARGET PASSED; otherwise why not.

    Valid ONLY as "N passed" (N >= 1) with exit 0, plus incidental parts
    (deselected siblings, warnings, passing subtests). Everything else refuses:

      unreadable summary               -> refused, never scored
      any `failed` or `error`          -> RED (a strict XPASS is reported as
                                          `failed`, so it lands here too)
      xfailed / xpassed / skipped, or
      a plugin's own key               -> refused, naming the outcome
      nothing passed (no tests ran,
      only deselected)                 -> refused, naming the summary
      exit code other than 0           -> refused: code and summary disagree

    This gate is where a strict xfail MUST be stopped: once a mutation makes it
    pass, pytest reports "1 failed" with exit 1, which no later reading can tell
    apart from a caught mutation.
    """
    counts = parse_pytest_summary(stdout)
    if counts is None:
        return (
            f"baseline run of {test} produced no readable pytest summary "
            f"line (exit {returncode}) -- it cannot be established as green"
        )
    shown = _render_counts(counts)
    if counts.get("failed") or counts.get("error"):
        return (
            f"baseline is RED: {test} already fails before any mutation "
            f"({shown}), so every 'BIT' from it would be meaningless"
        )
    other = {k: n for k, n in counts.items() if k != "passed" and k not in _INCIDENTAL}
    if other:
        return (
            f"baseline of {test} did not PASS its target ({shown}): "
            f"{_render_counts(other)} is not a green baseline. A strict "
            "xfail that a mutation makes pass is reported as `failed`, and a "
            "skipped target never runs, so either would score a BIT or a "
            "SURVIVED that means nothing"
        )
    if not counts.get("passed"):
        return (
            f"baseline of {test} ran nothing ({shown}, exit {returncode}) "
            "-- a target that did not run cannot be established as green"
        )
    if returncode != 0:
        return (
            f"baseline of {test}: exit {returncode} disagrees with the "
            f"summary ({shown}) -- neither alone establishes a green run"
        )
    return None


def mutation_verdict(returncode: int, stdout: str) -> tuple[str, str]:
    """(outcome, detail) for one mutated run of a target whose baseline PASSED.

    THE EXIT CODE AND THE SUMMARY MUST AGREE. pytest's codes are an enumerated
    contract (pytest.ExitCode): 0 OK, 1 TESTS_FAILED, 2 INTERRUPTED,
    3 INTERNAL_ERROR, 4 USAGE_ERROR, 5 NO_TESTS_COLLECTED; only 0 and 1 mean the
    tests ran. MEASURED, a live false GREEN: a mutation that COMPILES but raises
    at import time gives rc=2 with "1 error in 0.26s", which the old
    has-a-result-line-and-rc!=0 rule called BIT. `destructive_command_guard.py`
    carries a module-level re.compile, so that is reachable from the shipped gate.

      exit not 0/1                     -> ABORTED
      unreadable summary               -> ABORTED, never scored
      any `error` (setup, teardown)    -> ABORTED: the body may never have run
      xfailed / xpassed / skipped, or
      a plugin's own key               -> ABORTED, naming it: the baseline had
                                          none, so the mutation changed WHICH
                                          tests ran, not how they judged it
      nothing passed or failed         -> ABORTED
      exit 1 and >= 1 failed           -> BIT
      exit 0, 0 failed, >= 1 passed    -> SURVIVED
      anything else                    -> ABORTED: code and summary disagree
    """
    if returncode not in (0, 1):
        return ABORTED, (
            f"pytest exit {returncode}: the run did not happen "
            "(collection error, usage error, or nothing collected)"
        )
    counts = parse_pytest_summary(stdout)
    if counts is None:
        return ABORTED, (
            "no readable pytest summary line -- the run did not "
            "happen (lock, guard, collection error?)"
        )
    shown = _render_counts(counts)
    if counts.get("error"):
        return ABORTED, (
            f"{shown}: an error is not a result -- a setup, "
            "teardown or collection failure, and the test body may "
            "never have run"
        )
    failed, passed = counts.get("failed", 0), counts.get("passed", 0)
    # `subtests failed` rides along with a BIT only when pytest ALSO reports a
    # failed test at exit 1 (CodeRabbit's rule on the prior PR). Not added to
    # `_INCIDENTAL`, so the baseline stays strict. MEASURED on pytest 9.0.3: one
    # failing subtest prints "2 failed, 2 subtests passed" at exit 1 (the
    # subtest plus its parent); the built-in support never writes this key, and
    # review read the same in 9.2.0's source, so it comes from a plugin if it
    # appears at all.
    other = {
        k: n
        for k, n in counts.items()
        if k not in ("passed", "failed")
        and k not in _INCIDENTAL
        and not (k == "subtests failed" and n > 0 and returncode == 1 and failed >= 1)
    }
    if other:
        return ABORTED, (
            f"{shown}: {_render_counts(other)} is not a verdict on "
            "the target -- its baseline had none, so the mutation "
            "changed which tests ran"
        )
    if not failed and not passed:
        return ABORTED, f"{shown}: nothing passed or failed -- the run did not happen"
    if returncode == 1 and failed:
        return BIT, ""
    if returncode == 0 and not failed:
        return SURVIVED, ""
    return ABORTED, (
        f"exit {returncode} disagrees with the summary ({shown}) -- neither alone is a verdict"
    )


def _validate(case: Case, text: str) -> str | None:
    """Prove the mutated file is still valid, or say why that cannot be proven.

    Returns an error string, or None when the mutation is valid / deliberately
    unvalidated. NEVER silently skips: an unvalidatable target must have SAID so
    in its `validator` field, which `Case.__post_init__` enforces.
    """
    kind = case.validator.split(":", 1)[0].strip()
    if kind == "python":
        try:
            compile(text, str(case.path), "exec")
        except SyntaxError as exc:
            return f"mutation is not valid python: {exc}"
        return None
    if kind == "bash":
        try:
            proc = subprocess.run(
                ["bash", "-n"], input=text, capture_output=True, text=True, timeout=60
            )
        except subprocess.TimeoutExpired:
            return "could not validate the mutation: bash -n timed out after 60s"
        if proc.returncode != 0:
            return f"mutation is not valid bash: {proc.stderr.strip()[:200]}"
        return None
    # `none: <reason>` -- declared unvalidatable. The reason travels with the
    # case so a reader knows the postcondition is absent BY DECISION.
    return None


def _decode(case: Case, raw: bytes) -> tuple[str, str]:
    """Decode a target in ITS OWN encoding, and prove that is lossless.

    Python sources honour a BOM or a PEP 263 cookie -- the same rule CPython
    applies when it imports them -- so a valid latin-1 or UTF-8-BOM target is
    sweepable, and is written back in that encoding (``utf-8-sig`` re-adds the
    BOM). Everything else is UTF-8. The round-trip check refuses a source whose
    bytes would not survive decode/encode unchanged, because rewriting such a
    file would alter lines the case never named. Raises ``ValueError``.
    """
    kind = case.validator.split(":", 1)[0].strip()
    try:
        enc = tokenize.detect_encoding(io.BytesIO(raw).readline)[0] if kind == "python" else "utf-8"
        text = raw.decode(enc)
    except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot decode the target: {exc}") from exc
    if text.encode(enc) != raw:
        raise ValueError(
            f"the target does not round-trip through {enc}; "
            "rewriting it would change bytes the case never named"
        )
    return text, enc


def apply_edits(text: str, edits: tuple[Edit, ...]) -> tuple[str, str | None]:
    """``(mutated, None)``, or ``(text, why not)`` -- every edit judged against
    the PRISTINE text, never against an earlier edit's output.

    Counting on the progressively mutated text let edit 1's replacement
    MANUFACTURE edit 2's anchor (a reviewer reproduced a clean "1 bit" from an
    anchor that was never in the file) or destroy it. So each anchor must occur
    exactly once in the original: found once, and not found again from the next
    character on, because ``str.count`` counts non-overlapping matches and
    ``'aaa'.count('aa') == 1`` although it matches at two places. Two edits may
    not overlap (identical starts included). The replacements are then applied
    by offset, last first, so earlier offsets stay valid.
    """
    spans: list[tuple[int, int, int]] = []
    for i, edit in enumerate(edits):
        first = text.find(edit.anchor)
        where = f"edit {i + 1}/{len(edits)}"
        if first < 0:
            return text, f"{where}: anchor matched 0x, expected exactly 1"
        if text.find(edit.anchor, first + 1) >= 0:
            return text, f"{where}: anchor matched 2x or more, expected exactly 1"
        spans.append((first, first + len(edit.anchor), i))
    spans.sort()
    for (s1, e1, i1), (s2, _e2, i2) in zip(spans, spans[1:], strict=False):
        if s2 < e1 or s2 == s1:
            return text, (
                f"edits {i1 + 1} and {i2 + 1} overlap in the original text; "
                "each edit must replace its own span"
            )
    out = text
    for start, end, i in reversed(spans):
        out = out[:start] + edits[i].replacement + out[end:]
    if out == text:
        return text, "the edits cancel out: the mutated text equals the original"
    return out, None


def _render_result(r: Result) -> str:
    mark = {BIT: "BIT     ", SURVIVED: "SURVIVED", ABORTED: "ABORTED "}[r.outcome]
    lines = [f"  {mark}  {r.case.label}"]
    if r.detail:
        lines.append(f"            {r.detail}")
    if r.outcome == SURVIVED:
        lines.append(f"            expected to break: {r.case.why}")
    return "\n".join(lines)


def _render_tally(result: Sweep) -> str:
    lines = [
        "",
        f"  {len(result.bit)} bit, {len(result.survived)} SURVIVED, {len(result.aborted)} ABORTED",
    ]
    if result.survived:
        lines.append(
            "  A survivor means the test does not pin the property its "
            "case names -- or the mutation was behaviourally null."
        )
    if result.aborted:
        lines.append("  An abort is NOT a pass: those cases established nothing.")
    return "\n".join(lines)


def _contained_path(repo: Path, rel: str) -> Path:
    """``repo / rel``, refused when it would point outside the repository.

    The DIRECTORY part is resolved (so a symlinked directory cannot carry the
    target out of the tree); the final component is not, because a symlinked
    target must still reach `_load_target` AS a symlink to be refused there.
    """
    p = Path(rel)
    root = repo.resolve()
    if p.is_absolute() or ".." in p.parts or not ((repo / p).parent.resolve().is_relative_to(root)):
        raise ValueError(
            f"case path {rel!r} points outside the repository {root} -- an "
            "absolute path or a `..` component is refused even when it would "
            "land inside, because a manifest path is repository-relative"
        )
    return repo / p


def _str_list(value, what: str) -> tuple[str, ...]:
    """A manifest list of strings, or ValueError. A bare string would otherwise
    become a tuple of its characters."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{what} must be a list of strings, got {value!r}")
    return tuple(value)


def _str_map(value, what: str) -> dict[str, str]:
    """A manifest mapping of strings to strings, or ValueError."""
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{what} must map strings to strings, got {value!r}")
    return value


def cases_from_json(doc: dict, repo: Path) -> list[Case]:
    """Every manifest field, typed before anything runs.

    A value of the wrong type otherwise surfaces late and misleadingly: a bare
    string list becomes its characters, a non-string reaches ``re`` or
    ``subprocess`` as a traceback after the copy exists. Each refusal here is a
    ValueError naming the field.
    """
    cases = doc.get("cases") if isinstance(doc, dict) else None
    if not isinstance(cases, list):
        raise ValueError("the manifest must be an object with a `cases` list")
    out = []
    for c in cases:
        if not isinstance(c, dict):
            raise ValueError(f"each case must be an object, got {c!r}")
        name = f"case {c.get('label')!r}"
        for key in ("label", "path", "test", "why", "validator"):
            if not isinstance(c.get(key), str):
                raise ValueError(f"{name}: `{key}` must be a string, got {c.get(key)!r}")
        for key in ("anchor", "replacement", "python"):
            if c.get(key) is not None and not isinstance(c[key], str):
                raise ValueError(f"{name}: `{key}` must be a string, got {c[key]!r}")
        edits = c.get("edits", [])
        if not isinstance(edits, list) or not all(
            isinstance(e, dict)
            and isinstance(e.get("anchor"), str)
            and isinstance(e.get("replacement"), str)
            for e in edits
        ):
            raise ValueError(
                f"{name}: `edits` must be a list of {{anchor: str, replacement: str}} objects"
            )
        env = _str_map(c.get("env", {}), f"{name} `env`")
        out.append(
            Case(
                label=c["label"],
                path=_contained_path(repo, c["path"]),
                test=c["test"],
                why=c["why"],
                validator=c["validator"],
                anchor=c.get("anchor"),
                replacement=c.get("replacement"),
                edits=tuple(Edit(e["anchor"], e["replacement"]) for e in edits),
                python=c.get("python"),
                pytest_args=_str_list(c.get("pytest_args", []), "`pytest_args`"),
                env=env,
                requires=_str_list(c.get("requires", []), "`requires`"),
            )
        )
    return out
