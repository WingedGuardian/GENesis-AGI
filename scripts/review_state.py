#!/usr/bin/env python3
"""Review state tracker — manages review markers for enforcement hooks.

Used by:
- review_enforcement_prompt.py (UserPromptSubmit hook)
- review_enforcement_commit.py (PreToolUse hook)
- Claude (after /review + code-reviewer agent complete)

The marker file records a hash of ``git diff --cached --raw --no-abbrev -z`` (staged
changes only) at the time review was done.  If staged content changes, the marker
becomes stale and review is required again.  Unstaged working-tree edits
(e.g. from Codex) do not trigger review enforcement.

Review evidence: the authoritative signal is the code-reviewer agent output. It
defaults to a PER-WORKTREE path (``review_state.py evidence-path``) so concurrent
sessions don't overwrite each other's audit; ``--agent-output`` overrides it. gstack
skill-usage telemetry, when present, is recorded as advisory corroboration but never
gates marking — it is not installed on many hosts.

CLI usage:
    python3 review_state.py status         # prints current review state
    python3 review_state.py evidence-path  # prints this worktree's evidence path
    python3 review_state.py mark           # an internal (same-model) review
    python3 review_state.py mark --agent-output <path>                        # internal, explicit path
    python3 review_state.py mark --source external --defects                  # records an external review that found defects
    python3 review_state.py mark --source external --clean                    # records an external review that found none
    python3 review_state.py diff-hash      # prints current diff hash

``--source`` records WHO PRODUCED THE FINDINGS the mark represents:
  * ``--source internal`` (the DEFAULT) — a genesis-architect / genesis-security /
    any-subagent / self review. The outcome flag is optional and ignored here.
  * ``--source external`` — a review by a non-ANTHROPIC MODEL. EXTERNAL is judged by
    the reviewing MODEL, not the gateway: Anthropic Claude via any route (incl. an
    OpenRouter Claude route) is INTERNAL, and a Genesis internal model call is never a
    reviewer. It REQUIRES a review-outcome flag, recorded in the marker:
    ``--defects`` (a new BLOCKER/SHOULD-FIX/P1/P2) or ``--clean`` (none).
The ``--source`` value describes the review that produced the findings, NOT who typed
the evidence file: a mark recording "verified + fixed Codex's findings" is external; a
mark of your own architect/security audit is internal.

NOTHING HERE COUNTS ROUNDS. A local consecutive-defect streak lived here and is
deleted: no gate read it after the commit gate moved to GitHub-backed review rounds
(``review_budget``) and the round reflection it requires before each fix.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

#: Ambient git environment variables that change what THIS MODULE'S git reads
#: report. Two kinds, and the second is why the name is not "location":
#:
#: 1. Repository LOCATION — each redirects git away from the directory a caller
#:    explicitly passed as ``cwd``, so a decision derived from git describes a
#:    DIFFERENT repository than the one being reviewed.
#: 2. Diff BEHAVIOUR — the repository is right, but what the diff REPORTS is
#:    not, which lands on the same sentinels and the same decisions.
#:
#: MEASURED 2026-09-19, each variable alone, scratch repo A on `main` with staged
#: work against unrelated repo B on `feat/decoy`:
#:
#:   GIT_DIR            branch main -> feat/decoy, staged hash -> "clean"
#:   GIT_WORK_TREE      worktree marker key changes
#:   GIT_INDEX_FILE     staged hash -> "clean"
#:   GIT_EXTERNAL_DIFF  _staged_content_hash -> "clean"   (kind 2)
#:
#: Each one corrupts what the gates see. `"clean"` is this module's sentinel for
#: NOTHING STAGED: `is_review_current` reads it as nothing to review (fail OPEN),
#: while the depth gate's content bind (`marker_content_current`) refuses clearance
#: on it (a false block on reviewed work). The branch read defeats the
#: no-commits-to-main check; the key change makes the gate consult a marker file
#: that does not exist. `review_scope`'s substantiality classifier moves
#: `substantial` -> `inline` for the same reason.
#:
#: The REMAINING entries were measured to have NO effect in a standalone-repo
#: configuration. That is not "proven irrelevant" — the configuration was a
#: standalone repo, and GIT_COMMON_DIR inside a LINKED WORKTREE is the obvious
#: untested case — so they stay, because adding to a scrub is the safe
#: direction. Do not cite them as measured to matter; they are not.
#:
#: DUPLICATED, DELIBERATELY, rather than imported. FOUR copies of THIS list
#: exist and hold the SAME names — here, `scripts/review_scope.py`,
#: `src/genesis/session_awareness/zero_drop_git.py` (the original), and the bash
#: array in `.claude/hooks/genesis-hook`, because bash cannot import a Python
#: tuple. A FIFTH, NARROWER list lives in `scripts/worktree_lifecycle.py`
#: (3 names, its own comment concedes it is incomplete); it is NOT part of this
#: parity set and is tracked separately. This module is stdlib-only, and
#: `git_push_guard` wraps its `review_state` import in `try/except` precisely
#: because a module-load exception in a hook exits 1, which Claude Code treats as
#: non-blocking — silently disabling every fail-closed gate in that file. Adding
#: an import here would add exactly that failure mode to the layer this constant
#: exists to harden. `tests/test_hooks/test_git_env_scrub.py` asserts all four
#: copies are EQUAL and fails when any drifts. The lock is the chokepoint; an
#: import would be a liability.
#:
#: The launcher carrying the same names is what the push guard needs too.
#: `.claude/hooks/genesis-hook` scrubs for all ~50 LAUNCHED hooks, one of which
#: — `git_push_guard` — PREDICTS what a `git push` will do: its
#: `_push_config_is_simple` is an allowlist over the user's effective config
#: (`remote.pushDefault`, `push.default`, `push.recurseSubmodules`, …) where a
#: broadening value makes it return False and PROMPT. MEASURED 2026-09-19
#: through the real function, with a control that moves: with
#: `push.default = matching` in `~/.gitconfig` it returns False (prompts) when
#: the config is visible and True — ALLOWS SILENTLY — once that config is
#: scrubbed away. A guard that predicts a command's effect must see the config
#: that command will see, which is the second reason no config channel appears
#: in any of the four copies.
GIT_ENV_UNSET = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_EXTERNAL_DIFF",
    "GIT_DIFF_OPTS",
)

#: NO git CONFIG channel is handled here — not the FILE sources
#: (GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM) and not the INJECTION channels
#: (GIT_CONFIG_COUNT with its GIT_CONFIG_KEY_n/VALUE_n pairs,
#: GIT_CONFIG_PARAMETERS). Three review rounds each found the same generator, so
#: it is written out rather than left to be rediscovered a fourth time:
#:
#:   ALL FOUR ARE PROTECTED CONFIG, and protected config is the only place git
#:   reads `safe.directory` from — deliberately, so an untrusted repository
#:   cannot self-approve. Repo-local config CANNOT substitute (MEASURED: still
#:   rc=129). So removing ANY of them removes that capability, and under a uid
#:   mismatch — a bind-mounted devcontainer, container CI, a hook under sudo —
#:   git REFUSES with EMPTY stdout. `_staged_content_hash` branches on output
#:   alone, and its empty-output sentinel is "clean": NOTHING STAGED, over real
#:   staged work.
#:
#: MEASURED 2026-09-20, one file staged, uid mismatch, safe.directory supplied
#: ONLY through GIT_CONFIG_COUNT (which is how a container or CI supplies it):
#:   injected value present   rc=0    A staged.py
#:   GIT_CONFIG_COUNT scrubbed rc=129  (empty stdout) -> hash reads "clean"
#: The same measurement holds for GIT_CONFIG_GLOBAL via a config file.
#:
#: Unsetting a config channel is not neutral in the other direction either: it
#: re-enables $HOME/.gitconfig, so a caller that set GIT_CONFIG_GLOBAL=/dev/null
#: for isolation silently loses it.
#:
#: What hardens the config routes MEASURED to move a gate decision is a FLAG on
#: the command — `--no-ext-diff` for `diff.external`, `_ATTRIBUTES_HARDENING`
#: for `core.attributesFile`. A flag cannot remove a capability the way an
#: environment edit does, and it is scoped to the one command that needs it.

#: Hardening for git's ATTRIBUTES lookup, applied to the diff commands.
#:
#: MEASURED 2026-09-19 on 200 staged Python lines: a global `core.attributesFile`
#: marking `*.py binary` collapses `--numstat` to `-  -`, and
#: `review_scope.classify_change_substantiality` then reports `inline` instead of
#: `substantial` — the depth gate demands LESS review. `--no-ext-diff` and
#: `--no-textconv` do NOT reach it (different mechanism), and neither does
#: `--text` (MEASURED: `--numstat` still reports `-  -`). Pointing the lookup at
#: an empty file restores `200  0` AND keeps protected config readable, which is
#: the whole reason this is a flag rather than a pin.
#:
#: RESIDUAL, stated rather than implied: attributes have FOUR sources and this
#: closes ONE. `.git/info/attributes` and a STAGED `.gitattributes` reach the
#: same collapse — so a change can lower its own review depth — and no
#: environment or command-line handling can close either. Tracked as an issue.
#: The fourth is the SYSTEM file `/etc/gitattributes`, which git's own binary
#: names alongside its off switch `GIT_ATTR_NOSYSTEM` (both MEASURED present in
#: the git 2.43 binary; the file is absent on this install). Low reachability —
#: it is root-owned — and unlike the other two it IS closable, via
#: `git_env(GIT_ATTR_NOSYSTEM="1")`, which survives the filter because the
#: override merge precedes it. Left alone on purpose: adding it would be a
#: precautionary environment edit of exactly the kind three review rounds
#: established as the thing that removes capabilities, and nothing has measured
#: it moving a decision. Counted here so "three sources" is not read as complete.
#: Not live in this repo today: the tracked `.gitattributes` carries only
#: `/CHANGELOG.md merge=union`, no `binary`/`-diff` marker exists anywhere
#: tracked, and `.git/info/attributes` is absent.
_ATTRIBUTES_HARDENING = ("-c", f"core.attributesFile={os.devnull}")


def git_env(**overrides: str) -> dict[str, str]:
    """The ambient environment with git's own overrides removed.

    Every ``subprocess.run(["git", ...])`` in this module goes through here, so
    scrubbing is a property of the module rather than something four call sites
    each have to remember. ``overrides`` is for values a caller genuinely needs
    to PIN — the staged-diff hash pins ``COLUMNS`` so its output is not
    terminal-width sensitive.

    THE SCRUB IS APPLIED LAST, DELIBERATELY. An earlier revision applied it
    first and ``raise``d if an override would re-add a scrubbed name. That is
    the wrong failure direction HERE: an exception out of this helper leaves the
    hook process non-zero, which Claude Code treats as NON-BLOCKING — so the one
    guard written to fail loudly would have failed the gate OPEN. Filtering
    after the merge makes the guarantee structural instead: a caller simply
    cannot re-add a scrubbed variable, there is no error path to catch, and no
    call site has to remember anything.
    """
    return {k: v for k, v in {**os.environ, **overrides}.items() if k not in GIT_ENV_UNSET}


try:
    from review_deadline import DeadlineExpired, bounded_timeout, propagate_deadline_timeout
except Exception:  # Reverse-version skew: keep state reads available and bounded.

    class DeadlineExpired(RuntimeError):
        """Local reverse-skew equivalent of the shared deadline exception."""

    def bounded_timeout(deadline, cap, *, monotonic=time.monotonic):
        if deadline is None:
            return cap
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DeadlineExpired("aggregate review-gate deadline expired")
        return min(cap, remaining)

    def propagate_deadline_timeout(deadline, error):
        if deadline is not None:
            raise DeadlineExpired(
                "aggregate review-gate deadline expired during subprocess"
            ) from error

_MARKER_DIR = Path.home() / ".genesis" / "review_markers"
# Standing authorization is measured from GitHub-backed review rounds, not from
# any local counter.  Keep these constants here as the
# compatibility import surface used by hook trees at different revisions.
STANDING_REVIEWED_HEAD_LIMIT = 4
GATE_DISCOVERY_ROUND_LIMIT = 2
STRONGLY_DISCOURAGED_REVIEWED_HEADS = 5
# `FINAL_ROUND_CAP = 7` lived here until 2026-09-25 and is DELETED, not deprecated
# again. It was consulted by no gate — verified across the tree, zero conditionals
# — while its value had been chosen specifically to avoid colliding with the
# four-head boundary, so a retired constant was still shaping the live one and
# reading as though a seventh round were a real tier. The `final-round-accept`
# sigil remains PARSED and loudly REFUSED (`shell_parse._KNOWN_SIGILS`, plus the
# retired-NOTE paths in both gates): a stale worktree must still be told the sigil
# is dead, and none of those paths references a cap.
# Legacy single-file marker (pre per-worktree scoping). Only read as a fallback
# so an in-flight review from before an upgrade isn't lost mid-session.
_LEGACY_STATE_FILE = Path.home() / ".genesis" / "review_state.json"
# Per-worktree review-EVIDENCE (the code-reviewer/genesis-architect audit output the
# depth gate validates). Per-worktree — like the marker/round — so concurrent
# sessions don't overwrite each other's evidence (which would validate one session's
# marker against ANOTHER session's audit). Keyed by the same _worktree_key.
_EVIDENCE_DIR = Path.home() / ".genesis" / "review_evidence"
_MAX_EVIDENCE_AGE_SECONDS = 1800  # 30 minutes
_GSTACK_ANALYTICS = Path.home() / ".gstack" / "analytics" / "skill-usage.jsonl"


def _deadline_timeout(deadline: float | None, cap: float) -> float:
    """Subprocess timeout bounded by an optional aggregate ``time.monotonic()`` deadline.

    Callers running under a host kill-window (the commit/push hooks' registered
    timeouts) pass one deadline so a stalled probe consumes the SAME budget the
    later gates need instead of resetting it — a per-call cap alone lets serial
    probes overrun the kill, which fails OPEN.
    An already-elapsed deadline raises before another subprocess starts.
    """
    return bounded_timeout(deadline, cap)


def _worktree_root(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """Absolute worktree root used to key per-worktree state.

    Primary: ``git rev-parse --show-toplevel``. Fallback — git missing or timed out,
    LIKELIEST under the concurrent load this keying exists to survive: walk up from
    ``cwd`` to the nearest ``.git`` (a directory in the main tree, a FILE in a linked
    worktree) and use that directory; if none is found, use the resolved ``cwd``.

    CRITICAL: the fallback stays PER-LOCATION — never a single shared constant. The
    old ``"default"`` fallback meant a transient git hiccup collapsed every concurrent
    session onto ONE key, reopening the cross-session clobber #1244 fixed (observed as
    a marker vanishing / round counter resetting mid-workflow).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_deadline_timeout(deadline, 5),
            cwd=cwd,
            env=git_env(),
        )
        root = result.stdout.strip()
        if root:
            # realpath so the git-success key matches the fallback (which resolves
            # symlinks) for the SAME worktree — otherwise a git-success mark and a
            # git-failed hook check could compute different keys and disagree.
            return os.path.realpath(root)
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
    except (FileNotFoundError, OSError):
        pass
    base = Path(cwd).resolve() if cwd else Path.cwd()
    for d in (base, *base.parents):
        if (d / ".git").exists():
            return str(d)
    return str(base)


