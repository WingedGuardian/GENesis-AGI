#!/usr/bin/env python3
"""File a Genesis-repo issue on the SHARED public tracker, safely.

Four cross-model review rounds on this procedure — three while it was prose, one
after it became this script — found the same shape of defect each time: a step
CLAIMING MORE CERTAINTY THAN IT HAD. Every guarantee below exists because a
reviewer showed the previous version asserting something it could not know.

Targeting (rounds 1-3, prose):
  * `--repo <install-user>/<repo>` resolved to the operator's OWN FORK.
  * Bare `gh` commands re-resolve their target from the current directory, so a
    `cd` between check and post redirects an irreversible write.
  * `gh repo view` with no argument reports the CURRENT repo — on a fork clone
    that is the fork, where the operator IS ADMIN, so a permission check on it
    PASSES and the issue lands where nobody reads it.

Certainty at boundaries (round 4, this script):
  * A capped listing is not proof of absence. The duplicate check paginates to
    exhaustion; it never infers "no duplicate" from a truncated window.
  * One `parent` hop is not the fork-network root. A fork of a fork needs the
    chain walked. (This `gh` exposes `parent` but NOT `source`, so walking is
    the available mechanism, not a stylistic preference.)
  * Check-then-act is not atomic. Lookup and creation are serialised under a
    per-tracker lock, so two concurrent sessions cannot both pass the duplicate
    check and both post.
  * A failed `gh issue create` does NOT prove nothing was posted. If the server
    committed the issue and the response was lost, the issue EXISTS. That case
    is reported as INDETERMINATE, never as "refused" — telling an operator that
    nothing was posted when something may have been is the worst outcome here,
    because they then file a duplicate or a false local record.

Shell safety: everything runs through argv lists with `shell=False`. A title
containing backticks or `$(...)` — ordinary in a technical title — would execute
if interpolated into a command line, after the privacy scan, with its output in
the public title. The absence of a shell is the mechanism, not a precaution.

This does NOT decide whether the issue SHOULD be public. The caller owns the two
hard limits in CLAUDE.md — explicit user approval for an irreversible post, and
never publishing an unfixed security defect.

Usage:
    file_tracker_issue.py --title-file T --body-file B --area area:memory \\
        --difficulty "help wanted" [--dry-run]

Exit codes: 0 filed (or dry-run OK) · 2 refused, nothing posted · 3 duplicate
found · 4 INDETERMINATE — a post may or may not exist, reconcile before retrying
· 1 unexpected error.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from pathlib import Path

# Mirrors of the fail-closed sets enforced server-side by
# ``src/genesis/mcp/health/contributor_issue.py``. Duplicated deliberately —
# importing that module pulls the whole MCP stack into a standalone script — and
# pinned against drift by tests/test_scripts/test_file_tracker_issue.py, which
# asserts these equal the canonical sets exactly.
AREA_LABELS = frozenset(
    {
        "area:memory",
        "area:dashboard",
        "area:runtime",
        "area:guardian",
        "area:autonomy",
        "area:channels",
        "area:knowledge",
        "area:eval",
        "area:other",
    }
)
DIFFICULTY_LABELS = frozenset(
    {
        "good first issue",
        "first-timers-only",
        "needs-genesis-instance",
        "help wanted",
    }
)
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})

# A fork chain deeper than this is pathological; bound the walk so a cycle or a
# misbehaving API can never spin.
MAX_FORK_DEPTH = 10

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Refused(Exception):
    """A precondition failed. NOTHING was posted — this is a hard guarantee."""


class Indeterminate(Exception):
    """The post may or may not exist. Reconcile against the tracker.

    Deliberately NOT a subclass of Refused: the entire point is that a caller
    must not treat it as "nothing happened".
    """


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Execute argv with NO shell. The absence of a shell is the point."""
    return subprocess.run(  # noqa: S603 - argv list, shell=False, no interpolation
        list(argv), capture_output=True, text=True, timeout=120, check=False
    )


