"""recon-mcp server — reconnaissance findings, triage, scheduling, source management.

Watchlist is config-driven (static). Findings use the observations table.
Schedules and dynamic sources use YAML config files.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import aiosqlite
import httpx
import yaml
from fastmcp import FastMCP

from genesis.db.crud import observations as obs_crud

logger = logging.getLogger(__name__)

mcp = FastMCP("genesis-recon")

_REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_USER_CONFIG_DIR = Path.home() / ".genesis" / "config"

# Schedules and sources are user-modifiable — prefer user override
_REPO_SCHEDULES = _REPO_CONFIG_DIR / "recon_schedules.yaml"
_REPO_SOURCES = _REPO_CONFIG_DIR / "recon_sources.yaml"
_USER_SCHEDULES = _USER_CONFIG_DIR / "recon_schedules.yaml"
_USER_SOURCES = _USER_CONFIG_DIR / "recon_sources.yaml"

_db: aiosqlite.Connection | None = None
_router: object | None = None
_surplus_queue: object | None = None
_pipeline: object | None = None
_memory_store: object | None = None

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_VISIBILITY_QUALIFIER_RE = re.compile(
    r"(?<!\S)(?:is|visibility):(public|private|internal)(?!\S)", re.IGNORECASE,
)
_GITHUB_ISSUE_TYPE_QUALIFIER_RE = re.compile(
    r"(?<!\S)(?:is|type):(issue|pr)(?!\S)", re.IGNORECASE,
)
_GITHUB_BOOLEAN_OR_RE = re.compile(r"(?<!\S)OR(?!\S)")
_GITHUB_TREE_ENTRY_LIMIT = 2_000
_GITHUB_CONTENTS_MAX_BYTES = 8 * 1024 * 1024
_GITHUB_API_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
_GITHUB_BLOB_RESPONSE_MAX_BYTES = 12 * 1024 * 1024
_GITHUB_CONTENTS_MAX_BASE64_CHARS = 4 * ((_GITHUB_CONTENTS_MAX_BYTES + 2) // 3)


@dataclass(frozen=True)
class _GitHubAPIFailure:
    kind: str
    status_code: int | None = None


def _github_failure_message(action: str, failure: object) -> str:
    """Turn a bounded transport failure into useful recovery guidance."""
    if not isinstance(failure, _GitHubAPIFailure):
        return f"GitHub {action} failed"
    if failure.kind == "rate_limited":
        return f"GitHub {action} was rate limited; retry after the public API reset"
    if failure.kind == "forbidden_or_rate_limited":
        return (
            f"GitHub {action} returned HTTP 403 without decisive rate-limit headers; "
            "wait at least one minute before one retry, then treat recurrence as forbidden"
        )
    if failure.kind == "not_found":
        return f"GitHub {action} was not found"
    if failure.kind == "invalid_request":
        return f"GitHub {action} rejected the request; revise the query, path, or ref"
    if failure.kind == "response_too_large":
        return f"GitHub {action} response exceeded Genesis's bounded limit"
    if failure.kind == "invalid_response":
        return f"GitHub {action} returned an invalid response"
    if failure.kind == "timeout":
        return f"GitHub {action} timed out"
    if failure.kind == "network":
        return f"GitHub {action} failed because of a network error"
    return f"GitHub {action} failed with HTTP {failure.status_code}"


async def _github_public_api(
    endpoint: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int | float = 15,
    max_bytes: int = _GITHUB_API_RESPONSE_MAX_BYTES,
) -> tuple[bool, str | _GitHubAPIFailure]:
    """Fetch a bounded GitHub.com REST response without operator credentials."""
    url = f"https://api.github.com/{endpoint.lstrip('/')}"
    try:
        async with (
            httpx.AsyncClient(
                headers={
                    "Accept": "application/vnd.github+json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "Genesis-public-research",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                follow_redirects=False,
                timeout=timeout,
            ) as client,
            client.stream("GET", url, params=params) as response,
        ):
            if response.status_code != 200:
                logger.warning(
                    "Public GitHub API request failed (%s): %s",
                    response.status_code,
                    endpoint,
                )
                rate_limit_headers = (
                    response.headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in response.headers
                )
                if response.status_code == 429 or (
                    response.status_code == 403 and rate_limit_headers
                ):
                    kind = "rate_limited"
                elif response.status_code == 403:
                    kind = "forbidden_or_rate_limited"
                elif response.status_code == 404:
                    kind = "not_found"
                elif response.status_code == 422:
                    kind = "invalid_request"
                else:
                    kind = "http_error"
                return False, _GitHubAPIFailure(kind, response.status_code)
            body = bytearray()
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                if len(chunk) > max_bytes - len(body):
                    logger.warning("Public GitHub API response exceeded cap: %s", endpoint)
                    return False, _GitHubAPIFailure("response_too_large")
                body.extend(chunk)
        return True, body.decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("Public GitHub API returned invalid UTF-8: %s", endpoint)
        return False, _GitHubAPIFailure("invalid_response")
    except httpx.TimeoutException:
        logger.warning("Public GitHub API request timed out: %s", endpoint)
        return False, _GitHubAPIFailure("timeout")
    except (httpx.HTTPError, OSError, ValueError):
        logger.warning("Public GitHub API request failed: %s", endpoint, exc_info=True)
        return False, _GitHubAPIFailure("network")


async def _verify_public_repository(
    repository: str,
) -> tuple[bool, dict | None, _GitHubAPIFailure | None]:
    """Return verification state, public metadata, and any transport failure."""
    ok, raw = await _github_public_api(f"repos/{repository}")
    if not ok:
        if isinstance(raw, _GitHubAPIFailure) and raw.kind == "not_found":
            return True, None, None
        return False, None, raw if isinstance(raw, _GitHubAPIFailure) else None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, None, _GitHubAPIFailure("invalid_response")
    classification = _repository_visibility(payload)
    if classification is True:
        return True, payload, None
    if classification is False:
        return True, None, None
    return False, None, _GitHubAPIFailure("invalid_response")


def _repository_visibility(repository: object) -> bool | None:
    """Classify repository evidence as public, nonpublic, or invalid."""
    if not isinstance(repository, dict) or not isinstance(repository.get("private"), bool):
        return None
    private = repository["private"]
    visibility = repository.get("visibility")
    if visibility == "public" and private is False:
        return True
    if visibility in {"private", "internal"}:
        return False
    return None


def _issue_repository(item: object) -> str | None:
    """Extract owner/name from a GitHub issue-search repository URL."""
    if not isinstance(item, dict):
        return None
    url = item.get("repository_url")
    if not isinstance(url, str):
        return None
    prefix = "https://api.github.com/repos/"
    if not url.startswith(prefix):
        return None
    repository = url[len(prefix):]
    return repository if _GITHUB_REPO_RE.fullmatch(repository) else None


def _bounded_file_metadata(payload: object) -> dict:
    """Select small, non-content fields from an untrusted Contents response."""
    if not isinstance(payload, dict):
        return {"response_type": type(payload).__name__}
    fields = ("name", "path", "sha", "size", "type", "encoding", "html_url", "download_url")
    return {name: payload.get(name) for name in fields if name in payload}


def init_recon_mcp(
    *, db: aiosqlite.Connection, router: object | None = None,
    activity_tracker=None, pipeline: object | None = None,
    memory_store: object | None = None,
    surplus_queue: object | None = None,
) -> None:
    """Wire runtime dependencies. Called by GenesisRuntime."""
    global _db, _router, _pipeline, _memory_store, _surplus_queue
    _db = db
    _router = router
    _pipeline = pipeline
    _memory_store = memory_store
    _surplus_queue = surplus_queue

    if activity_tracker is not None:
        from genesis.observability.mcp_middleware import InstrumentationMiddleware

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "recon", db=db))


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_watchlist() -> list[dict]:
    """Active watchlist (base minus user-disabled + install overlay).

    Delegates to the shared ``recon.watchlist`` store so the recon_config MCP
    view reflects overlay edits made via the dashboard (previously this read
    base-only, so those edits were invisible here). Read-only by design —
    recon targets must not be self-modifiable from an autonomous loop.
    """
    from genesis.recon import watchlist
    return watchlist.active_entries()


def _load_schedules() -> dict[str, dict]:
    path = _USER_SCHEDULES if _USER_SCHEDULES.exists() else _REPO_SCHEDULES
    if not path.exists():
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("schedules", {}) if data else {}


def _save_schedules(schedules: dict[str, dict]) -> None:
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_USER_SCHEDULES, "w") as f:
        yaml.safe_dump({"schedules": schedules}, f, default_flow_style=False, sort_keys=False)


def _load_sources() -> list[dict]:
    path = _USER_SOURCES if _USER_SOURCES.exists() else _REPO_SOURCES
    if not path.exists():
        return []
    with open(path) as f:
        data = yaml.safe_load(f)
    return data.get("sources", []) if data else []


def _save_sources(sources: list[dict]) -> None:
    _USER_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_USER_SOURCES, "w") as f:
        yaml.safe_dump({"sources": sources}, f, default_flow_style=False, sort_keys=False)


# ── tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def recon_config(
    aspect: str,
    action: str = "view",
    job_type: str | None = None,
    new_schedule: str | None = None,
    source: dict | None = None,
    priority: str | None = None,
) -> list[dict] | dict:
    """View or modify recon configuration.

    aspect: 'watchlist' | 'schedule' | 'sources'

    For watchlist (read-only):
      action='view', optional priority filter.

    For schedule:
      action='view' to list all schedules, or + job_type for one.
      action='update' + job_type + new_schedule to change cron expression.

    For sources:
      action='list' to see watchlist + dynamic sources merged.
      action='add' + source dict to add a dynamic source.
      action='remove' + source dict to remove a dynamic source.
      Watchlist entries are immutable.
    """
    valid_aspects = {"watchlist", "schedule", "sources"}
    if aspect not in valid_aspects:
        return {"error": f"Invalid aspect '{aspect}'. Must be one of: {sorted(valid_aspects)}"}

    if aspect == "watchlist":
        projects = _load_watchlist()
        if priority:
            projects = [p for p in projects if p.get("priority") == priority]
        return projects

    if aspect == "schedule":
        schedules = _load_schedules()
        if not job_type:
            if action == "view":
                return [{"job_type": k, **v} for k, v in schedules.items()]
            return {"error": "job_type is required for schedule update"}
        if job_type not in schedules:
            return {"error": f"Unknown job_type '{job_type}'. Available: {list(schedules.keys())}"}
        if action == "view":
            return {"job_type": job_type, **schedules[job_type]}
        if action == "update":
            if not new_schedule:
                return {"error": "new_schedule is required for schedule update"}
            schedules[job_type]["cron"] = new_schedule
            _save_schedules(schedules)
            return {"job_type": job_type, "updated": True, **schedules[job_type]}
        return {"error": f"Invalid action '{action}' for schedule. Must be view or update."}

    # aspect == "sources"
    if action == "view" or action == "list":
        watchlist = [{"origin": "watchlist", **p} for p in _load_watchlist()]
        dynamic = [{"origin": "dynamic", **s} for s in _load_sources()]
        return watchlist + dynamic

    if action == "add":
        if not source or "name" not in source:
            return {"error": "source dict with 'name' required for add"}
        sources = _load_sources()
        sources.append(source)
        _save_sources(sources)
        return {"added": source["name"], "total_dynamic": len(sources)}

    if action == "remove":
        if not source or "name" not in source:
            return {"error": "source dict with 'name' required for remove"}
        watchlist_names = {p["name"] for p in _load_watchlist()}
        if source["name"] in watchlist_names:
            return {"error": f"Cannot remove watchlist entry '{source['name']}'. Watchlist is immutable."}
        sources = _load_sources()
        before = len(sources)
        sources = [s for s in sources if s.get("name") != source["name"]]
        _save_sources(sources)
        return {"removed": source["name"], "found": len(sources) < before, "total_dynamic": len(sources)}

    return {"error": f"Invalid action '{action}' for sources. Must be list, add, or remove."}


@mcp.tool()
async def recon_findings(
    job_type: str | None = None,
    priority: str | None = None,
    triaged: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query stored recon findings.

    Findings are observations with source='recon', type='finding'.
    job_type maps to category, triaged maps to resolved.
    """
    if _db is None:
        return [{"error": "Database not initialized"}]

    resolved = None
    if triaged is not None:
        resolved = triaged

    results = await obs_crud.query(
        _db,
        source="recon",
        type="finding",
        category=job_type,
        priority=priority,
        resolved=resolved,
        limit=limit,
    )
    return results


