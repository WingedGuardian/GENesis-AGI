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

Threat model (declared — the standing triage rule for the merge gate)
---------------------------------------------------------------------
This is a PROCESS gate for a SINGLE-AUTHOR, single-remote, github.com repo whose
PRs target the default branch. It defends the SESSION'S OWN process failures, not
an adversary:

  * merging a head Codex never reviewed, or reviewed at an earlier commit
    (``_check_codex_reviewed_head`` + the ``--match-head-commit`` binding);
  * a race with the session's OWN later push between check and merge (same
    binding — GitHub enforces it server-side);
  * an ACCIDENTAL wrong-repo merge — a bare merge run from another checkout or
    after a ``cd`` (``_derive_repo_from_cwd``); or an unresolvable ``--repo``;
  * an ACCIDENTAL base retarget that silently invalidates the review, since the
    head never moves (``_check_base_is_default``);
  * an unreadable mergeability / review status read as "clean" (allowlist
    posture: block unless a DEFINITE good value — MERGEABLE, a current review).

It does NOT model a malicious operator crafting argv to defeat the gate (they own
``--admin`` and ``# review-override`` already), NOR a multi-actor repo where a
hostile collaborator force-pushes or retargets. Findings outside this model
(adversarial argv shaping, hostile-collaborator timelines, non-github hosts) are
OUT OF SCOPE BY DESIGN — reply as such rather than adding another patch. The
cluster-safe ``--match-head-commit`` parse + fail-closed shadow-flag belt already
exceed the model (defense-in-depth), which is fine; they are not an invitation to
chase every argv-shaping edge. Conscious escapes are split by boundary so one
waiver cannot silently disarm an unrelated gate: ``# review-override`` waives the
FINDING scans (review-body + inline P1s), ``# stale-review-override`` waives the
review-CONTEXT gates (Codex-at-head freshness + base-is-default), and
``# ci-override`` waives the CI gate. A session that genuinely needs several
appends several (one trailing comment may carry multiple sigils).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time

# Self-locate so `from hook_input import …` resolves both when CC runs this as a
# script (sys.path[0] is this dir) AND when it is imported as a module for tests
# (importlib does not add the file's dir to sys.path).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# scripts/ (parent dir) for review_state — the shared escalation-cap constant, so
# the Codex-round gate below and the commit gate's Rule 3 stop at the same N.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hook_input import field, read_payload, run_guard  # noqa: E402

# SOFT dependency (mirrors review_enforcement_commit.py's guard for the SAME
# import): an unimportable review_state must degrade ONLY the round-escalation
# advisory to its documented default — never crash this module at load time.
# A module-load exception exits 1 BEFORE run_guard's fail-closed wrapper can
# convert it to a block, and CC treats non-2 as non-blocking → EVERY fail-closed
# gate in this file (force-push, merge, sqlite) would silently vanish.
try:
    from review_state import ESCALATION_ROUND_CAP  # noqa: E402
except ImportError:  # pragma: no cover — exercised via sys.modules stub in tests
    ESCALATION_ROUND_CAP = 3  # the genesis-development SKILL.md prose cap

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


# A gh --repo/-R (or PR URL) is PRESENT but cannot be resolved to a plain
# github.com OWNER/REPO (a shell variable, an enterprise HOST/OWNER/REPO, a URL
# with extra path). Callers MUST fail closed on this — gating the cwd repo while
# gh merges elsewhere is exactly the wrong-repo bug _merge_target_repo prevents.
_REPO_UNRESOLVED = object()


def _normalize_repo(value: str) -> str | None:
    """A plain github.com OWNER/REPO from a gh --repo value, or None if it cannot
    be resolved to one.

    Accepts ``owner/repo``, ``github.com/owner/repo``, and a github.com URL.
    Returns None for a shell variable (``$X`` / backtick), an enterprise
    ``HOST/OWNER/REPO`` (would misnormalize to github.com and gate the wrong
    repo), or any other shape — the caller turns None-when-present into a
    fail-closed block. Genesis is github.com-only, so refusing the exotic forms
    is safe (a user can re-issue as OWNER/REPO)."""
    if not value or "$" in value or "`" in value:
        return None
    v = value.strip().split("://", 1)[-1]  # strip scheme if a URL
    parts = [p for p in v.split("/") if p]
    # Drop a leading github.com HOST COMPONENT (exact match of the first path
    # segment — NOT a substring test, which would mishandle `github.com.evil/…`
    # or `evilgithub.com/…`; CodeQL py/incomplete-url-substring-sanitization).
    if parts and parts[0] == "github.com":
        parts = parts[1:]
    if len(parts) != 2:  # not a plain OWNER/REPO (host-prefixed / malformed)
        return None
    return f"{parts[0]}/{parts[1]}"


def _merge_target_repo(argv: list[str], cmd: str):
    """The repo a `gh pr merge` explicitly targets. Returns one of:

    * ``str`` — a normalized github.com OWNER/REPO to gate against;
    * ``None`` — NO explicit ``--repo``/``-R``/URL → gate gh's cwd repo (default);
    * ``_REPO_UNRESOLVED`` — an explicit target we cannot resolve → caller fails
      CLOSED. This is the fix for the residual the first cut missed: a variable
      ``--repo "$X"`` normalized to None and was indistinguishable from "no
      --repo", so the gates silently ran against the cwd repo while gh merged
      ``$X`` — re-opening the 2026-07-26 wrong-repo class through the fix itself.

    Sources: ``--repo <v>`` / ``--repo=<v>`` / ``-R <v>`` / ``-R<v>`` anywhere in
    the segment argv (gh pflag accepts any position), else a full PR URL in cmd.
    """
    argv = argv or []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("--repo", "-R") and i + 1 < len(argv):
            return _normalize_repo(argv[i + 1]) or _REPO_UNRESOLVED
        if tok.startswith("--repo="):
            return _normalize_repo(tok.split("=", 1)[1]) or _REPO_UNRESOLVED
        if tok.startswith("-R") and len(tok) > 2 and not tok.startswith("--"):
            return _normalize_repo(tok[2:]) or _REPO_UNRESOLVED  # glued -Rowner/repo
        i += 1
    url = re.search(r"(?:https?://)?([^/\s]+)/([^/\s]+)/([^/\s]+)/pull/\d+", cmd)
    if url:
        host, owner, repo = url.group(1), url.group(2), url.group(3)
        if "." in host and host != "github.com":
            return _REPO_UNRESOLVED  # enterprise host — can't gate via github.com
        return _normalize_repo(f"{owner}/{repo}") or _REPO_UNRESOLVED
    return None


# Merge-path SHARED-DEADLINE budget. `.claude/settings.json` kills this PreToolUse
# hook at ~60s. The gh-pr-merge gates run sequentially, so per-call timeouts alone
# cannot bound the AGGREGATE: on a degraded API where each call succeeds just under
# its own cap, the sum exceeds 60s and Claude SIGKILLs the hook MID-GATE — which
# fails toward "tool runs" and silently disengages the WHOLE gate stack (incl. the
# TOCTOU binding), the exact bypass the timeouts exist to prevent (Codex P1 #1373).
# main() computes ONE deadline before the gates and threads it through every
# merge-path gh helper via `_gh_timeout`, so the total finishes with headroom under
# 60s and each fail-closed gate reaches its own block/allow decision first.
_MERGE_GATE_BUDGET_S = 45.0
# Shared merge-path deadline (a monotonic() instant). main() sets it once before the
# gh-pr-merge gates; every merge-path gh call reads it via _gh_timeout so the AGGREGATE
# finishes with headroom under the hook's ~60s wall-clock. A module global is safe:
# this PreToolUse hook is a SINGLE-SHOT process (one command, no concurrency), and it
# stays None on every other path (push, the --check-pr report) → those use full caps.
_merge_deadline: float | None = None


def _gh_timeout(cap: float) -> float:
    """Per-call subprocess timeout under the shared merge-path deadline (``_merge_deadline``).

    ``cap`` when no merge deadline is set (every non-merge caller, and the tests, are
    unaffected). Under a deadline, the smaller of ``cap`` and the time remaining, floored
    at 1s so a nearly-expired budget makes the call fail FAST — its caller's existing
    error path then returns its fail-closed/open value — rather than overrun the
    wall-clock and get the whole hook SIGKILLed mid-gate. Never raises."""
    if _merge_deadline is None:
        return cap
    return max(1.0, min(cap, _merge_deadline - time.monotonic()))


def _derive_repo_from_cwd(cwd: str) -> str | None:
    """The OWNER/REPO gh resolves in directory ``cwd`` (``nameWithOwner``), or None.

    ``gh pr merge`` and ``gh repo view`` share gh's base-repo resolution (git
    remotes in the working dir + the ``gh-resolved`` config key), so this is the
    repo a bare (no ``--repo``) merge run FROM ``cwd`` will actually target.

    Root cause this addresses: the hook's gh queries run in the HOOK process's
    cwd, while gh executes the merge in the Bash tool's effective cwd. When those
    differ (a ``cd`` before the merge, or the session sitting in another checkout)
    a bare merge gated the wrong repo. Deriving the repo from the merge's own
    effective cwd re-aligns them. Tests inject via ``_TEST_GH_DERIVED_REPO``.
    """
    raw = os.environ.get("_TEST_GH_DERIVED_REPO")
    if raw is None:
        try:
            result = subprocess.run(
                ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): pre-gate resolution, fail-closed
                cwd=cwd,
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    got = (raw or "").strip()
    return _normalize_repo(got) if got else None


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


def _repo_args(repo: str | None) -> list[str]:
    """gh CLI args selecting an explicit target repo (empty = cwd repo)."""
    return ["--repo", repo] if repo else []


def _resolve_pr_number(cmd: str, repo: str | None = None, cwd: str | None = None) -> str | None:
    """Command PR number, else the current branch's open PR.

    No-arg `gh pr merge` from a PR branch is valid gh usage, but it
    used to skip EVERY merge gate here (the gates only ran under
    `if pr_num:`) — the 2026-07-10 audit points at this as a mechanism
    behind findings-ignored merges. Resolution failure is the caller's
    signal to fail CLOSED for merge commands.

    Numberless resolution of the branch's PR:
      * ``cwd`` given (the merge's repo was DERIVED from that cwd, F3): run
        ``gh pr view`` IN that dir with NO ``--repo`` — gh resolves the repo and
        the branch's PR together, exactly as the bare merge will, so the number
        matches the derived repo (both come from the same dir). Passing ``--repo``
        here would instead error ("argument required when using the --repo flag").
      * explicit user ``repo`` and NO ``cwd`` → fail CLOSED (None). Resolving a
        cwd-branch PR *number* and gating it against a DIFFERENT user-named repo
        would re-create the wrong-PR bug this gate kills.
      * neither → gh's own cwd (the hook process's), the legacy behavior.
    """
    pr_num = _extract_pr_number(cmd)
    if pr_num:
        return pr_num
    if repo is not None and cwd is None:
        return None  # explicit --repo + numberless → fail CLOSED (see above)
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--json", "number", "--jq", ".number"],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(
                6
            ),  # merge-path budget (see main): pre-gate resolution, fail-closed
            cwd=cwd,  # None ⇒ the hook's own cwd (legacy path)
        )
        resolved = result.stdout.strip()
        if result.returncode == 0 and resolved.isdigit():
            return resolved
    except Exception:
        pass
    return None


