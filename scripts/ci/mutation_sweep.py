#!/usr/bin/env python3
"""Verify-RED as a library: break the mechanism, prove the test notices.

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

All fifteen edited the shared source IN PLACE and restored it, and the drift
check -- the one people skip -- is the only one whose absence damages someone
ELSE's work. This module first made it mandatory; review showed the in-place
shape was itself the defect (per a premise check of its review threads,
2026-10-03: 15 of 28 findings came from writing the caller's tree and putting it
back). A check can only narrow a check-then-write window. Not writing closes it:
a sweep runs in private copies (`isolated_copy`), one per case, holding what is on disk, and
the caller's WORKING TREE is never written. (A `git worktree` shares the
caller's git metadata -- config, hooks, refs -- so a test that runs git
commands in its cwd can still change those.)

WHAT A SWEEP GUARANTEES, per case:
  1. The anchor matches exactly once. With two or more edits, a whole-file
     "did it change" test stays true while a later anchor silently misses, so
     a partial mutation reads as complete.
  2. The mutated source COMPILES, using the file's own parser. `compile()`, not
     `ast.parse`: the latter accepts context-invalid constructs (a `return`
     outside a function), and the SyntaxError then surfaces at COLLECTION, where
     a nonzero exit reads as a successful RED.
  3. The test command produced a RESULT LINE. No line means the run never
     happened -- a lock, a guard, a timeout -- and "all mutations survived" from
     a sweep that never ran is the confident false negative this class is famous
     for. It is an ABORT, never a survival. The line is read by ONE grammar
     (`parse_pytest_summary`), by exact outcome keyword, and a baseline counts
     as green only when the target PASSED -- never xfailed, xpassed, skipped
     or deselected.
  4. Each baseline and each case runs in its OWN copy, with its own TMPDIR,
     made from the same caller tree: every copy must match the first one's
     fingerprint (HEAD plus the overlaid files), or its case ABORTS. Whatever a
     test did to the target -- deleted it, replaced it with a directory, a
     symlink or a hard link -- the next write removes it and creates a fresh
     file, so no write follows a link out of the copy.
  5. A BIT needs PASS, FAIL, PASS in one copy: the unmutated test passes,
     the mutated one fails, and the unmutated one passes again with the
     pristine target restored. The first run catches state an earlier case left
     that already breaks the test; the last catches a failure the mutated run
     caused through state rather than through the mutation. Either one failing
     is an ABORT. Not covered: a flaky test, and state that changes between runs
     for reasons outside the copy.

WHAT IT DELIBERATELY DOES NOT DO. It does not generate mutations. Picking the
mutation is the thinking part -- a behaviourally-null edit (swapped operands
that commute, a type annotation Python does not enforce) produces a GREEN that
means nothing, and no generator knows which is which. It also does not replace
`mutmut`/`cosmic-ray`-style coverage sweeps; this is targeted verify-RED, where
each case names the property it is supposed to break.

Nor does it defend against a test that LIES: the verdict is read from the
child's exit code and summary line, both of which a test controls. That is
fine for an advisory check run on tests the reviewer can read; a blocking gate
would need its manifest and tests taken from the base branch.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tokenize
import uuid
from collections.abc import Iterator
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


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    """The ONE place that decides what a sweep's child process may see.

    An ALLOWLIST, not a subtract-list: a stray inherited variable is how a
    'deliberate concurrent run' lever, a disabled guard, or a pointer at another
    worktree silently changes what the test under mutation actually exercises.

    It is for a DETERMINISTIC verdict, not for secrecy: the child runs as the
    same user, so it can read the sweep's own environment and anything under
    HOME. A job that runs this on untrusted tests must carry no secrets.
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
        **(
            {"GENESIS_PYTEST_LOCK_HELD": os.environ["GENESIS_PYTEST_LOCK_HELD"]}
            if os.environ.get("GENESIS_PYTEST_LOCK_HELD")
            else {}
        ),
        # TMPDIR is a PATH, not a behaviour lever. Dropping it sends every child's
        # temp to /tmp, which this project forbids for anything large, and makes
        # tests/conftest.py take its no-op basetemp branch on a dev box.
        "TMPDIR": os.environ.get("TMPDIR", str(Path.home() / "tmp")),
        # CPython validates cached bytecode on (mtime_seconds, size), so a
        # same-length mutation restored inside the same integer second leaves a
        # .pyc compiled from the MUTATED source for the next run to import.
        # Narrow, and this tool's whole value is that its RED/GREEN means something.
        "PYTHONDONTWRITEBYTECODE": "1",
        # ...and the mirror, which DONTWRITEBYTECODE does not cover: it stops
        # WRITING caches, not READING them. A timestamp-valid .pyc for the
        # baseline plus a same-length mutation inside its recorded mtime second
        # makes the child import the BASELINE code, so a real catch reads as
        # SURVIVED. With a cache prefix set, CPython (and pytest's assertion
        # rewriter) look ONLY under the prefix, never in the source tree's
        # __pycache__; a fresh, never-created path per child means nothing is
        # read and, with writes off, nothing is left behind. The cost is that
        # every child compiles its imports cold.
        "PYTHONPYCACHEPREFIX": os.path.join(
            os.environ.get("TMPDIR", str(Path.home() / "tmp")),
            f"mutation-sweep-nopyc-{uuid.uuid4().hex}",
        ),
    }
    env.update(extra or {})
    return env


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
        proc = subprocess.run(
            ["bash", "-n"], input=text, capture_output=True, text=True, timeout=60
        )
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


