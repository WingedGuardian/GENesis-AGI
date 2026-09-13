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
continuations, global value-flags): every review round surfaces the next
spelling, and a block is
inherently a completeness claim, so every gap is a silent-loss hole. For the
RECOVERABLE verbs (checkout/restore/switch/reset) that job is therefore NOT done
here — a snapshot makes classification unnecessary. It survives only as the
crude, dependency-free substring blocks in ``.claude/settings.json`` and
``scripts/bash_safety_hook.sh`` (``git reset --hard``), an honest best-effort
SPEED-BUMP, not a security boundary.

``git clean`` is the EXCEPTION and the primary place this hook still BLOCKS
(exit 2): ``git stash create`` cannot capture untracked files — exactly what
``clean`` deletes — so the snapshot net gives clean ZERO protection. The block is
a CLOSED SET on the SAFE side (allow only the exact dry-run forms; block the open
complement), which a false-ALLOW cannot penetrate — see ``_clean_violation``.
The ``# discard-override`` sigil is its escape.

A submodule-RECURSIVE checkout/restore/switch/reset/read-tree (``--recurse-submodules``
or a truthy ``-c submodule.recurse``) is the SECOND block, for the same reason: a
superproject ``git stash create`` does not capture submodule worktrees, so
recursing into them is unrecoverable and a superproject snapshot there is a FALSE
recovery promise. Same ``# discard-override`` escape — see
``_submodule_recurse_violation``. NOTE this is NOT the same guarantee as clean's
block: clean is closed on the SAFE side (a false-ALLOW is impossible); this block
is closed only on the BLOCK side and still leaks a false-recovery-promise on the
safe side for recursion enabled by PERSISTENT config (no CLI token — a documented,
subprocess-free residual), so the snapshot note is hedged for recurse-capable verbs.

The SECURITY property this hook provides is RECOVERABILITY. For every
tracked-work-overwriting verb — ``checkout`` / ``restore`` / ``switch`` /
``reset``, plus ``rm`` / ``mv`` and the plumbing ``checkout-index`` /
``read-tree`` (which delete or rewrite tracked files from the worktree) — it runs
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
exit is the ``git clean`` block above, and it fails CLOSED: if the precise parse
raises on a command that mentions ``clean``, ``main`` blocks UNCONDITIONALLY and
asks the user to simplify (no bespoke coarse re-parse — that hand-rolled floor is
the exact trap that drew a review CRITICAL), so a parser bug can never silently
ALLOW a destructive clean — critically on the DIRECT ``settings.json`` wiring,
which has no shell floor behind it. The snapshot recovery note is
delivered to the model via ``hookSpecificOutput.additionalContext`` (stdout),
because Claude Code discards an exit-0 hook's stderr.

DOCUMENTED RESIDUALS (honest claim — the snapshot is a best-effort net):
  * ``git stash create`` excludes UNTRACKED files: a ``clean`` deletion or a
    checkout-from-tree onto an untracked path is not recovered here. (Full
    coverage — an "uncommitted reflog" that captures untracked state — is the
    tabled north-star follow-up.)
  * ``assume-unchanged`` / ``skip-worktree`` modifications and an UNMERGED
    (conflict) worktree are invisible to / error out of ``stash create``.
  * Submodule contents are not captured by a superproject ``stash create`` — so a
    submodule-RECURSIVE checkout/restore/switch/reset/read-tree requested ON THE
    COMMAND LINE (``--recurse-submodules`` / ``-c submodule.recurse=true``) is
    BLOCKED (not snapshotted; see ``_submodule_recurse_violation``) rather than given
    a false recovery promise. TWO residuals remain, both requiring a subprocess this
    argv-only block avoids: (a) recursion enabled by PERSISTENT config
    (``.git/config`` / global / ``GIT_CONFIG_*``, no CLI token) is not detected and
    still recurses+overwrites — NOT blocked; and (b) a NON-recursive verb touching a
    submodule path is only superproject-snapshotted. For both, the snapshot note is
    HEDGED (it states submodule worktrees are not captured) so the promise is honest.
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
import hashlib
import json
import os
import subprocess
import sys
import time

# Self-locate so sibling hook modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# SOFT dependency: this guard BLOCKS `git clean` (exit 2), and a module-load
# exception exits 1, which the harness treats as non-blocking — so an unimportable
# brand-new logging module would let an unrecoverable `git clean -fdx` through.
# Snapshot logging degrades to a no-op instead. Mirrors git_push_guard's guard on
# the same import, for the same reason.
try:
    import audit_jsonl  # noqa: E402
except Exception:  # noqa: BLE001 — logging must never disarm the clean block.
    audit_jsonl = None

from hook_input import field, read_payload  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze_checked,
    git_subcommand_index,
    has_trailing_override,
)

try:  # noqa: E402
    import discarded_write
except Exception:  # noqa: BLE001 — GUARDED: an unguarded import failure would abort
    # module load → exit 1 → CC reads non-2 as NON-blocking → the clean/checkout RUNS.
    discarded_write = None  # type: ignore[assignment]

try:  # noqa: E402
    import hook_output
except Exception:  # noqa: BLE001 — GUARDED for the same reason as the two above: the
    # advisory channel must never be able to disarm the clean BLOCK. Absent, the notes
    # are emitted unbounded — the pre-change behaviour, which is a lost advisory at
    # worst, never a permitted `git clean`.
    hook_output = None  # type: ignore[assignment]

#: The chokepoint, parsed ONCE per process. `main` reaches three consumers that each
#: analyse the SAME command string (_clean_violation, _submodule_recurse_violation,
#: _record_snapshots), and the parse is a pure function of that string — so three
#: identical parses were pure cost on a clock this guard SHARES.
#:
#: That clock is the reason this exists rather than being a tidiness nicety.
#: `bash_safety_hook.sh` is registered at 5s and delegates to THREE guards over the
#: same command, this one included, so those duplicates were 3 of the 5 full parses on
#: one budget. MEASURED end to end, worst payload inside both bounds: 6.12s BEFORE —
#: over the registration, i.e. the hook is killed and the command is PERMITTED — and
#: comfortably inside the budget after. The after-figure is the depth-5 row of the cost
#: table in `shell_parse.MAX_COMMAND_CHARS` and is deliberately NOT repeated here: this
#: line carried 2.92s while that table said 2.67s and a test docstring said 3.19s, and
#: the figure is load-dependent, so a copy is a copy that drifts. A copy that points at
#: its source is still a copy.
#:
#: Do not re-pair those two numbers into a speedup ratio, either. 6.12s is this guard's
#: own before/after-memoisation measurement; the depth-5 row comes from a sweep taken
#: to CHOOSE the bound. Nothing on record establishes they are the same run, and an
#: earlier revision of this comment asserted they were. Bounding the input was not
#: enough on its own; the work per command had to stop being done three times, and that
#: claim rests on the 6.12s pair alone.
_PARSE_MEMO: dict[str, tuple] = {}


def _parse_once(cmd: str) -> tuple:
    """`analyze_checked`, memoised for the lifetime of this hook process."""
    if cmd not in _PARSE_MEMO:
        _PARSE_MEMO[cmd] = analyze_checked(cmd)
    return _PARSE_MEMO[cmd]


