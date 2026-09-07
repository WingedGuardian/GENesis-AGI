#!/usr/bin/env python3
"""Verify-RED as a library: break the mechanism, prove the test notices, put it back.

WHY THIS IS COMMITTED. The repo's own discipline says every new test must be seen
to FAIL for the right reason before its green is trusted. Sessions do follow it --
and each one hand-rolls the harness and throws it away. MEASURED 2026-09-07:
**15 independent implementations** under ~/tmp, five of them written that day by
one session, two within ten minutes of each other. They are the same program.

The contract below is not designed; it is what those fifteen CONVERGED on:

    per-case test target      15/15
    baseline copy             14/15
    hash-verified restore     14/15
    anchor matched exactly 1x 14/15
    abort on no result line   14/15
    explicit child env        14/15
    compile() the mutation    13/15
    -----------------------------------------
    abort if the file drifted  9/15   <-- the one that matters most
    tally / rationale          8/15

That last group is why this exists. The near-universal features are the ones
people remember; **the drift check is the one they skip**, and it is the only
one whose absence damages someone ELSE's work -- a mutation that overwrites a
concurrent session's uncommitted edit, then "restores" a file it never owned.
Six of fifteen would have done exactly that. A convention nobody is forced
through is a convention with better documentation, so the obligation moves into
a chokepoint here instead.

WHAT A SWEEP GUARANTEES, per case:
  1. The target still matches the ONE baseline snapshot taken before the sweep.
     Re-snapshotting per case would make this vacuous -- the file trivially
     matches a copy a moment old. The baseline exists to catch a PREVIOUS case
     that failed to restore, or a concurrent editor.
  2. The anchor matches exactly once. With two or more edits, a whole-file
     "did it change" test stays true while a later anchor silently misses, so
     a partial mutation reads as complete.
  3. The mutated source COMPILES, using the file's own parser. `compile()`, not
     `ast.parse`: the latter accepts context-invalid constructs (a `return`
     outside a function), and the SyntaxError then surfaces at COLLECTION, where
     a nonzero exit reads as a successful RED.
  4. The test command produced a RESULT LINE. No line means the run never
     happened -- a lock, a guard, a timeout -- and "all mutations survived" from
     a sweep that never ran is the confident false negative this class is famous
     for. It is an ABORT, never a survival.
  5. The file is restored, and the restore is verified by hash. Restore happens
     in a `finally`, because the expected outcome of a good case is a NONZERO
     exit and a trailing restore is exactly the statement that does not run.
  6. The restore only overwrites what THIS mutation wrote. If the file changed
     underneath, the sweep PRESERVES it and reports a conflict rather than
     destroying an edit a final hash check would happily confirm it had made.

WHAT IT DELIBERATELY DOES NOT DO. It does not generate mutations. Picking the
mutation is the thinking part -- a behaviourally-null edit (swapped operands
that commute, a type annotation Python does not enforce) produces a GREEN that
means nothing, and no generator knows which is which. It also does not replace
`mutmut`/`cosmic-ray`-style coverage sweeps; this is targeted verify-RED, where
each case names the property it is supposed to break.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Outcomes. `ABORTED` is deliberately NOT a synonym for "survived" -- conflating
#: them is how a sweep that never ran reports a clean bill of health.
BIT = "BIT"
SURVIVED = "SURVIVED"
ABORTED = "ABORTED"
CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class Edit:
    """One anchored replacement. A case may carry several, applied together."""

    anchor: str
    replacement: str

    def __post_init__(self) -> None:
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
    "bash": None,   # handled out-of-process; see _validate
    "none": None,   # requires a reason
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
        kind = self.validator.split(":", 1)[0].strip()
        if kind not in VALIDATORS:
            raise ValueError(f"case {self.label!r}: unknown validator {kind!r}")
        if kind == "none" and ":" not in self.validator:
            raise ValueError(
                f"case {self.label!r}: `none` must say WHY there is no validator"
            )
        if self.anchor is not None:
            object.__setattr__(
                self, "edits", (Edit(self.anchor, self.replacement or ""),) + self.edits
            )
        if not self.edits:
            raise ValueError(f"case {self.label!r}: no edits")


@dataclass
class Result:
    case: Case
    outcome: str
    detail: str = ""
    stdout: str = ""


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
        return [r for r in self.results if r.outcome in (ABORTED, CONFLICT)]

    @property
    def clean(self) -> bool:
        """Every case bit, and nothing aborted.

        An abort is NOT a pass. A sweep that could not run half its cases has
        established nothing about them.
        """
        return bool(self.results) and not self.survived and not self.aborted


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    """The ONE place that decides what a sweep's child process may see.

    An ALLOWLIST, not a subtract-list: a stray inherited variable is how a
    'deliberate concurrent run' lever, a disabled guard, or a pointer at another
    worktree silently changes what the test under mutation actually exercises.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(Path.home())),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        # Queue rather than fail when a peer session holds the test lock; a
        # lock collision would otherwise land as an ABORT on every case.
        "GENESIS_PYTEST_LOCK_WAIT": "1",
        # The anti-deadlock lever, forwarded ONLY when the parent already holds
        # the box lock. Its documented purpose is that a pytest spawned inside a
        # locked run does not contend with its own parent; stripping it meant a
        # sweep launched from inside a locked session queued behind itself and
        # died at the per-case timeout, ~15 minutes per case.
        **({"GENESIS_PYTEST_LOCK_HELD": os.environ["GENESIS_PYTEST_LOCK_HELD"]}
           if os.environ.get("GENESIS_PYTEST_LOCK_HELD") else {}),
        # TMPDIR is a PATH, not a behaviour lever. Dropping it sends every child's
        # temp to /tmp, which this project forbids for anything large, and makes
        # tests/conftest.py take its no-op basetemp branch on a dev box.
        "TMPDIR": os.environ.get("TMPDIR", str(Path.home() / "tmp")),
        # CPython validates cached bytecode on (mtime_seconds, size), so a
        # same-length mutation restored inside the same integer second leaves a
        # .pyc compiled from the MUTATED source for the next run to import.
        # Narrow, and this tool's whole value is that its RED/GREEN means something.
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra or {})
    return env