def _check_mergeable(pr_num: str, repo: str | None = None) -> str | None:
    """Query GitHub for PR mergeable status. Returns MERGEABLE/UNKNOWN/CONFLICTING,
    or None on a failed query. Callers fail CLOSED: the merge gate blocks unless
    the value is a definite MERGEABLE (None/"" — a failed read — no longer merges)."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_num,
                *_repo_args(repo),
                "--json",
                "mergeable",
                "--jq",
                ".mergeable",
            ],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(
                8
            ),  # merge-path budget (see main): fail-closed → a timeout BLOCKS w/ retry
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None  # unreadable → caller treats as non-MERGEABLE → blocks


# Check-run conclusions / statuses that mean the CI is NOT green.
_CI_RED_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
_CI_RED_STATES = {"FAILURE", "ERROR"}  # legacy StatusContext state
_CI_SKIP_CONCLUSIONS = {"SKIPPED", "NEUTRAL"}
_CI_GREEN = {"SUCCESS"}
# The only CheckRun.status that means "finished". Everything else
# (QUEUED/IN_PROGRESS/PENDING/WAITING/REQUESTED/…) is treated as unfinished, so
# a new/renamed non-terminal state can never be silently mistaken for green.
_CI_TERMINAL_STATUSES = {"COMPLETED"}


def _pr_ci_status(pr_num: str, repo: str | None = None) -> tuple[str, list[str]]:
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
                    *_repo_args(repo),
                    "--json",
                    "statusCheckRollup",
                    "--jq",
                    ".statusCheckRollup",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(8),  # merge-path budget (see main): fail-open → "unknown"
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

# ── Direct-sqlite-write detection ────────────────────────────────────────────
# Robust bare-keyword match (the historical approach). A SINGLE statement keyword
# is inherently immune to shell quoting/escapes AND SQL comments BETWEEN tokens,
# because it never depends on two tokens being adjacent in the raw command. The
# ONLY refinement over the historical pattern is a negative lookahead excluding a
# keyword immediately followed by `(` — i.e. the `replace(...)` scalar FUNCTION
# (or any keyword-as-function) — so a read-only SELECT using replace() no longer
# false-positives. This is strictly narrower than the old pattern for reads and
# IDENTICAL for writes (no real write statement is `KEYWORD(`), so it cannot open
# a bypass. Detection stays on the WHOLE command so a heredoc/`bash -c`/wrapper
# cannot fragment and hide a write.
#
# Two earlier approaches were REJECTED for weakening the guard, and this returns
# to the robust original: (a) exe-scoping + a read-only exemption exempted
# read-only tokens appearing in write SQL *data* and missed heredocs/wrappers;
# (b) statement-position two-token matching was bypassable by a SQL comment or a
# shell escape/quote inserted between the two tokens (`DELETE /*c*/ FROM`,
# `DELETE\ FROM`, `DELETE' 'FROM`). A single-keyword match has neither failure.
#
# Accepted limitation (NOT a regression — the historical guard did the same): a
# command that merely MENTIONS a bare keyword alongside "sqlite3" without a write
# (a `grep 'sqlite3 … DELETE' file`, or a read whose STRING VALUE is a keyword
# like `WHERE s='DELETE'`) still matches. Excluding those needs real SQL/shell
# parsing — tracked as a follow-up, not worth reintroducing bypass risk for.
_DML_KEYWORD_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE)\b(?!\s*\()",
    re.IGNORECASE,
)


def _is_sqlite_write(cmd: str) -> bool:
    """Whether *cmd* issues a direct sqlite3 DML/DDL write.

    Broad on purpose (any mention of ``sqlite3`` in the whole command, matching
    the historical behavior) so a heredoc/`bash -c`/wrapper cannot fragment and
    hide the write. A DML keyword must be present in non-function position (the
    negative lookahead drops the ``replace()`` scalar function).
    """
    return "sqlite3" in cmd and bool(_DML_KEYWORD_RE.search(cmd))


def _inline_title(body: str) -> str:
    """First readable line of an inline finding body."""
    first = _INLINE_MARKUP_RE.sub("", body).strip().splitlines()
    return (first[0].strip() if first else "")[:120]


def _scan_unreadable(strict: bool, what: str) -> tuple[bool, str]:
    """Return value for a finding scan that could NOT be read — a gh error,
    timeout, or malformed JSON — as distinct from a scan that ran and found
    nothing (an empty result / Codex quota, which stays clean either way).

    Enforcement (``strict=False``, the default) fails OPEN: a transient gh error
    must not wedge merges, and the freshness gate already requires a review to
    EXIST at the head. Report mode (``strict=True``) fails CLOSED so the canonical
    ``--check-pr`` report never presents an UNREADABLE scan as "ok" / emits a
    false "all gates pass" (Codex P2, PR #1366).
    """
    if strict:
        return True, f"could not read {what} — review status UNREADABLE (retry), not clean."
    return False, ""


def _check_inline_review_findings(
    pr_num: str,
    *,
    force: bool = False,
    repo: str | None = None,
    strict: bool = False,
) -> tuple[bool, str]:
    """Scan INLINE review comments for P1/P2 badge findings.

    Returns (should_block, message). P1 findings block unless their
    thread has a reply (engagement = read) or the merge carries
    '# review-override'. P2 findings never block but are printed to
    stderr one per line — the session must consciously accept them.
    On an UNREADABLE scan (gh error/timeout/malformed): enforcement fails OPEN;
    ``strict`` (report mode) fails CLOSED — see ``_scan_unreadable``.
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
                f"repos/{repo or ':owner/:repo'}/pulls/{pr_num}/comments",
                "--paginate",
                "--jq",
                ".[] | {id: .id, reply_to: .in_reply_to_id, "
                "login: .user.login, type: .user.type, body: .body}",
            ],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(8),  # merge-path budget (see main): advisory scan, fail-open
        )
        if result.returncode != 0:
            return _scan_unreadable(strict, "inline review comments")
        raw = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except Exception:
        return _scan_unreadable(strict, "inline review comments")

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


