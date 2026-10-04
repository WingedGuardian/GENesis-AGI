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
a sweep runs in ONE private copy (`isolated_copy`) holding what is on disk, and
the caller's tree is never written.

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
  4. Each case starts from the PRISTINE target, read once when the copy is
     made. Whatever a test did to the file -- deleted it, replaced it with a
     directory, a symlink or a hard link -- the next write removes it and
     creates a fresh file, so no write follows a link out of the copy. A reset
     that cannot be verified aborts every later case: their verdicts would come
     from a copy that is no longer the one under test.

WHAT IT DELIBERATELY DOES NOT DO. It does not generate mutations. Picking the
mutation is the thinking part -- a behaviourally-null edit (swapped operands
that commute, a type annotation Python does not enforce) produces a GREEN that
means nothing, and no generator knows which is which. It also does not replace
`mutmut`/`cosmic-ray`-style coverage sweeps; this is targeted verify-RED, where
each case names the property it is supposed to break.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
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
        kind, _, reason = self.validator.partition(":")
        kind = kind.strip()
        if kind not in VALIDATORS:
            raise ValueError(f"case {self.label!r}: unknown validator {kind!r}")
        # The reason is the point, so an empty one ("none:", "none:   ") is the
        # same omission as no colon at all.
        if kind == "none" and not reason.strip():
            raise ValueError(
                f"case {self.label!r}: `none` must say WHY there is no validator"
            )
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
            object.__setattr__(
                self, "edits", (Edit(self.anchor, self.replacement),) + self.edits
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
#: The grammar is pytest's own, READ from `_pytest/terminal.py` (pytest 9.0.3):
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
        return (f"baseline run of {test} produced no readable pytest summary "
                f"line (exit {returncode}) -- it cannot be established as green")
    shown = _render_counts(counts)
    if counts.get("failed") or counts.get("error"):
        return (f"baseline is RED: {test} already fails before any mutation "
                f"({shown}), so every 'BIT' from it would be meaningless")
    other = {k: n for k, n in counts.items()
             if k != "passed" and k not in _INCIDENTAL}
    if other:
        return (f"baseline of {test} did not PASS its target ({shown}): "
                f"{_render_counts(other)} is not a green baseline. A strict "
                "xfail that a mutation makes pass is reported as `failed`, and a "
                "skipped target never runs, so either would score a BIT or a "
                "SURVIVED that means nothing")
    if not counts.get("passed"):
        return (f"baseline of {test} ran nothing ({shown}, exit {returncode}) "
                "-- a target that did not run cannot be established as green")
    if returncode != 0:
        return (f"baseline of {test}: exit {returncode} disagrees with the "
                f"summary ({shown}) -- neither alone establishes a green run")
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
        return ABORTED, (f"pytest exit {returncode}: the run did not happen "
                         "(collection error, usage error, or nothing collected)")
    counts = parse_pytest_summary(stdout)
    if counts is None:
        return ABORTED, ("no readable pytest summary line -- the run did not "
                         "happen (lock, guard, collection error?)")
    shown = _render_counts(counts)
    if counts.get("error"):
        return ABORTED, (f"{shown}: an error is not a result -- a setup, "
                         "teardown or collection failure, and the test body may "
                         "never have run")
    other = {k: n for k, n in counts.items()
             if k not in ("passed", "failed") and k not in _INCIDENTAL}
    if other:
        return ABORTED, (f"{shown}: {_render_counts(other)} is not a verdict on "
                         "the target -- its baseline had none, so the mutation "
                         "changed which tests ran")
    failed, passed = counts.get("failed", 0), counts.get("passed", 0)
    if not failed and not passed:
        return ABORTED, f"{shown}: nothing passed or failed -- the run did not happen"
    if returncode == 1 and failed:
        return BIT, ""
    if returncode == 0 and not failed:
        return SURVIVED, ""
    return ABORTED, (f"exit {returncode} disagrees with the summary ({shown}) -- "
                     "neither alone is a verdict")


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
        enc = (tokenize.detect_encoding(io.BytesIO(raw).readline)[0]
               if kind == "python" else "utf-8")
        text = raw.decode(enc)
    except (SyntaxError, LookupError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot decode the target: {exc}") from exc
    if text.encode(enc) != raw:
        raise ValueError(f"the target does not round-trip through {enc}; "
                         "rewriting it would change bytes the case never named")
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
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          env=env, timeout=7200)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} in {repo} failed: "
                           f"{os.fsdecode(proc.stderr).strip()}")
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