def _has_result_line(stdout: str) -> bool:
    """Did pytest actually report on the target?

    A SKIPPED/deselected line FOR THE CASE'S OWN TARGET means nothing ran and is
    treated as no result. A bare 'no tests ran' likewise.
    """
    tail = stdout.strip().splitlines()
    if not tail:
        return False
    for line in reversed(tail):
        # A REAL OUTCOME beats a sibling deselection. "1 failed, 1 deselected"
        # means the target ran and failed -- MEASURED, the previous rule read it
        # as "no result" and turned a genuinely-caught mutation into an ABORT.
        # Only a line with NO outcome at all ("2 deselected", "no tests ran")
        # means nothing ran. The earlier test covered the `passed` variant and
        # not the `failed` one, which is why this survived.
        if "passed" in line or "failed" in line:
            return True
        if "no tests ran" in line or "deselected" in line:
            return False
        if "passed" in line or "failed" in line or "error" in line.lower():
            return True
    return False


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
        proc = subprocess.run(["bash", "-n"], input=text, capture_output=True,
                              text=True, timeout=60)
        if proc.returncode != 0:
            return f"mutation is not valid bash: {proc.stderr.strip()[:200]}"
        return None
    # `none: <reason>` -- declared unvalidatable. The reason travels with the
    # case so a reader knows the postcondition is absent BY DECISION.
    return None


