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
the sweep never writes the caller's WORKING TREE. A child can: a test body, or an
option that names an output path, runs as the same user. (A `git worktree` also
shares the caller's git metadata -- config, hooks, refs -- so a test that runs
git commands in its cwd can change those.)

THE FILES. This one drives a sweep. `mutation_case.py` holds what a sweep judges,
`mutation_copy.py` the disposable copy, `mutation_stop.py` how a sweep stops.
To import this as a library, put this directory on `sys.path` first: the siblings
are imported by bare name.

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
  6. The mutated run OPENED the mutated file (`_watch_opens`), or its verdict is
     an ABORT with `never_opened` set. A run that loaded the file from somewhere
     else, through a path in pytest config, a conftest or the env, or that never
     loaded it at all, says nothing about the mutation. OPENED IS NOT EXERCISED:
     a module-level import counts, so a test that never calls the target and
     fails for its own reason still reads BIT, and only guarantee 5 stands
     against that shape. A target a test runs as a subprocess or through another
     interpreter is seen like any other, which a check inside the child could
     not do. A MIXED run, which opens the copy's target and also loads the
     caller's code, is not caught: it reads as its verdict, and SURVIVED at worst,
     which exits 1.
  7. pytest's temp directory stays in the copy. The harness passes its own
     `--basetemp` last, because pytest empties that directory when a test uses
     `tmp_path`, and the last one wins however a case's own is spelled. Options
     that name one output file (`--junitxml`) can still overwrite that file.
  8. A sweep that is stopped keeps what it learned. Each result is printed as its
     case finishes. A signal unwinds the sweep, which removes the copy and kills
     the child's group, and it waits for the steps a raise would damage (`_hold`):
     git's part in making a copy, removing one, starting a child. A stop ends
     non-zero, and a case that has reached its result still reports it.
     SIGKILL cannot be handled, and a test's descendant in its own session escapes
     the group kill. In CI the job must `exec` the sweep, or the signal stops at
     the step's shell.

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
import ctypes
import errno
import json
import os
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Run as a script, the sweep imports these siblings from the caller's tree, and
# it does not write that tree: their bytecode stays out of `__pycache__` too.
# This stops writes, not reads, so a `.pyc` a test run left there is still used
# when it looks current. A fresh checkout has none.
if __name__ == "__main__":
    sys.dont_write_bytecode = True

from mutation_case import (
    ABORTED,
    BIT,
    SURVIVED,
    Case,
    Result,
    Sweep,
    _decode,
    _render_result,
    _render_tally,
    _str_list,
    _str_map,
    _validate,
    apply_edits,
    baseline_problem,
    cases_from_json,
    mutation_verdict,
)
from mutation_case import Edit as Edit  # re-exported: a `Case` needs one
from mutation_copy import (
    _contained,
    _put,
    isolated_copy,
)
from mutation_stop import (
    _catch_stop_signals,
    _hold,
    _raise_if_stopped,
)


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
    }
    env.update(extra or {})
    # Fresh for EVERY child, under the TMPDIR that child actually gets (a
    # copy's own, when the sweep passes one). One prefix shared by a case's
    # runs let a cache the unmutated run wrote -- `py_compile` writes despite
    # DONTWRITEBYTECODE -- be imported by the mutated run.
    if "PYTHONPYCACHEPREFIX" not in (extra or {}):
        env["PYTHONPYCACHEPREFIX"] = os.path.join(
            env["TMPDIR"], f"mutation-sweep-nopyc-{uuid.uuid4().hex}"
        )
    return env


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


def _refuse_owned_env(env: dict[str, str] | None, cases: list[Case]) -> None:
    """Raise ValueError when a manifest sets what each copy owns. A shared TMPDIR
    let one case's leftovers change the next case's verdict. The fresh bytecode
    prefix is what makes an honest import open its source, so the watch depends
    on it: with a precompiled unchecked `.pyc` and no prefix, an import opens
    nothing."""
    for scope, mapping in [("sweep", env or {})] + [(f"case {c.label!r}", c.env) for c in cases]:
        for key in ("TMPDIR", "PYTHONPYCACHEPREFIX"):
            if key in mapping:
                raise ValueError(
                    f"{scope} env sets {key}, which each case's copy sets for itself; leave it out"
                )


