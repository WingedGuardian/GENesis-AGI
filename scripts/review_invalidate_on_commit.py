#!/usr/bin/env python3
"""PostToolUse hook (Bash): invalidate review marker after successful git commit.

Every commit must be preceded by a fresh review. This hook clears the marker
after any successful git commit, so the next commit will require review again.

Reads the CC PostToolUse payload from stdin (via hook_input) — the git
command from tool_input, the outcome from tool_response.

Marker resolution (the dir whose per-worktree marker this commit cleared) MUST
match the dir the PreToolUse checker (``review_enforcement_commit``) validated,
or a stale marker survives and a later ``git add && git commit`` chain sails
through the existence-only gate on a review that no longer applies.

The two hooks see DIFFERENT cwd semantics, so they resolve differently (verified
by a live probe 2026-07-30 — ``process_cwd == payload cwd`` in every sample):
  - PreToolUse (checker): payload ``cwd`` is PRE-execution, so it walks the
    command's ``cd``s onto that base (``_effective_diff_cwd``).
  - PostToolUse (here): payload ``cwd`` is POST-execution — it ALREADY reflects
    every ``cd`` the command ran — so we take it as-is and only adjust for a
    ``git -C`` on the commit segment (which redirects git without moving the
    shell). Re-walking the command's ``cd``s here (i.e. sharing
    ``_effective_diff_cwd`` verbatim) would DOUBLE-APPLY a relative ``cd`` and
    resolve a nonexistent dir → the ``"default"`` key → the stale-marker bypass
    this hook exists to prevent.

Exit codes:
  0 = always (PostToolUse hooks cannot block)
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import field, read_payload, tool_response  # noqa: E402

# review_state lives in scripts/ (this file's own dir).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_state import clear_marker  # noqa: E402

# Commit DETECTION uses shell_parse (the same precise `analyze()` the checker
# uses) so a loose "commit" token match doesn't clear on a non-commit that merely
# mentions the word. Commit-DIR resolution additionally needs the checker's cwd
# primitives, imported (not copied) so the pair can never drift:
#   _seg_dash_C   — the `git -C <dir>` on an argv (argv parse)
#   _resolve_against — resolve a dir against a base to an absolute path
#   _cd_target    — classify a segment as a cd (target / _CWD_UNKNOWN / None)
#   _CWD_UNKNOWN  — the "cannot resolve confidently" sentinel
# Both are guarded: a broken import must never crash the hook (a crash clears
# NOTHING → every marker stays valid for its TTL = the bypass). Detection degrades
# to a strict regex; resolution degrades to clearing the candidate set.
try:
    from shell_parse import analyze, git_subcommand  # noqa: E402

    _PARSE_OK = True
except Exception:  # pragma: no cover - defensive
    _PARSE_OK = False

try:
    from review_enforcement_commit import (  # noqa: E402
        _CWD_UNKNOWN,
        _cd_target,
        _effective_diff_cwd,
        _resolve_against,
        _seg_dash_C,
    )
    from shell_parse import split_segments  # noqa: E402

    _RESOLVER_OK = _PARSE_OK
except Exception:  # pragma: no cover - defensive
    _RESOLVER_OK = False
    # Bind the one name used on a path reachable when the import fails (_over_clear),
    # so it is always defined; its call is still gated behind _RESOLVER_OK.
    _effective_diff_cwd = None

# Strict fallback commit-detector for the (rare) case shell_parse is unavailable —
# matches the pre-broadening behavior so a bare "commit" mention can't trigger a
# clear when we cannot parse. `git -C` forms are then missed. Note this is a
# degraded regime, not a safe one: the CHECKER imports shell_parse UNGUARDED, so
# the same failure makes it crash (a non-blocking hook error → commits proceed)
# and go inert — the review gate as a whole is already compromised, so the
# invalidator's own conservative fallback here neither adds nor removes exposure.
# (Hardening the checker to fail-closed on that import is a separate follow-up.)
_STRICT_COMMIT = re.compile(r"\bgit\s+commit\b")

# Cheap early-out on the "commit" token — NOT a rigid "git commit" adjacency,
# which misses `git -C <dir> commit` / `git -c k=v commit` (see the matching
# comment in review_enforcement_commit.py). The pair MUST detect the same commit
# set: if the invalidator early-exits on a form the checker gates, the checked
# marker is never cleared → a later commit reuses the stale review.
_COMMIT_PATTERN = re.compile(r"\bcommit\b")


def _extract_working_dir(command: str) -> str | None:
    """The dir named by a leading ``cd <path> && ...`` (legacy signal).

    Retained as one member of the ambiguity over-clear set: it is the only
    resolver that recovers the ``cd W && git commit && cd Z`` case (commit runs
    in W, but the post-execution cwd is Z), where W is this leading ``cd``.
    """
    m = re.match(r"^cd\s+([^\s&|;]+)", command)
    if not m:
        return None
    path = os.path.expanduser(m.group(1))
    return path if os.path.isdir(path) else None


def _payload_cwd(payload: dict) -> str | None:
    """The Bash tool's POST-execution working directory from the payload."""
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if isinstance(cwd, str) and cwd:
        return os.path.normpath(cwd)
    return None


