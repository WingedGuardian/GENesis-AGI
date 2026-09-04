#!/usr/bin/env python3
"""Replay this install's real shell commands through a Bash guard and report the
benign-block rate.

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
    python3 scripts/replay_guard_corpus.py --all --show 20
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
"""

from __future__ import annotations

import argparse
import contextlib
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


# ── corpus ───────────────────────────────────────────────────────────────────


def _extract_commands() -> list[tuple[str, str]]:
    """Every Bash `input.command` in this install's transcripts, deduped.

    Streams line by line: the transcript tree is ~1.4 GB and this box is swapless,
    so it is never read whole.
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
                msg = rec.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                    continue
                for block in msg["content"]:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") != "Bash":
                        continue
                    cmd = (block.get("input") or {}).get("command")
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
            rows = [None]
        if any(r is None or isinstance(r, str) for r in rows):
            print(
                "cache predates the cwd field (v1) — rebuilding, because "
                "replaying it would measure the wrong directory",
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
    # ATOMICALLY. The build walks ~1.4 GB and takes minutes; interrupting it
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
    """
    mod = _run_python_guard._loaded.get(module_name)  # type: ignore[attr-defined]
    if mod is None:
        mod = __import__(module_name)
        _run_python_guard._loaded[module_name] = mod  # type: ignore[attr-defined]
    here = _effective_cwd(cwd)
    payload = _payload(cmd, here)
    original = getattr(mod, "read_payload", None)
    mod.read_payload = lambda: payload  # type: ignore[assignment]
    # These guards read os.getcwd() DIRECTLY (worktree_cwd_guard's self-brick
    # check, the routing guard's repo resolution) — the payload's cwd field is
    # not what they consult, so the process cwd has to move as well or the
    # threading would be cosmetic.
    prior = os.getcwd()
    try:
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
        os.chdir(prior)
        # Unconditional. If a guard ever lacks the attribute, restoring only on
        # the not-None branch leaves the patched lambda installed on the module
        # for the rest of the process — every later command would then be
        # classified against this command's payload.
        if original is not None:
            mod.read_payload = original
        else:
            with contextlib.suppress(AttributeError):
                delattr(mod, "read_payload")


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


@dataclass(frozen=True)
class ReplaySafety:
    """Whether replaying a guard 51,052 times is safe, and the evidence for it."""

    safe: bool
    why: str
    # Printed WITH the rate rather than instead of it. A caveat has to travel
    # with the number, because the number is what gets pasted into a PR body.
    caveat: str = ""


def replay_safe(why: str, *, caveat: str = "") -> ReplaySafety:
    return ReplaySafety(safe=True, why=why, caveat=caveat)


def not_replay_safe(why: str) -> ReplaySafety:
    return ReplaySafety(safe=False, why=why)


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