# --------------------------------------------------------------------------
# THE ISOLATED COPY.
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> bytes:
    """``git -C repo <args>`` -> stdout bytes; RuntimeError naming git's stderr.

    GIT_* is dropped from the environment. Run from inside a git hook (a
    pre-commit that runs the suite), an inherited GIT_DIR / GIT_WORK_TREE /
    GIT_INDEX_FILE aims every call at the HOOK's repository instead of ``repo``,
    and the copy would be of the wrong tree. Bytes, not text: a path git prints
    is a filename, and filenames need not be valid UTF-8.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, env=env, timeout=7200
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {repo} failed: {os.fsdecode(proc.stderr).strip()}"
        )
    return proc.stdout


def _git_paths(repo: Path, *args: str) -> set[str]:
    return {os.fsdecode(p) for p in _git(repo, *args).split(b"\0") if p}


def _contained(root: Path, path: Path) -> bool:
    """``path`` lies inside ``root`` once every symlinked DIRECTORY on the way
    is followed. The final component is not followed: it is what gets written,
    and every writer here removes whatever sits there first."""
    return path.parent.resolve().is_relative_to(root)


def _clear(path: Path) -> None:
    """Remove whatever sits at ``path`` -- a file, a link, a directory -- without
    following a link."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def _put(root: Path, path: Path, data: bytes, mode: int) -> None:
    """Make ``path`` a FRESH regular file holding ``data``, inside the copy.

    Fresh is the point: removing what is there and creating with O_EXCL |
    O_NOFOLLOW means no write ever lands in an inode or through a link that a
    test left behind -- a symlink or hard link to a file outside the copy would
    otherwise carry the write out of it. Refuses a directory that resolves
    outside ``root``. Raises ``OSError``.
    """
    if not _contained(root, path):
        raise OSError(f"{path} resolves outside the copy; refusing to write it")
    path.parent.mkdir(parents=True, exist_ok=True)
    _clear(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        os.fchmod(fh.fileno(), mode)


def _overlay(top: Path, tree: Path) -> str:
    """Make the fresh checkout at ``tree`` match ``top``'s WORKING TREE, and
    return a digest of exactly what was copied.

    The paths: `git diff HEAD` (staged and unstaged, renames split), untracked
    files that are not ignored, and every entry flagged assume-unchanged or
    skip-worktree, whose changes `git diff` does not report. Each is copied as
    it is on disk (a symlink as a symlink) or removed from the copy. Ignored
    files and untracked nested repositories (a trailing slash) are not part of
    this repository and are left out.

    The digest covers every overlaid path with what was put there (the bytes
    and mode copied, the link target, or "gone"), so two copies with the same
    HEAD and the same digest hold the same tree.
    """
    digest = hashlib.sha256()
    paths = _git_paths(top, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    paths |= _git_paths(top, "ls-files", "--others", "--exclude-standard", "-z")
    for entry in _git(top, "ls-files", "-v", "-z").split(b"\0"):
        if entry[:1].islower() or entry[:1] == b"S":
            paths.add(os.fsdecode(entry[2:]))
    for rel in sorted(p for p in paths if not p.endswith("/")):
        src, dst = top / rel, tree / rel
        if not _contained(tree, dst):
            raise RuntimeError(f"{rel} would be copied outside the copy")
        digest.update(os.fsencode(rel) + b"\0")
        if os.path.islink(src):
            link = os.readlink(src)
            _clear(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(link, dst)
            digest.update(b"link\0" + os.fsencode(link) + b"\0")
        elif src.is_file():
            # Permission bits only: setuid/setgid have no business in a copy.
            data, mode = src.read_bytes(), src.stat().st_mode & 0o777
            _put(tree, dst, data, mode)
            digest.update(b"file\0%o\0" % mode + hashlib.sha256(data).digest())
        else:
            # Deleted -- or now a DIRECTORY, whose files are entries of their
            # own (sorted after it); HEAD's file must not stay in their way.
            _clear(dst)
            digest.update(b"gone\0")
    return digest.hexdigest()


#: The calls `shutil.rmtree` (Python 3.12) hands its error handler that mean "I
#: could not LIST or ENTER this directory" -- as opposed to "I could not remove
#: this entry". `os.lstat` is here because rmtree calls the handler with it when
#: `entry.stat()` fails, which happens when the PARENT has lost search
#: permission (READ from shutil's source, 3.12.3).
_RMTREE_LISTING = (os.open, os.scandir, os.lstat)


def _force_rmtree(path: Path) -> None:
    """rmtree that first makes whatever a test left unreadable writable again.

    A removal failure (`unlink`, `rmdir`) is fixed by making the PARENT
    writable and retrying the same call. A listing failure (`open`, `scandir`,
    `lstat`) is fixed by making the parent AND the path itself searchable, then
    removing the path's whole subtree again -- retrying `os.open(p)` alone would
    raise TypeError (it needs flags) and remove nothing. A path that has gone in
    the meantime counts as removed. Anything still failing is left for the
    caller's "could not fully remove" report; each path is retried as a listing
    failure at most once, so a mode that cannot be changed cannot loop.

    Nothing ABOVE ``path`` is ever chmodded: when the failing entry is ``path``
    itself, its parent is the caller's temp root, which the sweep does not own.
    `os.close` failing leaves nothing to retry, and a failed `os.path.islink`
    check is retried as the unlink it was guarding.
    """
    top = os.fspath(path)
    tried: set[str] = set()

    def retry(func, p, _exc):
        if func is os.close:
            return
        if func is os.path.islink:
            func = os.unlink
        if os.fspath(p) != top:
            with contextlib.suppress(OSError):
                os.chmod(os.path.dirname(p), 0o700)
        if func in _RMTREE_LISTING:
            if os.fspath(p) in tried:
                return
            tried.add(os.fspath(p))
            with contextlib.suppress(OSError):
                if not os.path.islink(p):  # never chmod THROUGH a link
                    os.chmod(p, 0o700)
            with contextlib.suppress(OSError):
                if os.path.isdir(p) and not os.path.islink(p):
                    shutil.rmtree(p, onexc=retry)
                else:
                    os.unlink(p)
            return
        with contextlib.suppress(OSError):
            func(p)

    shutil.rmtree(path, onexc=retry)


@contextlib.contextmanager
def isolated_copy(
    repo: Path, tmp_root: Path | None = None, info: dict | None = None
) -> Iterator[Path]:
    """Yield ``repo``'s place inside a private copy of its whole repository.

    The copy is a detached `git worktree` of HEAD under a fresh directory in
    ``tmp_root`` (default ``~/tmp``: a real disk, never the session's TMPDIR,
    because a checkout is large temp), overlaid with the caller's working-tree
    changes (`_overlay`). On ANY exit the copy and its registration are removed
    -- this copy's only: `git worktree remove` on the path this call created,
    then, if git no longer recognises it (a test deleted the directory), the
    administrative entry recorded at creation.

    ``info``, when given, receives ``holder`` (the private directory the copy
    sits in, removed with it: the place for this copy's own TMPDIR) and
    ``fingerprint`` (HEAD plus the overlay digest), which is equal for two
    copies of the same caller tree.
    """
    try:
        top = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip()))
    except RuntimeError as exc:
        raise RuntimeError(
            f"the sweep copies the tree with git, and {repo} is not inside a git work tree ({exc})"
        ) from exc
    top, repo = top.resolve(), repo.resolve()
    parent = Path(tmp_root) if tmp_root is not None else Path.home() / "tmp"
    if parent.resolve().is_relative_to(top):
        raise ValueError(
            f"tmp_root {parent} is inside the repository {top}; the "
            "copy would be written into the tree it must not touch"
        )
    parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="mutation-sweep-", dir=str(parent))).resolve()
    tree = holder / holder.name  # the basename names git's admin entry too
    admin: Path | None = None
    try:
        _git(top, "worktree", "add", "--detach", "--quiet", str(tree), "HEAD")
        admin = Path(os.fsdecode(_git(tree, "rev-parse", "--absolute-git-dir").strip()))
        overlay = _overlay(top, tree)
        if info is not None:
            head = os.fsdecode(_git(tree, "rev-parse", "HEAD").strip())
            info["holder"] = holder
            info["fingerprint"] = f"{head}:{overlay}"
        yield tree / repo.relative_to(top)
    finally:
        with contextlib.suppress(RuntimeError):
            _git(top, "worktree", "remove", "--force", str(tree))
        if os.path.lexists(holder):
            _force_rmtree(holder)
        if admin is not None and admin.parent.name == "worktrees" and admin.exists():
            shutil.rmtree(admin, ignore_errors=True)
        if os.path.lexists(holder) or (admin is not None and admin.exists()):
            print(f"mutation-sweep: could not fully remove the copy at {holder}", file=sys.stderr)


