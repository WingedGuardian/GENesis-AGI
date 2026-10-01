#!/usr/bin/env python3
"""CI leak-scan range selector — emits the ADDED lines a PR/push introduces.

Feeds ``scripts/ci/private_pattern_scan.py``. The RANGE is the security-critical
part: it must scan exactly the commits this PR/push authored, never main's own
history.

Why this exists (the fail-BLOCK the former inline range hit):
  CI checks out the PR **merge ref** (``merge(main@ci, PR-head)``) for a
  ``pull_request`` event. The old range ``${base.sha}..HEAD`` used
  ``github.event.pull_request.base.sha``, which is frozen at PR creation and lags
  main. For a PR opened before some private value was remediated on main,
  ``base.sha..HEAD`` re-swept every main commit since base.sha and re-flagged a
  value main itself *added then removed* (the add commit's patch still shows it)
  — false-blocking a clean PR.

  The fix anchors on the merge base with the LIVE base branch:
  ``merge-base(origin/$BASE_REF, HEAD)..HEAD``. This is robust to every checkout
  shape — the synthetic merge ref (mergeable PR, where the merge base is
  ``base@ci``) AND the PR head (unmergeable PR, incl. one whose head is itself a
  merge commit). We do NOT infer "this is the merge ref" from HEAD's parent
  count: an unmergeable PR whose head is a merge commit also has two parents, and
  ``HEAD^1..HEAD`` would then exclude that PR's own first-parent history — a
  PR-authored secret there would escape the gate. A value the PR *introduces*
  always lives in a commit reachable from HEAD but not from its base branch, so
  it is always in ``merge-base..HEAD``; the deliberate add-then-remove-within-a-PR
  detection is preserved (all PR-own commits stay in range; ``--no-merges`` walks
  each).

  The base branch is NOT hardcoded to ``main``: a stacked PR's base is the
  parent PR's branch, and anchoring on main there would sweep the parent's
  commits into the range — re-flagging values the PARENT introduced (this
  install's own identifiers appear on main-authored history more than once).
  The branch name arrives via ``BASE_REF`` (mapped from ``github.base_ref`` on
  the workflow env, itself only present on pull_request events); absent, the
  selector falls back to ``main``, which preserves the pre-stacked-CI behaviour
  on ordinary PRs.

CONTRACT:
  stdout  the added ('^+') lines across the range's non-merge commit patches
          (kept verbatim, INCLUDING '+++ b/path' headers — repo-relative paths
          never match an install value, and a '+++' filter once dropped added
          content beginning '++')
  exit 0  range resolved and emitted (clean OR with content — the downstream
          private_pattern_scan decides leak vs clean)
  exit 3  range UNRESOLVABLE — fail-LOUD; never emit empty (that would be a
          fail-OPEN, silently passing the gate)

Robustness (a hard gate must neither false-green nor crash on odd input):
  - added lines are read as BYTES and split on ``b"\\n"`` only — git's patch line
    delimiter — never ``str.splitlines()`` (which also breaks on U+0085 / U+2028
    and would silently drop content after such a byte → a false green);
  - each kept line is decoded with ``errors="replace"`` so a non-UTF-8 blob in a
    diff cannot raise and spuriously block a clean commit;
  - git output is STREAMED and only '+'-lines retained, so a huge deletion /
    context-heavy diff does not buffer in the runner's memory.
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
    """Run a git command, capturing text output. Never raises on nonzero.

    Used only for SMALL outputs (SHAs, refs). ``errors="replace"`` keeps it from
    raising on stray bytes; ``added_lines`` streams the large patch output itself.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def _commit_exists(ref: str, cwd: str | None = None) -> bool:
    return bool(ref) and _git(["cat-file", "-e", f"{ref}^{{commit}}"], cwd).returncode == 0


def resolve_scan_spec(
    event_name: str,
    push_before: str,
    head_sha: str,
    base_ref: str = "",
    cwd: str | None = None,
) -> tuple[str, str]:
    """Return the scan spec as ``(kind, value)``.

    ``("range", "A..B")`` → scan ``git log -p --no-merges A..B``.
    ``("show", "<sha>")`` → scan a single commit's patch (new branch fallback).
    Raises :class:`RangeError` when the range cannot be resolved (fail closed).
    """
    if event_name == "pull_request":
        # Anchor on the live BASE branch via merge-base — robust for BOTH the
        # synthetic merge ref (mergeable PR) and the PR head (unmergeable, incl.
        # a merge-commit head). Parent count is NOT a reliable "is this the
        # merge ref" signal, so we never special-case HEAD^1. The base is the
        # PR's own base branch (stacked PRs diff against their parent branch,
        # not main); BASE_REF/GITHUB_BASE_REF supplies it, empty → "main".
        # Fetch is best-effort (a full checkout already has the tracking ref
        # under fetch-depth:0); the merge-base result is what gates.
        ref = base_ref.strip() or "main"
        _git(
            [
                "fetch",
                "--no-tags",
                "--quiet",
                "origin",
                f"+refs/heads/{ref}:refs/remotes/origin/{ref}",
            ],
            cwd,
        )
        mb = _git(["merge-base", f"origin/{ref}", "HEAD"], cwd)
        base = mb.stdout.strip()
        if mb.returncode != 0 or not base:
            raise RangeError(
                f"pull_request: merge-base(origin/{ref}, HEAD) failed — cannot "
                "bound the scan to the PR's own commits"
            )
        return ("range", f"{base}..HEAD")

    if _commit_exists(push_before, cwd):
        # Push with a known previous tip — scan only the newly pushed commits.
        return ("range", f"{push_before}..{head_sha or 'HEAD'}")

    # New branch / unknown base — scan the tip commit's patch (best effort).
    return ("show", head_sha or "HEAD")


def added_lines(spec: tuple[str, str], cwd: str | None = None) -> str:
    """Stream the '^+' lines for a scan spec. Raises :class:`RangeError` on git failure.

    Reads git output as BYTES, splits on ``b"\\n"`` only (git's LF patch
    delimiter — NOT ``str.splitlines()``), keeps '+'-prefixed lines, and decodes
    each with ``errors="replace"``. Streaming bounds memory to the added content.
    """
    kind, value = spec
    args = ["git", "log", "-p", "--no-merges", value] if kind == "range" else ["git", "show", value]

    proc = subprocess.Popen(
        args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = proc.stdout
    if stdout is None:  # pragma: no cover - PIPE always yields a stream
        proc.wait()
        raise RangeError(f"git {kind} {value!r} produced no stdout stream")
    kept: list[str] = []
    try:
        # Binary iteration splits on b"\n" ONLY (git's delimiter) — it does not
        # break on U+0085/U+2028 the way str.splitlines() would.
        for raw in stdout:
            if raw.startswith(b"+"):
                kept.append(raw.rstrip(b"\n").decode("utf-8", errors="replace"))
    finally:
        stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise RangeError(f"git {kind} {value!r} failed (rc={rc})")
    return "\n".join(kept)


def main(argv: list[str] | None = None) -> int:
    event_name = os.environ.get("EVENT_NAME", "")
    push_before = os.environ.get("PUSH_BEFORE", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    # github.base_ref mapped on the workflow env; GITHUB_BASE_REF is the
    # runner's own copy of the same value. Empty → "main" inside the resolver.
    base_ref = os.environ.get("BASE_REF", "") or os.environ.get("GITHUB_BASE_REF", "")
    try:
        spec = resolve_scan_spec(event_name, push_before, head_sha, base_ref=base_ref)
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