def run_case(
    case: Case,
    baseline: Path,
    *,
    cwd: Path,
    python: str,
    env: dict[str, str] | None = None,
    timeout: int = 900,
    available: set[str] | None = None,
) -> Result:
    target = case.path
    base_bytes = baseline.read_bytes()

    # A case gated on shared live state ABORTS when that state is absent. It does
    # NOT skip: a skipped case and a killed one look identical in a summary line,
    # and "11/11 bit" means nothing if two of them never ran.
    missing = [r for r in case.requires if r not in (available or set())]
    if missing:
        return Result(case, ABORTED,
                      f"requires {', '.join(missing)}, which this run did not "
                      "declare available -- not skipped, because a skipped case "
                      "and a killed one are indistinguishable in a tally")

    # (1) the file must still match the ONE baseline for the whole sweep.
    if target.read_bytes() != base_bytes:
        return Result(case, ABORTED,
                      "target differs from the sweep baseline -- a previous case "
                      "failed to restore, or someone else is editing this file")

    text = base_bytes.decode("utf-8")
    mutated = text
    for i, edit in enumerate(case.edits):
        # (2) EVERY edit is counted. With two or more, a whole-file "did it
        # change" test stays true while a later anchor silently misses, so a
        # partial mutation reads as complete.
        hits = mutated.count(edit.anchor)
        if hits != 1:
            return Result(case, ABORTED,
                          f"edit {i + 1}/{len(case.edits)}: anchor matched "
                          f"{hits}x, expected exactly 1")
        mutated = mutated.replace(edit.anchor, edit.replacement, 1)

    # (3) the declared validator, never a silent skip.
    err = _validate(case, mutated)
    if err:
        return Result(case, ABORTED, err)

    # TOCTOU: the drift check above happened before `compile()` (and, for a
    # `bash` validator, before an out-of-process `bash -n`). A peer edit landing
    # in that window would be clobbered by the write below and then "restored"
    # to the baseline -- silently destroying their work, which is the one thing
    # guarantee (6) exists to prevent. Re-check immediately before writing.
    if target.read_bytes() != base_bytes:
        return Result(case, CONFLICT,
                      "the target changed between the drift check and the "
                      "mutation write -- a peer's edit was left untouched")
    target.write_text(mutated, encoding="utf-8")
    wrote = _sha(target)
    verdict: Result | None = None
    conflict: list[str] = []
    try:
        cmd = [case.python or python, "-m", "pytest", case.test, "-q", "--no-header",
               "-p", "no:cacheprovider", *case.pytest_args]
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            env=_child_env({**(env or {}), **case.env}), timeout=timeout,
        )
        out = proc.stdout + proc.stderr
        # THE EXIT CODE IS THE VERDICT, not the stdout text. pytest's codes are
        # an enumerated contract (pytest.ExitCode): 0 OK, 1 TESTS_FAILED,
        # 2 INTERRUPTED, 3 INTERNAL_ERROR, 4 USAGE_ERROR, 5 NO_TESTS_COLLECTED.
        # ONLY 0 and 1 mean the tests actually ran.
        #
        # MEASURED, and this was a live false GREEN: a mutation that COMPILES but
        # raises at import time (an invalid module-level `re.compile`, a deleted
        # constant another line references, a changed decorator argument) gives
        # rc=2 with "1 error in 0.26s" on stdout. The old rule -- has a result
        # line AND rc != 0 -- read that as BIT while the test never ran.
        # `compile()` closes the SyntaxError door only, so the docstring's claim
        # to have closed this class was false until the exit code became the
        # verdict. `destructive_command_guard.py` carries a module-level
        # re.compile, so this is reachable from the shipped gate.
        if proc.returncode not in (0, 1):
            verdict = Result(case, ABORTED,
                             f"pytest exit {proc.returncode}: the run did not "
                             "happen (collection error, usage error, or nothing "
                             "collected)", out)
        elif not _has_result_line(proc.stdout):
            verdict = Result(case, ABORTED, "no pytest result line -- the run did "
                             "not happen (lock, guard, collection error?)", out)
        else:
            verdict = Result(case, BIT if proc.returncode == 1 else SURVIVED, "", out)
    except subprocess.TimeoutExpired:
        verdict = Result(case, ABORTED, f"test timed out after {timeout}s")
    finally:
        # (6) restore what this mutation wrote -- and NEVER lose the file.
        #
        # `_sha` raises when the target is gone or unreadable, and an exception
        # here escapes into `sweep`, whose own cleanup deletes the snapshot
        # directory -- the file's only remaining copy. MEASURED with a child that
        # unlinks the target: FileNotFoundError, target absent, zero surviving
        # snapshots. Unrecoverable loss, in the tool whose stated purpose is that
        # it must never damage anyone's work. An over-broad cleanup fixture in
        # the test under mutation is enough to trigger it.
        try:
            current: str | None = _sha(target) if target.exists() else None
        except OSError:
            current = None
        if current is None or current == wrote:
            # Gone, unreadable, or still exactly what we wrote -> put it back.
            # copy2 rather than write_bytes: when the child DELETED the target,
            # write_bytes recreates it at the default creation mode and the
            # original permission bits are gone. MEASURED 0o755 -> 0o644. The two
            # guards in the shipped manifest happen to be 0644 so it would not
            # bite today, but this is a general library and a hook script is
            # normally executable -- restoring it unrunnable is a quieter kind of
            # the same damage blocker 4 was about. `baseline` came from copy2, so
            # it carries the mode.
            shutil.copy2(baseline, target)
        else:
            # A peer edited it mid-run. PRESERVE their work -- and SAY SO, which
            # the previous version did not: CONFLICT was defined, rendered, and
            # never produced, so a verdict computed against a file that changed
            # mid-flight was returned as though it were trustworthy. For the last
            # case, the only case, or the only case touching that path, the next
            # case's drift check never runs and the conflict was invisible.
            conflict.append(
                "the target changed while the test ran -- a peer's edit was "
                "PRESERVED, so this case's verdict is not trustworthy"
            )

    # AFTER the finally, so a conflict detected during restore is visible.
    if verdict is None:  # pragma: no cover -- every branch above assigns one
        verdict = Result(case, ABORTED, "no verdict produced")
    if conflict:
        return Result(case, CONFLICT, conflict[0], verdict.stdout)
    return verdict


