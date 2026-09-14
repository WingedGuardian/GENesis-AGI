#!/usr/bin/env python3
"""Review state tracker — manages review markers for enforcement hooks.

Used by:
- review_enforcement_prompt.py (UserPromptSubmit hook)
- review_enforcement_commit.py (PreToolUse hook)
- genesis_stop_hook.py (Stop hook)
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
    python3 review_state.py mark           # an internal (same-model) review — never counts
    python3 review_state.py mark --agent-output <path>                        # internal, explicit path
    python3 review_state.py mark --source external --defects                  # external defect-bearing round → +1
    python3 review_state.py mark --source external --clean                    # external clean round → reset streak
    python3 review_state.py diff-hash      # prints current diff hash

THE ESCALATION STREAK IS CROSS-MODEL ONLY. ``--source`` records WHO PRODUCED THE
FINDINGS the mark represents, and it is what decides whether the round counts:
  * ``--source internal`` (the DEFAULT) — a genesis-architect / genesis-security /
    any-subagent / self review. Same author-model reviewing its own work: it is free,
    shares the author's blind spots, and NEVER moves the streak (not an increment, not
    a reset), whatever its outcome. The outcome flag is optional and ignored here.
  * ``--source external`` — a review by a non-ANTHROPIC MODEL found (or cleared) the round.
    EXTERNAL is judged by the reviewing MODEL, not the gateway: Anthropic Claude via any
    route (incl. an OpenRouter Claude route) is INTERNAL, and a Genesis internal model call
    is never a reviewer. The approved methods are Codex plus whatever the install names
    as its secondary reviewer — NOT OpenRouter, and never a Genesis internal model. Which
    reviewer that is, if any, is install-local: it belongs in local config, not in a file
    every clone receives.
    This is the only kind that counts, so it REQUIRES a review-outcome flag:
    ``--defects`` (a new BLOCKER/SHOULD-FIX/P1/P2 → +1) or ``--clean`` (none → reset the
    streak, circuit-breaker reset-on-success).
The ``--source`` value describes the review that produced the findings, NOT who typed
the evidence file: a mark recording "verified + fixed Codex's findings" is external; a
mark of your own architect/security audit is internal. (supersedes feea3f71 / #1446 —
the required-outcome trap it closed only ever bit internal re-audits, which now can't
inflate the streak at all; the requirement is kept where it is still load-bearing, on
external marks.)
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

_MARKER_DIR = Path.home() / ".genesis" / "review_markers"
# Per-worktree review-ROUND counter (escalation cap). Deliberately a SEPARATE store
# from the marker above: review_invalidate_on_commit clears the marker after every
# commit, but the round count must SURVIVE commits — a review→fix→re-review loop
# commits between rounds, and the whole point is to notice when that loop runs long.
_ROUND_DIR = Path.home() / ".genesis" / "review_rounds"
# After this many CONSECUTIVE review rounds on one change, the commit gate blocks
# pending an explicit '# escalation-ack'. Mirrors the genesis-development SKILL.md
# prose cap.
ESCALATION_ROUND_CAP = 3
# After this many rounds over the branch's WHOLE LIFE, the commit gate blocks
# pending a '# final-round-accept' — and unlike the cap above, that block returns
# on every subsequent commit.
#
# WHY A SECOND COUNTER EXISTS. `# escalation-ack` makes the gate call
# reset_review_round, so the consecutive cap is indefinitely REPEATABLE:
# rounds 1-2-3, ack, 4-5-6, ack, 7-8-9, ack, without end. A change can consume
# fifteen external review rounds and the machine never says "enough" — only
# "enough, for now", once every three rounds. The consecutive counter cannot
# express a terminal because resetting it is exactly what the ack is for.
#
# 7 is the start of the THIRD cycle: two complete free/audit/stop cycles have
# already run, each of which already demanded a fresh decision. A change still
# surfacing new defects from an independent reviewer after that is not converging,
# and the remaining question is a judgement call a machine should not keep
# deferring — accept the outstanding findings and merge, or abandon the branch and
# restart from a design that does not need seven rounds.
FINAL_ROUND_CAP = 7
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


def _worktree_root(cwd: str | None = None) -> str:
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
            timeout=5,
            cwd=cwd,
        )
        root = result.stdout.strip()
        if root:
            # realpath so the git-success key matches the fallback (which resolves
            # symlinks) for the SAME worktree — otherwise a git-success mark and a
            # git-failed hook check could compute different keys and disagree.
            return os.path.realpath(root)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    base = Path(cwd).resolve() if cwd else Path.cwd()
    for d in (base, *base.parents):
        if (d / ".git").exists():
            return str(d)
    return str(base)


def _worktree_key(cwd: str | None = None) -> str:
    """Stable, per-location key from the worktree root (see ``_worktree_root``).

    Concurrent CC sessions each work in their own git worktree; a single global
    marker file let them clobber each other's review state (session B's commit reset
    session A's marker mid-workflow). Keying by worktree root isolates them — and the
    key stays isolated even when git is briefly unavailable (no shared fallback).
    """
    return hashlib.sha256(_worktree_root(cwd).encode()).hexdigest()[:12]


def _evidence_file(cwd: str | None = None) -> Path:
    """Per-worktree review-evidence path (``--agent-output`` default).

    Same worktree key as the marker/round so a session's evidence can't be
    overwritten by a concurrent session between writing it and marking the review.
    """
    return _EVIDENCE_DIR / f"{_worktree_key(cwd)}.txt"


def _state_file(cwd: str | None = None) -> Path:
    """Per-worktree marker path (see ``_worktree_key``)."""
    return _MARKER_DIR / f"{_worktree_key(cwd)}.json"


def get_current_diff_hash(cwd: str | None = None) -> str:
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
            ["git", "diff", "--cached", "--raw", "--no-abbrev", "-z"],
            capture_output=True,  # bytes (no text= → no newline/encoding munging)
            timeout=10,
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
            env={**os.environ, "COLUMNS": "80"},
        )
        content = result.stdout  # raw bytes; do NOT strip (would drop trailing-ws paths)
        if not content:
            return "clean"
        return hashlib.sha256(content).hexdigest()[:16]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def has_code_changes(cwd: str | None = None) -> bool:
    """Check if there are any uncommitted code changes."""
    return get_current_diff_hash(cwd=cwd) not in ("clean", "unknown")


def _load_marker(cwd: str | None = None) -> dict | None:
    """Read the per-worktree marker, falling back to the legacy global file."""
    for path in (_state_file(cwd), _LEGACY_STATE_FILE):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
    return None


def is_review_current(cwd: str | None = None) -> bool:
    """Check if the stored (per-worktree) marker matches current diff state."""
    current = get_current_diff_hash(cwd=cwd)
    if current in ("clean", "unknown"):
        return True  # No changes = no review needed
    state = _load_marker(cwd)
    return bool(state) and state.get("diff_hash") == current


def marker_content_current(cwd: str | None = None) -> bool:
    """Whether the marker's recorded FULL-content hash binds the CURRENT staged diff.

    A belt-and-suspenders companion to :func:`is_review_current`. Since ``diff_hash``
    became OID-based (``git diff --cached --raw --no-abbrev``) it is already
    content-complete — a same-shape content swap changes it too — so this full-patch
    bind is now redundant defense-in-depth rather than a strictness upgrade. The commit
    depth gate still uses it to grant adversarial clearance ONLY when the marked content
    IS the staged content (an audit of diff A must not clear a different diff B).
    Fails CLOSED: a real staged hash that mismatches / is absent / errors → False.
    """
    current = _staged_content_hash(cwd=cwd)
    if current in ("clean", "unknown"):
        return False  # nothing concrete to bind (or a git error) — never clear on this
    state = _load_marker(cwd)
    return bool(state) and state.get("content_hash") == current


def has_valid_review_marker(cwd: str | None = None) -> bool:
    """Check if a (per-worktree) review marker exists and is not expired.

    Unlike is_review_current(), this does NOT short-circuit on clean staged
    area. Used when the caller knows changes are about to be staged (e.g.
    git add && git commit in the same command).
    """
    state = _load_marker(cwd)
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


def get_marker_depth(cwd: str | None = None) -> tuple[str | None, bool]:
    """``(level, adversarial)`` recorded in the current marker; ``(None, False)`` if absent.

    Read by the commit gate's depth check. ``level`` is the computed
    substantiality at mark time; ``adversarial`` is the derived content-verify
    result — NEITHER is self-reported by the caller.
    """
    state = _load_marker(cwd)
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

    ``source`` records WHO PRODUCED THE FINDINGS this mark represents and decides
    whether it moves the cross-model escalation streak:
      * ``"internal"`` (the DEFAULT) — a same-model self/subagent review
        (genesis-architect / genesis-security / any spawned agent). It NEVER moves
        the streak, so ``clean`` is irrelevant here (see ``bump_review_round``).
      * ``"external"`` — a non-Anthropic cross-model reviewer (Codex/Kimi/…). Here
        ``clean`` records the OUTCOME: ``clean=True`` (no new BLOCKER/SHOULD-FIX/P1/P2)
        resets the streak (circuit-breaker reset-on-success); ``clean=False`` counts
        the round as defect-bearing.
    Any value other than ``"external"`` is normalized to ``"internal"`` — the safe
    direction (an unknown provenance does not inflate the cross-model streak).

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
        "source": source,  # internal (same-model, never counts) | external (cross-model)
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
    # Update the escalation counter (see bump_review_round). A defect-bearing round
    # on a distinct staged diff advances the streak; a clean round resets it.
    # Best-effort: marking the review must ALWAYS succeed even if the counter is
    # unwritable, so a counter problem can never leave the user stuck behind the gate.
    try:
        round_n = bump_review_round(cwd=cwd, clean=clean, source=source)
    except Exception:  # noqa: BLE001 - the counter must never block marking a review
        round_n = 0
    if source == "internal":
        label = "internal review — cross-model streak unchanged"
    elif clean:
        label = "external clean → streak reset"
    else:
        label = f"external round {round_n}"
    print(f"Review marker written: {state['diff_hash']} ({label})")
    # Class-sweep reminder (369bbe0e): from the SECOND defect-bearing EXTERNAL round on
    # this branch you're in a cross-model review→fix loop — the #1 cause of
    # round-after-round loops is instance-patching the flagged line instead of sweeping
    # the whole defect CLASS. A print here fires the reminder DETERMINISTICALLY at the
    # mark-time decision moment (a recall-dependent memory didn't fire during PR #1397's
    # 3-round Codex loop). Advisory only — never blocks, never touches the return value.
    # Internal marks never reach round>=2 (they don't count), and a clean external round
    # (round_n == 0) is not a loop, so both correctly emit nothing.
    if source == "external" and round_n >= 2:
        print(
            f"REMINDER (round {round_n}): you're in a review→fix loop. Before the next "
            "fix, classify ALL findings into defect CLASSES and sweep every sibling — "
            "every reader of a shared store; both build-paths of a migration "
            "(create_all_tables + the numbered migration); both ends of a marker/parser "
            "spec; every commit-A-then-B writer; every status in a state machine. "
            "Instance-patching the one flagged line is the #1 cause of review-loop churn. "
            "(See CC memory review_loop_termination.)",
            file=sys.stderr,
        )
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


# ── Review-round counter (escalation cap) ─────────────────────────────────────
# Counts CONSECUTIVE defect-bearing review→fix→re-review rounds per change so the
# commit gate can force a conscious stop after ESCALATION_ROUND_CAP rounds. Kept
# in a per-worktree file SEPARATE from the marker (the marker is wiped on every
# commit; the round count must persist across the commits a review loop makes
# between rounds).
#
# A round counts (advances the streak) only when the review it records surfaced a
# NEW defect — the caller passes clean=False (the default) — AND the staged diff
# is distinct from the last-counted one on this branch. A CLEAN review round
# (clean=True: the review found no BLOCKER/SHOULD-FIX/P1/P2 finding) RESETS the
# streak to 0 — circuit-breaker reset-on-success. This makes the machine
# implement the SKILL.md cap literally ("3 rounds that each find NEW defects"):
# honestly-clean multi-commit development (three independent clean reviews) never
# trips, while a genuine review→fix loop that keeps surfacing defects still stops
# at round 3. A branch change resets the count; re-marking the same diff does not
# advance it.


def _round_file(cwd: str | None = None) -> Path:
    """Per-worktree round-counter path (same worktree key as the marker)."""
    return _ROUND_DIR / f"{_worktree_key(cwd)}.json"


def _staged_content_hash(cwd: str | None = None) -> str:
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
            ["git", "diff", "--cached"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
        content = result.stdout
        if not content.strip():
            return "clean"
        return hashlib.sha256(content.encode()).hexdigest()[:16]
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def _coerce_finite_int(value: object, default: int = 0) -> int:
    """Best-effort finite int from a persisted JSON value; ``default`` otherwise.

    Closes the malformed-counter VALUE class at a single point: a valid JSON number
    like ``1e999`` parses to ``float('inf')``, and ``int(inf)`` raises ``OverflowError``
    (NOT a subclass of ``TypeError``/``ValueError``), so a plain ``int()`` guard would
    let it escape and crash the gate — violating the never-raises/fail-open contract.
    Rejecting non-finite floats and catching ``OverflowError`` here means no persisted
    round value can ever raise, however corrupt. Never raises.
    """
    try:
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _load_round(cwd: str | None = None) -> dict:
    """Read the round-counter file, normalizing shape AND the round VALUE.

    Validating once, here at the single load boundary, is the whole class-fix for
    malformed counters. Two sub-classes are closed here:
      - SHAPE: valid JSON that is not an object (``[1,2,3]``, ``42``, ``"str"`` from a
        manual edit / schema skew) would reach a caller's ``.get()`` and raise
        ``AttributeError`` — normalized to ``{}``.
      - VALUE: a well-formed dict whose ``round`` is pathological (``1e999`` → inf →
        ``int()`` ``OverflowError``; a non-numeric string; null) — coerced to a finite
        int via ``_coerce_finite_int`` so every caller provably gets a clean ``int``.
    Every caller therefore receives a dict with a finite-int ``round`` → nothing a
    corrupt file contains can raise → it collapses to "no/zero counter → fail open".
    Never raises.
    """
    try:
        p = _round_file(cwd)
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                # LEGACY discard: a counter written by the pre-source-axis (reviewer-
                # agnostic) implementation has a `round` but no `last_source`. Its count
                # is untrusted — under the old model the local streak only ever counted
                # INTERNAL self-reviews, so a nonzero legacy count is exactly the
                # internally-inflated streak this change exists to stop counting. On
                # upgrade, preserving it would let the commit gate keep mode-switching /
                # hard-blocking on rounds that were never cross-model. Treat it as no
                # counter → the next EXTERNAL mark re-establishes a clean streak (and
                # stamps last_source); an internal mark leaves it at 0. (Self-healing
                # mechanism — obviates a one-time per-install data repair.)
                if "round" in data and "last_source" not in data:
                    # The discard is about the COUNTER's provenance, so it must
                    # discard the counter and nothing else. A gate demand is not a
                    # counter — it records a decision the user was asked for, and its
                    # trustworthiness has nothing to do with which reviewer produced
                    # a round.
                    #
                    # Returning a bare {} here was a FULL DISARM reachable by deleting
                    # ONE key. MEASURED: on a real capped state, removing `last_source`
                    # made read_gate_demand AND gate_demand_present both report
                    # nothing, and an acked commit went from exit 2 to exit 0 — past
                    # the very split that exists so an unreadable demand cannot be
                    # mistaken for an absent one, because this branch runs BEFORE
                    # either reader sees the file.
                    preserved = data.get(_GATE_DEMAND_KEY)
                    return {_GATE_DEMAND_KEY: preserved} if isinstance(preserved, dict) else {}
                if "round" in data:
                    data["round"] = _coerce_finite_int(data.get("round"))
                return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def get_review_round(cwd: str | None = None) -> int:
    """Review rounds recorded for the CURRENT branch's change.

    Returns 0 when the stored count is for a different branch (a new change starts
    fresh) or when no count exists. Never raises.
    """
    state = _load_round(cwd)
    if not state or state.get("branch") != get_current_branch(cwd=cwd):
        return 0
    return _coerce_finite_int(state.get("round", 0))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file plus ``os.replace``.

    Copied in shape from ``scripts/lib/index_marker.py::_atomic_write_text``, which
    is this repo's stdlib-only precedent. Deliberately NOT
    ``genesis.util.atomic.atomic_write_text``: ``review_state`` is stdlib-only at
    module scope and is soft-imported by several hooks with fail-open fallbacks, so
    a ``src/genesis`` dependency here would silently disarm review-enforcement hooks
    on any host where ``genesis`` is not importable.

    The ``except BaseException`` covers the WRITE as well as the rename — a handler
    wrapped around only the rename leaves the temp when the write fails, which is
    the class ``scripts/check_atomic_writes.py`` exists to catch.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _write_round(state: dict, cwd: str | None = None, *, preserve_demand: bool = True) -> None:
    """Persist the round-counter state ATOMICALLY (best-effort — never raises).

    ``Path.write_text`` truncates and THEN writes, so a concurrent reader landing in
    that window reads an empty file; ``_load_round`` catches the ``JSONDecodeError``
    and returns ``{}``, which ``get_review_round`` reads as "no counter" — a silent
    FAIL-OPEN on the escalation cap, with no error and no retry. MEASURED under
    synthetic contention (2 writers, 3 readers): 67,187 torn reads of 100,016, against
    0 of 122,703 with no writer running. That is the rate GIVEN a read/write
    coincidence, NOT a production rate — how often a real read coincides with a real
    write is unmeasured. What it establishes is that the window is large relative to
    the write, so a coincidence very likely tears.

    WHAT THIS DOES NOT CLOSE, stated because the paragraph below would otherwise read
    as "the traffic is now safe": every demand write is a read-modify-write over the
    whole file, so a concurrent counter bump can still LOSE its increment to a
    recorder write. ``os.replace`` makes each write all-or-nothing; it does not
    SERIALISE two of them. The round file is keyed per worktree and normally has a
    single session writing it, so this is honest debt rather than an active defect —
    but it is debt, not a closed hole.

    Why that stops being academic HERE. The counter's writers are all RARE
    conditional paths (a mark, an ack, a final-accept), so coincidences were
    correspondingly rare. Gate DEMANDS are written on EVERY gate block and written
    back by the PostToolUse recorder, which turns a rare-path writer into a
    common-path one — and a torn demand read is a gate that silently stops demanding.
    The atomicity is therefore justified by the demand traffic this change adds, not
    retrofitted to the counter.
    """
    if preserve_demand and _GATE_DEMAND_KEY not in state:
        # CHOKEPOINT, not a convention. The round file carries TWO unrelated things:
        # counters, and a gate demand recording a decision the user was asked for.
        # Every counter-writer builds a FRESH dict literal — `bump_review_round`'s
        # branch-change path and `consume_final_accept` both did — so each would have
        # to REMEMBER to carry the demand, and a convention that several call sites
        # must remember is what reviewers find one instance of at a time. MEASURED:
        # a smoke test caught `bump_review_round` silently deleting a live demand,
        # and an AST sweep then found a second site nobody had looked at.
        #
        # So the obligation lives here, where it cannot be forgotten: a writer that
        # says nothing about the demand PRESERVES it. Dropping one is possible only
        # by asking for it explicitly, which is exactly what `retire_gate_demand`
        # does and nothing else should.
        existing = _load_round(cwd).get(_GATE_DEMAND_KEY)
        if isinstance(existing, dict):
            state = {**state, _GATE_DEMAND_KEY: existing}
    with contextlib.suppress(OSError):
        _atomic_write_text(_round_file(cwd), json.dumps(state, indent=2))


def bump_review_round(
    cwd: str | None = None, *, clean: bool = False, source: str = "internal"
) -> int:
    """Update the CROSS-MODEL defect-bearing-round streak for the current branch.

    ``source`` decides whether the mark counts at all — the streak exists to catch
    *cross-model non-convergence* (a non-Anthropic reviewer finding new defects
    round after round), NOT to penalize free same-model self-review:
      * ``source != "external"`` (the DEFAULT, i.e. an internal same-model review) —
        NEVER moves the streak: no increment, no reset, no ``last_hash`` write. Returns
        the current round unchanged. This is the core of the fix — an internal audit
        (even the one the mode-switch gate itself mandates) can never trip the cap.
      * ``source == "external"`` — a non-Anthropic cross-model reviewer:
        - ``clean=True`` RESETS the streak to 0 (circuit-breaker reset-on-success)
          regardless of whether the staged diff changed.
        - ``clean=False`` (defect-bearing) increments when the staged diff changed
          since the last counted mark on this branch; re-marking the SAME diff does
          not (idempotent); a branch change resets to 1.

    Best-effort — never raises into the caller.
    """
    # Internal (same-model) reviews never touch the cross-model streak. Only an
    # explicit external reviewer counts; any other value is treated as internal (the
    # safe direction — unknown provenance must not inflate the streak).
    if source != "external":
        return get_review_round(cwd=cwd)
    branch = get_current_branch(cwd=cwd)
    content_hash = _staged_content_hash(cwd=cwd)
    # A CLEAN round resets the streak unconditionally — the review declared the
    # current state defect-free, so no review→fix loop is in progress. Record the
    # current content hash so the NEXT defect-bearing mark on a distinct diff
    # correctly reads as a new (round-1) streak.
    # The lifetime count is carried through EVERY write below. Read once here, from
    # the same-branch state only — a counter belonging to another branch is a
    # different change and contributes nothing.
    _prev = _load_round(cwd)
    lifetime = (
        _coerce_finite_int(_prev.get("lifetime", 0))
        if _prev and _prev.get("branch") == branch
        else 0
    )
    # Carried through every write below for the same reason as `lifetime`: each
    # branch here REBUILDS the state dict, so a field that is not named again is
    # dropped. A spent acceptance that vanished on the next marked round would
    # hand the change a fresh one — reopening the terminal by simply running
    # another review, which is the loop this tier exists to end.
    consumed = (
        bool(_prev.get("final_accept_consumed"))
        if _prev and _prev.get("branch") == branch
        else False
    )
    if clean:
        # A clean round resets the STREAK but not the lifetime. A change that
        # alternates clean and defect-bearing rounds has still consumed every one
        # of them, and resetting here would hand it an unbounded budget by simply
        # converging occasionally.
        _write_round(
            {
                "branch": branch,
                "round": 0,
                "lifetime": lifetime,
                "last_hash": content_hash,
                "last_source": "external",
                "final_accept_consumed": consumed,
            },
            cwd,
        )
        return 0
    # Defect-bearing round. Nothing meaningfully staged ("clean") or a git error
    # ("unknown") is NOT a review round — counting it would inflate toward a
    # false-positive cap block (e.g. a mark run before the next fix is staged, or
    # a transient git timeout).
    if content_hash in ("clean", "unknown"):
        return get_review_round(cwd=cwd)
    state = _prev
    if not state or state.get("branch") != branch:
        state = {
            "branch": branch,
            "round": 1,
            "lifetime": 1,
            "last_hash": content_hash,
            "last_source": "external",
        }
    elif state.get("last_hash") != content_hash:
        # A distinct staged diff → new defect-bearing round. Coerce the stored
        # round defensively: a corrupt / partial-write / version-skewed value must
        # NOT raise here, or it would crash `mark` and leave the user stuck behind
        # the gate (this counter is best-effort — see the never-raises contract).
        # _coerce_finite_int also absorbs the `1e999`→inf→OverflowError value class.
        prev = _coerce_finite_int(state.get("round", 0))
        state = {
            "branch": branch,
            "round": prev + 1,
            "lifetime": lifetime + 1,
            "last_hash": content_hash,
            "last_source": "external",
            "final_accept_consumed": consumed,
        }
    # else: same branch + same staged diff → same round (idempotent re-mark).
    _write_round(state, cwd)
    return _coerce_finite_int(state.get("round", 0))


def get_final_accept_consumed(cwd: str | None = None) -> bool:
    """True once this branch has spent its ONE final-round acceptance.

    Branch-scoped on purpose: a global latch would permanently disarm the sigil
    for every later change on the install. Never raises — an unreadable or
    foreign-branch counter reads as "not consumed", which fails toward letting a
    genuine acceptance through rather than stranding a change behind a gate whose
    state it cannot see.
    """
    state = _load_round(cwd)
    if not state or state.get("branch") != get_current_branch(cwd=cwd):
        return False
    return bool(state.get("final_accept_consumed"))


def consume_final_accept(cwd: str | None = None) -> None:
    """Spend this branch's final-round acceptance. Best-effort, never raises.

    Recorded at the moment the gate ALLOWS the acked commit, because a PreToolUse
    hook has no post-execution callback — there is no later point at which to
    learn the commit succeeded. Burning it on allow is the deliberate direction:
    if the commit then fails for an unrelated reason the change is stuck, and at
    round seven "stuck" means "take it to the user", which is the intent.

    No-ops when no same-branch counter exists. That state is unreachable from the
    only caller — the terminal fires on ``lifetime >= FINAL_ROUND_CAP``, which
    requires exactly such a counter — so writing a synthetic one here could only
    invent a lifetime that was never counted.

    ONE-SHOT IS NOT A HARD GUARANTEE, and the limit is worth naming: both this and
    ``get_final_accept_consumed`` compare against ``get_current_branch``, which
    returns ``"unknown"`` on a git timeout or read error. A transient git failure
    therefore makes this a silent no-op and makes the read report "not consumed",
    refunding the acceptance. That is the correct direction for a best-effort
    counter that must never raise into a commit — it fails toward letting a real
    decision through rather than stranding a change behind state it cannot see —
    but nobody should read "one-shot" as tamper-proof.
    """
    branch = get_current_branch(cwd=cwd)
    state = _load_round(cwd)
    if not branch or not state or state.get("branch") != branch:
        return
    _write_round({**state, "final_accept_consumed": True}, cwd)


def get_review_lifetime(cwd: str | None = None) -> int:
    """Counted review rounds over this branch's WHOLE life. Never raises.

    Differs from ``get_review_round`` in exactly one way that matters: an
    ``# escalation-ack`` resets the consecutive streak and does NOT reset this.
    That asymmetry is the feature — the streak measures "is the current loop
    running long", this measures "has this change consumed more review than it
    is worth". Returns 0 for a different branch (a new change starts fresh) or
    a counter written before this field existed.
    """
    state = _load_round(cwd)
    if not state or state.get("branch") != get_current_branch(cwd=cwd):
        return 0
    return _coerce_finite_int(state.get("lifetime", 0))


def reset_review_round(cwd: str | None = None) -> None:
    """Reset the CONSECUTIVE streak, PRESERVING the lifetime count. Never raises.

    Deliberately a rewrite rather than the unlink this used to do. The only
    caller is the escalation-cap ack, whose purpose is to clear the streak so the
    next stop is a fresh cap away — but deleting the file would take the lifetime
    counter with it, and a terminal that its own ack erases is not a terminal.
    So: streak to 0, lifetime and branch carried forward.

    ``last_hash`` is dropped on purpose. It exists to make a re-mark of the SAME
    staged diff idempotent within a streak; once the streak is reset, the next
    defect-bearing mark must be free to count as round 1 whether or not the diff
    moved in between.

    ``last_source`` MUST be written even though this function does not record a
    review. ``_load_round`` discards any counter carrying ``round`` without
    ``last_source`` as a pre-source-axis legacy file — so omitting it here makes
    the very next read throw the whole counter away, silently taking the lifetime
    count with it and leaving the terminal unreachable. Only external rounds are
    ever counted, so a surviving lifetime implies external provenance.
    """
    state = _load_round(cwd)
    lifetime = _coerce_finite_int(state.get("lifetime", 0)) if state else 0
    branch = state.get("branch") if state else None
    # A gate demand lives on this same file and is NOT a counter — it records a
    # decision the user was asked for. Resetting the streak must never destroy it:
    # the ack that triggers this reset is itself granted BECAUSE a remedy was
    # chosen, so dropping the record here would erase the evidence that authorised
    # the very commit being allowed.
    demand = state.get(_GATE_DEMAND_KEY) if isinstance(state, dict) else None
    if not lifetime or not branch:
        # Nothing worth carrying — keep the original delete so a stale/foreign
        # counter is cleared outright rather than resurrected as an empty shell.
        # UNLESS a demand is riding along: unlink takes the whole file, so a branch
        # whose lifetime is still 0 would silently lose its demand here.
        if isinstance(demand, dict):
            _write_round({_GATE_DEMAND_KEY: demand}, cwd)
            return
        with contextlib.suppress(OSError):
            _round_file(cwd).unlink(missing_ok=True)
        return
    # The demand rides along via `_write_round`'s chokepoint — this function says
    # nothing about it, which is the point: counter-writers should not have to.
    _write_round(
        {
            "branch": branch,
            "round": 0,
            "lifetime": lifetime,
            "last_source": "external",
            # Carried for the same reason as `lifetime`: an escalation-ack resets
            # the streak, and must not also refund a spent final acceptance.
            "final_accept_consumed": bool(state.get("final_accept_consumed")),
        },
        cwd,
    )


# ─── Gate demands: the gate's own question, carried as DATA ──────────────────
#
# THE FOUNDING INCIDENT (2026-08-31). The escalation cap printed three remedies.
# The relay to the user DROPPED the first, INVENTED a fourth, and added "ship
# as-is" — the one outcome the cap exists to prevent. The gate had said the right
# thing; the agent relaying it had not.
#
# PR #1863 attacked that by VALIDATING the agent's AskUserQuestion options against
# a declared set. Four review rounds, 16 findings, 7 P1 — the largest class being
# "matching agent-authored option text", a coverage rule over an OPEN SET of words
# the agent chooses. Open sets do not converge; each named fix ships the next
# round's gap. Premise rejected.
#
# THIS IS THE INVERSION. The gate declares its remedies as DATA here; a PreToolUse
# hook APPENDS the gate's own question to whatever the agent asked, so the agent
# never authors those options and cannot drop, reword, negate or pad them. A
# PostToolUse recorder reads the user's chosen label out of the harness's own
# structured result and records it against the demand. The largest finding class
# dies structurally rather than by being defended better.
#
# WHAT IS DELIBERATELY NOT HERE, because #1863 proved both wrong:
#   * No matching against agent-authored option text, in any form.
#   * No transcript reading. The transcript records a tool call AS THE AGENT
#     EMITTED IT — substitution is applied afterwards — so a transcript-read
#     verification reads exactly the untrusted values it is trying to check.
#
# Stored on the EXISTING per-worktree round file rather than in a new store (the
# New-Store Gate): it is already per-worktree and per-branch, which is exactly the
# demand's scope. That choice is what makes `_write_round` atomic above.
_GATE_DEMAND_KEY = "gate_demand"

# Demand lifecycle. A demand is created by a BLOCK and dies with the branch.
_DEMAND_LIVE = "live"  # written by a block, not yet answered
_DEMAND_ANSWERED = "answered"  # the user chose a DECLARED remedy
_DEMAND_UNRECOGNISED = "unrecognised"  # an answer arrived, matched no remedy
_DEMAND_CONSUMED = "consumed"  # one authorised commit has been spent
_DEMAND_STATES = frozenset({_DEMAND_LIVE, _DEMAND_ANSWERED, _DEMAND_UNRECOGNISED, _DEMAND_CONSUMED})

# A free-text ("Other") answer is REJECTED above this length rather than cut. The
# bound is a resource guard on a file the commit gate reads on every commit, and it
# is derived from the protocol rather than from a corpus: AskUserQuestion free text
# is human-typed into a terminal prompt, so 4096 characters is far above any real
# answer and far below anything that matters to a per-commit read. Over it, the
# answer is recorded as UNRECOGNISED with an explicit marker saying so — never
# silently truncated, because a half-quoted answer read back to the user looks like
# the gate misunderstood them.
_MAX_ANSWER_TEXT = 4096
# Same shape, on the declaring side: a gate declaring an implausible number of
# remedies is a bug in the gate, not a menu to render. Both shipped tiers declare
# 4 and 2.
_MAX_REMEDIES = 12


# Kill switch. An env var for a one-off, and a marker FILE for a durable disable.
# A marker rather than a YAML config on purpose: both readers are stdlib-only, so
# neither can parse YAML, and a file whose mere existence is the signal has nothing
# to be malformed.
#
# It lives HERE, in the module both the hook and the commit gate already import,
# rather than in either one of them — two copies of a kill switch is replica drift
# on the one control that has to work when everything else does not.
#
# It disables ENFORCEMENT, not just the append. Disabling only the append would
# leave a live demand unsatisfiable and wedge the gate, which is the opposite of a
# recovery tool. Honest caveat: anything that can write this file can disable the
# mechanism, including the agent it constrains. It is a recovery tool, never
# insulation.
_GATE_ACK_DISABLED_ENV = "GENESIS_GATE_ACK_DISABLED"
_GATE_ACK_DISABLED_MARKER = ".genesis/config/gate_ack_disabled"


def gate_ack_disabled() -> bool:
    """True when the operator has turned gate-demand enforcement off. Never raises."""
    if os.environ.get(_GATE_ACK_DISABLED_ENV, "").strip() not in ("", "0", "false", "False"):
        return True
    try:
        return (Path.home() / _GATE_ACK_DISABLED_MARKER).exists()
    except OSError:
        return False


def _valid_remedy(entry: object) -> dict | None:
    """Return a normalised remedy dict, or None if ``entry`` is malformed.

    Validates by TYPE at the single read boundary and SKIPS anything malformed, so
    no persisted value can raise into a gate. ``key`` is required to be a ``str``
    because callers compare and render it; nothing here uses it AS a dict key, and an
    earlier version of this docstring cited that as the reason — a true fact about
    #1863 and a false claim about this code.

    ``required_action`` is NULLABLE by contract, not by accident — a TERMINAL remedy
    (hand back, shelve) exits with NO action, and one flat non-null field per demand
    would declare something false about it. Contract requirement from the peer
    session holding #1971, independently validated here.
    """
    if not isinstance(entry, dict):
        return None
    key = entry.get("key")
    label = entry.get("label")
    if not isinstance(key, str) or not key:
        return None
    if not isinstance(label, str) or not label:
        return None
    authorizes = entry.get("authorizes_commit")
    resets = entry.get("resets_streak")
    if not isinstance(authorizes, bool) or not isinstance(resets, bool):
        return None
    action = entry.get("required_action")
    if action is not None and not isinstance(action, str):
        return None
    description = entry.get("description")
    return {
        "key": key,
        "label": label,
        "description": description if isinstance(description, str) else "",
        "authorizes_commit": authorizes,
        "resets_streak": resets,
        "required_action": action,
    }


# The three outcomes of trying to resolve a demand. They are DISTINCT on purpose:
# only ABSENT may lead the commit gate to allow, and ABSENT means one thing only —
# no demand key in the file at all.
_DEMAND_ABSENT = "absent"
_DEMAND_UNREADABLE = "unreadable"
_DEMAND_USABLE = "usable"


def _resolve_gate_demand(cwd: str | None = None) -> tuple[str, dict | None]:
    """THE single decision point for "is there a demand, and can I use it?".

    ROBUST BY CONSTRUCTION, and that phrasing is the point rather than decoration.
    The generator behind more findings in this change than any other was "absent
    means ALLOW": `read_gate_demand` returned a bare None for many unrelated reasons,
    the commit gate read None as nothing-to-enforce, and every validation added later
    became one more way to disarm the gate. Three separate reviews found three
    separate axes of it — a corrupt `remedies` list, the legacy counter discard, and
    a branch recorded as "unknown" while git was down — and each was patched at its
    own axis. A fourth axis was always constructible, because the judgement lived in
    two functions that each re-derived it.

    So it lives HERE, once, and the SHAPE enforces the property. ABSENT is returned
    for exactly three conditions, and each is an intentional "this gate has nothing
    to say here" rather than a failure to read something: the kill switch is armed,
    there is no demand key at all, or the demand belongs to a DIFFERENT branch. They
    are all decided in the first half, before any field of the demand is inspected.

    Past that point — the VALIDATION half, below the marker comment — every exit is
    UNREADABLE. That is the invariant, and it is the one worth having: a validation
    added next year lands there by construction and can only refuse-and-name-the-exit,
    never allow. `test_no_validation_failure_can_make_a_demand_look_ABSENT` holds it
    by corrupting each field in turn rather than by trusting this paragraph.

    (An earlier draft of this docstring claimed "exactly ONE ABSENT return". That was
    false — there are three — and the useful property was never the count.)

    Never raises.
    """
    if gate_ack_disabled():
        # The recovery path, and the ONE deliberate exception to the rule above. An
        # operator who disarms the mechanism must get the pre-demand behaviour
        # wholesale; reporting "present" here would leave the switch unable to
        # unwedge anything, which is the opposite of a recovery tool.
        return _DEMAND_ABSENT, None

    state = _load_round(cwd)
    demand = state.get(_GATE_DEMAND_KEY) if isinstance(state, dict) else None
    if not isinstance(demand, dict):
        return _DEMAND_ABSENT, None

    # ── Past this line every exit is UNREADABLE. Do not add an ABSENT return. ──

    branch = demand.get("branch")
    if not isinstance(branch, str) or branch in ("", "unknown"):
        # Declared while git could not answer (`get_current_branch` returns the
        # literal "unknown" on a timeout or OSError). It cannot be attributed to a
        # branch, so it is not usable — but it is emphatically still THERE.
        return _DEMAND_UNREADABLE, None
    current = get_current_branch(cwd=cwd)
    if current == "unknown":
        return _DEMAND_UNREADABLE, None
    if branch != current:
        # A demand belonging to a DIFFERENT branch is the one case that is genuinely
        # nothing to do with here — a new change starts fresh, and an answer about a
        # design that no longer exists must not authorise a commit on the next one.
        return _DEMAND_ABSENT, None

    status = demand.get("state")
    gate = demand.get("gate")
    question = demand.get("question")
    if status not in _DEMAND_STATES:
        return _DEMAND_UNREADABLE, None
    if not isinstance(gate, str) or not gate:
        return _DEMAND_UNREADABLE, None
    if not isinstance(question, str) or not question:
        return _DEMAND_UNREADABLE, None

    raw = demand.get("remedies")
    remedies = [r for r in (_valid_remedy(e) for e in raw) if r] if isinstance(raw, list) else []
    if not remedies:
        # No INTELLIGIBLE remedy means nothing the user could choose, so it cannot be
        # answered. Refusing is right; the demand self-heals when the gate re-declares.
        return _DEMAND_UNREADABLE, None

    answered_with = demand.get("answered_with")
    answer_text = demand.get("answer_text")
    worktree = demand.get("worktree")
    return _DEMAND_USABLE, {
        "gate": gate,
        "tier": demand.get("tier") if isinstance(demand.get("tier"), str) else "",
        "branch": branch,
        "worktree": worktree if isinstance(worktree, str) else None,
        "question": question,
        "remedies": remedies,
        "state": status,
        "answered_with": answered_with if isinstance(answered_with, str) else None,
        "answer_text": answer_text if isinstance(answer_text, str) else None,
        "declared_at": _coerce_finite_int(demand.get("declared_at"), 0),
    }


def read_gate_demand(cwd: str | None = None) -> dict | None:
    """The USABLE gate demand for the current branch, or None. Never raises.

    None here means "not usable" and deliberately does NOT distinguish absent from
    unreadable — callers that need that distinction ask `gate_demand_present`, and
    both derive from the same resolution so they cannot disagree.

    Remedy ORDER is preserved exactly as declared. Nothing normalises to a dict or a
    set: the cap tier's first option is hand-back, a test pins that position, and a
    session takes the menu in the order the gate prints it.
    """
    _, demand = _resolve_gate_demand(cwd)
    return demand


def gate_demand_present(cwd: str | None = None) -> bool:
    """True when a demand is RECORDED for this branch, readable or not. Never raises.

    This is what stops "cannot read it" collapsing into "nothing to enforce". Derived
    from the same resolution as `read_gate_demand`, so the two cannot drift apart —
    which they could when each re-implemented the branch logic.
    """
    outcome, _ = _resolve_gate_demand(cwd)
    return outcome != _DEMAND_ABSENT


def find_session_gate_demand(cwd: str | None = None) -> dict | None:
    """The demand this SESSION should be asked about, wherever the gate keyed it.

    THE CLASS THIS CLOSES, which a narrower fix missed. The commit gate keys a demand
    under the COMMIT's effective directory — `git -C <dir>` and a trailing `cd` both
    win over the session's own cwd (`_effective_diff_cwd`). A reader that COMPUTES a
    key from the session cwd therefore looks in the wrong place whenever the session
    sits in one worktree and commits into another, which is an ordinary shape here
    because worktrees are mandated. MEASURED: session in wt1, `git -C wt2 commit`,
    demand written under wt2 — the ask hook keyed on wt1 found nothing, so the menu
    never rendered while the gate blocked every commit and told the session to ask.
    A permanent wedge whose only exit was the kill switch.
    (An earlier fix threaded the PAYLOAD cwd instead of the process cwd. That closed a
    different axis — and in practice not even a live one, since Claude Code spawns
    hooks with cwd == the project dir == the payload cwd — so it fixed the instance
    and left the class. This is the class.)

    So: look under this cwd first (the common case), then ENUMERATE. A candidate is
    only accepted when its recorded branch still matches the branch of its recorded
    worktree, which is what keeps a stale demand from another checkout out.
    Ambiguity resolves to None rather than to a guess: appending the wrong gate's
    menu would ask the user to decide something they are not being blocked on.
    """
    direct = read_gate_demand(cwd)
    if direct is not None:
        return direct
    if gate_ack_disabled():
        return None
    candidates: list[dict] = []
    try:
        files = sorted(_ROUND_DIR.glob("*.json"))
    except OSError:
        return None
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        demand = data.get(_GATE_DEMAND_KEY)
        if not isinstance(demand, dict):
            continue
        worktree = demand.get("worktree")
        if not isinstance(worktree, str) or not worktree:
            # A demand with no recorded root cannot be re-keyed, so it is not
            # enumerable. It is still found by the direct read above whenever the
            # session IS in its worktree, so this degrades to the pre-enumeration
            # behaviour rather than losing anything. Only reachable across a version
            # downgrade, since every demand this code writes records its root.
            continue
        found = read_gate_demand(worktree)
        if found is not None:
            candidates.append(found)
    return candidates[0] if len(candidates) == 1 else None


def _store_gate_demand(demand: dict, cwd: str | None = None) -> None:
    """Persist ``demand`` onto the round file without disturbing the counters."""
    state = _load_round(cwd)
    if not isinstance(state, dict):
        state = {}
    state[_GATE_DEMAND_KEY] = demand
    _write_round(state, cwd)


def write_gate_demand(
    *,
    gate: str,
    tier: str,
    question: str,
    remedies: list[dict],
    cwd: str | None = None,
) -> None:
    """Declare a gate's remedy set as data, at the moment the gate BLOCKS.

    Called from the block path, never speculatively. That is the whole enforcement
    scope: a demand exists only once this gate has ACTUALLY refused a commit, which
    is exactly the founding incident's shape. A session that was never blocked has
    nothing to relay, so its sigil keeps its old meaning and every existing sigil
    test is untouched. (The separate, PRE-EXISTING hole — appending the sigil before
    ever being blocked — is out of scope here and filed as an issue; closing it would
    rewrite ~11 existing tests inside a lane with a hard stop at 2 review rounds,
    which is the over-scope shape that killed #1863.)

    IDEMPOTENT on re-block: an existing demand for the SAME gate on this branch is
    left alone unless it is spent. Clobbering it would erase a recorded answer every
    time the gate re-printed its menu — the user would answer, the next blocked
    commit would rewrite the demand, and the answer would be gone.
    """
    valid = [r for r in (_valid_remedy(e) for e in remedies) if r]
    if not valid or len(valid) > _MAX_REMEDIES:
        # Refuse to write an unanswerable or implausible demand. The gate still
        # blocks — it simply does not gain a demand it could never satisfy.
        return
    if gate_ack_disabled():
        # Disarmed: do not record a demand at all. The tier still blocks on its own
        # rules; it simply does not accrue a demand nobody can satisfy.
        return
    # Idempotency reads the RAW key, not `read_gate_demand`. Going through the
    # filtered read meant that while the kill switch was armed the check saw None and
    # a block CLOBBERED an ANSWERED demand back to LIVE (MEASURED: answered/narrow ->
    # one disarmed block -> live/None), so an operator who disarmed and re-armed made
    # the user answer all over again — and the "IDEMPOTENT on re-block" promise below
    # was simply false in that window.
    existing = _load_round(cwd).get(_GATE_DEMAND_KEY)
    if (
        isinstance(existing, dict)
        and existing.get("gate") == gate
        and existing.get("branch") == get_current_branch(cwd=cwd)
        and existing.get("state") != _DEMAND_CONSUMED
        # ...and it must still be READABLE. Protecting an UNREADABLE demand from
        # being overwritten made corruption permanent: the gate refused (correctly,
        # since presence is separate from readability), pointed at
        # `retire-gate-demand`, and retire resets only state/answered_with — never
        # `question` or `remedies` — so the next block hit this idempotency check,
        # no-op'd, and the loop never terminated. The kill switch was the only exit.
        # MEASURED with `remedies` corrupted: refuse -> retire -> re-block -> still
        # unreadable, forever.
        #
        # Self-healing instead: an unreadable demand is replaced by the gate's own
        # current declaration. Note this cannot silently discard a decision — an
        # unreadable demand has no legible answer to discard, which is exactly why
        # it was wedging.
        and read_gate_demand(cwd) is not None
    ):
        return
    _store_gate_demand(
        {
            "gate": gate,
            "tier": tier,
            "branch": get_current_branch(cwd=cwd),
            # Recorded so a reader can RE-KEY from the demand instead of computing a
            # key from its own cwd -- see find_session_gate_demand.
            "worktree": _worktree_root(cwd) if cwd else _worktree_root(os.getcwd()),
            "question": question,
            "remedies": valid,
            "state": _DEMAND_LIVE,
            "answered_with": None,
            "answer_text": None,
            "declared_at": int(time.time()),
        },
        cwd,
    )


def record_gate_answer(*, question: str, label: str, cwd: str | None = None) -> str | None:
    """Record the user's answer to the gate's OWN question. Never raises.

    Returns the matched remedy key, ``"unrecognised"``, or None if nothing was
    recorded at all.

    Matching is exact containment of the QUESTION+LABEL PAIR, and both strings were
    written by the gate itself — a closed set, so this is not the open-set matching
    that failed. The pair rather than the bare label is deliberate: the agent knows
    our labels from the block message and could author its OWN question reusing
    them under a misleading description, so the gate's actual question must be the
    one that was answered.

    A non-matching answer ("Other", free text) records UNRECOGNISED rather than
    nothing. It still authorises no commit — but the next block can then say an
    answer arrived and quote it back, instead of telling someone who just answered
    that they were never asked.
    """
    demand = read_gate_demand(cwd)
    if not demand or demand["state"] in (_DEMAND_ANSWERED, _DEMAND_CONSUMED):
        # First answer wins. A later ask must not silently re-decide a demand the
        # user has already settled.
        return None
    if demand["question"] != question:
        return None
    stored = _load_round(cwd).get(_GATE_DEMAND_KEY)
    if not isinstance(stored, dict):
        return None
    for remedy in demand["remedies"]:
        if remedy["label"] == label:
            stored["state"] = _DEMAND_ANSWERED
            stored["answered_with"] = remedy["key"]
            stored["answer_text"] = label
            _store_gate_demand(stored, cwd)
            return remedy["key"]
    stored["state"] = _DEMAND_UNRECOGNISED
    stored["answered_with"] = None
    stored["answer_text"] = (
        label
        if isinstance(label, str) and len(label) <= _MAX_ANSWER_TEXT
        else "<answer rejected: longer than the gate stores>"
    )
    _store_gate_demand(stored, cwd)
    return _DEMAND_UNRECOGNISED


def consume_gate_demand(cwd: str | None = None) -> None:
    """Spend an ANSWERED demand's single authorisation. Never raises.

    One-shot by design: the answer authorises THE commit that was blocked, not the
    branch's remaining life. The gate re-demands on the next block.
    """
    stored = _load_round(cwd).get(_GATE_DEMAND_KEY)
    if not isinstance(stored, dict) or stored.get("state") != _DEMAND_ANSWERED:
        return
    stored["state"] = _DEMAND_CONSUMED
    _store_gate_demand(stored, cwd)


def retire_gate_demand(cwd: str | None = None) -> bool:
    """Drop the current demand so the gate can ask again. Returns True if one went.

    The documented exit from a TERMINAL answer. Choosing hand-back or shelve blocks
    the branch permanently and that is the point — but a decision made on one day's
    understanding must not be unappealable, so this is the deliberate re-ask: the
    demand is dropped, the gate re-demands on its next block, and the user chooses
    again with the menu in front of them.

    Deliberately an OPERATOR command and not an automatic path. It is reachable by
    anything that can write this file, which is the same honest caveat the kill
    switch carries: this is a RECOVERY tool, not insulation. What it cannot do is
    happen silently — it drops the recorded answer, so the next block prints the
    full menu again rather than quietly proceeding.
    """
    state = _load_round(cwd)
    demand = state.get(_GATE_DEMAND_KEY) if isinstance(state, dict) else None
    if not isinstance(demand, dict):
        return False
    # BRANCH-SCOPED, because its reader is. Popping unconditionally meant running
    # this on one branch destroyed another branch's live demand while
    # `gate-demand` on that same branch correctly reported none — read and write
    # disagreeing about scope, with a success message asserting an effect on the
    # wrong branch.
    if demand.get("branch") != get_current_branch(cwd=cwd):
        return False
    # RESET, never DELETE. Deleting was a complete bypass and the worst kind: the
    # block message advertises this command as the appeal route from a TERMINAL
    # choice, so the gate itself handed the agent the override. MEASURED —
    # hand-back answered, acked commit refused (2), retire, SAME acked commit -> 0.
    # The CLI even printed "until then nothing is authorised" while authorising it.
    #
    # Resetting to LIVE delivers exactly what the docstring promises — the gate puts
    # the full menu up again — while the commit stays blocked until a NEW answer
    # lands. An appeal re-opens the question; it does not decide it.
    demand["state"] = _DEMAND_LIVE
    demand["answered_with"] = None
    demand["answer_text"] = None
    state[_GATE_DEMAND_KEY] = demand
    # `preserve_demand=False` because this call IS the deliberate write of the
    # demand key (see the chokepoint note in `_write_round`).
    _write_round(state, cwd, preserve_demand=False)
    return True


def get_current_branch(cwd: str | None = None) -> str:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=cwd,
        )
        return result.stdout.strip() or "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "Usage: review_state.py "
            "[status|mark|diff-hash|evidence-path|gate-demand|retire-gate-demand]",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "gate-demand":
        demand = read_gate_demand()
        if not demand:
            print("no gate demand on this branch")
            return
        print(f"gate:     {demand['gate']} ({demand['tier']})")
        print(f"state:    {demand['state']}")
        print(f"question: {demand['question']}")
        if demand["answered_with"]:
            print(f"answered: {demand['answered_with']}")
        elif demand["answer_text"]:
            print(f"answered: <no declared remedy matched> {demand['answer_text']}")
        print("remedies (declaration order):")
        for remedy in demand["remedies"]:
            authorises = "authorises a commit" if remedy["authorizes_commit"] else "TERMINAL"
            action = remedy["required_action"] or "no action"
            print(f"  - {remedy['label']}  [{authorises}; {action}]")
        return

    if cmd == "retire-gate-demand":
        # The documented re-ask. Choosing a TERMINAL remedy blocks the branch and
        # that is the point — but a decision made on one day's understanding must
        # not be unappealable, so this drops the demand and the gate puts the full
        # menu in front of the user again on its next block.
        if retire_gate_demand():
            print(
                "gate demand re-opened. Any recorded answer has been cleared and the "
                "demand is LIVE again, so the gate will put its full menu in front of "
                "you on the next ask. The commit stays BLOCKED until a new answer is "
                "recorded — re-opening the question does not decide it."
            )
        else:
            print("no gate demand on this branch — nothing to re-open")
        return

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
        # outcome (--clean / --defects). The escalation streak is CROSS-MODEL only, so
        # --source decides whether the outcome is even consulted:
        #   * --source internal (the DEFAULT) — a same-model self/subagent review. It
        #     never moves the streak, so the outcome flag is OPTIONAL and ignored; a
        #     bare `mark` is a valid internal review (it cannot inflate anything).
        #   * --source external — a non-Anthropic cross-model reviewer. This is the only
        #     kind that counts, so it REQUIRES exactly one outcome flag (--clean XOR
        #     --defects). The requirement is FAIL-CLOSED: a refusal writes NO marker, so
        #     the commit gate still blocks until the caller re-runs correctly.
        # (Supersedes feea3f71/#1446 — its unconditional required-outcome only ever
        # bit internal re-audits, which now can't inflate the streak regardless.)
        agent_path = None
        saw_clean = False
        saw_defects = False
        source = "internal"
        # Normalize `--key=value` into `--key value` first, so the conventional equals form is
        # parsed too. A split-token-only parser silently dropped `--source=external`, recording
        # a cross-model review as INTERNAL (an external round then wrote a marker without
        # advancing the cap). dont_hand_roll_cli_parsing_in_hooks — bind atomically. (Codex P2.)
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
                # recorded internal and never advances the cap. Refuse so the caller re-runs
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
                "Internal = a same-model self/subagent review (never counts); external = "
                "a non-Anthropic cross-model reviewer (Codex/Kimi/…) whose findings drove "
                "this round.",
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
                "P1/P2 → resets the streak) or --defects (it found one → advances the "
                "streak). The outcome is what moves the cross-model escalation counter; "
                "an internal mark doesn't need it.",
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