@dataclass(frozen=True)
class _Target:
    """A case's file INSIDE the copy, and its content when the copy was made."""

    path: Path
    pristine: bytes
    mode: int


def _load_target(root: Path, rel: Path) -> _Target | str:
    """The copy's file at ``rel``, or why it is refused (named by ``rel``: the
    copy is gone by the time anyone reads the message). A SYMLINK, or a
    directory on the way that resolves outside the copy, would carry the
    mutation to a file that may be the caller's own -- and which file is meant
    is ambiguous, so it is refused rather than guessed."""
    path = root / rel
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return f"{rel} does not exist in the tree under test"
    except OSError as exc:
        return f"{rel} cannot be inspected: {exc}"
    if stat.S_ISLNK(st.st_mode):
        return (
            f"{rel} is a SYMLINK; mutating it writes through to the "
            "referent, possibly outside the copy. Point the case at the real "
            "file."
        )
    if not _contained(root, path):
        return f"{rel} resolves outside the copy"
    if not stat.S_ISREG(st.st_mode):
        return f"{rel} is not a regular file"
    return _Target(path, path.read_bytes(), stat.S_IMODE(st.st_mode))


def _repo_relative(repo: Path, path: Path) -> Path:
    """``path`` relative to ``repo``, its directories resolved; ValueError when
    that lands outside the repository."""
    p = path if path.is_absolute() else repo / path
    p = p.parent.resolve() / p.name
    if not p.is_relative_to(repo):
        raise ValueError(f"case path {path} resolves outside the repository {repo}")
    return p.relative_to(repo)


