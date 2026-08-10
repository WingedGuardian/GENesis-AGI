#!/usr/bin/env python3
"""CI review-depth advisory: recompute substantiality on the PR range and LOUDLY
flag a substantial change so the human reviewer + the independent cloud reviewer
apply adversarial depth.

Advisory BY DESIGN. On a single-user-authored repo a committed audit artifact is
forgeable and a local hook is editable by the same actor, so this check does NOT
verify audit content and does NOT fail the build on its own. The enforcing teeth
live where the PR author has no control: the independent cloud reviewer (Codex) +
a REQUIRED human approval, gated by branch protection. This check's job is
VISIBILITY — make "this is substantial → it needs an adversarial /review-level
audit, not a lean pass" impossible to miss on the PR.

Exit 0 ALWAYS (a fail-open advisory — an unexpected error must never fail the
build). Emits a GitHub Actions ``::warning::`` annotation when the PR range
classifies substantial, AND — critically — a DISTINCT warning when the range can't
be computed (``unknown``), so a git error never masquerades as clearance. Reuses
``review_scope`` (the SAME predicate as the commit gate) over ``base...HEAD``.
"""

from __future__ import annotations

import os
import subprocess
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


def _ref_exists(ref: str, cwd: str | None) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _resolve_base(cwd: str | None = None) -> str | None:
    """The base for the PR range. Prefer the event base SHA (``PR_BASE_SHA``, set from
    ``github.event.pull_request.base.sha`` — robust vs whether ``origin/*`` tracking refs
    are materialized, matching the hardened ``leak-detector`` job; a SHA is trusted-format,
    not injectable). Fall back to ``origin/$GITHUB_BASE_REF`` then origin/main|master.
    Returns a ref/SHA or None (→ caller skips, advisory)."""
    sha = os.environ.get("PR_BASE_SHA", "").strip()
    if sha and _ref_exists(sha, cwd):
        return sha
    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    candidates = ([f"origin/{base_ref}"] if base_ref else []) + ["origin/main", "origin/master"]
    for cand in candidates:
        if _ref_exists(cand, cwd):
            return cand
    return None


def _run(cwd: str | None) -> int:
    from review_scope import classify_range_substantiality

    base = _resolve_base(cwd)
    if not base:
        print("review-depth-check: no resolvable base — skipping (advisory).")
        return 0
    level = classify_range_substantiality(base, cwd=cwd)
    if level == "substantial":
        msg = (
            "SUBSTANTIAL change: this PR needs an ADVERSARIAL /review-level audit "
            "(assume bugs, enumerate the edge/boundary/sentinel/hierarchy class, read "
            "authoritative semantics) — NOT a precision-filtered lean pass. A 'no "
            "findings' inline review is false confidence, not clearance. Confirm the "
            "independent reviewer (Codex) engaged and a human approved with depth."
        )
        print(f"::warning title=Review depth required::{msg}")
        print(f"review-depth-check: SUBSTANTIAL (base {base}) — advisory, see annotation.")
    elif level == "unknown":
        # A git error / unreachable merge-base must NEVER read as clearance (that is the
        # exact silent-false-inline this check exists to prevent). Fail LOUD, distinctly.
        print(
            "::warning title=Review depth: range not computable::Could NOT compute the PR "
            f"range ({base}...HEAD) — do NOT treat this as clearance; classify depth "
            "manually. (Is fetch-depth:0 set and the base reachable?)"
        )
        print(f"review-depth-check: UNKNOWN (base {base}) — range not computable.")
    else:
        print(f"review-depth-check: {level} (base {base}) — no adversarial-depth requirement.")
    return 0


def main(cwd: str | None = None) -> int:
    try:
        return _run(cwd)
    except Exception as e:  # noqa: BLE001 - fail-open advisory: never fail the build
        # An unexpected import/classifier error must NOT read as clearance: a green job
        # with no annotation is INDISTINGUISHABLE from "not substantial", the exact
        # silent-false-inline this check exists to surface. Emit the same DISTINCT
        # ::warning:: as an uncomputable range before exiting 0.
        print(
            "::warning title=Review depth: check errored::review-depth-check could NOT "
            f"run ({e}) — do NOT treat this as clearance; classify depth manually."
        )
        print(f"review-depth-check: skipped (unexpected error: {e}).")
        return 0


if __name__ == "__main__":
    sys.exit(main())