def _check_pr_review_findings(
    pr_num: str, *, force: bool = False, repo: str | None = None, strict: bool = False
) -> tuple[bool, str]:
    """Check PR comments for unresolved automated review findings.

    Returns (should_block, message).

    On an UNREADABLE scan (gh error/timeout/malformed JSON), enforcement fails
    OPEN — the hook must never become a single point of failure for merges — while
    ``strict`` (report mode) fails CLOSED so ``--check-pr`` never reports a failed
    scan as clean (see ``_scan_unreadable``). An empty result (no comments / Codex
    quota) is clean either way.
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
                f"repos/{repo or ':owner/:repo'}/issues/{pr_num}/comments",
                "--jq",
                "[.[] | {login: .user.login, type: .user.type, body: .body}]",
            ],
            capture_output=True,
            text=True,
            timeout=_gh_timeout(8),  # merge-path budget (see main): advisory scan, fail-open
        )
        if result.returncode != 0:
            return _scan_unreadable(strict, "review-body comments")  # gh error
    except Exception:
        return _scan_unreadable(strict, "review-body comments")

    output = result.stdout.strip()
    if not output or output == "[]":
        return False, ""  # No comments at all — allow (quota-exhausted case)

    # Parse JSON array of comments
    try:
        raw_comments = json.loads(output)
    except json.JSONDecodeError:
        return _scan_unreadable(strict, "review-body comments")  # malformed JSON

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


# ── Codex review FRESHNESS (a CURRENT review must exist, not just no findings) ──
# The finding scanners above block on UNRESOLVED findings, but a merge can still
# proceed with NO review at all, or a review of a STALE commit (Codex reviewed A,
# code B pushed after) — the scans come back empty and the merge sails through
# unseen. This gate requires Codex's most recent review to cover the PR's current
# head, compared on the FULL 40-char oid GitHub records for the review
# (the review object's ``commit_id``) — NOT a short prefix from the body, which a
# stale prefix (or a ground SHA sharing it) could satisfy. Waived by
# '# review-override' (the conscious "merge without a current Codex review" case).
_CODEX_REVIEW_BOT = "chatgpt-codex-connector[bot]"


def _pr_head_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The PR's current head commit oid (headRefOid), or None on any error.

    Tests inject via the ``_TEST_GH_HEAD_SHA`` env var so no network is needed.
    """
    raw = os.environ.get("_TEST_GH_HEAD_SHA")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    *_repo_args(repo),
                    "--json",
                    "headRefOid",
                    "--jq",
                    ".headRefOid",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable head BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    sha = (raw or "").strip()
    return sha or None


def _codex_reviews(pr_num: str, repo: str | None = None) -> list[dict] | None:
    """EVERY Codex review record ``{commit_id, state}`` on the PR, oldest-first,
    or None on any API/parse error (distinct from ``[]`` = query succeeded, no
    Codex review). INCLUDES ``DISMISSED`` reviews — consumers filter per their
    need: freshness (``_codex_review_commit_ids``) skips dismissed (a dismissed
    review vouches for NO commit); the escalation counter COUNTS them (a
    dismissed round already RAN and consumed the review budget — #1385 round-5:
    3 dismissed rounds must still trip the 3-round cap). Uses GitHub's
    authoritative per-review ``commit_id`` (immune to prefix grinding); the
    ``/pulls/N/reviews`` endpoint returns reviews oldest-first. Tests inject via
    ``_TEST_GH_CODEX_REVIEWS`` (one JSON object per line: ``{login,
    commit_id[, state]}``; missing ``state`` = active). Fail-safe: None on error.
    """
    raw = os.environ.get("_TEST_GH_CODEX_REVIEWS")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/pulls/{pr_num}/reviews",
                    "--paginate",
                    "--jq",
                    ".[] | {login: .user.login, commit_id: .commit_id, state: .state}",
                ],
                capture_output=True,
                text=True,
                # See the merge-path timeout budget note in main(): every gh call on
                # this path must finish (or fail open/closed on its own) well inside
                # the hook's 60s wall-clock, or a SIGKILL disengages ALL gates at once.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    reviews: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if (obj.get("login") or "") != _CODEX_REVIEW_BOT:
            continue
        reviews.append(
            {
                "commit_id": (obj.get("commit_id") or "").strip().lower(),
                "state": (obj.get("state") or "").upper(),
            }
        )
    return reviews


def _codex_review_commit_ids(pr_num: str, repo: str | None = None) -> list[str] | None:
    """NON-DISMISSED Codex review commit oids, oldest-first, or None on error.

    Freshness identity — a dismissed review vouches for no commit, so the #1366
    reviewed-head gate must not treat its sha as reviewed. For ROUND COUNTING
    (dismissed rounds still ran) the escalation gate uses ``_codex_reviews``
    directly. Consumer: ``_latest_codex_reviewed_sha`` (last entry).
    """
    reviews = _codex_reviews(pr_num, repo=repo)
    if reviews is None:
        return None
    return [r["commit_id"] for r in reviews if r["state"] != "DISMISSED" and r["commit_id"]]


def _latest_codex_reviewed_sha(pr_num: str, repo: str | None = None) -> str | None:
    """The FULL commit oid of Codex's MOST RECENT review, or None if Codex has
    posted no review (or on any API/parse error). See
    ``_codex_review_commit_ids`` for the query/identity model."""
    ids = _codex_review_commit_ids(pr_num, repo=repo)
    return ids[-1] if ids else None


def _comment_target(argv: list[str]) -> tuple[str | None, str | None]:
    """(pr_number, repo) from a ``gh pr comment`` segment's argv.

    Number from a bare number, #N, or PR URL; a URL target ALSO carries its
    OWNER/REPO (counting against the hook cwd's repo for a cross-repo URL could
    produce a wrong count and a FALSE block — Codex round-1 finding). An
    explicit ``--repo``/``-R`` flag wins over the URL-derived repo. A branch
    target (non-numeric, non-URL positional) yields (None, …) → fail-open.
    """
    pr_num: str | None = None
    url_repo: str | None = None
    try:
        idx = argv.index("comment")
    except ValueError:
        return None, None
    # Value-taking flags whose SEPARATED value must not be read as the positional
    # target — a PR URL inside a `--body` text would otherwise redirect the gate
    # to an unrelated repo (#1385 round-5). Glued forms (`--body=…`, `-b…`,
    # `-R…`, `--repo=…`) are single `-`-prefixed tokens already skipped below.
    _VALUE_FLAGS = {"-b", "--body", "-F", "--body-file", "-R", "--repo"}
    skip_next = False
    for tok in argv[idx + 1 :]:
        if skip_next:
            skip_next = False
            continue
        if tok in _VALUE_FLAGS:
            skip_next = True
            continue
        if tok.startswith("-"):
            continue
        if pr_num is None and tok.isdigit():
            pr_num = tok
        elif pr_num is None and tok.startswith("#") and tok[1:].isdigit():
            pr_num = tok[1:]
        elif pr_num is None:
            url = re.match(r"(?:[a-z]+://[^/\s]+/)?([^/\s]+/[^/\s]+)/pull/(\d+)\b", tok)
            if url:
                url_repo, pr_num = url.group(1), url.group(2)
    return pr_num, _comment_repo(argv) or url_repo


