"""Server-side proactive recall engine — the single-sited home for the
UserPromptSubmit injection context (and, later, voice W2 recall).

``scripts/proactive_memory_hook.py`` used to be a ~2,000-line hand-maintained
FORK of retrieval (FTS5 + Qdrant + RRF reimplemented in a subprocess), kept in
parity with ``genesis.memory.retrieval`` by comment discipline. It drifted:
the reranker (#1130), entity-lane probe (#1121), and 1-hop graph expansion
(#1069) all landed in the real engine and never in the fork.

This module ends the fork. The hook is now a thin HTTP client over
``/api/genesis/hook/recall`` → :func:`proactive_context`, which runs on the
warm in-server retriever via :func:`genesis.mcp.memory.core._proactive_impl`
(the SAME security pipeline the ``memory_proactive`` MCP tool uses — recall,
memory_operation filter, injection-defense wrap, gate-4 enforce-drop, graph
expansion, immunity emit). The presentation layer that only ever lived in the
hook — intent-aware result budget, KB slot cap, procedure surfacing, graph
breadcrumbs, the ``[Memory | age | wing | id]`` rendering, and the H-1
working-set shadow projection — lives here now, PROFILE-AWARE so a future
voice/s2s profile can share the engine and differ only in budget + renderer.

The hook keeps only session-local work (heartbeat, pivot trail, recent-activity
summary, working-set measurement, ambient fold) and a degraded FTS5-only
fallback for when the server is unreachable.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from genesis.memory import graph_expansion
from genesis.memory.intent import classify_intent, classify_stance

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — intent-aware budget + rerank posture, live-read from
# config/memory_recall.yaml ``proactive:`` (shares load_recall_config's
# mtime cache + .local overlay merge). Code defaults below keep a trimmed
# install (no yaml) working, per the generalizability gate.
# ---------------------------------------------------------------------------

# stance -> injected-memory count. ``max`` caps question_decision. A command
# ("restart the server") still surfaces the single best hit (cautionary recall
# — "last restart wedged the container"); chatter/greetings get the same floor.
_DEFAULT_BUDGETS: dict[str, int] = {
    "command": 1,
    "chatter": 1,
    "general": 3,  # status quo: the old fork's fixed _MAX_RESULTS
    "question_decision": 6,
    "max": 8,
}


def _proactive_config() -> dict[str, Any]:
    """The ``proactive`` section of the merged memory_recall config (live)."""
    try:
        cfg = graph_expansion.load_recall_config().get("proactive")
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        logger.debug("proactive config load failed — using defaults", exc_info=True)
        return {}


def proactive_enabled() -> bool:
    """Master on/off for the endpoint's engine — read live. Default on."""
    return bool(_proactive_config().get("enabled", True))


def _profile_config(profile: str) -> dict[str, Any]:
    profiles = _proactive_config().get("profiles")
    if isinstance(profiles, dict) and isinstance(profiles.get(profile), dict):
        return profiles[profile]
    return {}


def _budget_for(stance: str, profile: str) -> tuple[int, int]:
    """(count, max) for a stance under a profile — config over code defaults."""
    budgets = dict(_DEFAULT_BUDGETS)
    override = _profile_config(profile).get("budgets")
    if isinstance(override, dict):
        for key, val in override.items():
            if isinstance(val, int) and val >= 0:
                budgets[key] = val
    cap = budgets.get("max", _DEFAULT_BUDGETS["max"])
    count = budgets.get(stance, budgets.get("general", 3))
    return min(count, cap), cap


def _rerank_for(profile: str) -> bool:
    """Whether this profile reranks — config over code default (cc_hook: on).

    A ``rerank: off`` in config or a missing ``API_KEY_VOYAGE`` both degrade to
    no reranking downstream (the retriever's ``_maybe_rerank`` no-ops without a
    live reranker); this flag only decides whether we ASK for it.
    """
    val = _profile_config(profile).get("rerank")
    if isinstance(val, bool):  # YAML-1.1 unquoted on/off
        return val
    if isinstance(val, str):
        return val.strip().lower() not in {"off", "false", "no"}
    return _PROFILES[profile].rerank if profile in _PROFILES else True


