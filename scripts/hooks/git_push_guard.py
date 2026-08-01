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
from hook_input import field, read_payload, run_guard  # noqa: E402
from shell_parse import (  # noqa: E402
    analyze,
    commit_skips_hooks,
    gh_pr_subcommand,
    git_subcommand,
    has_trailing_override,
    split_segments,
)

# Sentinel: the effective cwd cannot be confidently resolved (a cd into a
# variable/command-substitution, a subshell, or a target nested at depth>0).
# Callers MUST fail closed on it — block the merge, do not soften a force push.
_CWD_UNKNOWN = object()

# git global options that consume the FOLLOWING token as their value — used to
# skip past `git -C <dir>` / `git -c KEY=VAL` when locating a push's positionals.
_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)


def _current_branch(cwd: str | None = None) -> str | None:
    """Get current git branch name, optionally in a specific working dir.

    When ``cwd`` is truthy, run ``git -C <cwd> branch --show-current`` so the
    branch reflects the worktree the command actually targets — not the hook's
    own cwd (always the main tree, on ``main``). Fail-safe: None on any error.
    """
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["branch", "--show-current"]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None


def _seg_dash_C(argv) -> str | None:
    """The dir named by a ``git -C <dir>`` in a segment's argv, else None."""
    argv = argv or []
    for i, tok in enumerate(argv):
        if tok == "-C" and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _cd_target(raw: str):
    """Classify a top-level command segment as a ``cd``.

    Returns the literal target dir when ``raw`` is a plain ``cd <literal-path>``
    segment; ``_CWD_UNKNOWN`` when it is a cd we cannot resolve (no arg, ``cd -``,
    a variable/command-substitution/glob target, or a subshell/group that would
    scope the cd); ``None`` when it is not a cd at all. Because ``split_segments``
    already split on shell operators, a genuine cd segment is exactly ``cd <path>``.
    """
    s = raw.strip()
    # A subshell/group ( … ) / { … } scopes its cd — we cannot track it.
    if s.startswith("(") or s.startswith("{"):
        return _CWD_UNKNOWN
    m = re.match(r"^cd(?:\s+(?P<p>.*))?$", s)
    if not m:
        return None
    p = (m.group("p") or "").strip()
    if not p or p == "-":
        return _CWD_UNKNOWN  # `cd` (home) / `cd -` (previous) — unresolvable here
    # Quoted target.
    if len(p) >= 2 and p[0] in "'\"" and p[-1] == p[0]:
        inner = p[1:-1]
        if p[0] == '"' and ("$" in inner or "`" in inner):
            return _CWD_UNKNOWN  # double-quote expansion
        return inner
    if " " in p or "\t" in p:
        return _CWD_UNKNOWN  # extra args / unexpected shape → don't guess
    if p.startswith("~"):
        p = os.path.expanduser(p)
    if any(ch in p for ch in "$`*?"):
        return _CWD_UNKNOWN  # expansion or glob → unresolvable
    return p


def _resolve_against(current, target: str):
    """Resolve a possibly-relative ``cd``/``-C`` target to an ABSOLUTE path.

    An absolute target is normalized and returned (it recovers even from a prior
    ``_CWD_UNKNOWN``). A relative target is joined onto ``current``; if
    ``current`` is unknown/None the result is ``_CWD_UNKNOWN`` (fail closed) —
    a relative path with no known base cannot be resolved, and running
    ``git -C <relative>`` from the hook's own cwd would silently target the wrong
    tree (P1-A).
    """
    if os.path.isabs(target):
        return os.path.normpath(target)
    if not isinstance(current, str) or not current:
        return _CWD_UNKNOWN
    return os.path.normpath(os.path.join(current, target))