def _comment_repo(argv: list[str]) -> str | None:
    """Explicit ``--repo``/``-R`` value on a gh pr comment segment, if any.

    Handles separated (``--repo o/r`` / ``-R o/r``), ``--repo=o/r``, and glued
    ``-Ro/r`` forms. A host-qualified ``HOST/OWNER/REPO`` is reduced to its
    OWNER/REPO tail (the REST path shape this gate queries).
    """
    val: str | None = None
    for i, tok in enumerate(argv):
        if tok in ("--repo", "-R") and i + 1 < len(argv):
            val = argv[i + 1]
        elif tok.startswith("--repo="):
            val = tok.split("=", 1)[1]
        elif tok.startswith("-R") and len(tok) > 2 and not tok.startswith("-R="):
            val = tok[2:]
        elif tok.startswith("-R=") and len(tok) > 3:
            val = tok[3:]
    if val and val.count("/") >= 2:
        val = "/".join(val.split("/")[-2:])
    return val


def _escalation_advisory(pr_num: str, rounds: int, repo: str | None = None) -> str:
    repo_arg = f" --repo {repo}" if repo else ""
    return (
        f"BLOCKED: this would be Codex round {rounds + 1} on PR #{pr_num} — "
        f"{rounds} rounds already ran (cap {ESCALATION_ROUND_CAP}). Repeated "
        "rounds each finding NEW defects is the whack-a-mole signature: the "
        "fixes themselves are becoming the bug source. STEP BACK before "
        "requesting another round:\n"
        "  1. TRIAGE every open finding FIRST — classify each as {live bug | "
        "latent trap | hardening | observation}. Only live bugs and "
        "cheaper-now-than-later traps may change already-reviewed code; "
        "everything else gets a documented acceptance or routes to the PR that "
        "owns that area. Findings are inputs to judgment, not a to-do list.\n"
        "  2. Fix MECHANISMS, not instances — ask 'what made this bug "
        "possible?' and remove that; patching the named instance leaves the "
        "class alive for the next round to find.\n"
        "  3. State-machine/queue/lifecycle code: enumerate EVERY status value "
        "and trace your change under each one. Your tests encode your own "
        "model of the states — they cannot catch the states you didn't "
        "consider.\n"
        "  4. Consider REVERTING a prior round's fix instead of patching it "
        "again — less code is often the real fix.\n"
        "  5. ESCALATE to the user with a minimize-change recommendation — "
        "past the cap, standing approval is consumed; each extra round needs "
        "a fresh, conscious decision.\n"
        "After doing the above (triage table produced, user consulted), "
        "re-run with a trailing shell comment (outside any quotes):\n"
        f'  gh pr comment {pr_num}{repo_arg} --body "@codex review"  # escalation-ack'
    )


def _check_codex_round_escalation(segs) -> tuple[bool, str]:
    """Block a ``gh pr comment … @codex review`` once the PR already carries
    ``ESCALATION_ROUND_CAP`` Codex reviews, until a trailing ``# escalation-ack``.

    Companion to the commit gate's Rule 3 (review_enforcement_commit.py): that
    counter tracks LOCAL review→fix rounds and stays asleep when every local
    review is clean while the loop churns through CODEX rounds on the PR — the
    exact blind spot of the 2026-08-12 MW-3 #1372 whack-a-mole (5 Codex rounds,
    local counter at 0). This gate counts the PR's actual Codex reviews from the
    GitHub API (stateless, authoritative) at the one moment the groove happens:
    requesting the next round.

    FAIL-OPEN state table (advisory logic must never break workflow — the
    opposite posture from the fail-closed merge gates, on purpose):
      segment isn't `gh pr comment`               → untouched
      comment without an '@codex review' body     → untouched (body-file/stdin
        bodies are unresolvable here — documented coverage limit, fail-open;
        blocking unresolvable bodies would false-block non-trigger comments)
      '# escalation-ack' trailing ANY segment     → allow the whole command (a
        nested `bash -c '…' # escalation-ack` carries the ack on the OUTER
        segment; the ack is a conscious human-directed act, so one ack licenses
        the command it trails)
      numberless/branch target (no PR number)     → that segment allows;
        SCANNING CONTINUES (an allowed segment must not shield a later one)
      gh/API/parse error (ids is None)            → that segment allows, scan on
      rounds < ESCALATION_ROUND_CAP               → that segment allows, scan on
      any segment at rounds >= cap, no ack        → BLOCK with the step-back
        order (URL targets carry their OWN repo into the count — counting the
        hook cwd's repo for a cross-repo URL could produce a FALSE block)
    Any unexpected exception → allow (caught here, NOT left to run_guard's
    fail-closed exit-2, which would turn an advisory bug into a hard block).
    """
    try:
        if any(has_trailing_override(s.raw, "escalation-ack") for s in segs):
            return False, ""
        # Earlier trigger segments in THIS command count toward the total: each
        # segment sees the same pre-execution API count, so `request && request`
        # at cap-1 would otherwise dispatch round N+1 unacknowledged (round-2
        # finding). Keyed per (repo, pr) so distinct PRs don't cross-count.
        in_cmd: dict[str, int] = {}
        for seg in segs:
            if gh_pr_subcommand(seg.argv) != "comment":
                continue
            if not any("@codex review" in tok.lower() for tok in seg.argv):
                continue
            pr_num, repo = _comment_target(seg.argv)
            if not pr_num:
                continue
            # Count ALL Codex review rounds — including DISMISSED, which still
            # ran and consumed the budget (#1385 round-5). Freshness uses the
            # dismissed-filtered ``_codex_review_commit_ids``; the cap does not.
            reviews = _codex_reviews(pr_num, repo=repo)
            if reviews is None:
                continue
            key = f"{repo or ''}|{pr_num}"
            effective = len(reviews) + in_cmd.get(key, 0)
            if effective >= ESCALATION_ROUND_CAP:
                return True, _escalation_advisory(pr_num, effective, repo)
            in_cmd[key] = in_cmd.get(key, 0) + 1
    except Exception:
        return False, ""
    return False, ""


