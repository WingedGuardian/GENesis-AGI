#!/usr/bin/env python3
"""PreToolUse hook: block git push/merge to main without user approval.

Catches all variations of pushing to or merging into the main branch:
- git push (bare, when on main)
- git push origin main
- git push -u origin main
- git merge <branch> (when on main)
- gh pr merge (without --admin — requires explicit user approval flag)
- gh pr merge with unresolved review findings (ERROR/[P1]/HARD BLOCK)

Stdlib-only. Fail-open on parse errors (don't block legitimate work).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys


def _current_branch() -> str | None:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _get_push_remote_and_branch(cmd: str) -> tuple[str | None, str | None]:
    """Parse git push command to determine target remote and branch.

    Returns (remote, branch) or (None, None) if can't determine.
    """
    parts = cmd.split()
    # Find 'push' position
    try:
        push_idx = parts.index("push")
    except ValueError:
        return None, None

    # Skip flags after 'push'
    args = []
    i = push_idx + 1
    while i < len(parts):
        if parts[i].startswith("-"):
            # Skip flags and their arguments
            if parts[i] in ("-u", "--set-upstream", "--force-with-lease"):
                i += 1  # These don't take a separate argument in this context
            i += 1
            continue
        args.append(parts[i])
        i += 1

    if len(args) == 0:
        # Bare 'git push' — pushes current branch to its upstream
        return "upstream", _current_branch()
    if len(args) == 1:
        # 'git push origin' — pushes current branch to remote
        return args[0], _current_branch()
    if len(args) >= 2:
        # 'git push origin main' or 'git push origin feature:main'
        remote = args[0]
        refspec = args[1]
        # Handle refspec like 'feature:main'
        branch = refspec.split(":")[-1] if ":" in refspec else refspec
        return remote, branch

    return None, None


def _extract_pr_number(cmd: str) -> str | None:
    """Extract PR number from a gh pr merge command."""
    match = re.search(r"gh pr merge\s+(\d+)", cmd)
    return match.group(1) if match else None


def _check_mergeable(pr_num: str) -> str | None:
    """Query GitHub for PR mergeable status. Returns MERGEABLE/UNKNOWN/CONFLICTING or None."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_num, "--json", "mergeable", "--jq", ".mergeable"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None  # Fail-open


# ── Review findings detection ──────────────────────────────────────────

# Patterns that indicate blocking review findings.
# Matches structural review ERRORs, gstack [P1] markers, and PII hard blocks.
_BLOCKING_PATTERNS = [
    re.compile(r"^#{2,3}\s*(?:🔴\s*)?ERROR\b", re.MULTILINE),
    re.compile(r"\[P1\](?!\d)"),
    re.compile(r"HARD\s+BLOCK", re.IGNORECASE),
]

# Patterns that indicate the review was clean (no real findings).
# If a comment matches both blocking AND clean, clean wins — it means
# the reviewer mentioned the category but found nothing.
_CLEAN_PATTERNS = [
    re.compile(r"(?:PII|Secrets|Wording)\s*(?:scan)?:\s*\**CLEAN\**", re.IGNORECASE),
    re.compile(r"Pre-Landing Review:\s*No issues found", re.IGNORECASE),
    re.compile(r"^Pre-Landing Review:\s*No issues found", re.IGNORECASE | re.MULTILINE),
    re.compile(r"VERDICT:\s*PASS", re.IGNORECASE),
]

# Bot usernames that post automated reviews
_REVIEW_BOTS = {"chatgpt-codex-connector[bot]", "github-actions[bot]"}

# ── Inline review comments (pulls/N/comments — a DIFFERENT endpoint) ──
# Codex posts its actual P1/P2 findings ONLY as inline review comments;
# its review body is boilerplate. This endpoint was never scanned, so
# the gate was blind to them (audited 2026-07-10: 173 findings across
# 118 merged PRs passed unseen, 64 of them P1).
_INLINE_P1_RE = re.compile(r"!\[P1 Badge\]")
_INLINE_P2_RE = re.compile(r"!\[P2 Badge\]")
_INLINE_REVIEW_BOTS = {
    "chatgpt-codex-connector[bot]",
    "github-advanced-security[bot]",
}
# Badge/markup prefix stripped when rendering a finding's title line.
_INLINE_MARKUP_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|</?sub>|[*]{1,2}")


def _inline_title(body: str) -> str:
    """First readable line of an inline finding body."""
    first = _INLINE_MARKUP_RE.sub("", body).strip().splitlines()
    return (first[0].strip() if first else "")[:120]