#: Where a path can hide inside one value: words, `--opt=value`, `a:b` path
#: lists (`os.pathsep`), `a,b` lists.
_PATH_PIECES = re.compile(r"[\s=,:;]+")


def _link_escape(repo: Path, rel: str) -> str | None:
    """The first symlink on ``rel``'s way through ``repo`` that leads OUT of the
    copy it is reproduced in, or None.

    The copy keeps the caller's symlinks as they are. A link with an ABSOLUTE
    target, or a relative one that resolves outside the repository, points
    the copy back at the caller's tree (or elsewhere), so a selector or import
    root that goes through it runs the caller's unmutated source -- a test that
    finds its source from ``__file__`` follows it. A relative link that stays
    inside the repository stays inside the copy and is fine.
    """
    real = Path(os.path.realpath(repo))
    here = repo
    for part in Path(rel).parts:
        here = here / part
        if os.path.islink(here):
            target = os.readlink(here)
            if os.path.isabs(target) or not Path(os.path.realpath(here)).is_relative_to(real):
                return f"{here.relative_to(repo).as_posix()} -> {target}"
    return None


def _back_pointer(repo: Path, value: str, top: Path | None = None) -> str | None:
    """The part of ``value`` that points a child BACK at the caller's tree, or None.

    ONE rule for every channel a case controls -- the selector, each
    `pytest_args` token, each env value -- because a child pointed back at the
    caller's tree imports the UNMUTATED file and reports SURVIVED for a mutation
    its test would catch. Refused:
      * an absolute path that lands inside the repository, judged both
        lexically (``/x/./repo/src``) and after resolving symlinks (an alias of
        the repository);
      * a relative path that climbs out (``../../repo/src``): the child runs in
        the COPY, so climbing out of it can only reach somewhere else, and the
        caller's tree is one of the places it can reach;
      * an ``@argfile``, whose contents this check cannot see;
      * the repository's path anywhere in the value (``file://...``).
    A relative path that stays inside (``PYTHONPATH=src``) resolves inside the
    copy and is fine.

    A value is split the way its consumer splits it -- shell words first
    (pytest runs ``shlex.split`` on an ``-o`` value, so a quoted path is still
    a path), then the separators above -- and judged against the repository
    AND its git toplevel (``top``), since a sweep may be pointed at a
    subdirectory. Leading slashes are collapsed (``//repo`` is ``/repo``).

    NOT covered: paths a test derives from HOME, the interpreter a case names,
    and an editable install's ``.pth``, which sits after PYTHONPATH on
    ``sys.path``. Each of these usually yields a false SURVIVED; with someone
    editing the caller's tree DURING the sweep, a run could read it red, so the
    rule is to name everything repository-relative.
    """
    roots = {repo, Path(os.path.realpath(repo))}
    if top is not None:
        roots |= {top, Path(os.path.realpath(top))}
    for root in roots:
        # The path itself, not a sibling that merely starts with it (`repo-data`).
        if re.search(re.escape(str(root)) + r"(?![\w.-])", value):
            return value
    try:
        words = shlex.split(value)
    except ValueError:
        words = [value]
    for word in words:
        for piece in _PATH_PIECES.split(word):
            if not piece:
                continue
            if piece.startswith("@"):
                return piece
            if os.path.isabs(piece):
                piece_abs = "/" + piece.lstrip("/")
                lexical = Path(os.path.normpath(piece_abs))
                resolved = Path(os.path.realpath(piece_abs))
                if any(p.is_relative_to(r) for p in (lexical, resolved) for r in roots):
                    return piece
            else:
                norm = os.path.normpath(piece)
                if norm == ".." or norm.startswith("../"):
                    return piece
                if escape := _link_escape(repo, norm):
                    return f"{piece} (through {escape})"
    return None