def _effective_cwd(cmd: str, payload: dict, seg=None):
    """The ABSOLUTE directory where a SINGLE target segment actually runs.

    Returns ``str`` (resolved), ``None`` (no cwd info → run in the hook's own
    cwd), or ``_CWD_UNKNOWN`` (ambiguous → caller must fail closed). Resolution:
      1. If the target is nested (depth>0, inside bash -c/subshell/$()), UNKNOWN.
      2. The LAST top-level ``cd`` that runs BEFORE the target segment — bash
         applies cds sequentially, so the last one wins; relative cds/-C are
         resolved against the running cwd, and an unresolvable one ⇒ UNKNOWN.
      3. ``git -C <dir>`` on the target segment's argv overrides, resolved
         against the cwd in effect at the target.
    Used for single-target callers (push); the merge check uses the multi-target
    walk in ``_walk_merge_into_main`` so every merge in a compound is covered.
    """
    if seg is not None and getattr(seg, "depth", 0) > 0:
        return _CWD_UNKNOWN
    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    target_raw = getattr(seg, "raw", None)
    if target_raw is not None:
        for raw in split_segments(cmd):
            if raw == target_raw:
                break
            cd = _cd_target(raw)
            if cd is _CWD_UNKNOWN:
                cur = _CWD_UNKNOWN
            elif cd is not None:
                cur = _resolve_against(cur, cd)
    if seg is not None:
        dash_c = _seg_dash_C(getattr(seg, "argv", None))
        if dash_c is not None:
            return _resolve_against(cur, dash_c)
    return cur


def _walk_merge_into_main(cmd: str, payload: dict, merge_git_segs: list) -> bool:
    """True if ANY executed ``git merge`` would run on main/master (fail-closed).

    Walks the top-level segments in bash order tracking the ABSOLUTE cwd (last
    ``cd`` wins; relative cds/-C resolved against it), and checks EACH ``git
    merge`` as it would actually run — so a compound like ``git -C <feat-wt>
    merge a && git merge b`` cannot smuggle the second (bare) merge into main
    behind the first. A per-segment ``# merge-to-main-override`` acknowledges that
    segment. Fail closed: a merge nested at depth>0, reached under an unresolvable
    cwd, OR whose branch cannot be read (None) is treated as targeting main and
    blocked (unless overridden). A detached HEAD ("") is left allowed.
    """
    # depth>0 merges cannot be associated with a top-level cwd → fail closed.
    for s in merge_git_segs:
        if getattr(s, "depth", 0) > 0 and not has_trailing_override(
            s.raw, "merge-to-main-override"
        ):
            return True

    base = payload.get("cwd") if isinstance(payload, dict) else None
    cur = os.path.normpath(base) if isinstance(base, str) and base else None
    for raw in split_segments(cmd):
        top = [s for s in analyze(raw) if getattr(s, "depth", 0) == 0]
        merge_here = next(
            (s for s in top if s.exe == "git" and git_subcommand(s.argv) == "merge"),
            None,
        )
        if merge_here is not None and not has_trailing_override(raw, "merge-to-main-override"):
            dash_c = _seg_dash_C(merge_here.argv)
            mcwd = _resolve_against(cur, dash_c) if dash_c is not None else cur
            if mcwd is _CWD_UNKNOWN:
                return True
            branch = _current_branch(cwd=mcwd if isinstance(mcwd, str) else None)
            if branch is None or branch in ("main", "master"):
                return True  # None branch (error/unresolved) fails closed
        cd = _cd_target(raw)
        if cd is _CWD_UNKNOWN:
            cur = _CWD_UNKNOWN
        elif cd is not None:
            cur = _resolve_against(cur, cd)
    return False


def _get_push_remote_and_branch(seg, cwd: str | None = None) -> tuple[str | None, str | None]:
    """The (remote, destination-branch) a ``git push`` segment targets.

    Parsed from the quote-stripped ``seg.argv`` via ``_push_positionals`` — NOT a
    naive ``cmd.split()``, which mis-skips value flags (``-o <val>``) and wrongly
    treats no-value flags like ``-u`` / ``--set-upstream`` as consuming the next
    token (collapsing the branch to the wrong value — a security bug once the
    branch feeds an approval decision). Positional handling mirrors
    ``_push_named_remote``:
      • no positional   → bare ``git push``               → (``"upstream"``, current branch);
      • one positional  → ``git push <remote>``           → (remote, current branch);
      • two positionals → ``git push <remote> <refspec>`` → (remote, refspec DST).
    Used for the approval-dialog LABEL; the safety decision uses the stricter
    ``_push_targets_current_branch``. Returns (None, None) if not a push.
    """
    argv = getattr(seg, "argv", None) or []
    if git_subcommand(argv) != "push":
        return None, None
    pos = _push_positionals(argv)
    if not pos:
        # Bare 'git push' — pushes current branch to its upstream
        return "upstream", _current_branch(cwd=cwd)
    if len(pos) == 1:
        # 'git push origin' — pushes current branch to remote
        return pos[0], _current_branch(cwd=cwd)
    # 'git push origin main' or 'git push origin feature:main'
    remote, refspec = pos[0], pos[1]
    branch = refspec.split(":")[-1] if ":" in refspec else refspec
    return remote, branch


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