def assert_green_baseline(
    cases: list[Case], *, cwd: Path, python: str, env: dict[str, str] | None,
    timeout: int,
) -> str | None:
    """Every case's test must PASS before anything is mutated.

    Without this the whole sweep is meaningless in the most flattering
    direction: an already-red test "fails" under every mutation, so each case
    reports BIT and the sweep declares success while proving nothing. Raised by
    the session that had been doing this check by hand.
    """
    # The env is part of the identity: a case carrying its own `env` must be
    # baselined UNDER that env. Running it under the sweep's env instead gives a
    # false RED (the case's flag is missing, the test fails, and the whole sweep
    # raises) -- so per-case env and the baseline gate were mutually exclusive.
    # The mirror is worse and silent: a baseline GREEN under the wrong
    # environment makes every later BIT from that case meaningless, which is the
    # exact thing this gate exists to prevent. Frozen to a tuple because a dict
    # is unhashable, which is why it was dropped from the key in the first place.
    seen = {(c.test, c.python, c.pytest_args, tuple(sorted(c.env.items())))
            for c in cases}
    for test, py, extra, case_env in seen:
        try:
            proc = _baseline_run(test, py, extra, case_env, cwd, python, env, timeout)
        except subprocess.TimeoutExpired:
            # Every other outcome in this module is enumerated; a raw traceback
            # here was the one path that escaped the contract. Nothing has been
            # mutated at this point, so this is purely a reporting fix.
            return (f"baseline run of {test} timed out after {timeout}s -- it "
                    "cannot be established as green, so no mutation is trustworthy")
        if not _has_result_line(proc.stdout):
            return f"baseline run of {test} produced no result line"
        if proc.returncode != 0:
            return (f"baseline is RED: {test} already fails before any mutation, "
                    "so every 'BIT' from it would be meaningless")
    return None


def _baseline_run(test, py, extra, case_env, cwd, python, env, timeout):
    """One baseline invocation, factored out so the timeout has somewhere to land."""
    return subprocess.run(
        [py or python, "-m", "pytest", test, "-q", "--no-header",
         "-p", "no:cacheprovider", *extra],
        cwd=str(cwd), capture_output=True, text=True,
        env=_child_env({**(env or {}), **dict(case_env)}), timeout=timeout,
    )


