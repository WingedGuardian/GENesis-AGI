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
    python3 review_state.py mark           # evidence read from the per-worktree path
    python3 review_state.py mark --agent-output <path>          # explicit override
    python3 review_state.py mark --agent-output <path> --clean  # clean round → reset streak
    python3 review_state.py diff-hash      # prints current diff hash
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

_MARKER_DIR = Path.home() / ".genesis" / "review_markers"
# Per-worktree review-ROUND counter (escalation cap). Deliberately a SEPARATE store
# from the marker above: review_invalidate_on_commit clears the marker after every
# commit, but the round count must SURVIVE commits — a review→fix→re-review loop
# commits between rounds, and the whole point is to notice when that loop runs long.
_ROUND_DIR = Path.home() / ".genesis" / "review_rounds"
# After this many review rounds on one change, the commit gate blocks pending an
# explicit '# escalation-ack'. Mirrors the genesis-development SKILL.md prose cap.
ESCALATION_ROUND_CAP = 3
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
) -> bool:
    """Write the per-worktree review marker after verifying evidence.

    The authoritative gate is Check 2 (code-reviewer agent output). Check 1
    (gstack telemetry) is advisory — it annotates the marker but never refuses,
    because gstack is not installed on many hosts (see ``_verify_review_log``).

    ``clean`` records the OUTCOME of the review for the escalation counter: pass
    ``clean=True`` when the review surfaced NO new BLOCKER/SHOULD-FIX/P1/P2
    finding — it resets the defect-bearing-round streak (circuit-breaker
    reset-on-success). The default (``clean=False``) counts the round as
    defect-bearing. See ``bump_review_round``.

    Returns True if the marker was written, False if the authoritative evidence
    check failed.
    """
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
        round_n = bump_review_round(cwd=cwd, clean=clean)
    except Exception:  # noqa: BLE001 - the counter must never block marking a review
        round_n = 0
    label = "clean → streak reset" if clean else f"round {round_n}"
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
    0 of 45,358 real commands, so the redundant-review cost is theoretical and the
    bypass it closes is not.

    Never raises: invalidation runs post-commit, and a crash here would leave the
    stale marker this exists to remove.
    """
    cleared = 0
    failures: list[str] = []

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
            return 0, [f"{_MARKER_DIR}: unreadable, so surviving markers cannot be seen"]
        return 0, []  # never created: genuinely nothing to clear

    for path in _MARKER_DIR.glob("*.json"):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            failures.append(f"{path}: {exc}")
        else:
            cleared += 1

    # Counted only when it actually existed: `missing_ok=True` succeeds on an absent
    # file, so counting it unconditionally reported "cleared 1" against an empty — or
    # entirely missing — marker directory, and the operator-facing message repeats
    # that number.
    if _LEGACY_STATE_FILE.exists():
        try:
            _LEGACY_STATE_FILE.unlink()
        except OSError as exc:
            failures.append(f"{_LEGACY_STATE_FILE}: {exc}")
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


def _write_round(state: dict, cwd: str | None = None) -> None:
    """Persist the round-counter state (best-effort — never raises)."""
    try:
        rf = _round_file(cwd)
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def bump_review_round(cwd: str | None = None, *, clean: bool = False) -> int:
    """Update the defect-bearing-round streak for the current branch; return it.

    ``clean=True`` — the review found no new BLOCKER/SHOULD-FIX/P1/P2 finding —
    RESETS the streak to 0 (circuit-breaker reset-on-success) and returns 0,
    regardless of whether the staged diff changed (a clean re-probe of the same
    diff still closes the breaker).

    ``clean=False`` (default, a defect-bearing round) increments when the staged
    diff changed since the last counted mark on this branch; re-marking the SAME
    diff does not (idempotent); a branch change resets to 1.

    Best-effort — never raises into the caller.
    """
    branch = get_current_branch(cwd=cwd)
    content_hash = _staged_content_hash(cwd=cwd)
    # A CLEAN round resets the streak unconditionally — the review declared the
    # current state defect-free, so no review→fix loop is in progress. Record the
    # current content hash so the NEXT defect-bearing mark on a distinct diff
    # correctly reads as a new (round-1) streak.
    if clean:
        _write_round({"branch": branch, "round": 0, "last_hash": content_hash}, cwd)
        return 0
    # Defect-bearing round. Nothing meaningfully staged ("clean") or a git error
    # ("unknown") is NOT a review round — counting it would inflate toward a
    # false-positive cap block (e.g. a mark run before the next fix is staged, or
    # a transient git timeout).
    if content_hash in ("clean", "unknown"):
        return get_review_round(cwd=cwd)
    state = _load_round(cwd)
    if not state or state.get("branch") != branch:
        state = {"branch": branch, "round": 1, "last_hash": content_hash}
    elif state.get("last_hash") != content_hash:
        # A distinct staged diff → new defect-bearing round. Coerce the stored
        # round defensively: a corrupt / partial-write / version-skewed value must
        # NOT raise here, or it would crash `mark` and leave the user stuck behind
        # the gate (this counter is best-effort — see the never-raises contract).
        # _coerce_finite_int also absorbs the `1e999`→inf→OverflowError value class.
        prev = _coerce_finite_int(state.get("round", 0))
        state = {"branch": branch, "round": prev + 1, "last_hash": content_hash}
    # else: same branch + same staged diff → same round (idempotent re-mark).
    _write_round(state, cwd)
    return _coerce_finite_int(state.get("round", 0))


def reset_review_round(cwd: str | None = None) -> None:
    """Delete the round counter (e.g. after the change lands). Never raises."""
    import contextlib

    with contextlib.suppress(OSError):
        _round_file(cwd).unlink(missing_ok=True)


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
        # Parse --agent-output <path> and the optional --clean flag.
        agent_path = None
        clean = False
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--agent-output" and i + 1 < len(sys.argv):
                agent_path = sys.argv[i + 1]
            elif arg == "--clean":
                clean = True
        if not mark_reviewed(agent_path, clean=clean):
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