# ---------------------------------------------------------------------------
# Rendering — profile-specific. cc_hook reproduces the old fork's one-line
# ``[Memory | age | wing | id]`` format. A voice profile (W2) would render one
# terse plain-English sentence and KEEP the injection-defense wrapper instead
# of stripping it — hence the seam.
# ---------------------------------------------------------------------------


def _format_age(iso_str: str | None) -> str:
    """ISO datetime → human age (``<1d``/``3d``/``2w``/``4mo``/``1y``)."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str)
        days = (datetime.now(UTC) - dt).days
        if days < 1:
            return "<1d"
        if days < 7:
            return f"{days}d"
        if days < 30:
            return f"{days // 7}w"
        if days < 365:
            return f"{days // 30}mo"
        return f"{days // 365}y"
    except (ValueError, TypeError):
        return "?"


def _clean_content(content: str, max_len: int) -> str:
    """Strip leaked injection-defense markers + extraction prefixes, then
    sentence-truncate — the cc_hook one-line contract (matches the old fork)."""
    from genesis.security.sanitizer import strip_boundary_markers

    content = strip_boundary_markers(content or "")
    # Drop extraction-pipeline prefixes ("[discovery] ", "[feature] ", ...)
    if content.startswith("[") and "] " in content[:30]:
        content = content[content.index("] ") + 2 :]
    if len(content) > max_len:
        for i in range(max_len - 1, max(max_len - 60, 0), -1):
            if content[i] in ".!?":
                return content[: i + 1]
        return content[:max_len]
    return content


def render_cc_hook(enriched: list[dict]) -> list[str]:
    """Render delivered memories as the CC hook's ``[Memory | ...]`` lines.

    ``enriched`` dicts carry the recall fields plus ``_created_at``/``_wing``
    (from metadata backfill) and ``related_ids`` (graph breadcrumbs). External
    content arrives wrapped by ``wrap_external_recall``; the one-line format
    can't carry a multi-line structural wrapper, so we strip it back and lean
    on the soft ``KB·source`` / ``Memory·external`` tier label instead — the
    old fork's deliberate CC-channel choice (the full-content MCP/expand paths
    keep the structural wrapper). A voice profile would keep the wrapper.
    """
    from genesis.memory.provenance import short_source

    lines: list[str] = []
    for rank, r in enumerate(enriched):
        is_rule = r.get("memory_class") == "rule"
        max_len = 300 if (rank == 0 or is_rule) else 200
        content = _clean_content(r.get("content", ""), max_len)

        mid = r.get("memory_id", "")
        age = _format_age(r.get("_created_at"))
        wing = r.get("_wing") or ""
        is_kb = r.get("collection") == "knowledge_base"

        if is_kb:
            parts = [f"KB·{short_source(r.get('source_pipeline'))}"]
        elif r.get("origin_class") == "external_untrusted":
            parts = ["Memory·external"]
        else:
            parts = ["Memory"]
        if age != "?" and not is_kb:
            parts.append(age)
        if wing and wing != "memory":
            parts.append(wing)
        if mid and not mid.startswith("code:"):
            parts.append(f"id:{mid[:8]}")

        tag = " | ".join(parts)
        related = r.get("related_ids")
        if related:
            tag += " | → " + ", ".join(f"id:{rid}" for rid in related)
        lines.append(f"[{tag}] {content}")

    if lines:
        lines.append(
            "Need more? Use `memory_recall` MCP (semantic search) "
            "or query `cc_sessions` in SQLite. Grep transcripts is last resort."
        )
    return lines


@dataclass(frozen=True)
class ProactiveProfile:
    """A channel's proactive-recall shape: budget curve + result renderer.

    Only ``cc_hook`` ships today. The voice/s2s profile (W2) plugs in here
    with a lower budget curve and a plain-English renderer that preserves the
    injection-defense wrapper — same engine, different presentation.
    """

    name: str
    rerank: bool
    renderer: Callable[[list[dict]], list[str]]
    # Budgets live in config (per-profile), not here — see _budget_for.


_PROFILES: dict[str, ProactiveProfile] = {
    "cc_hook": ProactiveProfile(
        name="cc_hook",
        rerank=True,  # user decision 2026-07-19 (config can flip via rerank: off)
        renderer=render_cc_hook,
    ),
}

DEFAULT_PROFILE = "cc_hook"


# ---------------------------------------------------------------------------
# Procedure surfacing — cosine of the query embedding against procedural
# principle embeddings. Tiered bar: 0.7 for PROVEN tiers, 0.78 for DORMANT
# drafts (stricter — unproven). Top-1 only. Ported from the old fork.
# ---------------------------------------------------------------------------

_PROCEDURE_SURFACE_THRESHOLD = 0.7
_DORMANT_SURFACE_THRESHOLD = 0.78

# TTL for the surfaceable-procedure cache. Procedure principle embeddings change
# rarely (novelty-gated writes), so a stale-by-≤300s cache on a soft top-1
# advisory is fine — and it keeps the ~300-row read + BLOB unpack + row
# normalization off the per-prompt hot path (they run once per TTL, not every
# call). The pure-Python O(n·d) cosine loop that dominated the old path
# (~98ms/call — DB read + unpack + scalar cosine over ~300×1024) becomes one
# cached matmul (~0.25ms). Single event loop → no lock needed (a concurrent
# rebuild just recomputes identical data, last writer wins).
_PROCEDURE_CACHE_TTL_S = 300.0


@dataclass(frozen=True)
class _ProcedureCache:
    """Pre-normalized principle-embedding matrix + row-aligned metadata."""

    matrix: Any  # (N, D) L2-normalized float64 ndarray (np.empty((0, 0)) when N=0)
    meta: list[tuple[str, str, str, str]]  # (id, task_type, principle, tier), row-aligned
    built_at: float  # time.monotonic() at build


_procedure_cache: _ProcedureCache | None = None


async def _load_procedure_cache(db: Any) -> _ProcedureCache | None:
    """Return the cached surfaceable-procedure matrix, rebuilding past the TTL.

    On a DB/read error the last good cache is served (returns None only when one
    was never built) — more resilient than the old per-call path, which surfaced
    nothing on a transient error. Staleness is normally ≤ TTL, but during a
    *sustained* read outage the last snapshot is served until the DB recovers
    (acceptable for a soft top-1 advisory; a down DB fails ``recall()`` upstream
    long before this matters). The cache is process-global and NOT keyed on
    ``db`` — correct because the runtime passes exactly one process-global
    connection (``memory_mod._db``) for its whole lifetime.
    """
    global _procedure_cache
    now = time.monotonic()
    cache = _procedure_cache
    if cache is not None and (now - cache.built_at) < _PROCEDURE_CACHE_TTL_S:
        return cache

    from genesis.learning.procedural.embedding import normalize_rows, unpack_embedding

    try:
        rows = await db.execute_fetchall(
            "SELECT id, task_type, principle, principle_embedding, activation_tier "
            "FROM procedural_memory "
            "WHERE deprecated = 0 AND quarantined = 0 "
            "AND principle_embedding IS NOT NULL "
            "ORDER BY confidence DESC LIMIT 1000"
        )
    except Exception:
        logger.debug("procedure cache reload failed — keeping prior cache", exc_info=True)
        return cache  # table absent / DB error — never block the endpoint

    import numpy as np

    vectors: list[list[float]] = []
    meta: list[tuple[str, str, str, str]] = []
    for row in rows:
        vec = unpack_embedding(row[3])
        if vec is None:
            continue
        vectors.append(vec)
        # Principle is truncated to the returned width here so the cache doesn't
        # pin full principle text for every row over the TTL (only [:200] is ever
        # surfaced). float64 matrix is deliberate — the scalar cosine_similarity
        # computes in Python float; a float32 matmul would diverge ~1e-6 and can
        # flip a near-threshold surface, so keep float64 for parity.
        meta.append((row[0], row[1] or "", (row[2] or "")[:200], row[4] or "DORMANT"))

    matrix = normalize_rows(np.asarray(vectors, dtype=np.float64)) if vectors else np.empty((0, 0))
    _procedure_cache = _ProcedureCache(matrix=matrix, meta=meta, built_at=now)
    return _procedure_cache


async def _surface_procedure(db: Any, vector: list[float]) -> dict | None:
    """Return ``{"id", "task_type", "principle", "tier"}`` for the best
    procedure clearing its tier-dependent cosine bar, else None.

    Vectorized against the TTL-cached, pre-normalized matrix (see
    :func:`_load_procedure_cache`) — parity with the old per-row scalar loop
    within floating-point tolerance (measured Δ ~1e-16 vs the scalar cosine):
    same confidence-DESC row order, same strict ``>`` tie-break (first/highest-
    confidence wins), same per-tier thresholds. The only conceivable divergence
    is a cosine sitting within ~machine-ε of a tier bar flipping surface/skip —
    measure-zero on continuous embeddings and immaterial for a soft advisory.
    """
    from genesis.learning.procedural.embedding import cosine_similarity_batch

    cache = await _load_procedure_cache(db)
    if cache is None or not cache.meta:
        return None

    sims = cosine_similarity_batch(cache.matrix, vector)

    best_idx = -1
    best_sim = -1.0
    for idx, (_pid, _task, _principle, tier) in enumerate(cache.meta):
        threshold = (
            _DORMANT_SURFACE_THRESHOLD if tier == "DORMANT" else _PROCEDURE_SURFACE_THRESHOLD
        )
        sim = float(sims[idx])
        if sim < threshold:
            continue
        if sim > best_sim:
            best_sim = sim
            best_idx = idx

    if best_idx < 0:
        return None
    proc_id, task_type, principle, tier = cache.meta[best_idx]
    return {"id": proc_id, "task_type": task_type, "principle": principle[:200], "tier": tier}


def _render_procedure_line(proc: dict) -> str:
    label = (
        "Procedure (unproven draft — suggestion, not authoritative)"
        if proc["tier"] == "DORMANT"
        else "Procedure"
    )
    task_type = proc.get("task_type") or ""
    pid = proc["id"][:8]
    tag = f"{label} | {task_type} | id:{pid}" if task_type else f"{label} | id:{pid}"
    return f"[{tag}] {proc['principle']}"


# ---------------------------------------------------------------------------
# Enrichment + breadcrumbs — async batch queries on the runtime db.
# ---------------------------------------------------------------------------


async def _enrich(db: Any, dicts: list[dict]) -> None:
    """Backfill ``_created_at`` + ``_wing`` from memory_metadata (in place)."""
    ids = [
        d["memory_id"]
        for d in dicts
        if d.get("memory_id") and not str(d["memory_id"]).startswith("code:")
    ]
    if not ids:
        return
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = await db.execute_fetchall(
            f"SELECT memory_id, created_at, wing FROM memory_metadata "  # noqa: S608
            f"WHERE memory_id IN ({placeholders})",
            ids,
        )
        meta = {row[0]: (row[1], row[2]) for row in rows}
        for d in dicts:
            created_at, wing = meta.get(d.get("memory_id"), (None, None))
            d["_created_at"] = created_at
            # Prefer a wing already on the recall payload; fall back to metadata.
            payload_wing = (d.get("payload") or {}).get("wing")
            d["_wing"] = payload_wing or wing
    except Exception:
        logger.debug("proactive metadata enrichment failed", exc_info=True)


async def _breadcrumbs(db: Any, dicts: list[dict]) -> None:
    """Attach up to 2 strongly-linked neighbor id-prefixes to the top 3
    delivered memories (``related_ids``), for memory_expand follow-up."""
    if not dicts:
        return
    seen = {d.get("memory_id") for d in dicts}
    try:
        for d in dicts[:3]:
            mid = d.get("memory_id")
            if not mid:
                continue
            rows = await db.execute_fetchall(
                "SELECT target_id FROM memory_links "
                "WHERE source_id = ? AND strength >= 0.5 "
                "ORDER BY strength DESC LIMIT 2",
                (mid,),
            )
            related = [row[0][:8] for row in rows if row[0] not in seen]
            if related:
                d["related_ids"] = related
    except Exception:
        logger.debug("proactive breadcrumbs failed", exc_info=True)


# ---------------------------------------------------------------------------
# H-1 working-set shadow projection (post-selection, degraded).
# ---------------------------------------------------------------------------

_SERENDIPITY_BOOST = 1.3  # documented for parity; not applied post-selection


def _shadow_projection(dicts: list[dict], suppress_ids: frozenset[str]) -> dict:
    """Project the PR2b novelty gate over the DELIVERED set (H-1 measurement).

    Post-selection (Option C): the endpoint reuses ``memory_proactive``'s
    delivered set, not the full scored candidate pool the old in-process fork
    saw, so the serendipity-boost promotion of never-seen items from deeper in
    the pool can't be measured — only suppression of already-surfaced ids and
    the never-surfaced count among the delivered. The PR2a window re-baselines
    at flip (user decision 2026-07-19), which this degraded projection matches.
    """
    delivered = [d.get("memory_id") for d in dicts if d.get("memory_id")]
    suppressed = sum(1 for mid in delivered if mid in suppress_ids)
    projected = [mid for mid in delivered if mid not in suppress_ids]
    serendipity = sum(
        1
        for d in dicts
        if d.get("collection") != "knowledge_base"
        and (d.get("payload") or {}).get("retrieved_count", -1) == 0
    )
    return {
        "projected_ids": projected,
        "projected_injected": len(projected),
        "suppressed": suppressed,
        "serendipity_boosted": serendipity,
    }


# ---------------------------------------------------------------------------
# The engine.
# ---------------------------------------------------------------------------


def _kind(d: dict) -> str:
    mid = str(d.get("memory_id", ""))
    if mid.startswith("code:"):
        return "code"
    if d.get("collection") == "knowledge_base":
        return "kb"
    return "memory"


def _result_row(d: dict) -> dict:
    """The per-result structured payload the hook's H-1 measurement consumes.

    ``retrieved_count`` is the recall payload's PRE-bump value (recall's own
    write-back runs after result assembly); a missing key (FTS-only hit) is
    left absent so the hook's ``_retrieved_count`` default (-1) excludes it
    from the never-surfaced stat, exactly as the old fork did.
    """
    payload = d.get("payload") or {}
    row = {
        "memory_id": d.get("memory_id"),
        "collection": d.get("collection"),
        "kind": _kind(d),
        "via_graph": bool(d.get("via_graph")),
        "score": d.get("score"),
        "origin_class": d.get("origin_class"),
        "source_pipeline": d.get("source_pipeline"),
    }
    if "retrieved_count" in payload:
        row["retrieved_count"] = payload["retrieved_count"]
    return row


def _apply_kb_cap(dicts: list[dict], kb_slots: int) -> list[dict]:
    """Keep every non-KB hit; cap knowledge_base hits at ``kb_slots`` (order-
    preserving) so KB can't flood episodic context under a wider budget."""
    out: list[dict] = []
    kb = 0
    for d in dicts:
        if d.get("collection") == "knowledge_base":
            kb += 1
            if kb > kb_slots:
                continue
        out.append(d)
    return out