def _effective_clear_cwd(command: str, payload: dict, segs: list):
    """The dir whose marker this commit cleared — POST-execution aware.

    Returns an absolute ``str`` dir, or ``_CWD_UNKNOWN`` when the post-execution
    cwd cannot be trusted to be the commit's dir (a ``cd`` AFTER the commit
    segment, or a commit nested in a subshell/``bash -c``). ``_CWD_UNKNOWN``
    tells the caller to over-clear the candidate set (fail toward clearing).
    """
    commit_seg = next((s for s in segs if git_subcommand(s.argv) == "commit"), None)
    if commit_seg is None:
        return _CWD_UNKNOWN
    if getattr(commit_seg, "depth", 0) > 0:
        # Nested (subshell / bash -c): the top-level post cwd need not reflect
        # the nested scope's cd. Over-clear rather than trust it.
        return _CWD_UNKNOWN

    base = _payload_cwd(payload)

    # A top-level `cd` AFTER the commit segment moves the post cwd DOWNSTREAM of
    # where the commit actually ran, so `base` is no longer the commit's dir.
    target_raw = getattr(commit_seg, "raw", None)
    seen_commit = False
    for raw in split_segments(command):
        if not seen_commit:
            if raw == target_raw:
                seen_commit = True
            continue
        if _cd_target(raw) is not None:  # any cd (resolvable or UNKNOWN) after commit
            return _CWD_UNKNOWN

    # `git -C <dir>` redirects git WITHOUT moving the shell, so the post cwd
    # (`base`) is not the commit's dir — resolve the -C target against it.
    dash_c = _seg_dash_C(getattr(commit_seg, "argv", None))
    if dash_c is not None:
        return _resolve_against(base, dash_c)
    return base


def _over_clear(command: str, payload: dict, segs: list | None = None) -> None:
    """Clear every candidate marker key (each a no-op if that key has no marker).

    Used when resolution is ambiguous or the shared resolver is unavailable.
    Over-clearing only forces a redundant re-review; under-clearing leaves a
    stale marker — the bypass — so the safe direction is to clear more.

    Candidates: the post-execution payload cwd, the legacy leading-``cd`` dir,
    ``None`` (the hook process cwd, which real CC keeps == payload cwd), and —
    when ``segs`` is available — the checker's OWN walk-based resolution
    (``_effective_diff_cwd``). The last candidate is what covers a contrived
    ``cd A && … ; cd W && git commit && cd Z`` form: the trailing ``cd Z`` makes
    the post cwd (Z) and the leading ``cd`` (A) both miss the real commit dir W,
    but the checker's walk lands on W — so including it clears the marker the
    checker actually validated. It can over-resolve a relative-``cd`` command
    (post-execution base), but that only adds a harmless extra clear, never an
    under-clear. Clearing an uninvolved worktree's marker at worst forces that
    session a redundant review; it never authorizes an unreviewed commit.
    """
    candidates = {_payload_cwd(payload), _extract_working_dir(command), None}
    if segs is not None and _RESOLVER_OK:
        walked = _effective_diff_cwd(command, payload, segs)
        if isinstance(walked, str):
            candidates.add(walked)
    for cwd in candidates:
        clear_marker(cwd=cwd)


def main() -> None:
    payload = read_payload()

    # Cheap early-out: no "commit" token anywhere → definitely not a commit.
    command = field(payload, "command")
    if not _COMMIT_PATTERN.search(command):
        sys.exit(0)

    # Confirm a REAL executed `git commit` segment before clearing anything — the
    # loose token match also hits "commit" in a filename / message / a non-commit
    # git subcommand (`commit-tree`), none of which should invalidate a review.
    # This mirrors the checker's post-early-out `analyze()` confirmation.
    segs = analyze(command) if _PARSE_OK else None
    if segs is not None:
        if not any(git_subcommand(s.argv) == "commit" for s in segs):
            sys.exit(0)
    elif not _STRICT_COMMIT.search(command):
        sys.exit(0)  # no parser: fall back to strict adjacency, never over-clear

    # Only invalidate on successful commits (exit code 0)
    try:
        result = tool_response(payload)
        # CC wraps Bash results — check for error indicators
        _stdout = result.get("stdout", "")  # noqa: F841
        _stderr = result.get("stderr", "")  # noqa: F841
        # A successful git commit prints to stdout with the branch and hash
        # A failed commit (e.g. pre-commit hook) has non-zero exit
        if "error" in result and result["error"]:
            sys.exit(0)  # Commit failed, don't invalidate
    except (json.JSONDecodeError, AttributeError):
        pass  # Can't parse result — be conservative, invalidate anyway

    # Clear the per-worktree review marker (matching the dir that authorized this
    # commit) so the next commit requires a fresh review. Resolution mirrors the
    # PreToolUse checker; ambiguity or a missing resolver over-clears.
    if not _RESOLVER_OK or segs is None:
        _over_clear(command, payload)
        sys.exit(0)

    eff = _effective_clear_cwd(command, payload, segs)
    if eff is _CWD_UNKNOWN:
        _over_clear(command, payload, segs)  # segs → include the checker's walk dir
    else:
        clear_marker(cwd=eff)

    sys.exit(0)


if __name__ == "__main__":
    main()