def _overlay(top: Path, tree: Path) -> None:
    """Make the fresh checkout at ``tree`` match ``top``'s WORKING TREE.

    The paths: `git diff HEAD` (staged and unstaged, renames split), untracked
    files that are not ignored, and every entry flagged assume-unchanged or
    skip-worktree, whose changes `git diff` does not report. Each is copied as
    it is on disk (a symlink as a symlink) or removed from the copy. Ignored
    files and untracked nested repositories (a trailing slash) are not part of
    this repository and are left out.
    """
    paths = _git_paths(top, "diff", "--name-only", "--no-renames", "-z", "HEAD")
    paths |= _git_paths(top, "ls-files", "--others", "--exclude-standard", "-z")
    for entry in _git(top, "ls-files", "-v", "-z").split(b"\0"):
        if entry[:1].islower() or entry[:1] == b"S":
            paths.add(os.fsdecode(entry[2:]))
    for rel in sorted(p for p in paths if not p.endswith("/")):
        src, dst = top / rel, tree / rel
        if not _contained(tree, dst):
            raise RuntimeError(f"{rel} would be copied outside the copy")
        if os.path.islink(src):
            _clear(dst)
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            _put(tree, dst, src.read_bytes(), stat.S_IMODE(src.stat().st_mode))
        else:
            # Deleted -- or now a DIRECTORY, whose files are entries of their
            # own (sorted after it); HEAD's file must not stay in their way.
            _clear(dst)


def _force_rmtree(path: Path) -> None:
    """rmtree that first makes a directory a test left read-only writable."""
    def retry(func, p, _exc):
        with contextlib.suppress(OSError):
            os.chmod(os.path.dirname(p), 0o700)
            func(p)
    shutil.rmtree(path, onexc=retry)


@contextlib.contextmanager
def isolated_copy(repo: Path, tmp_root: Path | None = None) -> Iterator[Path]:
    """Yield ``repo``'s place inside a private copy of its whole repository.

    The copy is a detached `git worktree` of HEAD under a fresh directory in
    ``tmp_root`` (default ``~/tmp``: a real disk, never the session's TMPDIR,
    because a checkout is large temp), overlaid with the caller's working-tree
    changes (`_overlay`). On ANY exit the copy and its registration are removed
    -- this copy's only: `git worktree remove` on the path this call created,
    then, if git no longer recognises it (a test deleted the directory), the
    administrative entry recorded at creation.
    """
    try:
        top = Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").strip()))
    except RuntimeError as exc:
        raise RuntimeError(f"the sweep copies the tree with git, and {repo} is "
                           f"not inside a git work tree ({exc})") from exc
    top, repo = top.resolve(), repo.resolve()
    parent = Path(tmp_root) if tmp_root is not None else Path.home() / "tmp"
    if parent.resolve().is_relative_to(top):
        raise ValueError(f"tmp_root {parent} is inside the repository {top}; the "
                         "copy would be written into the tree it must not touch")
    parent.mkdir(parents=True, exist_ok=True)
    holder = Path(tempfile.mkdtemp(prefix="mutation-sweep-", dir=str(parent))).resolve()
    tree = holder / holder.name   # the basename names git's admin entry too
    admin: Path | None = None
    try:
        _git(top, "worktree", "add", "--detach", "--quiet", str(tree), "HEAD")
        admin = Path(os.fsdecode(_git(tree, "rev-parse", "--absolute-git-dir").strip()))
        _overlay(top, tree)
        yield tree / repo.relative_to(top)
    finally:
        with contextlib.suppress(RuntimeError):
            _git(top, "worktree", "remove", "--force", str(tree))
        if os.path.lexists(holder):
            _force_rmtree(holder)
        if admin is not None and admin.parent.name == "worktrees" and admin.exists():
            shutil.rmtree(admin, ignore_errors=True)
        if os.path.lexists(holder) or (admin is not None and admin.exists()):
            print(f"mutation-sweep: could not fully remove the copy at {holder}",
                  file=sys.stderr)


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
        return (f"{rel} is a SYMLINK; mutating it writes through to the "
                "referent, possibly outside the copy. Point the case at the real "
                "file.")
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


