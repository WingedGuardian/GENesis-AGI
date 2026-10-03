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
     for. It is an ABORT, never a survival. The line is read by ONE grammar
     (`parse_pytest_summary`), by exact outcome keyword, and a baseline counts
     as green only when the target PASSED -- never xfailed, xpassed, skipped
     or deselected.
  5. The file is restored, and the restore is VERIFIED against the baseline.
     The mutation write and the restore share one `try/finally`, because the
     expected outcome of a good case is a NONZERO exit and a trailing restore is
     exactly the statement that does not run -- and a write that fails part-way
     is the other statement that must not escape it.
  6. The restore only overwrites what THIS mutation wrote. If the file changed
     underneath, the sweep PRESERVES it, reports a CONFLICT, and keeps the
     snapshots, rather than destroying an edit a final hash check would happily
     confirm it had made.

WHAT "THE FILE" MEANS. Guarantees 1, 5 and 6 all compare the target against
something, and each comparison is only as good as its notion of identity. A file
is not its path string plus its content hash: it has a TYPE (a child can replace
it with a directory or a link), a MODE (a peer can chmod it), a LINK COUNT (a
second hard link shares the bytes the mutation writes). One `_Fingerprint`
carries all of them, and every check uses it. Writes go through a staged file
and `os.replace`, so a failed write never leaves a truncated source and a
restore never writes INTO whatever now sits at the path.
Snapshots are keyed by position, not by a flattened path, so two targets cannot
share one and a deep path cannot overflow a filename.

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
import hashlib
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


@dataclass(frozen=True)
class _Fingerprint:
    """What "the same file" means for every check in this module.

    Content alone was the old identity, and each field below is a measured way it
    was blind: ``kind`` (a child replaced the target with a directory, and the
    restore copied the snapshot INTO it), ``mode`` (a peer's chmod during the run
    was silently reverted), ``nlink`` (a second hard link shares the bytes the
    mutation writes).

    Deliberately ABSENT: timestamps (the harness itself moves them, and the child
    runs with bytecode caching isolated, so nothing here is decided by an mtime)
    and the inode. An inode check would add only "replaced by a different file
    with the SAME bytes and mode" -- and its realistic trigger is a fixture that
    atomically rewrites the target with the mutated text, which it would turn
    into a CONFLICT that leaves the mutation in the tree.
    """

    kind: str
    sha: str | None
    mode: int
    nlink: int


def _fingerprint(path: Path) -> _Fingerprint | None:
    """The target's identity, or None when nothing is at the path.

    Uses ``lstat``: a symlink is reported AS a symlink, never as its referent.
    Any OSError other than absence propagates -- a file that cannot be inspected
    cannot be proven to be ours, and the caller must treat it as such.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(st.st_mode):
        kind, sha = "file", _sha(path)
    elif stat.S_ISLNK(st.st_mode):
        kind, sha = "symlink", None
    elif stat.S_ISDIR(st.st_mode):
        kind, sha = "directory", None
    else:
        kind, sha = "other", None
    return _Fingerprint(kind, sha, stat.S_IMODE(st.st_mode), st.st_nlink)


def _is_baseline(fp: _Fingerprint | None, base_sha: str, base_mode: int) -> bool:
    """A single-link regular file with the baseline's bytes AND mode."""
    return (fp is not None and fp.kind == "file" and fp.nlink == 1
            and fp.sha == base_sha and fp.mode == base_mode)


