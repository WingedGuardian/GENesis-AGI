#!/usr/bin/env python3
"""Replay this install's real shell commands through a Bash guard and report the
benign-block rate.

Rule 5 of the hook contract requires a `blocked k/N benign` line in the PR body of
any change to a Bash guard's PREDICATE. That number cannot come from CI: the corpus
is built from this install's own session transcripts, which contain real commands
(paths, hostnames, occasionally secrets in argv), so it is never checked in and
never leaves the box. Rule 5 is therefore a PRE-PUSH obligation, and this is the
tool that discharges it. Before this existed the figure was produced by a one-off
sweep whose only surviving trace is a comment (git_push_guard.py, the 11,488-command
measurement) — the third such one-off, which is why it is a script now.

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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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
    tmp = _CACHE.with_name(_CACHE.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # os.open's mode argument applies ONLY on the call that actually CREATES the
    # file, so a leftover temp from an earlier run would keep its old mode.
    # Tighten the inode explicitly, before a single byte is written.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        for pair in cmds:
            f.write(json.dumps(pair) + "\n")
    os.replace(tmp, _CACHE)
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
    except Exception:
        # A crash is what run_guard converts to a BLOCK for these fail-closed
        # guards, so count it as one rather than silently scoring it benign.
        return True
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


def _run_shell_guard(argv: list[str], cmd: str, cwd: str) -> bool:
    here = _effective_cwd(cwd)
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        input=json.dumps(_payload(cmd, here)),
        capture_output=True,
        text=True,
        timeout=_GUARD_TIMEOUT_S,
        cwd=here,
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


# git_discard_guard is deliberately ABSENT. Replaying it is not read-only: it
# runs a real `git stash create` per candidate command and appends a row to
# ~/.genesis/git_discard_snapshots.jsonl, the live recovery log. A measurement
# tool must not mutate the state it is measuring around, and 48,363 stash
# objects is not a side effect worth any number. It is also the one guard not
# wrapped by run_guard, so this harness's crash-counts-as-block rule would
# misreport it: in production it fails OPEN.
# git_push is deliberately ABSENT for a different reason, and it is worth
# stating separately rather than folding into the note above. It performs no
# writes — it is read-only in the filesystem sense — but it shells out to `gh
# repo view` / `gh pr view` and to git at classify time, and the corpus holds
# 1,195 push-shaped rows plus 924 `gh pr create|merge`-shaped ones. Replaying it
# would make on the order of thousands of live GitHub API calls against the
# user's account and burn their rate limit, from a tool whose docstring says it
# replays local history. "Read-only" and "safe to run 2,119 times against a
# remote API" are different claims. It also has no timeout on the in-process
# path (_GUARD_TIMEOUT_S reaches only the shell runner), so one hung call stalls
# the run indefinitely.
GUARDS: dict[str, object] = {
    "protected_paths": lambda c, w: _run_python_guard("protected_paths_guard", c, w),
    "worktree_cwd": lambda c, w: _run_python_guard("worktree_cwd_guard", c, w),
    "bash_safety": lambda c, w: _run_shell_guard(
        ["bash", str(_REPO / "scripts" / "bash_safety_hook.sh")], c, w
    ),
    "inline_blob": lambda c, w: _run_shell_guard(["bash", "-c", _INLINE_BLOB], c, w),
}


# Read ONCE. Inside the lambda this re-read and re-parsed settings.json for
# every corpus row, times every worker.
_INLINE_BLOB = _inline_blob()

_SHELL_GUARDS = frozenset({"bash_safety", "inline_blob"})


def _probe(args: tuple[str, str, str]) -> tuple[bool, bool, bool]:
    """(blocked, cwd_was_substituted, crashed) for one command.

    Returns the substitution flag rather than relying on the module counter:
    for shell guards this runs in a FORKED CHILD, so an increment to a module
    global lands in the child's copy and the parent never sees it. The
    disclosure the report calls the alternative to "quietly folding them into
    the rate" was doing precisely that, on the only two guards that fan out.

    Crashes are reported, not folded into the block count. Counting a crash as a
    block let a guard that never ran once print a clean, quotable 100%.
    """
    guard, cmd, cwd = args
    _SUBSTITUTED_CWD["n"] = 0
    try:
        hit = bool(GUARDS[guard](cmd, cwd))  # type: ignore[operator]
        return hit, bool(_SUBSTITUTED_CWD["n"]), False
    except subprocess.TimeoutExpired:
        return True, bool(_SUBSTITUTED_CWD["n"]), False  # a hung guard is not a pass
    except Exception:
        return True, bool(_SUBSTITUTED_CWD["n"]), True


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
    blocked: list[str] = []
    substituted = 0
    crashed = 0
    # BOTH paths go through _probe, so they cannot drift in what they count. They
    # did: the pool swallowed every exception as a block while the serial loop
    # let it propagate, so the same broken guard reported 100% blocked or a
    # traceback depending only on the job count.
    if guard in _SHELL_GUARDS and jobs > 1:
        import multiprocessing as mp

        with mp.Pool(jobs) as pool:
            results = pool.imap(_probe, ((guard, c, w) for c, w in corpus), chunksize=32)
            for i, (hit, sub, crash) in enumerate(results, 1):
                if i % 2000 == 0:
                    print(f"  … {i}/{len(corpus)}", file=sys.stderr)
                substituted += sub
                crashed += crash
                if hit:
                    blocked.append(corpus[i - 1][0])
    else:
        for i, (cmd, cwd) in enumerate(corpus, 1):
            if i % 5000 == 0:
                print(f"  … {i}/{len(corpus)}", file=sys.stderr)
            hit, sub, crash = _probe((guard, cmd, cwd))
            substituted += sub
            crashed += crash
            if hit:
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
    return len(blocked)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
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
        for name in sorted(GUARDS):
            print(name)
        return 0
    if not args.guard and not args.all:
        ap.error("pass --guard <name>, --all, or --list")

    corpus = load_corpus(rebuild=args.rebuild)
    if args.limit:
        corpus = corpus[: args.limit]
    print(f"corpus: {len(corpus)} unique real commands\n")

    names = sorted(GUARDS) if args.all else [args.guard]
    for name in names:
        replay(name, corpus, args.show, args.jobs)
    print(
        "\nA rate is not a verdict: 0 blocked reads the same for a correct guard "
        "and an inert one. Pair every number with a positive control.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    os.environ.setdefault("GENESIS_REPO_ROOT", str(_REPO))
    sys.exit(main())
