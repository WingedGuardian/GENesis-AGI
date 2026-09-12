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

  The fix anchors on the merge base with LIVE main:
  ``merge-base(origin/main, HEAD)..HEAD``. This is robust to every checkout
  shape — the synthetic merge ref (mergeable PR, where the merge base is
  ``main@ci``) AND the PR head (unmergeable PR, incl. one whose head is itself a
  merge commit). We do NOT infer "this is the merge ref" from HEAD's parent
  count: an unmergeable PR whose head is a merge commit also has two parents, and
  ``HEAD^1..HEAD`` would then exclude that PR's own first-parent history — a
  PR-authored secret there would escape the gate. A value the PR *introduces*
  always lives in a commit reachable from HEAD but not from main, so it is always
  in ``merge-base..HEAD``; the deliberate add-then-remove-within-a-PR detection is
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
    cwd: str | None = None,
) -> tuple[str, str]:
    """Return the scan spec as ``(kind, value)``.

    ``("range", "A..B")`` → scan ``git log -p --no-merges A..B``.
    ``("show", "<sha>")`` → scan a single commit's patch (new branch fallback).
    Raises :class:`RangeError` when the range cannot be resolved (fail closed).
    """
    if event_name == "pull_request":
        # Anchor on live main via merge-base — robust for BOTH the synthetic
        # merge ref (mergeable PR) and the PR head (unmergeable, incl. a
        # merge-commit head). Parent count is NOT a reliable "is this the merge
        # ref" signal, so we never special-case HEAD^1. Fetch is best-effort (a
        # full checkout already has origin/main under fetch-depth:0); the
        # merge-base result is what gates.
        _git(["fetch", "--no-tags", "--quiet", "origin", "main"], cwd)
        mb = _git(["merge-base", "origin/main", "HEAD"], cwd)
        base = mb.stdout.strip()
        if mb.returncode != 0 or not base:
            raise RangeError(
                "pull_request: merge-base(origin/main, HEAD) failed — cannot "
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