def sweep(
    cases: list[Case],
    *,
    cwd: Path,
    python: str = sys.executable,
    env: dict[str, str] | None = None,
    timeout: int = 900,
    available: set[str] | None = None,
    check_baseline: bool = True,
) -> Sweep:
    """Run every case, restoring between each. Never leaves a file mutated."""
    if not cases:
        raise ValueError("a sweep with no cases proves nothing")

    if check_baseline:
        # A case gated on state this run does not have CANNOT be baselined: its
        # test fails without that state, the gate reads that as a RED baseline,
        # and the RuntimeError kills the whole sweep -- so one unavailable
        # resource silently prevents every OTHER case from running. That
        # contradicts the per-case contract, which says such a case reports
        # ABORTED and leaves the rest intact. `run_case` still aborts it, loudly.
        #
        # The test for requirement 5 could not catch this: it passes
        # check_baseline=False, so it never exercised the interaction between the
        # two mechanisms. Both are now asserted together.
        baselineable = [
            c for c in cases
            if all(r in (available or set()) for r in c.requires)
        ]
        if baselineable:
            problem = assert_green_baseline(
                baselineable, cwd=cwd, python=python, env=env, timeout=timeout
            )
            if problem:
                raise RuntimeError(problem)

    # `~/tmp` is a Genesis-container convention (CLAUDE.md "Temp files"), NOT a
    # property of hosts in general -- a CI runner's HOME has no `tmp`, and
    # `mkdtemp` against an absent parent raises FileNotFoundError. Create it.
    tmp_root = Path.home() / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="mutation-sweep-", dir=str(tmp_root)))
    baselines: dict[Path, Path] = {}
    completed = False
    try:
        for c in {c.path for c in cases}:
            snap = tmpdir / (str(c).replace("/", "__"))
            shutil.copy2(c, snap)
            baselines[c] = snap
        out = Sweep()
        for case in cases:
            out.results.append(
                run_case(case, baselines[case.path], cwd=cwd, python=python,
                         env=env, timeout=timeout, available=available)
            )
        completed = True
        return out
    finally:
        if completed:
            shutil.rmtree(tmpdir, ignore_errors=True)
        else:
            # An abnormal exit is exactly when the snapshots are worth keeping:
            # they may be the only surviving copy of a file whose restore did not
            # finish. Deleting them here is what turned a crash into data loss.
            print(f"mutation-sweep: baselines PRESERVED for recovery: {tmpdir}",
                  file=sys.stderr)


def render(result: Sweep) -> str:
    lines = []
    for r in result.results:
        mark = {BIT: "BIT     ", SURVIVED: "SURVIVED", ABORTED: "ABORTED ",
                CONFLICT: "CONFLICT"}[r.outcome]
        lines.append(f"  {mark}  {r.case.label}")
        if r.detail:
            lines.append(f"            {r.detail}")
        if r.outcome == SURVIVED:
            lines.append(f"            expected to break: {r.case.why}")
    lines.append("")
    lines.append(f"  {len(result.bit)} bit, {len(result.survived)} SURVIVED, "
                 f"{len(result.aborted)} ABORTED")
    if result.survived:
        lines.append("  A survivor means the test does not pin the property its "
                     "case names -- or the mutation was behaviourally null.")
    if result.aborted:
        lines.append("  An abort is NOT a pass: those cases established nothing.")
    return "\n".join(lines)


def cases_from_json(doc: dict, repo: Path) -> list[Case]:
    out = []
    for c in doc["cases"]:
        out.append(Case(
            label=c["label"], path=repo / c["path"], test=c["test"],
            why=c["why"], validator=c["validator"],
            anchor=c.get("anchor"), replacement=c.get("replacement"),
            edits=tuple(Edit(e["anchor"], e["replacement"]) for e in c.get("edits", ())),
            python=c.get("python"), pytest_args=tuple(c.get("pytest_args", ())),
            env=c.get("env", {}), requires=tuple(c.get("requires", ())),
        ))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", type=Path, help="JSON file with a `cases` list")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument(
        "--available", action="append", default=[], metavar="RESOURCE",
        help="declare a shared resource as present (repeatable). A case whose "
             "`requires` names something not declared here ABORTS -- loudly, "
             "because a skipped case and a killed one look identical in a tally.",
    )
    args = ap.parse_args(argv)

    doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = cases_from_json(doc, args.repo)
    # The child env is an ALLOWLIST (see _child_env), so anything the tests need
    # must be DECLARED. A manifest-level `env` covers the sweep; a case's own
    # `env` overrides it. VERIFIED 2026-09-07 that the shipped cases pass under
    # the minimal set, but leaving this undeclarable would be a trap for the
    # first case that needs a marker variable.
    env = {"PYTHONPATH": str(args.repo / "src"), **doc.get("env", {})}
    available = set(doc.get("available", ())) | set(args.available)
    result = sweep(cases, cwd=args.repo, python=args.python, env=env,
                   available=available)
    print(render(result))
    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