# The first-push-only skip uses an ALLOWLIST posture (git's push surface is too
# flexible to blocklist safely): a push qualifies as a "plain current-branch
# update" ONLY if every flag it carries is ref-set-neutral — verbosity, dry-run,
# upstream tracking, transport — never one that changes WHICH refs are pushed.
# Anything outside these sets (``--all`` / ``--tags`` / ``--mirror`` / ``--delete``
# / ``--prune`` / ``--repo`` / ``--stdin`` / ``--follow-tags`` / a bundled ``-d`` /
# any unknown flag) forces the approval prompt.
_PUSH_SAFE_LONG_FLAGS = frozenset(
    {
        "--set-upstream",
        "--verbose",
        "--quiet",
        "--dry-run",
        "--progress",
        "--no-progress",
        "--porcelain",
        "--atomic",
        "--no-atomic",
        "--ipv4",
        "--ipv6",
        "--thin",
        "--no-thin",
    }
)
# Ref-neutral value flags (skip the flag AND its value token). ``--repo`` (redirects
# the push) and ``--receive-pack``/``--exec`` (select a receive-pack PROGRAM that git
# EXECUTES on local/SSH transports — an arbitrary-code vector) are deliberately
# EXCLUDED, so a push carrying any of them falls through to the approval prompt.
_PUSH_SAFE_VALUE_FLAGS = frozenset({"-o", "--push-option"})
# Ref-neutral short-flag letters (for bundles like ``-uq``). ``o`` is handled
# separately (glued push-option value). ``f`` (force) and ``d`` (delete) are absent
# by design — a bundle containing either is not a plain current-branch update.
_PUSH_SAFE_SHORT_LETTERS = frozenset("uvqn46")


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


def _push_named_remote(argv: list[str]) -> str | None:
    """The remote named on a ``git push <remote> …``, else None.

    argv is quote-stripped from ``shell_parse``. Skips git global options (and
    their values), the ``push`` token, and push flags/values; the first bare
    positional after that is the remote. A ``+<refspec>`` positional is NOT a
    remote (it is a force refspec) and is skipped.
    """
    i = 1  # skip argv[0] == "git"
    # advance past git global options to the `push` token
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return None
    i += 1
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-") or t.startswith("+"):
            i += 1
            continue
        return t  # first bare positional after `push` is the remote
    return None


def _push_repo_flag(argv: list[str]) -> str | None:
    """The value of a ``--repo <value>`` / ``--repo=value`` on a git push, else None.

    ``git push --repo <dest>`` overrides both the positional remote and the
    branch upstream as the push DESTINATION (P1-C), so it must win when deciding
    what a force push actually targets. The value may be a remote name OR a URL.
    """
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == "--repo" and i + 1 < len(argv):
            return argv[i + 1]
        if t.startswith("--repo="):
            return t.split("=", 1)[1]
        i += 1
    return None


def _push_positionals(argv) -> list[str]:
    """The bare positional args of a ``git push`` (``[remote, refspec, ...]``).

    Parsed from a quote-stripped argv, mirroring ``_push_named_remote``: git global
    options/values, the ``push`` token, and push flags/values are all skipped —
    including no-value flags (``-u`` / ``--set-upstream`` / ``--force-with-lease``)
    which take NO separate token, and value flags (``_PUSH_VALUE_FLAGS``:
    ``-o`` / ``--push-option`` / ``--repo`` / ``--receive-pack`` / ``--exec``) which
    take one. A ``+<refspec>`` (force) positional is excluded. Empty if not a push.
    """
    argv = argv or []
    i = 1  # skip argv[0] == "git"
    # advance past git global options (and their values) to the `push` token
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return []
    i += 1
    out: list[str] = []
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-") or t.startswith("+"):
            i += 1
            continue
        out.append(t)
        i += 1
    return out


