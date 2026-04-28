"""Module list, toggle, and config routes."""

from __future__ import annotations

import json
import sqlite3

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint, logger

# Explicit mapping: module_name → list of routing call_site IDs.
# Call sites use numeric IDs from model_routing.yaml, NOT module names.
# Update this when adding new modules or call sites.
_MODULE_CALL_SITES: dict[str, list[str]] = {
    "content_pipeline": ["35_content_draft"],
    # prediction_markets and crypto_ops: add call sites when enabled and routed
}

def _validate_config_update(fields: list[dict], updates: dict) -> list[str]:
    """Pre-validate config updates against the field schema.

    Checks required, numeric min/max, and type coercibility.
    Returns a list of error strings (empty = no errors).
    Modules still own cross-field validation in update_config().
    """
    schema = {f["name"]: f for f in fields}
    errors = []
    for field_name, value in updates.items():
        f = schema.get(field_name)
        if f is None:
            continue  # Unknown fields pass through — module decides
        if f.get("required") and (value is None or value == ""):
            errors.append(f"{field_name} is required")
            continue
        field_type = f.get("type")
        if field_type in ("int", "float") and value is not None:
            try:
                v = float(value)
                mn = f.get("min")
                mx = f.get("max")
                if mn is not None and v < mn:
                    errors.append(f"{field_name} must be >= {mn}")
                if mx is not None and v > mx:
                    errors.append(f"{field_name} must be <= {mx}")
            except (ValueError, TypeError):
                errors.append(f"{field_name} must be a number")
    return errors


def _get_module_description(mod) -> str:
    """Get description from module — YAML-sourced for both native and external."""
    from genesis.modules.external.adapter import ExternalProgramAdapter
    if isinstance(mod, ExternalProgramAdapter):
        return mod.config.description
    # Native modules may have a _description set from YAML
    desc = getattr(mod, "_description", None)
    if isinstance(desc, str) and desc:
        return desc
    return ""


def _get_identity_fields(mod) -> dict:
    """Extract display_name, category, tags, version from a module instance."""
    from genesis.modules.external.adapter import ExternalProgramAdapter
    if isinstance(mod, ExternalProgramAdapter):
        cfg = mod.config
        return {
            "display_name": cfg.display_name or mod.name,
            "category": cfg.category,
            "tags": cfg.tags,
            "version": cfg.version,
        }
    return {
        "display_name": getattr(mod, "_display_name", None) or mod.name,
        "category": getattr(mod, "_category", ""),
        "tags": getattr(mod, "_tags", []),
        "version": getattr(mod, "_version", ""),
    }