def _refuse_back_pointers(repo: Path, env: dict[str, str] | None, cases: list[Case]) -> None:
    """Raise ValueError naming the first selector, pytest arg or env value that
    `_back_pointer` refuses. Runs before any copy is made, so a bad case fails
    the whole sweep loudly rather than producing a meaningless verdict."""
    try:
        top: Path | None = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip()))
    except RuntimeError:
        top = None  # `isolated_copy` refuses a tree that is not a git work tree
    # The copy owns these; a manifest value would quietly share one temp
    # directory between cases again (`_copy_env`).
    for scope, mapping in [("sweep", env or {})] + [(f"case {c.label!r}", c.env) for c in cases]:
        for key in ("TMPDIR", "PYTHONPYCACHEPREFIX"):
            if key in mapping:
                raise ValueError(
                    f"{scope} env sets {key}, which each case's copy sets for itself; leave it out"
                )
    hint = (
        "The sweep runs in an isolated copy, so a child pointed there tests "
        "the UNMUTATED file; name test files and import roots repo-relative "
        "(`pythonpath`) instead"
    )
    for key, value in (env or {}).items():
        if bad := _back_pointer(repo, value, top):
            raise ValueError(
                f"sweep env {key}={value!r} points into the tree under test ({bad!r}). {hint}"
            )
    for c in cases:
        for key, value in c.env.items():
            if bad := _back_pointer(repo, value, top):
                raise ValueError(
                    f"case {c.label!r} env {key}={value!r} points into "
                    f"the tree under test ({bad!r}). {hint}"
                )
        for arg in c.pytest_args:
            if bad := _back_pointer(repo, arg, top):
                raise ValueError(
                    f"case {c.label!r} pytest arg {arg!r} points into "
                    f"the tree under test ({bad!r}). {hint}"
                )


def _copy_selector(repo: Path, case: Case) -> str:
    """The case's pytest selector, with its FILE part made repository-relative,
    so it names the copy's file when the child runs in the copy.

    An absolute file inside the repository is rewritten; one outside it, or a
    relative one that climbs out, is refused (ValueError) -- either would
    collect a test, and import source, from outside the copy.
    """
    if case.test.startswith("-"):
        raise ValueError(
            f"case {case.label!r} test {case.test!r} would be read as a pytest option, not a test"
        )
    file_part, sep, rest = case.test.partition("::")
    rewritten = case.test
    if os.path.isabs(file_part):
        file_abs = "/" + file_part.lstrip("/")
        real = Path(os.path.realpath(repo))
        for path, root in (
            (Path(os.path.normpath(file_abs)), repo),
            (Path(os.path.realpath(file_abs)), real),
        ):
            if path.is_relative_to(root):
                file_part = path.relative_to(root).as_posix()
                rewritten = file_part + sep + rest
                break
        else:
            raise ValueError(
                f"case {case.label!r} test {case.test!r} is outside the "
                f"repository {repo}; the child runs in a copy of it"
            )
    # Checked AFTER the rewrite too: `/repo/linked/t.py` becomes `linked/t.py`.
    if escape := _link_escape(repo, os.path.normpath(file_part)):
        raise ValueError(
            f"case {case.label!r} test {case.test!r} goes through a symlink that leads "
            f"out of the copy ({escape}); name the real path"
        )
    if _back_pointer(repo, file_part):
        raise ValueError(
            f"case {case.label!r} test {case.test!r} climbs out of the "
            "repository; name it repository-relative"
        )
    return rewritten


# --------------------------------------------------------------------------
# RUNNING.
# --------------------------------------------------------------------------


