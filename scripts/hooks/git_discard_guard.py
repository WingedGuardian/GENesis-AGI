#!/usr/bin/env python3
"""PreToolUse hook (Bash): a RECOVERY net for git commands that overwrite the
working tree — it snapshots, it never blocks.

Origin: 2026-08-22 — a verify-RED experiment restored a temporarily-broken file
with ``git checkout <file>``, silently discarding the session's real uncommitted
edits (no confirmation, no reflog for unstaged changes).

DESIGN — recoverability, not classification (2026-08-24, after the review loop)
==============================================================================
The job of DECIDING "is this command destructive?" from its argv is an OPEN-set
parser problem (flags, ``=value``, single-letter abbreviations, backslash-newline
continuations, ``--recurse-submodules``, global value-flags, config-form
recursion): every review round surfaces the next spelling, and a block is
inherently a completeness claim, so every gap is a silent-loss hole. For the
RECOVERABLE verbs (checkout/restore/switch/reset) that job is therefore NOT done
here — a snapshot makes classification unnecessary. It survives only as the
crude, dependency-free substring blocks in ``.claude/settings.json`` and
``scripts/bash_safety_hook.sh`` (``git reset --hard``), an honest best-effort
SPEED-BUMP, not a security boundary.

``git clean`` is the EXCEPTION and the one place this hook still BLOCKS (exit 2):
``git stash create`` cannot capture untracked files — exactly what ``clean``
deletes — so the snapshot net gives clean ZERO protection. The block is a
CLOSED SET on the SAFE side (allow only the exact dry-run forms; block the open
complement), which a false-ALLOW cannot penetrate — see ``_clean_violation``.
The ``# discard-override`` sigil is its escape.

The SECURITY property this hook provides is RECOVERABILITY. For every
``checkout`` / ``restore`` / ``switch`` / ``reset`` segment it runs
``git stash create`` FIRST — capturing worktree + index without mutating
anything (no ref, no stash-list entry, no tree change) — and logs the snapshot
sha. If the command then overwrites work, recovery is
``git stash apply --index <sha>``. This needs to recognize only the VERB, never
the destructive flag, so it is immune to the spelling games that sink a block —
and a MISS (unresolvable cwd, non-repo, clean tree, tokenizer split) degrades to
the status quo (no snapshot), never to a false block and never to a false
guarantee. The verb-triggered snapshot even covers commands a substring block
misses (e.g. ``git reset \\<newline> --hard`` still tokenizes with the ``reset``
verb present), turning "silent unrecoverable loss" into "recoverable".

For the recoverable verbs this hook is advisory and NEVER exits non-zero: a bug
in the snapshot path must fail OPEN (let the command run). The ONLY non-zero
exit is the ``git clean`` block above; if even that path raises, it degrades to
the dependency-free shell floor rather than crashing or false-blocking a
recoverable verb.

DOCUMENTED RESIDUALS (honest claim — the snapshot is a best-effort net):
  * ``git stash create`` excludes UNTRACKED files: a ``clean`` deletion or a
    checkout-from-tree onto an untracked path is not recovered here. (Full
    coverage — an "uncommitted reflog" that captures untracked state — is the
    tabled north-star follow-up.)
  * ``assume-unchanged`` / ``skip-worktree`` modifications and an UNMERGED
    (conflict) worktree are invisible to / error out of ``stash create``.
  * Submodule contents are not captured by a superproject ``stash create``.
  * cwd resolution is deliberately simple (payload cwd + a leading ``git -C``);
    a cd-chain / exotic repo selector may snapshot the wrong repo or none.
  * The snapshot is taken at the PreToolUse boundary, BEFORE the Bash payload
    runs, so an edit produced by an EARLIER segment of the same compound
    post-dates it.
  * Snapshot commits are unreferenced; ``git gc`` prunes them after the
    unreachable horizon (~2 weeks). Recovery is for "just happened".
  * In-project this hook is wired TWICE (the ``.claude/settings.json`` Bash
    matcher AND ``bash_safety_hook.sh``'s advisory invocation), so a recoverable
    verb may be snapshotted twice — up to two identical-cwd rows/notes. Harmless
    (idempotent ``stash create``); broader dedup is the all-hooks-review
    follow-up.
  * Git ALIASES (``git co`` for checkout, a user ``nuke`` alias for a clean) are
    invisible to argv — the guard sees the alias token, not its expansion, and
    degrades to the status quo (no snapshot; and an aliased clean is NOT blocked
    here — only its expanded form, if typed, is). No false promise, no false
    block; just uncovered.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import subprocess
import sys

# Self-locate so sibling hook modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import analyze, has_trailing_override  # noqa: E402

# Substrings that gate the parse path; absent all of them the command cannot be
# a worktree-overwriting git op, so we return instantly. `clean` is included
# because it is the ONE verb this guard still BLOCKS (see _clean_violation).
_TRIGGER_SUBSTRINGS = ("checkout", "restore", "reset", "switch", "clean")
# Verbs whose worktree effect `git stash create` can (best-effort) recover.
_SNAPSHOT_VERBS = frozenset({"checkout", "restore", "switch", "reset"})
# The sanctioned escape for the clean block: `git clean -f  # discard-override`.
_OVERRIDE_SIGIL = "discard-override"
# `git clean` is the ONE unrecoverable verb — `git stash create` cannot capture
# untracked files, which is exactly what clean deletes, so the snapshot net
# gives clean ZERO protection and it must keep a real block. The block is a
# CLOSED SET on the SAFE side: allow ONLY the exact dry-run forms, block the
# (open) complement. MEASURED (git 2.43): any dry-run flag makes clean
# print-only and never delete, in every cluster/order — so a dry-run token in
# the tail is a sound allow, and requiring the WHOLE tail ⊆ this closed set (see
# _tokens_after_subcommand's superset proof) makes a false-ALLOW impossible: an
# extra token can only enlarge the tail out of the set → over-block, never
# under-block. The canonical preview CLUSTERS (-nd/-dn) are explicit LITERAL
# members — never decomposed (cluster decomposition is flag semantics, the tar
# pit this design removed); any other cluster over-blocks, with the override the
# escape.
_CLEAN_DRY_RUN_TOKENS = frozenset({"-n", "--dry-run", "-nd", "-dn", "-d", "-x", "-X"})
_CLEAN_DRY_RUN_REQUIRED = frozenset({"-n", "--dry-run", "-nd", "-dn"})
# Bound the snapshot subprocess so a hung/huge repo can never wedge the tool
# call (blocks live in the shell layer, so the worst a timeout costs is the
# advisory recovery note). Fits the tightest 5s user-level hook budget.
_GIT_TIMEOUT_S = 3
# Self-trim the snapshot JSONL when it grows past this (no external consumer /
# rotation exists); keep the most recent half.
_SNAPSHOT_LOG_MAX_BYTES = 1_000_000
# Recovery logs may sit in ~/.genesis alongside secrets — own-user only.
_LOG_DIR_MODE = 0o700
_LOG_FILE_MODE = 0o600


def _snapshot_log_path() -> str:
    """JSONL recovery log. ``GENESIS_DISCARD_SNAPSHOT_LOG`` overrides (test seam
    + config knob); default lives outside any repo so it survives worktree
    removal and is never committed."""
    return os.environ.get("GENESIS_DISCARD_SNAPSHOT_LOG") or os.path.expanduser(
        "~/.genesis/git_discard_snapshots.jsonl"
    )


def _segment_cwd(seg, payload: dict) -> str | None:
    """DELIBERATELY simple cwd model for the best-effort snapshot: the payload
    cwd, adjusted by a leading ``git -C <dir>`` on this segment. No cd-chain
    walking, no --git-dir modeling — a shape this misses yields a missed
    snapshot (status quo), never a block (see DOCUMENTED RESIDUALS)."""
    base = payload.get("cwd") or os.getcwd()
    argv = seg.argv
    for i, tok in enumerate(argv[1:], start=1):
        if tok == "-C" and i + 1 < len(argv):
            return os.path.normpath(os.path.join(base, argv[i + 1]))
        if not tok.startswith("-"):
            break  # subcommand reached — no leading -C
    return base


def _snapshot_worktree(cwd: str) -> str | None:
    """``git stash create`` — capture worktree+index WITHOUT mutating anything.
    Returns the snapshot sha, or None (clean tree / non-repo / any error)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "stash", "create", "git-discard-guard snapshot"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _open_own(path: str, *, append: bool):
    """Open ``path`` for writing, creating it own-user-only (0600) with NO
    umask window — ``os.open`` applies the mode AT create time, unlike
    ``open()`` then ``chmod`` (which is briefly world/group-readable per the
    process umask). A pre-existing looser file is separately tightened by
    ``_restrict``; the 0700 log dir already gates access regardless."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, _LOG_FILE_MODE)
    return os.fdopen(fd, "a" if append else "w", encoding="utf-8")


def _write_log_row(row: dict) -> str | None:
    """Append ``row`` to the recovery JSONL under an exclusive flock on a SIDECAR
    lock file (locking the log fh itself is defeated by the trim's os.replace —
    the lock rides the OLD inode and a waiter appends to the orphan). The
    sidecar is never replaced, so every writer serializes on one inode. Returns
    the log path on success, None on any OS error. Files are created own-user
    only (the log can carry local repo paths and sits beside secrets)."""
    log_path = _snapshot_log_path()
    # A relative override has an empty dirname — makedirs("") raises.
    os.makedirs(os.path.dirname(log_path) or ".", mode=_LOG_DIR_MODE, exist_ok=True)
    lock_path = log_path + ".lock"
    with _open_own(lock_path, append=True) as lk:
        _restrict(lock_path)  # tighten a pre-existing loose sidecar
        fcntl.flock(lk, fcntl.LOCK_EX)
        with _open_own(log_path, append=True) as fh:
            _restrict(log_path)
            fh.write(json.dumps(row) + "\n")
        if os.path.getsize(log_path) > _SNAPSHOT_LOG_MAX_BYTES:
            with open(log_path, encoding="utf-8") as rd:
                lines = rd.readlines()
            tmp = log_path + ".tmp"
            with _open_own(tmp, append=False) as wr:
                wr.writelines(lines[len(lines) // 2 :])
            os.replace(tmp, log_path)
    return log_path


def _restrict(path: str) -> None:
    """Best-effort chmod to own-user-only; a chmod failure never aborts logging."""
    with contextlib.suppress(OSError):
        os.chmod(path, _LOG_FILE_MODE)


def _tokens_after_subcommand(argv: list[str], sub: str) -> list[str]:
    """Every token after the first literal occurrence of ``sub``. Positional, no
    flag/value modeling (that would be the tar pit) — the caller makes only
    closed-set membership claims about these tokens. Direction proof: the first
    occurrence of ``clean`` is at or before the REAL subcommand (e.g.
    ``git -C clean clean`` anchors on the ``-C`` VALUE), so the returned tail is
    a SUPERSET of the real tail — a whitelist check over a superset can only
    OVER-block (+ override escape), never under-block."""
    try:
        idx = argv.index(sub)
    except ValueError:
        return []
    return argv[idx + 1 :]


def _clean_violation(cmd: str) -> str | None:
    """The ONE block this otherwise snapshot-only guard still makes: a
    non-dry-run ``git clean``. Returns a block message if any ``git`` segment
    runs ``clean`` in a form outside the exact dry-run whitelist (and lacks a
    ``# discard-override``), else None. Pure argv — no subprocess, no repo
    state — so it cannot be starved by a slow probe. Literal-membership verb
    detection (``"clean" in argv``) NOT positional resolution: resolution
    depends on skipping git's OPEN set of value-taking global flags, so a
    pathological ``git checkout clean`` merely over-blocks (override escape) —
    the direction this boundary wants."""
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        if "clean" not in set(seg.argv[1:]):
            continue
        if has_trailing_override(seg.raw, _OVERRIDE_SIGIL):
            continue
        tail = _tokens_after_subcommand(seg.argv, "clean")
        is_exact_dry_run = (
            bool(set(tail) & _CLEAN_DRY_RUN_REQUIRED) and set(tail) <= _CLEAN_DRY_RUN_TOKENS
        )
        if not is_exact_dry_run:
            return (
                "[git-discard-guard] BLOCKED: `git clean` permanently deletes "
                "untracked files, which the recovery snapshot CANNOT restore "
                "(`git stash create` excludes untracked). Only the exact dry-run "
                "form is allowed (-n / --dry-run, optionally with -d/-x/-X); "
                "anything else — force flags, path arguments, exclude patterns — "
                "needs `# discard-override`. Preview first with `git clean -nd`."
            )
    return None


def _record_snapshots(cmd: str, payload: dict) -> None:
    """Best-effort recovery net. NEVER blocks, never raises past its own
    boundary; one snapshot per distinct resolved cwd. The logged row is
    METADATA ONLY (ts, cwd, sha) — deliberately NOT the command: the Bash
    payload can carry credentials (`curl -H 'Authorization: …' && git checkout`)
    and this log is durable."""
    seen_cwds: set[str] = set()
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        # Literal VERB membership — never positional subcommand resolution
        # (which depends on skipping git's open set of value-taking global
        # flags). Over-matching a pathological argv costs one harmless snapshot;
        # under-matching costs a missed snapshot (status quo). Neither can block.
        if not set(seg.argv[1:]) & _SNAPSHOT_VERBS:
            continue
        cwd = _segment_cwd(seg, payload)
        if not cwd or cwd in seen_cwds or not os.path.isdir(cwd):
            continue
        seen_cwds.add(cwd)
        sha = _snapshot_worktree(cwd)
        if not sha:
            continue
        row = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "cwd": cwd,
            "sha": sha,
        }
        log_path = _snapshot_log_path()
        with contextlib.suppress(OSError):
            log_path = _write_log_row(row) or log_path
        print(
            f"[git-discard-guard] snapshotted the worktree at {cwd} as "
            f"{sha[:12]} (tracked changes only — `git stash create` does not "
            f"capture untracked files). IF that is the repo this command "
            f"discarded work in, recover with: git stash apply --index {sha}  "
            f"(--index restores the staged/unstaged split; drop it if the apply "
            f"conflicts. log: {log_path})",
            file=sys.stderr,
        )


def main() -> int:
    """Block a non-dry-run ``git clean`` (the one unrecoverable verb); for every
    other trigger verb, snapshot-and-allow. Returns 2 ONLY for a clean
    violation; 0 otherwise. Any unexpected error falls through to 0 (fail OPEN)
    — the precise clean block is backstopped by the dependency-free shell floor,
    so a parser bug here degrades to that layer, never to a crash or a false
    block on a recoverable verb."""
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd or "git" not in cmd:
            return 0
        if not any(s in cmd for s in _TRIGGER_SUBSTRINGS):
            return 0
        # clean is UNRECOVERABLE by the snapshot net → the one fail-CLOSED verb.
        block_msg = _clean_violation(cmd)
        if block_msg:
            print(block_msg, file=sys.stderr)
            return 2
        _record_snapshots(cmd, payload)
    except Exception:
        pass  # advisory paths fail OPEN; a clean-parse bug degrades to the shell floor
    return 0


if __name__ == "__main__":
    sys.exit(main())