@mcp.tool()
async def recon_store_finding(
    title: str,
    summary: str,
    job_type: str,
    priority: str = "medium",
    source_url: str | None = None,
    expires_at: str | None = None,
    life_domain: str | None = None,
) -> dict:
    """Store a new recon finding as an observation.

    Args:
        life_domain: Optional life domain tag ("personal", "employment", "genesis").
            Stored as a content annotation for context — NOT queryable via
            recon_findings. To query by domain, grep the content field.

    Returns the finding ID.
    """
    if _db is None:
        return {"error": "Database not initialized"}

    finding_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    content = title
    if summary:
        content = f"{title}\n\n{summary}"
    if source_url:
        content += f"\n\nSource: {source_url}"
    if life_domain:
        content += f"\n\n[life_domain: {life_domain}]"

    await obs_crud.create(
        _db,
        id=finding_id,
        source="recon",
        type="finding",
        category=job_type,
        content=content,
        priority=priority,
        created_at=now,
        expires_at=expires_at,
    )

    return {"finding_id": finding_id, "created_at": now, "life_domain": life_domain}


@mcp.tool()
async def recon_triage(
    finding_id: str,
    notes: str,
    action: str,
) -> dict:
    """Triage a recon finding. action: dismiss, acknowledge, or defer.

    dismiss/acknowledge mark the finding as resolved.
    defer adds notes without resolving.
    """
    if _db is None:
        return {"success": False, "error": "Database not initialized"}

    valid_actions = {"dismiss", "acknowledge", "defer"}
    if action not in valid_actions:
        return {"success": False, "error": f"Invalid action '{action}'. Must be one of: {valid_actions}"}

    now = datetime.now(UTC).isoformat()

    if action in ("dismiss", "acknowledge"):
        resolution = f"[{action}] {notes}"
        ok = await obs_crud.resolve(_db, finding_id, resolved_at=now, resolution_notes=resolution)
        return {"success": ok, "action": action}
    else:
        # defer: add notes without resolving
        cursor = await _db.execute(
            "UPDATE observations SET resolution_notes = ? WHERE id = ?",
            (f"[deferred] {notes}", finding_id),
        )
        await _db.commit()
        return {"success": cursor.rowcount > 0, "action": "defer"}


