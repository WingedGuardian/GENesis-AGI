"""recon-mcp server — reconnaissance findings, triage, scheduling, source management.

Watchlist is config-driven (static). Findings use the observations table.
Schedules and dynamic sources use YAML config files.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import yaml
from fastmcp import FastMCP

from genesis.db.crud import observations as obs_crud

logger = logging.getLogger(__name__)

mcp = FastMCP("genesis-recon")

_REPO_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_USER_CONFIG_DIR = Path.home() / ".genesis" / "config"

# Watchlist is read-only (curated by developer), always from repo
_WATCHLIST_PATH = _REPO_CONFIG_DIR / "recon_watchlist.yaml"

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
    """Load the hardcoded project watchlist from config/recon_watchlist.yaml."""
    if not _WATCHLIST_PATH.exists():
        return []
    with open(_WATCHLIST_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("projects", []) if data else []


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