def _refuse_shared_tree_env(repo: Path, env: dict[str, str] | None,
                            cases: list[Case]) -> None:
    """An env value naming the caller's tree points the child BACK at it.

    The child would then import the unmutated file and report SURVIVED for a
    mutation its test would catch -- the isolation defeated by configuration.
    Import roots are named repo-relative instead (`pythonpath`).
    """
    scopes = [("sweep", env or {})] + [(f"case {c.label!r}", c.env) for c in cases]
    for scope, mapping in scopes:
        for key, value in mapping.items():
            if str(repo) in value:
                raise ValueError(
                    f"{scope} env {key}={value!r} points into the tree under test "
                    f"({repo}). The sweep runs in an isolated copy, so a child "
                    "pointed there tests the UNMUTATED file; name import roots "
                    "repo-relative with `pythonpath` instead")


# --------------------------------------------------------------------------
# RUNNING.
# --------------------------------------------------------------------------

def _pytest(cmd: list[str], *, cwd: Path, env: dict[str, str],
            timeout: int) -> subprocess.CompletedProcess:
    """The ONE place a pytest child is started, baseline and mutation alike."""
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                          env=env, timeout=timeout)


def _pytest_cmd(python: str, test: str, extra: tuple[str, ...]) -> list[str]:
    return [python, "-m", "pytest", test, "-q", "--no-header",
            "-p", "no:cacheprovider", *extra]