@mcp.tool()
async def recon_cc_update_check(
    old_version: str,
    new_version: str,
) -> dict:
    """Analyze a Claude Code version change for impact on Genesis.

    Fetches changelog, classifies impact (none/informational/action_needed/breaking),
    stores finding, and alerts on high-impact changes.
    """
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.recon.cc_update_analyzer import CCUpdateAnalyzer

    analyzer = CCUpdateAnalyzer(
        db=_db, router=_router, pipeline=_pipeline, memory_store=_memory_store,
    )
    return await analyzer.analyze(old_version, new_version)


@mcp.tool()
async def recon_run_model_intelligence() -> dict:
    """Run model intelligence scan — check for new models, pricing changes, stale profiles.

    Normally runs weekly (Sundays 6am). This tool runs it on-demand.
    Compares OpenRouter model list against known profiles, flags new models
    with 100k+ context, pricing changes, and profiles not reviewed in 30+ days.
    """
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.recon.model_intelligence import ModelIntelligenceJob

    # Try to load profile registry if available
    profile_registry = None
    try:
        from genesis.routing.model_profiles import ModelProfileRegistry
        profiles_path = _REPO_CONFIG_DIR / "model_profiles.yaml"
        if profiles_path.exists():
            profile_registry = ModelProfileRegistry(profiles_path)
            profile_registry.load()
    except Exception:
        logger.debug("Profile registry load failed", exc_info=True)

    job = ModelIntelligenceJob(
        db=_db, profile_registry=profile_registry, surplus_queue=_surplus_queue,
    )
    return await job.run()


