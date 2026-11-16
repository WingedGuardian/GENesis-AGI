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
    fields: str = PR_FIELDS,
    runner: Runner | None = None,
) -> dict:
    """Enumerate PRs merged on/after ``since_date`` (YYYY-MM-DD, date-granular).

    ``fields`` is the ``--json`` field list, ``PR_FIELDS`` unless a caller needs
    others (the validation picker asks for ``mergeCommit``). Rows are still
    filtered on ``number`` and ``mergedAt``, so a custom list must include both.

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
            fields,
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


_QUEUE_PR_QUERY = """query($owner:String!, $name:String!, $after:String) {
  repository(owner:$owner, name:$name) {
    url
    defaultBranchRef { name }
    pullRequests(states:OPEN, first:100, after:$after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft mergeable updatedAt createdAt headRefOid baseRefName
        labels(first:100) { totalCount pageInfo { hasNextPage endCursor } nodes { name } }
        commits(last:1) { nodes { commit { committedDate } } }
      }
    }
  }
}"""
_QUEUE_LABELS_QUERY = """query($owner:String!, $name:String!, $number:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      labels(first:100, after:$after) {
        totalCount pageInfo { hasNextPage endCursor } nodes { name }
      }
    }
  }
}"""
_QUEUE_COMMENTS_QUERY = """query($owner:String!, $name:String!, $number:Int!, $after:String) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      comments(first:100, after:$after) {
        totalCount pageInfo { hasNextPage endCursor }
        nodes { authorAssociation body createdAt }
      }
    }
  }
}"""


def _queue_graphql_argv(repo: str, query: str, variables: dict) -> list[str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ValueError("repo must be an owner/name slug")
    argv = ["gh", "api", "graphql", "-f", f"query={query}"]
    argv.extend(["-F", f"owner={parts[0]}", "-F", f"name={parts[1]}"])
    for key, value in variables.items():
        if value is not None:
            argv.extend(["-F", f"{key}={value}"])
    return argv


async def _queue_graphql(
    repo: str, query: str, variables: dict, run: Runner
) -> tuple[dict | None, str | None]:
    try:
        argv = _queue_graphql_argv(repo, query, variables)
    except ValueError as exc:
        return None, str(exc)
    rc, out, err = await run(argv)
    if rc:
        return None, f"gh api graphql failed (rc={rc}): {err.strip()[:400]}"
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        return None, f"gh api graphql returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "gh api graphql returned a non-object payload"
    if payload.get("errors"):
        return None, f"gh api graphql returned errors: {str(payload['errors'])[:400]}"
    data = payload.get("data")
    return (data, None) if isinstance(data, dict) else (None, "gh api graphql omitted data")


async def _complete_queue_labels(pr: dict, repo: str, run: Runner) -> str | None:
    labels = pr.get("labels")
    if not isinstance(labels, dict) or not isinstance(labels.get("nodes"), list):
        return "queue listing omitted labels"
    page = labels.get("pageInfo")
    if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
        return "queue listing returned malformed label pagination"
    seen = set()
    while page["hasNextPage"]:
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            return "label pagination cursor missing or repeated"
        seen.add(cursor)
        data, error = await _queue_graphql(
            repo, _QUEUE_LABELS_QUERY, {"number": pr["number"], "after": cursor}, run
        )
        if error:
            return error
        repository = data.get("repository")
        pull = repository.get("pullRequest") if isinstance(repository, dict) else None
        connection = pull.get("labels") if isinstance(pull, dict) else None
        if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
            return "label pagination returned malformed data"
        labels["nodes"].extend(connection["nodes"])
        page = connection.get("pageInfo")
        if not isinstance(page, dict) or type(page.get("hasNextPage")) is not bool:
            return "label pagination returned malformed pageInfo"
        if connection.get("totalCount") != labels.get("totalCount"):
            return "label totalCount changed during pagination"
    if labels.get("totalCount") != len(labels["nodes"]):
        return "label totalCount does not match rows read"
    return None


async def list_queue_prs(*, repo: str | None = None, runner: Runner | None = None) -> dict:
    """Read every open PR and the live default branch through gh GraphQL.

    The result retains malformed numbered rows for the sweep to report. A
    missing/non-integer number or failed cursor page is a listing error; a
    terminal page whose count disagrees with ``totalCount`` is returned with
    ``limit_hit`` and honest ``read``/``open_total`` values.
    """
    run = runner or _default_runner
    if repo is None:
        repo = await resolve_repo(run)
    if not repo:
        return {"repo": repo, "prs": [], "open_total": 0, "read": 0,
                "limit_hit": True, "error": "repo slug resolve failed"}
    prs, total, default_branch, repo_url = [], None, None, None
    cursor, seen, limit_hit = None, set(), False
    while True:
        data, error = await _queue_graphql(repo, _QUEUE_PR_QUERY, {"after": cursor}, run)
        if error:
            return {"repo": repo, "prs": prs, "open_total": total or 0, "read": len(prs),
                    "default_branch": default_branch, "repo_url": repo_url,
                    "limit_hit": True, "error": error}
        repository = data.get("repository")
        connection = repository.get("pullRequests") if isinstance(repository, dict) else None
        branch = repository.get("defaultBranchRef") if isinstance(repository, dict) else None
        page = connection.get("pageInfo") if isinstance(connection, dict) else None
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        count = connection.get("totalCount") if isinstance(connection, dict) else None
        if type(count) is not int or not isinstance(nodes, list) or not isinstance(page, dict):
            return {"repo": repo, "prs": prs, "open_total": total or 0, "read": len(prs),
                    "default_branch": default_branch, "repo_url": repo_url,
                    "limit_hit": True, "error": "queue listing returned malformed connection"}
        if total is None:
            total = count
            default_branch = branch.get("name") if isinstance(branch, dict) else None
            repo_url = repository.get("url")
            if not default_branch or not isinstance(repo_url, str):
                return {"repo": repo, "prs": prs, "open_total": total, "read": 0,
                        "limit_hit": True, "error": "queue listing omitted repo URL or default branch"}
        elif count != total:
            limit_hit = True
            total = max(total, count)
        for row in nodes:
            if not isinstance(row, dict) or type(row.get("number")) is not int:
                return {"repo": repo, "prs": prs, "open_total": total, "read": len(prs),
                        "default_branch": default_branch, "repo_url": repo_url,
                        "limit_hit": True, "error": "queue listing row omitted an integer number"}
            prs.append(row)
            label_error = await _complete_queue_labels(row, repo, run)
            if label_error:
                row["_queue_error"] = label_error
        more = page.get("hasNextPage")
        if type(more) is not bool:
            return {"repo": repo, "prs": prs, "open_total": total, "read": len(prs),
                    "default_branch": default_branch, "repo_url": repo_url,
                    "limit_hit": True, "error": "queue listing omitted hasNextPage"}
        if not more:
            break
        next_cursor = page.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            return {"repo": repo, "prs": prs, "open_total": total, "read": len(prs),
                    "default_branch": default_branch, "repo_url": repo_url,
                    "limit_hit": True, "error": "queue listing cursor missing or repeated"}
        seen.add(next_cursor)
        cursor = next_cursor
    return {"repo": repo, "prs": prs, "open_total": total, "read": len(prs),
            "default_branch": default_branch, "repo_url": repo_url,
            "limit_hit": limit_hit or len(prs) != total}


async def list_queue_pr_comments(
    repo: str, number: int, *, runner: Runner | None = None
) -> dict:
    """Read all issue comments for a labeled PR, preserving partial-read errors."""
    run = runner or _default_runner
    comments, total, cursor, seen = [], None, None, set()
    while True:
        data, error = await _queue_graphql(
            repo, _QUEUE_COMMENTS_QUERY, {"number": number, "after": cursor}, run
        )
        if error:
            return {"comments": comments, "read": len(comments), "total": total or 0,
                    "limit_hit": True, "error": error}
        repository = data.get("repository")
        pull = repository.get("pullRequest") if isinstance(repository, dict) else None
        connection = pull.get("comments") if isinstance(pull, dict) else None
        page = connection.get("pageInfo") if isinstance(connection, dict) else None
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        count = connection.get("totalCount") if isinstance(connection, dict) else None
        if type(count) is not int or not isinstance(nodes, list) or not isinstance(page, dict):
            return {"comments": comments, "read": len(comments), "total": total or 0,
                    "limit_hit": True, "error": "comments returned malformed connection"}
        if total is None:
            total = count
        elif total != count:
            return {"comments": comments, "read": len(comments), "total": max(total, count),
                    "limit_hit": True, "error": "comment totalCount changed during pagination"}
        comments.extend(nodes)
        more = page.get("hasNextPage")
        if type(more) is not bool:
            return {"comments": comments, "read": len(comments), "total": total,
                    "limit_hit": True, "error": "comments omitted hasNextPage"}
        if not more:
            break
        next_cursor = page.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
            return {"comments": comments, "read": len(comments), "total": total,
                    "limit_hit": True, "error": "comment cursor missing or repeated"}
        seen.add(next_cursor)
        cursor = next_cursor
    return {"comments": comments, "read": len(comments), "total": total,
            "limit_hit": len(comments) != total}


async def read_queue_pr_mergeability(
    repo: str, number: int, *, runner: Runner | None = None
) -> dict:
    """One delayed mergeability reread for a queue row initially marked UNKNOWN."""
    run = runner or _default_runner
    rc, out, err = await run([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "mergeable", "-q", ".mergeable",
    ])
    value = out.strip()
    if rc:
        return {"error": f"mergeability reread failed (rc={rc}): {err.strip()[:400]}"}
    if value not in {"MERGEABLE", "CONFLICTING", "UNKNOWN"}:
        return {"error": f"mergeability reread returned invalid value: {value[:120]}"}
    return {"mergeable": value}


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