def _classify_post_review_delta(reviewed_sha: str, head_sha: str, repo: str | None) -> str | None:
    """Substantiality of what HEAD adds OVER the Codex-reviewed SHA, via the compare API.

    Returns ``"substantial"`` / ``"inline"`` (a real classification of the unreviewed
    delta) or ``None`` when it cannot be classified (API/parse error). The reviewed SHA
    is often NOT in the local object store (a past PR head), so the delta is fetched
    remotely rather than via local ``git diff``. The CALLER decides the fail direction:
    inside the fail-closed freshness gate, only a definitive ``"inline"`` narrows the
    block — ``None`` blocks like ``"substantial"`` (stale is the default-block state;
    triviality is an exception granted only on positive evidence).

    ``status`` semantics (top-level compare status): ``identical`` (trees match) →
    ``"inline"``. ``behind`` is NOT inline — an ancestor head can still carry code the
    reviewed commit removed → it fails closed (None). ``ahead``
    (the normal append case) or ``diverged`` (a rebase/force-push rewrite) → classify
    the actual changed ``files`` — a diverged rewrite must NOT be assumed trivial.
    Compare TRUNCATION guard: GitHub caps ``files`` at 300 per comparison; at the cap a
    substantial code file may sit beyond it → ``"substantial"`` (conservative).

    Tests inject via ``_TEST_GH_COMPARE`` (the JSON object this would fetch).
    """
    raw = os.environ.get("_TEST_GH_COMPARE")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repo or ':owner/:repo'}/compare/{reviewed_sha}...{head_sha}",
                    "--jq",
                    "{status: .status, files: [.files[]? | {filename, additions, deletions, "
                    'status, previous_filename, has_patch: has("patch")}]}',
                ],
                capture_output=True,
                text=True,
                # Merge-path timeout budget (see main()): one compare round-trip is
                # fast; 8s keeps the worst case inside the hook's 60s wall-clock.
                timeout=_gh_timeout(8),
            )
            if result.returncode != 0:
                return None
            raw = result.stdout
        except Exception:
            return None
    try:
        data = json.loads(raw or "{}")
    except Exception:
        return None
    if not isinstance(data, dict):  # valid JSON but not an object → unclassifiable
        return None
    status = data.get("status")
    if status == "identical":
        return "inline"  # trees match exactly → nothing unreviewed
    if status not in ("ahead", "diverged"):
        # NOTE `behind` is NOT treated as inline (Codex P1 #1373): head being an
        # ANCESTOR of the reviewed commit does not make head's TREE a content subset
        # — if the reviewed commit deleted/replaced code and the PR is then reset to
        # its parent, head carries code Codex never approved (it reviewed the removal).
        # `behind` falls here → None → fail closed (re-review required).
        # A MISSING/unknown status (`{status: null, …}` from a truncated or
        # unexpected compare response) must fail CLOSED — NOT fall through to file
        # classification, where an empty `files` would read as "inline" and permit
        # a STALE review to bind the merge on a delta that was never verified
        # (Codex P2, #1373). Only the documented linear statuses are classifiable.
        return None
    files = data.get("files")
    if not isinstance(files, list):
        return None
    if len(files) >= 300:
        return "substantial"  # compare file cap hit → can't rule out substantial code
    try:
        # review_scope lives in scripts/ (parent of scripts/hooks/). Lazy import ON
        # PURPOSE: an import failure must degrade THIS classification to None (the
        # caller then blocks — the gate's fail direction), never crash the guard's
        # module load and drop every push/merge protection. De-duped sys.path insert.
        _scripts_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from review_scope import classify_compare_substantiality

        return classify_compare_substantiality(files)
    except Exception:
        return None


def _check_codex_reviewed_head(
    pr_num: str, *, force: bool = False, repo: str | None = None
) -> tuple[bool, str, str | None]:
    """Block a merge unless Codex has reviewed the PR's CURRENT head commit —
    or the delta since its last review is provably TRIVIAL.

    Compares the PR's ``headRefOid`` against the FULL ``commit_id`` of Codex's
    latest non-dismissed review — an EXACT identity, no prefix match. Fail-CLOSED:
    if the head cannot be read, or Codex has no review, the merge is BLOCKED. A
    STALE review (Codex reviewed an older commit) blocks UNLESS the unreviewed
    delta (``reviewed...head`` via the compare API) classifies as review-trivial
    (``review_scope`` substantiality: docs-only / a small single-file touch-up) —
    the smart-delta narrowing that keeps the gate's teeth pointed at UNREVIEWED
    SUBSTANTIAL CODE instead of taxing every post-review typo fix (measured
    2026-08-11: a binary stale-blocks gate would have blocked 14/18 recent
    merges). An UNCLASSIFIABLE delta blocks — triviality is an exception granted
    only on positive evidence. ``force`` (a ``# stale-review-override`` on the
    merge segment — deliberately NOT ``# review-override``, which waives the P1
    finding scans; the two boundaries are independent) is the conscious escape
    (e.g. a genuine Codex outage).

    Returns ``(should_block, message, verified_head)`` — ``verified_head`` is the
    full head oid this check verified/classified against (when not blocked and
    not forced — including the trivial-delta allow, so the merge is still bound
    to the exact head that was assessed); the caller binds the MERGE to it via
    ``--match-head-commit`` so a push landing between this check and the merge
    cannot smuggle an unreviewed head through (TOCTOU — Codex P1, PR #1366).
    """
    if force:
        return False, "", None
    head = _pr_head_sha(pr_num, repo=repo)
    if not head:
        return (
            True,
            (
                f"could not read PR #{pr_num}'s head commit to verify a current Codex "
                f"review (GitHub query failed).\n"
                f"Retry, or append '# stale-review-override' to merge anyway."
            ),
            None,
        )
    head = head.strip().lower()
    reviewed = _latest_codex_reviewed_sha(pr_num, repo=repo)
    if not reviewed:
        return (
            True,
            (
                f"no Codex review found for PR #{pr_num} at head {head[:12]}.\n"
                f"Codex reviews on PR-open — it does NOT auto-review a later fix-commit; "
                f"comment '@codex review' on the PR to review the current head (then wait), "
                f"or append '# stale-review-override' to merge without a current Codex "
                f"review (e.g. Codex is down)."
            ),
            None,
        )
    if reviewed != head:
        level = _classify_post_review_delta(reviewed, head, repo)
        if level == "inline":
            # The unreviewed delta is provably review-trivial — allow, but still
            # bind the merge to THIS head (TOCTOU): the triviality claim is about
            # exactly this reviewed...head range, not any later push.
            print(
                f"NOTE: Codex's review on PR #{pr_num} is on {reviewed[:12]} (head "
                f"{head[:12]}), but the delta since is review-trivial — allowing. "
                f"Inspect: git log {reviewed[:12]}..{head[:12]} --oneline",
                file=sys.stderr,
            )
            return False, "", head
        delta_note = (
            "the unreviewed delta is SUBSTANTIAL"
            if level == "substantial"
            else "the unreviewed delta could not be classified (treated as substantial)"
        )
        return (
            True,
            (
                f"Codex's latest review is STALE: it reviewed {reviewed[:12]}, but PR "
                f"#{pr_num} head is {head[:12]}, and {delta_note} — Codex never saw "
                f"this code.\n"
                f"Comment '@codex review' on the PR to re-review the current head (Codex "
                f"does NOT auto-review fix-commits), then wait; or append "
                f"'# stale-review-override' to merge anyway.\n"
                f"  (inspect the unreviewed commits: git log {reviewed[:12]}..{head[:12]} "
                f"--oneline)"
            ),
            None,
        )
    return False, "", head


def _pr_base_ref(pr_num: str, repo: str | None = None) -> str | None:
    """The branch this PR MERGES INTO (``baseRefName``), or None on any error.

    Tests inject via ``_TEST_GH_BASE_REF`` (its own seam, mirroring
    ``_pr_head_sha``'s ``_TEST_GH_HEAD_SHA`` — one gh interaction per seam keeps
    each independently fakeable; the extra ``gh pr view`` is cheap on a rare merge).
    """
    raw = os.environ.get("_TEST_GH_BASE_REF")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_num,
                    *_repo_args(repo),
                    "--json",
                    "baseRefName",
                    "--jq",
                    ".baseRefName",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable base BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    ref = (raw or "").strip()
    return ref or None


def _repo_default_branch(repo: str | None = None) -> str | None:
    """The repo's default branch name (``defaultBranchRef.name``), or None on error.

    ``gh repo view`` takes the repo as a POSITIONAL (``gh repo view OWNER/REPO``),
    not ``--repo`` — verified: ``gh pr view`` has no ``defaultBranchRef`` field, so
    this cannot be folded into the base query. No positional ⇒ gh's cwd repo.
    Tests inject via ``_TEST_GH_DEFAULT_BRANCH``.
    """
    raw = os.environ.get("_TEST_GH_DEFAULT_BRANCH")
    if raw is None:
        try:
            result = subprocess.run(
                [
                    "gh",
                    "repo",
                    "view",
                    *([repo] if repo else []),
                    "--json",
                    "defaultBranchRef",
                    "--jq",
                    ".defaultBranchRef.name",
                ],
                capture_output=True,
                text=True,
                timeout=_gh_timeout(
                    6
                ),  # merge-path budget (see main): fail-closed → unreadable default BLOCKS
            )
            raw = result.stdout if result.returncode == 0 else ""
        except Exception:
            return None
    name = (raw or "").strip()
    return name or None