def _check_inline_review_findings(
    pr_num: str, *, force: bool = False,
) -> tuple[bool, str]:
    """Scan INLINE review comments for P1/P2 badge findings.

    Returns (should_block, message). P1 findings block unless their
    thread has a reply (engagement = read) or the merge carries
    '# review-override'. P2 findings never block but are printed to
    stderr one per line — the session must consciously accept them.
    Fail-open on any error, like _check_pr_review_findings.
    """
    if force:
        return False, ""  # override NOTE already printed by the body gate
    try:
        # --paginate: findings beyond the first REST page (30 comments)
        # must still gate. With a per-element jq filter, gh emits one
        # compact JSON object per line across ALL pages.
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/:owner/:repo/pulls/{pr_num}/comments",
                "--paginate",
                "--jq",
                '.[] | {id: .id, reply_to: .in_reply_to_id, '
                'login: .user.login, type: .user.type, body: .body}',
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, ""
        raw = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip()
        ]
    except Exception:
        return False, ""

    replied_to = {c.get("reply_to") for c in raw if c.get("reply_to")}
    p1: list[str] = []
    p2: list[str] = []
    for c in raw:
        login, utype = c.get("login") or "", c.get("type") or ""
        body = c.get("body") or ""
        if utype != "Bot" and login not in _INLINE_REVIEW_BOTS:
            continue
        if c.get("reply_to"):
            continue  # replies aren't findings
        if _INLINE_P1_RE.search(body):
            if c.get("id") in replied_to:
                continue  # thread engaged — treated as acknowledged
            p1.append(_inline_title(body))
        elif _INLINE_P2_RE.search(body):
            p2.append(_inline_title(body))

    if p2:
        print(
            f"WARNING: PR #{pr_num} has {len(p2)} inline [P2] review "
            f"finding(s) (not blocking — address or consciously accept):",
            file=sys.stderr,
        )
        for title in p2[:8]:
            print(f"  [P2] {title}", file=sys.stderr)
    if p1:
        listing = "\n".join(f"  [P1] {t}" for t in p1[:5])
        return True, (
            f"{len(p1)} inline [P1] finding(s) with no reply:\n{listing}\n"
            f"Fix and reply in-thread, or append '# review-override' "
            f"to the merge command to acknowledge and proceed."
        )
    return False, ""


