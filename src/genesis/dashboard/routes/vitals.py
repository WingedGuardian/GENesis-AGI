"""Operational vitals — real system internals from Qdrant, SQLite, LLM routing, embeddings, MCP."""

from __future__ import annotations

import contextlib
import logging
import os
from collections import defaultdict

import httpx
from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.env import (
    dashscope_api_key,
    deepinfra_api_key,
    genesis_db_path,
    ollama_enabled,
    ollama_tags_url,
    ollama_url,
    qdrant_collections_url,
    repo_root,
)

logger = logging.getLogger(__name__)

_TABLES_WITH_CREATED_AT = {
    "observations", "events", "cost_events", "cc_sessions",
    "awareness_ticks", "pending_embeddings", "outreach_history",
    "surplus_tasks", "surplus_insights", "inbox_items",
    "session_bookmarks", "telegram_messages", "activity_log",
}

# Known MCP servers — shown even with zero activity so user sees full inventory.
_KNOWN_MCP_SERVERS = ["health", "memory", "outreach", "recon"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_routing_context() -> dict:
    """Load model_routing.yaml + model_profiles.yaml for provider enrichment.

    Returns dict with:
      providers: {name: {type, model, free, enabled, profile}}
      call_site_assignments: {provider_name: [call_site_id, ...]}
      profiles: {profile_name: {intelligence_tier, display_name, ...}}
    """
    try:
        from genesis.routing.config import load_config

        config_path = repo_root() / "config" / "model_routing.yaml"
        if not config_path.exists():
            return {"providers": {}, "call_site_assignments": {}, "profiles": {}}

        cfg = load_config(config_path)

        providers = {}
        for name, pc in cfg.providers.items():
            providers[name] = {
                "type": pc.provider_type,
                "model": pc.model_id,
                "free": pc.is_free,
                "enabled": pc.enabled,
                "profile": pc.profile,
            }

        # Cross-reference: which call_sites use each provider
        assignments: dict[str, list[str]] = defaultdict(list)
        for cs_id, cs in cfg.call_sites.items():
            for pname in cs.chain:
                assignments[pname].append(cs_id)

        # Load model profiles for intelligence tier enrichment
        profiles: dict[str, dict] = {}
        try:
            from genesis.routing.model_profiles import ModelProfileRegistry

            profiles_path = repo_root() / "config" / "model_profiles.yaml"
            if profiles_path.exists():
                registry = ModelProfileRegistry(profiles_path)
                registry.load()
                for pname, prof in registry.all_profiles().items():
                    profiles[pname] = {
                        "display_name": prof.display_name,
                        "intelligence_tier": prof.intelligence_tier,
                        "reasoning": prof.reasoning,
                        "cost_tier": prof.cost_tier,
                        "context_window": prof.context_window,
                        "latency": prof.latency,
                    }
        except Exception:
            logger.debug("Failed to load model profiles for vitals", exc_info=True)

        return {
            "providers": providers,
            "call_site_assignments": dict(assignments),
            "profiles": profiles,
        }
    except Exception:
        logger.warning("Failed to load routing config for vitals", exc_info=True)
        return {"providers": {}, "call_site_assignments": {}, "profiles": {}}


def _provider_display_name(name: str, meta: dict) -> str:
    """Build a human-readable display name like 'Kimi K2.5 (via OpenRouter)'."""
    ptype = meta.get("type", "")
    if ptype == "openrouter":
        return f"{name} (via OpenRouter)"
    if ptype == "zenmux":
        return f"{name} (via ZenMux)"
    return name


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------

@blueprint.route("/api/genesis/operational-vitals")
@_async_route
async def operational_vitals():
    """Return operational vitals: real system internals."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped:
        return jsonify({"error": "not bootstrapped"}), 503

    routing_ctx = _load_routing_context()

    result: dict = {
        "qdrant": await _build_qdrant_section(rt),
        "sqlite": await _build_sqlite_section(rt),
        "embedding": await _build_embedding_section(rt),
        "llm": await _build_llm_section(rt, routing_ctx),
        "mcp": await _build_mcp_section(rt),
        "onprem": await _build_onprem_section(),
    }

    return jsonify(result)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

async def _build_qdrant_section(rt) -> dict:
    """Qdrant vector store: collections, points, real embedding throughput."""
    section: dict = {"collections": [], "total_points": 0, "error": None}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            collections_url = qdrant_collections_url()
            resp = await client.get(collections_url)
            resp.raise_for_status()
            collections = resp.json().get("result", {}).get("collections", [])
            total_points = 0
            for coll in collections:
                name = coll["name"]
                try:
                    detail_resp = await client.get(f"{collections_url}/{name}")
                    detail_resp.raise_for_status()
                    info = detail_resp.json().get("result", {})
                    points = info.get("points_count", 0)
                    vectors = info.get("vectors_count", 0)
                    segments = info.get("segments_count", 0)
                    dim = (
                        info.get("config", {}).get("params", {})
                        .get("vectors", {}).get("size", 0)
                    )
                    indexed = info.get("indexed_vectors_count", 0)
                    threshold = (
                        info.get("config", {}).get("hnsw_config", {})
                        .get("full_scan_threshold", 10000)
                    )
                    total_points += points
                    section["collections"].append({
                        "name": name, "points": points, "vectors": vectors,
                        "segments": segments, "dimension": dim,
                        "indexed_vectors": indexed,
                        "full_scan_threshold": threshold,
                    })
                except Exception:
                    section["collections"].append(
                        {"name": name, "error": "detail fetch failed"},
                    )
            section["total_points"] = total_points
            # Flag collections where indexing should be active but isn't
            warnings = []
            for coll in section["collections"]:
                threshold = coll.get("full_scan_threshold", 10000)
                pts = coll.get("points", 0)
                if pts > threshold and coll.get("indexed_vectors", 0) == 0:
                    warnings.append(
                        f"Collection '{coll['name']}' has {pts} points exceeding "
                        f"index threshold ({threshold}) but 0 indexed vectors."
                    )
            section["warnings"] = warnings
    except httpx.ConnectError:
        section["error"] = "unreachable"
    except Exception:
        section["error"] = "query failed"

    if rt.db:
        try:
            cursor = await rt.db.execute(
                "SELECT COUNT(*) FROM pending_embeddings WHERE status = 'pending'"
            )
            row = await cursor.fetchone()
            section["pending_queue"] = row[0] if row else 0
        except Exception:
            pass

        # Real embedding throughput from activity_log (not just pending_embeddings)
        try:
            cursor = await rt.db.execute(
                "SELECT COUNT(*) FROM activity_log "
                "WHERE provider LIKE '%embed%' AND success = 1 "
                "AND created_at >= datetime('now', '-24 hours')"
            )
            row = await cursor.fetchone()
            section["embedded_24h"] = row[0] if row else 0
        except Exception:
            section["embedded_24h"] = 0

    return section


async def _build_sqlite_section(rt) -> dict:
    """SQLite relational store: tables, rows, observations, events."""
    section: dict = {"tables": [], "db_size_mb": 0.0, "error": None}
    db_path = os.path.expanduser(str(genesis_db_path()))
    with contextlib.suppress(OSError):
        section["db_size_mb"] = round(os.path.getsize(db_path) / (1024 * 1024), 1)

    if rt.db:
        try:
            cursor = await rt.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '%_fts%' "
                "ORDER BY name"
            )
            tables = [r[0] for r in await cursor.fetchall()]
            for tbl in tables:
                entry: dict = {"name": tbl, "rows": 0}
                try:
                    cursor = await rt.db.execute(
                        f"SELECT COUNT(*) FROM [{tbl}]"  # noqa: S608
                    )
                    row = await cursor.fetchone()
                    entry["rows"] = row[0] if row else 0
                except Exception:
                    entry["rows"] = -1
                if tbl in _TABLES_WITH_CREATED_AT:
                    try:
                        cursor = await rt.db.execute(
                            f"SELECT COUNT(*) FROM [{tbl}] "  # noqa: S608
                            "WHERE created_at >= datetime('now', '-24 hours')"
                        )
                        row = await cursor.fetchone()
                        entry["rows_24h"] = row[0] if row else 0
                    except Exception:
                        pass
                section["tables"].append(entry)

            try:
                cursor = await rt.db.execute(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE created_at >= datetime('now', '-24 hours')), "
                    "  COUNT(*) FILTER (WHERE resolved = 1 AND resolved_at >= datetime('now', '-24 hours')), "
                    "  COUNT(*) FILTER (WHERE resolved = 0) "
                    "FROM observations"
                )
                row = await cursor.fetchone()
                if row:
                    section["observations"] = {
                        "created_24h": row[0],
                        "resolved_24h": row[1],
                        "open_total": row[2],
                    }
            except Exception:
                pass

            try:
                cursor = await rt.db.execute(
                    "SELECT "
                    "  COUNT(*) FILTER (WHERE created_at >= datetime('now', '-1 hour')), "
                    "  COUNT(*) FILTER (WHERE created_at >= datetime('now', '-24 hours')) "
                    "FROM events"
                )
                row = await cursor.fetchone()
                if row:
                    section["events"] = {
                        "events_1h": row[0], "events_24h": row[1],
                    }
            except Exception:
                pass

        except Exception:
            section["error"] = "query failed"

    return section


async def _build_embedding_section(rt) -> dict:
    """Embedding pipeline: per-backend stats, active model, dual chain order."""
    section: dict = {
        "backends": [],
        "total_embeddings_24h": 0,
        "error": None,
    }

    section["active_model"] = os.environ.get(
        "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b-fp16",
    )

    # Build both chain orderings from env config (mirrors EmbeddingProvider.build_chain)
    ollama_names = ["ollama"] if ollama_enabled() else []
    cloud_names = []
    if deepinfra_api_key():
        cloud_names.append("deepinfra")
    if dashscope_api_key():
        cloud_names.append("dashscope")

    storage_chain = ollama_names + cloud_names  # writes: Ollama first
    recall_chain = cloud_names + ollama_names    # reads: cloud first

    section["storage_chain"] = storage_chain if storage_chain else ["none configured"]
    section["recall_chain"] = recall_chain if recall_chain else ["none configured"]
    # Backward compat: keep chain_order as the storage chain
    section["chain_order"] = section["storage_chain"]

    if rt.db:
        try:
            cursor = await rt.db.execute(
                "SELECT provider, COUNT(*) as calls, "
                "  COALESCE(AVG(latency_ms), 0) as avg_lat, "
                "  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors "
                "FROM activity_log "
                "WHERE (provider LIKE '%embedding%' OR provider LIKE '%embed%') "
                "  AND provider NOT LIKE 'mcp.%' "
                "  AND created_at >= datetime('now', '-24 hours') "
                "GROUP BY provider ORDER BY calls DESC"
            )
            total = 0
            for row in await cursor.fetchall():
                calls = row[1]
                errors = row[3]
                total += calls
                section["backends"].append({
                    "name": row[0],
                    "calls_24h": calls,
                    "avg_latency_ms": round(row[2], 1),
                    "error_rate": round(errors / calls, 3) if calls > 0 else 0.0,
                    "errors_24h": errors,
                })
            section["total_embeddings_24h"] = total
        except Exception:
            section["error"] = "query failed"

    return section


async def _build_llm_section(rt, routing_ctx: dict) -> dict:
    """LLM routing: all providers enriched with routing config, merged activity + cost."""
    section: dict = {
        "providers": [],
        "totals": {},
        "sessions": {},
        "error": None,
    }
    routing_providers = routing_ctx.get("providers", {})
    assignments = routing_ctx.get("call_site_assignments", {})
    profiles = routing_ctx.get("profiles", {})

    if rt.db:
        try:
            # Activity data from activity_log
            activity_by_provider: dict[str, dict] = {}
            cursor = await rt.db.execute(
                "SELECT provider, COUNT(*) as calls, "
                "  COALESCE(AVG(latency_ms), 0) as avg_lat, "
                "  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors "
                "FROM activity_log "
                "WHERE provider LIKE 'llm.%' "
                "  AND created_at >= datetime('now', '-24 hours') "
                "GROUP BY provider ORDER BY calls DESC"
            )
            for row in await cursor.fetchall():
                pname = row[0][4:] if row[0].startswith("llm.") else row[0]
                calls = row[1]
                errors = row[3]
                activity_by_provider[pname] = {
                    "calls_24h": calls,
                    "avg_latency_ms": round(row[2], 1),
                    "error_rate": round(errors / calls, 3) if calls > 0 else 0.0,
                    "errors_24h": errors,
                }

            # Cost data from cost_events
            cost_by_provider: dict[str, dict] = {}
            cursor = await rt.db.execute(
                "SELECT provider, "
                "  COALESCE(SUM(cost_usd), 0), "
                "  COALESCE(AVG(input_tokens + output_tokens), 0) "
                "FROM cost_events "
                "WHERE created_at >= datetime('now', '-24 hours') "
                "GROUP BY provider"
            )
            for row in await cursor.fetchall():
                cost_by_provider[row[0]] = {
                    "cost_24h": round(row[1], 4),
                    "avg_tokens": round(row[2]),
                }

            # Merge: all routing-config providers + any with activity
            all_names = set(routing_providers.keys()) | set(activity_by_provider.keys())
            providers_list = []
            for pname in sorted(all_names):
                meta = routing_providers.get(pname, {})
                activity = activity_by_provider.get(pname, {})
                cost = cost_by_provider.get(pname, {})
                cs_list = assignments.get(pname, [])

                # Profile enrichment: intelligence tier, cost tier, etc.
                profile_key = meta.get("profile", "")
                prof = profiles.get(profile_key, {}) if profile_key else {}

                providers_list.append({
                    "name": pname,
                    "display_name": _provider_display_name(pname, meta),
                    "provider_type": meta.get("type", "unknown"),
                    "model_id": meta.get("model", ""),
                    "free": meta.get("free", False),
                    "enabled": meta.get("enabled", True),
                    "call_sites": cs_list,
                    "call_site_count": len(cs_list),
                    "calls_24h": activity.get("calls_24h", 0),
                    "avg_latency_ms": activity.get("avg_latency_ms", 0),
                    "error_rate": activity.get("error_rate", 0),
                    "errors_24h": activity.get("errors_24h", 0),
                    "cost_24h": cost.get("cost_24h", 0),
                    "avg_tokens": cost.get("avg_tokens", 0),
                    "intelligence_tier": prof.get("intelligence_tier", ""),
                    "reasoning_tier": prof.get("reasoning", ""),
                    "cost_tier": prof.get("cost_tier", ""),
                    "context_window": prof.get("context_window", 0),
                })

            providers_list.sort(key=lambda p: (-p["calls_24h"], p["name"]))
            section["providers"] = providers_list

            # Totals
            cursor = await rt.db.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE created_at >= datetime('now', '-1 hour')), "
                "  COUNT(*) FILTER (WHERE created_at >= datetime('now', '-24 hours')), "
                "  COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= datetime('now', '-1 hour')), 0), "
                "  COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= datetime('now', '-24 hours')), 0) "
                "FROM cost_events"
            )
            row = await cursor.fetchone()
            if row:
                section["totals"] = {
                    "calls_1h": row[0], "calls_24h": row[1],
                    "cost_1h": round(row[2], 4), "cost_24h": round(row[3], 4),
                }

            # Sessions
            cursor = await rt.db.execute(
                "SELECT "
                "  COUNT(*) FILTER (WHERE status = 'active' AND session_type = 'foreground'), "
                "  COUNT(*) FILTER (WHERE status = 'active' AND session_type != 'foreground'), "
                "  COUNT(*) FILTER (WHERE started_at >= datetime('now', '-24 hours')), "
                "  COALESCE(SUM(cost_usd) FILTER (WHERE started_at >= datetime('now', '-24 hours')), 0) "
                "FROM cc_sessions"
            )
            row = await cursor.fetchone()
            if row:
                section["sessions"] = {
                    "fg_active": row[0], "bg_active": row[1],
                    "created_24h": row[2],
                    "session_cost_24h": round(row[3], 4),
                }
        except Exception:
            logger.warning("LLM vitals query failed", exc_info=True)
            section["error"] = "query failed"

    return section


async def _build_mcp_section(rt) -> dict:
    """MCP servers: actual server inventory with per-tool invocation stats."""
    section: dict = {"servers": [], "error": None}

    if rt.db:
        try:
            cursor = await rt.db.execute(
                "SELECT provider, COUNT(*) as calls, "
                "  COALESCE(AVG(latency_ms), 0) as avg_lat, "
                "  SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors "
                "FROM activity_log "
                "WHERE provider LIKE 'mcp.%' "
                "  AND created_at >= datetime('now', '-24 hours') "
                "GROUP BY provider ORDER BY calls DESC"
            )

            server_tools: dict[str, list[dict]] = defaultdict(list)
            for row in await cursor.fetchall():
                parts = row[0].split(".", 2)  # mcp.server.tool
                server = parts[1] if len(parts) >= 2 else "unknown"
                tool = parts[2] if len(parts) >= 3 else row[0]
                server_tools[server].append({
                    "tool": tool,
                    "calls": row[1],
                    "errors": row[3],
                    "avg_latency_ms": round(row[2], 1),
                })

            seen = set(server_tools.keys())
            all_servers = seen | set(_KNOWN_MCP_SERVERS)

            for server_name in sorted(all_servers):
                tools = server_tools.get(server_name, [])
                total_calls = sum(t["calls"] for t in tools)
                section["servers"].append({
                    "name": f"genesis-{server_name}",
                    "tools_invoked": tools,
                    "total_calls_24h": total_calls,
                    "tool_count": len(tools),
                })
        except Exception:
            logger.warning("MCP vitals query failed", exc_info=True)
            section["error"] = "query failed"

    return section


async def _build_onprem_section() -> dict:
    """On-prem inference: Ollama and LM Studio status (optional)."""
    section: dict = {}

    # Ollama
    ollama_info: dict = {"enabled": ollama_enabled()}
    if ollama_info["enabled"]:
        ollama_info["url"] = ollama_url()
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(ollama_tags_url())
                resp.raise_for_status()
                models = resp.json().get("models", [])
                ollama_info["status"] = "online"
                ollama_info["models"] = [
                    {
                        "name": m.get("name", "unknown"),
                        "size_gb": round(m.get("size", 0) / (1024**3), 2),
                        "parameter_size": m.get("details", {}).get("parameter_size", ""),
                        "quantization": m.get("details", {}).get("quantization_level", ""),
                        "family": m.get("details", {}).get("family", ""),
                    }
                    for m in models
                ]
        except httpx.ConnectError:
            ollama_info["status"] = "unreachable"
            ollama_info["models"] = []
        except Exception:
            ollama_info["status"] = "error"
            ollama_info["models"] = []
    section["ollama"] = ollama_info

    # LM Studio
    lm_enabled = os.environ.get(
        "GENESIS_ENABLE_LM_STUDIO", "false",
    ).lower() in ("1", "true", "yes")
    lm_info: dict = {"enabled": lm_enabled}
    if lm_enabled:
        from genesis.env import lm_studio_health_url

        lm_url = lm_studio_health_url()
        lm_info["url"] = lm_url
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(lm_url)
                resp.raise_for_status()
                lm_info["status"] = "online"
                lm_info["models"] = resp.json().get("data", [])
        except httpx.ConnectError:
            lm_info["status"] = "unreachable"
            lm_info["models"] = []
        except Exception:
            lm_info["status"] = "error"
            lm_info["models"] = []
    section["lmstudio"] = lm_info

    return section