def _check_base_is_default(
    pr_num: str, *, force: bool = False, repo: str | None = None
) -> tuple[bool, str]:
    """Block unless the PR's base branch == the repo's default branch.

    A PR retargeted AFTER Codex reviewed it still passes the head-freshness gate —
    the head oid never moved — even though the base change can substantially alter
    the effective diff (GitHub's review object records no base, so freshness alone
    cannot see it). This repo's PRs always target the default branch, so a
    non-default base is anomalous → block. Fail-CLOSED: an unreadable base OR
    default is treated as unverifiable → block (matching the rest of this gate).
    ``force`` (a ``# stale-review-override`` on the merge segment — the
    review-CONTEXT sigil, shared with the freshness gate and deliberately NOT
    ``# review-override``, which waives the P1 finding scans) is the conscious
    escape for a deliberate stacked/non-default PR. Declared threat model: this
    guards an ACCIDENTAL retarget on a single-author repo, not an adversary.
    """
    if force:
        return False, ""
    base = _pr_base_ref(pr_num, repo=repo)
    default = _repo_default_branch(repo=repo)
    if not base or not default:
        return (
            True,
            (
                f"could not confirm PR #{pr_num}'s base branch against the repo default "
                f"(base={base or '?'}, default={default or '?'}) — retry, or append "
                f"'# stale-review-override' to merge anyway."
            ),
        )
    if base != default:
        return (
            True,
            (
                f"PR #{pr_num} targets base '{base}', not the default branch '{default}'. "
                f"A retargeted PR's Codex review may not reflect the new diff — re-run "
                f"'@codex review' on the PR, or append '# stale-review-override' for a "
                f"deliberate stacked/non-default PR."
            ),
        )
    return False, ""


def _suggested_merge_cmd(pr_num: str, verified_head: str, repo: str | None) -> str:
    """The exact atomic merge command to copy — PRESERVING an explicit --repo.

    Dropping the target repo from a generated command retargets the merge to the
    cwd repo, which can merge an unrelated same-numbered PR (Codex P1, round 3).
    ``repo`` is the already-normalized OWNER/REPO the gates ran against, or None
    (cwd repo → no --repo).
    """
    repo_part = f"--repo {repo} " if repo else ""
    return f"gh pr merge {pr_num} {repo_part}--squash --admin --match-head-commit {verified_head}"


# gh pr merge flags that CONSUME the FOLLOWING token as their value (separate
# form). When scanning for --match-head-commit these values MUST be skipped, else
# a `--body --match-head-commit=<sha>` — where the sha is --body's VALUE, taken as
# body TEXT by gh with NO head binding — is misread as an active binding while gh
# merges unbound (Codex P1, round 3). --repo is gate-relevant (handled elsewhere)
# but still value-consuming here; --match-head-commit itself consumes its own SHA.
# NOTE: the risk is the PARSE MODEL (pflag argument consumption + short-flag
# clustering), not the flag names — the long value-flag set is identical across
# gh 2.45–2.96 `gh pr merge --help`.
_GH_MERGE_VALUE_FLAGS = frozenset(
    {
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--subject",
        "-t",
        "--author-email",
        "-A",
        "--repo",
        "-R",
        "--match-head-commit",
    }
)
# Content value-flags (LONG forms) that can SHADOW --match-head-commit as their
# value and have NO legitimate use on a gated --admin squash-merge. Their mere
# presence is refused (fail-closed belt over the value-skipping parse). Short
# forms are handled letter-wise in the cluster helpers below.
_GH_MERGE_SHADOW_FLAGS = frozenset({"--body", "--body-file", "--subject", "--author-email"})
# Short flags on gh pr merge. VALUE letters consume a value (glued remainder, else
# the NEXT token); the rest are booleans (-d/-m/-r/-s). A cluster like `-db` is
# -d(bool) + -b(value), so gh's -b swallows the FOLLOWING token — a scan treating
# `-db` as opaque misses that consumption (audit round 4: `-db --match-head-commit=X`
# merges UNBOUND). SHADOW shorts are the content ones (not -R).
_GH_MERGE_VALUE_SHORTS = "bFtAR"
_GH_MERGE_SHADOW_SHORTS = "bFtA"


def _is_short_cluster(tok: str) -> bool:
    return tok.startswith("-") and not tok.startswith("--") and len(tok) > 1


def _short_cluster_consumes_next(tok: str) -> bool:
    """Whether a ``-<letters>`` short cluster makes gh consume the NEXT argv token
    as a value — a value-short letter with NO glued remainder after it. Boolean
    letters before it are consumed in place. Mirrors pflag."""
    if not _is_short_cluster(tok):
        return False
    letters = tok[1:]
    for j, ch in enumerate(letters):
        if ch in _GH_MERGE_VALUE_SHORTS:
            return letters[j + 1 :] == ""  # glued remainder ⇒ value in-token, not next
    return False


def _merge_match_head(argv: list[str]) -> str | None:
    """The value gh will use for ``--match-head-commit`` on a merge argv, or None.

    Parses with gh-equivalent argument consumption: value-taking flags
    (``_GH_MERGE_VALUE_FLAGS``) consume the next token, so a --match-head-commit
    token that is actually ANOTHER flag's value is not misread as a binding. A
    bare ``--`` ends option parsing (everything after is positional; gh rejects
    extra positionals). Returns the LAST occurrence — gh pflag last-value-wins
    (same rule as ``_pr_create_head_raw``); first-wins would let a trailing
    ``--match-head-commit <other>`` be enforced by gh while the hook validated an
    earlier value.
    """
    argv = argv or []
    result: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            break  # end of options — the rest are positionals gh would reject
        if tok in _GH_MERGE_VALUE_FLAGS:
            # Separate-form value flag consumes the NEXT token. When the flag is
            # --match-head-commit itself, that next token is the value we want.
            if tok == "--match-head-commit" and i + 1 < len(argv):
                result = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--match-head-commit="):
            result = tok.split("=", 1)[1]
            i += 1
            continue
        if _short_cluster_consumes_next(tok):
            i += 2  # cluster's trailing value-short swallows the next token
            continue
        i += 1
    return result