def run_case(
    case: Case,
    target: _Target,
    *,
    cwd: Path,
    python: str,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
    available: set[str] | None = None,
) -> Result:
    """Mutate ``target`` (a file in the copy, from its pristine bytes) and run
    the case's test from ``cwd``. Leaves the target mutated: the sweep resets
    it, because only the sweep knows whether a later case needs the copy."""
    # A case gated on shared live state ABORTS when that state is absent. It does
    # NOT skip: a skipped case and a killed one look identical in a summary line,
    # and "11/11 bit" means nothing if two of them never ran.
    missing = [r for r in case.requires if r not in (available or set())]
    if missing:
        return Result(case, ABORTED,
                      f"requires {', '.join(missing)}, which this run did not "
                      "declare available -- not skipped, because a skipped case "
                      "and a killed one are indistinguishable in a tally")
    try:
        text, encoding = _decode(case, target.pristine)
    except ValueError as exc:
        return Result(case, ABORTED, str(exc))
    mutated = text
    for i, edit in enumerate(case.edits):
        # (1) EVERY edit is counted. With two or more, a whole-file "did it
        # change" test stays true while a later anchor silently misses, so a
        # partial mutation reads as complete.
        hits = mutated.count(edit.anchor)
        if hits != 1:
            return Result(case, ABORTED,
                          f"edit {i + 1}/{len(case.edits)}: anchor matched "
                          f"{hits}x, expected exactly 1")
        mutated = mutated.replace(edit.anchor, edit.replacement, 1)

    # (2) the declared validator, never a silent skip.
    err = _validate(case, mutated)
    if err:
        return Result(case, ABORTED, err)
    try:
        data = mutated.encode(encoding)
    except UnicodeEncodeError as exc:
        return Result(case, ABORTED,
                      f"the mutation cannot be written in the target's own "
                      f"encoding ({encoding}): {exc}")
    try:
        _put(cwd, target.path, data, target.mode)
    except OSError as exc:
        return Result(case, ABORTED, f"could not write the mutation: {exc}")
    try:
        proc = _pytest(_pytest_cmd(case.python or python, case.test, case.pytest_args),
                       cwd=cwd, env=_child_env({**(env or {}), **case.env}),
                       timeout=timeout)
    except subprocess.TimeoutExpired:
        return Result(case, ABORTED, f"test timed out after {timeout}s")
    # (3) the exit code AND the summary decide, together, in `mutation_verdict`
    # -- the one place a mutated run is classified. Only stdout is parsed:
    # pytest writes its summary there, and stderr belongs to whatever else ran.
    outcome, detail = mutation_verdict(proc.returncode, proc.stdout)
    return Result(case, outcome, detail, proc.stdout + proc.stderr)


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
            proc = _pytest(_pytest_cmd(py or python, test, extra), cwd=cwd,
                           env=_child_env({**(env or {}), **dict(case_env)}),
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            # Every other outcome in this module is enumerated; a raw traceback
            # here was the one path that escaped the contract.
            return (f"baseline run of {test} timed out after {timeout}s -- it "
                    "cannot be established as green, so no mutation is trustworthy")
        problem = baseline_problem(test, proc.returncode, proc.stdout)
        if problem:
            return problem
    return None


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
    """Run every case in ONE isolated copy of ``repo``'s repository.

    ``cases`` name files in ``repo`` (the caller's tree); each is mutated at the
    same relative place in the copy. ``pythonpath`` lists repo-relative import
    roots, put on the children's PYTHONPATH as copy paths. ``tmp_root`` is where
    the copy is made (default ``~/tmp``). The caller's tree is never written.
    """
    if not cases:
        raise ValueError("a sweep with no cases proves nothing")
    repo = Path(repo).resolve()
    rels = {c.path: _repo_relative(repo, Path(c.path)) for c in cases}
    _refuse_shared_tree_env(repo, env, cases)

    with isolated_copy(repo, tmp_root) as root:
        child_env = dict(env or {})
        if pythonpath:
            child_env = {"PYTHONPATH": os.pathsep.join(str(root / p) for p in pythonpath),
                         **child_env}
        # Read ONCE, before any child runs: whatever the baseline or a case does
        # to a target afterwards, every case starts from this.
        targets = {p: _load_target(root, rel) for p, rel in rels.items()}

        if check_baseline:
            # A case gated on state this run does not have CANNOT be baselined:
            # its test fails without that state, the gate reads that as a RED
            # baseline, and one unavailable resource would kill every OTHER case.
            # `run_case` still aborts it, loudly. A refused target is reported
            # per case below rather than through its baseline.
            baselineable = [
                c for c in cases
                if all(r in (available or set()) for r in c.requires)
                and isinstance(targets[c.path], _Target)
            ]
            if baselineable:
                problem = assert_green_baseline(
                    baselineable, cwd=root, python=python, env=child_env,
                    timeout=timeout)
                if problem:
                    raise RuntimeError(problem)

        out = Sweep()
        dirty: str | None = None
        for case in cases:
            target = targets[case.path]
            if isinstance(target, str):
                out.results.append(Result(case, ABORTED, target))
            elif dirty:
                out.results.append(Result(
                    case, ABORTED, f"the copy is no longer pristine ({dirty}); "
                    "a verdict from it would not be about the tree under test"))
            else:
                out.results.append(run_case(
                    case, target, cwd=root, python=python, env=child_env,
                    timeout=timeout, available=available))
                dirty = _reset(root, target)
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
    lines.append(f"  {len(result.bit)} bit, {len(result.survived)} SURVIVED, "
                 f"{len(result.aborted)} ABORTED")
    if result.survived:
        lines.append("  A survivor means the test does not pin the property its "
                     "case names -- or the mutation was behaviourally null.")
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
    if p.is_absolute() or ".." in p.parts or not (
        (repo / p).parent.resolve().is_relative_to(root)
    ):
        raise ValueError(
            f"case path {rel!r} points outside the repository {root} -- an "
            "absolute path or a `..` component is refused even when it would "
            "land inside, because a manifest path is repository-relative"
        )
    return repo / p


def cases_from_json(doc: dict, repo: Path) -> list[Case]:
    out = []
    for c in doc["cases"]:
        out.append(Case(
            label=c["label"], path=_contained_path(repo, c["path"]), test=c["test"],
            why=c["why"], validator=c["validator"],
            anchor=c.get("anchor"), replacement=c.get("replacement"),
            edits=tuple(Edit(e["anchor"], e["replacement"]) for e in c.get("edits", ())),
            python=c.get("python"), pytest_args=tuple(c.get("pytest_args", ())),
            env=c.get("env", {}), requires=tuple(c.get("requires", ())),
        ))
    return out


def build_parser() -> argparse.ArgumentParser:
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
    ap.add_argument(
        "--timeout", type=int, default=7200, metavar="SECONDS",
        help="per test run (default 7200, the repo's timeout floor). A run "
             "that exceeds it ABORTS, which fails the gate.",
    )
    ap.add_argument(
        "--tmp-root", type=Path, default=None, metavar="DIR",
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
    available = set(doc.get("available", ())) | set(args.available)
    result = sweep(cases, repo=repo, python=args.python, env=doc.get("env", {}),
                   pythonpath=("src",), available=available, timeout=args.timeout,
                   tmp_root=args.tmp_root)
    print(render(result))
    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
