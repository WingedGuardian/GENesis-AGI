"""infrastructure_profile MCP tool — the machine's body schema, on demand.

Read view of ~/.genesis/infrastructure/ plus an optional facts-only refresh.

Cross-process contract (architect review 2026-07-11): this MCP server is a
SEPARATE process from genesis-server, so a refresh here runs WITHOUT the
router (no annotation regeneration — annotations catch up at the server's
next refresh) and relies on the service's flock for write safety. The MCP
server's DB handle is passed through so drift observations still land.
"""

from __future__ import annotations

import logging

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _get_db():
    """Late-import DB from the health MCP module state."""
    import genesis.mcp.health_mcp as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return None
    return getattr(svc, "_db", None)


async def _impl_infrastructure_profile(
    refresh: bool = False,
    include_doc: bool = False,
) -> dict:
    from genesis.infra_profile import store
    from genesis.infra_profile.paths import DOC_PATH
    from genesis.infra_profile.render import headline_facts

    profile = None
    if refresh:
        from genesis.infra_profile.service import refresh as service_refresh

        try:
            # Use the returned profile — a re-load could race the daily job
            # and desync `headline` from `sections` (review 2026-07-12).
            profile = await service_refresh("mcp", db=_get_db(), router=None)
        except Exception:
            logger.warning("infrastructure_profile: refresh failed", exc_info=True)

    if profile is None:
        profile = store.load_profile()
    if not profile.get("sections"):
        return {
            "error": "no profile collected yet",
            "hint": "pass refresh=true, or wait for the boot/daily refresh",
        }

    annotations = store.load_annotations().get("sections", {})
    sections = {}
    for name, section in profile.get("sections", {}).items():
        annotation = annotations.get(name, {})
        sections[name] = {
            "plane": section.get("plane"),
            "status": section.get("status"),
            "hash": (section.get("hash") or "")[:12] or None,
            "facts_changed_at": section.get("facts_changed_at"),
            "annotated": bool(annotation.get("annotation")),
            "annotation_stale": bool(annotation)
            and annotation.get("source_hash") != section.get("hash"),
        }

    result: dict = {
        "collected_at": profile.get("collected_at"),
        "refresh_reason": profile.get("refresh_reason"),
        "planes": profile.get("planes"),
        "headline": headline_facts(profile),
        "sections": sections,
        "doc_path": str(DOC_PATH),
    }
    if include_doc:
        try:
            result["doc"] = DOC_PATH.read_text()
        except OSError as exc:
            result["doc_error"] = str(exc)
    return result


@mcp.tool()
async def infrastructure_profile(
    refresh: bool = False,
    include_doc: bool = False,
) -> dict:
    """Infrastructure body schema — what machine Genesis runs on.

    Per-section status/hash/annotation state, headline facts (memory limit,
    CPU, kernel, storage headroom, SQLite mode), and the path to the full
    INFRASTRUCTURE.md. Consult before any infrastructure-adjacent work.

    Args:
        refresh: re-collect facts now (facts + drift only — annotations
            regenerate at the server's next refresh; rate-limited, ~15s)
        include_doc: inline the full rendered INFRASTRUCTURE.md
    """
    return await _impl_infrastructure_profile(
        refresh=refresh,
        include_doc=include_doc,
    )
