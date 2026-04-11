"""Phase 6 contribution — `genesis contribute <sha>` CLI.

Orchestrates the full pipeline:

    identity → read commit → divergence → version gate → sanitize
    → review → consent prompt → PR opener

Exposed via `python -m genesis contribute <sha>`. Wired into
`src/genesis/__main__.py` as a new subcommand.

Also supports `--list`: print the pending markers in
`~/.genesis/pending-offers/` so power users can see what's queued.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .divergence import check_divergence
from .identity import load_install_info, pseudonym_email
from .pr_opener import create_pr, resolve_target_repo
from .review import run_review_chain
from .sanitize import scan_diff
from .version_gate import (
    check_version_gate,
    format_version_string,
    read_install_sha,
)

logger = logging.getLogger(__name__)

# Strict SHA validator — rejects `--option` injection in arg positions.
# Accepts 4-40 hex chars (short or full SHA) or a full ref name that
# matches git's safe subset. Applied before any git subcommand that
# accepts a SHA.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _validate_sha(sha: str) -> str:
    if not sha or not _SHA_RE.match(sha):
        raise ValueError(
            f"invalid commit SHA: {sha!r} (expected 4-40 hex chars)"
        )
    return sha


@dataclass
class CommitInfo:
    sha: str
    subject: str
    body: str
    diff: str


def _run_git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        timeout=30,
        check=False,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def read_commit(sha: str, *, repo_path: Path | None = None) -> CommitInfo:
    """Read a commit's subject, body, and unified diff from git.

    Validates the SHA first — we never pass user-controlled strings
    to git without `--` separation AND format validation. A SHA like
    `--output=/tmp/x` would otherwise be parsed as an option.

    REJECTS merge commits. `git show --format= <merge-sha>` emits an
    EMPTY diff for clean merges (no -m flag), which would make the
    sanitizer happily pass an empty diff and ship the entire merged
    side branch upstream unreviewed. Phase 6 MVP is bug-fix-only, and
    a fix is always a single-parent commit; a merge commit is a sign
    the user picked the wrong SHA. Codex review P1 finding.
    """
    sha = _validate_sha(sha)
    # Parent count check — merges have >1 parent.
    parents = _run_git("rev-list", "--parents", "-n", "1", sha, "--", cwd=repo_path).strip()
    parent_count = max(0, len(parents.split()) - 1)
    if parent_count > 1:
        raise RuntimeError(
            f"commit {sha[:12]} is a merge commit ({parent_count} parents); "
            "Phase 6 contributions must be single-parent fix commits. "
            "Pick the specific fix commit from the merged branch."
        )
    delim = "\x1e"
    fmt = f"%s{delim}%b"
    meta = _run_git("log", "-1", f"--format={fmt}", sha, "--", cwd=repo_path)
    parts = meta.split(delim, 1)
    subject = parts[0].strip() if parts else ""
    body = parts[1].strip() if len(parts) > 1 else ""
    # `git show --format= -- <sha>` does not work; show doesn't accept
    # `--` separator the same way. Validated SHA is the safety net.
    diff = _run_git("show", "--format=", sha, cwd=repo_path)
    return CommitInfo(sha=sha, subject=subject, body=body, diff=diff)


def fetch_upstream_head(
    *, repo_path: Path | None = None, remote: str = "origin"
) -> str | None:
    """Best-effort fetch of upstream HEAD commit.

    Tries `git ls-remote <remote> HEAD` first (no network state
    required). Falls back to `git rev-parse <remote>/main` which
    requires a prior fetch.
    """
    try:
        out = _run_git("ls-remote", remote, "HEAD", cwd=repo_path, check=False)
    except Exception:  # noqa: BLE001
        out = ""
    if out.strip():
        return out.strip().split()[0]
    try:
        out = _run_git(
            "rev-parse", f"{remote}/main",
            cwd=repo_path, check=False,
        )
    except Exception:  # noqa: BLE001
        out = ""
    return out.strip() or None


def _prompt_yes_no(message: str, *, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(message)
        print("  (non-interactive; pass --yes to auto-confirm)")
        return False
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def _list_pending_markers() -> int:
    """Print pending-offers markers, oldest first. Returns exit code."""
    base = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis"))
    pending = base / "pending-offers"
    if not pending.is_dir():
        print("no pending offers")
        return 0
    markers = [p for p in pending.iterdir() if p.suffix == ".json"]
    if not markers:
        print("no pending offers")
        return 0
    markers.sort(key=lambda p: p.stat().st_mtime)
    print(f"{len(markers)} pending offer(s):")
    for m in markers:
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
            sha = str(data.get("sha", ""))[:12]
            subject = str(data.get("subject", ""))
            created = str(data.get("created_at", ""))
            print(f"  {sha}  {created}  {subject}")
        except (OSError, ValueError) as e:
            print(f"  (unreadable marker {m.name}: {e})")
    return 0


def _print_header(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


async def _contribute_async(args: argparse.Namespace) -> int:
    repo_path = Path(args.repo).resolve() if args.repo else Path.cwd()
    install = load_install_info()

    _print_header("step 1 — read commit")
    try:
        commit = read_commit(args.sha, repo_path=repo_path)
    except RuntimeError as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return 2
    print(f"  sha:     {commit.sha}")
    print(f"  subject: {commit.subject}")

    if not args.allow_non_fix and not commit.subject.startswith("fix"):
        print(
            "  WARNING: commit is not a `fix:` conventional commit. "
            "Phase 6 MVP contributions are bug-fix-only. Pass --allow-non-fix "
            "to override.",
            file=sys.stderr,
        )
        return 2

    _print_header("step 2 — identity")
    if args.identify:
        author = _run_git("config", "user.email", cwd=repo_path).strip() or "unknown"
        print(f"  attribution: real identity ({author})")
    else:
        print(f"  attribution: pseudonym ({pseudonym_email(install.install_id)})")

    _print_header("step 3 — divergence check")
    install_sha = read_install_sha(repo_path) or commit.sha
    upstream_sha = args.upstream_sha or fetch_upstream_head(repo_path=repo_path)
    if not upstream_sha:
        print(
            "  WARNING: could not resolve upstream HEAD. "
            "Pass --upstream-sha <sha> to override.",
            file=sys.stderr,
        )
        upstream_sha = install_sha  # degrade gracefully
    div = check_divergence(commit.sha, upstream_sha, repo_path=repo_path)
    if not div.clean:
        print(f"  BLOCKED: {div.message}", file=sys.stderr)
        return 3
    print(f"  {div.message}")

    # P2-3 from the 6.1b.2 review: sanitizer runs BEFORE the version gate
    # so no unsanitized diff ever leaves the machine. The version gate ships
    # the full diff to a third-party LLM; we must guarantee secrets /
    # forbidden-path content are already blocked at that point.
    _print_header("step 4 — sanitizer")
    san = scan_diff(
        commit.diff,
        protected_paths_yaml=repo_path / "config" / "protected_paths.yaml",
    )
    print(f"  scanners run: {', '.join(san.scanners_run)}")
    print(f"  findings:     {len(san.findings)}")
    if not san.ok:
        print("  BLOCKED: sanitizer flagged:", file=sys.stderr)
        for f in san.blocking():
            loc = f"{f.file}:{f.line}" if f.file and f.line else (f.file or "")
            print(f"    [{f.severity}] {f.scanner}: {f.message} {loc}".rstrip(), file=sys.stderr)
        return 5

    _print_header("step 5 — version gate")
    vg = await check_version_gate(
        user_subject=commit.subject,
        user_body=commit.body,
        user_diff=commit.diff,
        user_sha=install_sha,
        upstream_sha=upstream_sha,
        repo_path=repo_path,
    )
    print(f"  upstream commits considered: {vg.upstream_commit_count}")
    print(f"  confidence: {vg.confidence}")
    if vg.reasoning:
        print(f"  reasoning: {vg.reasoning}")
    if vg.already_fixed:
        print(
            f"  BLOCKED: the version gate believes this is already fixed "
            f"upstream (matched {vg.matched_sha or 'unknown'}). Update your "
            "install with `git pull upstream main` and verify before retrying.",
            file=sys.stderr,
        )
        return 4

    _print_header("step 6 — adversarial review")
    rev = run_review_chain(
        commit.diff,
        skip_codex=args.skip_review,
        skip_cc_reviewer=args.skip_review,
        skip_native=args.skip_review,
    )
    if rev.available:
        print(f"  reviewer: {rev.reviewer}")
        print(f"  verdict:  {'PASS' if rev.passed else 'FAIL'} ({rev.finding_count} findings)")
    else:
        print("  review chain unavailable — proceeding with 'Review: unavailable'")

    _print_header("step 7 — consent")
    version_display = format_version_string(repo_path)
    # Resolve target repo BEFORE prompting so the user sees exactly which
    # repo their fix will land against. Codex review I5: hiding this
    # behind "(gh default)" let `gh repo view` silently pick the wrong
    # remote (e.g., a personal fork instead of the public upstream).
    resolved_target = args.target_repo or resolve_target_repo(repo_path)
    print(f"  install version: {version_display}")
    print(f"  target repo:     {resolved_target or '(unresolved — set --target-repo)'}")
    print("  This will open a DRAFT PR on the public Genesis repo using the")
    print("  sanitized diff above. Review output will be attached for triage.")
    if not _prompt_yes_no("  Open the draft PR now?", assume_yes=args.yes):
        print("  cancelled by user.")
        return 0

    _print_header("step 8 — open PR")
    pr = create_pr(
        install=install,
        source_sha=commit.sha,
        subject=commit.subject,
        version_display=version_display,
        version_gate=vg,
        sanitizer=san,
        review=rev,
        upstream_sha=upstream_sha,
        target_repo=args.target_repo,
        repo_path=repo_path,
        dry_run=args.dry_run,
    )
    if not pr.ok:
        print(f"  FAILED: {pr.error}", file=sys.stderr)
        if pr.body:
            print("\n  PR body that would have been submitted:", file=sys.stderr)
            print(pr.body, file=sys.stderr)
        return 6
    print(f"  branch: {pr.branch}")
    if pr.url:
        print(f"  URL:    {pr.url}")
    if args.dry_run:
        print("  (dry-run: PR NOT actually created)")
    return 0


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    """Register `genesis contribute` on an existing subparser group.

    Called from `src/genesis/__main__.py`.
    """
    p = subparsers.add_parser(
        "contribute",
        help="Contribute a fix upstream via Phase 6 pipeline",
        description=(
            "Open a draft PR on the public Genesis repo with a sanitized "
            "version of the given fix commit."
        ),
    )
    p.add_argument("sha", nargs="?", help="Commit SHA to contribute (required unless --list)")
    p.add_argument("--identify", action="store_true",
                   help="Use real git identity instead of the install pseudonym")
    p.add_argument("--list", action="store_true",
                   help="List pending contribution offers and exit")
    p.add_argument("--repo", help="Repo path (default: cwd)")
    p.add_argument("--upstream-sha",
                   help="Upstream HEAD SHA (default: ls-remote origin HEAD)")
    p.add_argument("--target-repo",
                   help="owner/repo for `gh pr create --repo` (default: gh auto)")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip interactive confirmation")
    p.add_argument("--dry-run", action="store_true",
                   help="Build the body and command but do not call gh")
    p.add_argument("--skip-review", action="store_true",
                   help="Skip the adversarial review chain")
    p.add_argument("--allow-non-fix", action="store_true",
                   help="Allow commits that don't start with fix: (MVP bug-fix-only override)")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    """Dispatcher called by __main__.py after argparse. Returns exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    if args.list:
        return _list_pending_markers()
    if not args.sha:
        print("ERROR: commit sha required (or pass --list)", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_contribute_async(args))
    except KeyboardInterrupt:
        print("\ncancelled", file=sys.stderr)
        return 130