def _replace_atomically(target: Path, *, data: bytes | None = None,
                        source: Path | None = None, mode: int) -> _Fingerprint:
    """Put new content at ``target`` all at once, and return what was put there.

    Staged in the target's own directory, then ``os.replace``d: a write that
    fails part-way (disk full, a file-size limit) fails on the STAGING file and
    the target is untouched, and a reader never sees a half-written source. The
    replace also never writes INTO a directory that took the target's place --
    it raises instead. ``source`` copies with ``copy2`` so a restore carries the
    snapshot's timestamps as well as its bytes; ``mode`` is applied explicitly
    because the staging file is created 0600.

    The fingerprint is taken from the staged file BEFORE the replace -- a rename
    moves the same inode, so it is what lands at ``target`` -- rather than read
    back afterwards, which would race the child (or a peer) for no gain.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                    prefix=f".{target.name}.",
                                    suffix=".mutation-sweep")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            if data is not None:
                fh.write(data)
        if source is not None:
            shutil.copy2(source, tmp)
        os.chmod(tmp, mode)
        staged = _fingerprint(tmp)
        if staged is None:
            raise FileNotFoundError(f"staging file {tmp} vanished before the replace")
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    return staged


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


def _reject_unsafe_target(path: Path) -> str | None:
    """A target whose restore cannot be made exact is REFUSED, not handled.

    SYMLINK -- MEASURED: the mutation writes THROUGH the link into the referent;
    if the child then deletes the link, the restore recreates `target` as a
    regular file holding the baseline text while THE REFERENT STAYS MUTATED --
    permanently, with the case still reporting BIT. The correct semantics
    (restore the link? the referent? one outside the repo?) are genuinely
    ambiguous, and a mutation harness has no business guessing about them.

    HARD LINK -- the same shape through an inode instead of a name: a second
    link shares the bytes the mutation writes, and a child deleting THIS name
    leaves the other one mutated. Refused for the same reason.

    Anything that is not a regular file has no restore at all.
    """
    try:
        fp = _fingerprint(path)
    except OSError as exc:
        return f"{path} cannot be inspected: {exc}"
    if fp is None:
        return f"{path} does not exist"
    if fp.kind == "symlink":
        return (f"{path} is a SYMLINK; mutating it writes through to the "
                "referent and the restore cannot put the link back. Point the "
                "case at the real file.")
    if fp.kind != "file":
        return f"{path} is a {fp.kind}, not a regular file"
    if fp.nlink > 1:
        return (f"{path} is HARD-LINKED ({fp.nlink} links); the mutation would "
                "change every link and a restore through this name cannot put "
                "the others back. Point the case at a file with one link.")
    return None


def run_case(
    case: Case,
    baseline: Path,
    *,
    cwd: Path,
    python: str,
    env: dict[str, str] | None = None,
    timeout: int = 7200,
    available: set[str] | None = None,
) -> Result:
    target = case.path

    # A case gated on shared live state ABORTS when that state is absent. It does
    # NOT skip: a skipped case and a killed one look identical in a summary line,
    # and "11/11 bit" means nothing if two of them never ran.
    unsafe = _reject_unsafe_target(target)
    if unsafe:
        return Result(case, ABORTED, unsafe)

    missing = [r for r in case.requires if r not in (available or set())]
    if missing:
        return Result(case, ABORTED,
                      f"requires {', '.join(missing)}, which this run did not "
                      "declare available -- not skipped, because a skipped case "
                      "and a killed one are indistinguishable in a tally")

    base_bytes = baseline.read_bytes()
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    base_mode = stat.S_IMODE(baseline.stat().st_mode)

    def drifted() -> bool:
        try:
            return not _is_baseline(_fingerprint(target), base_sha, base_mode)
        except OSError:
            return True

    # (1) the file must still match the ONE baseline for the whole sweep -- by
    # bytes AND mode, so a chmod after the snapshot is drift too.
    if drifted():
        return Result(case, ABORTED,
                      "target differs from the sweep baseline -- a previous case "
                      "failed to restore, or someone else is editing this file")

    try:
        text, encoding = _decode(case, base_bytes)
    except ValueError as exc:
        return Result(case, ABORTED, str(exc))
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
    try:
        data = mutated.encode(encoding)
    except UnicodeEncodeError as exc:
        return Result(case, ABORTED,
                      f"the mutation cannot be written in the target's own "
                      f"encoding ({encoding}): {exc}")

    # TOCTOU, NARROWED BUT NOT CLOSED -- stated plainly because the difference
    # matters and a re-check cannot do better.
    #
    # The original drift check ran before `compile()` and, for a `bash`
    # validator, before an out-of-process `bash -n`: a window measured in
    # subprocess time. Re-checking here shrinks it to the gap between this read
    # and the replace below. It does NOT eliminate it: a peer writing in that gap
    # is clobbered, the restore then sees its own fingerprint, and no CONFLICT is
    # reported. (The replace being atomic does not change that; it changes what
    # a FAILED write leaves behind, not who can race a successful one.)
    #
    # A re-check can only ever narrow a TOCTOU. Closing it needs one of:
    #   * mutating an ISOLATED COPY so the shared file is never written -- the
    #     only option that actually closes it, and a rewrite of this module's
    #     core (the test must then run against the copy);
    #   * an exclusive lock across check/write/test/restore -- which serialises
    #     other SWEEPS but not an arbitrary editor, and the arbitrary editor is
    #     the threat. `flock` is advisory; a peer CC session does not take it.
    # Accepted for now, and named here rather than left for the next reviewer to
    # rediscover. Raised by CodeRabbit on PR #1851, tagged "heavy lift" by it too.
    if drifted():
        return Result(case, CONFLICT,
                      "the target changed between the drift check and the "
                      "mutation write -- a peer's edit was left untouched")

    wrote: _Fingerprint | None = None
    verdict: Result | None = None
    conflict: str | None = None
    try:
        # The write is INSIDE the scope that restores. It used to sit just
        # above it, so a write that failed after truncating the source escaped
        # with the source damaged and nothing putting it back.
        try:
            wrote = _replace_atomically(target, data=data, mode=base_mode)
        except OSError as exc:
            verdict = Result(case, ABORTED, f"could not write the mutation: {exc}")
        if verdict is None:
            verdict = _run_target(case, cwd=cwd, python=python, env=env,
                                  timeout=timeout)
    finally:
        # (6) restore what this mutation wrote -- and NEVER lose the file.
        conflict = _restore(target, baseline, wrote, base_sha=base_sha,
                            base_mode=base_mode)

    # AFTER the finally, so a conflict detected during restore is visible.
    if verdict is None:  # pragma: no cover -- every branch above assigns one
        verdict = Result(case, ABORTED, "no verdict produced")
    if conflict:
        return Result(case, CONFLICT, f"{conflict} (baseline copy: {baseline})",
                      verdict.stdout)
    return verdict


def _run_target(case: Case, *, cwd: Path, python: str,
                env: dict[str, str] | None, timeout: int) -> Result:
    """Run the case's test against the mutated file and classify the outcome."""
    cmd = [case.python or python, "-m", "pytest", case.test, "-q", "--no-header",
           "-p", "no:cacheprovider", *case.pytest_args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True,
            env=_child_env({**(env or {}), **case.env}), timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return Result(case, ABORTED, f"test timed out after {timeout}s")
    # The exit code AND the summary decide, together, in `mutation_verdict` --
    # the one place a mutated run is classified. Only stdout is parsed: pytest
    # writes its summary there, and stderr belongs to whatever else ran.
    outcome, detail = mutation_verdict(proc.returncode, proc.stdout)
    return Result(case, outcome, detail, proc.stdout + proc.stderr)


def _restore(target: Path, baseline: Path, wrote: _Fingerprint | None, *,
             base_sha: str, base_mode: int) -> str | None:
    """Undo the mutation if -- and only if -- the target is still what it wrote.

    Returns None when the target is back at its baseline (verified, not
    assumed), or a CONFLICT reason when it was left alone. Never raises for a
    target-side problem: an exception here used to escape into `sweep`, whose
    cleanup then deleted the snapshot directory -- the file's only remaining
    copy (MEASURED with a child that unlinked the target).

      * nothing at the path      -> the child deleted it; put it back.
      * exactly what we wrote    -> ours; put it back.
      * we never wrote, and it is
        still the baseline       -> nothing to undo (the write failed cleanly).
      * anything else            -> a peer's edit, a chmod, a directory or link
                                    in its place, or something we cannot read.
                                    PRESERVED, and reported, because a verdict
                                    computed against a file that changed
                                    mid-flight is not trustworthy.
    """
    try:
        current = _fingerprint(target)
    except OSError as exc:
        return f"the target could not be inspected for restore ({exc}); left as is"
    if current is not None and not (wrote is not None and current == wrote):
        if wrote is None and _is_baseline(current, base_sha, base_mode):
            return None
        return ("the target changed while the test ran -- a peer's edit, or "
                f"something replacing the file (now a {current.kind}), was "
                "PRESERVED, so this case's verdict is not trustworthy")
    try:
        # `copy2` via the staging file rather than `write_bytes`: a target the
        # child DELETED would otherwise come back at the default creation mode
        # (MEASURED 0o755 -> 0o644) -- a restored hook script left unrunnable.
        _replace_atomically(target, source=baseline, mode=base_mode)
        restored = _fingerprint(target)
    except OSError as exc:
        return f"RESTORE FAILED ({exc}); the baseline copy is the only good one"
    if not _is_baseline(restored, base_sha, base_mode):
        return "the restore did not verify against the baseline"
    return None


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
        problem = baseline_problem(test, proc.returncode, proc.stdout)
        if problem:
            return problem
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
    timeout: int = 7200,
    available: set[str] | None = None,
    check_baseline: bool = True,
) -> Sweep:
    """Run every case, restoring between each. Never leaves a file mutated."""
    if not cases:
        raise ValueError("a sweep with no cases proves nothing")

    # SNAPSHOT FIRST, BEFORE ANY CHILD PROCESS RUNS. The baseline check used to
    # come first, and a baseline test (or one of its fixtures) that edits or
    # deletes a target then had no recovery copy anywhere: a deleted target made
    # the later copy2 raise with nothing to restore from, and an edited one
    # became the snapshot and survived the sweep. A failing baseline left the
    # same damage. The window existed for every case, on every run, before a
    # single mutation was written.
    #
    # `~/tmp` is a Genesis-container convention, not a property of hosts, and
    # mkdtemp against an absent parent raises.
    tmp_root = Path.home() / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="mutation-sweep-", dir=str(tmp_root)))
    baselines: dict[Path, Path] = {}
    completed = False

    try:
        # Keyed by POSITION, never by a flattened path: `a/b.py` and `a__b.py`
        # used to map to one snapshot name (the later copy overwrote the
        # earlier, and a valid case aborted against another file's bytes), and a
        # deep absolute path overflowed the single-component name limit. One
        # directory per target keeps its real basename for whoever recovers it.
        for i, c in enumerate(sorted({c.path for c in cases}, key=str)):
            slot = tmpdir / f"{i:03d}"
            slot.mkdir()
            snap = slot / c.name
            shutil.copy2(c, snap)
            baselines[c] = snap
        _run_baseline_and_verify(
            cases, baselines, cwd=cwd, python=python, env=env, timeout=timeout,
            available=available, check_baseline=check_baseline,
        )
    except BaseException:
        print(f"mutation-sweep: baselines PRESERVED for recovery: {tmpdir}",
              file=sys.stderr)
        raise

    try:
        out = Sweep()
        for case in cases:
            out.results.append(
                run_case(case, baselines[case.path], cwd=cwd, python=python,
                         env=env, timeout=timeout, available=available)
            )
        # A CONFLICT means the target was deliberately NOT restored -- a peer's
        # edit (possibly made on top of the mutated text), or something that
        # replaced the file. The snapshot is then the only clean copy, so a
        # sweep that "completed" with a conflict keeps it like a crash would.
        completed = not any(r.outcome == CONFLICT for r in out.results)
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