# push.default modes that push the CURRENT branch to a SAME-NAMED remote ref (or
# refuse). `upstream`/`tracking` push to a possibly-differently-named upstream, and
# `matching` pushes every same-named branch — both broaden beyond a plain
# current-branch update. Unset defaults to `simple` (safe), so it need not appear.
_SAFE_PUSH_DEFAULTS = frozenset({"simple", "current"})


def _git_config_get(base: list[str], key: str, *, all_values: bool = False, as_bool: bool = False):
    """Read a git config value. Returns ``(rc, stripped_stdout)``, or ``None`` on a
    read error / timeout / UNEXPECTED return code.

    git config exits 0 when the key is set and 1 when it is absent; any other code
    (bad config file, etc.) is an error the caller must treat as fail-closed — so
    an empty stdout under rc 2/128 is NOT mistaken for "unset". ``as_bool`` reads via
    ``--type=bool`` so every boolean spelling (``TRUE`` / ``on`` / the valueless
    ``[section]\\n\\tkey`` shorthand) normalizes to a canonical ``"true"``/``"false"``.

    NOTE: this reads the effective REPO config; it does NOT see a command-line
    ``git -c key=val`` override on the push itself (tabled residue — the config
    check is best-effort for the common repo-config case, not a hard boundary).
    """
    args = ["config"]
    if as_bool:
        args.append("--type=bool")
    args.append("--get-all" if all_values else "--get")
    args.append(key)
    try:
        r = subprocess.run(base + args, capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode not in (0, 1):
        return None
    return r.returncode, r.stdout.strip()


# push.recurseSubmodules values that do NOT push submodule commits (safe). Anything
# else (on-demand / only / a boolean-true spelling) side-channels a submodule push.
_SAFE_RECURSE_SUBMODULES = frozenset({"no", "false", "off", "0", "check"})


def _push_config_is_simple(remote: str | None, cwd: str | None = None) -> bool:
    """Whether a bare / remote-only push updates ONLY the current branch under the
    effective REPO config — ALLOWLIST posture (mirrors ``_push_targets_current_branch``).

    Broadening/redirecting knobs checked (each case-insensitive):
      • ``remote.<remote>.push`` refspec (``push = HEAD:main`` → sends HEAD to main);
      • ``remote.<remote>.mirror`` (a bare push mirrors ALL refs — force-updates /
        deletes unrelated remote refs);
      • ``push.default`` other than ``simple``/``current`` (``upstream``/``tracking``
        push cur to a differently-named upstream; ``matching`` pushes every
        same-named branch);
      • ``push.recurseSubmodules``/``submodule.recurse`` (side-channel-publishes
        submodule commits).
    Simple ONLY when NONE is set to a broadening value. Fail-closed: an unresolved
    remote, any of the above, or any config-read error → False (prompt).

    BEST-EFFORT, not a hard boundary (`remote` is already resolved with git's
    pushRemote/pushDefault precedence by the caller). It reads REPO config only, so a
    command-line ``git -c key=val push`` override, ``remote.<remote>.mirror``,
    ``push.followTags``, or a ``url.*.pushInsteadOf`` rewrite are NOT caught — tabled
    adversarial residue (each needs a deliberately unusual command / hostile config,
    and `git config` writes are already soft-warned). Bounded by 5s timeouts.
    """
    if not remote:
        return False
    base = ["git"] + (["-C", cwd] if cwd else [])
    # 1. A configured push refspec can redirect/broaden the destination.
    got = _git_config_get(base, f"remote.{remote}.push", all_values=True)
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out:
        return False
    # 1b. remote.<remote>.mirror makes a bare push mirror EVERY ref (force/delete).
    got = _git_config_get(base, f"remote.{remote}.mirror", as_bool=True)
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out == "true":
        return False
    # 2. push.default must be a same-name mode (unset → simple → safe).
    got = _git_config_get(base, "push.default")
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out and out.lower() not in _SAFE_PUSH_DEFAULTS:
        return False
    # 3. Submodule recursion would also publish submodule commits (a side channel).
    got = _git_config_get(base, "push.recurseSubmodules")
    if got is None:
        return False
    rc, out = got
    if rc == 0 and out and out.lower() not in _SAFE_RECURSE_SUBMODULES:
        return False  # on-demand / only / true → pushes submodule commits
    # submodule.recurse is a boolean — read via --type=bool so TRUE / on / the
    # valueless shorthand all normalize to a canonical "true".
    got = _git_config_get(base, "submodule.recurse", as_bool=True)
    if got is None:
        return False
    rc, out = got
    return not (rc == 0 and out == "true")


def _push_targets_current_branch(
    seg, cur: str | None, remote: str | None, cwd: str | None = None
) -> bool:
    """Whether a non-force ``git push`` seg plainly UPDATES the current branch ``cur``.

    ``remote`` is the destination the push will ACTUALLY go to, resolved by the
    caller with git's pushRemote/pushDefault precedence (``_effective_push_remote``).

    ALLOWLIST posture — the branch checked against the remote is always ``cur``
    itself, never a parsed destination. True ONLY when BOTH hold:
      1. Every flag after ``push`` is ref-set-neutral — a member of
         ``_PUSH_SAFE_LONG_FLAGS`` / ``_PUSH_SAFE_VALUE_FLAGS``, or a short bundle
         whose every letter is in ``_PUSH_SAFE_SHORT_LETTERS`` (``o`` = glued
         push-option value). ANY other flag (``--all`` / ``--tags`` / ``--delete``
         / a bundled ``-d`` / ``--stdin`` / ``--repo`` / unknown) → False.
      2. The positionals name a plain current-branch update:
         • ``git push <remote> <cur>`` (no ``src:dst`` colon) → an explicit refspec
           overrides ``remote.push`` / ``push.default`` / ``pushRemote`` → True;
         • bare ``git push`` / ``git push <remote>`` → True only if
           ``_push_config_is_simple(remote)`` (no redirecting/broadening repo config);
         • a colon refspec, a differently-named branch, or ≥2 refspecs → False.
    Conservative by construction: any unrecognized form re-prompts. argv-based
    (quote-stripped).
    """
    if not cur:
        return False
    argv = getattr(seg, "argv", None) or []
    # Advance past git global options to the `push` token.
    i = 1
    while i < len(argv):
        t = argv[i]
        if t in _GIT_GLOBAL_VALUE_FLAGS:
            i += 2
            continue
        if t.startswith("-"):
            i += 1
            continue
        break
    if i >= len(argv) or argv[i] != "push":
        return False
    i += 1
    positionals: list[str] = []
    while i < len(argv):
        t = argv[i]
        if t in _PUSH_SAFE_VALUE_FLAGS:
            i += 2  # ref-neutral value flag: skip the flag and its value token
            continue
        if t.startswith("--"):
            base = t.split("=", 1)[0]
            if "=" in t and base in _PUSH_SAFE_VALUE_FLAGS:
                i += 1  # --push-option=value etc.
                continue
            if "=" not in t and base in _PUSH_SAFE_LONG_FLAGS:
                i += 1
                continue
            return False  # unknown/broadening long flag (or a =form of a no-value flag)
        if t.startswith("+"):
            return False  # +<refspec> force shorthand
        if t.startswith("-") and len(t) > 1:
            # Short single/bundle — every letter must be ref-neutral. An `o` starts a
            # glued push-option value, so the rest of the token is that value.
            safe = True
            for ch in t[1:]:
                if ch == "o":
                    break
                if ch not in _PUSH_SAFE_SHORT_LETTERS:
                    safe = False
                    break
            if not safe:
                return False
            i += 1
            continue
        positionals.append(t)
        i += 1
    if len(positionals) >= 3:
        return False  # multiple refspecs → not a single plain current-branch update
    if len(positionals) == 2:
        refspec = positionals[1]
        # Explicit `<remote> <cur>` — an explicit refspec overrides remote.push /
        # push.default / pushRemote, so it is a plain current-branch update.
        return ":" not in refspec and refspec == cur
    # Bare `git push` or `git push <remote>` → the ref set depends on repo config,
    # keyed on the remote git will ACTUALLY push to (resolved by the caller).
    return _push_config_is_simple(remote, cwd=cwd)


def _resolve_push_remote(seg, cwd: str | None = None) -> str | None:
    """The push DESTINATION (remote name or URL) for a segment, or None if UNKNOWN.

    Resolution order: an explicit ``--repo <dest>`` (P1-C) → an explicitly named
    positional remote → else the current branch's upstream remote (the part
    before ``/`` of ``@{upstream}``); else None. Callers FAIL CLOSED on None — an
    undeterminable destination is treated as origin/public and blocked.
    """
    argv = getattr(seg, "argv", None) or []
    repo = _push_repo_flag(argv)
    if repo:
        return repo
    remote = _push_named_remote(argv)
    if remote:
        return remote
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["rev-parse", "--abbrev-ref", "@{upstream}"]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            upstream = result.stdout.strip()
            if "/" in upstream:
                return upstream.split("/", 1)[0]
    except Exception:
        pass
    return None


def _effective_push_remote(seg, cur: str | None, cwd: str | None = None) -> str | None:
    """The remote a ``git push`` will ACTUALLY push to, honoring git's precedence.

    An explicit ``--repo`` or positional remote wins. For a bare ``git push`` git
    picks, in order: ``branch.<cur>.pushRemote`` → ``remote.pushDefault`` →
    ``branch.<cur>.remote`` (the ``@{upstream}`` remote) → ``origin``. The
    republish + config checks MUST target this remote, not the fetch/upstream
    remote — otherwise a triangular fork workflow (pull from origin, push to fork)
    checks the wrong remote and can silently allow a first push to the fork.
    """
    argv = getattr(seg, "argv", None) or []
    if _push_repo_flag(argv) or _push_named_remote(argv):
        return _resolve_push_remote(seg, cwd=cwd)  # explicit --repo / positional wins
    base = ["git"] + (["-C", cwd] if cwd else [])
    if cur:
        got = _git_config_get(base, f"branch.{cur}.pushRemote")
        if got and got[0] == 0 and got[1]:
            return got[1]
    got = _git_config_get(base, "remote.pushDefault")
    if got and got[0] == 0 and got[1]:
        return got[1]
    return _resolve_push_remote(seg, cwd=cwd) or "origin"


def _looks_like_url(dest: str) -> bool:
    """Whether a push destination is a URL/path rather than a remote NAME."""
    return (
        "://" in dest
        or "@" in dest
        or ":" in dest  # scp-like git@host:path or host:path
        or dest.startswith(("/", "./", "../", "~"))
    )


def _remote_push_urls(name: str, cwd: str | None = None) -> set[str]:
    """The set of PUSH urls configured for a remote NAME (empty if unresolvable).

    git pushes to the PUSH url, which can differ from the fetch url
    (``git remote set-url --push``), so classifying by the fetch url misses a
    public push target (P1-B). ``--push --all`` returns every push url (one per
    line). An empty set means the name is not a resolvable remote → callers FAIL
    CLOSED.
    """
    try:
        args = ["git"]
        if cwd:
            args += ["-C", cwd]
        args += ["remote", "get-url", "--push", "--all", name]
        result = subprocess.run(args, capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {ln.strip() for ln in result.stdout.splitlines() if ln.strip()}
    except Exception:
        pass
    return set()


def _push_dest_urls(dest: str, cwd: str | None = None) -> set[str]:
    """Push-url set for a destination that may be a remote NAME or a raw URL/path.

    A known remote name resolves to its ``--push`` urls; otherwise, if the
    destination looks like a URL/path (e.g. a ``--repo https://…`` value), it IS
    the target url. An unresolvable name yields an empty set (fail closed).
    """
    urls = _remote_push_urls(dest, cwd)
    if urls:
        return urls
    if _looks_like_url(dest):
        return {dest}
    return set()


def _remote_branch_sha(remote: str, branch: str, cwd: str | None = None) -> str | None:
    """The remote's tip sha for EXACTLY ``refs/heads/<branch>``, or None.

    Queries the LIVE remote via ``git ls-remote`` (not a local remote-tracking
    ref, which goes stale the moment a remote branch is deleted). Accepts only the
    line whose ref path is EXACTLY ``refs/heads/<branch>`` — a bare pattern
    tail-matches namespaced refs. Fail-safe: None on rc!=0 / timeout / any error /
    absent branch — callers treat None as "not confirmed present". Bounded by a 10s
    timeout inside the hook's 60s budget.
    """
    try:
        args = ["git"] + (["-C", cwd] if cwd else []) + ["ls-remote", "--heads", remote, branch]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        target_ref = f"refs/heads/{branch}"
        for ln in result.stdout.splitlines():
            parts = ln.split()
            if len(parts) >= 2 and parts[1] == target_ref:
                return parts[0]
    except Exception:
        pass
    return None


def _push_is_republish(remote: str | None, branch: str | None, cwd: str | None = None) -> bool:
    """Whether this push targets a branch ALREADY on ``remote`` (a re-push).

    A branch's FIRST push creates it on the remote (and prompted the user for
    approval); a re-push updates that already-published branch. So a branch present
    on the remote was approved on its first push and must not re-prompt. Fail-safe:
    an unresolved remote/branch, or any ls-remote error/timeout/absence, → False
    (fall through to the approval prompt). Deliberately does NOT check for unpushed
    commits — re-pushing new fixes to an already-published branch is exactly the
    case we allow.
    """
    if not remote or not branch:
        return False
    return _remote_branch_sha(remote, branch, cwd=cwd) is not None


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
        # Live-remote check via ls-remote (exact refs/heads/<branch>), shared with
        # the push republish gate. None ⇒ unreachable OR branch absent → gate.
        remote_sha = _remote_branch_sha("origin", branch)
        if not remote_sha:
            return True  # cannot reach the remote, or branch not on it → gh would push → gate
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
        payload = read_payload()
        cmd = field(payload, "command")
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
        # A first-push-only re-push AUTO-ALLOW is ALSO deferred to the END (same
        # reason): emitting `_allow` inline would short-circuit the whole Bash
        # invocation before the hard-blocks run, so `git push <republish> && git
        # commit --no-verify` would sail through. Set the reason here; emit at the tail.
        push_allow_reason: str | None = None

        # ── git push (any branch) ──────────────────────────────────
        # Interactive → the user approves in a dialog only they can satisfy.
        # Dispatched/autonomous → hard-denied (no human to ask; real autonomous
        # delivery goes through the scope-gated server path, not the CC Bash
        # tool). The old `# review-override` token is dropped for push: the
        # dialog replaces it, so the agent can no longer self-approve.
        if push_segs:
            _pcwd = _effective_cwd(cmd, payload, seg=push_segs[0])
            pcwd_unknown = _pcwd is _CWD_UNKNOWN
            pcwd = _pcwd if isinstance(_pcwd, str) else None
            # Force push is destructive. argv-based force detection (via
            # shell_parse) so a quoted `'-f'` / `--force-with-lease` / bundled
            # `-uf` / `+refspec` all still count and cannot evade into a generic
            # prompt.
            #
            # SECURITY INVARIANT: a force push is HARD-BLOCKED in ALL sessions
            # whenever it targets the public repo. "Public" is decided by PUSH URL,
            # not remote name — an explicit `origin`, an UNKNOWN/None destination,
            # an ambiguous cwd, a `--repo origin` (P1-C), OR a differently-named
            # remote whose PUSH url set INTERSECTS origin's (e.g. `git remote add
            # mirror <origin-url>`, or a `set-url --push` to origin's url, P1-B)
            # all count as public ⇒ blocked (fail closed). Only a destination
            # whose push urls resolve AND are DISJOINT from origin's gets the
            # softer cautious-ask path — interactive asks, dispatched denies.
            force_segs = [s for s in push_segs if _push_is_force(s.argv)]
            if force_segs:
                remote = _resolve_push_remote(force_segs[0], cwd=pcwd)
                if pcwd_unknown or remote is None or remote == "origin":
                    print(
                        "BLOCKED: Force push to origin/<public> is not allowed — open a PR.",
                        file=sys.stderr,
                    )
                    return 2
                # Classify by PUSH-url set — a non-"origin" name/url that shares any
                # push url with origin is still a public force. Unresolvable ⇒ block.
                dest_urls = _push_dest_urls(remote, cwd=pcwd)
                origin_urls = _remote_push_urls("origin", cwd=pcwd)
                if not dest_urls or not origin_urls or (dest_urls & origin_urls):
                    print(
                        "BLOCKED: Force push to origin/<public> is not allowed — open a PR.",
                        file=sys.stderr,
                    )
                    return 2
                # Definitely a different repo from origin → cautious, never silent.
                if _is_dispatched():
                    print(
                        f"BLOCKED: force push rewrites remote history on "
                        f"'{remote}'; autonomous/dispatched sessions cannot "
                        f"force-push.",
                        file=sys.stderr,
                    )
                    return 2
                ask_reason = (
                    f"FORCE push detected — this REWRITES remote history on "
                    f"'{remote}' (a non-origin remote). Approve only if you "
                    f"intend to rewrite that remote's history."
                )
                # Fall through: any hard-block below still takes precedence.
            else:
                # Non-force push: interactive asks, dispatched hard-denies.
                _remote, branch = _get_push_remote_and_branch(push_segs[0], cwd=pcwd)
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
                # Prompt only on the FIRST push of a branch/PR. A branch already on
                # the remote was published — and approved — on its first push, so a
                # re-push of fixes to the SAME branch must not re-prompt (user
                # directive 2026-07-30). The check is deliberately narrow — it fires
                # ONLY for a plain update of the CURRENT branch, and the ref checked
                # against the remote is always ``cur`` itself (never a parsed
                # destination). Each guard fails safe toward the prompt:
                #   • not pcwd_unknown → an ambiguous cwd can't resolve the branch;
                #   • cur truthy → a real (non-detached) current branch;
                #   • cur not in (main, master) → a push to the default branch never
                #     goes silent (it is always on the remote);
                #   • _push_targets_current_branch → a bare / `<remote>` / `<remote>
                #     <cur>` push with only ref-neutral flags and (for bare/remote-only)
                #     a simple repo config — everything else prompts;
                #   • _push_is_republish → live ls-remote confirms `cur` is present on
                #     the remote git will ACTUALLY push to (pushRemote/pushDefault
                #     resolved by _effective_push_remote, so a triangular fork workflow
                #     checks the fork, not the upstream).
                # Dispatched sessions were hard-denied above, so this _allow is
                # unreachable for them (the human-not-agent boundary is preserved;
                # this only relaxes RE-approval of an already-approved branch).
                cur = _current_branch(cwd=pcwd)
                push_remote = _effective_push_remote(push_segs[0], cur, cwd=pcwd)
                if (
                    not pcwd_unknown
                    and cur
                    and cur not in ("main", "master")
                    and _push_targets_current_branch(push_segs[0], cur, push_remote, cwd=pcwd)
                    and _push_is_republish(push_remote, cur, pcwd)
                ):
                    # Deferred (NOT an inline return) so any hard-block in a compound
                    # command still takes precedence — see push_allow_reason above.
                    push_allow_reason = (
                        f"re-push to '{cur}' (already on the remote — approved on "
                        f"its first push); only the first push of a branch/PR prompts."
                    )
                else:
                    ask_reason = (
                        f"git push needs your approval before publishing externally "
                        f"(target: {branch or 'default'})."
                    )

        # ── git merge into main ─────────────────────────────────────
        # Worktree-aware AND compound-aware: EVERY git-merge in the command is
        # checked in the dir it actually runs (git -C / the last cd before it /
        # payload cwd), NOT just the first segment and NOT the hook's own cwd — so
        # a feature-branch merge cannot chaperone a second bare `git merge` into
        # main. A per-segment `# merge-to-main-override` acknowledges an intended
        # on-main merge; an ambiguous cwd fails closed (blocked). See
        # _walk_merge_into_main.
        if merge_git_segs and _walk_merge_into_main(cmd, payload, merge_git_segs):
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

        # A first-push-only re-push auto-allow — emitted ONLY here, after every
        # hard-block has had its chance to return 2, so a compound
        # `git push <republish> && git commit --no-verify` still hard-blocks.
        if push_allow_reason is not None:
            return _allow(push_allow_reason)

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

    except (json.JSONDecodeError, KeyError):
        # A malformed/partial payload is a parse-ambiguity fail-open (matches the
        # sibling guards). Any OTHER exception is an orchestration BUG and must
        # NOT silently allow a push/merge — it propagates to run_guard(), which
        # fails CLOSED (exit 2). The per-check network/parse helpers keep their
        # own intentional inner fail-opens; this only removes the blanket
        # swallow-everything that turned real bugs into silent allows.
        return 0

    return 0


if __name__ == "__main__":
    run_guard(main, "git_push_guard")