def _worktree_key(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """Stable, per-location key from the worktree root (see ``_worktree_root``).

    Concurrent CC sessions each work in their own git worktree; a single global
    marker file let them clobber each other's review state (session B's commit reset
    session A's marker mid-workflow). Keying by worktree root isolates them — and the
    key stays isolated even when git is briefly unavailable (no shared fallback).
    """
    return hashlib.sha256(_worktree_root(cwd, deadline=deadline).encode()).hexdigest()[:12]


def _evidence_file(cwd: str | None = None) -> Path:
    """Per-worktree review-evidence path (``--agent-output`` default).

    Same worktree key as the marker/round so a session's evidence can't be
    overwritten by a concurrent session between writing it and marking the review.
    """
    return _EVIDENCE_DIR / f"{_worktree_key(cwd)}.txt"


def _state_file(cwd: str | None = None, *, deadline: float | None = None) -> Path:
    """Per-worktree marker path (see ``_worktree_key``)."""
    return _MARKER_DIR / f"{_worktree_key(cwd, deadline=deadline)}.json"


def get_current_diff_hash(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """SHA-256 of ``git diff --cached --raw --no-abbrev -z`` output (staged only).

    Only staged changes trigger review enforcement.  Unstaged changes
    (e.g. from Codex or other tools editing the working tree) are ignored
    so they don't cause false-positive review blocks.

    Uses ``--raw --no-abbrev -z`` — a NUL-separated, BYTE-oriented machine format
    that is:
    - **width-independent**: unlike ``--stat`` (path truncation + change-bar
      scaling by ``COLUMNS``/terminal), so the SAME staged content no longer
      hashes differently when ``mark`` (one width) and the commit gate (another
      width) run — the systematic false "code changes without review" block;
    - **content-complete**: the destination blob OID changes on ANY content
      change, INCLUDING binary (unlike ``--numstat``, which collapses every
      binary change to ``-\\t-\\t<path>`` and would let a same-path binary
      swap read as already-reviewed). ``--no-abbrev`` pins full 40-char OIDs so
      the hash can't shift with git's auto-abbreviation length; and
    - **byte-exact on paths**: ``-z`` NUL-separates records and emits RAW path
      bytes (no ``core.quotePath`` quoting, no trailing-whitespace loss). The
      output is hashed as bytes with NO stripping, so a rename ``foo`` → ``foo ``
      (or any non-UTF8 / whitespace path) cannot collide with the original.

    Args:
        cwd: Working directory for git commands. When None, uses the
             process CWD. Pass a worktree path to check worktree state.
    """
    try:
        result = subprocess.run(
            # --no-ext-diff/--no-textconv here are CONSISTENCY, not protection.
            # MEASURED 2026-09-19: the `--raw` format reports metadata rather
            # than content, so no external diff driver runs and this hash held
            # steady across all four injection routes while the plain-diff hash
            # below fell to "clean". A mutation removing those two therefore
            # SURVIVES the suite, correctly — there is no behaviour to pin.
            #
            # --no-replace-objects is DIFFERENT: it IS protection here, and this
            # site is why the distinction had to be re-measured. MEASURED
            # 2026-09-20: a `refs/replace` ref swapping HEAD for a commit whose
            # tree equals the index empties the `--raw` output too, at rc=0 —
            # so the "the raw format is immune" reasoning above is true of the
            # DIFF-DRIVER class and false of this one. Replacement applies at
            # object-read time, below the format layer, which is exactly why the
            # format does not save it. No environment component, so no scrub
            # could have reached it either.
            [
                "git",
                "--no-replace-objects",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--cached",
                "--raw",
                "--no-abbrev",
                "-z",
            ],
            capture_output=True,  # bytes (no text= → no newline/encoding munging)
            timeout=_deadline_timeout(deadline, 10),
            cwd=cwd,
            # --stat output is TERMINAL-WIDTH sensitive: git truncates long paths
            # to fit COLUMNS (honored even without a tty). The mark is written from
            # one process (COLUMNS often unset → width 80) and checked from the
            # hook process (COLUMNS tracks the live terminal), so a long staged
            # path hashed differently in the two and the gate intermittently
            # denied a freshly-marked commit as "without review" (MEASURED
            # 2026-08-11: identical index → 62e24043 @80/unset, cd67763b @120,
            # a3966055 @200). Pin the width so the hash is env-independent; 80
            # matches the unset default, so previously stored markers stay valid.
            # git_env(), not {**os.environ}: an ambient GIT_INDEX_FILE or
            # GIT_DIR makes `--cached` describe a DIFFERENT index, and this hash
            # is what binds a review marker to the staged content.
            env=git_env(COLUMNS="80"),
        )
        content = result.stdout  # raw bytes; do NOT strip (would drop trailing-ws paths)
        if not content:
            return "clean"
        return hashlib.sha256(content).hexdigest()[:16]
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
        return "unknown"
    except (FileNotFoundError, OSError):
        return "unknown"


def has_code_changes(cwd: str | None = None, *, deadline: float | None = None) -> bool:
    """Check if there are any uncommitted code changes."""
    return get_current_diff_hash(cwd=cwd, deadline=deadline) not in ("clean", "unknown")


def _load_marker(cwd: str | None = None, *, deadline: float | None = None) -> dict | None:
    """Read the per-worktree marker, falling back to the legacy global file."""
    for path in (_state_file(cwd, deadline=deadline), _LEGACY_STATE_FILE):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return None


def is_review_current(cwd: str | None = None, *, deadline: float | None = None) -> bool:
    """Check if the stored (per-worktree) marker matches current diff state."""
    current = get_current_diff_hash(cwd=cwd, deadline=deadline)
    if current in ("clean", "unknown"):
        return True  # No changes = no review needed
    state = _load_marker(cwd, deadline=deadline)
    return bool(state) and state.get("diff_hash") == current


def marker_content_current(cwd: str | None = None, *, deadline: float | None = None) -> bool:
    """Whether the marker's recorded FULL-content hash binds the CURRENT staged diff.

    A belt-and-suspenders companion to :func:`is_review_current`. Since ``diff_hash``
    became OID-based (``git diff --cached --raw --no-abbrev``) it is already
    content-complete — a same-shape content swap changes it too — so this full-patch
    bind is now redundant defense-in-depth rather than a strictness upgrade. The commit
    depth gate still uses it to grant adversarial clearance ONLY when the marked content
    IS the staged content (an audit of diff A must not clear a different diff B).
    Fails CLOSED: a real staged hash that mismatches / is absent / errors → False.
    """
    current = _staged_content_hash(cwd=cwd, deadline=deadline)
    if current in ("clean", "unknown"):
        return False  # nothing concrete to bind (or a git error) — never clear on this
    state = _load_marker(cwd, deadline=deadline)
    return bool(state) and state.get("content_hash") == current


def has_valid_review_marker(cwd: str | None = None, *, deadline: float | None = None) -> bool:
    """Check if a (per-worktree) review marker exists and is not expired.

    Unlike is_review_current(), this does NOT short-circuit on clean staged
    area. Used when the caller knows changes are about to be staged (e.g.
    git add && git commit in the same command).
    """
    state = _load_marker(cwd, deadline=deadline)
    if not state:
        return False
    try:
        reviewed_at = state.get("reviewed_at", "")
        if not reviewed_at:
            return False
        ts = datetime.fromisoformat(reviewed_at)
        age = (datetime.now(UTC) - ts).total_seconds()
        return age <= _MAX_EVIDENCE_AGE_SECONDS
    except (ValueError, TypeError):
        return False


def _verify_review_log() -> tuple[bool, str]:
    """Corroborate a recent /review from gstack telemetry — ADVISORY ONLY.

    gstack (the skill-usage logger that writes ``skill-usage.jsonl``) is not
    installed on many hosts, so this must NEVER gate ``mark_reviewed``. The
    authoritative review evidence is the code-reviewer agent output checked by
    ``_verify_agent_output``; this only annotates the marker with whatever
    corroboration it can find. The returned bool is informational (recorded in
    ``review_evidence``), never a refusal signal.
    """
    if not _GSTACK_ANALYTICS.exists():
        return False, "gstack analytics absent — agent-output evidence authoritative"
    try:
        lines = _GSTACK_ANALYTICS.read_text().strip().splitlines()
        now = time.time()
        for line in reversed(lines[-50:]):  # Check last 50 entries
            try:
                entry = json.loads(line)
                if entry.get("skill") == "review":
                    ts_str = entry.get("ts", "")
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    age = now - ts.timestamp()
                    if age <= _MAX_EVIDENCE_AGE_SECONDS:
                        return True, f"gstack /review ran {int(age)}s ago"
            except (json.JSONDecodeError, ValueError, KeyError):
                continue
        return False, "no recent gstack /review entry — agent-output evidence authoritative"
    except OSError as e:
        return False, f"gstack analytics unreadable ({e}) — agent-output evidence authoritative"


def _verify_agent_output(path: str) -> tuple[bool, str]:
    """Verify code-reviewer agent output file exists and is recent."""
    p = Path(path)
    if not p.exists():
        return False, f"Agent output file not found: {path}"
    if p.stat().st_size == 0:
        return False, f"Agent output file is empty: {path}"
    age = time.time() - p.stat().st_mtime
    if age > _MAX_EVIDENCE_AGE_SECONDS:
        return False, f"Agent output is stale ({int(age)}s old, max {_MAX_EVIDENCE_AGE_SECONDS}s)"
    return True, f"Agent output valid ({int(age)}s old, {p.stat().st_size} bytes)"


# ── Review DEPTH signals (for the substantial-change depth gate) ──────────────
# A SUBSTANTIAL change needs an ADVERSARIAL audit, not a precision-filtered inline
# pass. We can't semantically grade a review, so we verify STRUCTURAL engagement
# markers a shallow "looks good, 88% confident" pass lacks. Deliberately LENIENT on
# vocabulary (accepts every real reviewer's ladder — genesis-architect
# BLOCKER/SHOULD-FIX/NOTE, genesis-security CRITICAL/HIGH/LOW, Codex P1/P2/P3, the
# CODE_AUDITOR JSON contract) but STRICT on engagement (must reference specific
# code), so it rejects a rubber stamp without false-blocking a differently-shaped
# real audit. This is an advisory anti-autopilot floor, NOT tamper-proof: a
# determined same-user agent can hand-write these markers — the real teeth are the
# CI review-depth check + the independent cloud reviewer + the human merge.
# Severity-ladder LABELS — matched CASE-SENSITIVELY (uppercase): every real
# reviewer emits them in caps (genesis-architect BLOCKER/SHOULD-FIX, genesis-security
# CRITICAL/HIGH/LOW, Codex P1/P2/P3, CODE_AUDITOR JSON "severity":"high" values). NOT
# IGNORECASE, because lowercase "high confidence"/"low risk"/"medium-sized" is ordinary
# prose a SHALLOW pass uses — matching it would gut the discriminator (audit finding).
_LADDER_LABEL_RE = re.compile(r"\b(BLOCKER|SHOULD[- ]FIX|CRITICAL|HIGH|MEDIUM|LOW|P[123])\b")
# Structural markers that are unambiguous even lowercased (a JSON key / a heading).
_LADDER_PHRASE_RE = re.compile(r'"severity"\s*:|scope\s*check', re.IGNORECASE)
# Engagement with SPECIFIC code (not prose) — accept either shape a real reviewer
# uses: a contiguous ``foo.py:42`` pointer (architect/security/Codex prose), OR the
# CODE_AUDITOR JSON contract's separate ``"file"``/``"line"`` fields (which never
# render as ``file:line``). Requiring only the prose form would false-block a
# legitimate JSON audit.
_FILE_LINE_RE = re.compile(r"[\w./-]+\.\w+:\d+")
_JSON_FILE_RE = re.compile(r'"file"\s*:')
_JSON_LINE_RE = re.compile(r'"line"\s*:')
_MIN_EVIDENCE_CHARS = 400  # a real audit is substantive, not a one-liner


def _evidence_is_adversarial(text: str) -> tuple[bool, str]:
    """Lenient STRUCTURAL check that review evidence is an adversarial audit.

    NOT semantic grading — requires all three engagement markers a shallow pass
    lacks: (1) a recognized severity ladder OR a scope-coverage statement, (2)
    engagement with specific code (a ``file:line`` pointer OR the JSON
    ``"file"``+``"line"`` finding contract), (3) a minimum length. The conjunction
    is what makes a casual prose "high confidence" (no code reference) fail while a
    genuinely clean audit ("Scope Check: … no BLOCKER at foo.py:42") passes.
    """
    stripped = text.strip()
    if len(stripped) < _MIN_EVIDENCE_CHARS:
        return False, f"evidence too short ({len(stripped)} < {_MIN_EVIDENCE_CHARS} chars)"
    if not (_LADDER_LABEL_RE.search(text) or _LADDER_PHRASE_RE.search(text)):
        return False, "no severity ladder / scope-check marker (reads like a shallow pass)"
    has_engagement = bool(_FILE_LINE_RE.search(text)) or bool(
        _JSON_FILE_RE.search(text) and _JSON_LINE_RE.search(text)
    )
    if not has_engagement:
        return False, "no file:line engagement (no specific code referenced)"
    return True, "adversarial-audit structure present"


def get_marker_depth(
    cwd: str | None = None, *, deadline: float | None = None
) -> tuple[str | None, bool]:
    """``(level, adversarial)`` recorded in the current marker; ``(None, False)`` if absent.

    Read by the commit gate's depth check. ``level`` is the computed
    substantiality at mark time; ``adversarial`` is the derived content-verify
    result — NEITHER is self-reported by the caller.
    """
    state = _load_marker(cwd, deadline=deadline)
    if not state:
        return None, False
    level = state.get("level")
    return (level if isinstance(level, str) else None), bool(state.get("adversarial"))


def mark_reviewed(
    agent_output_path: str | None = None,
    cwd: str | None = None,
    *,
    clean: bool = False,
    source: str = "internal",
) -> bool:
    """Write the per-worktree review marker after verifying evidence.

    The authoritative gate is Check 2 (code-reviewer agent output). Check 1
    (gstack telemetry) is advisory — it annotates the marker but never refuses,
    because gstack is not installed on many hosts (see ``_verify_review_log``).

    ``source`` records WHO PRODUCED THE FINDINGS this mark represents:
    ``"internal"`` (the DEFAULT, a same-model review) or ``"external"`` (a
    non-Anthropic cross-model reviewer), with ``clean`` recording an external
    review's OUTCOME. Both are recorded in the marker; nothing counts rounds. Any
    value other than ``"external"`` is normalized to ``"internal"``.

    Returns True if the marker was written, False if the authoritative evidence
    check failed.
    """
    # Normalize to the safe direction: only an explicit "external" counts.
    source = "external" if source == "external" else "internal"
    # Check 1 (advisory): gstack corroboration — recorded, never refuses.
    _review_found, review_msg = _verify_review_log()

    # Check 2 (authoritative): code-reviewer agent output must exist and be recent.
    agent_path = agent_output_path or str(_evidence_file(cwd))
    agent_ok, agent_msg = _verify_agent_output(agent_path)
    if not agent_ok:
        print(f"REFUSED: {agent_msg}", file=sys.stderr)
        print("Dispatch the genesis-architect agent (adversarial audit) first.", file=sys.stderr)
        return False

    # Review-DEPTH signals — COMPUTED here, never self-reported by the caller.
    # ``level`` is the staged-change substantiality; ``adversarial`` is the derived
    # result of structurally verifying the evidence. Both fail-soft: a classifier or
    # read error must never block writing the marker (depth is advisory).
    try:
        from review_scope import classify_change_substantiality

        level = classify_change_substantiality(cwd=cwd)
    except Exception:  # noqa: BLE001 - depth is advisory; never block marking a review
        level = "unknown"
    adversarial = False
    depth_msg = "not checked"
    try:
        adversarial, depth_msg = _evidence_is_adversarial(
            Path(agent_path).read_text(errors="replace")
        )
    except OSError as e:
        depth_msg = f"evidence unreadable ({e})"

    # Authoritative evidence present — write the per-worktree marker.
    state_file = _state_file(cwd)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "diff_hash": get_current_diff_hash(cwd=cwd),
        # FULL-patch content hash — a belt-and-suspenders bind for the depth gate (an
        # audit of diff A must not clear a different diff B). diff_hash is now OID-based
        # (--raw --no-abbrev) and already content-complete, so this is redundant
        # defense-in-depth, kept as an independent signal (see marker_content_current).
        "content_hash": _staged_content_hash(cwd=cwd),
        "reviewed_at": datetime.now(UTC).isoformat(),
        "review_evidence": review_msg,  # advisory annotation (gstack corroboration)
        "agent_evidence": agent_msg,
        "level": level,  # computed substantiality (substantial|inline|unknown)
        "adversarial": adversarial,  # derived: evidence has adversarial-audit structure
        "depth_evidence": depth_msg,
        "source": source,  # internal (same-model) | external (cross-model)
        "outcome": ("clean" if clean else "defects") if source == "external" else None,
    }
    state_file.write_text(json.dumps(state, indent=2))
    # Loud feedback when a substantial change was marked WITHOUT adversarial-audit
    # structure: the commit-time depth gate will block it (this is the fast-feedback).
    if level == "substantial" and not adversarial:
        print(
            f"WARNING: substantial change marked with a non-adversarial review ({depth_msg}). "
            "The commit depth gate will BLOCK this — run a genesis-architect adversarial "
            "audit (enumerate the edge/boundary/sentinel class; read authoritative "
            "semantics) and re-mark.",
            file=sys.stderr,
        )
    if source == "internal":
        label = "internal review"
    else:
        label = "external review, " + ("clean" if clean else "defects found")
    print(f"Review marker written: {state['diff_hash']} ({label})")
    return True


def clear_marker(cwd: str | None = None) -> None:
    """Delete the per-worktree review marker (and the legacy global file).

    Called by the post-commit invalidation hook so the next commit requires a
    fresh review. MUST target the same per-worktree path ``mark_reviewed`` wrote
    — deleting only the legacy file would leave the real marker in place and
    silently authorize every subsequent commit for the marker's TTL. Never raises.
    """
    import contextlib

    for path in (_state_file(cwd), _LEGACY_STATE_FILE):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def clear_all_markers() -> tuple[int, list[str]]:
    """Delete EVERY per-worktree review marker. Returns ``(cleared, failures)``.

    A marker this cannot remove is REPORTED, never swallowed. Under-clearing is
    precisely the bypass this function exists to close, so a silent failure would
    leave the caller believing it had closed it. The caller surfaces ``failures``.

    The last resort for the post-commit invalidator, for the one case where the
    marker to clear cannot be IDENTIFIED: a command whose parse was stopped by a
    shell_parse bound. The candidate-set over-clear cannot cover it, because a
    repository named only by an explicit selector (``git -C <dir> commit``) is
    discoverable solely from the parse — and a bounded parse returns nothing, by
    design, since a partial one is a wrong answer rather than a weak one. Deriving
    the selector from the raw string instead would be a second, hand-rolled shell
    parser, which is the trap this module's guards were built to avoid.

    So the choice is between clearing markers that were not involved and leaving a
    marker whose commit already happened. This module's cost model settles it: an
    extra clear costs some session a redundant re-review, while a survivor
    authorizes an UNREVIEWED commit for the marker's TTL. MEASURED, this fires on
    0 of 45,956 real commands, so the redundant-review cost is theoretical and the
    bypass it closes is not.

    Never raises: invalidation runs post-commit, and a crash here would leave the
    stale marker this exists to remove.
    """
    cleared = 0
    failures: list[str] = []

    # THE LEGACY MARKER IS CLEARED FIRST, BEFORE ANY EARLY RETURN CAN SKIP IT.
    # `_load_marker` reads `(_state_file(cwd), _LEGACY_STATE_FILE)` in that order, so
    # the legacy file authorizes a commit entirely on its own — it does not need the
    # per-worktree directory to exist, or to be readable, or to hold anything. It was
    # cleared at the END of this function, below the `_MARKER_DIR` probe, which made
    # it unreachable in exactly the two states where the probe returns early:
    #
    #     dir never created  -> `return 0, []`     — reported as a CLEAN success
    #     dir unreadable     -> `return 0, [err]`  — a REAL failure, but not this one
    #
    # MEASURED at both, with a valid legacy marker present: it survived and
    # `_load_marker()` returned it. The first is the dangerous one — an upgraded
    # install that has only the legacy file gets "cleared 0, no failures", i.e. this
    # function reporting that it closed the bypass while the bypass is intact.
    #
    # Ordering is the fix, not an extra branch: a cleanup placed after an early return
    # is inert, so hoisting it above every return makes a future early return unable
    # to reintroduce this. Counted only when it actually existed, because
    # `missing_ok=True` succeeds on an absent file and "cleared 1" against nothing
    # propagates into the operator-facing message.
    if _LEGACY_STATE_FILE.exists():
        try:
            _LEGACY_STATE_FILE.unlink()
        except OSError as exc:
            failures.append(f"{_LEGACY_STATE_FILE}: {exc}")
        else:
            # The same re-read the per-worktree markers get below, applied here because
            # hoisting this above the probe moved it OUT of that survivors glob — which
            # scans `_MARKER_DIR` only. This file is the one deletion in this function
            # that authorizes a commit ON ITS OWN, so it is the last one that should be
            # counted on trust: an `unlink()` returning success against a file that is
            # still there (a racing writer, an overlay that drops the write) is exactly
            # the case the survivors check exists for, and `cleared` is printed to the
            # operator verbatim.
            if _LEGACY_STATE_FILE.exists():
                failures.append(f"{_LEGACY_STATE_FILE}: still present after unlink")
            else:
                cleared += 1

    # `Path.glob` does NOT raise on an unreadable directory — it SWALLOWS the
    # PermissionError and yields nothing. MEASURED on 3.12: a marker dir at mode 000
    # holding one marker globs to `[]` with no exception. So an `except OSError`
    # around it is dead code, and an empty result is indistinguishable between "no
    # markers exist" and "every marker survived and I could not see them". The second
    # is the bypass this function exists to close, reported as a clean success.
    #
    # Probing readability explicitly is what tells them apart. Checked BEFORE the glob
    # and treated as a hard failure, because "cleared 0, no failures" on an unreadable
    # directory is the single most dangerous thing this function could return.
    if not os.access(_MARKER_DIR, os.R_OK | os.X_OK):
        if _MARKER_DIR.exists():
            failures.append(f"{_MARKER_DIR}: unreadable, so surviving markers cannot be seen")
        # Both branches land here, and they mean opposite things: unreadable → markers
        # survive UNSEEN and the failure above says so; absent → nothing more to clear.
        return cleared, failures

    for path in _MARKER_DIR.glob("*.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
        else:
            cleared += 1

    # A marker still present after its unlink "succeeded" is the same silent
    # under-clear in a different disguise (a racing writer, an overlay that drops the
    # write). Cheap to check, and the only way this function can honestly claim the
    # bypass is closed.
    survivors = [str(p) for p in _MARKER_DIR.glob("*.json")]
    if survivors:
        failures.extend(f"{p}: still present after unlink" for p in survivors)
    return cleared, failures


def _staged_content_hash(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """SHA-256 of the FULL staged patch (``git diff --cached``).

    A belt-and-suspenders content bind for the depth gate. ``get_current_diff_hash``
    is now OID-based (``--raw --no-abbrev``) and already distinguishes two different
    fixes to the same file, so this full-patch hash is redundant defense-in-depth —
    kept (and wired at the depth gate) as a second, independent content signal. (The
    patch body carries the actual +/- content, so it is content-sensitive; the
    abbreviated OID in the ``index`` header is harmless — mark and hook run in the same
    repo/env, so it never diverges between the two.)
    """
    try:
        result = subprocess.run(
            # --no-ext-diff is LOAD-BEARING, not hygiene. An external diff
            # driver makes this command emit NOTHING, so this hash becomes
            # the "clean" nothing-staged sentinel over real staged work, and
            # the depth gate's content bind then reads it as nothing staged.
            # Scrubbing GIT_EXTERNAL_DIFF alone does NOT close that: MEASURED
            # 2026-09-19, the same driver can be set through GIT_CONFIG_COUNT,
            # GIT_CONFIG_PARAMETERS or GIT_CONFIG_GLOBAL, and through a
            # repo-local .git/config that no environment scrub can reach. The
            # flag asks git not to do the thing at all, which is the only
            # form that does not depend on enumerating the ways in.
            # --no-textconv closes the sibling; MEASURED byte-identical in the
            # ordinary case, so it costs nothing.
            # _ATTRIBUTES_HARDENING is CONSISTENCY here rather than protection:
            # a `binary` attribute changes this diff's TEXT, so the hash moves
            # and a stored marker stops matching — MORE review, the fail-CLOSED
            # direction. It is load-bearing at review_scope's --numstat site,
            # where the same attribute collapses the count and the depth gate
            # reads `inline`. Applied at both so the two are computed alike.
            [
                "git",
                *_ATTRIBUTES_HARDENING,
                "--no-replace-objects",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--cached",
            ],
            capture_output=True,
            text=True,
            timeout=_deadline_timeout(deadline, 10),
            cwd=cwd,
            env=git_env(),
        )
        content = result.stdout
        if not content.strip():
            return "clean"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
        return "unknown"
    except (FileNotFoundError, OSError):
        return "unknown"


def get_current_branch(cwd: str | None = None, *, deadline: float | None = None) -> str:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=_deadline_timeout(deadline, 5),
            cwd=cwd,
            env=git_env(),
        )
        return result.stdout.strip() or "unknown"
    except subprocess.TimeoutExpired as exc:
        propagate_deadline_timeout(deadline, exc)
        return "unknown"
    except (FileNotFoundError, OSError):
        return "unknown"


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: review_state.py [status|mark|diff-hash|evidence-path]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "status":
        diff_hash = get_current_diff_hash()
        changes = has_code_changes()
        current = is_review_current()
        print(f"diff_hash: {diff_hash}")
        print(f"has_changes: {changes}")
        print(f"review_current: {current}")
        state = _load_marker()
        if state:
            print(f"last_reviewed: {state.get('reviewed_at', 'unknown')}")
            print(f"stored_hash: {state.get('diff_hash', 'none')}")

    elif cmd == "diff-hash":
        print(get_current_diff_hash())

    elif cmd == "evidence-path":
        # Create the dir so a `save-to $(evidence-path)` shell redirect (the flow the
        # hook/SKILL instructions describe) doesn't fail on a fresh host — otherwise
        # the evidence write fails, `mark` finds no evidence, and the gate REFUSES.
        _EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        print(_evidence_file())

    elif cmd == "mark":
        # Parse --agent-output <path>, --source {internal,external}, and the review
        # outcome (--clean / --defects):
        #   * --source internal (the DEFAULT) — a same-model self/subagent review. The
        #     outcome flag is OPTIONAL and ignored; a bare `mark` is a valid review.
        #   * --source external — a non-Anthropic cross-model reviewer. It REQUIRES
        #     exactly one outcome flag (--clean XOR --defects), recorded in the marker.
        #     The requirement is FAIL-CLOSED: a refusal writes NO marker, so the
        #     commit gate still blocks until the caller re-runs correctly.
        agent_path = None
        saw_clean = False
        saw_defects = False
        source = "internal"
        # Normalize `--key=value` into `--key value` first, so the conventional equals form is
        # parsed too. A split-token-only parser silently dropped `--source=external`, recording
        # a cross-model review as INTERNAL. dont_hand_roll_cli_parsing_in_hooks — bind
        # atomically. (Codex P2.)
        norm_args: list[str] = []
        for a in sys.argv[2:]:
            if a.startswith("--") and "=" in a:
                k, v = a.split("=", 1)
                norm_args.extend([k, v])
            else:
                norm_args.append(a)
        i = 0
        while i < len(norm_args):
            arg = norm_args[i]
            if arg == "--agent-output":
                if i + 1 >= len(norm_args):
                    print("REFUSED: --agent-output requires a value (a path).", file=sys.stderr)
                    sys.exit(1)
                agent_path = norm_args[i + 1]
                i += 1
            elif arg == "--clean":
                saw_clean = True
            elif arg == "--defects":
                saw_defects = True
            elif arg == "--source":
                # A valueless --source must NOT fall through to the internal default (that would
                # miscount an intended external mark) — refuse instead. Fail-closed.
                if i + 1 >= len(norm_args):
                    print(
                        "REFUSED: --source requires a value (internal or external).",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                source = norm_args[i + 1]
                i += 1
            elif arg.startswith("--"):
                # FAIL-CLOSED on any unknown option (e.g. a `--soruce` typo). Silently skipping
                # it would leave the internal default in place → an intended external review is
                # recorded internal. Refuse so the caller re-runs
                # correctly. (dont_hand_roll_cli_parsing_in_hooks; Codex P2.)
                print(
                    f"REFUSED: unknown option {arg!r}. Recognized: --agent-output, --source, "
                    "--clean, --defects.",
                    file=sys.stderr,
                )
                sys.exit(1)
            i += 1
        if source not in ("internal", "external"):
            print(
                f"REFUSED: --source must be 'internal' or 'external', got {source!r}. "
                "Internal = a same-model self/subagent review; external = a "
                "non-Anthropic cross-model reviewer (Codex/Kimi/…) whose findings this "
                "mark records.",
                file=sys.stderr,
            )
            sys.exit(1)
        if saw_clean and saw_defects:
            print(
                "REFUSED: pass exactly one of --clean / --defects, not both "
                "(contradictory review outcome).",
                file=sys.stderr,
            )
            sys.exit(1)
        if source == "external" and not (saw_clean or saw_defects):
            print(
                "REFUSED: an `--source external` mark must state the review OUTCOME — "
                "pass --clean (the cross-model review found no new BLOCKER/SHOULD-FIX/"
                "P1/P2) or --defects (it found one). The marker records it; an internal "
                "mark doesn't need it.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not mark_reviewed(agent_path, clean=saw_clean, source=source):
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
