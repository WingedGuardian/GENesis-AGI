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
import io
import json
import os
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


def _extract_commands() -> list[str]:
    """Every Bash `input.command` in this install's transcripts, deduped.

    Streams line by line: the transcript tree is ~1.4 GB and this box is swapless,
    so it is never read whole.
    """
    seen: set[str] = set()
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
                        seen.add(cmd)
    return sorted(seen)


def load_corpus(*, rebuild: bool = False) -> list[str]:
    if _CACHE.exists() and not rebuild:
        _harden(_CACHE)
        with _CACHE.open() as f:
            return [json.loads(line) for line in f if line.strip()]
    print("building corpus from transcripts (streaming)…", file=sys.stderr)
    cmds = _extract_commands()
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    # Create it 0600 BEFORE writing, not after: the corpus is verbatim command
    # lines from real sessions and demonstrably contains secrets passed in argv
    # (an inline `SSHPASS=…` was found in it), so it must never exist even
    # briefly at the default 0644.
    fd = os.open(_CACHE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        for c in cmds:
            f.write(json.dumps(c) + "\n")
    print(f"cached {len(cmds)} unique commands -> {_CACHE} (mode 0600)", file=sys.stderr)
    return cmds


def _harden(path: Path) -> None:
    """Tighten an existing cache written before the 0600 default."""
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
            print(f"tightened {path} to 0600 (it held real commands)", file=sys.stderr)
    except OSError:
        pass


# ── guard invocation ─────────────────────────────────────────────────────────
#
# Two modes, because the guards genuinely have two shapes. Both are the shape the
# existing tests already use, so a disagreement between this harness and the suite
# would be a bug in one of them, not a third opinion.


def _payload(cmd: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "cwd": str(_REPO),
    }


def _run_python_guard(module_name: str, cmd: str) -> bool:
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
    payload = _payload(cmd)
    original = getattr(mod, "read_payload", None)
    mod.read_payload = lambda: payload  # type: ignore[assignment]
    try:
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
        if original is not None:
            mod.read_payload = original


_run_python_guard._loaded = {}  # type: ignore[attr-defined]


def _run_shell_guard(argv: list[str], cmd: str) -> bool:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        argv,
        input=json.dumps(_payload(cmd)),
        capture_output=True,
        text=True,
        timeout=_GUARD_TIMEOUT_S,
        cwd=str(_REPO),
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
GUARDS: dict[str, object] = {
    "protected_paths": lambda c: _run_python_guard("protected_paths_guard", c),
    "worktree_cwd": lambda c: _run_python_guard("worktree_cwd_guard", c),
    "git_push": lambda c: _run_python_guard("git_push_guard", c),
    "bash_safety": lambda c: _run_shell_guard(
        ["bash", str(_REPO / "scripts" / "bash_safety_hook.sh")], c
    ),
    "inline_blob": lambda c: _run_shell_guard(["bash", "-c", _inline_blob()], c),
}


_SHELL_GUARDS = frozenset({"bash_safety", "inline_blob"})


def _probe(args: tuple[str, str]) -> bool:
    guard, cmd = args
    try:
        return bool(GUARDS[guard](cmd))  # type: ignore[operator]
    except subprocess.TimeoutExpired:
        return True  # a hung guard is not a pass
    except Exception:
        return True


def replay(guard: str, corpus: list[str], show: int, jobs: int) -> int:
    """Replay the corpus through one guard.

    The two guard shapes have wildly different costs: an in-process Python guard
    runs the whole corpus in ~17s, while a shell guard spawns a process per command
    (~260 ms each — it shells out to git internally), which is ~3.5 h serially. So
    shell guards fan out across processes. Python guards stay serial: they are
    already fast, and they hold module state that a pool would have to re-import
    per worker.
    """
    blocked: list[str] = []
    if guard in _SHELL_GUARDS and jobs > 1:
        import multiprocessing as mp

        with mp.Pool(jobs) as pool:
            for i, hit in enumerate(
                pool.imap(_probe, ((guard, c) for c in corpus), chunksize=32), 1
            ):
                if i % 2000 == 0:
                    print(f"  … {i}/{len(corpus)}", file=sys.stderr)
                if hit:
                    blocked.append(corpus[i - 1])
    else:
        fn = GUARDS[guard]
        for i, cmd in enumerate(corpus, 1):
            if i % 5000 == 0:
                print(f"  … {i}/{len(corpus)}", file=sys.stderr)
            try:
                if fn(cmd):  # type: ignore[operator]
                    blocked.append(cmd)
            except subprocess.TimeoutExpired:
                blocked.append(cmd)
    n = len(corpus)
    pct = (100.0 * len(blocked) / n) if n else 0.0
    print(f"{guard:16s} blocked {len(blocked)}/{n} benign ({pct:.2f}%)")
    for cmd in blocked[:show]:
        flat = " ".join(cmd.split())
        print(f"    {flat[:150]}")
    if show and len(blocked) > show:
        print(f"    … and {len(blocked) - show} more")
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