def _gh_json(run: Runner, argv: Sequence[str]) -> dict:
    proc = run(argv)
    if proc.returncode != 0:
        raise Refused(f"`{' '.join(argv)}` failed: {proc.stderr.strip() or proc.returncode}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise Refused(f"`{' '.join(argv)}` returned unparseable JSON: {exc}") from exc


def _parent_slug(data: dict) -> str | None:
    parent = data.get("parent") or {}
    owner = (parent.get("owner") or {}).get("login")
    name = parent.get("name")
    return f"{owner}/{name}" if owner and name else None


def resolve_tracker(run: Runner = _run) -> str:
    """The SHARED tracker slug — the ROOT of the fork network.

    Walks the parent chain rather than taking a single hop: a fork of a fork
    would otherwise resolve to the intermediate fork, where the operator may
    well have write access, so the permission check passes and the post lands on
    the wrong tracker. This `gh` exposes ``parent`` but not ``source``, so the
    walk is the available mechanism.
    """
    data = _gh_json(run, ["gh", "repo", "view", "--json", "isFork,parent,nameWithOwner"])
    slug = data.get("nameWithOwner")
    if not slug:
        raise Refused("could not resolve a repository from this directory")

    depth = 0
    while data.get("isFork"):
        depth += 1
        if depth > MAX_FORK_DEPTH:
            raise Refused(
                f"fork chain deeper than {MAX_FORK_DEPTH} from {slug} — refusing rather "
                "than guessing which repository is the shared tracker"
            )
        parent = _parent_slug(data)
        if not parent:
            raise Refused(
                f"{slug} is a fork but its parent could not be resolved — refusing to "
                "file into the fork"
            )
        slug = parent
        data = _gh_json(run, ["gh", "repo", "view", slug, "--json", "isFork,parent,nameWithOwner"])
    return str(slug)


def check_permission(slug: str, run: Runner = _run) -> str:
    """Verify write access ON THE RESOLVED SLUG. Identity is not permission."""
    data = _gh_json(run, ["gh", "repo", "view", slug, "--json", "viewerPermission"])
    perm = data.get("viewerPermission")
    if perm not in WRITE_PERMISSIONS:
        raise Refused(
            f"no write access to {slug} (viewerPermission={perm!r}). "
            "Keep this as a local follow-up until a maintainer carries it across."
        )
    return str(perm)


def _normalize(title: str) -> str:
    """Fold case/whitespace/unicode so trivially-different titles compare equal."""
    return " ".join(unicodedata.normalize("NFKC", title).casefold().split())


def find_duplicate(slug: str, title: str, run: Runner = _run) -> int | None:
    """Exact normalized-title match over an EXHAUSTIVE listing.

    Two things this deliberately does not do:

    * It does not use ``--search``. A title containing ``repo:`` / ``is:`` /
      ``label:`` would be parsed as query syntax rather than searched literally.
    * It does not cap. A capped window that happens to exclude the match is
      indistinguishable from no match — absence from a truncated read is not
      absence. ``--paginate`` walks every page; the ``pull_request`` filter is
      required because GitHub's issues endpoint returns PRs too.
    """
    proc = run(
        [
            "gh",
            "api",
            "--paginate",
            f"repos/{slug}/issues?state=all&per_page=100",
            "--jq",
            '.[] | select(has("pull_request")|not) | {number,title}',
        ]
    )
    if proc.returncode != 0:
        raise Refused(
            f"duplicate check failed on {slug}: {proc.stderr.strip() or proc.returncode}. "
            "Refusing to file — a failed lookup is not 'no duplicate'."
        )
    target = _normalize(title)
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            issue = json.loads(line)
        except json.JSONDecodeError as exc:
            raise Refused(
                f"duplicate check returned unparseable JSON: {exc}. Refusing — a partial "
                "read cannot prove absence."
            ) from exc
        if _normalize(str(issue.get("title", ""))) == target:
            return int(issue["number"])
    return None


def validate_labels(area: str, difficulty: str) -> list[str]:
    """Both classes are mandatory, and a nonexistent label makes `gh` fail."""
    if area not in AREA_LABELS:
        raise Refused(
            f"{area!r} is not a real area label. Allowed: "
            f"{', '.join(sorted(AREA_LABELS))}. Use area:other if none fits."
        )
    if difficulty not in DIFFICULTY_LABELS:
        raise Refused(
            f"{difficulty!r} is not a real difficulty/environment label. Allowed: "
            f"{', '.join(sorted(DIFFICULTY_LABELS))}."
        )
    return [area, difficulty]


def _lock_path(slug: str) -> Path:
    """Per-tracker lock file. Home-based so a tmp cleaner cannot drop it."""
    digest = hashlib.sha256(slug.encode()).hexdigest()[:16]
    base = Path.home() / ".genesis" / "locks"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir())
    return base / f"tracker-issue-{digest}.lock"