def _merge_has_shadow_flag(argv: list[str]) -> bool:
    """Whether the merge carries a content flag that could shadow the head-match
    binding — long form, ``=`` form, bare short, or inside a short cluster
    (``-db`` etc.). Refused outright as a fail-closed belt."""
    for tok in argv or []:
        if tok == "--":
            break
        base = tok.split("=", 1)[0]
        if base in _GH_MERGE_SHADOW_FLAGS:
            return True
        if _is_short_cluster(base):
            for ch in base[1:]:
                if ch in _GH_MERGE_SHADOW_SHORTS:
                    return True
                if ch in _GH_MERGE_VALUE_SHORTS:
                    break  # a value-short (e.g. -R): the rest is its glued value
    return False


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

        # ── Codex round-escalation gate (`gh pr comment … @codex review`) ──
        # Once a PR already carries ESCALATION_ROUND_CAP Codex reviews,
        # requesting another round is the whack-a-mole moment — force the
        # step-back (triage / mechanism / state-space) before round N+1.
        # Fail-open inside the check; '# escalation-ack' is the conscious
        # continue after a fresh user decision.
        esc_block, esc_msg = _check_codex_round_escalation(segs)
        if esc_block:
            print(esc_msg, file=sys.stderr)
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
            # An explicit --repo/-R (or PR URL) retargets EVERY gate below —
            # without it, merging a cross-repo PR checked the CWD repo's
            # same-numbered PR (wrong-repo gate; 2026-07-26 incident).
            # Pass the merge SEGMENT's raw text (not the whole compound command)
            # so an unrelated segment's PR URL cannot select the gated repo
            # (`echo …/other/repo/pull/9 && gh pr merge 12` must gate the cwd repo).
            #
            # Arm the SHARED merge-path deadline BEFORE the first gh call: every gate's
            # gh subprocess (repo/PR resolution → mergeable → CI → base → freshness →
            # findings) reads it via _gh_timeout so the AGGREGATE finishes under the
            # hook's ~60s wall-clock, instead of summing per-call caps past it and
            # getting SIGKILLed mid-gate (Codex P1 #1373). Budget < 60s with headroom.
            global _merge_deadline
            _merge_deadline = time.monotonic() + _MERGE_GATE_BUDGET_S
            merge_repo = _merge_target_repo(merge_seg.argv, merge_seg.raw)
            # For a DERIVED (no --repo) merge, the dir gh resolves the repo AND a
            # numberless branch-PR from — threaded to _resolve_pr_number so a
            # numberless `gh pr merge` still resolves (an explicit --repo would
            # error there). None ⇒ an explicit --repo or the legacy no-cwd path.
            merge_cwd: str | None = None
            if merge_repo is None:
                # No explicit --repo/-R: gh resolves the repo from the merge's
                # EFFECTIVE cwd (payload cwd + any preceding `cd`), which can
                # differ from the HOOK process's cwd — so the gates' gh queries
                # (run in the hook's cwd) could check a DIFFERENT repo than the
                # one gh merges in (a bare merge from another checkout, or after
                # a `cd`). Derive the repo gh will actually target and gate THAT.
                # Fail CLOSED only when the cwd is ambiguous (_CWD_UNKNOWN) or gh
                # can't resolve a repo there; no cwd info at all (None) keeps
                # today's cwd-based behavior — there is nothing to derive from.
                eff_cwd = _effective_cwd(cmd, payload, seg=merge_seg)
                if eff_cwd is _CWD_UNKNOWN:
                    merge_repo = _REPO_UNRESOLVED
                elif eff_cwd is not None:
                    merge_repo = _derive_repo_from_cwd(eff_cwd) or _REPO_UNRESOLVED
                    merge_cwd = eff_cwd  # resolve a numberless PR in the same dir
            if merge_repo is _REPO_UNRESOLVED:
                print(
                    "BLOCKED: cannot determine which repository this merge targets "
                    "(an unresolvable --repo/-R value, an enterprise host, an odd "
                    "URL, or an ambiguous working directory).",
                    file=sys.stderr,
                )
                print(
                    "Append --repo OWNER/REPO so the CI/review gates check the "
                    "right repository, not the current directory's.",
                    file=sys.stderr,
                )
                return 2
            pr_num = _resolve_pr_number(cmd, repo=merge_repo, cwd=merge_cwd)
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
                # ── Merge-path gh TIMEOUT BUDGET ──────────────────────────
                # This hook runs under a 60s CC wall-clock (settings.json). A
                # wall-clock overrun SIGKILLs the hook MID-GATE, which "fails
                # toward tool-runs" — i.e. it silently disengages EVERY merge
                # gate at once, precisely when the GitHub API is degraded. So
                # each sequential gh call on this path — INCLUDING the
                # pre-gate repo/PR resolution above (6s each) — carries a tight
                # per-call timeout (6-8s): pre-gates + fail-closed gates
                # (derive 6 + resolve 6 + mergeable 8 + ci 8 + base 6+6 +
                # freshness 6+8 + delta 8 = 62s absolute worst) each reach
                # their own block/allow decision at or inside the budget, and
                # any ONE of them timing out fail-closes IMMEDIATELY (the
                # additive worst case needs every call slow-but-successful);
                # the TOCTOU binding runs argv-only right after freshness, so
                # only the tail advisory scanners (fail-OPEN by design) could
                # ever be clipped, which nets the same outcome as their error
                # path.
                # A timing-out fail-closed gate returns BLOCK immediately, so
                # the additive worst case needs every call slow-but-successful.
                mergeable = _check_mergeable(pr_num, repo=merge_repo)
                if mergeable == "CONFLICTING":
                    print(
                        f"BLOCKED: PR #{pr_num} has merge conflicts. Resolve before merging.",
                        file=sys.stderr,
                    )
                    return 2
                if mergeable != "MERGEABLE":
                    # Allowlist (fail-CLOSED): anything that is not a definite
                    # MERGEABLE is unverifiable — UNKNOWN (GitHub still computing
                    # conflicts), None/"" (the query FAILED; _check_mergeable
                    # fails OPEN to None), or an unrecognized future state. The
                    # old `== "UNKNOWN"` check let None/"" sail through and merge.
                    # Retry is the remedy (a transient gh hiccup resolves).
                    print(
                        f"BLOCKED: PR #{pr_num} mergeable status is "
                        f"'{mergeable or 'unreadable'}', not MERGEABLE.",
                        file=sys.stderr,
                    )
                    print(
                        "GitHub may still be computing it, or the query failed. Wait and retry.",
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
                ci_state, ci_bad = _pr_ci_status(pr_num, repo=merge_repo)
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

                force_override = merge_seg.override
                # The review-CONTEXT waiver (freshness + base gates) is a SEPARATE
                # sigil from # review-override: the freshness gate is the
                # high-traffic one (Codex never auto-re-reviews a push), and if its
                # escape also waived the P1 finding scans, the path of least
                # resistance would systematically disarm P1 enforcement (Codex P1 +
                # architect SHOULD-FIX on #1366). One trailing comment may carry
                # both sigils when both waivers are genuinely intended.
                stale_override = has_trailing_override(merge_seg.raw, "stale-review-override")

                # Base-branch invariant: a PR retargeted AFTER Codex reviewed it
                # keeps the SAME head oid, so head-freshness alone can't see the
                # base change that may have altered the effective diff. Require
                # base == the repo's default branch. Waived by
                # # stale-review-override for a deliberate stacked/non-default PR.
                should_block, base_msg = _check_base_is_default(
                    pr_num,
                    force=stale_override,
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — base branch is not the repo default.",
                        file=sys.stderr,
                    )
                    print(base_msg, file=sys.stderr)
                    return 2

                # Codex must have reviewed the CURRENT head (existence + freshness)
                # — not merely have no open findings. This runs BEFORE the finding
                # scans below: a review published between the scans and this check
                # would otherwise pass freshness while its own P1 comments went
                # unscanned (the scans came back empty). Freshness first, then
                # findings. A not-yet-reviewed head blocks; a stale-reviewed head
                # blocks unless the unreviewed delta is provably review-trivial
                # (smart-delta — see _check_codex_reviewed_head). Waived by
                # # stale-review-override (NOT # review-override).
                should_block, fresh_msg, verified_head = _check_codex_reviewed_head(
                    pr_num,
                    force=stale_override,
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} — Codex has not reviewed the current head.",
                        file=sys.stderr,
                    )
                    print(fresh_msg, file=sys.stderr)
                    return 2

                # Bind the MERGE to the verified head (TOCTOU — Codex P1): a push
                # landing between the check above and the merge would otherwise
                # merge an UNREVIEWED head under a stale verification. GitHub
                # enforces `--match-head-commit` server-side (the merge is
                # rejected if the head moved), making check→merge atomic. Only
                # engaged when the freshness check ran (not # stale-review-override).
                # Placed IMMEDIATELY after the freshness gate — BEFORE the two
                # advisory network scanners below — so a hook wall-clock SIGKILL
                # during a slow scan can never skip the binding enforcement (it
                # needs only argv + verified_head, no gh call): a clipped scanner
                # nets its own fail-open outcome, but a clipped BINDING would have
                # re-opened the unbound-merge race this block exists to close.
                if verified_head:
                    # Fail-closed belt: content value-flags can smuggle a
                    # --match-head-commit token as their VALUE (gh takes it as
                    # text → no binding) and have no use on a gated squash-merge.
                    if _merge_has_shadow_flag(merge_seg.argv):
                        print(
                            "BLOCKED: --body/--subject/--body-file/--author-email are "
                            "not allowed on a gated merge — they can shadow the "
                            "--match-head-commit binding. Remove them (set a squash "
                            "message via the GitHub UI if needed).",
                            file=sys.stderr,
                        )
                        return 2
                    match_head = _merge_match_head(merge_seg.argv)
                    if match_head is None:
                        print(
                            "BLOCKED: merge must be bound to the Codex-verified head "
                            "commit so a race with a new push cannot merge an "
                            "unreviewed head. Re-run with:",
                            file=sys.stderr,
                        )
                        print(
                            "  " + _suggested_merge_cmd(pr_num, verified_head, merge_repo),
                            file=sys.stderr,
                        )
                        return 2
                    if match_head.strip().lower() != verified_head:
                        print(
                            f"BLOCKED: --match-head-commit {match_head[:12]} does not "
                            f"equal the Codex-verified head {verified_head[:12]} — the "
                            f"branch moved (or the sha is stale). Re-verify and use "
                            f"the current verified head.",
                            file=sys.stderr,
                        )
                        return 2

                # Unresolved review findings (review body) — AFTER freshness +
                # binding (see the ordering note above). Waived by
                # # review-override (the FINDINGS sigil — not the stale one).
                should_block, review_msg = _check_pr_review_findings(
                    pr_num,
                    force=force_override,
                    repo=merge_repo,
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
                    repo=merge_repo,
                )
                if should_block:
                    print(
                        f"BLOCKED: PR #{pr_num} has unresolved INLINE review findings.",
                        file=sys.stderr,
                    )
                    print(inline_msg, file=sys.stderr)
                    return 2

        # ── sqlite3 write operations ────────────────────────────────
        # Whole-command match (never misses a fragmented/wrapped invocation),
        # narrowed to DML in statement position so the `replace()` scalar
        # function and bare keywords in a grep pattern no longer false-positive.
        # See _is_sqlite_write / _DML_STATEMENT_RE.
        if _is_sqlite_write(cmd):
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