# Substrings that gate the parse path; absent all of them the command cannot be
# a worktree-overwriting git op, so we return instantly. `clean` is included
# because it is the ONE verb this guard still BLOCKS (see _clean_violation). The
# short `rm`/`mv` substrings widen this gate only for git-CONTAINING commands
# (main() already returns on `"git" not in cmd`), so the extra analyze() cost is
# bounded to git usage; correctness never rests on the substring — the exact VERB
# token is re-checked in _record_snapshots. `checkout-index` rides `checkout`.
_TRIGGER_SUBSTRINGS = (
    "checkout",
    "restore",
    "reset",
    "switch",
    "clean",
    "rm",
    "mv",
    "read-tree",
)
# Verbs whose worktree effect `git stash create` can (best-effort) recover — the
# CLASS of tracked-work-overwriting/deleting operations, not just the original
# four: `rm` (deletes tracked files from the worktree), `mv` (overwrites the
# destination), and the plumbing `checkout-index`/`read-tree -u` (rewrite the
# worktree from the index/a tree). The snapshot captures the pre-command TRACKED
# state (recover with `git checkout <sha> -- <path>` or `git stash apply --index`).
# Same untracked-files residual as elsewhere: `git mv -f tracked dest` where DEST
# was an UNTRACKED file destroys that dest unrecoverably (stash create excludes
# untracked) — the documented residual, not closed here. `clean` is deliberately
# EXCLUDED (all-untracked → zero snapshot value — it stays the one BLOCK).
_SNAPSHOT_VERBS = frozenset(
    {"checkout", "restore", "switch", "reset", "rm", "mv", "checkout-index", "read-tree"}
)
# The sanctioned escape for the clean block: `git clean -f  # discard-override`.
_OVERRIDE_SIGIL = "discard-override"
# Verbs that can recurse into submodule worktrees via `--recurse-submodules` or a
# truthy `-c submodule.recurse`. A superproject `git stash create` does NOT capture
# submodule worktrees, so a submodule-recursive restore/checkout/switch/reset is
# UNRECOVERABLE by the snapshot — the same condition as clean — and a superproject
# snapshot there would be a FALSE recovery promise. So it BLOCKS (honoring the
# override), rather than snapshot-and-allow. (git reset has no --recurse-submodules
# flag, but submodule.recurse config still recurses its --hard, so it is included;
# read-tree likewise supports --[no-]recurse-submodules and is a snapshot verb.)
_SUBMODULE_RECURSE_VERBS = frozenset({"checkout", "restore", "switch", "reset", "read-tree"})
# git-config truthiness: a `submodule.recurse` set to one of these is OFF (no
# recursion → the superproject snapshot suffices → not blocked). Bare
# `submodule.recurse` (no value) and any other value are treated as ON.
_SUBMODULE_RECURSE_FALSE = frozenset({"false", "0", "no", "off"})
# ── whole-tree rewind: a LOUD NOTE, deliberately NOT a block ──────────────────
# `git checkout <commit> -- .` is not "discard my edits" — the broad pathspec
# matches every tracked path, so it rewrites the WHOLE worktree to that commit
# and silently reverts anything merged since. MEASURED 2026-08-31: one such
# command reverted two already-merged PRs; the first was found by luck and the
# second only by a second, different check.
#
# WHY THIS IS A NOTE AND NOT A BLOCK, since every other guard here that fires is
# a block. The admission test for a block in this module is UNRECOVERABILITY —
# `clean` (stash cannot capture untracked files) and submodule-recursion (stash
# cannot capture submodule worktrees). A tree rewind fails that test: `checkout`
# is in _SNAPSHOT_VERBS, so `git stash create` runs first and the pre-command
# worktree is recoverable. MEASURED on the real hook: exit 0, snapshot taken,
# recovery sha in the note.
#
# So the thing that failed in 2026-08-31 was not recovery, it was NOTICING. The
# snapshot note fires identically for every checkout/restore/switch/reset — it is
# routine furniture, and it never said "you just rewound the tree". This makes
# the note SPECIFIC for this shape while leaving the exit code alone, which keeps
# the module's stated contract intact ("for the recoverable verbs this hook is
# advisory and NEVER exits non-zero") and costs no completeness claim: a spelling
# this misses degrades to the ordinary snapshot note, not to a hole.
#
# THE TRIPWIRE (owner decision 2026-09-06): a match writes `tree_rewind: true`
# onto the row for the repo that was rewound, so recurrence is COUNTABLE rather
# than argued. If a rewind reverts merged work again despite this note, that is
# the measured evidence to escalate to a block; without it the escalation would
# rest on the same n=1 the first attempt did.
#
# COUNT ATTEMPTS, NOT ROWS — and note they are ATTEMPTS: this is a PreToolUse
# hook, so a row precedes execution and a short-circuited or rejected command still
# writes one (MEASURED). The count is an UPPER BOUND on rewinds that happened.
#
# This hook is wired twice (the .claude/settings.json
# Bash matcher AND bash_safety_hook.sh's advisory invocation), so one rewind
# normally writes TWO identical-cwd rows. A naive `grep -c` therefore roughly
# doubles the count, which pushes in the wrong direction for a decision about
# whether to add a block. Count the BROAD ones — a directory-scoped checkout is
# recorded too (same verb class, worth seeing) and is not the incident:
#   cat "${GENESIS_DISCARD_SNAPSHOT_DIR:-$HOME/.genesis/git_discard_snapshots}"/*.jsonl \
#     | jq -r 'select(.tree_rewind and .broad) | .event' | sort -u | wc -l
# Dedup on `event`, not ts+cwd: whole-second timestamps collapse two
# different-source rewinds of one repo in the same second, and split one
# command counted by both wirings across a second boundary — measured wrong in
# both directions. `event` digests (source, cwd, command), so the two wirings
# agree and distinct in-payload events differ.
# The store is a DIRECTORY of one file per snapshot, not a single file. This recipe
# named `~/.genesis/git_discard_snapshots.jsonl` until adversarial review measured
# it: that path is the pre-existing legacy file which `_snapshot_dir` deliberately
# does NOT migrate, so the documented command returned 7 (all of them throwaway
# probe rows) while the store the guard actually writes returned 0. Nothing
# conflicted during the merge that moved the store, because the resolver and this
# comment sit in different places — and the count is the entire stated basis for
# escalating to a block. `// "unknown"` because `.cwd` can be JSON null, which makes
# a bare `+` error out rather than undercount.
# Also note the count is only meaningful for rows dated after this shipped —
# building it generated probe rows against throwaway repos under ~/tmp.
#
# `switch` is carried for symmetry with _SUBMODULE_RECURSE_VERBS. Precisely: GIT
# rejects this shape — VERIFIED on 2.43, `git switch <ref> -- .` exits "fatal:
# only one reference expected", because switch takes no pathspec — but the
# PREDICATE here still matches it, so the note is emitted for a command that will
# then fail. Cosmetic (the note is advisory and the command never runs), and
# listed so a future git that grows a pathspec is covered. Do not read this as
# "the predicate cannot fire on switch": it can.
# `reset` is DELIBERATELY absent, stated because every other inclusion here is
# justified and a reader cannot otherwise tell a decision from an oversight (it IS
# in `_SUBMODULE_RECURSE_VERBS`, which is a different question). The defect this
# note exists for is a revert that lands INSIDE the author's own diff and so reads
# as deliberate. `git reset --hard <commit>` moves HEAD, so the difference shows up
# as the branch pointer rather than as working-tree changes attributed to the
# author; and `git reset <commit> -- .` rewrites the INDEX only, leaving the
# worktree alone. Neither produces the invisible-in-review shape. Both are still
# snapshotted by the net — they are in `_SNAPSHOT_VERBS`.
_TREE_REWIND_VERBS = frozenset({"checkout", "restore", "switch", "read-tree"})
# A source that is NOT these is a rewind to some OTHER commit. `HEAD`/`@` mean
# "discard my uncommitted edits back to where I already am", which reverts no
# merged work and is exactly what the snapshot net exists for.
_TREE_REWIND_HEAD_ALIASES = frozenset({"HEAD", "@"})
# LITERAL broad-pathspec tokens — never a filesystem test (this stays argv-only,
# subprocess-free, like every other predicate here). A directory operand is
# matched by the trailing-slash rule below rather than by stat'ing it.
# `-A`/`--all` were here initially and are removed: none of these verbs accepts
# them, so they only ever produced a warning for a command git itself rejects.
_TREE_REWIND_BROAD_PATHSPECS = frozenset({".", "./", ":/", ":/.", "*"})
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
# Bound a SINGLE snapshot subprocess so a hung/huge repo can never wedge the tool
# call (blocks live in the shell layer, so the worst a timeout costs is the
# advisory recovery note). Fits the tightest 5s user-level hook budget.
_GIT_TIMEOUT_S = 3
# WHOLE-PAYLOAD ceiling across ALL repos a compound touches — a per-repo timeout
# alone lets N slow repos spend N × _GIT_TIMEOUT_S and blow the 10s hook cap in
# .claude/settings.json (Codex round-5 P2). Each snapshot gets min(_GIT_TIMEOUT_S,
# remaining); once the budget is spent, later repos are skipped and the skip is
# SURFACED in a recovery note (never a silent cap). < the 10s hook cap, with
# margin for launcher/parse.
_TOTAL_SNAPSHOT_BUDGET_S = 8.0
# Retention is a size bound over the whole store, applied by
# scripts/prune_hook_audit_logs.py on the daily disk_hygiene.sh timer. It is NOT
# done here: self-trimming on the hook path meant a guard about to refuse a
# destructive command was also rewriting a file, and that retention engine was the
# source of most of the shared writer's defects.
# Own-user-only modes for stores that sit in ~/.genesis beside secrets live in
# audit_jsonl (LOG_DIR_MODE / LOG_FILE_MODE), shared with the merge-gate override
# store so the two cannot diverge.


def _snapshot_dir() -> str:
    """Directory of recovery records, one file per snapshot.
    ``GENESIS_DISCARD_SNAPSHOT_DIR`` overrides (test seam + config knob); default
    lives outside any repo so it survives worktree removal and is never committed.

    NOT BACKED UP, deliberately — recorded here because the omission looks like an
    oversight and is not. Each row's recovery payload is a ``git stash create``
    sha, and that object lives ONLY in the local repo's object store: unreachable
    objects are never pushed, git prunes them on its own schedule (default two
    weeks), and ``scripts/backup.sh`` captures no object store. Copying the JSONL
    to a backup would therefore restore a list of pointers to nothing — a log that
    LOOKS recoverable while every recovery fails, which is worse than none.
    At a live install, of 76 recorded shas one was already unresolvable in the repo
    that wrote it. Making this store genuinely restorable means backing up the
    referenced objects, not the files; the merge-gate override store, whose rows are
    self-contained, IS backed up (``backup.sh`` §6d).

    The pre-existing single file ``~/.genesis/git_discard_snapshots.jsonl`` is left
    where it is rather than migrated. Its rows point at unreachable stash objects git
    prunes on its own schedule, so it self-obsoletes within weeks; moving it would
    carry pointers that are about to stop resolving anyway."""
    # The knob this REPLACES is not silently ignored. `GENESIS_DISCARD_SNAPSHOT_LOG`
    # named a FILE and is documented in the code it superseded as a config knob, so
    # an install that set it would otherwise keep writing to the new default while
    # its operator tooling read the old path — snapshots appearing to have stopped,
    # with nothing said (Codex P2, PR #1609). It is NOT auto-translated: a file path
    # does not carry a correct directory, and inventing one would put recovery
    # records somewhere the operator did not choose. Say it, and let them decide.
    _legacy = os.environ.get("GENESIS_DISCARD_SNAPSHOT_LOG")
    if _legacy:
        with contextlib.suppress(Exception):
            print(
                "[audit-log] GENESIS_DISCARD_SNAPSHOT_LOG is no longer read (the store "
                "is now a DIRECTORY of one file per snapshot) — set "
                "GENESIS_DISCARD_SNAPSHOT_DIR to an absolute directory instead; "
                "records are being written to the default until you do",
                file=sys.stderr,
            )
    # ONE resolver, shared with the other guard and the pruner. This rule
    # was written out three times and the pruner's copy omitted the
    # absolute-path refusal, so it trimmed an unrelated directory while the
    # real store grew unbounded (Codex P2, PR #1609). See
    # audit_jsonl.resolve_store_dir.
    from audit_jsonl import resolve_store_dir

    return resolve_store_dir("GENESIS_DISCARD_SNAPSHOT_DIR")