def _check_pr_review_findings(pr_num: str, *, force: bool = False) -> tuple[bool, str]:
    """Check PR comments for unresolved automated review findings.

    Returns (should_block, message).

    Fail-open: returns (False, "") on any error — the hook must never
    become a single point of failure for merges.
    """
    if force:
        print(
            f"NOTE: Review gate override for PR #{pr_num}. "
            "Findings acknowledged by session.",
            file=sys.stderr,
        )
        return False, ""

    try:
        # Fetch comments as JSON array with author info
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/:owner/:repo/issues/{pr_num}/comments",
                "--jq", '[.[] | {login: .user.login, type: .user.type, body: .body}]',
            ],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return False, ""  # Fail-open on API error
    except Exception:
        return False, ""  # Fail-open

    output = result.stdout.strip()
    if not output or output == "[]":
        return False, ""  # No comments at all — allow (quota-exhausted case)

    # Parse JSON array of comments
    try:
        raw_comments = json.loads(output)
    except json.JSONDecodeError:
        return False, ""  # Fail-open on parse error

    # GitHub API can return "body": null for deleted comments —
    # use `or ""` to coerce None to empty string (get's default only
    # fires when the key is absent, not when the value is None).
    comments: list[tuple[str, str, str]] = [
        (c.get("login") or "", c.get("type") or "", c.get("body") or "")
        for c in raw_comments
    ]

    if not comments:
        return False, ""

    # Walk comments in reverse (most recent first). The last review
    # comment determines the state — if findings were addressed and a
    # re-review posted, the newer clean review wins.
    for login, user_type, body in reversed(comments):
        # Only check bot comments (automated reviews)
        if user_type != "Bot" and login not in _REVIEW_BOTS:
            continue

        # Skip Codex quota-exhausted messages (not a real review)
        if "reached your Codex usage limits" in body and not any(
            p.search(body) for p in _BLOCKING_PATTERNS
        ):
            continue

        # Check if this review is clean
        is_clean = any(p.search(body) for p in _CLEAN_PATTERNS)

        # Check for blocking findings
        blocking_matches = [p.pattern for p in _BLOCKING_PATTERNS if p.search(body)]

        if blocking_matches and not is_clean:
            # Found unresolved findings in the most recent review
            return True, (
                f"Automated review has unresolved findings.\n"
                f"Matched patterns: {', '.join(blocking_matches[:3])}\n"
                f"Fix the findings, or append '# review-override' to "
                f"the merge command to acknowledge and proceed."
            )

        if is_clean or not blocking_matches:
            # Most recent review is clean or has no blocking findings
            return False, ""

    # No bot review comments found — allow (no review posted)
    return False, ""


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0

        data = json.loads(raw)
        cmd = data.get("command", "")
        if not cmd:
            return 0

        # ── git push (any branch) ──────────────────────────────────
        if "git push" in cmd:
            _remote, branch = _get_push_remote_and_branch(cmd)
            print(
                f"BLOCKED: git push requires user approval before "
                f"publishing code externally (target: {branch or 'default'}).",
                file=sys.stderr,
            )
            print(
                "Ask the user: 'Ready to push?' before proceeding.",
                file=sys.stderr,
            )
            return 2

        # ── git merge into main ─────────────────────────────────────
        if "git merge" in cmd:
            current = _current_branch()
            if current in ("main", "master"):
                print(
                    "BLOCKED: Merging into main directly is not allowed.",
                    file=sys.stderr,
                )
                print(
                    "Use the PR workflow instead.",
                    file=sys.stderr,
                )
                return 2

        # ── gh pr create ───────────────────────────────────────────
        if "gh pr create" in cmd:
            print(
                "BLOCKED: Creating a PR requires user approval before "
                "publishing externally.",
                file=sys.stderr,
            )
            print(
                "Ask the user: 'Ready to create the PR?' before proceeding.",
                file=sys.stderr,
            )
            return 2

        # ── gh pr merge ────────────────────────────────────────────
        if "gh pr merge" in cmd:
            if "--admin" not in cmd:
                print(
                    "BLOCKED: gh pr merge without --admin is not allowed.",
                    file=sys.stderr,
                )
                print(
                    "Use: gh pr merge --squash --admin",
                    file=sys.stderr,
                )
                return 2

            # Check mergeable status before allowing merge
            pr_num = _extract_pr_number(cmd)
            if pr_num:
                mergeable = _check_mergeable(pr_num)
                if mergeable == "UNKNOWN":
                    print(
                        f"BLOCKED: PR #{pr_num} mergeable status is UNKNOWN.",
                        file=sys.stderr,
                    )
                    print(
                        "GitHub hasn't finished conflict analysis. "
                        "Wait and retry.",
                        file=sys.stderr,
                    )
                    return 2
                if mergeable == "CONFLICTING":
                    print(
                        f"BLOCKED: PR #{pr_num} has merge conflicts. "
                        "Resolve before merging.",
                        file=sys.stderr,
                    )
                    return 2

                # Check for unresolved review findings
                force_override = bool(re.search(r"#\s*review-override\b", cmd))
                should_block, review_msg = _check_pr_review_findings(
                    pr_num, force=force_override,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} has unresolved review findings.",
                        file=sys.stderr,
                    )
                    print(review_msg, file=sys.stderr)
                    return 2

                # Inline review comments (Codex P1/P2 badges) — separate
                # endpoint, separate check. P1 blocks; P2 warns.
                should_block, inline_msg = _check_inline_review_findings(
                    pr_num, force=force_override,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} has unresolved INLINE "
                        f"review findings.",
                        file=sys.stderr,
                    )
                    print(inline_msg, file=sys.stderr)
                    return 2

        # ── sqlite3 write operations ────────────────────────────────
        if "sqlite3" in cmd and re.search(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE)\b", cmd, re.IGNORECASE,
        ):
            print(
                "BLOCKED: Direct database writes via sqlite3 are not allowed. "
                "Use CRUD modules or MCP tools instead.",
                file=sys.stderr,
            )
            return 2

        # ── git commit --no-verify ────────────────────────────────
        if "git commit" in cmd and "--no-verify" in cmd:
            print(
                "BLOCKED: --no-verify bypasses review enforcement hooks. "
                "Remove --no-verify and run /review first.",
                file=sys.stderr,
            )
            return 2

        # ── Process kill (soft warn) ──────────────────────────────
        if re.search(r"(?:^|\s|&&|;)\s*(?:kill|killall|pkill)\s", cmd):
            print(
                "⚠️  STOP: Process kill detected. "
                "Have you received explicit user approval?",
                file=sys.stderr,
            )

        # ── git config writes (soft warn) ─────────────────────────
        if (
            "git config" in cmd
            and not re.search(r"git config\s+(--get|--list|-l|--show)\b", cmd)
            and re.search(r"git config\s+[\w.-]+\s+\S", cmd)
        ):
                print(
                    "⚠️  STOP: git config modification detected. "
                    "Have you received explicit user approval?",
                    file=sys.stderr,
                )

    except Exception:
        pass  # Fail-open on any error — never block legitimate work

    return 0


if __name__ == "__main__":
    sys.exit(main())
