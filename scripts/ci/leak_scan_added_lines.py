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
  preserved (all PR-own commits stay in range and each commit's patch is read).
  A merge commit is read as its ``--remerge-diff``: only what its conflict
  resolution added beyond git's own automatic merge, so a value introduced while
  resolving a conflict is scanned and main's content merged in cleanly is not.

Branch pushes (``LEAK_SCAN_RANGE=branch``, set only by
``.github/workflows/branch-leak-scan.yml``) use the same merge-base anchor, so
every non-main branch is scanned the moment it is pushed, PR or not. A push to
main keeps ``before..after``.

CONTRACT:
  stdout  the added ('^+') lines across the range's commit patches (a merge
          commit contributes its --remerge-diff: its conflict resolution only)
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


def _branch_range(cwd: str | None = None) -> tuple[str, str]:
    """Scan spec for a push to a NON-main branch: every commit main lacks.

    ``merge-base(origin/main, HEAD)..HEAD`` — the same anchor the pull_request
    path uses, so a branch is scanned identically whether or not it has a PR.
    Deliberately NOT ``before..after``: that scans only the newest push, so a
    run cancelled by a later push (one run per branch) would leave the
    cancelled push's commits unscanned, and a new branch (``before`` all
    zeros) would fall back to its tip commit alone. Scanning the whole branch
    each run is idempotent, so any later run covers what an earlier one missed.

    A branch with NO common ancestor with main (an orphan branch) carries no
    main history at all, so every commit on it is branch-authored and the scan
    covers its entire history. Any other failure (no origin/main, a git error)
    raises :class:`RangeError` — fail closed, never empty.
    """
    _git(["fetch", "--no-tags", "--quiet", "origin", "main"], cwd)
    if not _commit_exists("origin/main", cwd):
        raise RangeError(
            "branch push: origin/main does not resolve — cannot bound the scan "
            "to the branch's own commits"
        )
    mb = _git(["merge-base", "origin/main", "HEAD"], cwd)
    base = mb.stdout.strip()
    if mb.returncode == 0 and base:
        return ("range", f"{base}..HEAD")
    # `git merge-base` exits 1 with no output when the two share no ancestor.
    if mb.returncode == 1 and not base and _commit_exists("HEAD", cwd):
        return ("range", "HEAD")
    raise RangeError(f"branch push: merge-base(origin/main, HEAD) failed (rc={mb.returncode})")


def resolve_scan_spec(
    event_name: str,
    push_before: str,
    head_sha: str,
    cwd: str | None = None,
    *,
    branch_push: bool = False,
) -> tuple[str, str]:
    """Return the scan spec as ``(kind, value)``.

    ``("range", "A..B")`` → scan ``git log -p --remerge-diff A..B``.
    ``("range", "HEAD")`` → scan every commit reachable from HEAD (orphan branch).
    ``("show", "<sha>")`` → scan a single commit's patch (new branch fallback).
    ``branch_push`` (set by the branch-leak-scan workflow via
    ``LEAK_SCAN_RANGE=branch``) selects :func:`_branch_range` for a push.
    Raises :class:`RangeError` when the range cannot be resolved (fail closed).
    """
    if branch_push and event_name == "push":
        return _branch_range(cwd)

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


#: How each spec kind becomes a patch stream. ``--remerge-diff`` shows a merge
#: commit as the difference between git's own automatic merge and the recorded
#: result, so a conflict resolution's additions are read while content a clean
#: merge brings in from main is not (MEASURED, git 2.43: a resolution's value
#: appears, a clean merge adds nothing). A non-merge commit shows its ordinary
#: patch. Needs git 2.36 or later; an older git exits nonzero, which fails closed.
_PATCH_ARGS = {
    "range": ["log", "-p", "--remerge-diff", "--format=commit %H"],
    "show": ["show", "--remerge-diff", "--format=commit %H"],
}