def _segment_cwd(seg, payload: dict) -> str | None:
    """DELIBERATELY simple cwd model for the best-effort snapshot: the payload
    cwd, adjusted by the leading ``git -C <dir>`` options on this segment. No
    cd-chain walking, no --git-dir modeling — a shape this misses yields a missed
    snapshot (status quo), never a block (see DOCUMENTED RESIDUALS).

    ALL leading ``-C`` operands are folded, composing left to right, because git
    documents each subsequent relative ``-C`` as interpreted relative to the one
    before it. The first version returned at the FIRST ``-C``, which was harmless
    while the note carried no repo-specific sha — but once each rewind note began
    advertising ITS OWN repo's snapshot, ``git -C outer -C inner checkout <c> -- .``
    rewound outer/inner while the note offered OUTER's sha: a recovery command
    that does not even resolve where the reader would run it, which is the exact
    false-promise class the per-repo association was built to end (Codex P2,
    round 2). MEASURED before: ``/base/outer``; after: ``/base/outer/inner``."""
    base = payload.get("cwd") or os.getcwd()
    argv = seg.argv
    resolved = base
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-C" and i + 1 < len(argv):
            resolved = os.path.normpath(os.path.join(resolved, argv[i + 1]))
            i += 2
            continue
        if not tok.startswith("-"):
            break  # subcommand reached — no more leading -C
        i += 1
    return resolved


