"""PR enumeration over the gh CLI (injectable runner).

Home of three listings: merged PRs (the repo-pulse cursor lane), open PRs (the
age-stale SessionStart surface), and — for the zero-drop detector's head-ref
history join — the full ``--state all`` listing. All three share the live slug
resolution, the timeout and the loud-cap discipline below. The import is
one-way (``zero_drop`` reads this module; nothing here knows about it).

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
    return {"repo": repo, "prs": prs, "limit_hit": len(raw) >= limit}


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


# Full-history fields for the zero-drop branch join (session-awareness
# zero_drop). Deliberately NOT PR_FIELDS: the join reads identity, state and
# timing only — never title or body, so no PR prose can reach a model prompt
# through this path.
#
# `headRefOid` is the load-bearing one and it costs nothing extra: it is the
# head SHA as of the merge or close, so `headRefOid == local tip` is PROOF the
# PR contained exactly this commit, where the head-ref NAME is only a
# heuristic. MEASURED 2026-09-06 on this repo (1665 PRs): every PR carries it,
# and it is a SNAPSHOT rather than a live pointer — 4 of 4 PRs whose branch
# moved after merge/close still report the old SHA, 0 counterexamples in the 28
# cases where the head branch still exists. That snapshot property is what
# makes it evidence about the merge instead of evidence about the branch now.
# `closedAt` gives the CLOSED verdict the same time guard `mergedAt` gives the
# merged one, and `headRepositoryOwner` scopes the join to this repo so a fork
# PR cannot cover a local branch that merely shares its name.
ALL_PR_FIELDS = "number,headRefName,headRefOid,state,mergedAt,closedAt,url,headRepositoryOwner"


async def list_all_prs(
    *,
    limit: int = 2000,
    repo: str | None = None,
    runner: Runner | None = None,
) -> dict:
    """Enumerate PRs in EVERY state for a head-ref-name history join.

    Returns ``{"repo", "prs", "limit_hit"}`` or ``{"error": ...}`` without
    raising. Rows missing an int ``number`` or a string ``headRefName`` are
    dropped (gh contract violation, not a crash). Slug resolves LIVE, the same
    stale-config hardening as the merged/open enumerations.

    The whole history is the point: the consumer classifies a local branch by
    what EVER happened to its name, so a windowed fetch would silently
    reclassify old branches as never-PR'd. ``limit_hit`` is therefore not a
    nicety — the consumer must FREEZE (skip its branch classes for that run)
    when the window caps, because a truncated history turns a merged branch
    into a false "stranded" finding. MEASURED 2026-09-05: 1651 PRs in one
    ~6s call at the default limit.
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
            "all",
            "--json",
            ALL_PR_FIELDS,
            "--limit",
            str(limit),
        ]
    )
    if rc != 0:
        return {"error": f"all pr list failed (rc={rc}): {err.strip()[:400]}"}
    try:
        raw = json.loads(out)
    except json.JSONDecodeError as exc:
        return {"error": f"all pr list returned invalid JSON: {exc}"}
    if not isinstance(raw, list):
        return {"error": "all pr list returned a non-list payload"}
    prs = []
    for pr in raw:
        if not (
            isinstance(pr, dict)
            and isinstance(pr.get("number"), int)
            and isinstance(pr.get("headRefName"), str)
            and pr["headRefName"]
        ):
            continue
        # Flatten the owner to its LOGIN and drop the rest of the object. gh
        # returns `{id, name, login}` and `name` is the account holder's real
        # name — which would otherwise ride into the findings store, the logs
        # and an MCP response read by a model, for a join that only ever needs
        # to answer "is this head ref in our own repo?". Narrowing it here
        # keeps that value out of everything downstream by construction rather
        # than by every consumer remembering not to read it.
        owner = pr.get("headRepositoryOwner")
        login = owner.get("login") if isinstance(owner, dict) else None
        pr = {k: v for k, v in pr.items() if k != "headRepositoryOwner"}
        pr["headRepositoryOwnerLogin"] = login if isinstance(login, str) else None
        prs.append(pr)
    return {"repo": repo, "prs": prs, "limit_hit": len(raw) >= limit}