def _run_baseline_and_verify(
    cases, baselines, *, cwd, python, env, timeout, available, check_baseline
) -> None:
    """Green-baseline check, then confirm no target was damaged by running it.

    The verification half is not paranoia: a baseline test -- or one of its
    fixtures -- can edit or delete the very file a case is about to mutate. With
    the snapshot now taken first, that damage is recoverable; without the CHECK
    it would still go unnoticed, and the sweep would mutate a file that no longer
    matches what it was baselined against.
    """
    problem: str | None = None
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
    # NOTE the deliberate ordering: the baseline verdict is CAPTURED, not raised
    # yet. A damaged target EXPLAINS a red baseline -- if a fixture deleted the
    # file under test, "baseline is RED" is a true statement and a useless
    # diagnosis, and it hides the one fact the operator needs (their file is gone,
    # and here is the copy). The specific cause reports first.

    # Whether or not the baseline ran, confirm every target still matches its
    # snapshot before a single mutation is written.
    for target, snap in baselines.items():
        if not target.exists():
            raise RuntimeError(
                f"{target} was DELETED before any mutation -- by a baseline test "
                f"or its fixture. A recovery copy exists at {snap}."
            )
        if target.read_bytes() != snap.read_bytes():
            raise RuntimeError(
                f"{target} was MODIFIED before any mutation -- by a baseline test "
                f"or its fixture. Mutating it now would be mutating a file that "
                f"no longer matches what it was baselined against. Snapshot: {snap}"
            )

    if problem:
        raise RuntimeError(problem)


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