async def proactive_context(
    *,
    prompt: str,
    session_id: str = "",
    profile: str = DEFAULT_PROFILE,
    file_keywords: list[str] | None = None,
    suppress_ids: frozenset[str] | list[str] | None = None,
) -> dict:
    """Build the proactive injection context for one prompt.

    Returns a JSON-serializable dict (see routes/proactive.py for the wire
    contract): ``lines`` (print-ready, server-owned), ``results`` (structured,
    for the hook's H-1 measurement), ``procedure``, ``shadow``, ``budget``,
    ``embedding`` (for the hook's ambient fold), ``timings_ms``, ``engine``.

    Never raises for content reasons — every enrichment stage is best-effort;
    a hard failure propagates so the route can 5xx and the hook falls back.
    """
    t0 = time.monotonic()
    if profile not in _PROFILES:
        profile = DEFAULT_PROFILE
    prof = _PROFILES[profile]
    suppress = frozenset(suppress_ids or ())

    stance = classify_stance(prompt)
    budget, cap = _budget_for(stance, profile)

    empty = {
        "status": "ok",
        "lines": [],
        "results": [],
        "procedure": None,
        "shadow": {},
        "budget": {"stance": stance, "limit": budget, "kb_slots": 0},
        "embedding": None,
        "timings_ms": {},
        "engine": {},
    }
    if budget <= 0:
        return empty

    from genesis.mcp.memory import core as _core

    memory_mod = _core._memory_mod()
    memory_mod._require_init()
    retriever = memory_mod._retriever
    db = memory_mod._db

    # Embed once (cache-shared with recall's internal embed of the same text →
    # a warm hit) — reused for procedure cosine + returned for the ambient fold.
    t_embed = time.monotonic()
    vector: list[float] | None = None
    try:
        vector, _available = await retriever._embed_query(prompt)
    except Exception:
        logger.debug("proactive embed failed — degrading", exc_info=True)
    embed_ms = (time.monotonic() - t_embed) * 1000

    # Rerank posture is read LIVE from config (proactive.profiles.<p>.rerank) so
    # the documented ``rerank: off`` latency/cost kill switch takes effect
    # without a restart — _PROFILES only supplies the default when config is
    # silent, and reranking still no-ops downstream without API_KEY_VOYAGE.
    rerank_live = _rerank_for(profile)

    # The shared security pipeline (recall + filters + wrap + enforce + graph
    # expansion + immunity emit + retrieved_count write-back).
    dicts = await _core._proactive_impl(
        prompt,
        limit=budget,
        rerank=rerank_live,
        extra_fts_terms=[k for k in (file_keywords or []) if k] or None,
    )

    kb_slots = max(1, budget // 3)
    dicts = _apply_kb_cap(dicts, kb_slots)

    await _enrich(db, dicts)
    await _breadcrumbs(db, dicts)

    lines = prof.renderer(dicts)

    procedure = None
    if vector:
        procedure = await _surface_procedure(db, vector)
        if procedure:
            lines.append(_render_procedure_line(procedure))

    shadow = _shadow_projection(dicts, suppress)
    results = [_result_row(d) for d in dicts]

    total_ms = (time.monotonic() - t0) * 1000
    return {
        "status": "ok",
        "lines": lines,
        "results": results,
        "procedure": ({"id": procedure["id"], "tier": procedure["tier"]} if procedure else None),
        "shadow": shadow,
        "budget": {"stance": stance, "limit": budget, "kb_slots": kb_slots, "cap": cap},
        "embedding": vector,
        "timings_ms": {"embed": round(embed_ms, 1), "total": round(total_ms, 1)},
        "engine": {
            "reranked": rerank_live,
            "graph_expansion": graph_expansion.expansion_mode(),
            "intent": classify_intent(prompt).category,
            "profile": profile,
        },
    }