@blueprint.route("/api/genesis/modules")
@_async_route
async def modules_list():
    """Return registered capability modules with status and stats."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.module_registry is None:
        return jsonify([])

    result = []
    for name in rt.module_registry.list_modules():
        mod = rt.module_registry.get(name)
        if mod is None:
            continue
        from genesis.modules.external.adapter import ExternalProgramAdapter

        entry = {
            "name": mod.name,
            "enabled": mod.enabled,
            "description": _get_module_description(mod),
            "research_profile": mod.get_research_profile_name(),
            "type": "native",
            **_get_identity_fields(mod),
        }

        # Enrich external modules with adapter-specific data
        if isinstance(mod, ExternalProgramAdapter):
            entry["type"] = "external"
            entry["description"] = mod.config.description or entry["description"]
            entry["ipc_url"] = mod.config.ipc.url
            entry["ipc_method"] = mod.config.ipc.method
            entry["ipc_healthy"] = mod.healthy
            entry["ipc_error"] = mod.last_health_error
            if mod.config.lifecycle:
                entry["has_lifecycle"] = True
            if mod.config.health_check:
                entry["health_endpoint"] = mod.config.health_check.endpoint

        tracker = getattr(mod, "_tracker", None)
        if tracker is not None and hasattr(tracker, "stats"):
            try:
                entry["stats"] = tracker.stats()
            except Exception:
                logger.warning("Failed to get stats for module %s", name, exc_info=True)
        if hasattr(mod, "configurable_fields") and callable(getattr(mod, "configurable_fields", None)):
            try:
                fields = mod.configurable_fields()
                if isinstance(fields, list):
                    # Mask value/default for secret and sensitive fields — never
                    # expose credentials in the API response even in read-only form.
                    masked = []
                    for f in fields:
                        if f.get("type") == "secret" or f.get("sensitive"):
                            f = {**f, "value": None, "default": None}
                        masked.append(f)
                    entry["config_fields"] = masked
            except Exception:
                pass

        # Universal health data — assembled from existing tables
        entry["health"] = await _build_module_health(rt, mod)
        result.append(entry)
    return jsonify(result)


async def _build_module_health(rt, mod) -> dict:
    """Assemble universal health data for a module from existing DB tables.

    Queries job_health, cost_events, and module_config.  Gracefully degrades
    when the database is unavailable or mocked (returns sensible defaults).
    """
    health: dict = {"status": "unknown", "last_run": None, "cost": {}}
    profile = mod.get_research_profile_name()
    db = getattr(rt, "db", None)

    # Guard: db must be a real aiosqlite connection, not a MagicMock
    if db is not None and not hasattr(db, "execute_fetchall"):
        try:
            # Quick smoke test — if execute isn't a coroutine, skip DB queries
            import inspect
            if not inspect.iscoroutinefunction(getattr(db, "execute", None)):
                db = None
        except Exception:
            db = None

    if profile and db:
        # Job health from pipeline:* entries
        job_name = f"pipeline:{profile}"
        try:
            cursor = await db.execute(
                "SELECT last_run, last_success, last_failure, last_error, "
                "consecutive_failures, total_runs, total_successes, total_failures "
                "FROM job_health WHERE job_name = ?",
                (job_name,),
            )
            row = await cursor.fetchone()
            if row:
                health["last_run"] = row[0]
                health["last_success"] = row[1]
                health["last_failure"] = row[2]
                health["last_error"] = row[3]
                health["consecutive_failures"] = row[4]
                health["total_runs"] = row[5]
                health["success_rate"] = round(row[6] / row[5] * 100, 1) if row[5] > 0 else None
                health["status"] = "error" if row[4] > 0 else "healthy"
        except (sqlite3.Error, TypeError):
            logger.debug("Module health job query failed for %s", job_name, exc_info=True)

    # Cost attribution via explicit call site mapping
    call_sites = _MODULE_CALL_SITES.get(mod.name, [])
    if call_sites and db:
        placeholders = ",".join("?" * len(call_sites))
        for period, clause in [("today", "date('now')"), ("month", "date('now', 'start of month')")]:
            try:
                cursor = await db.execute(
                    f"SELECT COALESCE(SUM(cost_usd), 0), COUNT(*) FROM cost_events "
                    f"WHERE json_extract(metadata, '$.call_site') IN ({placeholders}) "
                    f"AND created_at >= {clause}",
                    call_sites,
                )
                row = await cursor.fetchone()
                if row:
                    health["cost"][f"{period}_usd"] = round(row[0], 4)
                    health["cost"][f"{period}_calls"] = row[1]
            except (sqlite3.Error, TypeError):
                logger.debug("Module cost query failed for %s/%s", mod.name, period, exc_info=True)

    # Config metadata
    if db:
        try:
            cursor = await db.execute(
                "SELECT updated_at FROM module_config WHERE module_name = ?",
                (mod.name,),
            )
            row = await cursor.fetchone()
            if row:
                health["config_updated_at"] = row[0]
        except (sqlite3.Error, TypeError):
            pass

    # Derive final status — enabled state takes priority over historical data
    from genesis.modules.external.adapter import ExternalProgramAdapter

    if not mod.enabled:
        health["status"] = "disabled"
    elif isinstance(mod, ExternalProgramAdapter):
        # External modules: use real IPC health, not pipeline job data
        if mod.healthy:
            health["status"] = "healthy"
        else:
            health["status"] = "error"
            health["last_error"] = mod.last_health_error or "IPC health check failed"
    elif health["status"] == "unknown":
        if not profile:
            health["status"] = "no_pipeline"
        else:
            health["status"] = "idle"

    return health


@blueprint.route("/api/genesis/modules/<name>/toggle", methods=["POST"])
@_async_route
async def module_toggle(name: str):
    """Toggle a capability module on or off.

    Side effect: when a module owns a research profile (via
    ``get_research_profile_name()``), toggling the module also
    adds/removes the profile's pipeline scheduler job.
    """
    from genesis.modules.persistence import save_module_state
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.module_registry is None:
        return jsonify({"status": "error", "message": "runtime not available"}), 503

    mod = rt.module_registry.get(name)
    if mod is None:
        return jsonify({"status": "error", "message": f"module '{name}' not found"}), 404

    new_state = not mod.enabled
    mod.enabled = new_state
    logger.info("Module '%s' toggled to %s via dashboard", name, "enabled" if new_state else "disabled")

    if rt.db is not None:
        await save_module_state(rt.db, name, enabled=new_state)

    # Manage the associated research profile's scheduler job
    profile_name = mod.get_research_profile_name()
    profile_managed = False
    if profile_name and rt.surplus_scheduler is not None:
        scheduler = rt.surplus_scheduler._scheduler
        job_id = f"pipeline_{profile_name}"
        if new_state:
            # Module enabled → add profile job if profile YAML is also enabled
            try:
                from apscheduler.triggers.interval import IntervalTrigger

                from genesis.pipeline.profiles import ProfileLoader

                loader = ProfileLoader()
                loader.load_all()
                loader.merge_overlay()
                profile = loader.get(profile_name)
                if profile and profile.enabled:
                    async def _cycle(pname: str = profile_name) -> None:
                        from genesis.runtime.init.pipeline import run_pipeline_cycle
                        await run_pipeline_cycle(rt, pname)

                    scheduler.add_job(
                        _cycle,
                        IntervalTrigger(minutes=profile.tier0_interval_minutes),
                        id=job_id,
                        max_instances=1,
                        misfire_grace_time=300,
                        replace_existing=True,
                    )
                    logger.info("Added pipeline job %s (module %s enabled)", job_id, name)
                    profile_managed = True
                elif profile and not profile.enabled:
                    logger.info(
                        "Module %s enabled but profile %s is disabled in YAML — no job added",
                        name, profile_name,
                    )
            except Exception:
                logger.warning("Failed to add pipeline job for %s", profile_name, exc_info=True)
        else:
            # Module disabled → remove profile job
            try:
                scheduler.remove_job(job_id)
                logger.info("Removed pipeline job %s (module %s disabled)", job_id, name)
                profile_managed = True
            except Exception:
                # Job may not exist (e.g., profile was disabled in YAML)
                logger.debug("No pipeline job %s to remove", job_id)

    return jsonify({
        "status": "ok",
        "name": name,
        "enabled": new_state,
        "profile_managed": profile_managed,
    })


@blueprint.route("/api/genesis/modules/<name>/config", methods=["PATCH"])
@_async_route
async def module_config(name: str):
    """Update a module's configurable fields."""
    from genesis.modules.persistence import save_module_state
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.module_registry is None:
        return jsonify({"status": "error", "message": "runtime not available"}), 503

    mod = rt.module_registry.get(name)
    if mod is None:
        return jsonify({"status": "error", "message": f"module '{name}' not found"}), 404

    if not hasattr(mod, "update_config"):
        return jsonify({"status": "error", "message": f"module '{name}' has no configurable fields"}), 400

    data = request.get_json(silent=True) or {}

    # Framework pre-validation: enforce min/max/required from the field schema
    # before delegating to the module. Modules still handle cross-field logic.
    if hasattr(mod, "configurable_fields") and callable(mod.configurable_fields):
        try:
            schema_errors = _validate_config_update(mod.configurable_fields(), data)
            if schema_errors:
                return jsonify({"status": "error", "message": "; ".join(schema_errors)}), 400
        except Exception:
            logger.debug("Config schema pre-validation failed for %s", name, exc_info=True)

    try:
        new_config = mod.update_config(data)
        logger.info("Module '%s' config updated via dashboard: fields=%s", name, list(data.keys()))

        if rt.db is not None:
            await save_module_state(rt.db, name, config_json=json.dumps(new_config))

        return jsonify({"status": "ok", "name": name, "config": new_config})
    except (ValueError, TypeError) as e:
        return jsonify({"status": "error", "message": str(e)}), 400