def _inline_blob() -> str:
    """The inline mega-guard, read from tracked settings.json.

    Located the same way tests/test_hooks/test_inline_settings_guard.py locates it,
    deliberately: if that discovery ever breaks, both break together and loudly,
    rather than this harness silently measuring a different hook.
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
    "protected_paths": Guard(
        run=lambda c, w: _run_python_guard("protected_paths_guard", c, w),
        safety=replay_safe(
            "a pure argv/string classifier — no filesystem writes, no subprocess, "
            "no network, no environment reads."
        ),
    ),
    "worktree_cwd": Guard(
        run=lambda c, w: _run_python_guard("worktree_cwd_guard", c, w),
        safety=replay_safe(
            "no writes, no network, no subprocess. It does scan /proc "
            "(worktree_cwd_guard.py:168,178) to list processes sitting in a "
            "target directory.",
            caveat=(
                "reads /proc, so the DIAGNOSTIC TEXT varies between runs. The "
                "verdict does not: every branch after that read returns 2 "
                "(_block_with_pids and _block_no_direct_removal are both "
                "unconditional), so the rate is reproducible even though the "
                "message is not."
            ),
        ),
    ),
    "inline_blob": Guard(
        run=lambda c, w: _run_shell_guard(["bash", "-c", _INLINE_BLOB], c, w),
        spawns_process=True,
        safety=replay_safe(
            "a stdin->stderr classifier. Its only redirect is >/dev/null and its "
            "only nonzero exit is 2; the `stash` and `sqlite3` tokens in it are "
            "message text and a grep pattern, not invocations. MEASURED: the "
            "blob references only CMD and IN, its own shell locals — it reads no "
            "inherited environment variable at all."
        ),
    ),
    "bash_safety": Guard(
        run=lambda c, w: _run_shell_guard(
            ["bash", str(_REPO / "scripts" / "bash_safety_hook.sh")], c, w
        ),
        spawns_process=True,
        safety=not_replay_safe(
            "it DELEGATES to git_discard_guard.py (bash_safety_hook.sh:245-265, "
            "on a git checkout/restore/reset/switch/clean/rm/mv/read-tree glob), "
            "whose git_discard_guard.py:227 runs `git stash create` against the "
            "LIVE repository at each row's recorded directory. That is not "
            "redirectable: GENESIS_DISCARD_SNAPSHOT_LOG (git_discard_guard.py:201) "
            "moves the JSONL recovery log only, never the objects the stash "
            "writes. MEASURED in a scratch repo with uncommitted work: 4 "
            "discard-shaped commands produced 4 recovery rows plus loose objects. "
            "A clean tree writes nothing, which is why an early probe found no "
            "problem. Secondary, and latent rather than live: bash_safety_hook.sh "
            "374/381 call `gh pr view`, reachable when _in_genesis is 0 or when "
            'GENESIS_CC_SESSION is exactly "1" — the value every dispatched '
            "session sets."
        ),
    ),
    "git_discard": Guard(
        run=lambda c, w: _run_python_guard("git_discard_guard", c, w),
        safety=not_replay_safe(
            "PRESENT AND REFUSED rather than absent, because absence teaches "
            "nothing at --list and absence-as-exclusion is the pattern that "
            "already failed here. `git stash create` per candidate command "
            "writes objects into whatever live repository the row was recorded "
            "in, and each snapshot appends to ~/.genesis/git_discard_snapshots.jsonl "
            "— a log that self-trims at 1 MB keeping the most recent half, so one "
            "replay would evict the genuine recovery history it exists to hold. "
            "It is also the one guard not wrapped by run_guard, so this harness's "
            "crash-counts-as-block rule would misreport it: in production it "
            "fails OPEN."
        ),
    ),
    "git_push": Guard(
        run=lambda c, w: _run_python_guard("git_push_guard", c, w),
        safety=not_replay_safe(
            "read-only on the filesystem, but it shells out to `gh repo view` / "
            "`gh pr view` and to git at classify time, and the corpus holds 1,195 "
            "push-shaped rows plus 924 `gh pr create|merge`-shaped ones. That is "
            "thousands of live GitHub API calls against the owner's account, and "
            "their rate limit, from a tool whose docstring says it replays local "
            'history. "Read-only" and "safe to run 2,119 times against a remote '
            'API" are different claims. It also has no timeout on the in-process '
            "path, so one hung call stalls the run indefinitely."
        ),
    ),
}


# Read ONCE, and AFTER the table. Inside the lambda this is resolved at call
# time, so the ordering works; a field that evaluated it eagerly would NameError
# at import. Hoisted out of the lambda because it was re-reading and re-parsing
# settings.json for every corpus row, times every worker.
_INLINE_BLOB = _inline_blob()


class Outcome(NamedTuple):
    """One command's result. A NamedTuple because it crosses the mp.Pool
    boundary and pickles as a plain tuple, and because four positional bools
    were already one transposition away from a silent mis-count."""

    blocked: bool
    substituted: bool
    crashed: bool
    timed_out: bool


def _probe(args: tuple[str, str, str]) -> Outcome:
    """One command's outcome, with the reasons kept SEPARATE from the verdict.

    Crashes and timeouts both count as blocks — that is what production does for
    these fail-closed guards — but each is reported on its own channel. Folding
    them into the numerator silently is how a guard that never ran once printed a
    clean, quotable 100%.
    """
    guard, cmd, cwd = args
    _SUBSTITUTED_CWD["n"] = 0
    try:
        hit = bool(GUARDS[guard].run(cmd, cwd))
        return Outcome(hit, bool(_SUBSTITUTED_CWD["n"]), False, False)
    except subprocess.TimeoutExpired:
        # A hung guard is not a pass. It was already counted as a block, but
        # invisibly: crashed stayed False, so nothing in the report distinguished
        # "the guard blocked this" from "the guard never answered". A hang and a
        # crash also have different fixes, which is why they get different verbs.
        return Outcome(True, bool(_SUBSTITUTED_CWD["n"]), False, True)
    except Exception:
        return Outcome(True, bool(_SUBSTITUTED_CWD["n"]), True, False)


def replay(guard: str, corpus: list[tuple[str, str]], show: int, jobs: int) -> int:
    """Replay the corpus through one guard.

    The two guard shapes have wildly different costs: an in-process Python guard
    runs the whole corpus in ~17s (measured on `protected_paths`, which shells
    out to nothing — a guard that spawns git per command is far slower), while a
    shell guard spawns a process per command
    (~260 ms each — it shells out to git internally), which is ~3.5 h serially. So
    shell guards fan out across processes. Python guards stay serial: they are
    already fast, and they hold module state that a pool would have to re-import
    per worker.
    """
    # Second layer. The CLI refuses before it gets here, but importing this
    # module and calling replay() directly must not be a way around the
    # declaration — a bypass that needs no flag is still a bypass.
    safety = GUARDS[guard].safety
    if not safety.safe:
        raise RuntimeError(f"{guard} is not replay-safe: {safety.why}")

    blocked: list[str] = []
    substituted = 0
    crashed = 0
    timed_out = 0
    # BOTH paths go through _probe, so they cannot drift in what they count. They
    # did: the pool swallowed every exception as a block while the serial loop
    # let it propagate, so the same broken guard reported 100% blocked or a
    # traceback depending only on the job count.
    if GUARDS[guard].spawns_process and jobs > 1:
        import multiprocessing as mp

        with mp.Pool(jobs) as pool:
            results = pool.imap(_probe, ((guard, c, w) for c, w in corpus), chunksize=32)
            for i, outcome in enumerate(results, 1):
                if i % 2000 == 0:
                    print(f"  … {i}/{len(corpus)}", file=sys.stderr)
                substituted += outcome.substituted
                crashed += outcome.crashed
                timed_out += outcome.timed_out
                if outcome.blocked:
                    blocked.append(corpus[i - 1][0])
    else:
        for i, (cmd, cwd) in enumerate(corpus, 1):
            if i % 5000 == 0:
                print(f"  … {i}/{len(corpus)}", file=sys.stderr)
            outcome = _probe((guard, cmd, cwd))
            substituted += outcome.substituted
            crashed += outcome.crashed
            timed_out += outcome.timed_out
            if outcome.blocked:
                blocked.append(cmd)
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
    for cmd in blocked[:show]:
        flat = " ".join(cmd.split())
        print(f"    {flat[:150]}")
    if show and len(blocked) > show:
        print(f"    … and {len(blocked) - show} more")
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
    return len(blocked)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    # Refused guards stay in `choices` deliberately. Dropping them would answer
    # `--guard bash_safety` with "invalid choice", which reads as a typo; the
    # useful answer is the paragraph saying what replaying it would do.
    ap.add_argument("--guard", choices=sorted(GUARDS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rebuild", action="store_true", help="re-extract the corpus")
    ap.add_argument(
        "--show",
        type=int,
        default=0,
        help="print N blocked commands VERBATIM. Off by default: these are real\n"
        "command lines and demonstrably contain secrets passed in argv, so the\n"
        "output must never be pasted into a PR body or an issue.",
    )
    ap.add_argument("--limit", type=int, default=0, help="cap corpus size (smoke runs)")
    ap.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 2),
        help="parallel workers for the subprocess-based shell guards "
        "(leaves 2 cores for the live services on this box)",
    )
    args = ap.parse_args()

    if args.list:
        # Every guard, INCLUDING the refused ones. A refused guard vanishing from
        # --list is exactly the absence-as-exclusion pattern this replaced.
        for name in sorted(GUARDS):
            safety = GUARDS[name].safety
            print(f"{name:16s} {'replayable' if safety.safe else 'REFUSED'}")
            print(f"    {safety.why}")
            if safety.caveat:
                print(f"    caveat: {safety.caveat}")
        return 0
    if not args.guard and not args.all:
        ap.error("pass --guard <name>, --all, or --list")

    # Refuse BEFORE load_corpus: the corpus build walks ~1.4 GB, and a refusal
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
    if args.limit:
        corpus = corpus[: args.limit]
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

    for name in names:
        replay(name, corpus, args.show, args.jobs)

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
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GENESIS_REPO_ROOT", str(_REPO))
    sys.exit(main())