@mcp.tool()
async def recon_run_skill_scan() -> dict:
    """Run the skill-security scan on-demand (NVIDIA SkillSpector → recon findings).

    Normally runs weekly (Monday 2am). Scans installed skills and files findings
    for UNTRUSTED skills only — trusted-source skills (first-party + the
    --seed-trusted allowlist) are scanned but kept out of recon to avoid noise.
    Requires SkillSpector installed (see scripts/bootstrap.sh); returns a
    {"skipped": ...} summary if the binary is missing.
    """
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.recon.skill_security_scan_job import SkillSecurityScanJob

    job = SkillSecurityScanJob(db=_db)
    return await job.run()


@mcp.tool()
async def recon_run_github_discovery(query: str, limit: int = 10) -> dict:
    """Discover GitHub repos for a topic, ranked by momentum/activity/maturity.

    On-demand foreground tool — searches GitHub (newest+most-starred pool),
    scores each repo on three axes, and returns the top `limit` ranked
    candidates. Files NOTHING (read-only). The composite `score` plus its
    momentum/activity/maturity breakdown are returned so a fast-growing
    lower-star repo can visibly outrank a stale high-star one.

    momentum = stars-per-day-since-creation (log-damped); activity = push
    recency; maturity = repo age. Forks and archived repos are excluded.
    """
    from genesis.recon.github_discovery import search_repos

    candidates = await search_repos(query, limit=limit)
    repos = [
        {
            "full_name": c.full_name,
            "url": c.url,
            "stars": c.stars,
            "language": c.language,
            "description": (c.description or "")[:200],
            "created_at": c.created_at,
            "pushed_at": c.pushed_at,
            "score": round(c.score, 4),
            "momentum": round(c.momentum, 4),
            "activity": round(c.activity, 4),
            "maturity": round(c.maturity, 4),
        }
        for c in candidates
    ]
    result = {"query": query, "count": len(repos), "repos": repos}
    if not repos:
        result["note"] = "no results — if unexpected, check gh auth / rate-limit (30/min) in logs"
    return result


