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
import shlex
import subprocess
import sys

# Self-locate so `from hook_input import …` resolves both when CC runs this as a
# script (sys.path[0] is this dir) AND when it is imported as a module for tests
# (importlib does not add the file's dir to sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    commit_skips_hooks,
    gh_pr_subcommand,
    git_subcommand,
    has_trailing_override,
)


def _current_branch() -> str | None:
    """Get current git branch name."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=5,
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
    """PR number from a gh pr merge command (bare number, #N, or URL)."""
    match = re.search(r"\bgh pr merge\b(.*)$", cmd, re.DOTALL)
    if not match:
        return None
    # A newline ends the command too, but shlex collapses it to plain
    # whitespace (so a `\n echo 456` chain would leak its digits) — cut
    # the tail at the first newline before tokenizing.
    tail = match.group(1).split("\n", 1)[0]
    # Pre-space shell separators into standalone tokens (shlex keeps them
    # attached, e.g. '123;'). Quotes still protect a separator inside an
    # arg value — shlex parses the quoted region as one token afterwards.
    spaced = re.sub(r"(\|\||&&|[|;&])", r" \1 ", tail)
    try:
        # shlex keeps quoted args whole so digits inside a --subject
        # string are never mistaken for the PR number.
        tokens = shlex.split(spaced)
    except ValueError:
        tokens = spaced.split()
    for tok in tokens:
        # Stop at the end of THIS command — tokens after a separator
        # belong to a chained command, and their digits must not be read
        # as this merge's target (`gh pr merge 123; echo 456` merges 123
        # but this loop would otherwise return 456). 2026-07-10 review.
        if tok in {";", "&", "|", "&&", "||"}:
            break
        if tok.isdigit():
            return tok
        if tok.startswith("#") and tok[1:].isdigit():
            return tok[1:]
        url = re.match(r"\S*/pull/(\d+)\b", tok)
        if url:
            return url.group(1)
    return None


def _resolve_pr_number(cmd: str) -> str | None:
    """Command PR number, else the current branch's open PR.

    No-arg `gh pr merge` from a PR branch is valid gh usage, but it
    used to skip EVERY merge gate here (the gates only ran under
    `if pr_num:`) — the 2026-07-10 audit points at this as a mechanism
    behind findings-ignored merges. Resolution failure is the caller's
    signal to fail CLOSED for merge commands.
    """
    pr_num = _extract_pr_number(cmd)
    if pr_num:
        return pr_num
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        resolved = result.stdout.strip()
        if result.returncode == 0 and resolved.isdigit():
            return resolved
    except Exception:
        pass
    return None


def _check_mergeable(pr_num: str) -> str | None:
    """Query GitHub for PR mergeable status. Returns MERGEABLE/UNKNOWN/CONFLICTING or None."""
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_num, "--json", "mergeable", "--jq", ".mergeable"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None  # Fail-open


# Check-run conclusions / statuses that mean the CI is NOT green.
_CI_RED_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_CI_RED_STATES = {"FAILURE", "ERROR"}  # legacy StatusContext state
_CI_SKIP_CONCLUSIONS = {"SKIPPED", "NEUTRAL"}
_CI_GREEN = {"SUCCESS"}
# The only CheckRun.status that means "finished". Everything else
# (QUEUED/IN_PROGRESS/PENDING/WAITING/REQUESTED/…) is treated as unfinished, so
# a new/renamed non-terminal state can never be silently mistaken for green.
_CI_TERMINAL_STATUSES = {"COMPLETED"}


def _pr_ci_status(pr_num: str) -> tuple[str, list[str]]:
    """Classify a PR's CI check-runs.

    Returns ``(state, problem_checks)`` where state is one of:
      * ``"green"``   — every non-skipped check concluded SUCCESS
      * ``"red"``     — at least one check failed/cancelled/timed-out
      * ``"pending"`` — a check is still queued/running (and none are red)
      * ``"unknown"`` — could not determine (API error, no checks, or an
                        unrecognized payload shape) → callers FAIL OPEN, because
                        blocking a merge on our own inability to read CI would be
                        worse than the gap this gate closes.

    Tests inject via the ``_TEST_GH_CI_ROLLUP`` env var (a JSON array like
    ``gh pr view --json statusCheckRollup``) so no network is needed.
    """
    raw = os.environ.get("_TEST_GH_CI_ROLLUP")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    "--json",
                    "statusCheckRollup",
                    "--jq",
                    ".statusCheckRollup",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return "unknown", []
            raw = result.stdout.strip()
        except Exception:
            return "unknown", []
    try:
        checks = json.loads(raw) if raw else []
    except Exception:
        return "unknown", []
    if not isinstance(checks, list) or not checks:
        return "unknown", []

    red: list[str] = []
    pending: list[str] = []
    saw_recognized = False
    for c in checks:
        if not isinstance(c, dict):
            continue
        name = c.get("name") or c.get("context") or "check"
        conclusion = c.get("conclusion")  # CheckRun
        status = c.get("status")  # CheckRun: QUEUED/IN_PROGRESS/COMPLETED/PENDING/…
        state = c.get("state")  # StatusContext: SUCCESS/FAILURE/PENDING/ERROR
        if conclusion in _CI_SKIP_CONCLUSIONS:
            saw_recognized = True
            continue
        if conclusion in _CI_RED_CONCLUSIONS or state in _CI_RED_STATES:
            saw_recognized = True
            red.append(name)
        elif conclusion in _CI_GREEN or state in _CI_GREEN:
            saw_recognized = True
        elif status in _CI_TERMINAL_STATUSES:
            # COMPLETED with an unrecognized/None conclusion — treat as done,
            # don't fabricate pending; ignore this entry.
            saw_recognized = True
        elif status is not None or state == "PENDING":
            # ANY non-terminal CheckRun status (QUEUED/IN_PROGRESS/PENDING/
            # WAITING/REQUESTED/…) or a PENDING StatusContext is unfinished →
            # block. Enumerating "known pending" states would silently miss new
            # ones (P1 review finding), so we invert: not-terminal ⇒ pending.
            saw_recognized = True
            pending.append(name)
        # else: no status/state/conclusion at all — unrecognized shape, ignore

    if red:
        return "red", sorted(set(red))
    if pending:
        return "pending", sorted(set(pending))
    if not saw_recognized:
        return "unknown", []  # payload had no CI-shaped entries
    return "green", []


# The CI-status gate is waived only by a genuine ``# ci-override`` trailing
# comment on the merge segment itself — detected with the same quote-aware,
# segment-bound parser as ``# review-override`` (shell_parse.has_trailing_override)
# so it cannot be spoofed from inside a quoted --body or a different chained
# command. Distinct from --admin and # review-override; waives ONLY this gate.


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
    pr_num: str,
    *,
    force: bool = False,
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
                "gh",
                "api",
                f"repos/:owner/:repo/pulls/{pr_num}/comments",
                "--paginate",
                "--jq",
                ".[] | {id: .id, reply_to: .in_reply_to_id, "
                "login: .user.login, type: .user.type, body: .body}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return False, ""
        raw = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
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
            f"NOTE: Review gate override for PR #{pr_num}. Findings acknowledged by session.",
            file=sys.stderr,
        )
        return False, ""

    try:
        # Fetch comments as JSON array with author info
        result = subprocess.run(
            [
                "gh",
                "api",
                f"repos/:owner/:repo/issues/{pr_num}/comments",
                "--jq",
                "[.[] | {login: .user.login, type: .user.type, body: .body}]",
            ],
            capture_output=True,
            text=True,
            timeout=15,
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
        (c.get("login") or "", c.get("type") or "", c.get("body") or "") for c in raw_comments
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


def _is_dispatched() -> bool:
    """True in a Genesis-dispatched (autonomous/headless) CC session.

    ``cc/invoker.py`` stamps ``GENESIS_CC_SESSION=1`` on every dispatched
    session; a user-launched foreground session does not carry it. Dispatched
    sessions have no human to answer an ``ask`` prompt and never push via the CC
    Bash tool in normal operation — the executor pushes from the server
    subprocess, scope-gated (``autonomy/executor``) — so they are hard-denied
    rather than prompted.
    """
    return os.environ.get("GENESIS_CC_SESSION") == "1"


# git push flags that consume the NEXT token as their value — so a value that
# happens to start with '+' or contain 'f' is not misread as a force.
_PUSH_VALUE_FLAGS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


def _push_is_force(argv: list[str]) -> bool:
    """Whether a parsed ``git push`` argv performs a force push.

    Argv comes from ``shell_parse`` (quote-stripped), so a quoted ``'-f'``
    counts and a branch/refspec name that merely contains ``-f`` (a bare
    positional) does not. Catches both the flag forms — ``--force`` /
    ``--force-with-lease`` / ``--force-if-includes`` / ``--mirror`` / ``-f`` /
    bundled ``-uf`` — and the ``+<refspec>`` shorthand (``git push origin +main``),
    which git treats as ``--force`` for that ref. A short cluster stops at
    ``o`` and the value token of ``-o`` / ``--push-option`` / ``--repo`` /
    ``--exec`` / ``--receive-pack`` is skipped, so a push-option value that
    starts with ``+`` or contains ``f`` (e.g. ``-oci.skip``) is not mistaken
    for a force.
    """
    i = 1  # skip argv[0] == "git"
    while i < len(argv):
        tok = argv[i]
        if tok in _PUSH_VALUE_FLAGS:
            i += 2  # skip the flag and its value token
            continue
        if tok == "--force" or tok.startswith("--force-"):
            return True
        if tok == "--mirror":
            # --mirror force-updates EVERY ref and deletes remote refs that are
            # absent locally — an unconditional destructive push, never a plain
            # one, so it must hard-block rather than reach an approvable prompt.
            return True
        if tok.startswith("+"):
            return True  # +<refspec> is git shorthand for --force on that ref
        if tok.startswith("-") and not tok.startswith("--") and len(tok) > 1:
            for ch in tok[1:]:
                if ch == "o":  # -oVALUE glued push-option — rest is its value
                    break
                if ch == "f":
                    return True
        i += 1
    return False


def _ask(reason: str) -> int:
    """Emit a PreToolUse ``ask`` decision — a native approve/deny dialog.

    Prints the hook JSON to stdout and returns 0; Claude Code shows the user a
    permission prompt and runs the tool only on explicit approval — a gate the
    agent cannot self-satisfy. Verified to render in a wrapped child session
    2026-07-27.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def _allow(reason: str) -> int:
    """Emit a PreToolUse ``allow`` decision — auto-approve, no prompt.

    Prints the hook JSON to stdout and returns 0. An ``allow`` decision bypasses
    Claude Code's own permission prompt, unlike a bare exit-0 (which leaves the
    command to the normal permission flow — and a non-allow-listed ``gh pr create``
    would then still prompt). Used ONLY on the verified-safe pr-create path (the
    branch is already on the remote, or an explicit ``--head`` means gh cannot
    push), so opening the PR rides on the push's approval instead of demanding its
    own.
    """
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


def _pr_create_head_raw(argv: list[str]) -> str | None:
    """The RAW ``--head``/``-H`` value (``owner:`` prefix intact), or None.

    Returns the LAST occurrence, mirroring gh's pflag last-value-wins semantics
    for a repeated string flag — ``--head real --head=`` resolves to ``""`` in gh,
    so this must too, else a stale earlier value could wrongly look like a real
    head. ``None`` = no ``--head`` given at all (distinct from an empty value)."""
    result: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--head", "-H") and i + 1 < len(argv):
            result = argv[i + 1]
        elif tok.startswith("--head="):
            result = tok.split("=", 1)[1]
        i += 1
    return result


def _pr_create_would_publish(argv: list[str]) -> bool:
    """Whether a ``gh pr create`` might PUSH/fork its branch (bypassing the push gate).

    Per the gh manual (``gh pr create --help``): only a create with NO ``--head``
    can publish — "when the current branch isn't fully pushed to a git remote, a
    prompt will ask where to push the branch and offer an option to fork ... Use
    ``--head`` to explicitly skip any forking or pushing behavior." So:

    * A NON-EMPTY, plausibly-LITERAL explicit ``--head`` (local, unpushed, or
      ``owner:fork``) → gh does NOT push/fork; it references the head ref as-is
      (erroring if absent) → cannot publish code around this hook → **un-gate**.
      (gh only takes this skip-push path when its resolved ``HeadBranch != ""``.
      An EMPTY head — ``--head=`` / ``--head ""``, or a repeated flag whose LAST
      value is empty — is gh's implicit path, and a value carrying shell-expansion
      metacharacters (``$VAR`` / ``$(...)`` / backticks) could resolve to empty at
      runtime and we can't see through it; both are NOT trusted as a real head and
      fall through to the implicit verification below.)
    * No ``--head`` (or an empty one) → gh may push the CURRENT branch when it
      isn't fully on the remote. Verified against the ACTUAL remote with ``git
      ls-remote`` — not the LOCAL remote-tracking ref, which goes stale the moment
      a merged branch is deleted (a squash-merge auto-delete leaves
      refs/remotes/origin/<branch> pointing at a gone branch, so a local-ref check
      would wrongly report "already pushed"). Accept only the ls-remote line whose
      ref path is EXACTLY ``refs/heads/<branch>`` (a bare pattern tail-matches
      namespaced refs).

    Assumes origin's push and fetch destinations coincide (standard single-remote
    setup). Fail-safe — any uncertainty (network error, timeout, current branch
    not on the remote, unpushed commits, detached HEAD) → True (gate). The
    ls-remote call is bounded by a 10s timeout inside the hook's 60s budget; a
    hung/slow remote fails closed, never open.
    """
    raw_head = _pr_create_head_raw(argv)
    if raw_head and "$" not in raw_head and "`" not in raw_head:
        # A NON-EMPTY, plausibly-LITERAL --head → gh skips push/fork → un-gate.
        # An empty head (--head= / --head "") is gh's implicit path; a value with
        # shell-expansion metacharacters ($VAR / $(...) / `...`) could resolve to
        # empty (→ implicit push) at runtime and we can't see through it — both
        # fall through to the live current-branch verification below (fail-safe).
        return False
    branch = _current_branch()
    if not branch:
        return True  # detached / unknown current branch → can't verify → gate
    try:
        head_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        if not head_sha:
            return True  # can't resolve HEAD → gate
        remote = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if remote.returncode != 0:
            return True  # cannot reach/read the remote → gate (fail-safe)
        target_ref = f"refs/heads/{branch}"
        remote_sha = ""
        for ln in remote.stdout.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == target_ref:
                remote_sha = parts[0]
                break
        if not remote_sha:
            return True  # current branch not on the remote → gh would push it → gate
        if head_sha == remote_sha:
            return False  # current branch tip is on the remote → nothing to push
        # HEAD differs from the remote tip: un-gate only if HEAD is already
        # contained in the remote branch (needs the object locally; if absent,
        # is-ancestor fails → gate).
        contained = (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", remote_sha],
                capture_output=True,
                timeout=5,
            ).returncode
            == 0
        )
        return not contained  # unpushed commits on HEAD → gh may push → gate
    except Exception:
        return True  # fail-safe → gate


def main() -> int:
    try:
        cmd = field(read_payload(), "command")
        if not cmd:
            return 0

        # Analyze the command into the segments it actually executes (wrappers
        # like sudo/env and /path/to/git stripped, nested `bash -c` recursed,
        # quoted mentions excluded). Each guarded subcommand is matched on real
        # argv, and the `# review-override` approval binds to its OWN segment.
        segs = analyze(cmd)
        push_segs = [s for s in segs if s.exe == "git" and git_subcommand(s.argv) == "push"]
        merge_git_segs = [s for s in segs if s.exe == "git" and git_subcommand(s.argv) == "merge"]
        create_segs = [s for s in segs if gh_pr_subcommand(s.argv) == "create"]
        merge_pr_segs = [s for s in segs if gh_pr_subcommand(s.argv) == "merge"]

        # Each git push / gh pr merge is a SEPARATE gated action. A single Bash
        # command carrying more than one would collapse into ONE ask/gate
        # (evaluated only for the first), so approving it would run every push —
        # and every UNCHECKED merge — behind it. Reject the compound and require
        # each to be its own separately-gated tool call. (Merges are included so
        # `gh pr merge 1 --admin && gh pr merge 2 --admin` can't smuggle the
        # second past the CI/review gates, which only inspect the first segment.
        # gh pr create is un-gated (#1241) — a review request on already-pushed
        # code, riding a push's approval — so it is NOT counted here; the
        # exception where gh itself would push an unpushed branch is handled by
        # the create arm below.)
        if len(push_segs) + len(merge_pr_segs) > 1:
            print(
                "BLOCKED: multiple publish/merge operations (git push / gh pr "
                "merge) in one command would share a single gate. Run each as "
                "its own command so each is gated separately.",
                file=sys.stderr,
            )
            return 2

        # An interactive push defers to a native approve/deny dialog at the END
        # of main(), so every hard-block below (merge-into-main, the pr-merge
        # gates, sqlite, --no-verify) still takes precedence — a compound
        # `git push && git commit --no-verify` blocks, never asks.
        ask_reason: str | None = None

        # ── git push (any branch) ──────────────────────────────────
        # Interactive → the user approves in a dialog only they can satisfy.
        # Dispatched/autonomous → hard-denied (no human to ask; real autonomous
        # delivery goes through the scope-gated server path, not the CC Bash
        # tool). The old `# review-override` token is dropped for push: the
        # dialog replaces it, so the agent can no longer self-approve.
        if push_segs:
            # Force push is destructive — hard-block in EVERY session (never a
            # mere ask). argv-based (via shell_parse) so a quoted `'-f'` or a
            # `--force-with-lease` cannot evade it into a generic approval prompt.
            if any(_push_is_force(s.argv) for s in push_segs):
                print(
                    "BLOCKED: Force push is not allowed — open a PR instead.",
                    file=sys.stderr,
                )
                return 2
            _remote, branch = _get_push_remote_and_branch(cmd)
            if _is_dispatched():
                print(
                    f"BLOCKED: git push requires user approval before "
                    f"publishing code externally (target: {branch or 'default'}).",
                    file=sys.stderr,
                )
                print(
                    "Autonomous/dispatched sessions cannot push directly — "
                    "delivery goes through the scope-gated server path.",
                    file=sys.stderr,
                )
                return 2
            ask_reason = (
                f"git push needs your approval before publishing externally "
                f"(target: {branch or 'default'})."
            )

        # ── git merge into main ─────────────────────────────────────
        if merge_git_segs:
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

        # ── gh pr create ────────────────────────────────────────────
        # Un-gated when its branch is already on the remote — opening a PR is then
        # just a review request on already-pushed code, so `git push && gh pr
        # create` prompts once (for the push) and the create rides along. BUT a
        # bare create from an UNPUSHED branch makes gh push (and possibly fork) the
        # branch itself — a code-publish that would bypass the push gate — so gate
        # that form like a push: dispatched → deny, interactive → ask.
        if create_segs and any(_pr_create_would_publish(s.argv) for s in create_segs):
            if _is_dispatched():
                print(
                    "BLOCKED: this gh pr create would push a not-yet-pushed branch; "
                    "autonomous/dispatched sessions cannot publish code.",
                    file=sys.stderr,
                )
                return 2
            if ask_reason is None:
                ask_reason = (
                    "gh pr create would push this (not-yet-pushed) branch — approve it like a push."
                )

        # ── gh pr merge ────────────────────────────────────────────
        if merge_pr_segs:
            merge_seg = merge_pr_segs[0]
            if "--admin" not in merge_seg.argv:
                print(
                    "BLOCKED: gh pr merge without --admin is not allowed.",
                    file=sys.stderr,
                )
                print(
                    "Use: gh pr merge --squash --admin",
                    file=sys.stderr,
                )
                return 2

            # Check mergeable status before allowing merge. Merge is
            # the one command that fails CLOSED: if we can't tell which
            # PR this is, we can't run the gates, so we don't merge.
            pr_num = _resolve_pr_number(cmd)
            if pr_num is None:
                print(
                    "BLOCKED: cannot resolve which PR this merges "
                    "(no number in the command and no open PR for the "
                    "current branch).",
                    file=sys.stderr,
                )
                print(
                    "Specify the PR number: gh pr merge <N> --squash --admin",
                    file=sys.stderr,
                )
                return 2
            if pr_num:
                mergeable = _check_mergeable(pr_num)
                if mergeable == "UNKNOWN":
                    print(
                        f"BLOCKED: PR #{pr_num} mergeable status is UNKNOWN.",
                        file=sys.stderr,
                    )
                    print(
                        "GitHub hasn't finished conflict analysis. Wait and retry.",
                        file=sys.stderr,
                    )
                    return 2
                if mergeable == "CONFLICTING":
                    print(
                        f"BLOCKED: PR #{pr_num} has merge conflicts. Resolve before merging.",
                        file=sys.stderr,
                    )
                    return 2

                # CI-status gate. On an unprotected default branch,
                # mergeStateStatus=CLEAN is NOT a CI verdict (it only means "no
                # conflict / no REQUIRED check blocking", and nothing is required
                # when the branch is unprotected). So --admin merges would sail
                # past a red `test` job — exactly how main was broken on
                # 2026-07-28. Block red/pending CI unless a conscious, separate
                # `# ci-override` is appended (never waived by --admin or
                # # review-override). Fail-OPEN on "unknown" so a transient API
                # hiccup can't wedge merges.
                ci_state, ci_bad = _pr_ci_status(pr_num)
                ci_override = has_trailing_override(merge_seg.raw, "ci-override")
                if ci_state in ("red", "pending") and not ci_override:
                    print(
                        f"BLOCKED: PR #{pr_num} CI is {ci_state.upper()} "
                        f"({', '.join(ci_bad[:6])}).",
                        file=sys.stderr,
                    )
                    print(
                        f"Do NOT merge red/pending CI. Wait for green "
                        f"(gh pr checks {pr_num}). If these checks are a known, "
                        "documented pre-existing flake you are consciously "
                        "accepting, append a trailing '# ci-override' to merge "
                        "anyway (logged).",
                        file=sys.stderr,
                    )
                    return 2
                if ci_state in ("red", "pending"):
                    print(
                        f"NOTE: CI {ci_state.upper()} on #{pr_num} "
                        f"({', '.join(ci_bad[:6])}) — merging via # ci-override "
                        "(consciously accepted).",
                        file=sys.stderr,
                    )

                # Check for unresolved review findings
                force_override = merge_seg.override
                should_block, review_msg = _check_pr_review_findings(
                    pr_num,
                    force=force_override,
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
                    pr_num,
                    force=force_override,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} has unresolved INLINE review findings.",
                        file=sys.stderr,
                    )
                    print(inline_msg, file=sys.stderr)
                    return 2

        # ── sqlite3 write operations ────────────────────────────────
        # NB: match on the raw command, not the unquoted one — the DML keyword
        # lives inside the quoted SQL argument (sqlite3 db "DELETE FROM ...").
        if "sqlite3" in cmd and re.search(
            r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE)\b",
            cmd,
            re.IGNORECASE,
        ):
            print(
                "BLOCKED: Direct database writes via sqlite3 are not allowed. "
                "Use CRUD modules or MCP tools instead.",
                file=sys.stderr,
            )
            return 2

        # ── git commit --no-verify / -n (any executed segment) ─────
        if any(commit_skips_hooks(s.argv) for s in segs):
            print(
                "BLOCKED: --no-verify / -n bypasses review enforcement hooks. "
                "Remove it and run /review first.",
                file=sys.stderr,
            )
            return 2

        # ── Process kill (soft warn) ──────────────────────────────
        if re.search(r"(?:^|\s|&&|;)\s*(?:kill|killall|pkill)\s", cmd):
            print(
                "⚠️  STOP: Process kill detected. Have you received explicit user approval?",
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

        # ── Interactive push / PR-create approval prompt (deferred) ──
        # Reached only if no hard-block above returned. Dispatched sessions
        # were already denied inline; here, an interactive human session gets a
        # native approve/deny dialog for its push / PR-create.
        if ask_reason is not None:
            return _ask(ask_reason)

        # A standalone, un-gated `gh pr create` (branch already on the remote, or
        # an explicit --head that gh won't push): reaching here means nothing
        # needed approval. Emit an explicit `allow` so CC's OWN permission prompt
        # doesn't fire for this non-allow-listed command — the un-gate is only
        # meaningful if it actually suppresses the prompt. (A push in the same
        # command would have set ask_reason above, so this never overrides a push.)
        if create_segs:
            return _allow(
                "gh pr create cannot publish code here (explicit --head, or current "
                "branch already on the remote) — no push, so no separate approval"
            )

    except Exception:
        pass  # Fail-open on any error — never block legitimate work

    return 0


if __name__ == "__main__":
    sys.exit(main())