def _contained_path(repo: Path, rel: str) -> Path:
    """``repo / rel``, refused when it would point outside the repository.

    The DIRECTORY part is resolved (so a symlinked directory cannot carry the
    target out of the tree); the final component is not, because a symlinked
    target must still reach `run_case` AS a symlink to be refused there.
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
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Resolved ONCE, before anything is derived from it. The child runs with
    # cwd=<repo>, so a relative --repo baked into PYTHONPATH was resolved by the
    # child against its own cwd: <repo>/<repo>/src.
    repo = args.repo.resolve()

    doc = json.loads(args.manifest.read_text(encoding="utf-8"))
    cases = cases_from_json(doc, repo)
    # The child env is an ALLOWLIST (see _child_env), so anything the tests need
    # must be DECLARED. A manifest-level `env` covers the sweep; a case's own
    # `env` overrides it. VERIFIED 2026-09-07 that the shipped cases pass under
    # the minimal set, but leaving this undeclarable would be a trap for the
    # first case that needs a marker variable.
    env = {"PYTHONPATH": str(repo / "src"), **doc.get("env", {})}
    available = set(doc.get("available", ())) | set(args.available)
    result = sweep(cases, cwd=repo, python=args.python, env=env,
                   available=available, timeout=args.timeout)
    print(render(result))
    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