def _pytest(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess:
    """The ONE place a pytest child is started, baseline and mutation alike.

    Output is captured as BYTES and decoded with ``errors="replace"``: a test
    may write any bytes (``os.write`` under ``-s``), and ``text=True`` raised
    UnicodeDecodeError before either verdict could be reached. Replacement
    cannot forge a verdict: U+FFFD is outside the summary grammar, so a damaged
    line can only become unreadable, which ABORTS.

    Each child runs in its OWN session, and its whole process group is killed
    when it ends -- on a timeout, and after a normal exit too. `subprocess.run`
    kills only pytest on a timeout, so a test's subprocesses kept running (and
    holding locks) after the sweep had reported and removed the copy.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        # Bounded: a descendant that started its OWN session escapes the group
        # kill and may hold the pipes open; the timeout is reported regardless.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=10)
        raise
    finally:
        _kill_group(proc.pid)
    return subprocess.CompletedProcess(
        cmd,
        proc.returncode,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


def _kill_group(pgid: int) -> None:
    """SIGKILL every process left in the child's group (it leads its own
    session, so its pid is the group id). A group already gone is fine."""
    if pgid <= 1:  # never signal init or every process this user owns
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _pytest_cmd(python: str, test: str, extra: tuple[str, ...]) -> list[str]:
    return [python, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider", *extra]


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


def run_case(
    case: Case,
    target: _Target,
    *,
    cwd: Path,
    python: str,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
    available: set[str] | None = None,
    selector: str | None = None,
) -> Result:
    """Run the case's test from ``cwd`` unmutated, then mutated, then unmutated
    again: a BIT needs PASS, FAIL, PASS in this one copy.

    The first run proves the test passes HERE before the mutation, so state a
    previous case left (in HOME, or git refs and config shared between copies)
    that already breaks it is an ABORT, not a catch. The control after a BIT
    puts the pristine file back and runs the same test again, so a failure the
    mutated run caused through state rather than through the mutation is an
    ABORT too. A previous case's leftover that the mutated run USES UP is the
    case the first run exists for: an after-only control passes once it is
    gone. Not covered: a flaky test (a flaky failure followed by a passing
    control reads as BIT) and state that changes between runs for reasons
    outside the copy. A SURVIVED needs no control: it already fails closed.
    ``selector`` is the case's test rewritten for the copy (`_copy_selector`);
    it defaults to ``case.test``.
    """
    test = selector or case.test
    # A case gated on shared live state ABORTS when that state is absent. It does
    # NOT skip: a skipped case and a killed one look identical in a summary line,
    # and "11/11 bit" means nothing if two of them never ran.
    missing = [r for r in case.requires if r not in (available or set())]
    if missing:
        return Result(
            case,
            ABORTED,
            f"requires {', '.join(missing)}, which this run did not "
            "declare available -- not skipped, because a skipped case "
            "and a killed one are indistinguishable in a tally",
        )
    try:
        text, encoding = _decode(case, target.pristine)
    except ValueError as exc:
        return Result(case, ABORTED, str(exc))
    mutated, problem = apply_edits(text, case.edits)
    if problem:
        return Result(case, ABORTED, problem)

    # (2) the declared validator, never a silent skip.
    err = _validate(case, mutated)
    if err:
        return Result(case, ABORTED, err)
    try:
        data = mutated.encode(encoding)
    except UnicodeEncodeError as exc:
        return Result(
            case,
            ABORTED,
            f"the mutation cannot be written in the target's own encoding ({encoding}): {exc}",
        )
    cmd = _pytest_cmd(case.python or python, test, case.pytest_args)
    problem = _unmutated_problem(
        cmd, cwd=cwd, env={**(env or {}), **case.env}, timeout=timeout, test=test
    )
    if problem:
        return Result(
            case,
            ABORTED,
            "before the mutation, the UNMUTATED test does not pass in this copy, so "
            f"no verdict from it means anything ({problem})",
        )
    try:
        _put(cwd, target.path, data, target.mode)
    except OSError as exc:
        return Result(case, ABORTED, f"could not write the mutation: {exc}")
    try:
        proc = _pytest(cmd, cwd=cwd, env=_child_env({**(env or {}), **case.env}), timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(case, ABORTED, f"test timed out after {timeout}s")
    except OSError as exc:  # e.g. a `python` the case names that does not exist
        return Result(case, ABORTED, f"could not start the test run: {exc}")
    # (3) the exit code AND the summary decide, together, in `mutation_verdict`
    # -- the one place a mutated run is classified. Only stdout is parsed:
    # pytest writes its summary there, and stderr belongs to whatever else ran.
    outcome, detail = mutation_verdict(proc.returncode, proc.stdout)
    output = proc.stdout + proc.stderr
    if outcome != BIT:
        return Result(case, outcome, detail, output)

    # (4) the control.
    why = _reset(cwd, target)
    if why:
        return Result(
            case, ABORTED, f"could not restore the target for the control run: {why}", output
        )
    problem = _unmutated_problem(
        cmd, cwd=cwd, env={**(env or {}), **case.env}, timeout=timeout, test=test
    )
    if problem:
        return Result(
            case,
            ABORTED,
            "contaminated: the UNMUTATED test does not pass in this copy either, so the "
            f"failure need not have come from the mutation ({problem})",
            output,
        )
    return Result(case, BIT, detail, output)


def _unmutated_problem(
    cmd: list[str], *, cwd: Path, env: dict[str, str], timeout: int, test: str
) -> str | None:
    """Run the UNMUTATED test once in ``cwd``: None when it passes, else why not
    (a timeout or a run that could not start count as not passing)."""
    try:
        proc = _pytest(cmd, cwd=cwd, env=_child_env(env), timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"the unmutated run timed out after {timeout}s"
    except OSError as exc:
        return f"the unmutated run could not start: {exc}"
    return baseline_problem(test, proc.returncode, proc.stdout)


def _reset(root: Path, target: _Target) -> str | None:
    """Put the pristine file back and VERIFY it; None, or why it failed."""
    try:
        _put(root, target.path, target.pristine, target.mode)
        st = os.lstat(target.path)
        if stat.S_ISREG(st.st_mode) and target.path.read_bytes() == target.pristine:
            return None
        return f"{target.path} did not verify against its pristine content"
    except OSError as exc:
        return f"could not reset {target.path}: {exc}"


def _baseline_key(case: Case, selectors: dict[int, str] | None) -> tuple:
    """What makes two cases' baselines the same run."""
    test = (selectors or {}).get(id(case), case.test)
    return (test, case.python, case.pytest_args, tuple(sorted(case.env.items())))


