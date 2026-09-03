#!/usr/bin/env python3
"""File a Genesis-repo issue on the SHARED public tracker, safely.

Three consecutive cross-model review rounds on the prose version of this
procedure each found a different way for it to target the wrong repository or
execute its own inputs. The failures were not independent bugs — they were the
same class, and prose cannot enforce any of the five properties that matter:

1. Resolve the UPSTREAM tracker, not whatever repo the cwd happens to be in.
   `gh repo view` with no argument reports the CURRENT directory's repo, so on a
   fork-cloned install it returns the operator's own fork — where they are ADMIN,
   so a permission check on it passes and the issue lands somewhere nobody reads.
2. Check `viewerPermission` on THAT EXPLICIT SLUG. Identity is not permission:
   a direct clone reports the right slug while the user still lacks push access,
   and GitHub then silently DROPS the mandatory labels.
3. Never let issue text reach a shell. A title containing backticks or `$(...)` —
   ordinary in a technical title — is executed if it is interpolated into a
   command line, and its output lands in the public title. Everything here goes
   through argv lists; no shell is ever involved.
4. Per-invocation temp files. Two concurrent sessions sharing a fixed draft path
   can overwrite each other between the privacy scan and the post.
5. Exact-title dedup. `gh issue list --search` interprets `repo:` / `is:` /
   `label:` inside a title as query syntax, so a raw-title search both misses
   duplicates and can match unrelated issues.

Everything fails CLOSED: any check that cannot complete refuses to file.

This does NOT decide whether the issue *should* be public. The caller is
responsible for the two hard limits in CLAUDE.md — explicit user approval for an
irreversible post, and never publishing an unfixed security defect.

Usage:
    file_tracker_issue.py --title-file T --body-file B --area area:memory \\
        --difficulty "help wanted" [--dry-run]

Exit codes: 0 filed (or dry-run OK) · 2 refused (a precondition failed) ·
3 duplicate found · 1 unexpected error.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence

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

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


class Refused(Exception):
    """A precondition failed. Nothing was posted."""


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


def resolve_tracker(run: Runner = _run) -> str:
    """The SHARED tracker slug — the fork's parent when this is a fork.

    Never returns the current repo blindly: that is the round-3 defect.
    """
    data = _gh_json(run, ["gh", "repo", "view", "--json", "isFork,parent,nameWithOwner"])
    if data.get("isFork"):
        parent = data.get("parent") or {}
        owner = (parent.get("owner") or {}).get("login")
        name = parent.get("name")
        if not owner or not name:
            raise Refused(
                "this clone is a fork but its parent could not be resolved — "
                "refusing to file into the fork"
            )
        return f"{owner}/{name}"
    slug = data.get("nameWithOwner")
    if not slug:
        raise Refused("could not resolve a repository from this directory")
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


def find_duplicate(slug: str, title: str, run: Runner = _run, limit: int = 200) -> int | None:
    """Exact normalized-title match over structured output.

    Deliberately does NOT use `--search`: a title containing `repo:` or `is:`
    would be parsed as query syntax rather than searched literally.
    """
    proc = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            slug,
            "--state",
            "all",
            "--limit",
            str(limit),
            "--json",
            "number,title",
        ]
    )
    if proc.returncode != 0:
        raise Refused(
            f"duplicate check failed on {slug}: {proc.stderr.strip() or proc.returncode}. "
            "Refusing to file — a failed lookup is not 'no duplicate'."
        )
    try:
        issues = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise Refused(f"duplicate check returned unparseable JSON: {exc}") from exc
    target = _normalize(title)
    for issue in issues:
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


def create_issue(
    slug: str, title: str, body_path: str, labels: Sequence[str], run: Runner = _run
) -> str:
    """Post it. Title and body travel as argv, never through a shell."""
    argv = ["gh", "issue", "create", "--repo", slug, "--title", title, "--body-file", body_path]
    for label in labels:
        argv += ["--label", label]
    proc = run(argv)
    if proc.returncode != 0:
        raise Refused(f"gh issue create failed: {proc.stderr.strip() or proc.returncode}")
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
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