def _patch_stream(spec: tuple[str, str], cwd: str | None):
    """Yield the raw patch lines (bytes, LF stripped) for a scan spec.

    Raises :class:`RangeError` when git fails.
    """
    kind, value = spec
    proc = subprocess.Popen(
        ["git", *_PATCH_ARGS[kind], value],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    stdout = proc.stdout
    if stdout is None:  # pragma: no cover - PIPE always yields a stream
        proc.wait()
        raise RangeError(f"git {kind} {value!r} produced no stdout stream")
    try:
        # Binary iteration splits on b"\n" ONLY (git's delimiter) — it does not
        # break on U+0085/U+2028 the way str.splitlines() would.
        for raw in stdout:
            yield raw.rstrip(b"\n")
    finally:
        stdout.close()
        rc = proc.wait()
    if rc != 0:
        raise RangeError(f"git {kind} {value!r} failed (rc={rc})")


def _path_from_header(raw: bytes) -> str:
    """The new-side path of a ``+++ b/<path>`` header ('' for /dev/null).

    A path git had to quote (``"b/a\\tb"``) keeps its quoting minus the outer
    quotes and prefix; callers that scope by path then see a name no scope
    excludes, so an odd path is over-scanned, never skipped.
    """
    name = raw[4:].decode("utf-8", errors="replace")
    if name == "/dev/null":
        return ""
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name[2:] if name.startswith("b/") else name


def added_lines_with_paths(spec: tuple[str, str], cwd: str | None = None) -> list[str]:
    """Each added line in the range as ``<path>:<commit12>:<content>``.

    For the history half of the email scan, which scopes by path and reports
    ``path:commit`` locations. A ``+++`` line is a file header only before the
    first hunk of its ``diff --git`` section, so added content that itself
    begins ``++`` is kept as content.
    """
    out: list[str] = []
    commit = path = ""
    in_header = False
    for raw in _patch_stream(spec, cwd):
        if raw.startswith(b"commit ") and not in_header:
            commit = raw[7:19].decode("ascii", errors="replace")
        elif raw.startswith(b"diff --git ") or raw.startswith(b"diff --cc "):
            in_header = True
            path = ""
        elif in_header and raw.startswith(b"+++ "):
            path = _path_from_header(raw)
        elif raw.startswith(b"@@"):
            in_header = False
        elif not in_header and raw.startswith(b"+") and path:
            out.append(f"{path}:{commit}:{raw[1:].decode('utf-8', errors='replace')}")
    return out


def added_lines(spec: tuple[str, str], cwd: str | None = None) -> str:
    """Stream the '^+' lines for a scan spec. Raises :class:`RangeError` on git failure.

    Reads git output as BYTES, splits on ``b"\\n"`` only (git's LF patch
    delimiter — NOT ``str.splitlines()``), keeps '+'-prefixed lines, and decodes
    each with ``errors="replace"``. Streaming bounds memory to the added content.
    """
    return "\n".join(
        raw.decode("utf-8", errors="replace")
        for raw in _patch_stream(spec, cwd)
        if raw.startswith(b"+")
    )


def main(argv: list[str] | None = None) -> int:
    # --range prints the git revision range instead of the added lines, for the
    # gitleaks history step: one range definition for both scans.
    # --with-paths prints each added line as path:commit:content, for the history
    # half of the email scan, which scopes by path.
    args = sys.argv[1:] if argv is None else argv
    print_range = "--range" in args
    with_paths = "--with-paths" in args
    event_name = os.environ.get("EVENT_NAME", "")
    push_before = os.environ.get("PUSH_BEFORE", "")
    head_sha = os.environ.get("HEAD_SHA", "")
    scope = os.environ.get("LEAK_SCAN_RANGE", "")
    if scope not in ("", "branch"):
        # A typo here must not silently fall back to the narrower push range.
        print(
            f"::error::LEAK_SCAN_RANGE={scope!r} is not recognised (expected 'branch' "
            "or unset). Failing closed.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVABLE
    if scope == "branch" and event_name != "push":
        print(
            f"::error::LEAK_SCAN_RANGE=branch requires EVENT_NAME=push, got "
            f"{event_name!r}. Failing closed.",
            file=sys.stderr,
        )
        return EXIT_UNRESOLVABLE
    try:
        spec = resolve_scan_spec(event_name, push_before, head_sha, branch_push=scope == "branch")
        if print_range:
            kind, value = spec
            # `X^!` is git's "commit X alone", the history form of `git show X`.
            print(value if kind == "range" else f"{value}^!")
            return EXIT_OK
        out = "\n".join(added_lines_with_paths(spec)) if with_paths else added_lines(spec)
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