def assert_green_baseline(
    cases: list[Case],
    *,
    cwd: Path,
    python: str,
    env: dict[str, str] | None,
    timeout: int,
    selectors: dict[int, str] | None = None,
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
    seen = {_baseline_key(c, selectors) for c in cases}
    for test, py, extra, case_env in seen:
        try:
            proc = _pytest(
                _pytest_cmd(py or python, test, extra),
                cwd=cwd,
                env=_child_env({**(env or {}), **dict(case_env)}),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # Every other outcome in this module is enumerated; a raw traceback
            # here was the one path that escaped the contract.
            return (
                f"baseline run of {test} timed out after {timeout}s -- it "
                "cannot be established as green, so no mutation is trustworthy"
            )
        except OSError as exc:
            return f"baseline run of {test} could not start: {exc}"
        problem = baseline_problem(test, proc.returncode, proc.stdout)
        if problem:
            return problem
    return None


def _copy_env(
    env: dict[str, str] | None, root: Path, holder: Path, pythonpath: tuple[str, ...]
) -> dict[str, str]:
    """The sweep env for children running in ONE copy: its import roots on
    PYTHONPATH, and a TMPDIR (with the bytecode-cache prefix under it) that
    belongs to this copy alone and is removed with it. A shared TMPDIR let one
    case's leftovers change the next case's verdict."""
    tmp = holder / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    out = {
        "TMPDIR": str(tmp),
        "PYTHONPYCACHEPREFIX": str(tmp / f"mutation-sweep-nopyc-{uuid.uuid4().hex}"),
        **(env or {}),
    }
    if pythonpath:
        out = {"PYTHONPATH": os.pathsep.join(str(root / p) for p in pythonpath), **out}
    return out


def sweep(
    cases: list[Case],
    *,
    repo: Path,
    python: str = sys.executable,
    env: dict[str, str] | None = None,
    pythonpath: tuple[str, ...] = (),
    timeout: int = 7200,
    available: set[str] | None = None,
    check_baseline: bool = True,
    tmp_root: Path | None = None,
) -> Sweep:
    """Run every baseline and every case in its OWN isolated copy of ``repo``.

    ``cases`` name files in ``repo`` (the caller's tree); each is mutated at the
    same relative place in its copy. ``pythonpath`` lists repo-relative import
    roots, put on the children's PYTHONPATH as copy paths. ``tmp_root`` is where
    copies are made (default ``~/tmp``). The caller's tree is never written.

    One copy for all cases let a file one test wrote change a later case's
    verdict. Now nothing is shared between cases except what lives outside a
    copy (HOME, git's refs and config), and the unmutated runs either side of
    the mutated one in `run_case` turn most of that into an ABORT. The first copy fixes the targets' pristine bytes and a
    fingerprint; a later copy with a different fingerprint means the caller's
    tree changed mid-sweep, and its case ABORTS rather than judging a different
    tree.
    """
    if not cases:
        raise ValueError("a sweep with no cases proves nothing")
    repo = Path(repo).resolve()
    rels = {c.path: _repo_relative(repo, Path(c.path)) for c in cases}
    for root in pythonpath:
        # `root / p` with an absolute `p` would REPLACE the copy's path.
        if os.path.isabs(root) or os.path.normpath(root).split(os.sep)[0] == "..":
            raise ValueError(
                f"pythonpath entry {root!r} must be repository-relative: "
                "it is put on the children's PYTHONPATH inside the copy"
            )
    _refuse_back_pointers(repo, env, cases)
    selectors = {id(c): _copy_selector(repo, c) for c in cases}

    first: dict = {}
    with isolated_copy(repo, tmp_root, first) as root:
        # Read ONCE, before any child runs: every case starts from these bytes.
        targets = {p: _load_target(root, rel) for p, rel in rels.items()}
    snapshot = first["fingerprint"]

    if check_baseline:
        # A case gated on state this run does not have CANNOT be baselined:
        # its test fails without that state, the gate reads that as a RED
        # baseline, and one unavailable resource would kill every OTHER case.
        # `run_case` still aborts it, loudly. A refused target is reported per
        # case below rather than through its baseline.
        groups: dict[tuple, Case] = {}
        for c in cases:
            if all(r in (available or set()) for r in c.requires) and isinstance(
                targets[c.path], _Target
            ):
                groups.setdefault(_baseline_key(c, selectors), c)
        for c in groups.values():
            info: dict = {}
            with isolated_copy(repo, tmp_root, info) as root:
                if info["fingerprint"] != snapshot:
                    raise RuntimeError(
                        "the caller's tree changed while the sweep ran its "
                        "baselines; no verdict would be about one tree"
                    )
                problem = assert_green_baseline(
                    [c],
                    cwd=root,
                    python=python,
                    env=_copy_env(env, root, info["holder"], pythonpath),
                    timeout=timeout,
                    selectors=selectors,
                )
            if problem:
                raise RuntimeError(problem)

    out = Sweep()
    for case in cases:
        target = targets[case.path]
        if isinstance(target, str):
            out.results.append(Result(case, ABORTED, target))
            continue
        info = {}
        with contextlib.ExitStack() as stack:
            # A copy that cannot be made (a file vanishing mid-overlay, a full
            # disk) costs this case, not every result the sweep already has.
            try:
                root = stack.enter_context(isolated_copy(repo, tmp_root, info))
            except (RuntimeError, OSError) as exc:
                out.results.append(Result(case, ABORTED, f"could not make this case's copy: {exc}"))
                continue
            if info["fingerprint"] != snapshot:
                out.results.append(
                    Result(
                        case,
                        ABORTED,
                        "the caller's tree changed during the sweep; this copy is not the "
                        "tree the other cases were judged against",
                    )
                )
                continue
            out.results.append(
                run_case(
                    case,
                    _Target(root / rels[case.path], target.pristine, target.mode),
                    cwd=root,
                    python=python,
                    env=_copy_env(env, root, info["holder"], pythonpath),
                    timeout=timeout,
                    available=available,
                    selector=selectors[id(case)],
                )
            )
    return out


def render(result: Sweep) -> str:
    lines = []
    for r in result.results:
        mark = {BIT: "BIT     ", SURVIVED: "SURVIVED", ABORTED: "ABORTED "}[r.outcome]
        lines.append(f"  {mark}  {r.case.label}")
        if r.detail:
            lines.append(f"            {r.detail}")
        if r.outcome == SURVIVED:
            lines.append(f"            expected to break: {r.case.why}")
    lines.append("")
    lines.append(
        f"  {len(result.bit)} bit, {len(result.survived)} SURVIVED, {len(result.aborted)} ABORTED"
    )
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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("manifest", type=Path, help="JSON file with a `cases` list")
    ap.add_argument("--repo", type=Path, default=Path.cwd())
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument(
        "--available",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="declare a shared resource as present (repeatable). A case whose "
        "`requires` names something not declared here ABORTS -- loudly, "
        "because a skipped case and a killed one look identical in a tally.",
    )
    ap.add_argument(
        "--timeout",
        type=int,
        default=7200,
        metavar="SECONDS",
        help="per test run (default 7200, the repo's timeout floor). A run "
        "that exceeds it ABORTS, which fails the gate.",
    )
    ap.add_argument(
        "--tmp-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="where the sweep makes its private copy of the repository "
        "(default ~/tmp). A full checkout: put it on a real disk, never a "
        "RAM-backed or quota-capped temp directory.",
    )
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Resolved ONCE, before anything is derived from it. A relative --repo once
    # reached the child as a relative PYTHONPATH, resolved against the child's
    # own cwd: <repo>/<repo>/src.
    repo = args.repo.resolve()

    doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = cases_from_json(doc, repo)
    # The child env is an ALLOWLIST (see _child_env), so anything the tests need
    # must be DECLARED. A manifest-level `env` covers the sweep; a case's own
    # `env` overrides it. `src` is the package root, named repo-relative so it
    # lands on the COPY's PYTHONPATH.
    available = set(_str_list(doc.get("available", []), "manifest `available`")) | set(
        args.available
    )
    result = sweep(
        cases,
        repo=repo,
        python=args.python,
        env=_str_map(doc.get("env", {}), "manifest `env`"),
        pythonpath=("src",),
        available=available,
        timeout=args.timeout,
        tmp_root=args.tmp_root,
    )
    print(render(result))
    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
