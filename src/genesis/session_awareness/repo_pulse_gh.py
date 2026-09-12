"""Merged-PR enumeration for the repo-pulse worker (gh CLI, injectable).

Clone of the ``pr_review_harvest`` gh pattern with two pulse-specific
hardenings, both live-verified during PR-4 due diligence:

1. **The repo slug is resolved LIVE, never from config.** A configured slug
   can name a real-but-wrong repo and return PLAUSIBLE STALE data (the
   working-repo config entry answered with April's PRs — zero error, zero
   matches, permanently silent). ``gh repo view`` resolves from the git
   remote of the cwd, so the default runner pins ``cwd=repo_root()``.
2. **A capped window is loud, never silent.** GitHub search cannot sort by
   mergedAt ascending, so when ``len(prs) == limit`` the enumeration MAY
   have dropped older PRs inside the window — the result carries
   ``limit_hit=True`` and the worker records it on the run row. PR velocity
   here is ~100/week; the default limit (200) covers ~2 weeks of catch-up.

Nothing here writes anywhere; errors return ``{"error": ...}`` without
raising (the worker records them as failed runs and leaves the cursor).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from genesis.env import repo_root

Runner = Callable[[list[str]], Awaitable[tuple[int, str, str]]]

logger = logging.getLogger(__name__)

# 30s per gh call: one GitHub API round-trip (network-bound, normally <2s).
# A hung call would sit on pulse.lock and starve every later session
# boundary's pulse until process death — the exact raw-subprocess-with-no-
# external-watchdog case the timeout policy carves out.
_GH_TIMEOUT_S = 30

# baseRefName + closingIssuesReferences added for the issue-close lane (WS-A):
# a contributor PR's `Closes #N` populates closingIssuesReferences (each ref
# carries {number, repository:{name, owner:{login}}, url}); baseRefName gates the
# default-branch-only rule (GitHub auto-closes the issue only on default merge).
# Additive — list_merged_prs only validates number/mergedAt; the fuzzy prompt
# reads only title/body, so the extra fields never enter a model prompt.
PR_FIELDS = "number,title,body,mergedAt,baseRefName,closingIssuesReferences"


async def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run a gh CLI command from the repo root, returning (rc, stdout, stderr).

    cwd is pinned to the repo so ``gh repo view`` resolves the slug from
    THIS repo's git remote regardless of where the detached worker was
    spawned from (hardening 1 above).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_root()),
        )
    except Exception as exc:
        # gh missing from PATH / non-executable / bad cwd: a raised
        # FileNotFoundError here would bypass the {"error": ...} path and
        # escape to the worker's outer catch — no run row, no telemetry,
        # no debounce. Convert to a nonzero runner result instead so the
        # normal failed-run recording path handles it.
        return 127, "", f"gh spawn failed: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh call timed out after {_GH_TIMEOUT_S}s"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def resolve_repo(runner: Runner | None = None) -> str | None:
    """Live ``owner/name`` slug of the repo the worker runs against."""
    run = runner or _default_runner
    rc, out, err = await run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    )
    slug = out.strip()
    if rc != 0 or not slug:
        logger.warning("repo_pulse slug resolve failed (rc=%s): %s", rc, err.strip())
        return None
    return slug


async def resolve_default_branch(runner: Runner | None = None) -> str | None:
    """Live default-branch name of the repo the worker runs against.

    The issue-close lane (WS-A) honors a contributor PR's ``Closes #N`` only when
    the PR merged to the default branch (GitHub auto-closes the linked issue only
    then). Resolved LIVE from the repo, like ``resolve_repo`` — never hardcoded
    'main' (other installs may differ). None on failure → the worker skips the
    lane rather than risk a false absorb against the wrong branch."""
    run = runner or _default_runner
    rc, out, err = await run(
        ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    )
    name = out.strip()
    if rc != 0 or not name:
        logger.warning("repo_pulse default-branch resolve failed (rc=%s): %s", rc, err.strip())
        return None
    return name


async def list_merged_prs(
    *,
    since_date: str,
    until_date: str | None = None,
    limit: int = 200,
    repo: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Enumerate PRs merged on/after ``since_date`` (YYYY-MM-DD, date-granular).

    ``until_date`` closes the window (``merged:since..until``) — the worker's
    pagination bound when a capped result forces paging down (hardening 2).
    Returns ``{"repo", "prs", "limit_hit"}`` with prs sorted by mergedAt
    ASCENDING (cursor math processes oldest-first), or ``{"error": ...}``.
    Rows missing an int ``number`` or a ``mergedAt`` are dropped — gh's
    contract violation, not a crash. The date-granular search re-covers up
    to a day behind the cursor by design; the caller filters client-side
    against its exact ISO watermark.
    """
    run = runner or _default_runner
    if repo is None:
        repo = await resolve_repo(run)
        if repo is None:
            return {"error": "repo slug resolve failed"}
    qualifier = f"merged:{since_date}..{until_date}" if until_date else f"merged:>={since_date}"
    rc, out, err = await run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--search",
            qualifier,
            "--json",
            PR_FIELDS,
            "--limit",
            str(limit),
        ]
    )
    if rc != 0:
        return {"error": f"pr list failed (rc={rc}): {err.strip()[:400]}"}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"pr list returned invalid JSON: {exc}"}
    if not isinstance(raw, list):
        return {"error": "pr list returned a non-list payload"}
    prs = [
        pr
        for pr in raw
        if isinstance(pr, dict)
        and isinstance(pr.get("number"), int)
        and isinstance(pr.get("mergedAt"), str)
        and pr["mergedAt"]
    ]
    prs.sort(key=lambda p: p["mergedAt"])
    # A DROPPED row makes this listing INCOMPLETE, and saying so is the whole
    # point: the filter above silently discards a merged PR whose `number` or
    # `mergedAt` is missing or malformed, and a caller that advances a watermark
    # over the survivors would move it PAST the dropped merge, which then never
    # receives its row. Reporting the count lets the caller treat the window as
    # incomplete instead of trusting a listing that quietly lost something
    # (Codex P2, PR #1836).
    return {
        "repo": repo,
        "prs": prs,
        "limit_hit": len(raw) >= limit,
        "dropped": len(raw) - len(prs),
    }


