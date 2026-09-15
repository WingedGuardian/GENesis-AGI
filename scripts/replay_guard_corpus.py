#!/usr/bin/env python3
"""Replay this install's real shell commands through a Bash guard and report the
rate at which it blocks them.

Deliberately NOT "the benign-block rate", which is what this line used to say.
The corpus is every command anyone typed here, DANGEROUS ONES INCLUDED, so
nothing in it has been classified as benign — which is why every rate this tool
prints is stamped UNCLASSIFIED. This docstring is also the CLI's --help text, so
the old wording was recreating the exact misreading the rest of the file exists
to prevent, on the most-read surface it has.

The dev skill's "Acceptance Bar + Measured Rate" asks a change to a Bash guard's
PREDICATE to carry a measured `blocked k/N` figure. That is a CONVENTION, not an
enforced gate — the PR template does not ask for it, no CI job checks it, and the
review-depth check is advisory by design. An earlier draft of this docstring called
it "Rule 5 of the hook contract"; there is no such rule at any ref, the phrase
appeared nowhere but here, and this repo's guard rules are lettered rather than
numbered. It was invented, and citing a gate that does not exist is worse than
citing none, so it is corrected rather than quietly dropped.

The number cannot come from CI in any case: the corpus is built from this install's
own session transcripts, which hold real commands (paths, hostnames, occasionally
secrets passed in argv), so it is never checked in and never leaves the box. That
also bounds what the figure can mean — see below. Before this existed the figure was
produced by a one-off sweep whose only surviving trace is a comment
(git_push_guard.py, the 11,488-command measurement) — the third such one-off, which
is why it is a script now.

    python3 scripts/replay_guard_corpus.py --list
    python3 scripts/replay_guard_corpus.py --guard protected_paths
    python3 scripts/replay_guard_corpus.py --all
    python3 scripts/replay_guard_corpus.py --rebuild --all

WHAT THE NUMBER DOES AND DOES NOT MEAN
--------------------------------------
It measures ONE side: how often a guard blocks a command drawn from ordinary work.
A rate measured on one side of a tradeoff is half a measurement, and a benign-block
rate of 0 reads IDENTICALLY for a correct guard and for an inert one. So this is
never sufficient on its own — every predicate change also ships a positive control
proving the dangerous form still blocks. This script deliberately refuses to print
a verdict, only a rate, so it cannot be mistaken for one.

It is also a REALISM check, not a coverage check: the corpus contains only shapes
someone actually typed here. A construct nobody has typed has no entry and cannot
show up as a false positive, so a clean sweep says nothing about it.

WHY A GUARD IS REPLAYABLE, AND WHO CHECKED
------------------------------------------
Each guard carries a ReplaySafety record, and the default is refusal. Those
records were prose for a while, and prose about code rots: the declarations have
been wrong four times, including all six line-number citations going stale inside
five days. So each one now also carries evidence a test re-derives —
`--list` prints it:

  * FACTS for a Python guard (reads_env / reads_argv / spawns / writes_fs),
    checked against a transitive walk of every import resolving under
    scripts/hooks/, bidirectionally: an undeclared fact fails, and so does a
    declared one that stopped being true.
  * CITATIONS, as symbol + VERBATIM fragment, re-resolved against the source.
  * A DELEGATION SCAN for the two shell guards, which no Python walk can reach —
    bash_safety has no module at all and hands off through a pipe. It is a
    closed-set cross-reference, never a shell parser, and it proves OCCURRENCE
    rather than invocation: the obligation is to say what each name is doing
    there.

None of this is a proof of purity, and --list says so with the number: a False
fact means "none of the N spellings this checker knows", and the N are printed.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

_HOOKS = Path(__file__).resolve().parent / "hooks"
sys.path.insert(0, str(_HOOKS))

_REPO = Path(__file__).resolve().parent.parent
# Outside the repo, per the output-files rule, and because it holds real commands.
_CACHE = Path.home() / ".genesis" / "output" / "guard-corpus.jsonl"
_TRANSCRIPTS = Path.home() / ".claude" / "projects"

_GUARD_TIMEOUT_S = 15

# The bare names the replayable guards import from _HOOKS. Enumerated rather than
# discovered: this list is what `_load_guard_from_this_checkout` refuses on, so a
# name missing here is a silent hole, and a name that is wrong here is a loud
# refusal. Kept in step with the guards' own import lines.
_GUARD_BARE_DEPS = ("hook_input", "shell_parse")


# ── corpus ───────────────────────────────────────────────────────────────────


def _extract_commands() -> list[tuple[str, str]]:
    """Every Bash `input.command` in this install's transcripts, deduped.

    Streams line by line: the transcript tree is multi-gigabyte and grows without
    bound, so it is never read whole. Sizes are stated relatively on purpose —
    MEASURED on this install 2026-09-10 the tree was 6.0 GB across 11,624 files
    and yielded 140,293 unique pairs; the same figures read ~1.4 GB / 51,052 five
    days earlier, so any absolute number written here is stale on arrival. What
    the design depends on is the SHAPE (unbounded growth, a host that may be
    swapless), not the magnitude.
    """
    seen: set[tuple[str, str]] = set()
    files = sorted(_TRANSCRIPTS.rglob("*.jsonl"))
    for n, path in enumerate(files, 1):
        if n % 200 == 0:
            print(f"  … {n}/{len(files)} files, {len(seen)} unique commands", file=sys.stderr)
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                if '"Bash"' not in line:  # cheap pre-filter; correctness is below
                    continue
                try:
                    rec = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict):
                    # A JSON line containing "Bash" but shaped as, say, ['Bash'].
                    # Every other malformed record here is skipped; this one
                    # raised AttributeError and killed a multi-gigabyte walk outright.
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                    continue
                for block in msg["content"]:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") != "Bash":
                        continue
                    inp = block.get("input")
                    if not isinstance(inp, dict):
                        continue  # a schema change could make this a str or list
                    cmd = inp.get("command")
                    if isinstance(cmd, str) and cmd.strip():
                        # The cwd the command was actually typed in. Guards that
                        # ask "am I in a worktree?" answer differently here than
                        # at the repo root, so replaying without it measures a
                        # situation that never happened.
                        cwd = rec.get("cwd")
                        seen.add((cmd, cwd if isinstance(cwd, str) else ""))
    return sorted(seen)


def load_corpus(*, rebuild: bool = False) -> list[tuple[str, str]]:
    """The corpus as (command, cwd) pairs.

    A v1 cache held bare command strings with no cwd. Rather than silently
    replaying those from the repo root — the very defect this format change
    fixes — a legacy cache is detected and rebuilt, so a stale file cannot
    masquerade as a valid measurement.
    """
    if _CACHE.exists() and not rebuild:
        _harden(_CACHE)
        # Whether the cache was READ at all, kept separate from what it held. The
        # unreadable branches used to signal themselves by stuffing `[None]` into
        # `rows`, which then fell into the v1-format check below — so a cache
        # truncated by an interrupted rebuild printed its true cause AND a second
        # line asserting a false one ("cache predates the cwd field"), sending a
        # reader after a format migration that does not exist. Recovery was right
        # in every case; only the stated cause was wrong, in a file whose whole
        # argument is that a false diagnostic is worse than none.
        readable = True
        rows: list = []
        try:
            with _CACHE.open() as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except ValueError as exc:
            # A cache truncated by an interrupted rebuild. Say what and where,
            # and rebuild — the previous behaviour was a bare JSONDecodeError
            # from inside a comprehension, naming neither.
            print(
                f"corpus cache is corrupt ({exc}) — rebuilding {_CACHE}",
                file=sys.stderr,
            )
            readable = False
        except OSError as exc:
            # The cache existed at the `exists()` check above and does not now.
            # That is not a rare race: the daily genesis-disk-hygiene timer prunes
            # this very file at 45 days, and nothing coordinates the two — so a
            # rebuild is the CORRECT answer, and it is already the answer every
            # other unusable-cache branch gives. A lock spanning load and prune
            # would be the wrong size for a file whose whole property is that it
            # is regenerable.
            print(
                f"corpus cache became unreadable ({exc}) — rebuilding {_CACHE}",
                file=sys.stderr,
            )
            readable = False
        if not readable:
            pass  # the branch above already named the real cause
        elif any(r is None or isinstance(r, str) for r in rows):
            print(
                "cache predates the cwd field (v1) — rebuilding, because "
                "replaying it would measure the wrong directory",
                file=sys.stderr,
            )
        elif not all(
            isinstance(r, list) and len(r) == 2 and all(isinstance(x, str) for x in r) for r in rows
        ):
            # A row that is valid JSON but the wrong SHAPE. `42` or `["one"]`
            # crashed the loader; worse, a `{"command": …, "cwd": …}` object
            # unpacked to the literal pair ("command", "cwd") and produced a
            # confident measurement of two words nobody typed. Rebuild, which is
            # the same answer the v1 branch above already gives.
            print(
                "cache rows are not (command, cwd) string pairs — rebuilding "
                f"{_CACHE} rather than measuring a shape nobody wrote",
                file=sys.stderr,
            )
        else:
            return [(c, w) for c, w in rows]
    print("building corpus from transcripts (streaming)…", file=sys.stderr)
    cmds = _extract_commands()
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    # Create it 0600 BEFORE writing, not after: the corpus is verbatim command
    # lines from real sessions and demonstrably contains secrets passed in argv
    # (an inline `SSHPASS=…` was found in it), so it must never exist even
    # briefly at the default 0644.
    # Written to a sibling and renamed, so the cache is only ever replaced
    # ATOMICALLY. The build walks the whole transcript tree and takes minutes;
    # interrupting it
    # used to leave a truncated final line, and the next load then died inside a
    # list comprehension with a JSONDecodeError that named neither the cache nor
    # the remedy. The tool stayed dead until someone deleted the file by hand.
    # A UNIQUE temp per rebuild. A fixed `<cache>.tmp` is shared state: two
    # concurrent rebuilds opened the same path, the second O_TRUNC'd the inode
    # the first was still writing, and the first's os.replace then died
    # FileNotFoundError after minutes of work. mkstemp is O_EXCL, so the name
    # cannot collide, and 0600 by construction.
    #
    # dir=_CACHE.parent deliberately, NOT the default temp root: on this box
    # TMPDIR points at Claude Code's working temp, which a watchdog kills
    # sessions over when it fills, and a corpus rebuild is exactly the fill. It
    # also keeps the temp on the same filesystem, which is what makes os.replace
    # atomic rather than a copy.
    fd, tmp_name = tempfile.mkstemp(dir=_CACHE.parent, prefix=_CACHE.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        # mkstemp already creates at 0600. Asserted on the fd anyway: this is the
        # one file whose mode is a security control, so it is measured here
        # rather than inherited from a documented promise.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            for pair in cmds:
                f.write(json.dumps(pair) + "\n")
        os.replace(tmp, _CACHE)
    except BaseException:
        # Leave no orphan behind, but do NOT sweep sibling temps: a delete loop
        # over a directory outside the repo is a worse hazard than one stray file.
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    # Report the mode the file ACTUALLY carries. A hard-coded "(mode 0600)" is
    # how the bug above stayed invisible: the line claimed a mode nothing had
    # verified, on a file that demonstrably holds secrets.
    mode = stat.S_IMODE(_CACHE.stat().st_mode)
    print(
        f"cached {len(cmds)} unique commands -> {_CACHE} (mode {mode:04o})",
        file=sys.stderr,
    )
    return cmds


def _harden(path: Path) -> None:
    """Tighten an existing cache written before the 0600 default."""
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
            print(f"tightened {path} to 0600 (it held real commands)", file=sys.stderr)
    except OSError as exc:
        # NOT silent. Swallowing this means reading and replaying from a
        # world-readable file full of real commands while saying nothing.
        print(f"WARNING: could not tighten {path} ({exc})", file=sys.stderr)


# ── guard invocation ─────────────────────────────────────────────────────────
#
# Two modes, because the guards genuinely have two shapes. Both are the shape the
# existing tests already use, so a disagreement between this harness and the suite
# would be a bug in one of them, not a third opinion.


# Commands whose recorded directory no longer exists are replayed from the repo
# root, which is a SUBSTITUTED cwd — counted here so the report can say so rather
# than quietly folding them into the rate.
_SUBSTITUTED_CWD = {"n": 0}


def _effective_cwd(cwd: str) -> str:
    """The recorded cwd if it still exists, else the repo root (counted)."""
    if cwd and os.path.isdir(cwd):
        return cwd
    _SUBSTITUTED_CWD["n"] += 1
    return str(_REPO)


def _payload(cmd: str, cwd: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": cwd,
    }


def _run_python_guard(module_name: str, cmd: str, cwd: str) -> bool:
    """True if the guard BLOCKS. Imports once, then calls main() in process.

    `main()` returns the intended exit code (0 allow / 2 block) — `run_guard` only
    wraps it to convert a crash into a block, so calling main() directly is the
    same verdict without a process spawn. The guard binds `read_payload` at import
    (`from hook_input import read_payload`), so the patch has to land on the GUARD
    module's attribute, not on hook_input's.

    NO WALL-CLOCK BOUND on this path, unlike `_run_shell_guard`'s
    `timeout=_GUARD_TIMEOUT_S` — an in-process call cannot be interrupted the way
    a subprocess can. That is a property of THIS HARNESS, not of any guard: every
    `subprocess.run` in the guards replayed here carries its own `timeout=`. The
    distinction matters because an earlier version of the git_push refusal stated
    the harness's limitation as if it were a defect in that guard, which is the
    false-evidence failure this table exists to avoid.
    """
    mod = _run_python_guard._loaded.get(module_name)  # type: ignore[attr-defined]
    if mod is None:
        mod = _load_guard_from_this_checkout(module_name)
        _run_python_guard._loaded[module_name] = mod  # type: ignore[attr-defined]
    here = _effective_cwd(cwd)
    payload = _payload(cmd, here)
    # THREE ambient channels reach these guards, and all three are captured here
    # as PURE READS before anything is mutated. Nothing between this point and
    # the `try` may raise: a mutation applied outside the try has no `finally` in
    # scope, so it would leak for the rest of the process. `os.getcwd()` is
    # exactly such a raiser — it fails when the invocation directory has been
    # deleted, which is not exotic in a harness whose subject is worktrees being
    # removed.
    #
    #   1. read_payload — the guard binds it at import
    #      (`from hook_input import read_payload`), so the patch has to land on
    #      the GUARD module's attribute, not on hook_input's.
    #   2. the process cwd — these guards call os.getcwd() DIRECTLY
    #      (worktree_cwd_guard's self-brick check, the routing guard's repo
    #      resolution) rather than reading the payload's cwd field, so the
    #      process has to move too or the threading would be cosmetic.
    #   3. sys.argv — worktree_cwd_guard.main() selects its Enter/ExitWorktree
    #      classifiers on `"--enter-worktree" in sys.argv`, so ANY argv the host
    #      happens to carry reaches it, and `--enter-worktree` makes every row
    #      block. Not hypothetical for an imported caller: replay() is a
    #      supported API, so the host's argv is whatever ITS operator typed.
    #      Pinned to the guard's PRODUCTION argv, which for the Bash path is the
    #      bare script path — .claude/settings.json wires both in-process guards
    #      as `genesis-hook hooks/<name>.py` with no flags, and only the separate
    #      Enter/ExitWorktree wirings pass any.
    #
    # argv is restored by SLICE ASSIGNMENT rather than rebinding, and the
    # snapshot is a copy. Rebinding `sys.argv` is invisible to a guard that
    # captured the list itself — `from sys import argv`, or a module-level
    # `_ARGV = sys.argv` — which would silently keep seeing the HOST's argv and
    # defeat the pin with no signal. No guard in scripts/hooks/ spells it that
    # way today; slice assignment means one written that way tomorrow is still
    # covered.
    original = getattr(mod, "read_payload", None)
    prior = os.getcwd()
    prior_argv = list(sys.argv)
    try:
        mod.read_payload = lambda: payload  # type: ignore[assignment]
        sys.argv[:] = [str(_HOOKS / f"{module_name}.py")]
        os.chdir(here)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mod.main()
        return code == 2
    except SystemExit as exc:  # a guard that exits directly rather than returning
        return exc.code == 2
    # DELIBERATELY no `except Exception`. Catching it here returned True, which
    # is the right VERDICT — run_guard converts a crash to a block for these
    # fail-closed guards — but it consumed the exception, so _probe recorded
    # crashed=False and the run printed a clean, quotable rate. An ImportError
    # crashes every row and reported 100.00% with no warning at all, while
    # _probe's own docstring promised crashes were reported. Letting it
    # propagate reaches _probe, which produces (blocked=True, crashed=True):
    # same numerator, disclosed.
    finally:
        # ORDER IS LOAD-BEARING, and getting it wrong was MEASURED to corrupt a
        # run silently. The restores that CANNOT throw go first; os.chdir CAN
        # throw (the prior directory may have been removed while the guard ran)
        # and so goes last, wrapped. With chdir first and unwrapped, one raising
        # restore abandoned the two below it — leaving the patched lambda
        # installed, so every LATER row was classified against this row's
        # payload while _probe recorded the whole thing as a single crash. A
        # wrong number that discloses one crash is exactly the outcome this file
        # exists to prevent.
        sys.argv[:] = prior_argv
        # Unconditional. If a guard ever lacks the attribute, restoring only on
        # the not-None branch leaves the patched lambda installed on the module
        # for the rest of the process — every later command would then be
        # classified against this command's payload.
        if original is not None:
            mod.read_payload = original
        else:
            with contextlib.suppress(AttributeError):
                delattr(mod, "read_payload")
        with contextlib.suppress(OSError):
            os.chdir(prior)


def _load_guard_from_this_checkout(module_name: str):
    """Load a guard from THIS harness's hooks directory, or refuse.

    `__import__(module_name)` returns whatever is already in `sys.modules` under
    that bare name. A host process that imported `protected_paths_guard` — or one
    of its bare-named dependencies — from ANOTHER checkout first therefore gets
    that module back, and the harness reports a rate for the changed guard while
    having measured the unchanged one. The scenario is not exotic: `replay()` is a
    supported API and worktree-based guard development is the reason to call it,
    so the wrong answer arrives exactly when the answer matters most, silently.

    Two halves, because loading the target by path is not sufficient on its own:
    the guards import their dependencies by BARE NAME (`from hook_input import
    read_payload`), which resolves through `sys.path` and would still pick up a
    foreign entry. So a pre-existing dependency from outside this checkout is a
    REFUSAL rather than something to work around — a measurement of the wrong
    code is the failure this whole file exists to prevent, and it cannot be
    disclosed after the fact because nothing downstream can tell.
    """
    path = _HOOKS / f"{module_name}.py"
    if not path.is_file():
        raise SystemExit(f"guard module not found in this checkout: {path}")

    for dep in _GUARD_BARE_DEPS:
        existing = sys.modules.get(dep)
        if existing is None:
            continue
        dep_file = getattr(existing, "__file__", None)
        if dep_file is None or Path(dep_file).resolve().parent != _HOOKS:
            raise SystemExit(
                f"REFUSED: '{dep}' is already imported from {dep_file!r}, which is "
                f"not this harness's {_HOOKS}. The guards import it by bare name, "
                "so replaying now would measure another checkout's code and report "
                "the number as this one's. Run the harness in a process that has "
                "not already imported the hook modules."
            )

    spec = importlib.util.spec_from_file_location(f"_rgc_{module_name}", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"could not load a module spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    # Registered under a HARNESS-PRIVATE name, never the bare one: claiming the
    # bare name would make this harness the thing that poisons a host process's
    # import cache for everyone else.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    loaded = getattr(mod, "__file__", None)
    if loaded is None or Path(loaded).resolve() != path.resolve():
        raise SystemExit(f"loaded {module_name} from {loaded!r}, expected {path}")
    return mod


_run_python_guard._loaded = {}  # type: ignore[attr-defined]


# ── replay safety, declared per guard ────────────────────────────────────────
#
# This replaced a pair of prose comments that reasoned about WHICH GUARD was in
# the table below. That shape was defeated in review, twice, and the second time
# is the instructive one: `bash_safety` IS in the table and DELEGATES to
# `git_discard_guard`, which was not — so excluding a guard by absence excluded
# nothing, because the side effect arrived through a CALLER. A comment cannot be
# defeated that way once what it governs is permission to run at all.
#
# The default is REFUSAL. A guard added later by someone who did not think about
# side effects is refused until they do, which is the one case a denylist can
# never cover.


class Cite(NamedTuple):
    """One piece of evidence, in a form a test can re-resolve.

    `why` is prose, and prose about code rots silently. Every line number in the
    first revision of this table had drifted within five days — one of them onto
    a comment about an unrelated cap — while the sentences around them still read
    as verified. A Cite is the same evidence stated so a checker can go and look:
    find `symbol` in `module`, and assert `fragment` still occurs inside it.

    `fragment` is VERBATIM. Never elided, never reflowed — an ellipsis makes the
    claim unresolvable, which is the failure mode this record exists to remove.
    (One declaration cited `["git", "-C", cwd, "stash", "create", …]`; the source
    reads `["git", "-C", cwd, "stash", "create", "git-discard-guard snapshot"]`,
    and nothing could have told them apart.)

    `symbol` is None for a file-level fragment — a shell script has no Python
    symbol to scope to — and then the fragment need only occur somewhere in the
    file.

    An EMPTY `fragment` means "this symbol still exists", and nothing more. Some
    evidence is a claim about absence ("_block_with_pids returns 2 on every
    branch"), which no substring can carry; pinning the NAME at least fails when
    the referent is renamed away, and overclaiming it as behaviour would be the
    prose problem again in a machine-readable wrapper.
    """

    module: str  # a module basename under scripts/hooks/, or a repo-relative path
    symbol: str | None
    fragment: str


class PyEvidence(NamedTuple):
    """A guard whose behaviour a Python AST walk can establish.

    The four facts are checked against the TRANSITIVE closure of `module`'s
    imports that resolve under scripts/hooks/ — bounded there because that is the
    same boundary `_GUARD_BARE_DEPS` already names, so the walk terminates and
    never wanders into the stdlib. Transitive matters: `audit_jsonl` sits two hops
    out from git_discard and git_push, writing files and reading argv, and no
    declaration named it.
    """

    module: str
    reads_env: bool
    reads_argv: bool
    spawns: bool
    writes_fs: bool


class ShEvidence(NamedTuple):
    """A guard whose behaviour is established by scanning shell source.

    There is no AST here and DELIBERATELY no shell parser. The two fields are a
    closed-set cross-reference — every tracked repo script whose name occurs in
    the text, and every program from `DELEGATED_PROGRAMS` — not a semantic model
    of the shell. Modelling shell semantics by hand is the unbounded-divergence
    tar pit the guards themselves are forbidden to enter, and a declaration
    checker has no business entering it either.

    This is the half a Python import walk cannot do, and it is not a corner case:
    `bash_safety` has no Python module at all, and its delegation to
    `git_discard_guard` — the side effect that got past prose review twice — is a
    shell pipe. An import-closure walk misses it by construction.

    Note what the fields are named for. The scan proves OCCURRENCE, never
    invocation: it cannot tell `"$_py" "$SCRIPT_DIR/hooks/git_discard_guard.py"`
    from the same name in a comment, and a checker that claimed to would be
    modelling the shell again. So the obligation is to ACKNOWLEDGE each
    occurrence in `why` — as a delegation, or as message text — which is exactly
    the reading a human has to do anyway. MEASURED on the current tree:
    bash_safety references six guards, invokes three, and its declaration named
    one.

    Both fail directions are cheap: a missed occurrence leaves a declaration
    unchecked (the status quo), and a false positive costs one clause in a
    sentence saying why the name is there.
    """

    source: Callable[[], str]  # returns the shell text at check time
    label: str  # what to call it in a failure message
    references_scripts: tuple[str, ...]  # repo script basenames occurring in it
    references_programs: tuple[str, ...]  # DELEGATED_PROGRAMS occurring in it


#: Dotted spellings, matched as an attribute chain rooted at a module name.
_FACT_DOTTED: dict[str, frozenset[str]] = {
    "reads_env": frozenset(
        {"os.environ", "os.environb", "os.getenv", "os.path.expanduser", "os.path.expandvars"}
    ),
    "reads_argv": frozenset({"sys.argv"}),
    "spawns": frozenset(
        {
            "subprocess.run",
            "subprocess.Popen",
            "subprocess.call",
            "subprocess.check_call",
            "subprocess.check_output",
            "os.system",
            "os.popen",
            "os.fork",
            "os.forkpty",
            "os.execv",
            "os.execve",
            "os.execl",
            "os.execlp",
            "os.execvp",
            "os.execvpe",
            "os.spawnv",
            "os.spawnve",
            "os.posix_spawn",
            "os.posix_spawnp",
            "multiprocessing.Process",
            "multiprocessing.Pool",
        }
    ),
    "writes_fs": frozenset(
        {
            "os.replace",
            "os.rename",
            "os.remove",
            "os.unlink",
            "os.mkdir",
            "os.makedirs",
            "os.rmdir",
            "os.truncate",
            "os.chmod",
            "os.symlink",
            "os.link",
            "os.write",
            "os.open",
            "shutil.copy",
            "shutil.copy2",
            "shutil.copyfile",
            "shutil.copytree",
            "shutil.move",
            "shutil.rmtree",
            "shutil.unpack_archive",
            "shutil.make_archive",
            "shutil.copyfileobj",
            "shutil.copymode",
            "shutil.copystat",
            "shutil.chown",
            "tempfile.mkstemp",
            "tempfile.mkdtemp",
            "tempfile.NamedTemporaryFile",
            "tempfile.TemporaryDirectory",
        }
    ),
}

#: Bare method names, matched on ANY receiver because the AST cannot resolve the
#: type. Only names no other common type carries are eligible.
#:
#: `.replace` and `.write` are deliberately ABSENT, and the reason is measured: a
#: first cut of this walk reported `writes_fs=True` for protected_paths, from
#: `prot.replace(home, "~", 1)` in protected_paths_guard._legacy_substring_block
#: — a STRING replace. A matcher that counts it forces a pure argv classifier to
#: declare a filesystem write, which is the declaration lying in the other
#: direction. An over-reporting checker is not the safe kind of wrong here: it
#: trains the reader to disbelieve the facts.
#:
#: ELIGIBILITY IS AN INVARIANT, NOT A LIST OF EXCLUSIONS. Naming `.replace`,
#: `.write` and `.truncate` as forbidden is a three-name denylist: it cannot see
#: `.read`, `.close`, `.seek`, `.flush` or `.pop`, and the next ambiguous name
#: added would pass. The property that actually matters is "pathlib carries this
#: name and the common non-filesystem receivers do not", which
#: `test_a_bare_method_name_is_unambiguous_by_construction` asserts against the
#: STDLIB rather than against a hand-maintained list.
_FACT_METHODS: dict[str, frozenset[str]] = {
    "reads_env": frozenset(),
    "reads_argv": frozenset(),
    "spawns": frozenset(),
    "writes_fs": frozenset(
        {"write_text", "write_bytes", "touch", "mkdir", "unlink", "symlink_to", "hardlink_to"}
    ),
}

#: An `open()` whose mode contains any of these is a write.
_WRITE_MODE_CHARS = "wax+"

#: Names that OPEN a file and therefore need their MODE read, unlike write_text
#: above which is a write whatever its arguments. Matched as the builtin, as
#: `<expr>.open(...)` (the `Path(...).open("w")` idiom this very script uses at
#: `_CACHE.open()`), and as `os.fdopen` / `io.open`.
_OPEN_NAMES = frozenset({"open", "fdopen"})

#: Where the mode sits POSITIONALLY, which is not the same for every spelling and
#: cannot be inferred from the AST node type: `os.fdopen` is an Attribute like
#: `p.open` is, but its mode is the second argument because the first is a file
#: descriptor, while `p.open("w")`'s path is the receiver. Reading position 1 for
#: both scored every `Path(...).open("w")` as a READ.
_OPEN_MODE_AT_1 = frozenset({"open", "os.fdopen", "io.open"})

#: The external programs a shell guard must ACKNOWLEDGE if their name occurs in
#: it. Chosen for SIDE EFFECTS — each mutates state, calls out over the network,
#: or executes something else.
#:
#: NOT exhaustive, and saying so is the point: an earlier revision of this comment
#: called it "a closed set chosen for side effects", which reads as a guarantee it
#: cannot make. There is no closed set of side-effecting programs. What IS closed
#: is this list, and `test_every_published_spelling_is_actually_detected` binds it
#: to the scanner so the list and the behaviour cannot drift.
#:
#: `python3` earns its place even though the delegates it runs are already caught
#: by the script-name half: `bash_safety_hook.sh` invokes ALL THREE of its real
#: delegates through it, so a shell guard that gained an inline `python3 -c` would
#: otherwise be invisible to both halves.
DELEGATED_PROGRAMS: tuple[str, ...] = (
    "gh",
    "git",
    "curl",
    "wget",
    "ssh",
    "sqlite3",
    "rsync",
    "python3",
    "systemctl",
    "rm",
    "mv",
    "chmod",
    "dd",
    "kill",
    "pkill",
)


@functools.cache
def repo_script_names() -> frozenset[str]:
    """Basenames a shell guard could delegate to, from `git ls-files`.

    Lives HERE rather than in the checker so the claim `--list` prints and the set
    the scan uses are one object. They were two, and the published one was wrong
    in three separate ways: a `{.py, .sh}` suffix filter dropped the six
    extension-less git hooks sitting in the guards' OWN directory (`pre-push`,
    `commit-msg`, …), a non-recursive glob dropped everything under `scripts/*/`,
    and reading the filesystem rather than the index made the word "tracked"
    false. MEASURED: 180 names against 236 tracked, missing 56.

    Fail CLOSED. If git cannot answer, the scan must refuse rather than quietly
    fall back to a glob that under-reports — an under-reporting delegation scan
    is indistinguishable from a clean one, which is the whole failure this
    mechanism exists to prevent.
    """
    proc = subprocess.run(  # noqa: S603
        ["git", "-C", str(_REPO), "ls-files", "scripts"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"cannot enumerate tracked scripts (git exited {proc.returncode}): "
            f"{proc.stderr.strip()[:200]}. The delegation scan refuses rather than "
            "falling back to a filesystem glob, which would under-report and read "
            "exactly like a clean result."
        )
    return frozenset(Path(rel).name for rel in proc.stdout.split())


def fact_coverage() -> dict[str, tuple[str, ...]]:
    """What each fact is actually checked against.

    PUBLISHED, and printed by --list, because `spawns=False` means "none of the N
    spellings this checker knows" and NEVER "proven pure". Saying which N is the
    difference between a bounded claim and a false one — the same honesty
    `check_atomic_writes.py` practises with its UNSCANNED_ROOTS.
    """
    out: dict[str, tuple[str, ...]] = {}
    for fact in _FACT_DOTTED:
        spellings = sorted(_FACT_DOTTED[fact]) + sorted(f".{m}()" for m in _FACT_METHODS[fact])
        if fact == "writes_fs":
            spellings += [
                f"{n}(..., mode with any of {_WRITE_MODE_CHARS!r})"
                for n in [*sorted(_OPEN_MODE_AT_1), ".open"]
            ]
        out[fact] = tuple(spellings)
    return out


@dataclass(frozen=True)
class ReplaySafety:
    """Whether replaying a guard once per corpus row is safe, and the evidence.

    Deliberately not "51,052 times", which is what this line used to say. That
    number was this install's corpus when the line was written; it read 140,293
    five days later, and a docstring that has to be re-measured to stay true is a
    claim with a shelf life. The property being declared does not depend on the
    count — it is whether a REPEATED invocation writes, spawns, or calls out.

    Every claim in `why` is EVIDENCE and cites a SYMBOL plus a quoted fragment
    rather than a line number, for the same reason: four other open PRs touch the
    guards cited here, and every line number in an earlier revision of this table
    had already drifted (one of them onto a comment about an unrelated cap) while
    the prose around it still read as verified.
    """

    safe: bool
    why: str
    # Printed WITH the rate rather than instead of it. A caveat has to travel
    # with the number, because the number is what gets pasted into a PR body.
    caveat: str = ""
    # The machine-checkable half. `evidence` says WHAT to check and how; `cites`
    # says which constructs the prose above is standing on. Both are keyword-only
    # and un-defaulted at the constructors, so a guard added later cannot arrive
    # unchecked — the same allowlist polarity that makes _UNDECLARED the default.
    evidence: PyEvidence | ShEvidence | None = None
    cites: tuple[Cite, ...] = ()


def replay_safe(
    why: str,
    *,
    evidence: PyEvidence | ShEvidence,
    cites: tuple[Cite, ...] = (),
    caveat: str = "",
) -> ReplaySafety:
    """Declare a guard replayable. `why` is the EVIDENCE, not an assurance — it
    is printed verbatim by --list, so a claim in it that turns out to be false
    (as "no environment reads" was for protected_paths) is worse than saying
    nothing. `caveat` qualifies the resulting number and prints beside it.

    `evidence` is required and has no default: the whole point of this record was
    that a newcomer who did not think about side effects gets refused rather than
    trusted, and a fact field that defaults to "unknown" would put that hole back
    one level in."""
    return ReplaySafety(safe=True, why=why, caveat=caveat, evidence=evidence, cites=cites)


def not_replay_safe(
    why: str,
    *,
    evidence: PyEvidence | ShEvidence,
    cites: tuple[Cite, ...] = (),
) -> ReplaySafety:
    """Refuse a guard, with the evidence for the refusal. Refused guards stay in
    the table and still appear in --list: absence teaches nothing, and
    exclusion-by-absence is the pattern this replaced.

    A REFUSED guard still declares its facts, and that is not ceremony: the facts
    are what JUSTIFY the refusal, so they are the ones most worth checking. If
    git_push's `spawns` ever went false, the paragraph explaining that it makes
    thousands of live API calls would have quietly stopped being true."""
    return ReplaySafety(safe=False, why=why, evidence=evidence, cites=cites)


_UNDECLARED = ReplaySafety(
    safe=False,
    why=(
        "no replay-safety declaration. Replaying a guard runs it against every "
        "command in this install's real history, so it is refused until someone "
        "states what that does to this machine: writes, subprocesses, network "
        "calls, and anything it DELEGATES to. Add safety=replay_safe(...) or "
        "not_replay_safe(...) to its entry in GUARDS."
    ),
)


@dataclass(frozen=True)
class Guard:
    """One replayable guard.

    `shell` used to live in a separate `_SHELL_GUARDS` frozenset — two globals
    that could disagree about the same guard. One record per guard instead.
    """

    run: Callable[[str, str], bool]  # (command, cwd) -> blocked
    # NOT subprocess's shell=True. This means the guard is invoked by
    # SPAWNING A PROCESS per row rather than calling main() in-process,
    # which is what makes pool fan-out worth its cost. Named `shell` at
    # first; ruff S604 flagged every construction, and it was right that the
    # name reads as a security smell it is not.
    spawns_process: bool = False
    safety: ReplaySafety = _UNDECLARED  # DEFAULT REFUSED
    # Resolved ONCE in the parent, before any worker exists. Two reasons, and the
    # second is why it is a field rather than a call inside replay(): a resource
    # a guard needs must be fetched where a failure is still a clean exit rather
    # than a dead worker, and fork can only hand down what the parent already
    # has. Without it each worker resolved the resource itself, which made the
    # "the parent parses once and workers inherit it" claim below false.
    prepare: Callable[[], object] | None = None
    # The module _run_python_guard loads, or None for a shell guard. It exists so
    # the evidence KIND can be bound to the MECHANISM, and that binding is not
    # cosmetic: requiring `evidence` to be present is NOT allowlist polarity,
    # because an isinstance check is satisfied by whichever type is cheapest to
    # fake. MEASURED — git_discard, refused because `git stash create` writes
    # objects into whatever live repo a row was recorded in, was re-declared
    # `replay_safe` with an ShEvidence over an empty string and passed ALL FOUR
    # checkers: the fact walk iterates PyEvidence only, and the shell scan found
    # nothing in nothing. Four green, zero verification, on the most dangerous
    # guard in the table.
    #
    # Set it through `python_guard()` rather than by hand, so the module name is
    # written once and the runner and the evidence cannot name different things.
    py_module: str | None = None


def python_guard(module: str, *, safety: ReplaySafety, **kw) -> Guard:
    """A guard that loads `module` in-process, with the name written ONCE.

    The runner used to be a lambda closing over the module name while nothing
    else recorded it, so no checker could tell which artifact a declaration was
    about — and adding a `py_module` field by hand would just create a second
    copy of the string to drift from the first. Here there is one.
    """
    return Guard(
        run=lambda c, w: _run_python_guard(module, c, w), py_module=module, safety=safety, **kw
    )


def _run_shell_guard(argv: list[str], cmd: str, cwd: str) -> bool:
    here = _effective_cwd(cwd)
    env = dict(os.environ)
    # The ONE variable measured to change a verdict rather than only a message.
    # bash_safety_hook.sh gates its `gh pr view` calls on
    #   [ "$_in_genesis" -eq 1 ] && [ "${GENESIS_CC_SESSION:-}" != "1" ] -> exit 0
    # so the same corpus yields one rate from an interactive session and another
    # from a dispatched one, which sets it to "1". A measurement tool whose
    # number depends on who ran it is not reporting a property of the guard.
    # Pinned ABSENT, and announced at startup, rather than silently inherited.
    #
    # Deliberately NOT a full allowlist. The obvious model is _child_env() in
    # tests/test_hooks/test_guard_ansic_fail_closed.py, and most of it is wrong
    # here: it pins HOME to a sandbox and sets GIT_CEILING_DIRECTORIES because a
    # TEST must not touch real state. This harness exists to measure THIS
    # install, so a sandbox HOME falsifies the number and a ceiling breaks the
    # recorded-cwd fidelity the corpus format was changed to get. Same reasoning,
    # opposite conclusion — stated so nobody "fixes" one to match the other.
    env.pop("GENESIS_CC_SESSION", None)
    # The shell's OWN startup channel, which is a different thing from a variable
    # the guard reads and is why the no-allowlist decision above does not cover
    # it. A non-interactive `bash -c` SOURCES $BASH_ENV before the command, and
    # this guard runs TWO bash processes per corpus row — the settings.json
    # command is itself a `bash -c '…'` string, which _run_shell_guard then wraps
    # in another (faithful to production, where the harness's outer shell stands
    # in for the one Claude Code uses to run the hook) — and spawns nested shells and command
    # substitutions inside each — so an operator whose environment exports
    # BASH_ENV would have their startup file executed hundreds of thousands of
    # times by a tool that claims to replay local history. SHELLOPTS/BASHOPTS are
    # the same channel by another door: exported, they turn options on in the
    # child (`errexit`, `xtrace`, `onecmd`) and change what the guard DOES, not
    # merely what it prints. Executing an operator's startup file is a side effect
    # of the HARNESS, never a property of this install being measured, so these
    # are pinned absent rather than inherited. ENV is included for the same reason
    # one level out: it is the POSIX-mode spelling, and the argv here is not
    # guaranteed to stay `bash` forever.
    for _startup in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD"):
        env.pop(_startup, None)
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        input=json.dumps(_payload(cmd, here)),
        capture_output=True,
        text=True,
        timeout=_GUARD_TIMEOUT_S,
        cwd=here,
        env=env,
    )
    return proc.returncode == 2


@functools.cache
def _inline_blob() -> str:
    """The inline mega-guard, read from tracked settings.json.

    Located the same way tests/test_hooks/test_inline_settings_guard.py locates it,
    deliberately: if that discovery ever breaks, both break together and loudly,
    rather than this harness silently measuring a different hook.

    LAZY and memoised, not read at import. Eagerly, a reworded hook, a malformed
    settings.json, or a checkout where the blob moved made `import
    replay_guard_corpus` exit — taking down --list, whose entire job is
    EXPLAINING refusals, plus both in-process guards, replay() for library
    callers, and the whole test module. Five of six invocations never need this
    string; only one guard does, and now only that guard pays for it being
    absent. functools.cache keeps the single-parse property the eager read was
    hoisted out of the lambda to get (and with the fork pool, the parent parses
    once and workers inherit it).
    """
    data = json.loads((_REPO / ".claude" / "settings.json").read_text())
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if "git reset --hard" in cmd and "worktree" in cmd and "case " in cmd:
                    return cmd
    raise SystemExit("inline mega-guard not found in .claude/settings.json")


GUARDS: dict[str, Guard] = {
    "protected_paths": python_guard(
        "protected_paths_guard",
        safety=replay_safe(
            "a pure argv/string classifier — no filesystem writes, no subprocess, "
            "no network. It DOES read the environment, which an earlier version "
            "of this line wrongly denied: protected_paths_guard._expand runs "
            "`os.path.expanduser(os.path.expandvars(token))` on each operand, "
            "main() expands again to spot a surviving `$` "
            '(`if "$" in os.path.expandvars(operand):`), and '
            "_legacy_substring_block / _protected_dirs / _protected_files each "
            'resolve `home = os.path.expanduser("~")`. Reads only, so replay is '
            "still safe.",
            caveat=(
                "resolves ~ and $VARS while classifying, so a verdict depends on "
                "HOME and on whatever variables the command references. The "
                "recorded rows carry no environment, so the replay uses this "
                "session's — which is the honest choice for measuring THIS "
                "install, but it means an operand like $SOME_PATH is classified "
                "against today's value rather than the one it had when typed."
            ),
            evidence=PyEvidence(
                module="protected_paths_guard",
                reads_env=True,
                reads_argv=False,
                spawns=False,
                writes_fs=False,
            ),
            cites=(
                Cite(
                    "protected_paths_guard",
                    "_expand",
                    "os.path.expanduser(os.path.expandvars(token))",
                ),
                Cite("protected_paths_guard", "main", 'if "$" in os.path.expandvars(operand):'),
                Cite(
                    "protected_paths_guard",
                    "_legacy_substring_block",
                    'home = os.path.expanduser("~")',
                ),
                Cite("protected_paths_guard", "_protected_dirs", 'home = os.path.expanduser("~")'),
                Cite("protected_paths_guard", "_protected_files", 'home = os.path.expanduser("~")'),
            ),
        ),
    ),
    "worktree_cwd": python_guard(
        "worktree_cwd_guard",
        safety=replay_safe(
            "no writes, no network, no subprocess. It does scan /proc — "
            "worktree_cwd_guard._find_processes_in_dir runs "
            '`entries = os.listdir("/proc")` and '
            '`os.readlink(f"/proc/{pid}/cwd")` to list processes sitting in a '
            "target directory. It also reads sys.argv: main() branches on "
            '`if "--enter-worktree" in sys.argv:` (and the --exit-worktree '
            "sibling). That is a READ, so replay stays safe — but it is ambient "
            "process state, so _run_python_guard pins argv to this guard's "
            "production Bash-mode argv rather than inheriting the host's. Left "
            "inherited, an unrelated caller flag put every row through the "
            "Enter/ExitWorktree classifier instead.",
            caveat=(
                "reads /proc, so the DIAGNOSTIC TEXT varies between runs. The "
                "verdict does not: every branch after that read returns 2 "
                "(_block_with_pids and _block_no_direct_removal are both "
                "unconditional), so the rate is reproducible even though the "
                "message is not."
            ),
            evidence=PyEvidence(
                module="worktree_cwd_guard",
                reads_env=True,
                reads_argv=True,
                spawns=False,
                writes_fs=False,
            ),
            cites=(
                Cite(
                    "worktree_cwd_guard", "_find_processes_in_dir", 'entries = os.listdir("/proc")'
                ),
                Cite(
                    "worktree_cwd_guard",
                    "_find_processes_in_dir",
                    'os.readlink(f"/proc/{pid}/cwd")',
                ),
                Cite("worktree_cwd_guard", "main", 'if "--enter-worktree" in sys.argv:'),
                # Empty fragment: the caveat's claim is that every branch after
                # the /proc read returns 2, which is an absence and not a
                # substring. Pinning the names is what a citation can honestly do.
                Cite("worktree_cwd_guard", "_block_with_pids", ""),
                Cite("worktree_cwd_guard", "_block_no_direct_removal", ""),
            ),
        ),
    ),
    "inline_blob": Guard(
        run=lambda c, w: _run_shell_guard(["bash", "-c", _inline_blob()], c, w),
        spawns_process=True,
        prepare=_inline_blob,
        safety=replay_safe(
            "a stdin->stderr classifier. Its only FILE redirect is >/dev/null; "
            "the rest are `>&2`, the diagnostic channel rather than a write. Its "
            "only nonzero exit is 2; the `stash` and `sqlite3` tokens in it are "
            "message text and a grep pattern, not invocations. RE-MEASURED "
            "2026-09-10 against the blob currently in .claude/settings.json "
            "(which changed since this line was first written): its only "
            "variable references are CMD and IN, its own shell locals — the blob "
            "itself reads no inherited environment variable. That is a claim "
            "about the BLOB, not about bash, and the difference is not academic: "
            "a non-interactive `bash -c` sources $BASH_ENV before running "
            "anything, and exported shell options change what the child DOES. "
            "MEASURED against this blob with the real payload for a "
            "recursive-force reset: clean, SHELLOPTS=onecmd and SHELLOPTS=errexit "
            "all give rc=2 (blocked), while SHELLOPTS=noexec gives rc=0 — the "
            "classifier goes inert and the harness scores the row as ALLOWED. So "
            "_run_shell_guard pins BASH_ENV/ENV/SHELLOPTS/BASHOPTS/BASH_XTRACEFD "
            "absent: without it, replaying this guard executes an operator's "
            "startup file twice per corpus row, and one exported option turns "
            "the whole measurement into a fail-open. It REFERENCES no other repo "
            "script, and the three program names in it — `git`, `sqlite3` and "
            "`systemctl`, the last inside the advice string "
            '`"Use: systemctl --user restart …"` — are '
            "both non-invocations: `git` appears inside the case pattern "
            '`*"git reset --hard"*` and in the advice text that follows it, and '
            '`sqlite3` inside the grep pattern `"sqlite3.*genesis\\.db"`. A '
            "pattern that MATCHES a command is not a command, and neither is a "
            "sentence telling the operator which one to run.",
            evidence=ShEvidence(
                source=_inline_blob,
                label=".claude/settings.json inline blob",
                references_scripts=(),
                references_programs=("git", "sqlite3", "systemctl"),
            ),
        ),
    ),
    "bash_safety": Guard(
        run=lambda c, w: _run_shell_guard(
            ["bash", str(_REPO / "scripts" / "bash_safety_hook.sh")], c, w
        ),
        spawns_process=True,
        safety=not_replay_safe(
            "it DELEGATES to git_discard_guard.py — bash_safety_hook.sh pipes the "
            "raw command into it "
            '(`printf \'%s\' "$RAW" | "$_py" "$SCRIPT_DIR/hooks/'
            'git_discard_guard.py"`) on a git '
            "checkout/restore/reset/switch/clean/rm/mv/read-tree glob, and "
            "git_discard_guard._snapshot_worktree then runs git stash create "
            '(`["git", "-C", cwd, "stash", "create", "git-discard-guard '
            'snapshot"]` — quoted whole, because the elided form this line used '
            "to carry was a claim no checker could resolve) against the LIVE "
            "repository at each row's recorded directory. The objects that writes "
            "are not redirectable by any knob: _snapshot_dir's "
            '`resolve_store_dir("GENESIS_DISCARD_SNAPSHOT_DIR")` relocates the '
            "recovery RECORDS only, never the objects the stash puts in the "
            "target repo. MEASURED in a scratch repo with uncommitted work: 4 "
            "discard-shaped commands produced 4 recovery rows plus loose objects. "
            "A clean tree writes nothing, which is why an early probe found no "
            "problem. Secondary, and latent rather than live: bash_safety_hook.sh "
            "calls `gh pr view` (twice, for the PR number and its mergeable "
            "state), reachable when _in_genesis is 0 or when GENESIS_CC_SESSION "
            'is exactly "1" — the value every dispatched session sets. '
            "It delegates to TWO MORE guards, which no earlier revision of this "
            "line mentioned and the delegation scan added here found: the loop "
            "`for _guard in destructive_command_guard.py protected_paths_guard.py` "
            'pipes the same raw command into each ("$_py" "$SCRIPT_DIR/hooks/'
            '$_guard"). Both are pure argv/string classifiers — walked '
            "transitively, neither spawns nor writes — so they add no side effect "
            "to a replay, and the refusal above still rests entirely on "
            "git_discard_guard. Recorded anyway, because the reason this guard's "
            "declaration was wrong twice is that it reasoned about the script and "
            "not about what the script runs. The remaining three guard names in "
            "the file are not invocations: git_push_guard.py appears only in an "
            "`[ -f ... ]` existence test used to detect a genesis checkout, and "
            "shell_parse.py and worktree_cwd_guard.py only in comments. The "
            "delegates run through `python3` — `_py=$(command -v python3 …)`, "
            "which no earlier revision of this line mentioned either — so a "
            "replay pays a process spawn per matching row on top of the guard's "
            "own work. `rm` and `mv` occur as the SUBCOMMAND names in the "
            "`*git*rm*|*git*mv*` case glob and in comments about what the guard "
            "matches; neither is invoked.",
            evidence=ShEvidence(
                source=lambda: (_REPO / "scripts" / "bash_safety_hook.sh").read_text(),
                label="scripts/bash_safety_hook.sh",
                references_scripts=(
                    "destructive_command_guard.py",
                    "git_discard_guard.py",
                    "git_push_guard.py",
                    "protected_paths_guard.py",
                    "shell_parse.py",
                    "worktree_cwd_guard.py",
                ),
                references_programs=("gh", "git", "python3", "rm", "mv"),
            ),
            cites=(
                Cite(
                    "scripts/bash_safety_hook.sh",
                    None,
                    'printf \'%s\' "$RAW" | "$_py" "$SCRIPT_DIR/hooks/git_discard_guard.py"',
                ),
                Cite(
                    "scripts/bash_safety_hook.sh",
                    None,
                    "for _guard in destructive_command_guard.py protected_paths_guard.py; do",
                ),
                Cite(
                    "git_discard_guard",
                    "_snapshot_worktree",
                    '["git", "-C", cwd, "stash", "create", "git-discard-guard snapshot"]',
                ),
                Cite(
                    "git_discard_guard",
                    "_snapshot_dir",
                    'resolve_store_dir("GENESIS_DISCARD_SNAPSHOT_DIR")',
                ),
            ),
        ),
    ),
    "git_discard": python_guard(
        "git_discard_guard",
        safety=not_replay_safe(
            "PRESENT AND REFUSED rather than absent, because absence teaches "
            "nothing at --list and absence-as-exclusion is the pattern that "
            "already failed here. `git stash create` per candidate command "
            "writes objects into whatever live repository the row was recorded "
            "in, and each snapshot writes a recovery record into the store "
            "git_discard_guard._snapshot_dir resolves. That store is bounded — "
            "one file per hook flush, size-trimmed by disk_hygiene.sh's "
            "prune_hook_audit_logs step, oldest whole files dropped — so one "
            "replay would evict the genuine recovery history it exists to hold. "
            "(An earlier revision of this line described a single JSONL "
            "self-trimming at 1 MB. That was true when written and #1609 replaced "
            "it; the conclusion is unchanged, the mechanism is not.) "
            "It is also the one guard not wrapped by run_guard, so this harness's "
            "crash-counts-as-block rule would misreport it: in production it "
            "fails OPEN.",
            evidence=PyEvidence(
                module="git_discard_guard",
                reads_env=True,
                reads_argv=True,
                spawns=True,
                writes_fs=True,
            ),
            cites=(
                Cite(
                    "git_discard_guard",
                    "_snapshot_worktree",
                    '["git", "-C", cwd, "stash", "create", "git-discard-guard snapshot"]',
                ),
                Cite(
                    "git_discard_guard",
                    "_snapshot_dir",
                    'resolve_store_dir("GENESIS_DISCARD_SNAPSHOT_DIR")',
                ),
                # run_guard is cited by NAME only. The claim is that this guard is
                # not wrapped by it — an absence, which no substring of anything
                # can establish. What the citation pins is that the wrapper still
                # exists under that name, so the sentence keeps a live referent.
                Cite("hook_input", "run_guard", ""),
            ),
        ),
    ),
    "git_push": python_guard(
        "git_push_guard",
        safety=not_replay_safe(
            "read-only on the filesystem, but it shells out to `gh repo view` / "
            "`gh pr view` and to git at classify time, and a large fraction of "
            "the corpus is exactly the shape that reaches those calls. MEASURED "
            "on this install 2026-09-10: 2,371 push-shaped rows and 2,587 "
            "`gh pr create|merge`-shaped ones out of 140,293 (3.5%). That is "
            "thousands of live GitHub API calls against the owner's account, and "
            "their rate limit, from a tool whose docstring says it replays local "
            'history. "Read-only" and "safe to run thousands of times against a '
            'remote API" are different claims, and the count only grows: the '
            "same three figures read 1,195 / 924 / 51,052 five days earlier.",
            evidence=PyEvidence(
                module="git_push_guard",
                reads_env=True,
                reads_argv=True,
                spawns=True,
                writes_fs=True,
            ),
            cites=(
                # `spawns` is the fact this refusal rests on, so it gets the cites
                # that fail if the shelling-out moves. writes_fs arrives
                # transitively through audit_jsonl, the override-record store.
                Cite("git_push_guard", "_current_branch", "subprocess.run"),
                Cite("git_push_guard", "_derive_repo_from_cwd", "subprocess.run"),
            ),
        ),
    ),
}


class Result(NamedTuple):
    """One guard's replay. `valid` is False when anything crashed or timed out —
    the rate is still printed, because seeing it is how you diagnose the cause,
    but the process must not exit 0 on it."""

    blocked: int
    valid: bool


class Outcome(NamedTuple):
    """One command's result. A NamedTuple because it crosses the mp.Pool
    boundary and pickles as a plain tuple, and because four positional bools
    were already one transposition away from a silent mis-count."""

    blocked: bool
    substituted: bool
    crashed: bool
    timed_out: bool
    # The row this outcome is ABOUT, carried rather than re-derived. The pooled
    # path used to recover it as `corpus[i - 1]`, which is correct only because
    # `imap` preserves order — so a future switch to `imap_unordered` for speed
    # would silently attribute every printed sample to the wrong command, and
    # the samples are exactly what a human reads to turn a rate into a verdict.
    # It already crosses the pickle boundary; carrying two more strings costs
    # nothing and removes the ordering dependency entirely.
    row: tuple[str, str]


def _probe(args: tuple[str, str, str]) -> Outcome:
    """One command's outcome, with the reasons kept SEPARATE from the verdict.

    Crashes and timeouts both count as blocks — that is what production does for
    these fail-closed guards — but each is reported on its own channel. Folding
    them into the numerator silently is how a guard that never ran once printed a
    clean, quotable 100%.
    """
    guard, cmd, cwd = args
    row = (cmd, cwd)
    _SUBSTITUTED_CWD["n"] = 0
    try:
        hit = bool(GUARDS[guard].run(cmd, cwd))
        return Outcome(hit, bool(_SUBSTITUTED_CWD["n"]), False, False, row)
    except subprocess.TimeoutExpired:
        # A hung guard is not a pass. It was already counted as a block, but
        # invisibly: crashed stayed False, so nothing in the report distinguished
        # "the guard blocked this" from "the guard never answered". A hang and a
        # crash also have different fixes, which is why they get different verbs.
        return Outcome(True, bool(_SUBSTITUTED_CWD["n"]), False, True, row)
    except KeyboardInterrupt:
        # The one BaseException that must still propagate: swallowing it would
        # make Ctrl-C during a multi-minute sweep do nothing visible.
        raise
    except BaseException:
        # BaseException, not Exception, and the difference is a HANG rather than
        # a mis-count. `SystemExit` is a BaseException, so it escapes an
        # `except Exception` here, escapes multiprocessing.pool.worker's own
        # `except Exception`, and kills the worker WITHOUT delivering a result —
        # the parent then blocks forever in imap's next(). MEASURED: with a
        # settings.json carrying no inline blob, `--guard inline_blob --jobs 4`
        # hung indefinitely (killed at 25s) while `--jobs 1` exited cleanly, so
        # the behaviour also depended on the host's core count.
        #
        # A guard is allowed to exit rather than return — `run_guard` is built
        # around exactly that — so this is a property of the guard contract, not
        # of one guard. Every non-interrupt exit becomes a disclosed crash, which
        # is what the serial path already did.
        return Outcome(True, bool(_SUBSTITUTED_CWD["n"]), True, False, row)


def replay(guard: str, corpus: list[tuple[str, str]], jobs: int) -> Result:
    """Replay the corpus through one guard.

    The two guard shapes have different costs, so they get different strategies.
    Figures are PER ROW, because the corpus size is not stable — MEASURED on this
    install 2026-09-10 over 140,293 rows:

      * in-process Python guard  ~0.14 ms/row  (protected_paths: 20 s total)
      * subprocess shell guard   ~6 ms/row wall at 6 workers (inline_blob:
        2,000 rows in 12 s, so the full corpus projects to ~14 min)

    So shell guards fan out across processes and Python guards stay serial: the
    Python ones are already fast, and they hold module state a pool would have to
    re-import per worker.

    An earlier revision put the shell cost at ~260 ms/row and the serial total at
    ~3.5 h. That conflated the two shell guards: the 260 ms belongs to
    `bash_safety`, which shells out to git per invocation — and `bash_safety` is
    declared NOT replay-safe and never runs. The only shell guard that reaches
    this path is a pure string classifier that spawns bash and nothing else,
    which is ~40x cheaper. Quoting the expensive guard's cost for the cheap one
    made the tool look unusable at full corpus size when it is not.
    """
    # Second layer. The CLI refuses before it gets here, but importing this
    # module and calling replay() directly must not be a way around the
    # declaration — a bypass that needs no flag is still a bypass.
    safety = GUARDS[guard].safety
    if not safety.safe:
        raise RuntimeError(f"{guard} is not replay-safe: {safety.why}")

    # BEFORE the fan-out, and before the first row. A guard that resolves a
    # resource lazily must do it here or every worker repeats the work — and if
    # resolving RAISES, doing it in the parent turns what was an undelivered
    # result and an indefinitely blocked imap into an ordinary exception.
    if GUARDS[guard].prepare is not None:
        GUARDS[guard].prepare()

    blocked: list[tuple[str, str]] = []
    substituted = 0
    crashed = 0
    timed_out = 0
    # BOTH paths go through _probe, so they cannot drift in what they count. They
    # did: the pool swallowed every exception as a block while the serial loop
    # let it propagate, so the same broken guard reported 100% blocked or a
    # traceback depending only on the job count.
    if GUARDS[guard].spawns_process and jobs > 1:
        import multiprocessing as mp

        # fork EXPLICITLY, not the platform default. `_probe` resolves the guard
        # out of the module-global GUARDS table, so a spawn/forkserver worker —
        # which re-imports this module rather than inheriting it — loses any
        # entry a caller registered at runtime, and re-does whatever `prepare`
        # already resolved. fork inherits both copy-on-write instead.
        #
        # (An earlier version of this comment justified the choice with
        # `_INLINE_BLOB`, a module-level constant that no longer exists, and
        # claimed the parent parses settings.json once. Neither was true after
        # the read became lazy: the parent never invoked the guard, so every
        # worker parsed it. `prepare` is what makes the single-parse claim true,
        # rather than deleting the claim.)
        #
        # Python 3.14 makes forkserver the Linux default while `requires-python`
        # here is >=3.12, so leaving it implicit means the tool behaves
        # differently on two supported interpreters. get_context("fork") is the
        # documented way to say a program needs fork; the DeprecationWarning that
        # accompanies it fires only for MULTI-THREADED parents, and this is a
        # single-threaded CLI.
        ctx = mp.get_context("fork")
        with ctx.Pool(jobs) as pool:
            results = pool.imap(_probe, ((guard, c, w) for c, w in corpus), chunksize=32)
            for i, outcome in enumerate(results, 1):
                if i % 2000 == 0:
                    print(f"  … {i}/{len(corpus)}", file=sys.stderr)
                substituted += outcome.substituted
                crashed += outcome.crashed
                timed_out += outcome.timed_out
                if outcome.blocked:
                    blocked.append(outcome.row)
    else:
        for i, (cmd, cwd) in enumerate(corpus, 1):
            if i % 5000 == 0:
                print(f"  … {i}/{len(corpus)}", file=sys.stderr)
            outcome = _probe((guard, cmd, cwd))
            substituted += outcome.substituted
            crashed += outcome.crashed
            timed_out += outcome.timed_out
            if outcome.blocked:
                blocked.append(outcome.row)
    n = len(corpus)
    pct = (100.0 * len(blocked) / n) if n else 0.0
    # "blocked", not "benign". Nothing here classifies a command as benign or
    # dangerous, and the earlier wording asserted the classification anyway —
    # directly against this script's promise to report a rate and never a
    # verdict. It matters: the figure it produced was quoted in a PR body as a
    # false-positive rate when this PR's own numbers said 146 of 150 were
    # genuine. Reading the sample below is how a rate becomes a verdict.
    print(f"{guard:16s} blocked {len(blocked)}/{n} ({pct:.2f}%) — UNCLASSIFIED")
    if safety.caveat:
        # With the rate, not in --list only. The number is what gets pasted into
        # a PR body, so anything qualifying it has to travel alongside it.
        print(f"    caveat: {safety.caveat}")
    # NO SAMPLE OUTPUT. `--show` printed the first N blocked commands, and it is
    # LIFTED OUT of this PR rather than patched, because two independent review
    # findings landed on those nine lines and both were about the same thing: the
    # printed sample not being what was actually measured.
    #
    #   * it promised VERBATIM and delivered `" ".join(cmd.split())[:150]`, which
    #     collapses significant whitespace and drops the tail — possibly the very
    #     token that caused the block;
    #   * it labelled the row with the RECORDED cwd while `_effective_cwd()` had
    #     classified it from the repo root, and 27% of rows are substituted, so a
    #     reader could attribute a cwd-dependent verdict to a directory that was
    #     never used.
    #
    # Both are fixable, neither is fixable in nine lines, and the surface prints
    # real command lines that demonstrably contain secrets passed in argv — so it
    # gets its own change with its own review rather than riding along here — see
    # issue #2007, which records what a correct version owes. The
    # blocked ROWS are still accumulated (that is where the count comes from, and
    # it is what a differential mode would diff); only the printing is gone.
    if substituted:
        print(
            f"    note: {substituted}/{n} replayed from the repo root because the "
            "recorded directory no longer exists",
        )
    if crashed:
        # Loud, and phrased so the number above cannot be quoted as a rate. A
        # crash counted as a block is how a guard that never ran once prints a
        # clean 100%.
        print(
            f"    WARNING: {crashed}/{n} invocations RAISED and were counted as "
            "blocks — the figure above is not a measurement until this is 0"
        )
    if timed_out:
        print(
            f"    WARNING: {timed_out}/{n} invocations TIMED OUT after "
            f"{_GUARD_TIMEOUT_S}s and were counted as blocks — production would "
            "block too, but a hung guard is not the same measurement as a "
            "deliberate one"
        )
    # The COUNT is not the whole result. A guard that crashed or hung on every
    # row still produces a number, and main() used to discard this value and
    # return 0 — so a wrapper saw success while the output said, in words, that
    # the figure is not a measurement. Refusals already exit 2 for exactly that
    # reason; this closes the same hole one level in.
    return Result(blocked=len(blocked), valid=not crashed and not timed_out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # MUTUALLY EXCLUSIVE, because every pairing of these silently produced a
    # SUCCESSFUL PARTIAL RUN rather than an error. `--all --guard <name>` ran
    # that one guard and exited 0 while the closing `NOT MEASURED` line named
    # only the REFUSED guards — so the other replayable ones were omitted from
    # both the run and the disclosure, which is the one thing that line exists to
    # prevent. `--list` combined with either simply won and exited 0 without
    # measuring anything. A selector conflict is a question the tool cannot
    # answer, and answering it with a subset is worse than refusing.
    mode = ap.add_mutually_exclusive_group()
    # Refused guards stay in `choices` deliberately. Dropping them would answer
    # `--guard bash_safety` with "invalid choice", which reads as a typo; the
    # useful answer is the paragraph saying what replaying it would do.
    mode.add_argument("--guard", choices=sorted(GUARDS))
    mode.add_argument("--all", action="store_true")
    mode.add_argument("--list", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-extract the corpus")
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="cap corpus size (smoke runs); omit for the whole corpus",
    )
    ap.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 2),
        help="parallel workers for the subprocess-based shell guards "
        "(leaves 2 cores for the live services on this box)",
    )
    args = ap.parse_args()

    if args.limit is not None and args.limit < 1:
        # `--limit 0` used to mean NO LIMIT, because 0 is falsy and the slice was
        # guarded by `if args.limit`. Meanwhile `--show 0` means "show none". An
        # operator smoke-testing the empty-corpus refusal with `--limit 0` got
        # the full multi-minute run instead, with no message. The default is None
        # now, so "omitted" and "zero" are different things and zero is refused
        # rather than silently reinterpreted.
        ap.error("--limit must be >= 1 (omit it entirely to use the whole corpus)")
    if args.jobs < 1:
        # The same guard --show and --limit already have, and it was the missing
        # one. `jobs > 1` is the pool test, so 0 or a negative silently takes the
        # SERIAL path — turning a ~14-minute pooled run into hours with no
        # message at all. A wrong number is loud here; a wrong runtime is not.
        ap.error("--jobs must be >= 1")

    if args.list:
        if args.rebuild:
            # --list returns before load_corpus, so --rebuild here does nothing.
            # Say so: the only thing worse than ignoring a flag is ignoring it
            # quietly, and an operator who passed it is waiting for a rebuild.
            print(
                "note: --rebuild has no effect with --list (nothing reads the "
                "corpus on this path); run it with --guard or --all.",
                file=sys.stderr,
            )
        # Every guard, INCLUDING the refused ones. A refused guard vanishing from
        # --list is exactly the absence-as-exclusion pattern this replaced.
        for name in sorted(GUARDS):
            safety = GUARDS[name].safety
            print(f"{name:16s} {'replayable' if safety.safe else 'REFUSED'}")
            print(f"    {safety.why}")
            if safety.caveat:
                print(f"    caveat: {safety.caveat}")
            ev = safety.evidence
            if isinstance(ev, PyEvidence):
                facts = " ".join(
                    f"{f}={getattr(ev, f)}"
                    for f in ("reads_env", "reads_argv", "spawns", "writes_fs")
                )
                print(f"    facts ({ev.module}, transitive): {facts}")
            elif isinstance(ev, ShEvidence):
                print(
                    f"    shell ({ev.label}): "
                    f"scripts={', '.join(ev.references_scripts) or 'none'} | "
                    f"programs={', '.join(ev.references_programs) or 'none'}"
                )
            if safety.cites:
                print(f"    cites: {len(safety.cites)} construct(s), machine-resolved")
        # Printed ONCE, at the end, because it qualifies every `false` above. A
        # fact is only ever "none of these spellings" — the tool says which, so
        # nobody reads `spawns=False` as a proof of purity it cannot be.
        print("\ncoverage — a False fact means none of these spellings were found:")
        for fact, spellings in fact_coverage().items():
            print(f"  {fact:11s} {len(spellings):2d}  {', '.join(spellings)}")
        print(
            f"  shell       {len(DELEGATED_PROGRAMS):2d}  {', '.join(DELEGATED_PROGRAMS)}"
            f"\n              plus {len(repo_script_names())} tracked script basenames "
            "under scripts/ (git ls-files)"
        )
        print(
            "\nNot covered, and named rather than left silent:"
            "\n  - Facts are matched as TEXT, over the transitive import closure. An"
            "\n    aliased or from-imported spelling IS resolved (import subprocess as"
            "\n    sp; from subprocess import run), but a name reached through getattr"
            "\n    or rebound at runtime is not."
            "\n  - The shell scan reads NAMES in the text. A path built at runtime —"
            '\n    bash_safety_hook.sh already invokes through "$SCRIPT_DIR/hooks/$_guard"'
            "\n    — is only visible because the loop above it lists the basenames"
            "\n    literally. Replace that with a glob and the delegation disappears from"
            "\n    this scan while widening in reality."
            "\n  - inline_blob's claim that the blob reads no inherited variable is a"
            "\n    MEASUREMENT over an embedded shell string, not a citation. Re-running"
            "\n    it is different machinery and this checker does not vouch for it."
        )
        return 0
    if not args.guard and not args.all:
        ap.error("pass --guard <name>, --all, or --list")

    # Refuse BEFORE load_corpus: the corpus build walks the whole transcript
    # tree, and a refusal
    # that arrives after it has already spent the time is not a refusal.
    if args.guard and not GUARDS[args.guard].safety.safe:
        print(f"REFUSED: {args.guard} is not replay-safe.\n", file=sys.stderr)
        print(f"  {GUARDS[args.guard].safety.why}\n", file=sys.stderr)
        print(
            "  There is deliberately no --force. An override on a measurement "
            "tool is a bypass, and bypasses do not stay confined to the tool "
            "that adds them. Make the guard safe to replay instead.",
            file=sys.stderr,
        )
        # Exit 2, never 0: a refusal that exits 0 lets a wrapper — or a reader
        # skimming a CI log — conclude the measurement succeeded.
        return 2

    names = [args.guard] if args.guard else sorted(n for n in GUARDS if GUARDS[n].safety.safe)
    refused = sorted(n for n in GUARDS if not GUARDS[n].safety.safe) if args.all else []
    if not names:
        print(
            "REFUSED: no guard in the table is replay-safe, so --all measured "
            "nothing. Exiting 2 rather than 0, because a run that measured "
            "nothing must not read as a clean sweep.",
            file=sys.stderr,
        )
        return 2

    corpus = load_corpus(rebuild=args.rebuild)
    if args.limit is not None:
        corpus = corpus[: args.limit]
    if not corpus:
        # The same rule as the no-safe-guards refusal above, one level in. An
        # unreadable transcript tree, an empty cache file, or a tree holding no
        # Bash records all reach here with `corpus == []`, and every selected
        # guard would then print `blocked 0/0 (0.00%)` and report valid=True,
        # because `pct` is defined as 0.0 when n == 0 and nothing crashed. Exit 0
        # on that is the empty-success shape this file already refuses twice: a
        # run that measured NOTHING must not read as a clean sweep, and this is
        # the output most likely to be pasted into a PR body.
        print(
            "REFUSED: the corpus is empty, so every guard would print 0/0 "
            "(0.00%). Exiting 2 rather than 0, because a measurement of nothing "
            "must not read as a clean sweep. Try --rebuild.",
            file=sys.stderr,
        )
        return 2
    print(f"corpus: {len(corpus)} unique real commands\n")

    if any(GUARDS[n].spawns_process for n in names):
        # Announced, not silent. The recorded rows carry no session flag, so the
        # harness has to choose one; it pins the variable ABSENT, and a rate
        # measured under one setting is not the rate under the other.
        print(
            "note: GENESIS_CC_SESSION is unset in the guard child. A dispatched "
            'session sets it to "1", which changes what the shell hooks do — '
            "these numbers are the interactive-session rates.",
        )

    invalid: list[str] = []
    for name in names:
        if not replay(name, corpus, args.jobs).valid:
            invalid.append(name)

    if refused:
        # Named, not omitted. A sweep that silently skipped guards reads as a
        # sweep that covered them.
        print(f"\nNOT MEASURED: {', '.join(refused)}")
        print("    run --list for why each is refused.")

    print(
        "\nA rate is not a verdict: 0 blocked reads the same for a correct guard "
        "and an inert one. Pair every number with a positive control.",
        file=sys.stderr,
    )
    if invalid:
        print(
            f"\nEXIT 2: {', '.join(invalid)} produced no valid measurement — see "
            "the RAISED/TIMED OUT lines above. The rates are printed because they "
            "help diagnose the cause, not because they can be quoted.",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    # Belt and braces, and deliberately kept even though nothing currently reads
    # it on this path: GREPPED 2026-09-10, the readers are src/genesis/env.py and
    # four scripts/genesis_*.py, and neither replayed guard imports any of them.
    # It is set only under __main__, so an importing caller is unaffected. Kept
    # rather than dropped because a guard added later that resolves the repo via
    # genesis.env would otherwise resolve it from the CWD the harness just moved.
    os.environ.setdefault("GENESIS_REPO_ROOT", str(_REPO))
    sys.exit(main())