@contextmanager
def tracker_lock(slug: str):
    """Serialise duplicate-check + create for one tracker on this install.

    Without it, two concurrent foreground sessions can both pass the duplicate
    check before either posts, and both then post — defeating the check that
    exists precisely for that case. Cross-INSTALL races remain possible; that is
    inherent to client-side dedup and is stated here rather than hidden.
    """
    path = _lock_path(slug)
    with open(path, "w", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def create_issue(
    slug: str, title: str, body_path: str, labels: Sequence[str], run: Runner = _run
) -> str:
    """Post it. Title and body travel as argv, never through a shell.

    A nonzero exit raises Indeterminate, NOT Refused: GitHub may have committed
    the issue before the response was lost, in which case the post exists. The
    caller must reconcile rather than assume nothing happened.
    """
    argv = ["gh", "issue", "create", "--repo", slug, "--title", title, "--body-file", body_path]
    for label in labels:
        argv += ["--label", label]
    proc = run(argv)
    if proc.returncode != 0:
        raise Indeterminate(
            f"gh issue create on {slug} exited {proc.returncode}: "
            f"{proc.stderr.strip() or '(no stderr)'}. The issue MAY have been created — "
            "GitHub can commit the post and still lose the response. Check the tracker "
            "for this title BEFORE retrying or recording a local row."
        )
    return proc.stdout.strip()


def main(argv: Sequence[str] | None = None, run: Runner = _run) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--title-file", required=True, help="file holding the title (never a shell arg)"
    )
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--area", required=True, help=f"one of: {', '.join(sorted(AREA_LABELS))}")
    ap.add_argument(
        "--difficulty", required=True, help=f"one of: {', '.join(sorted(DIFFICULTY_LABELS))}"
    )
    ap.add_argument("--dry-run", action="store_true", help="run every check, post nothing")
    args = ap.parse_args(argv)

    try:
        with open(args.title_file, encoding="utf-8") as fh:
            title = fh.read().strip()
        if not title:
            raise Refused("title file is empty")
        with open(args.body_file, encoding="utf-8") as fh:
            if not fh.read().strip():
                raise Refused("body file is empty")

        labels = validate_labels(args.area, args.difficulty)
        slug = resolve_tracker(run)
        perm = check_permission(slug, run)

        # Lookup and creation are ONE critical section — see tracker_lock().
        with tracker_lock(slug):
            dup = find_duplicate(slug, title, run)
            if dup is not None:
                print(
                    f"DUPLICATE: {slug}#{dup} already has this exact title. Nothing filed.",
                    file=sys.stderr,
                )
                return 3
            if args.dry_run:
                print(f"DRY RUN ok — would file to {slug} (perm={perm}) with labels {labels}")
                return 0
            print(create_issue(slug, title, args.body_file, labels, run))
        return 0
    except Indeterminate as exc:
        print(f"INDETERMINATE: {exc}", file=sys.stderr)
        return 4
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