# GitHub caps pulls/N/files at 3000 entries; at the cap the listing MAY be
# incomplete, and an incomplete file list must read as unreadable, never as
# "these are all the files" (a docs-only verdict over a truncated list would
# silently exempt code). Ported from git_push_guard._pr_changed_files, which
# carries the same constant for the same reason.
_PR_FILES_API_CAP = 3000


async def list_pr_files(
    pr_number: int,
    *,
    repo: str,
    runner: Runner | None = None,
) -> dict:
    """Every filename a merged PR touches, or ``{"error": ...}`` — never raises.

    Returns ``{"files": [path, ...]}``. The verification lane's docs-only
    exemption rides on this being COMPLETE, so the three fail-closed rules from
    ``git_push_guard._pr_changed_files`` are ported verbatim in spirit:

    1. Rename SOURCES count: a row's ``previous_filename`` is a path the PR
       touched (a file renamed out of code into docs must not read docs-only).
    2. Strict record shape: any unparseable line, non-dict row, or null/empty
       filename makes the WHOLE read an error — a partial list that looks
       complete is the failure mode, not the remedy.
    3. The 3000-row API cap: ``rows >= cap`` → error (see ``_PR_FILES_API_CAP``).

    The caller's fail direction: an error here means the PR is NOT classified
    docs-only, so its obligation row stays open — a validator look is the cost
    of an unreadable list; a silent exemption would be the defect. ``--jq``
    emits one JSON object per line (the hook's idiom), which also sidesteps the
    ``--paginate`` concatenated-arrays parse trap ``pr_review_harvest`` handles
    with raw_decode.
    """
    run = runner or _default_runner
    rc, out, err = await run(
        [
            "gh",
            "api",
            f"repos/{repo}/pulls/{pr_number}/files",
            "--paginate",
            "--jq",
            ".[] | {filename: .filename, previous_filename: .previous_filename}",
        ]
    )
    if rc != 0:
        return {"error": f"pr files failed (rc={rc}): {err.strip()[:400]}"}
    files: list[str] = []
    rows = 0
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rows += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"error": f"pr files line unparseable: {exc}"}
        if not isinstance(obj, dict):
            return {"error": "pr files row is not an object"}
        name = obj.get("filename")
        if not isinstance(name, str) or not name:
            return {"error": "pr files row missing filename"}
        files.append(name)
        prev = obj.get("previous_filename")
        if prev is not None:
            if not isinstance(prev, str) or not prev:
                return {"error": "pr files row has malformed previous_filename"}
            files.append(prev)
    if rows >= _PR_FILES_API_CAP:
        return {
            "error": f"pr files hit the {_PR_FILES_API_CAP}-row API cap — list may be incomplete"
        }
    return {"files": files}


# Open-PR lane fields (session-manager PR-4c). Deliberately NOT PR_FIELDS
# (merged-only): the open-PR surface keys on updatedAt for age, the author's
# ``is_bot`` flag for the dependabot tag, and isDraft/mergeable for the clause.
# NO statusCheckRollup / reviewDecision — the surface is age-based only (every
# owner PR reads REVIEW_REQUIRED forever, so it carries no signal).
OPEN_PR_FIELDS = "number,title,url,isDraft,mergeable,updatedAt,createdAt,author"


async def list_open_prs(
    *,
    limit: int = 50,
    repo: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Enumerate OPEN PRs for the age-stale SessionStart surface.

    Returns ``{"repo", "prs", "limit_hit"}`` (fetched STALEST-first — see below),
    or ``{"error": ...}`` without raising. Rows missing an int ``number`` are
    dropped (gh contract violation, not a crash). Slug resolves LIVE (same
    stale-config hardening as ``list_merged_prs``).

    ``--search "sort:updated-asc"`` fetches least-recently-updated FIRST, so a
    capped window (``len(prs) == limit`` on a repo with >``limit`` open PRs) keeps
    the STALEST end — the lane's whole target — rather than gh's default
    newest-first order, which would silently drop exactly the aged PRs this
    surface exists to raise (and could hide them indefinitely). Any PR omitted by
    the cap is then strictly newer than every fetched one, so it cannot be stale;
    the client-side ``stale_days`` filter over this window is complete for the
    stale set (the ``≥N`` count floor stays honest when >``limit`` PRs are stale).
    Unlike merged enumeration (GitHub search cannot sort by mergedAt asc), open
    PRs sort by ``updatedAt`` fine — verified live against gh 2.x.
    """
    run = runner or _default_runner
    if repo is None:
        repo = await resolve_repo(run)
        if repo is None:
            return {"error": "repo slug resolve failed"}
    rc, out, err = await run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            "sort:updated-asc",
            "--json",
            OPEN_PR_FIELDS,
            "--limit",
            str(limit),
        ]
    )
    if rc != 0:
        return {"error": f"open pr list failed (rc={rc}): {err.strip()[:400]}"}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"open pr list returned invalid JSON: {exc}"}
    if not isinstance(raw, list):
        return {"error": "open pr list returned a non-list payload"}
    prs = [pr for pr in raw if isinstance(pr, dict) and isinstance(pr.get("number"), int)]
    return {"repo": repo, "prs": prs, "limit_hit": len(raw) >= limit}