def _snapshot_worktree(cwd: str, timeout: float = _GIT_TIMEOUT_S) -> str | None:
    """``git stash create`` — capture worktree+index WITHOUT mutating anything.
    Returns the snapshot sha, or None (clean tree / non-repo / any error).
    ``timeout`` is clamped by the caller to the remaining whole-payload budget."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "stash", "create", "git-discard-guard snapshot"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _write_log_row(row: dict) -> str | None:
    """Write ``row`` as one new file in the recovery store.

    The own-user create modes and the create-or-fail naming live in ``audit_jsonl``
    — extracted from HERE when the merge gate's override store needed the same
    properties, so the two cannot drift apart. Returns the file path, or None if the
    write failed (reported on stderr, never raised: a logging failure must not break
    the user's command) or the writer is unavailable (see the guarded import).

    Size retention belongs to the daily timer, not to this call. A guard about to
    refuse a destructive command has no business also rewriting a file.
    """
    if audit_jsonl is None:
        # SAY SO. `_record_snapshots` tells the operator the snapshot was "NOT
        # logged — see the [audit-log] line on stderr for why", and on this path no
        # such line existed: the message pointed at evidence that was never emitted
        # (Codex P2, PR #1609). The sibling in git_push_guard._flush_overrides
        # already reports the same condition; this is the second half of that rule.
        # Cannot use audit_jsonl.warn — that is the module we do not have. Suppressed
        # for the same reason it is there: an unwritable stderr must not raise out of
        # a best-effort logging call on a guard's verdict path.
        with contextlib.suppress(Exception):
            print(
                "[audit-log] audit_jsonl unavailable — snapshot record NOT written",
                file=sys.stderr,
            )
        return None
    return audit_jsonl.write_batch(_snapshot_dir(), [row])


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


_CLEAN_BLOCK_MSG = (
    "[git-discard-guard] BLOCKED: `git clean` permanently deletes "
    "untracked files, which the recovery snapshot CANNOT restore "
    "(`git stash create` excludes untracked). Only the exact dry-run "
    "form is allowed (-n / --dry-run, optionally with -d/-x/-X); "
    "anything else — force flags, path arguments, exclude patterns — "
    "needs `# discard-override`. Preview first with `git clean -nd`."
)


# Shown when the precise parser CRASHES on a command that mentions `clean`. We
# deliberately do NOT re-implement a coarse clean detector on the crash path —
# a bespoke dependency-free floor is a hand-rolled shell parser inside a security
# gate (the exact trap that drew a review CRITICAL + a deadlock), with an
# unbounded finding tail. Instead the crash fails CLOSED unconditionally and asks
# the user to simplify so the precise, override/dry-run-aware parser can run.
_CLEAN_PARSE_FAILED_MSG = (
    "[git-discard-guard] BLOCKED: could not safely parse this command, and it "
    "mentions `clean`. `git clean` permanently deletes untracked files that no "
    "snapshot can recover, so an UNPARSEABLE clean fails CLOSED. This is almost "
    "always a pathological command shape (e.g. deeply nested `$(...)`). Run the "
    "`git clean` on its OWN line and the guard will parse it precisely — dry-run "
    "forms are allowed, and `# discard-override` works on the parsed path."
)


def _clean_violation(cmd: str) -> str | None:
    """The ONE block this otherwise snapshot-only guard still makes: a
    non-dry-run ``git clean``. Returns a block message if any ``git`` segment
    runs ``clean`` in a form outside the exact dry-run whitelist (and lacks a
    ``# discard-override``), else None. Pure argv — no subprocess, no repo
    state — so it cannot be starved by a slow probe. Literal-membership verb
    detection (``"clean" in argv``) NOT positional resolution: resolution
    depends on skipping git's OPEN set of value-taking global flags, so a
    pathological ``git checkout clean`` merely over-blocks (override escape) —
    the direction this boundary wants.

    A parse cut short by one of shell_parse's BOUNDS lands on the same fail-closed
    message as a parser crash, because it is the same situation: this guard cannot
    prove a clean-mentioning command safe. It matters that the parser says so rather
    than raising — a bound does not crash, it silently returns fewer segments, and
    reading that as "no clean here" is a silent allow of the one unrecoverable
    operation this guard blocks. MEASURED before this call was switched: a
    `git clean -fd` nested 9 deep went from refused to allowed.

    `untokenizable` is deliberately EXCLUDED. It predates the bounds and this guard
    already allowed those commands, so failing closed on it would be a new
    over-block rather than a restoration — MEASURED at 209 of 1,367 real
    clean-mentioning commands, against 0 for the bounds. Widening to it is a
    separate decision with its own evidence, not a rider on this one.

    BOTH bounds refuse here. An earlier revision honoured it and softened the length axis, on the
    written grounds that "`bash_safety_hook.sh` keeps the real coverage there: its
    `git clean` check greps RAW text per shell segment". THAT WAS FALSE, and measured
    so: the coarse fallback at bash_safety_hook.sh:220 runs only when `_handled == 0`,
    i.e. when python3 or this guard is ABSENT or this guard CRASHED. Exiting 0 — which
    is exactly what softening produced — sets `_handled=1` and SKIPS the fallback.
    MEASURED on `echo "<49,200 chars>" && git clean -fd`: guard rc=0, hook rc=0, so
    nothing anywhere blocked a real, executing `git clean -fd`.

    That is the sibling-layer trap in its purest form: a fail-open justified by a
    second layer that does not actually cover the case, asserted in a comment rather
    than checked. The rule this leaves behind: a guard whose only verdicts are BLOCK
    and ALLOW must fail closed on ANY blindness, because "ask" is not available to it
    and the alternative to blocking is permitting. The per-axis severity flag that
    made this mistake possible has since been DELETED outright — see BlindSpot.

    Cost of refusing both: 0 of 45,956 real commands reach either bound."""
    segs, blind = _parse_once(cmd)
    if blind is not None and blind.bounds_induced:
        return _CLEAN_PARSE_FAILED_MSG
    for seg in segs:
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
            return _CLEAN_BLOCK_MSG
    return None


_SUBMODULE_BLOCK_MSG = (
    "[git-discard-guard] BLOCKED: a submodule-RECURSIVE checkout/restore/switch/"
    "reset (`--recurse-submodules` or `-c submodule.recurse=true`) resets nested "
    "submodule worktrees, and `git stash create` does NOT capture submodule "
    "contents — so the recovery snapshot gives them ZERO protection (an edit inside "
    "a dirty submodule would be lost unrecoverably). Commit/stash the submodule "
    "first, drop `--recurse-submodules`, or append `# discard-override` to proceed."
)


_SUBMODULE_PARSE_FAILED_MSG = (
    "[git-discard-guard] BLOCKED: this command could not be parsed within the "
    "parser's bounds AND it mentions submodule recursion, which `git stash create` "
    "cannot capture — so the guard cannot prove it is recoverable and refuses. "
    "`# discard-override` is NOT honoured here: the override is read per-segment on "
    "the parsed path, which is the path that just failed. Run the git command on its "
    "OWN line (shorter / less deeply nested) and the override will be honoured."
)


def _argv_recurses_submodules(argv: list[str]) -> bool:
    """True if this git argv enables submodule recursion — via the
    ``--recurse-submodules`` flag (bare or ``=value``) or a truthy
    ``-c submodule.recurse`` config. Value-range aware: bare forms are ON (git's
    default is true when bare); an explicit false/0/no/off value is OFF — for BOTH
    the flag ``=value`` and the config ``=value`` (they must agree, else
    ``--recurse-submodules=no`` false-blocks a safe command). The CLI flag is matched
    case-sensitively (git flags are); the ``-c`` config KEY is matched
    case-INSENSITIVELY (git config keys are — ``-c Submodule.Recurse=true`` recurses).
    ``--no-recurse-submodules`` is correctly not matched.

    RESIDUALS (documented, the guard's stance on the argv tar pit): an ABBREVIATED
    flag spelling (``--recurse-sub``) is not matched; and recursion enabled by
    PERSISTENT config (``.git/config`` / global / ``GIT_CONFIG_*``, no CLI token) is
    invisible to this pure-argv check — that case is NOT blocked and still gets a
    superproject-only snapshot (see the DOCUMENTED RESIDUALS + the hedged snapshot
    note). Catching it would need a subprocess, which this block avoids by design."""
    for tok in argv[1:]:
        # CLI flag — git flags are case-SENSITIVE; the =value form honors the false-set.
        if tok == "--recurse-submodules":
            return True
        if tok.startswith("--recurse-submodules="):
            if tok.split("=", 1)[1].strip().lower() not in _SUBMODULE_RECURSE_FALSE:
                return True
            continue  # explicit OFF value → not recursion
        # -c config KEY — git config keys are case-INSENSITIVE.
        low = tok.lower()
        if low == "submodule.recurse":
            return True  # bare `-c submodule.recurse` → git treats as true
        if low.startswith("submodule.recurse=") and (
            low.split("=", 1)[1].strip() not in _SUBMODULE_RECURSE_FALSE
        ):
            return True
    return False


def _submodule_recurse_violation(cmd: str) -> str | None:
    """Block a submodule-RECURSIVE snapshot-verb command (see
    _SUBMODULE_RECURSE_VERBS): the superproject snapshot cannot recover submodule
    worktrees, so recursing into them is unrecoverable (like clean) and a
    superproject snapshot is a false recovery promise. Closed-set: literal
    snapshot-verb membership AND a recursing token; honors ``# discard-override``
    per segment. Returns a block message or None. Pure argv (no subprocess).

    Fails CLOSED when a shell_parse BOUND cut the parse short, for the reason given
    in _clean_violation: a bound returns fewer segments without raising, so treating
    that as "no recursing verb" silently allows the unrecoverable case this blocks.
    MEASURED: `git checkout --recurse-submodules .` nested 9 deep went from refused
    to allowed. The guard's documented fail-OPEN is for a parser CRASH, which is a
    different event; `untokenizable` stays on that fail-open path unchanged.

    BOTH bounds refuse, for the reason given in _clean_violation — and here there was
    never even a sibling to appeal to: `bash_safety_hook.sh` has no submodule check at
    all, so softening the length axis left this operation with NO coverage whatsoever.
    MEASURED on `echo "<49,200 chars>" && git checkout --recurse-submodules .`: guard
    rc=0, hook rc=0."""
    segs, blind = _parse_once(cmd)
    if blind is not None and blind.bounds_induced:
        # Its OWN message: this early return precedes the per-segment override
        # check, so `_SUBMODULE_BLOCK_MSG`'s closing offer to "append
        # `# discard-override` to proceed" could not be taken — MEASURED, an
        # over-length command carrying the sigil still returned 2 with that text,
        # leaving no route forward at all. The `clean` twin already got this right.
        return _SUBMODULE_PARSE_FAILED_MSG
    for seg in segs:
        if seg.exe != "git":
            continue
        if not (set(seg.argv[1:]) & _SUBMODULE_RECURSE_VERBS):
            continue
        if not _argv_recurses_submodules(seg.argv):
            continue
        if has_trailing_override(seg.raw, _OVERRIDE_SIGIL):
            continue
        return _SUBMODULE_BLOCK_MSG
    return None


#: Options that consume a SEPARATE following token, per verb. MEASURED from
#: ``git <verb> -h`` on git 2.43.0 — not transcribed from prose, because the cost
#: of a wrong entry here is a note that names the wrong commit as the thing that
#: reverted your work.
#:
#: This exists because the walk it feeds used to skip a ``-`` token and then read
#: the NEXT one as an operand, so an option's VALUE became the rewind source:
#: ``git read-tree --reset -u --index-output tmpindex <old>`` named ``tmpindex``
#: as the commit being restored. Naming the real source is the whole point of the
#: note, so a confidently wrong name is worse than the generic note it replaced.
#:
#: ``--opt=value`` spellings need no entry — they are one token. Clustered shorts
#: (``-um``) are deliberately NOT decomposed; that is the flag-semantics tar pit
#: this module exists to avoid, and it is a stated residual below.
#: `switch` genuinely has no `--pathspec-from-file` — it takes no pathspec at all.
#: It was listed here by symmetry with checkout/restore, which is exactly the
#: "transcribed from prose" failure this comment disclaims, and adversarial review
#: caught it. Harmless in effect (it could only over-skip a flag git rejects), but
#: the claim above has to be true or it is worth nothing.
_TREE_REWIND_OPTS_WITH_ARG: dict[str, frozenset[str]] = {
    "checkout": frozenset({"-b", "-B", "--conflict", "--orphan", "--pathspec-from-file"}),
    "switch": frozenset({"-c", "-C", "--conflict", "--orphan"}),
    "restore": frozenset({"-s", "--source", "--conflict", "--pathspec-from-file"}),
    "read-tree": frozenset({"--index-output", "--prefix", "--exclude-per-directory"}),
}

#: Options that make the command CREATE A BRANCH rather than rewind paths. git
#: rejects these alongside a pathspec, so a command carrying both is not a rewind —
#: it is a command that will not run. Without this, `git checkout -b tmp <sha> -- .`
#: read `<sha>` as a rewind source and wrote a tripwire row for an event that cannot
#: happen, inflating the count that would justify escalating this note to a block.
_TREE_REWIND_BRANCH_CREATING = frozenset({"-b", "-B", "-c", "-C", "--orphan"})


def _rewind_verb_and_operands(argv: list[str]) -> tuple[str, list[str], list[str]] | None:
    """``(verb, operands, options)`` for a git argv whose SUBCOMMAND is a rewind verb.

    ``options`` holds only the tokens that belong to the VERB — everything after
    the resolved subcommand index. Scoping matters: git's own global ``-C <dir>``
    is spelled identically to ``switch``'s force-create ``-C``, so a check written
    against the whole argv read ``git -C <path> checkout <sha> -- .`` as a
    branch-creating command and stopped warning about it — a false NEGATIVE on the
    multi-repository shape this feature exists for. Caught by an existing test.

    ONE walk, consumed by both `_rewind_source` and `_rewrites_whole_tree`. They
    each had their own copy keyed on ``argv.index(verb)``, which is the replica
    drift `shell_parse.git_subcommand_index` was extracted to end — its own
    docstring says so. Three copies of git's option grammar in one file is three
    chances to disagree, and they did.

    The subcommand is RESOLVED, never matched by membership. ``verb in argv`` made
    any token equal to a verb name the verb, so ``git add checkout old .`` — three
    filenames — reported source ``old``, emitted the whole-tree warning and marked
    the snapshot row as a rewind. That last part is what made it worth fixing
    rather than tolerating: the row is the recurrence evidence that would justify
    escalating this note to a block, so a false positive there argues for a block
    on the strength of something that never happened.

    ``-`` is an OPERAND, not an option. ``git checkout - -- .`` is valid — ``-``
    means the previously-checked-out branch — and MEASURED on git 2.43 it replaces
    the named paths from that branch, i.e. exactly the reversion this note exists
    to make conspicuous. The old walk skipped every dash-prefixed token and so
    stayed silent on it.

    Returns None when the argv is not git, has no subcommand, or its subcommand is
    not a rewind verb. ``--`` is KEPT in the operand list because a caller needs to
    know where the pathspec section starts.
    """
    i = git_subcommand_index(argv)
    if i is None:
        return None
    verb = argv[i]
    if verb not in _TREE_REWIND_VERBS:
        return None
    with_arg = _TREE_REWIND_OPTS_WITH_ARG.get(verb, frozenset())
    operands: list[str] = []
    options: list[str] = []
    j = i + 1
    pathspec_mode = False
    while j < len(argv):
        tok = argv[j]
        if pathspec_mode:
            # After `--`, git reads EVERY token as a pathspec — including ones
            # spelled like options. Without this, a file named `-b` after the
            # separator read as branch-creation and suppressed the warning, and
            # `git restore -- --source=old .` (an index restore of a file named
            # `--source=old`) reported `old` as a rewind source — false evidence
            # in the log whose purpose is deciding whether this becomes a block
            # (Codex P2, round 2).
            operands.append(tok)
            j += 1
            continue
        if tok == "--":
            pathspec_mode = True
            operands.append(tok)
            j += 1
            continue
        if tok in with_arg:
            options.append(tok)
            j += 2  # the option AND its value
            continue
        if tok.startswith("-") and tok != "-":
            options.append(tok)
            j += 1
            continue
        operands.append(tok)
        j += 1
    return verb, operands, options


def _writes_the_worktree(argv: list[str], verb: str) -> bool:
    """Does this verb, as spelled, write the WORKING TREE at all?

    An index-only command reverts no merged work, so claiming it rewrote the
    worktree is a false alarm AND false recurrence evidence. Both exclusions below
    are MEASURED on git 2.43 against a repo with an uncommitted edit, checking
    whether the worktree file actually changed — not read off ``--help``, because
    the cost of being wrong here is a MISSED rewind, which is worse than the false
    positive being removed:

      * ``git read-tree --reset <old>``  -> index only (worktree untouched)
      * ``git read-tree --reset -u <old>`` -> REWRITES the worktree
      * ``git restore --staged --source=<old> .`` -> index only
      * ``git restore -S --source=<old> .``       -> index only
      * ``git restore --staged --worktree --source=<old> .`` -> REWRITES
      * ``git restore --source=<old> .`` -> REWRITES (worktree is the default)

    checkout and switch always write the worktree, so they are unconditional here.
    Unrecognised spellings fall toward TRUE — an over-eager note costs a sentence,
    a missed one costs the silent reversion this guard was built for.
    """
    if verb == "read-tree":
        # `-n/--dry-run` reports and changes nothing — MEASURED on git 2.43:
        # `git read-tree -n -u --reset <old>` exits 0 with worktree AND index
        # untouched, yet it warned and wrote `broad: true` recurrence evidence
        # (Codex P2, round 2).
        if {"-n", "--dry-run"} & set(argv):
            return False
        return "-u" in argv
    if verb in ("checkout", "restore") and {"-p", "--patch"} & set(argv):
        # Patch mode is an INTERACTIVE hunk picker: nothing is written until a
        # human selects hunks, and a session-driven Bash tool has no interactive
        # stdin to select them with. The silent-revert shape this note hunts is
        # precisely the non-interactive one, so warning here is only false
        # recurrence evidence (Codex P2, round 2).
        return False
    if verb == "restore":
        staged = {"-S", "--staged"} & set(argv)
        worktree = {"-W", "--worktree"} & set(argv)
        return bool(worktree) or not staged
    return True


def _as_commitish(candidate: str | None) -> str | None:
    """The candidate as a rewind source, or None if it cannot be one.

    Rejects ``HEAD``/``@`` — "put my tracked files back how they already are"
    reverts no merged work and is the ordinary discard the snapshot net covers.
    Also rejects ``--`` and any other dash-prefixed token, which reach here only
    when an option consumed a separator or a flag as its value. The bare ``-`` is
    kept: for ``checkout`` it names the previously-checked-out branch and really
    does rewind from it (measured on git 2.43).

    ONE home for this rule, because it was previously spelled at four return
    points and the fourth disagreed with the other three.
    """
    if not candidate or candidate in _TREE_REWIND_HEAD_ALIASES:
        return None
    if candidate != "-" and candidate.startswith("-"):
        return None
    return candidate


def _rewind_source(argv: list[str], verb: str, cwd: str | None = None) -> str | None:
    """The commit-ish a tree-rewriting git command reads FROM, or None.

    Per-verb rather than one generic rule, because the verbs genuinely differ and
    a single positional rule mis-reads them:
      * ``restore`` takes its source ONLY from ``--source=X`` / ``-s X``. Reading
        it positionally would make ``git restore . src/`` claim ``.`` as a source
        commit, when restore with no ``--source`` reads the INDEX and rewinds
        nothing.
      * ``checkout`` / ``read-tree`` take it as the first bare operand before any
        ``--`` separator.
    Returns None for ``HEAD``/``@`` — "put my tracked files back how they already
    are" reverts no merged work, and is the ordinary local discard the snapshot
    net already covers.

    A candidate that is ``--`` or starts with ``-`` is NOT a commit-ish and is
    rejected. The bare ``-`` is the one exception: for ``checkout`` it names the
    previously-checked-out branch and really does rewind from it. Without this,
    ``git restore -s -- .`` reported source ``--`` — the option consumed the
    separator as its value — and wrote a tripwire row for a command git rejects.
    """
    if verb == "restore":
        for i, tok in enumerate(argv):
            if tok == "--":
                # Everything after the separator is a pathspec, so a token
                # spelled `--source=X` there is a FILENAME, not the option.
                return None
            if tok.startswith("--source="):
                return _as_commitish(tok.split("=", 1)[1])
            if tok in ("-s", "--source") and i + 1 < len(argv):
                return _as_commitish(argv[i + 1])
            if tok.startswith("-s") and not tok.startswith("--") and len(tok) > 2:
                # Attached SHORT form `-s<commit-ish>`, which git accepts and
                # performs the rewind for. Its long twin `--source=<c>` was
                # handled from the start and this was not — same class, and the
                # asymmetry meant two spellings of one command behaved
                # differently (measured: `-s <c> .` warned, `-s<c> .` was silent).
                return _as_commitish(tok[2:])
        return None
    resolved = _rewind_verb_and_operands(argv)
    if resolved is None:
        return None
    if _TREE_REWIND_BRANCH_CREATING & set(resolved[2]):
        # Creating a branch, not rewinding paths — git rejects the combination with
        # a pathspec, so there is no rewind to warn about. See the constant. Scoped
        # to the VERB's options, never the whole argv: git's global `-C <dir>` shares
        # a spelling with switch's `-C`.
        return None
    if verb == "read-tree":
        # read-tree takes UP TO THREE trees and the RESULT content comes from the
        # last one, so `HEAD` in first position is not the "put things back how
        # they are" no-op it is for checkout. MEASURED on git 2.43: with a clean,
        # refreshed index `git read-tree -m -u HEAD <old>` succeeds and rewinds
        # the worktree to <old> — and a clean tree is the likeliest setting for
        # the incident this note exists for. Reading the FIRST operand stopped at
        # HEAD and stayed silent (Codex P2, round 2).
        trees = [t for t in resolved[1] if t != "--"]
        return _as_commitish(trees[-1]) if trees else None
    for tok in resolved[1]:
        if tok == "--":
            return None  # `git checkout -- .` — no source, a plain local discard
        if (
            tok in _TREE_REWIND_BROAD_PATHSPECS
            or tok.endswith("/")
            or _operand_is_directory(tok, cwd)
        ):
            # `git checkout .` / `git checkout src/` / `git checkout src` — the first
            # bare operand is a PATHSPEC, not a commit-ish, so this is the ordinary
            # local discard (identical to `git checkout -- .`) and rewinds nothing. A
            # commit-ish is never one of these tokens, so refusing them here cannot
            # hide a real rewind — and without this the most common discard in the
            # repo would read as its own source and warn on every use.
            #
            # BOTH ENDS OF THE RULE MOVE TOGETHER. The breadth predicate learned to
            # recognise a slashless directory by a stat while this site still asked
            # only about a trailing slash, and the disagreement was immediately a
            # FALSE POSITIVE: MEASURED, `git checkout src` (rc=0, an ordinary discard
            # restoring from the index, reverting nothing merged) fired with source
            # `src`, which is exactly what this branch exists to prevent. Found by
            # sweeping the sibling rather than by the finding that prompted the fix.
            return None
        return _as_commitish(tok)
    return None


def _rewrites_whole_tree(argv: list[str], verb: str, cwd: str | None = None) -> bool:
    """Does this argv apply *source* across the whole worktree rather than to
    named paths?

    ``read-tree`` is its own case and has no pathspec at all: ``-u`` / ``--reset``
    is what makes it write the WORKTREE (without them it only loads the index),
    and when it does, it does so for every path. Requiring a pathspec there would
    miss the shape entirely.

    For the others it is a LITERAL broad-pathspec token — ``.`` / ``./`` / ``:/``
    / ``*`` — or any operand ending in ``/`` (a directory). Deliberately no
    filesystem test: this predicate stays argv-only and subprocess-free like every
    other one here, and being ADVISORY it can afford to be generous — a false
    positive costs one over-eager note, never a refused command.
    """
    # An index-only spelling rewrites nothing in the worktree, so it is excluded
    # before any pathspec question — see `_writes_the_worktree` for the measurements.
    if not _writes_the_worktree(argv, verb):
        return False
    if verb == "read-tree":
        # `--prefix <dir>/` reads the tree UNDER that subdirectory — MEASURED on
        # git 2.43: `git read-tree --prefix=sub/new/ -u <old>` creates sub/new/
        # and touches nothing else. Same verb class, worth a note, but recording
        # it `broad: true` pollutes the very count meant to separate
        # repository-wide incidents from directory-scoped work.
        # Checked on argv directly: read-tree takes no pathspec, so there is no
        # `--` section where this spelling could be a filename instead.
        # (`-u` already established the worktree write; no pathspec exists here,
        # so "broad" is the default and --prefix is the one scoping spelling.)
        return not any(t == "--prefix" or t.startswith("--prefix=") for t in argv)
    # Scan every operand after the verb rather than only those following the
    # source token. Locating the source by VALUE (`argv.index(source)`) silently
    # fails for the attached spelling `--source=<sha>`, where the sha is not a
    # token of its own — measured: `git restore -s <c> .` was detected and
    # `git restore --source=<c> .` was not, for the same command.
    # Safe because a commit-ish is never a broad-pathspec token, and
    # `_rewind_source` now refuses those outright, so the source can never be
    # mistaken for the pathspec that makes this fire.
    resolved = _rewind_verb_and_operands(argv)
    if resolved is None:
        return False
    for tok in resolved[1]:
        if tok == "--":
            continue
        if tok in _TREE_REWIND_BROAD_PATHSPECS or tok.endswith("/"):
            return True
        if _operand_is_directory(tok, cwd):
            return True
    return False


def _operand_is_directory(token: str, cwd: str | None) -> bool:
    """Is this operand a DIRECTORY on disk, spelled without a trailing slash?

    A single `os.path.isdir` — and it is a deliberate departure from the rule stated
    in this module's comments that the predicate stays argv-only with no filesystem
    test. That rule was about not MODELLING git's semantics, which is an open-set
    trap; a stat is not a model, it is a fact, and it is not a subprocess either.

    The cost of not asking was MEASURED: `git checkout <old> -- src` rewinds every
    tracked file under src/ recursively (verified on git 2.43 — src/f.py went back to
    the old content while a file outside src/ was untouched), and the predicate saw
    nothing, because it recognised a directory only by a trailing slash. `src` and
    `./src` are the natural spellings, so the most likely way to write the thing was
    the one way it stayed silent.

    Fails toward FALSE: no cwd, a path that is not there, or any OSError leaves the
    old trailing-slash answer standing. That is a missed note — the status quo before
    this existed — never a false one, and this predicate is advisory.
    """
    if not cwd or not token or token.startswith("-"):
        return False
    try:
        return os.path.isdir(os.path.join(cwd, token))
    except OSError:
        return False


def _tree_rewind_segments(cmd: str, payload: dict) -> list[tuple[str, bool, str | None, bool]]:
    """``(source_commitish, overridden, cwd, broad)`` for every rewind in *cmd*.

    The **cwd is part of the result on purpose**. A payload can touch several
    repos (``git -C A … && git -C B checkout <c> -- .`` is ordinary here, where
    worktrees are the norm), and a recovery instruction naming another repo's
    snapshot is a FALSE recovery promise — the sha does not even resolve there.
    MEASURED before this returned cwd: a two-repo compound advertised repo A's
    sha for a rewind in repo B.

    NEVER blocks — the caller turns this into a louder note and a countable log
    flag. ``# discard-override`` suppresses the NOTE (the author has said they
    know) but is still reported here so the caller can log the tripwire either
    way: an override is exactly the case where knowing it happened still matters.

    RESIDUALS, stated because this is a note and may be generous but must not be
    dishonest. Each degrades to the ordinary snapshot note — the status quo
    before this existed — never to a wrong claim:
      * a pathspec from ``--pathspec-from-file`` is invisible to an argv check;
      * an aliased spelling (``git co``) is invisible to every predicate here;
      * an abbreviated flag (``--sour=X``) is not matched;
      * magic pathspec prefixes (``:(top)``, ``:!``) are not in the broad set, so
        ``git checkout <c> -- ':(top)'`` is silent;
      * clustered short options (``git read-tree -um <c>``) are not decomposed —
        cluster decomposition is the flag-semantics tar pit this module removed.
    """
    found: list[tuple[str, bool, str | None]] = []
    # `_parse_once` (memoised `analyze_checked`), never bare `analyze` — and this
    # is not a style preference. Upstream moved this whole file off bare `analyze`
    # and now enforces that with an allowlist test, which this guard is
    # deliberately NOT on: its own commit message records that writing the entry
    # would have meant recording a known fail-open on a BLOCKING guard that
    # searches. A bare call here would reintroduce exactly that, and the merge
    # that brought the policy in would not have flagged it — the import hunk and
    # this call sit in different places, so nothing conflicted while the name
    # simply stopped being imported.
    segs, _blind = _parse_once(cmd)
    for seg in segs:
        if seg.exe != "git":
            continue
        resolved = _rewind_verb_and_operands(seg.argv)
        if resolved is None:
            continue
        verb = resolved[0]
        # cwd is resolved FIRST because BOTH the source and the breadth question need
        # it: a directory pathspec spelled without a trailing slash can only be
        # recognised relative to the repository the segment runs in, and the two sites
        # must agree or their disagreement is itself a defect.
        cwd = None
        with contextlib.suppress(Exception):
            cwd = _segment_cwd(seg, payload)
        source = _rewind_source(seg.argv, verb, cwd)
        if source is None:
            continue
        if not _rewrites_whole_tree(seg.argv, verb, cwd):
            continue
        # BREADTH is carried out, because the log row cannot recover it later and is
        # forbidden from carrying the command text. `git checkout origin/main -- docs/`
        # is routine work and fires this predicate by design (the note's prose says
        # "UNDER THE PATHSPEC YOU GAVE" for exactly that reason) — but a row that
        # cannot tell it from `-- .` makes the recurrence count, whose whole purpose
        # is deciding whether this becomes a block, argue for one from routine work.
        broad = verb == "read-tree" or any(t in _TREE_REWIND_BROAD_PATHSPECS for t in resolved[1])
        found.append((source, has_trailing_override(seg.raw, _OVERRIDE_SIGIL), cwd, broad))
    return found


def _tree_rewind_note(source: str, cwd: str | None, snapshot_sha: str | None) -> str:
    """The specific warning. Says what the command DOES, not merely that a
    snapshot exists — the generic note already said that on 2026-08-31 and the
    reversion still shipped.

    THE RECOVERY ADVICE WAS WRONG FOUR TIMES, and the fifth version was not chosen
    by judgement — it was measured. Each earlier revision picked a command chain that
    looked right and failed in a state nobody had constructed, so the fifth pass
    stopped picking: 320 enumerated states (intervening commit x unstaged x staged x
    tracked deletion x untracked file x what you did after the rewind x same-file
    overlap x pathspec scope), six candidate procedures, with a no-op and a
    copy-the-reference-back ORACLE as instrument controls, and the success predicate
    and decision rule written down before any result was read.

    Two populations, because the question is only well-posed in one of them. With
    HEAD unchanged since the snapshot (240 cells), success means the pre-rewind
    worktree, index and staged/unstaged split are all reproduced AND any work made
    after the rewind is recoverable:

        capture + `checkout HEAD -- .` + `stash apply --index`   240/240  (= ORACLE)
        `checkout HEAD -- .` + `stash apply --index`              80/240
        `stash apply --index`                                     56/240
        capture + `checkout --no-overlay <snap> -- .`              36/240
        every other single command                               <= 56/240

    The winner is three steps because the two halves of the damage come from
    different places and no single command can fetch both: the merged work that was
    reverted lives on the BRANCH, and your own uncommitted edits live in the
    SNAPSHOT. A stash commit's parent is HEAD-at-create, so replaying it onto a
    pristine HEAD reproduces the snapshot exactly — which is also why
    `stash apply` ALONE scores so badly: it replays a delta, and at snapshot time a
    file the rewind later reverted had no delta, so it comes back reverted while the
    command exits 0 and looks like it worked.

    In the second population — you COMMITTED after the rewind (80 cells) — the
    winner scores 0/80, correctly: HEAD now holds the reversion, so step 2 reinstates
    it. Only content is well-posed there (porcelain is measured against HEAD, which
    moved), and `checkout --no-overlay` gets 72/80. The note says both, because
    telling someone to run the three steps after they have committed would hand back
    the exact reversion they are trying to undo.

    The ORACLE control is what made this trustworthy, and it failed FOUR times first
    — each a flaw in the instrument, not the candidates: an oracle that did not
    capture, a scorer that dropped a modified path from both sides of its own
    comparison, an oracle whose .git swap deleted the capture object it had just
    made, and a porcelain comparison that demanded deleting a file the user created
    after the rewind while another property demanded preserving it. A sweep whose
    oracle scores below 100% proves nothing; the numbers above come from a run where
    it scored 240/240 and the no-op scored 42/240.

    TWO things here were wrong when first written, both caught by EXECUTING them
    rather than reading them, and both are the reason this docstring is long:

    1. It recommended ``git stash apply --index <sha>`` alone, then swung to
       ``git checkout <snap> -- .`` alone. BOTH single-command answers were wrong,
       and the reasoning for the swing contained two claims that do not survive
       re-measurement. RE-MEASURED on git 2.43 against a repo carrying every state
       that distinguishes the candidates — a staged change, an unstaged change, a
       tracked DELETION, and a dirty file outside the rewound pathspec:

       * ``git stash apply --index`` on a dirty tree exits 1 — but it leaves the
         file UNTOUCHED. **No conflict markers.** The earlier "writes conflict
         markers into the file you are rescuing" overstated it: this is a SAFE
         refusal, which is recoverable (try the fallback), not corruption, which
         is not. On a clean post-rewind tree it restores everything with the
         porcelain BYTE-IDENTICAL, including the staged/unstaged split.
       * ``git checkout <snap> -- .`` is the one that silently loses work. Path
         checkout is OVERLAY by default, so a path your work had DELETED is not
         removed again — the rewind's version survives and can be committed. It
         also writes the snapshot tree into index AND worktree, flattening the
         staged/unstaged split, and ``-- .`` reaches paths outside the rewound
         pathspec. "Restores it cleanly, byte-identical" was simply false.
       * ``--no-overlay`` fixes the deletion loss at zero cost and is robust where
         stash-apply refuses, but still flattens the split.

       That reasoning produced the third revision — complete-form-first with a
       stated fallback — and the sweep above refuted it too: `stash apply --index`
       scores 56/240, because "restores the staged/unstaged split exactly" is true
       only when it restores anything at all, and on the central shape it exits 0
       having left the reverted file reverted. Handing someone a single command that
       half-restores is the FALSE RECOVERY PROMISE this module treats as severe
       enough to justify its second hard block, and picking between two partial
       answers commits that error whichever one is picked. The answer was to stop
       picking and measure.
    2. It asserted the command "rewrites EVERY tracked path". The predicate is
       deliberately generous (any operand ending in ``/``), so it also fires on
       ``git checkout main -- tests/``, which does not. The wording now matches
       what argv actually proves.

    The repo is NAMED because a payload can rewind one repo while snapshotting
    several; an unqualified sha sends the reader to the wrong worktree.
    """
    where = f" in {cwd}" if cwd else ""
    if snapshot_sha:
        recover = (
            f" To undo it, IF YOU HAVE NOT COMMITTED since the rewind, run these "
            f"three in order: (1) git stash create — captures whatever is in your "
            f"tree right now and prints a sha, so the next step cannot cost you "
            f"anything; (2) git checkout HEAD -- . — brings the merged work back "
            f"from the branch, which is where it lives; (3) git stash apply "
            f"--index {snapshot_sha[:12]} — replays YOUR OWN edits from the "
            f"snapshot. Steps 2 and 3 are separate because the two halves come "
            f"from different places, and a single command cannot do both: MEASURED "
            f"over 240 states, this reproduces the pre-rewind worktree, index and "
            f"staged/unstaged split in 240 — every single-command alternative "
            f"managed at most 56. IF YOU HAVE ALREADY COMMITTED, none of that "
            f"applies: HEAD now contains the reversion, so step 2 would put it "
            f"straight back. The snapshot tree is then the only record of your "
            f"pre-rewind content — git checkout --no-overlay {snapshot_sha[:12]} "
            f"-- . recovers it in 72 of 80 such states, but lands everything "
            f"STAGED and REMOVES tracked paths the snapshot does not have, "
            f"including any your later commit added, so diff it before committing."
        )
    else:
        recover = (
            " No snapshot was taken for that repo, so there is no recovery point "
            "here — check the snapshot log before you commit."
        )
    return (
        f"[git-discard-guard] WHOLE-TREE REWIND{where}: this rewrites every tracked "
        f"path UNDER THE PATHSPEC YOU GAVE from '{source}' — with `.` or `:/` that "
        f"is the entire worktree, not just the files you edited. Anything merged "
        f"since '{source}' is reverted, and because the revert lands in YOUR "
        f"working tree it shows up inside your own diff looking deliberate, which "
        f"is why the last one was found only by luck. Before committing, diff "
        f"against the upstream branch in BOTH directions, not just `git diff`. If "
        f"you meant to apply someone's changes onto a moved base, use `git merge "
        f"--squash`, `git cherry-pick`, or `git apply --3way` — those FAIL LOUDLY "
        f"on conflict where this succeeds silently.{recover}"
    )


def _record_snapshots(cmd: str, payload: dict) -> list[str]:
    """Best-effort recovery net. NEVER blocks, never raises past its own
    boundary; one snapshot per distinct resolved cwd. Returns the human-facing
    recovery notes (the caller delivers them via additionalContext — see
    _emit_additional_context). The logged row is METADATA ONLY (ts, cwd, sha) —
    deliberately NOT the command: the Bash payload can carry credentials
    (`curl -H 'Authorization: …' && git checkout`) and this log is durable.

    Bounded by ONE whole-payload time budget (_TOTAL_SNAPSHOT_BUDGET_S) across
    every repo, so a compound touching many slow repos can't blow the hook cap;
    a budget-forced skip is SURFACED in a note, never silent."""
    notes: list[str] = []
    seen_cwds: set[str] = set()
    deadline = time.monotonic() + _TOTAL_SNAPSHOT_BUDGET_S
    budget_hit = False
    # Never raises past here — a predicate bug must not cost the snapshot, which
    # is the actual recovery mechanism; the note is a courtesy on top of it.
    try:
        rewinds = _tree_rewind_segments(cmd, payload)
    except Exception:
        rewinds = []
    # cwd -> sha, so a rewind's note carries ITS OWN repo's recovery point. A
    # payload can snapshot several repos while rewinding one; a sha from the
    # wrong repo does not even resolve there.
    sha_by_cwd: dict[str, str] = {}
    segs, blind = _parse_once(cmd)
    # The same "never silent" rule the time budget already obeys, applied to the other
    # reason this can come up short: a parse stopped by a bound yields no segment for
    # a nested snapshot verb, so the recovery point is simply missing — and a missing
    # recovery point that says nothing is indistinguishable from "nothing needed one"
    # exactly when someone is about to discard work. (`untokenizable` excluded for the
    # reason given in _clean_violation: pre-existing here, and noting it would fire on
    # ordinary work.)
    #
    # DEFERRED until after the loop, never appended ahead of it. This note asserts
    # that NO snapshot was recorded, and the loop below can still record one for a
    # repository the parse did reach. Emitting first produced additional context that
    # said "no recovery snapshot was recorded" AND supplied a recovery SHA — leaving
    # the recovery status unreadable at the one moment it is load-bearing. The note is
    # about what the guard could NOT see, so it can only be written once the loop has
    # finished establishing what it could.
    blind_unrecorded = blind is not None and blind.bounds_induced
    for seg in segs:
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
        # One shared budget across all repos: clamp this snapshot to what's left,
        # and stop (surfacing the skip) once too little remains to be useful.
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            budget_hit = True
            break
        sha = _snapshot_worktree(cwd, timeout=min(_GIT_TIMEOUT_S, remaining))
        if not sha:
            continue
        sha_by_cwd[cwd] = sha
        row = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "cwd": cwd,
            "sha": sha,
        }
        # NO tripwire flag here — the rewind event gets its OWN row after this
        # loop. It lived on the snapshot row at first, and that made the count a
        # function of whether `git stash create` happened to produce a sha.
        # MEASURED: a rewind from a CLEAN worktree writes no row at all, so it was
        # warned about and never counted — and a clean tree is the likeliest
        # setting for the incident this exists to measure, where you have no local
        # edits and the rewind silently reverts someone else's merged work. Budget
        # skips and snapshot failures dropped the same way.
        # _write_log_row no longer raises — audit_jsonl converts an OS error to
        # None and reports it — so the old `contextlib.suppress(OSError)` here is
        # gone rather than left reading as live protection.
        # Say where the row LANDED, or say it did not land — never name a path as
        # though a row is in it when the write returned None. The recovery note is
        # what an operator reads while trying to get work back; pointing them at
        # an empty file is the same false promise this store was built to end.
        #
        # And do not name the CAUSE: None means the writer was unimportable, OR
        # the lock was still busy at its deadline, OR an OS/serialisation error —
        # two different things. Naming one would be a fresh false statement in
        # the message this was rewritten to make truthful. The writer reports the
        # real reason on stderr.
        written = _write_log_row(row)
        log_note = (
            f"(log: {written})"
            if written
            else "(NOT logged — see the [audit-log] line on stderr for why; the "
            "snapshot sha above is the only record, so keep it)"
        )
        notes.append(
            f"[git-discard-guard] snapshotted the worktree at {cwd} as "
            f"{sha[:12]} (tracked changes only — `git stash create` does NOT "
            f"capture untracked files OR submodule worktree contents; a "
            f"submodule-recursive discard enabled by PERSISTENT config is NOT "
            f"recoverable from this snapshot). IF that is the repo this command "
            f"discarded work in, recover with: git stash apply --index {sha}  "
            f"(--index restores the staged/unstaged split; drop it if the apply "
            f"conflicts). That holds while HEAD HAS NOT MOVED since the snapshot, "
            f"which is the ordinary case for a local discard. If commits have "
            f"landed in between, a stash apply replays only your own delta and "
            f"leaves anything the command reverted still reverted while exiting 0 "
            f"— MEASURED; run `git checkout HEAD -- .` first, then this. For a "
            f"DELETED/overwritten file (rm/mv), pull it straight "
            f"from the snapshot: git checkout {sha[:12]} -- <path>. {log_note}"
        )
    # THE TRIPWIRE — one row per rewind ATTEMPT, written whether or not a snapshot
    # exists, because "how often does this come up?" is the question that decides
    # whether this note ever escalates to a block.
    #
    # ATTEMPT, not completed rewind, and the distinction is the honest one rather
    # than a hedge. This is a PreToolUse hook: it runs BEFORE bash does, so a row is
    # written for a command that then does not rewind anything. MEASURED: `false &&
    # git checkout <old> -- .` leaves every file untouched (shell short-circuit) and
    # an invalid ref exits 128 having changed nothing — both produce a row. Narrowing
    # the predicate cannot fix that, because the gap is WHEN the hook runs, not what
    # it matches; knowing the outcome would need a PostToolUse correlation this guard
    # does not have. So the count is an upper bound on rewinds, and anything reading
    # it to justify a block must treat it as one.
    #
    # Scoped to the repo actually rewound: it was payload-scoped (`if rewinds:`) at
    # first, which flagged every repo in a compound — MEASURED, a two-repo command
    # scored one event twice. An inflated count argues FOR a block on evidence that
    # does not exist, which is the wrong direction to be wrong in.
    #
    # Recorded even when the note is suppressed by `# discard-override`: an
    # acknowledged rewind is still a rewind, and the count is about frequency, not
    # about whether the author was surprised.
    #
    # METADATA ONLY, like the snapshot row: no command text and no source
    # commit-ish. This log is durable and a Bash payload can carry credentials
    # (`curl -H 'Authorization: …' && git checkout`). `snapshot` carries the local
    # stash sha — already logged in the snapshot row for the same cwd — so an event
    # row answers "was this one recoverable?" on its own; None means it was not.
    #
    # Deduped per (source, repo): two rewinds of one repo from DIFFERENT sources
    # are two events, while the same source twice in one payload is one.
    # `broad` is what makes the count answer the question it is for: True only when
    # the pathspec really was the whole worktree (`.` / `:/` / `*`, or a read-tree
    # that has no pathspec at all). A directory-scoped `git checkout <ref> -- docs/`
    # still gets a row — it is the same verb class and worth seeing — but it is
    # distinguishable, so the escalation count can be taken over the broad ones only.
    logged_events: set[tuple[str, str | None]] = set()
    for source, _overridden, rewind_cwd, broad in rewinds:
        if (source, rewind_cwd) in logged_events:
            continue
        logged_events.add((source, rewind_cwd))
        # `event` is the row's DEDUP identity, and it has to satisfy two opposite
        # requirements at once: IDENTICAL across the two hook wirings that both see
        # one tool call, and DISTINCT across separate invocations. ts+cwd failed both
        # (whole seconds collapse two in-payload events, and one command counted by
        # both wirings can straddle a second boundary); a digest of source+cwd+command
        # fixed the first and failed the second — the same rewind run again next week
        # collapsed into the one event forever, so the tripwire could not see the
        # recurrence it exists to measure (Codex P2, rounds 2 and 3).
        #
        # `tool_use_id` satisfies both, and it is MEASURED present rather than
        # assumed: 58 captured PreToolUse payloads on this install carry it, and
        # `bash_safety_hook.sh` pipes the VERBATIM payload to this guard, so both
        # wirings digest the same id while a re-invocation is a different tool call.
        # One captured payload shape lacks it, so absence degrades to the old
        # source+cwd+command digest — coarse, but still correct for cross-wiring
        # dedup, which is the property that must not break.
        #
        # Still a DIGEST, so the metadata-only rule stands: a Bash payload can carry
        # credentials and sixteen hex of sha256 reverses to nothing.
        invocation = ""
        with contextlib.suppress(Exception):
            raw_id = payload.get("tool_use_id")
            if isinstance(raw_id, str):
                invocation = raw_id
        event = hashlib.sha256(
            f"{source}\x00{rewind_cwd or ''}\x00{cmd}\x00{invocation}".encode()
        ).hexdigest()[:16]
        _write_log_row(
            {
                "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
                "cwd": rewind_cwd,
                "tree_rewind": True,
                "broad": broad,
                "event": event,
                "snapshot": sha_by_cwd.get(rewind_cwd or ""),
            }
        )
    # One note per distinct (source, repo), each carrying THAT repo's snapshot —
    # never a sha borrowed from another repo in the same compound. Only for
    # sources the author has not already acknowledged with the override.
    seen_rewinds: set[tuple[str, str | None]] = set()
    warnings: list[str] = []
    for source, overridden, rewind_cwd, _broad in rewinds:
        if overridden or (source, rewind_cwd) in seen_rewinds:
            continue
        seen_rewinds.add((source, rewind_cwd))
        warnings.append(_tree_rewind_note(source, rewind_cwd, sha_by_cwd.get(rewind_cwd or "")))
    # WARNINGS GO FIRST, and this ordering is load-bearing, not presentation.
    # `_fit_whole_notes` keeps a PREFIX, so whatever is appended last is what a
    # tight budget drops — and rewind warnings were appended after the routine
    # snapshot notes. MEASURED (Codex P1, round 2): 20 dirty repos + 3 rewinds
    # kept 13 routine notes and ZERO rewind warnings — the model saw ordinary
    # recovery furniture and never learned merged work was reverted, which is
    # the pre-change failure this feature exists to end. Ordering also decides
    # what an omission COSTS: a dropped snapshot note's content (cwd + sha) is
    # in the snapshot log the remainder line points at; a dropped rewind
    # warning is recorded nowhere else.
    if budget_hit:
        warnings.append(
            "[git-discard-guard] NOTE: hit the ~"
            f"{_TOTAL_SNAPSHOT_BUDGET_S:.0f}s snapshot budget — one or more later "
            "repos in this compound were NOT snapshotted. Run a single git "
            "command per repo if you need its recovery point."
        )
    if blind_unrecorded:
        # `seen_cwds` is provably EMPTY here, so this says so plainly rather than
        # hedging. A bounds-induced blind spot means `analyze_checked` returned no
        # segments at all, and `seen_cwds` is filled only from inside the segment
        # loop — so "cut short but still snapshotted something" is not a state this
        # function can be in. An earlier revision branched on `sorted(seen_cwds)` and
        # spoke of the repositories it "did snapshot"; that branch was unreachable,
        # and the wording it left on the live path implied repositories that cannot
        # exist. Defensive phrasing against an impossible state is not caution, it is
        # a false claim with a conditional in front of it.
        warnings.append(
            f"[git-discard-guard] no recovery snapshot was recorded at all: this "
            f"command {blind.cause}, so the guard could not tell which "
            f"repositories it touches. To get the missing snapshots: {blind.hint}."
        )
    return warnings + notes


def _emit_additional_context(notes: list[str]) -> None:
    """Deliver recovery notes to the MODEL. A snapshot path exits 0, and Claude
    Code DISCARDS stderr from an exit-0 PreToolUse hook (behavioral_linter.py
    documents the same constraint), so the note must ride
    hookSpecificOutput.additionalContext on STDOUT — the channel actually
    delivered on exit 0. (Via the bash_safety_hook.sh wiring this stdout is
    redirected to that script's stderr and dropped; the DIRECT settings.json
    hook wiring — which `exec`s python so stdout passes straight through —
    delivers the real one. Double-wiring means at most a harmless duplicate.)

    BOUNDED, because the advisory is what this guard exists to deliver. Claude Code
    persists a hook payload over ~10,000 chars and hands the model a preview
    instead, so an oversized advisory is not a shortened advisory — it is an ABSENT
    one, and silently so. MEASURED with the 40-char synthetic cwd the tests use:
    one rewind note is 1,319 chars, seven repositories serialise to 9,430 (under the
    10,000 cap) and EIGHT to 10,766 (over it) — and the crossing comes sooner with
    longer real paths. The first version of this paragraph said 1,329 and 10,846,
    taken from an earlier probe with a different cwd width; adversarial review
    re-derived them. The conclusion is unchanged, but a figure presented as MEASURED
    has to be the figure. A compound rewinding that many repos is the case the
    multi-repo note was added for, so the cap was reachable exactly where the
    feature matters.

    The bound DROPS WHOLE NOTES and says how many, rather than trimming the joined
    text. That is not a style preference: every note ends in a recovery command
    carrying a snapshot sha, and a mid-value cut would hand the reader a TRUNCATED
    SHA — a command that fails, or worse resolves to something else, presented as a
    recovery instruction. An explicitly omitted note sends the reader to the
    snapshot log; an amputated one sends them to a wrong object.

    ``print_json_bounded`` then runs as the envelope backstop for the case the
    selection above cannot fix — a SINGLE note over the cap — where the JSON itself
    must stay parseable."""
    if not notes:
        return
    payload: dict = {
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": ""}
    }
    if hook_output is not None:
        # Budget the CONTENT, which means subtracting the envelope the writer also
        # counts. These two layers measured different things: selection charged the
        # joined notes while `print_json_bounded` charges the serialised payload, so
        # a selection that "fit" could still overflow by the ~96 chars of JSON
        # scaffolding and get cut mid-note. MEASURED: ten rewinds in one payload
        # came to 9,793 chars WITH the writer's trim marker present, i.e. a note was
        # amputated after selection had declared it whole.
        envelope = hook_output.emit_cost(json.dumps(payload))
        notes = _fit_whole_notes(notes, hook_output.DEFAULT_BUDGET - envelope)
    payload["hookSpecificOutput"]["additionalContext"] = "\n".join(notes)
    if hook_output is None:
        print(json.dumps(payload))
        return
    hook_output.print_json_bounded(payload, text_keys=("hookSpecificOutput.additionalContext",))


def _fit_whole_notes(notes: list[str], budget: int) -> list[str]:
    """Keep as many COMPLETE notes as fit, then say what was left out.

    Selection, not truncation. The remainder line is part of the budget rather
    than an overflow added after it — reserving it up front is what stops the
    bound from being the thing that breaks the bound.
    """
    if not notes:
        return notes
    # A note is worth keeping only whole, so measure the real serialised cost of
    # each and stop before the budget rather than after it.
    # Resolved DEFENSIVELY: `_snapshot_dir` reads config and can raise, and this is
    # the cosmetic bound — it must never be the reason an advisory is lost. Naming
    # the env var is a usable answer when the path cannot be resolved; raising here
    # would not be.
    try:
        where = _snapshot_dir()
    except Exception:  # noqa: BLE001 — see above.
        where = "the directory named by GENESIS_DISCARD_SNAPSHOT_DIR"
    remainder = (
        "[git-discard-guard] …and {n} more repository note(s) omitted to stay "
        "within the hook output cap — read the snapshot log for their recovery "
        f"shas: {where}"
    )
    def _cost(text: str) -> int:
        # The SERIALISED cost, not the raw one. The payload ships through
        # `json.dumps`, where a quote or backslash becomes two characters and a
        # control character six — MEASURED (Codex P2, round 2): a note whose cwd
        # carried JSON metacharacters cost 1,461 raw and 1,895 serialised, so
        # raw-cost selection approved "complete" notes the writer then cut
        # mid-note, removing exactly the recovery sha this selector exists to
        # protect. `json.dumps(text)[1:-1]` is the string's own wire form; +2
        # charges the escaped "\n" that joins it to its neighbour.
        return hook_output.emit_cost(json.dumps(text)[1:-1]) + 2

    reserve = _cost(remainder.format(n=len(notes)))
    kept: list[str] = []
    used = 0
    for note in notes:
        cost = _cost(note)
        # Reserve room for the remainder line only while notes actually remain.
        floor = 0 if len(kept) + 1 == len(notes) else reserve
        if used + cost > budget - floor:
            break
        kept.append(note)
        used += cost
    if len(kept) == len(notes):
        return kept
    if not kept:
        # Nothing fit even once. The comment here used to say "a single note alone
        # exceeds the budget" and return `notes[:1]` — but the condition it actually
        # tests is `budget - reserve`, and it never checked how many notes there
        # were. MEASURED: a 9,680-char first note (UNDER the 9,800 budget) followed
        # by a second note returned one note, dropped the second, and emitted NO
        # omission line — a silent drop, from the function whose entire contract is
        # that it never drops anything silently. Found by adversarial review.
        if len(notes) == 1:
            # Genuinely one oversized note: keep it and let the envelope backstop
            # trim it. Dropping it would be a worse answer than a trimmed one.
            return notes
        # The omission line goes FIRST here, which is the opposite of every other
        # path and is deliberate. The kept note alone already exceeds the budget, so
        # the envelope backstop WILL trim the tail — and with the omission last, the
        # trim removed the very statement that something was dropped, leaving a
        # silent drop again one layer down. Measured that way round before this
        # ordering. In this degenerate case the reader's first need is knowing the
        # list is incomplete; the note's tail is what the cap was always going to
        # cost. Ordinary multi-note payloads keep the omission last, where it reads
        # naturally.
        return [remainder.format(n=len(notes) - 1), notes[0]]
    kept.append(remainder.format(n=len(notes) - len(kept)))
    return kept


def main() -> int:
    """Block a non-dry-run ``git clean`` (the one unrecoverable verb); for every
    other trigger verb, snapshot-and-allow. Returns 2 ONLY for a clean
    violation; 0 otherwise.

    Fail directions are SPLIT by consequence (Codex round-5 P1): the clean BLOCK
    fails CLOSED — if the precise parse raises, `main` blocks UNCONDITIONALLY with
    `_CLEAN_PARSE_FAILED_MSG` and the override does NOT escape (it is read inside
    the function that raised); the user simplifies the command instead. So a parser
    bug can never become a silent ALLOW on the direct settings.json wiring, which
    has no shell floor behind it. This paragraph previously described a
    "dependency-free token check" on that path — there is none, and the module
    docstring above explains why a bespoke coarse re-parse was deliberately
    refused. Adversarial review caught the contradiction. The snapshot net
    is ADVISORY and fails OPEN — any error there just means no recovery point,
    never a block on a recoverable verb. An unreadable payload also fails OPEN."""
    try:
        payload = read_payload()
        cmd = field(payload, "command")
    except Exception:
        return 0  # unreadable payload — nothing to act on; fail OPEN
    if discarded_write is not None:
        with contextlib.suppress(
            Exception
        ):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
            discarded_write.remember(cmd)
    if not cmd or "git" not in cmd:
        return 0
    if not any(s in cmd for s in _TRIGGER_SUBSTRINGS):
        return 0

    # Phase 1 — the clean BLOCK (UNRECOVERABLE → fail CLOSED).
    if "clean" in cmd:
        try:
            block_msg = _clean_violation(cmd)
        except Exception:
            # ROBUST-BY-CONSTRUCTION crash path: we are already inside
            # `"clean" in cmd`, so the command the parser choked on mentions clean
            # and we cannot prove it safe. Rather than re-parse it with a bespoke
            # coarse detector (the hand-rolled-parser trap that drew a CRITICAL),
            # fail CLOSED unconditionally and tell the user to simplify. Over-blocks
            # a clean-MENTIONING non-clean (e.g. `git checkout clean-branch`) ONLY
            # on this rare crash path — the accepted safe direction.
            block_msg = _CLEAN_PARSE_FAILED_MSG
        if block_msg:
            # Keep the block even if stderr is unwritable (a closed fd would raise
            # and, on the direct wiring, downgrade exit 1 to non-blocking).
            with contextlib.suppress(OSError):
                print(block_msg, file=sys.stderr)
            if discarded_write is not None:
                with contextlib.suppress(
                    Exception
                ):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
                    discarded_write.warn()
            return 2

    # Phase 1b — submodule-RECURSIVE overwrite is UNRECOVERABLE by the superproject
    # snapshot (stash create can't capture submodule worktrees), so it BLOCKS like
    # clean rather than emit a false recovery promise. Fails OPEN on a parser crash
    # (UNLIKE clean): a snapshot verb is normally recoverable, so we must not
    # over-block every crashed checkout — the rare crash+submodule case is a
    # documented residual. Cheap `recurse` gate avoids analyze() on ordinary cmds
    # (lowered — the config KEY is case-insensitive, so `Submodule.Recurse` must
    # still pass this gate).
    if "recurse" in cmd.lower():
        with contextlib.suppress(Exception):
            sub_msg = _submodule_recurse_violation(cmd)
            if sub_msg:
                with contextlib.suppress(OSError):
                    print(sub_msg, file=sys.stderr)
                if discarded_write is not None:
                    with contextlib.suppress(
                        Exception
                    ):  # not run_guard-wrapped: a raise here exits 1 = NON-blocking
                        discarded_write.warn()
                return 2

    # Phase 2 — the snapshot recovery net (ADVISORY → fail OPEN).
    try:
        notes = _record_snapshots(cmd, payload)
        _emit_additional_context(notes)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
