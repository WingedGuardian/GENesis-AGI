#!/usr/bin/env python3
"""CI leak-scan range selector — emits the ADDED lines a PR/push introduces.

Feeds ``scripts/ci/private_pattern_scan.py``. The RANGE is the security-critical
part: it must scan exactly the commits this PR/push authored, never main's own
history.

Why this exists (the fail-BLOCK the former inline range hit):
  CI checks out the PR **merge ref** (``merge(main@ci, PR-head)``) for a
  ``pull_request`` event, so ``HEAD``'s first parent IS current main. The old
  range ``${base.sha}..HEAD`` used ``github.event.pull_request.base.sha``, which
  is frozen at PR creation and lags main. For a PR opened before some private
  value was remediated on main, ``base.sha..HEAD`` re-swept every main commit
  since base.sha and re-flagged a value main itself *added then removed* (the
  add commit's patch still shows it) — false-blocking a clean PR. Anchoring on
  the merge ref's first parent (``HEAD^1`` = ``main@ci``) scans the PR's own
  commits only. A value the PR *introduces* still lives in a commit reachable
  from HEAD but not from main, so it is always in range — the gate is not
  weakened. The deliberate per-commit add-then-remove-WITHIN-a-PR detection is
  preserved (all PR-own commits stay in range; ``--no-merges`` walks each).

CONTRACT:
  stdout  the added ('^+') lines across the range's non-merge commit patches
          (kept verbatim, INCLUDING '+++ b/path' headers — repo-relative paths
          never match an install value, and a '+++' filter once dropped added
          content beginning '++')
  exit 0  range resolved and emitted (clean OR with content — the downstream
          private_pattern_scan decides leak vs clean)
  exit 3  range UNRESOLVABLE — fail-LOUD; never emit empty (that would be a
          fail-OPEN, silently passing the gate)
"""

from __future__ import annotations

import os
import subprocess
import sys

EXIT_OK = 0
EXIT_UNRESOLVABLE = 3


class RangeError(RuntimeError):
    """The scan range cannot be resolved — the gate must fail closed."""


def _git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing text output. Never raises on nonzero."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def head_parent_count(cwd: str | None = None) -> int:
    """Number of parents of HEAD (2+ ⇒ a merge commit / the PR merge ref)."""
    cp = _git(["rev-list", "--parents", "-n", "1", "HEAD"], cwd)
    if cp.returncode != 0 or not cp.stdout.strip():
        raise RangeError("cannot read HEAD parents")
    # Output: "<commit> <parent1> [<parent2> ...]" — parents = words - 1.
    return len(cp.stdout.split()) - 1


def _commit_exists(ref: str, cwd: str | None = None) -> bool:
    return bool(ref) and _git(["cat-file", "-e", f"{ref}^{{commit}}"], cwd).returncode == 0


def resolve_scan_spec(
    event_name: str,
    push_before: str,
    head_sha: str,
    cwd: str | None = None,
) -> tuple[str, str]:
    """Return the scan spec as ``(kind, value)``.

    ``("range", "A..B")`` → scan ``git log -p --no-merges A..B``.
    ``("show", "<sha>")`` → scan a single commit's patch (new branch fallback).
    Raises :class:`RangeError` when the range cannot be resolved (fail closed).
    """
    if event_name == "pull_request":
        # HEAD is normally the merge ref; its first parent is main@ci.
        if head_parent_count(cwd) >= 2:
            return ("range", "HEAD^1..HEAD")
        # Unmergeable PR (no merge ref → PR head checked out): anchor on live
        # main via merge-base. Fetch is best-effort; a full checkout already has
        # origin/main under fetch-depth:0.
        _git(["fetch", "--no-tags", "--quiet", "origin", "main"], cwd)
        mb = _git(["merge-base", "origin/main", "HEAD"], cwd)
        base = mb.stdout.strip()
        if mb.returncode != 0 or not base:
            raise RangeError("pull_request: no merge ref and merge-base(origin/main, HEAD) failed")
        return ("range", f"{base}..HEAD")

    if _commit_exists(push_before, cwd):
        # Push with a known previous tip — scan only the newly pushed commits.
        return ("range", f"{push_before}..{head_sha or 'HEAD'}")

    # New branch / unknown base — scan the tip commit's patch (best effort).
    return ("show", head_sha or "HEAD")


def added_lines(spec: tuple[str, str], cwd: str | None = None) -> str:
    """Emit the '^+' lines for a scan spec. Raises :class:`RangeError` on git failure."""
    kind, value = spec
    if kind == "range":
        cp = _git(["log", "-p", "--no-merges", value], cwd)
    else:
        cp = _git(["show", value], cwd)
    if cp.returncode != 0:
        raise RangeError(f"git {kind} {value!r} failed (rc={cp.returncode})")
    return "\n".join(line for line in cp.stdout.splitlines() if line.startswith("+"))


def main(argv: list[str] | None = None) -> int:
    event_name = os.environ.get("EVENT_NAME", "")
    push_before = os.environ.get("PUSH_BEFORE", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    try:
        spec = resolve_scan_spec(event_name, push_before, head_sha)
        out = added_lines(spec)
    except RangeError as exc:
        print(
            f"::error::leak scan range unresolvable — {exc}. Failing closed.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVABLE
    print(out)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