def _copy_selector(repo: Path, case: Case) -> str:
    """The case's pytest selector, with its FILE part made repository-relative,
    so it names the copy's file when the child runs in the copy.

    An absolute file inside the repository is rewritten, so the case tests the
    copy; one outside it is refused (ValueError). Anything else that leads away
    from the copy is the watch's job (guarantee 6): no list of spellings could
    be complete, because a tracked `pytest.ini` names nothing a check could see.
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
    return rewritten


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

    Output goes to unlinked files, not pipes, so the run ends when its PROCESS
    does. Under `-s` or `--capture=sys` a test's subprocess inherits the
    stream, and a pipe would stay open until that subprocess exits: a finished
    run then waited out the timeout. One in its own session escapes the group
    kill, so killing first and then draining a pipe would still hang on it.
    """
    tmp = env.get("TMPDIR")
    where = tmp if tmp and os.path.isdir(tmp) else None  # a test may have removed it
    with tempfile.TemporaryFile(dir=where) as out, tempfile.TemporaryFile(dir=where) as err:
        proc = None
        try:
            # Held: a raise between the child starting and this name being bound
            # would leave nothing that could kill it.
            with _hold():
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd), env=env, stdout=out, stderr=err, start_new_session=True
                )
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc.pid)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=10)
            raise
        finally:
            if proc is not None:
                _kill_group(proc.pid)
        return subprocess.CompletedProcess(
            cmd,
            proc.returncode,
            _whole(out).decode("utf-8", errors="replace"),
            _whole(err).decode("utf-8", errors="replace"),
        )


def _whole(fh) -> bytes:
    """What ``fh`` holds now, read by position. Its offset is shared with any
    descendant still holding the file (one in its own session escapes the group
    kill), so a `seek(0)` here would make that writer overwrite the summary."""
    fd, got, at = fh.fileno(), [], 0
    size = os.fstat(fd).st_size
    while at < size and (chunk := os.pread(fd, min(1 << 20, size - at), at)):
        got.append(chunk)
        at += len(chunk)
    return b"".join(got)


def _kill_group(pgid: int) -> None:
    """SIGKILL every process left in the child's group (it leads its own
    session, so its pid is the group id). A group already gone is fine."""
    if pgid <= 1:  # never signal init or every process this user owns
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


def _pytest_cmd(
    python: str, test: str, extra: tuple[str, ...], env: dict[str, str] | None = None
) -> list[str]:
    """The child's command. Its `--basetemp` comes LAST, so it wins over any a
    case passes, however spelled: pytest empties that directory when a test uses
    `tmp_path`, and a case aiming it at the caller's tree would delete the tree.
    After a case's `--`, this reads as a missing file and the run stops before
    any test. A sweep always passes a TMPDIR (`_copy_env`), which each copy owns.
    """
    cmd = [python, "-m", "pytest", test, "-q", "--no-header", "-p", "no:cacheprovider", *extra]
    if tmp := (env or {}).get("TMPDIR"):
        cmd.append(f"--basetemp={os.path.join(tmp, 'pytest')}")
    return cmd


_IN_OPEN = 0x20
_IN_Q_OVERFLOW = 0x4000
_INOTIFY_EVENT = struct.Struct("iIII")


