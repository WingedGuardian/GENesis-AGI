"""Reference store browser routes — list, search, detail, reveal, delete, stats.

Live, access-controlled view of the reference store (``project_type='reference'``
rows in ``knowledge_units``) — the replacement for the retired
``known-to-genesis.md`` flat-file mirror. Single source of truth, rendered
behind auth, with an explicit reveal step for secret values.

Security model:
- Every route is gated with ``is_authenticated()`` (a NO-OP when
  ``DASHBOARD_PASSWORD`` is unset, enforced 403 when it's set). This is a
  deliberate, narrow exception to the dashboard's "API routes bypass auth"
  default (``auth.py``) — these routes serve the human UI and the reveal
  endpoint returns plaintext credentials, so they get the same protection as
  the login-gated web pages.
- list / search / detail return ONLY the parsed description (never the raw
  body, never the value). The value is reachable only via the explicit
  ``/reveal`` endpoint. Parsing is fail-closed (see ``parse_reference_body``).
"""

from __future__ import annotations

import json
import logging

from flask import jsonify, request

from genesis.dashboard._blueprint import _async_route, blueprint
from genesis.dashboard.auth import is_authenticated
from genesis.memory.reference_ops import (
    REFERENCE_KINDS,
    REFERENCE_PROJECT,
    delete_reference_entry,
    parse_reference_body,
)

logger = logging.getLogger(__name__)

_DOMAIN_PREFIX = "reference."

# Kinds whose value is a genuine secret (a password/token). For these, if the
# value string also appears in the free-text description, mask it there too so
# the secret never shows in a non-reveal payload — closing the gap the old
# markdown mirror had (it only redacted the structured ``Value:`` line). Other
# kinds (network IPs, URLs, account handles) are identifiers, not secrets, and
# naturally recur in their own descriptions, so they're left readable.
_SENSITIVE_KINDS = frozenset({"credentials"})
_MASK = "[hidden — reveal to view]"


def _auth_or_403():
    """Return a 403 response tuple if not authenticated, else None."""
    if not is_authenticated():
        return jsonify({"error": "authentication required"}), 403
    return None


def _kind_of(domain: str | None) -> str:
    """Strip the ``reference.`` prefix from a domain to get the kind."""
    if domain and domain.startswith(_DOMAIN_PREFIX):
        return domain[len(_DOMAIN_PREFIX):]
    return domain or ""


def _parse_tags(tags) -> list[str]:
    """Normalize the stored tags column (JSON array string) into a list."""
    if not tags:
        return []
    if isinstance(tags, list):
        return tags
    try:
        parsed = json.loads(tags)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _summary(row: dict) -> dict:
    """Build a value-free summary dict for list/search/detail views.

    parse_reference_body excludes the structured value; for sensitive kinds we
    additionally mask any occurrence of the value inside the free-text
    description, so a secret never appears in a non-reveal payload.
    """
    kind = _kind_of(row.get("domain"))
    parsed = parse_reference_body(row.get("body"))
    description = parsed.get("description", "")
    value = parsed.get("value", "")
    if kind in _SENSITIVE_KINDS and value and value in description:
        description = description.replace(value, _MASK)
    return {
        "id": row.get("id") or row.get("unit_id"),
        "concept": row.get("concept"),
        "kind": kind,
        "tags": _parse_tags(row.get("tags")),
        "ingested_at": row.get("ingested_at"),
        "source_pipeline": row.get("source_pipeline"),
        "confidence": row.get("confidence"),
        "description": description,
    }


@blueprint.route("/api/genesis/references/list")
@_async_route
async def references_list():
    """All reference entries grouped by kind. Never returns values."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        by_domain = await knowledge.list_by_domain(
            rt.db, project_type=REFERENCE_PROJECT,
        )
        by_kind: dict[str, list[dict]] = {}
        total = 0
        for domain, rows in by_domain.items():
            kind = _kind_of(domain)
            by_kind[kind] = [_summary(r) for r in rows]
            total += len(rows)
        return jsonify({"by_kind": by_kind, "total": total})
    except Exception:
        logger.exception("References list failed")
        return jsonify({"error": "Failed to list references"}), 500


@blueprint.route("/api/genesis/references/search")
@_async_route
async def references_search():
    """FTS search scoped to references, optional kind filter. No values."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q parameter required"}), 400
    kind = request.args.get("kind") or None
    domain = f"{_DOMAIN_PREFIX}{kind}" if kind else None
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))

    try:
        rows = await knowledge.search_fts(
            rt.db, query, project=REFERENCE_PROJECT, domain=domain, limit=limit,
        )
        results = [_summary(r) for r in rows]
        return jsonify({"results": results, "query": query, "count": len(results)})
    except Exception:
        logger.exception("References search failed")
        return jsonify({"error": "Search failed"}), 500


@blueprint.route("/api/genesis/references/stats")
@_async_route
async def references_stats():
    """Reference store counts: total + by domain + by source_pipeline."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        return jsonify(await knowledge.stats(rt.db, project=REFERENCE_PROJECT))
    except Exception:
        logger.exception("References stats failed")
        return jsonify({"error": "Failed to fetch stats"}), 500


@blueprint.route("/api/genesis/references/kinds")
@_async_route
async def references_kinds():
    """The valid reference kinds (for the filter dropdown)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    return jsonify({"kinds": sorted(REFERENCE_KINDS)})


@blueprint.route("/api/genesis/references/<unit_id>")
@_async_route
async def references_detail(unit_id: str):
    """Single reference entry — metadata + description, NO value."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        row = await knowledge.get(rt.db, unit_id)
        if row is None or row.get("project_type") != REFERENCE_PROJECT:
            return jsonify({"error": "Reference not found"}), 404
        return jsonify({"reference": _summary(row)})
    except Exception:
        logger.exception("Reference detail failed")
        return jsonify({"error": "Failed to fetch reference"}), 500


@blueprint.route("/api/genesis/references/<unit_id>/reveal", methods=["POST"])
@_async_route
async def references_reveal(unit_id: str):
    """Return the secret value for one reference. Auth-gated, no audit (single-user)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.db.crud import knowledge
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    try:
        row = await knowledge.get(rt.db, unit_id)
        if row is None or row.get("project_type") != REFERENCE_PROJECT:
            return jsonify({"error": "Reference not found"}), 404
        value = parse_reference_body(row.get("body")).get("value", "")
        return jsonify({"id": unit_id, "value": value})
    except Exception:
        logger.exception("Reference reveal failed")
        return jsonify({"error": "Reveal failed"}), 500


@blueprint.route("/api/genesis/references/<unit_id>", methods=["DELETE"])
@_async_route
async def references_delete(unit_id: str):
    """Delete a reference: SQLite row + FTS + Qdrant point (both collections)."""
    if (resp := _auth_or_403()) is not None:
        return resp
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "Not bootstrapped"}), 503

    store = getattr(rt, "memory_store", None)
    try:
        deleted = await delete_reference_entry(rt.db, store, unit_id)
    except ValueError:
        # Not a reference entry — refuse.
        return jsonify({"error": "Not a reference entry"}), 400
    except Exception:
        logger.exception("Reference delete failed")
        return jsonify({"error": "Delete failed"}), 500

    if not deleted:
        return jsonify({"error": "Reference not found"}), 404
    return jsonify({"status": "ok", "deleted": True})
