#!/usr/bin/env python3
"""PreToolUse hook (Bash): stop git from SILENTLY destroying uncommitted work.

Origin: 2026-08-22 — a verify-RED experiment restored a temporarily-broken file
with ``git checkout <file>``, silently discarding the session's real uncommitted
edits (no confirmation, no reflog for unstaged changes). A memory only helps the
session that recalls it; this guard fires unconditionally.

DESIGN — recoverability, not classification (2026-08-23 redesign)
=================================================================
The first version of this guard classified argv→effect: which flags force, which
operands are dirty paths, which modes destroy. That is the hand-rolled-parser
tar pit in a new coat — Codex returned 13 real findings (10 P1), every one in
the argv→effect layer, regardless of the canonical tokenizer underneath.
Decision test (genesis-development skill): *could a git flag you've never heard
of change this guard's verdict?* The redesign makes the answer NO by shrinking
the claim to two provable pieces:

1. COARSE BLOCKS — closed sets of literal tokens, per-subcommand, pure argv,
   zero repo/operand semantics. An unknown flag cannot flip a verdict because
   verdicts depend only on closed-set membership:
     * ``reset`` carrying literal ``--hard`` or ``--merge`` (NOT ``--keep`` —
       it aborts rather than overwrite local changes) → BLOCK.
     * ``clean`` → ALLOW only the exact dry-run shape: every token after the
       subcommand ∈ {-n, --dry-run, -nd, -dn, -d, -x, -X} AND a dry-run token
       present; ANYTHING else (force, paths, exclude flags, unknown flags,
       other clusters) → BLOCK.
       Exotic-but-safe forms over-block by construction — the override is the
       escape, and over-block is the fail direction this boundary wants.
     * ``checkout``/``switch`` carrying literal ``--force``/``--discard-changes``
       or a short cluster containing ``f`` (``-f``, ``-fb`` …) → BLOCK.
2. SNAPSHOT-THEN-ALLOW — every other ``checkout``/``restore``/``switch``
   segment is ALLOWED, but first ``git stash create`` snapshots the worktree +
   index (it mutates NOTHING — no ref, no stash list entry, no tree change) and
   the sha is logged to ``~/.genesis/git_discard_snapshots.jsonl`` with a
   recovery note on stderr. If the command then discards work, recovery is
   ``git stash apply --index <sha>`` (``--index`` restores the staged/
   unstaged split; drop it if the apply conflicts). A MISS (unresolvable cwd, non-repo, git error,
   clean tree) silently allows — degrading exactly to the status quo, never to
   a false block and never to a false guarantee.

Escape hatch: append ``# discard-override`` to the git segment (a deliberate,
auditable "yes, discard it") — it waives the coarse blocks; snapshots still run
(they cost nothing and never block). Fail-OPEN by contract everywhere: an
unparseable command, an unknown subcommand, any subprocess trouble → ALLOW.

DOCUMENTED RESIDUALS (honest claim — the snapshot is a best-effort net):
  * ``git stash create`` excludes UNTRACKED files: a checkout that overwrites an
    untracked path is not covered by the snapshot (the coarse blocks still cover
    forced forms).
  * cwd resolution is deliberately simple (payload cwd + a leading ``git -C``):
    a cd-chain or exotic repo selector (``--git-dir``/``--work-tree``) may
    snapshot the wrong repo or none — a missed snapshot, i.e. the status quo.
  * Snapshot commits are unreferenced; ``git gc`` prunes them after the
    unreachable horizon (default ~2 weeks). Recovery is for "just happened",
    not archaeology.
  * ``assume-unchanged``/``skip-worktree`` modifications are INVISIBLE to
    ``git stash create`` (measured: empty output with a hidden modification
    present) — a checkout over them is allowed and unrecovered.
  * An UNMERGED (conflict) worktree makes ``stash create`` error → silent
    miss for ``checkout --ours/--theirs`` flows.
  * The override sigil binds to the git SEGMENT: on a nested form
    (``bash -c 'git reset --hard' # discard-override``) the outer comment is
    not on the inner segment → still blocks (over-block; put the sigil inside
    the inner script, or run the git command directly).
  * In-project, BOTH the settings.json wiring and the global
    bash_safety_hook delegation run this guard per Bash call → a dirty
    checkout may log two snapshot rows (harmless; the wider dedup is the
    all-hooks-review follow-up).
  * The snapshot is taken BEFORE the whole Bash payload runs (PreToolUse) —
    edits produced by an EARLIER segment of the same compound
    (``gen > f.py && git checkout -- f.py``) post-date it and are not
    captured. Inherent to the hook boundary; split such compounds.
  * Submodule recursion via CONFIG FILE (``submodule.recurse=true`` in
    gitconfig) is invisible to argv: nested-worktree resets it triggers are
    neither blocked nor snapshotted (the explicit ``--recurse-submodules``
    flag — incl. ``=value`` and abbreviated spellings — and the canonical
    separated ``-c submodule.recurse=true`` ARE blocked; glued/exotic ``-c``
    spellings are part of this residual; a superproject ``stash create``
    stores nothing from a dirty submodule — measured).
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
from hook_input import field, read_payload, run_guard  # noqa: E402
from shell_parse import analyze, has_trailing_override  # noqa: E402

_OVERRIDE_SIGIL = "discard-override"
# Substrings that gate the parse path; absent all of them the command cannot be
# a discard-shaped git op, so we return instantly.
_TRIGGER_SUBSTRINGS = ("checkout", "restore", "reset", "clean", "switch")
# Bound the snapshot subprocess so a hung/huge repo can never wedge the tool
# call. 3s (not 5): the USER-LEVEL bash_safety_hook wiring runs under a 5s hook
# budget, and blocks are decided argv-only BEFORE any snapshot — so the only
# thing a timeout can cost is the advisory recovery note, and it must fit the
# tightest wiring's budget (adversarial-review F9).
_GIT_TIMEOUT_S = 3
# Self-trim the snapshot JSONL when it grows past this (no external consumer or
# rotation exists — adversarial-review F8); keep the most recent half.
_SNAPSHOT_LOG_MAX_BYTES = 1_000_000
# The exact-form dry-run whitelist for `git clean` — closed set BY CONSTRUCTION:
# membership here is the ONLY thing that can allow a clean, so requireForce
# configs, -e's value-taking ambiguity, and flags we have never heard of all
# land on BLOCK (over-block + override, never under-block). The two canonical
# preview CLUSTERS (-nd/-dn) are explicit LITERAL members — never decomposed
# (cluster decomposition is flag semantics, the tar pit this design removed);
# any other cluster over-blocks with the override as the escape.
_CLEAN_DRY_RUN_TOKENS = frozenset({"-n", "--dry-run", "-nd", "-dn", "-d", "-x", "-X"})
_CLEAN_DRY_RUN_REQUIRED = frozenset({"-n", "--dry-run", "-nd", "-dn"})
# Literal force tokens for checkout/switch (long forms; short clusters are
# handled structurally by _has_force_cluster).
_CHECKOUT_FORCE_TOKENS = frozenset({"--force", "--discard-changes"})
# Explicit submodule recursion resets NESTED worktrees that the superproject
# snapshot cannot capture (measured: `git stash create` in a superproject with
# a dirty submodule returns NO sha) — so flag-form recursion is a coarse BLOCK
# on checkout/switch/restore (Codex round-2 P1). The CONFIG form
# (submodule.recurse=true) is invisible to argv — documented residual.
_RECURSE_SUBMODULES_FLAG = "--recurse-submodules"


def _snapshot_log_path() -> str:
    """JSONL recovery log. ``GENESIS_DISCARD_SNAPSHOT_LOG`` overrides (test seam
    + config knob); default lives outside any repo so it survives worktree
    removal and is never committed."""
    return os.environ.get("GENESIS_DISCARD_SNAPSHOT_LOG") or os.path.expanduser(
        "~/.genesis/git_discard_snapshots.jsonl"
    )


def _has_force_cluster(argv: list[str]) -> bool:
    """A short-flag cluster containing ``f`` (``-f``, ``-fb``, ``-qf`` …).
    Structural, not semantic: we do not ask what the OTHER letters mean, only
    whether the literal letter ``f`` rides in a short cluster."""
    return any(t.startswith("-") and not t.startswith("--") and "f" in t[1:] for t in argv[1:])


def _tokens_after_subcommand(argv: list[str], sub: str) -> list[str]:
    """Every token after the first literal occurrence of ``sub``. Positional:
    no flag/value modeling (that would be the tar pit) — callers make only
    closed-set membership claims about these tokens. Direction proof: the first
    occurrence is at or before the real subcommand (e.g. ``git -C clean clean``
    anchors on the -C VALUE), so the returned tail is a SUPERSET of the real
    tail — a whitelist check over a superset can only OVER-block (+ override),
    never under-block."""
    try:
        idx = argv.index(sub)
    except ValueError:
        return []
    return argv[idx + 1 :]


def _has_abbrev_of(argv: list[str], long_flags: tuple[str, ...]) -> bool:
    """git parse-options accepts any unambiguous long-flag PREFIX — including
    SINGLE-LETTER ones: measured on git 2.43, ``git reset --h`` performs a full
    hard reset and ``git checkout --f`` force-switches (Codex round-2 P1; the
    earlier ``len >= 4`` floor was empirically wrong). Tokens are normalized at
    ``=`` before matching: optional-arg spellings (``--recurse-submodules=yes``,
    abbreviated ``--recurse=yes``) are longer than the flag, so raw prefix
    matching missed them (adversarial-review round-3 BLOCKER, measured). The
    ``f == h`` arm also makes exact ``=``-forms of every blocked flag match.
    Closed PREFIX-set check, over-block direction: any ``--``-token whose
    pre-``=`` head is length >= 3 (anything beyond the bare ``--`` separator)
    and prefixes a blocked long flag counts. Ambiguous prefixes and no-arg
    flags given values (``--force=true``) over-block — git errors on those
    itself, so nothing legitimate is lost."""
    return any(
        (h := t.split("=", 1)[0]).startswith("--")
        and len(h) >= 3
        and any(f == h or f.startswith(h) for f in long_flags)
        for t in argv[1:]
    )


def _coarse_violations(cmd: str) -> list[str]:
    """The closed-set token blocks. PURE ARGV — no subprocess, no repo state, no
    operand semantics — so the verdict is deterministic and cannot be starved by
    a slow probe under the hook's wall-clock budget.

    Block classes gate on LITERAL VERB MEMBERSHIP in the segment argv, NOT on
    positional subcommand resolution: resolution depends on skipping git's
    value-taking global flags — an OPEN set (adversarial-review F1, executed:
    ``git --attr-source HEAD reset --hard`` shifted the resolved subcommand to
    the flag's value and flipped the verdict to ALLOW). ``"reset" in argv`` has
    no such dependency; a pathological ``git checkout reset --hard`` over-blocks
    with the override as the escape — the direction this boundary wants."""
    reasons: list[str] = []
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        argv_set = set(seg.argv[1:])
        if not argv_set & {"reset", "clean", "checkout", "switch", "restore"}:
            continue  # restore participates only in the recurse-submodules block
        if has_trailing_override(seg.raw, _OVERRIDE_SIGIL):
            continue
        if "reset" in argv_set and (
            {"--hard", "--merge"} & argv_set or _has_abbrev_of(seg.argv, ("--hard", "--merge"))
        ):
            reasons.append(
                "`git reset --hard/--merge` overwrites uncommitted work with no "
                "reflog for unstaged changes. Stash or commit first, or append "
                "`# discard-override` if you truly mean it. (`--keep` aborts on "
                "local changes and is allowed.)"
            )
        if "clean" in argv_set:
            tail = _tokens_after_subcommand(seg.argv, "clean")
            is_exact_dry_run = (
                bool(set(tail) & _CLEAN_DRY_RUN_REQUIRED) and set(tail) <= _CLEAN_DRY_RUN_TOKENS
            )
            if not is_exact_dry_run:
                reasons.append(
                    "`git clean` permanently deletes files. Only the exact dry-run "
                    "form is allowed (-n/-nd/--dry-run with -d/-x/-X); anything "
                    "else — including path arguments — needs `# discard-override`. "
                    "Preview with `git clean -nd` first."
                )
        if argv_set & {"checkout", "switch", "restore"} and (
            _RECURSE_SUBMODULES_FLAG in argv_set
            or _has_abbrev_of(seg.argv, (_RECURSE_SUBMODULES_FLAG,))
            # canonical -c spelling puts the config form IN argv — cheap
            # closed-set literal; exotic glued spellings stay a residual
            or "submodule.recurse=true" in argv_set
        ):
            reasons.append(
                "`--recurse-submodules` resets NESTED submodule worktrees that "
                "the recovery snapshot cannot capture (a superproject `git stash "
                "create` stores nothing from a dirty submodule). Commit/stash "
                "inside the submodule first, or append `# discard-override`."
            )
        # Per-verb force tuples (adversarial-review F5): --discard-changes is
        # SWITCH-only — including it in checkout's tuple made `git checkout
        # --d` (a unique abbreviation of harmless --detach) over-block. On
        # switch, --d is genuinely ambiguous (--detach/--discard-changes), so
        # blocking there is free.
        checkout_force = "checkout" in argv_set and (
            "--force" in argv_set
            or _has_force_cluster(seg.argv)
            or _has_abbrev_of(seg.argv, ("--force",))
        )
        switch_force = "switch" in argv_set and (
            _CHECKOUT_FORCE_TOKENS & argv_set
            or _has_force_cluster(seg.argv)
            or _has_abbrev_of(seg.argv, tuple(_CHECKOUT_FORCE_TOKENS))
        )
        if checkout_force or switch_force:
            reasons.append(
                "`git checkout/switch` with force (`-f`/`--force`/"
                "`--discard-changes`) throws away local changes. Stash or commit "
                "first, or append `# discard-override` if intended."
            )
    return reasons


def _segment_cwd(seg, payload: dict) -> str | None:
    """DELIBERATELY simple cwd model for the best-effort snapshot: the payload
    cwd, adjusted by a leading ``git -C <dir>`` on this segment. No cd-chain
    walking, no --git-dir modeling — a shape this misses yields a missed
    snapshot (status quo), never a block, so the model does not need to be
    complete (see DOCUMENTED RESIDUALS)."""
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
    Returns the snapshot sha, or None (clean tree / non-repo / any error) —
    every failure is a silent allow by contract."""
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


def _record_snapshots(cmd: str, payload: dict) -> None:
    """Best-effort recovery net for allowed checkout/restore/switch segments.
    NEVER blocks, never raises past its own boundary; one snapshot per distinct
    resolved cwd (two segments in the same repo share one snapshot)."""
    seen_cwds: set[str] = set()
    for seg in analyze(cmd):
        if seg.exe != "git":
            continue
        # Literal verb membership, mirroring _coarse_violations (F1: positional
        # resolution is an open set). "reset" is included (F3): an OVERRIDDEN
        # `git reset --hard # discard-override` — the sanctioned discard path —
        # is exactly where a recovery sha is most valuable, and stash create
        # captures precisely what reset --hard destroys. Over-matching (a
        # pathological argv containing these verbs) costs one harmless
        # snapshot. "clean" is excluded: stash create never captures untracked
        # files, which is all clean deletes.
        if not set(seg.argv[1:]) & {"checkout", "restore", "switch", "reset"}:
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
            "cmd": cmd[:500],
            "sha": sha,
        }
        try:
            log_path = _snapshot_log_path()
            # A relative override has an empty dirname — makedirs("") raises
            # and would silently drop the row (Codex round-2 P2).
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            # Append + trim under an exclusive flock on a SIDECAR lock file:
            # locking the log fh itself is defeated by the trim's os.replace
            # (the lock identity is the OLD inode — a waiter would acquire the
            # orphaned inode and append invisibly; demonstrated in review).
            # The sidecar is never replaced, so every writer serializes on the
            # same inode. Trim rewrites via temp + os.replace (atomic).
            with open(log_path + ".lock", "a", encoding="utf-8") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                with open(log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
                if os.path.getsize(log_path) > _SNAPSHOT_LOG_MAX_BYTES:
                    with open(log_path, encoding="utf-8") as rd:
                        lines = rd.readlines()
                    tmp = log_path + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as wr:
                        wr.writelines(lines[len(lines) // 2 :])
                    os.replace(tmp, log_path)
                # lock releases on close
        except OSError:
            pass  # the stderr note below still carries the recovery sha
        print(
            f"[git-discard-guard] uncommitted state in {cwd} snapshotted as "
            f"{sha[:12]} — if this command discarded work you needed: "
            f"git stash apply --index {sha}  (--index restores the staged/"
            f"unstaged split; drop it if the apply conflicts. "
            f"log: {_snapshot_log_path()})",
            file=sys.stderr,
        )


def _violations(cmd: str) -> list[str]:
    """Test-facing view of the coarse blocks (snapshots are separate on purpose:
    they never contribute to a verdict)."""
    return _coarse_violations(cmd)


def main() -> int:
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd or "git" not in cmd:
            return 0
        if not any(s in cmd for s in _TRIGGER_SUBSTRINGS):
            return 0

        # NO blanket except around the verdict (adversarial-review F4): a crash
        # in analyze/_coarse_violations propagates to run_guard, which converts
        # it to a VISIBLE fail-closed block — aligning all three artifacts (this
        # guard, hook_input's fail-closed wiring, bash_safety_hook's
        # "degraded, NEVER open" delegation contract, whose legacy fallback
        # engages on rc∉{0,2}). Only discard-shaped commands reach here (the
        # pre-filters above), so a parser crash can never block unrelated work.
        reasons = _coarse_violations(cmd)
        if reasons:
            for r in reasons:
                print(f"BLOCKED: {r}", file=sys.stderr)
            print(
                "This git command discards work with no recovery path. Stage "
                "(`git add`), stash, or commit first. If you truly mean to "
                "discard, append `# discard-override` to the git segment.",
                file=sys.stderr,
            )
            return 2
        # Allowed path: best-effort snapshot so a checkout/restore/switch that
        # DOES overwrite uncommitted work leaves a recovery sha behind. The
        # net is ADVISORY — its own crash must never block an allowed command.
        with contextlib.suppress(Exception):
            _record_snapshots(cmd, payload)
    except (json.JSONDecodeError, KeyError):
        pass  # Malformed payload → fail-open (cannot even tell what would run)
    return 0


if __name__ == "__main__":
    run_guard(main, "git_discard_guard")