@mcp.tool()
async def recon_github_search(
    kind: str,
    query: str,
    page: int = 1,
    per_page: int = 30,
) -> dict:
    """Search public GitHub.com repositories or issues without shell access.

    This is a read-only, fixed-endpoint wrapper around GitHub's search API.
    It reports transport/API failure separately from a successful empty result.
    Pagination is explicit: page >= 1 and per_page is limited to 1..100.
    """
    if kind not in {"repositories", "issues"}:
        return {"ok": False, "error": "kind must be repositories or issues"}
    if not query.strip():
        return {"ok": False, "error": "query must not be empty"}
    if "\x00" in query:
        return {"ok": False, "error": "query must not contain NUL bytes"}
    if page < 1 or not 1 <= per_page <= 100:
        return {"ok": False, "error": "page must be >= 1 and per_page must be 1..100"}

    # Remove caller-supplied visibility terms before adding the authoritative
    # public constraint. Local response checks below provide a second boundary.
    public_query = _GITHUB_VISIBILITY_QUALIFIER_RE.sub("", query).strip()
    if kind == "issues":
        # GitHub's search/issues endpoint combines issues and pull requests.
        # Its legacy REST grammar does not reliably preserve a trailing type
        # qualifier across Boolean OR branches, so callers must split those into
        # separate searches. Remove other caller-supplied type qualifiers and
        # add the authoritative issue-only constraint.
        if _GITHUB_BOOLEAN_OR_RE.search(public_query):
            return {
                "ok": False,
                "error": "issues query must not contain Boolean OR; run separate searches",
            }
        public_query = _GITHUB_ISSUE_TYPE_QUALIFIER_RE.sub("", public_query).strip()
        if not public_query:
            return {
                "ok": False,
                "error": "query must include terms beyond visibility/type qualifiers",
            }
        public_query = f"{public_query} is:issue is:public"
    else:
        public_query = f"{public_query} is:public".strip()
    ok, raw = await _github_public_api(
        f"search/{kind}",
        params={"q": public_query, "page": str(page), "per_page": str(per_page)},
    )
    if not ok:
        return {"ok": False, "error": _github_failure_message("search", raw)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": "GitHub search returned invalid JSON"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "GitHub search returned an invalid payload"}

    if "items" not in payload or "total_count" not in payload:
        return {"ok": False, "error": "GitHub search response omitted required fields"}
    raw_items = payload["items"]
    if not isinstance(raw_items, list):
        return {"ok": False, "error": "GitHub search returned an invalid items payload"}

    if kind == "issues":
        if any("pull_request" in item for item in raw_items if isinstance(item, dict)):
            return {
                "ok": False,
                "error": "GitHub issues search unexpectedly returned a pull request",
            }
        if any(_issue_repository(item) is None for item in raw_items):
            return {
                "ok": False,
                "error": "GitHub issues search returned an invalid repository identity",
            }
        # This fixed, unauthenticated api.github.com transport cannot see private
        # repositories. Exact repository URLs above prevent ambiguous identities.
        items = raw_items
    else:
        classifications = [_repository_visibility(item) for item in raw_items]
        if any(classification is None for classification in classifications):
            return {
                "ok": False,
                "error": "GitHub repository search could not verify repository visibility",
            }
        items = [
            item for item, classification in zip(raw_items, classifications, strict=True)
            if classification is True
        ]

    api_total = payload["total_count"]
    if isinstance(api_total, bool) or not isinstance(api_total, int) or api_total < 0:
        return {"ok": False, "error": "GitHub search returned an invalid total_count"}
    # The transport never sends operator credentials, so this count and its
    # pagination signal contain public GitHub.com results only.
    total = api_total
    accessible = min(api_total, 1000)
    has_more = page * per_page < accessible
    return {
        "ok": True,
        "kind": kind,
        "query": query,
        "total_count": total,
        "accessible_count": accessible,
        "incomplete_results": bool(payload.get("incomplete_results", False)),
        "items": items,
        "visibility_filter_applied": True,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
    }


@mcp.tool()
async def recon_github_read(
    repository: str,
    operation: str = "repository",
    path: str = "",
    ref: str = "",
    max_chars: int = 50000,
) -> dict:
    """Inspect GitHub repository metadata, a recursive tree, or one file.

    Read-only operations: ``repository``, ``tree``, and ``file``. File content
    is decoded as UTF-8 and capped at max_chars (1..100000); the response says
    when it was truncated and provides the exact total plus GitHub URLs.
    """
    if not _GITHUB_REPO_RE.fullmatch(repository):
        return {"ok": False, "error": "repository must be owner/name"}
    if operation not in {"repository", "tree", "file"}:
        return {"ok": False, "error": "operation must be repository, tree, or file"}
    if not 1 <= max_chars <= 100000:
        return {"ok": False, "error": "max_chars must be 1..100000"}
    if operation == "file" and not path.strip("/"):
        return {"ok": False, "error": "path is required for file reads"}
    if "\x00" in ref or "\x00" in path:
        return {"ok": False, "error": "path and ref must not contain NUL bytes"}

    visibility_verified, repository_metadata, visibility_failure = (
        await _verify_public_repository(repository)
    )
    if not visibility_verified:
        return {
            "ok": False,
            "error": _github_failure_message("repository visibility check", visibility_failure),
        }
    if repository_metadata is None:
        return {"ok": False, "error": "repository must exist and be public"}

    if operation == "repository":
        return {
            "ok": True,
            "operation": "repository",
            "repository": repository,
            "result": repository_metadata,
        }
    if operation == "tree":
        endpoint = f"repos/{repository}/git/trees/{quote(ref or 'HEAD', safe='')}"
        params = {"recursive": "1"}
    else:
        # Rejecting '..' keeps the endpoint constrained to the requested
        # repository's contents route.
        parts = [part for part in path.strip("/").split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return {"ok": False, "error": "path may not contain . or .. segments"}
        encoded_path = "/".join(quote(part, safe="") for part in parts)
        endpoint = f"repos/{repository}/contents/{encoded_path}"
        params = {"ref": ref} if ref else None

    ok, raw = await _github_public_api(endpoint, params=params)
    if not ok:
        return {"ok": False, "error": _github_failure_message(f"{operation} read", raw)}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"GitHub {operation} read returned invalid JSON"}

    if operation == "tree":
        if not isinstance(payload, dict) or not isinstance(payload.get("tree"), list):
            return {"ok": False, "error": "GitHub tree read returned an invalid payload"}
        tree = payload["tree"]
        upstream_truncated = bool(payload.get("truncated", False))
        local_truncated = len(tree) > _GITHUB_TREE_ENTRY_LIMIT
        result = dict(payload)
        result["tree"] = tree[:_GITHUB_TREE_ENTRY_LIMIT]
        result["truncated"] = upstream_truncated or local_truncated
        result["upstream_truncated"] = upstream_truncated
        result["local_truncated"] = local_truncated
        result["returned_entries"] = len(result["tree"])
        result["received_entries"] = len(tree)
        return {"ok": True, "operation": "tree", "repository": repository, "result": result}
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "requested path is not a file",
            "metadata": _bounded_file_metadata(payload),
        }
    if payload.get("type") != "file":
        return {
            "ok": False,
            "error": "GitHub contents response was not a file",
            "metadata": _bounded_file_metadata(payload),
        }
    file_size = payload.get("size")
    if isinstance(file_size, bool) or not isinstance(file_size, int) or file_size < 0:
        return {
            "ok": False,
            "error": "GitHub file response had invalid size metadata",
            "metadata": _bounded_file_metadata(payload),
        }
    if file_size > _GITHUB_CONTENTS_MAX_BYTES:
        return {
            "ok": False,
            "error": "GitHub file exceeds the supported 8 MiB limit",
            "metadata": _bounded_file_metadata(payload),
        }
    content_payload = payload
    if payload.get("encoding") == "none":
        sha = payload.get("sha")
        if not isinstance(sha, str):
            return {
                "ok": False,
                "error": "GitHub large-file response had invalid metadata",
                "metadata": _bounded_file_metadata(payload),
            }
        blob_ok, blob_raw = await _github_public_api(
            f"repos/{repository}/git/blobs/{quote(sha, safe='')}",
            timeout=60,
            max_bytes=_GITHUB_BLOB_RESPONSE_MAX_BYTES,
        )
        if not blob_ok:
            return {
                "ok": False,
                "error": _github_failure_message("large-file read", blob_raw),
                "metadata": _bounded_file_metadata(payload),
            }
        try:
            content_payload = json.loads(blob_raw)
        except json.JSONDecodeError:
            return {"ok": False, "error": "GitHub large-file read returned invalid JSON"}
        if not isinstance(content_payload, dict):
            return {"ok": False, "error": "GitHub large-file read returned an invalid payload"}
        blob_size = content_payload.get("size")
        if (
            isinstance(blob_size, bool)
            or not isinstance(blob_size, int)
            or blob_size != file_size
        ):
            return {
                "ok": False,
                "error": "GitHub large-file response had inconsistent size metadata",
                "metadata": _bounded_file_metadata(payload),
            }
    if content_payload.get("encoding") != "base64":
        return {"ok": False, "error": "GitHub file response was not base64 encoded", "metadata": _bounded_file_metadata(payload)}
    encoded_content = content_payload.get("content")
    if not isinstance(encoded_content, str):
        return {"ok": False, "error": "GitHub file response had invalid content", "metadata": _bounded_file_metadata(payload)}
    try:
        encoded_chars = sum(not char.isspace() for char in encoded_content)
        if encoded_chars > _GITHUB_CONTENTS_MAX_BASE64_CHARS:
            return {
                "ok": False,
                "error": "GitHub file exceeds the supported 8 MiB limit",
                "metadata": _bounded_file_metadata(payload),
            }
        compact_content = "".join(encoded_content.split())
        decoded_bytes = base64.b64decode(compact_content, validate=True)
        if len(decoded_bytes) > _GITHUB_CONTENTS_MAX_BYTES:
            return {
                "ok": False,
                "error": "GitHub file exceeds the supported 8 MiB limit",
                "metadata": _bounded_file_metadata(payload),
            }
        if len(decoded_bytes) != file_size:
            return {
                "ok": False,
                "error": "GitHub file response had inconsistent size metadata",
                "metadata": _bounded_file_metadata(payload),
            }
        decoded = decoded_bytes.decode("utf-8")
    except (binascii.Error, TypeError, ValueError, UnicodeDecodeError):
        return {"ok": False, "error": "file is not UTF-8 text", "metadata": _bounded_file_metadata(payload)}
    return {
        "ok": True,
        "operation": "file",
        "repository": repository,
        "path": path,
        "ref": ref or None,
        "sha": payload.get("sha"),
        "size": payload.get("size"),
        "html_url": payload.get("html_url"),
        "download_url": payload.get("download_url"),
        "content": decoded[:max_chars],
        "truncated": len(decoded) > max_chars,
        "total_chars": len(decoded),
    }


@mcp.tool()
async def recon_run_github_discovery_job() -> dict:
    """Run the curated GitHub Discovery JOB on-demand (files new repos → triage).

    Normally runs weekly (Wednesday 6am). Searches the configured topics
    (config/github_discovery_topics.yaml), scores candidates, and files the top
    few NEW high-signal repos as recon findings to the TRIAGE queue (surfaced via
    recon_findings job_type="github_discovery") — never the knowledge base.
    Curated by design: narrow topics, a hard per-run cap, a score threshold, and
    dedup vs the watchlist + already-filed findings. Returns a count summary.
    """
    if _db is None:
        return {"error": "Database not initialized"}

    from genesis.recon.github_discovery import GitHubDiscoveryJob

    job = GitHubDiscoveryJob(db=_db, router=_router)
    return await job.run()
