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
import fcntl
import json
import os
import subprocess
import sys
import time

# Self-locate so sibling hook modules resolve whether run as a script or imported.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import analyze_checked, has_trailing_override  # noqa: E402

#: The chokepoint, parsed ONCE per process. `main` reaches three consumers that each
#: analyse the SAME command string (_clean_violation, _submodule_recurse_violation,
#: _record_snapshots), and the parse is a pure function of that string — so three
#: identical parses were pure cost on a clock this guard SHARES.
#:
#: That clock is the reason this exists rather than being a tidiness nicety.
#: `bash_safety_hook.sh` is registered at 5s and delegates to THREE guards over the
#: same command, this one included, so those duplicates were 3 of the 5 full parses on
#: one budget. MEASURED end to end, worst payload inside both bounds: 6.12s BEFORE
#: (over the registration, i.e. the hook is killed and the command is PERMITTED) and
#: 2.92s after. Bounding the input was not enough on its own; the work per command had
#: to stop being done three times.
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
        return _SUBMODULE_BLOCK_MSG
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
        row = {
            "ts": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
            "cwd": cwd,
            "sha": sha,
        }
        log_path = _snapshot_log_path()
        with contextlib.suppress(OSError):
            log_path = _write_log_row(row) or log_path
        notes.append(
            f"[git-discard-guard] snapshotted the worktree at {cwd} as "
            f"{sha[:12]} (tracked changes only — `git stash create` does NOT "
            f"capture untracked files OR submodule worktree contents; a "
            f"submodule-recursive discard enabled by PERSISTENT config is NOT "
            f"recoverable from this snapshot). IF that is the repo this command "
            f"discarded work in, recover with: git stash apply --index {sha}  "
            f"(--index restores the staged/unstaged split; drop it if the apply "
            f"conflicts). For a DELETED/overwritten file (rm/mv), pull it straight "
            f"from the snapshot: git checkout {sha[:12]} -- <path>. (log: {log_path})"
        )
    if budget_hit:
        notes.append(
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
        notes.append(
            f"[git-discard-guard] no recovery snapshot was recorded at all: this "
            f"command {blind.cause}, so the guard could not tell which "
            f"repositories it touches. To get the missing snapshots: {blind.hint}."
        )
    return notes


def _emit_additional_context(notes: list[str]) -> None:
    """Deliver recovery notes to the MODEL. A snapshot path exits 0, and Claude
    Code DISCARDS stderr from an exit-0 PreToolUse hook (behavioral_linter.py
    documents the same constraint), so the note must ride
    hookSpecificOutput.additionalContext on STDOUT — the channel actually
    delivered on exit 0. (Via the bash_safety_hook.sh wiring this stdout is
    redirected to that script's stderr and dropped; the DIRECT settings.json
    hook wiring — which `exec`s python so stdout passes straight through —
    delivers the real one. Double-wiring means at most a harmless duplicate.)"""
    if not notes:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(notes),
                }
            }
        )
    )


def main() -> int:
    """Block a non-dry-run ``git clean`` (the one unrecoverable verb); for every
    other trigger verb, snapshot-and-allow. Returns 2 ONLY for a clean
    violation; 0 otherwise.

    Fail directions are SPLIT by consequence (Codex round-5 P1): the clean BLOCK
    fails CLOSED — if the precise parse raises, a dependency-free token check
    still blocks a bare-`clean` command (over-block, `# discard-override`
    escapes), so a parser bug can never become a silent ALLOW on the direct
    settings.json wiring (which has no shell-floor behind it). The snapshot net
    is ADVISORY and fails OPEN — any error there just means no recovery point,
    never a block on a recoverable verb. An unreadable payload also fails OPEN."""
    try:
        payload = read_payload()
        cmd = field(payload, "command")
    except Exception:
        return 0  # unreadable payload — nothing to act on; fail OPEN
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
