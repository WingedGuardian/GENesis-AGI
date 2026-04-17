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

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
_WATCHLIST_PATH = _CONFIG_DIR / "recon_watchlist.yaml"
_SCHEDULES_PATH = _CONFIG_DIR / "recon_schedules.yaml"
_SOURCES_PATH = _CONFIG_DIR / "recon_sources.yaml"

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

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "recon"))


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_watchlist() -> list[dict]:
    """Load the hardcoded project watchlist from config/recon_watchlist.yaml."""
    if not _WATCHLIST_PATH.exists():
        return []
    with open(_WATCHLIST_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("projects", []) if data else []


def _load_schedules() -> dict[str, dict]:
    if not _SCHEDULES_PATH.exists():
        return {}
    with open(_SCHEDULES_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("schedules", {}) if data else {}


def _save_schedules(schedules: dict[str, dict]) -> None:
    with open(_SCHEDULES_PATH, "w") as f:
        yaml.safe_dump({"schedules": schedules}, f, default_flow_style=False, sort_keys=False)


def _load_sources() -> list[dict]:
    if not _SOURCES_PATH.exists():
        return []
    with open(_SOURCES_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("sources", []) if data else []


def _save_sources(sources: list[dict]) -> None:
    with open(_SOURCES_PATH, "w") as f:
        yaml.safe_dump({"sources": sources}, f, default_flow_style=False, sort_keys=False)


# ── tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
async def recon_watchlist(
    priority: str | None = None,
) -> list[dict]:
    """Return the hardcoded project watchlist.

    These are projects Genesis actively monitors for updates, changes,
    and releases. Filtered by priority if provided.
    """
    projects = _load_watchlist()
    if priority:
        projects = [p for p in projects if p.get("priority") == priority]
    return projects


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
) -> dict:
    """Store a new recon finding as an observation.

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

    return {"finding_id": finding_id, "created_at": now}


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
async def recon_schedule(
    job_type: str,
    new_schedule: str | None = None,
) -> dict:
    """View or modify a recon gathering schedule.

    If new_schedule is None, returns the current schedule for job_type.
    Otherwise updates the cron expression.
    """
    schedules = _load_schedules()

    if job_type not in schedules:
        return {"error": f"Unknown job_type '{job_type}'. Available: {list(schedules.keys())}"}

    if new_schedule is None:
        return {"job_type": job_type, **schedules[job_type]}

    schedules[job_type]["cron"] = new_schedule
    _save_schedules(schedules)
    return {"job_type": job_type, "updated": True, **schedules[job_type]}


@mcp.tool()
async def recon_sources(
    action: str,
    source: dict | None = None,
) -> list[dict] | dict:
    """Manage watched sources. action: add, remove, or list.

    list: returns merged watchlist + dynamic sources.
    add/remove: operate on dynamic sources only (watchlist is immutable).
    source dict should have at minimum: name, url, type.
    """
    if action == "list":
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
        # Cannot remove watchlist entries
        watchlist_names = {p["name"] for p in _load_watchlist()}
        if source["name"] in watchlist_names:
            return {"error": f"Cannot remove watchlist entry '{source['name']}'. Watchlist is immutable."}
        sources = _load_sources()
        before = len(sources)
        sources = [s for s in sources if s.get("name") != source["name"]]
        _save_sources(sources)
        return {"removed": source["name"], "found": len(sources) < before, "total_dynamic": len(sources)}

    return {"error": f"Invalid action '{action}'. Must be add, remove, or list."}


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
        profiles_path = _CONFIG_DIR / "model_profiles.yaml"
        if profiles_path.exists():
            profile_registry = ModelProfileRegistry(profiles_path)
            profile_registry.load()
    except Exception:
        logger.debug("Profile registry load failed", exc_info=True)

    job = ModelIntelligenceJob(
        db=_db, profile_registry=profile_registry, surplus_queue=_surplus_queue,
    )
    return await job.run()