class _OpenWatch:
    """Whether a file was opened since the watch started, by anyone: inotify
    carries no pid, so an outside process's open counts too."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._opened = False

    def opened(self) -> bool:
        """Yes or no, never a count: the kernel merges identical queued events.
        An unlink delivers IN_IGNORED, which is not an open. An overflow means
        events were lost, and only an open is being watched for."""
        while not self._opened:
            try:
                data = os.read(self._fd, 65536)
            except BlockingIOError:
                break
            at = 0
            while at < len(data):
                _wd, mask, _cookie, name_len = _INOTIFY_EVENT.unpack_from(data, at)
                at += _INOTIFY_EVENT.size + name_len
                if mask & (_IN_OPEN | _IN_Q_OVERFLOW):
                    self._opened = True
        return self._opened

    def close(self) -> None:
        os.close(self._fd)


def _watch_opens(path: Path) -> _OpenWatch:
    """Start watching ``path``, or raise OSError. Set it AFTER the file is
    written: `_put` makes a new inode, and a watch follows the inode. Close-on-exec,
    so no child holds it."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        init, add = libc.inotify_init1, libc.inotify_add_watch
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "this platform has no inotify") from exc
    add.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
    # <linux/inotify.h> defines IN_CLOEXEC and IN_NONBLOCK as these two.
    fd = init(os.O_CLOEXEC | os.O_NONBLOCK)
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, f"inotify_init1: {os.strerror(code)}")
    if add(fd, os.fsencode(path), _IN_OPEN) < 0:
        code = ctypes.get_errno()
        os.close(fd)
        raise OSError(code, f"inotify_add_watch {path}: {os.strerror(code)}")
    return _OpenWatch(fd)


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
    cmd = _pytest_cmd(case.python or python, test, case.pytest_args, {**(env or {}), **case.env})
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
        watch = _watch_opens(target.path)
    except OSError as exc:
        return Result(
            case,
            ABORTED,
            f"could not watch whether the mutated run opens its target, so no verdict: {exc}",
        )
    with contextlib.closing(watch):
        try:
            proc = _pytest(
                cmd, cwd=cwd, env=_child_env({**(env or {}), **case.env}), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return Result(case, ABORTED, f"test timed out after {timeout}s")
        except OSError as exc:  # e.g. a `python` the case names that does not exist
            return Result(case, ABORTED, f"could not start the test run: {exc}")
        opened = watch.opened()
    # (3) the exit code AND the summary decide, together, in `mutation_verdict`
    # -- the one place a mutated run is classified. Only stdout is parsed:
    # pytest writes its summary there, and stderr belongs to whatever else ran.
    outcome, detail = mutation_verdict(proc.returncode, proc.stdout)
    output = proc.stdout + proc.stderr
    # Only a verdict needs overriding: a run that could not decide (a usage
    # error, a lock refusal) also opens nothing, and keeps its own reason.
    if outcome in (BIT, SURVIVED) and not opened:
        return Result(
            case,
            ABORTED,
            f"the mutated run never opened {target.path.name}, so its result (it would have "
            f"read {outcome}) says nothing about the mutation: the test does not load this "
            "file, or it loaded a copy from outside this sweep's, through a path in pytest "
            "config, a conftest or the env",
            output,
            never_opened=True,
        )
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
                _pytest_cmd(py or python, test, extra, {**(env or {}), **dict(case_env)}),
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
    PYTHONPATH, and a TMPDIR that belongs to this copy alone and is removed
    with it. A shared TMPDIR let one case's leftovers change the next case's
    verdict. The bytecode-cache prefix is NOT set here: `_child_env` makes a
    fresh one under this TMPDIR for every run."""
    tmp = holder / "tmp"
    tmp.mkdir(mode=0o700, exist_ok=True)
    out = {"TMPDIR": str(tmp), **(env or {})}
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
    on_start: Callable[[Case], None] | None = None,
    on_result: Callable[[Result], None] | None = None,
) -> Sweep:
    """Run every baseline and every case in its OWN isolated copy of ``repo``.

    ``cases`` name files in ``repo`` (the caller's tree); each is mutated at the
    same relative place in its copy. ``pythonpath`` lists repo-relative import
    roots, put on the children's PYTHONPATH as copy paths. ``tmp_root`` is where
    copies are made (default ``~/tmp``). The sweep never writes the caller's tree.

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
        if os.path.isabs(root):
            raise ValueError(
                f"pythonpath entry {root!r} must be repository-relative: "
                "it is put on the children's PYTHONPATH inside the copy"
            )
    _refuse_owned_env(env, cases)
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
            if on_start:
                on_start(c)
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

    def one(case: Case, target: _Target) -> Result:
        info: dict = {}
        with isolated_copy(repo, tmp_root, info) as root:
            if info["fingerprint"] != snapshot:
                return Result(
                    case,
                    ABORTED,
                    "the caller's tree changed during the sweep; this copy is not the "
                    "tree the other cases were judged against",
                )
            return run_case(
                case,
                _Target(root / rels[case.path], target.pristine, target.mode),
                cwd=root,
                python=python,
                env=_copy_env(env, root, info["holder"], pythonpath),
                timeout=timeout,
                available=available,
                selector=selectors[id(case)],
            )

    out = Sweep()
    for case in cases:
        _raise_if_stopped()
        if on_start:
            on_start(case)
        target = targets[case.path]
        if isinstance(target, str):
            result = Result(case, ABORTED, target)
        else:
            # The ONE error boundary per case: anything escaping here would end
            # the sweep with its remaining cases unjudged and no tally. Deliberately
            # broad: a list of types missed MemoryError and RecursionError. An abort
            # is never a pass, and the traceback is kept. A signal's SystemExit is
            # not an Exception, so it passes through and still cleans up.
            try:
                result = one(case, target)
            except Exception as exc:
                traceback.print_exc()
                result = Result(
                    case, ABORTED, f"this case could not finish: {type(exc).__name__}: {exc}"
                )
        out.results.append(result)
        if on_result:
            on_result(result)
    _raise_if_stopped()
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
    # A label the stream cannot encode must not lose the report at the last step.
    sys.stdout.reconfigure(errors="backslashreplace")
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
    previous = _catch_stop_signals()
    try:
        result = sweep(
            cases,
            repo=repo,
            python=args.python,
            env=_str_map(doc.get("env", {}), "manifest `env`"),
            pythonpath=("src",),
            available=available,
            timeout=args.timeout,
            tmp_root=args.tmp_root,
            # stderr, so the report on stdout is the same as it ever was.
            on_start=lambda c: print(f"running: {c.label}", file=sys.stderr, flush=True),
            on_result=lambda r: print(_render_result(r), flush=True),
        )
    finally:
        for each, handler in previous.items():
            signal.signal(each, handler)
    _raise_if_stopped()  # a raise swallowed in a `__del__` must not reach a clean tally
    print(_render_tally(result))
    return 0 if result.clean else 1


if __name__ == "__main__":
    sys.exit(main())