# Sentinel: `--check-pr` was given a repo option with an EXPLICITLY EMPTY value
# (`--repo`, `--repo=`, `-R`, `-R=`). Distinct from None ("no repo option → cwd
# repo"): an empty value is a MISTAKE, and silently falling back to the cwd repo
# would report an all-clear (and a suggested merge command) for an unrelated
# same-numbered PR (Codex P2, #1373). The caller rejects it instead.
_CHECK_PR_REPO_EMPTY = object()


def _parse_check_pr_repo(argv: list[str]):
    """The target repo named on a ``--check-pr`` invocation's trailing args.

    Returns the OWNER/REPO string, ``None`` (no repo option → cwd repo), or
    ``_CHECK_PR_REPO_EMPTY`` (an option was given with an empty value → reject).
    Accepts every normal gh spelling — ``--repo X``, ``-R X``, ``--repo=X``,
    ``-R=X``, glued ``-Rowner/repo`` — not just the separate ``--repo`` form: a
    form that went unrecognized silently checked the CWD repo (Codex P2, #1366).
    """
    for i, tok in enumerate(argv):
        if tok in ("--repo", "-R"):
            val = argv[i + 1] if i + 1 < len(argv) else ""
            return val if val else _CHECK_PR_REPO_EMPTY
        if tok.startswith(("--repo=", "-R=")):
            return tok.split("=", 1)[1] or _CHECK_PR_REPO_EMPTY
        if tok.startswith("-R") and not tok.startswith("--") and len(tok) > 2:
            # glued pflag shorthand `-Rowner/repo` — enforcement's
            # _merge_target_repo accepts it, so the report must too.
            return tok[2:]
    return None


def check_pr_report(pr_num: str, repo: str | None = None) -> int:
    """CANONICAL pre-merge report: run the SAME checks the merge gate enforces.

    This exists so sessions never hand-roll the review check with ad-hoc
    gh/jq (2026-08-10: a hand-rolled query used the GraphQL bot login on the
    REST endpoint — `chatgpt-codex-connector` vs `…[bot]` — matched nothing,
    and the empty result was reported as "Codex clean" while 5 P2 findings sat
    unread). Report and enforcement share these functions, so a bug in one is a
    bug in both. The ONE deliberate divergence: the report runs the finding scans
    in ``strict`` mode, so an UNREADABLE scan (gh error) shows as a failure here
    rather than fail-open as enforcement does — the report must never issue a
    false all-clear.

    Prints one line per gate; returns 0 when every gate would pass, 1 otherwise.
    """
    failures = 0
    mergeable = _check_mergeable(pr_num, repo=repo)
    print(f"mergeable      : {mergeable or 'unreadable'}")
    # Allowlist, matching the enforcement arm: anything that is not a definite
    # MERGEABLE (UNKNOWN, None/"" query failure, or a new state) counts as a
    # failure — the old `in (UNKNOWN, CONFLICTING)` set let None/"" pass.
    if mergeable != "MERGEABLE":
        failures += 1
    ci_state, ci_bad = _pr_ci_status(pr_num, repo=repo)
    print(f"ci             : {ci_state}{' (' + ', '.join(ci_bad[:6]) + ')' if ci_bad else ''}")
    if ci_state in ("red", "pending"):
        failures += 1
    # Order mirrors the gate: base-invariant → freshness → finding scans, so a
    # review published mid-run can't pass freshness with its P1s unscanned.
    blocked, msg = _check_base_is_default(pr_num, repo=repo)
    print(f"base-branch    : {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok (default)'}")
    failures += 1 if blocked else 0
    blocked, msg, verified_head = _check_codex_reviewed_head(pr_num, repo=repo)
    if blocked:
        label = "BLOCK — " + msg.splitlines()[0]
    else:
        # Distinguish a genuinely-current review from a stale-but-trivial-delta
        # allow — both return the same tuple, but the report must NOT assert
        # "current" when Codex reviewed an older SHA (Codex P2, #1373). Re-derive
        # the reviewed SHA vs HEAD for an honest label (structured-stdout consumers
        # read this, not the stderr NOTE).
        _reviewed = _latest_codex_reviewed_sha(pr_num, repo=repo)
        _head = _pr_head_sha(pr_num, repo=repo)
        if _reviewed is None or _head is None:
            # A transiently-failed re-read must NOT read as "current" (Codex P2
            # #1373): the enforcement gate already passed, but the report must not
            # ASSERT the head was reviewed when it could not confirm it.
            label = "ok (freshness label unverified — re-read failed)"
        elif _reviewed != _head.strip().lower():
            label = f"ok (STALE review of {_reviewed[:12]}, delta since is trivial)"
        else:
            label = "ok (current)"
    print(f"codex-at-head  : {label}")
    if not blocked and verified_head:
        print("merge-with     : " + _suggested_merge_cmd(pr_num, verified_head, repo))
    failures += 1 if blocked else 0
    # strict=True: a scan that could not be READ (gh error/malformed) must show as
    # a failure here, never as "ok" — the report must not issue a false all-clear.
    blocked, msg = _check_pr_review_findings(pr_num, repo=repo, strict=True)
    print(f"review-body    : {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok'}")
    failures += 1 if blocked else 0
    blocked, msg = _check_inline_review_findings(pr_num, repo=repo, strict=True)
    print(
        f"inline-findings: {'BLOCK — ' + msg.splitlines()[0] if blocked else 'ok (P2s, if any, printed above)'}"
    )
    failures += 1 if blocked else 0
    print(
        "verdict        :",
        "MERGEABLE (all gates pass)" if failures == 0 else f"{failures} gate(s) would block",
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    # Report mode: `git_push_guard.py --check-pr <N> [--repo OWNER/REPO]` — the
    # canonical pre-merge check (same functions as enforcement). No hook payload.
    if len(sys.argv) >= 3 and sys.argv[1] == "--check-pr":
        _repo = _parse_check_pr_repo(sys.argv[3:])
        if _repo is _CHECK_PR_REPO_EMPTY:
            print(
                "ERROR: --repo/-R was given an empty value. Specify OWNER/REPO, or "
                "omit the option to check the current repository — an empty value "
                "would silently report the WRONG repo.",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.exit(check_pr_report(sys.argv[2], repo=_repo))
    run_guard(main, "git_push_guard")
