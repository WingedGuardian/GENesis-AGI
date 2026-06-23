"""Entity resolution for Genesis memory system.

Three capabilities:
1. **Surface form normalization** — alias expansion at ingestion time
2. **Dedup candidate discovery** — find near-duplicate memories via Qdrant
3. **Semantic overlap checking** — LLM classification of candidate pairs
4. **Audit logging** — every resolution action logged for post-hoc review

Used by the dream cycle entity resolution phase and the store pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import QdrantClient

    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

DEDUP_THRESHOLD: float = 0.92
AUTO_MERGE_THRESHOLD: float = 0.95
LLM_CHECK_FLOOR: float = 0.85
MAX_ENTITY_CHECKS_PER_RUN: int = 50
CALL_SITE_ID: str = "dream_cycle_entity_check"

# Evidence gate for auto-merge (spec ③). A ≥0.95-cosine pair auto-merges only
# when its multi-signal evidence strength clears this floor; below it, the pair
# is flagged for review instead of being silently deprecated. Calibrated
# against 1727 historical auto-merges: at 0.30 the gate blocks ~5% of merges,
# entirely within the 0.95–0.96 suspect band, and never blocks a ≥0.98
# near-identical pair (see PR for the calibration replay).
EVIDENCE_THRESHOLD: float = 0.30

_ALIAS_PATH = Path(
    os.environ.get(
        "GENESIS_ENTITY_ALIASES",
        os.path.expanduser("~/.genesis/config/entity_aliases.yaml"),
    )
)

_SEED_ALIASES = """\
# Entity alias dictionary for surface form normalization.
# Canonical form on the right, aliases on the left.
# Dream cycle auto-appends discovered aliases to the 'discovered' section.
aliases:
  "CC": "Claude Code"
  "claude-code": "Claude Code"
discovered: {}
"""

# ── Surface Form Normalization ───────────────────────────────────────────

_alias_cache: dict[str, str] | None = None
_alias_cache_mtime: float = 0.0


def load_aliases() -> dict[str, str]:
    """Load alias dictionary from YAML, cached with mtime check.

    Creates a seed file on first call if none exists. Returns empty dict
    on any error (normalization is best-effort).
    """
    global _alias_cache, _alias_cache_mtime

    if not _ALIAS_PATH.exists():
        try:
            _ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _ALIAS_PATH.write_text(_SEED_ALIASES)
            logger.info("Created seed entity aliases at %s", _ALIAS_PATH)
        except OSError:
            logger.debug("Cannot create alias file", exc_info=True)
            return {}

    try:
        mtime = _ALIAS_PATH.stat().st_mtime
    except OSError:
        return _alias_cache or {}

    if _alias_cache is not None and mtime == _alias_cache_mtime:
        return _alias_cache

    try:
        import yaml

        data = yaml.safe_load(_ALIAS_PATH.read_text()) or {}
        aliases: dict[str, str] = {}
        for section in ("aliases", "discovered"):
            section_data = data.get(section)
            if isinstance(section_data, dict):
                aliases.update(
                    {str(k): str(v) for k, v in section_data.items()}
                )
        _alias_cache = aliases
        _alias_cache_mtime = mtime
        return aliases
    except Exception:
        logger.debug("Failed to load entity aliases", exc_info=True)
        return _alias_cache or {}


def normalize_content(content: str, aliases: dict[str, str] | None = None) -> str:
    """Replace known surface forms with canonical names.

    Case-insensitive, whole-word matching. Returns content unchanged if
    no aliases loaded or no matches found.
    """
    if aliases is None:
        aliases = load_aliases()
    if not aliases:
        return content

    import re

    for alias, canonical in aliases.items():
        if alias == canonical:
            continue
        # Word-boundary replacement, case-insensitive
        pattern = re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)
        content = pattern.sub(canonical, content)
    return content


# ── Dedup Candidate Discovery ────────────────────────────────────────────


async def find_dedup_candidates(
    qdrant: QdrantClient,
    points: list[dict],
    vectors: dict[str, list[float]],
    *,
    threshold: float = DEDUP_THRESHOLD,
    max_candidates_per_point: int = 5,
    collection: str = "episodic_memory",
) -> list[tuple[dict, dict, float]]:
    """Find near-duplicate pairs using Qdrant similarity search.

    Returns list of ``(point_a, point_b, cosine_score)`` tuples.
    Deduplicates pairs so ``(A, B)`` and ``(B, A)`` only appear once.

    The inner loop is synchronous Qdrant I/O and runs via
    ``asyncio.to_thread`` to avoid blocking the event loop.
    """
    import asyncio

    return await asyncio.to_thread(
        _find_dedup_candidates_sync,
        qdrant, points, vectors,
        threshold=threshold,
        max_candidates_per_point=max_candidates_per_point,
        collection=collection,
    )


def _find_dedup_candidates_sync(
    qdrant: QdrantClient,
    points: list[dict],
    vectors: dict[str, list[float]],
    *,
    threshold: float,
    max_candidates_per_point: int,
    collection: str,
) -> list[tuple[dict, dict, float]]:
    """Synchronous inner loop for dedup candidate discovery."""
    from genesis.qdrant import collections as qdrant_ops

    seen_pairs: set[tuple[str, str]] = set()
    candidates: list[tuple[dict, dict, float]] = []

    for point in points:
        pid = point["id"]
        vec = vectors.get(pid)
        if vec is None:
            continue

        hits = qdrant_ops.search(
            qdrant,
            collection=collection,
            query_vector=vec,
            limit=max_candidates_per_point + 1,  # +1 for self-match
        )

        for hit in hits:
            hid = hit["id"]
            if hid == pid:
                continue  # self-match
            if hit["score"] < threshold:
                continue

            pair_key = tuple(sorted((pid, hid)))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            # Build point_b from hit data
            point_b = {"id": hid, "payload": hit.get("payload", {})}
            candidates.append((point, point_b, hit["score"]))

    return candidates


# ── Evidence Gate (spec ③) ───────────────────────────────────────────────


def _parse_created_at(payload: dict) -> datetime | None:
    """Parse a payload's ``created_at`` ISO timestamp; None on absence/garbage."""
    ts = payload.get("created_at")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def compute_evidence_strength(
    payload_a: dict,
    payload_b: dict,
    cosine: float,
) -> tuple[float, dict[str, Any]]:
    """Multi-signal evidence score in ``[0, 1]`` for an auto-merge candidate.

    Higher = safer to auto-merge. The auto-merge gate flags (not merges) a
    pair whose strength is below :data:`EVIDENCE_THRESHOLD`.

    Coincident-weakness design (calibrated against 1727 historical merges):
    cosine is the dominant term so near-identical text keeps a strong floor and
    always clears the gate regardless of the other signals. Temporal closeness
    and confidence are *capped* modifiers — neither can block on its own; only
    coincident weakness (e.g. floor cosine AND far apart) drops below the gate.
    The load-bearing factor is an intentional bar-raiser (a heavily-retrieved
    memory needs more corroboration before deprecation), not a "weakness".
    Absent payload fields resolve to neutral and never block on their own (so
    sparse payloads keep merging as before).

    Returns ``(strength, signals)`` where ``signals`` is an audit-friendly
    breakdown (cosine, temporal distance in days, mean confidence, max
    retrieved_count).
    """
    def clamp01(x: float) -> float:
        return max(0.0, min(1.0, x))

    conf_a = payload_a.get("confidence")
    conf_b = payload_b.get("confidence")
    conf_a = 0.5 if conf_a is None else conf_a
    conf_b = 0.5 if conf_b is None else conf_b
    conf_mean = (conf_a + conf_b) / 2.0

    rc_max = max(payload_a.get("retrieved_count") or 0,
                 payload_b.get("retrieved_count") or 0)

    dt_a = _parse_created_at(payload_a)
    dt_b = _parse_created_at(payload_b)
    if dt_a is not None and dt_b is not None:
        dt_days: float | None = abs((dt_a - dt_b).total_seconds()) / 86400.0
        s_temporal = clamp01(1.0 - dt_days / 30.0)  # 0d→1, 30d→0
    else:
        dt_days = None
        s_temporal = 0.7  # unknown timestamps → mildly supportive (neutral)

    s_cos = clamp01((cosine - 0.95) / 0.045)   # 0.95→0, 0.995→1 (dominant)
    s_conf = clamp01((conf_mean - 0.5) / 0.4)  # 0.5(default)→0, 0.9→1
    s_load = 1.0 if rc_max < 5 else 0.5        # heavily-retrieved → raise the bar

    strength = clamp01(
        0.45 * s_cos + 0.25 * s_temporal + 0.15 * s_conf + 0.15 * s_load
    )
    signals = {
        "cosine": round(cosine, 4),
        "dt_days": round(dt_days, 2) if dt_days is not None else None,
        "conf_mean": round(conf_mean, 3),
        "retrieved_count_max": rc_max,
    }
    return strength, signals


def pick_duplicate_survivor(
    id_a: str,
    payload_a: dict,
    dt_a: datetime,
    id_b: str,
    payload_b: dict,
    dt_b: datetime,
) -> tuple[str, str]:
    """Pick ``(survivor_id, deprecated_id)`` for a CONFIRMED-DUPLICATE pair.

    Prefers the more-retrieved (load-bearing) memory as survivor even when it is
    the older one — duplicates carry ~identical content, so we keep the
    established memory rather than deprecating one that is actively used. Ties
    break to the newer memory (prior behavior).

    For duplicate paths only (auto_merge / llm_merge). The contradiction /
    succeeded_by path keeps temporal survivorship (newer supersedes older) and
    must NOT be routed through here.
    """
    rc_a = payload_a.get("retrieved_count") or 0
    rc_b = payload_b.get("retrieved_count") or 0
    if rc_a > rc_b:
        return id_a, id_b
    if rc_b > rc_a:
        return id_b, id_a
    return (id_a, id_b) if dt_a >= dt_b else (id_b, id_a)


# ── Semantic Overlap Checker ─────────────────────────────────────────────

_OVERLAP_PROMPT = """\
Compare these two memories. Respond with JSON only, no other text:
{{"relationship": "duplicate|contradicts|distinct", "reasoning": "one sentence"}}

- "duplicate": same information, possibly reworded
- "contradicts": same topic but conflicting claims (different numbers, opposite conclusions)
- "distinct": related but genuinely different information

Memory A:
{content_a}

Memory B:
{content_b}"""


async def check_semantic_overlap(
    router: Router,
    content_a: str,
    content_b: str,
) -> dict[str, Any]:
    """Quick LLM check for semantic overlap or contradiction.

    Returns ``{"relationship": str, "reasoning": str}`` or a fallback
    dict on error.
    """
    prompt = _OVERLAP_PROMPT.format(
        content_a=content_a[:1500],
        content_b=content_b[:1500],
    )
    try:
        result = await router.route_call(
            CALL_SITE_ID,
            [{"role": "user", "content": prompt}],
            suppress_dead_letter=True,
        )
        if not result.success:
            logger.warning("Entity check LLM call failed: %s", result.error)
            return {"relationship": "distinct", "reasoning": f"LLM error: {result.error}"}

        text = (result.content or "").strip()
        # Strip markdown fence if present
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        rel = data.get("relationship", "distinct")
        if rel not in ("duplicate", "contradicts", "distinct"):
            rel = "distinct"
        return {"relationship": rel, "reasoning": data.get("reasoning", "")}
    except (json.JSONDecodeError, Exception):
        logger.debug("Entity check parse/call error", exc_info=True)
        return {"relationship": "distinct", "reasoning": "parse error — defaulting to distinct"}


# ── Audit Logger ─────────────────────────────────────────────────────────


async def log_resolution(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    action: str,
    memory_id_a: str,
    memory_id_b: str,
    content_a: str | None = None,
    content_b: str | None = None,
    cosine_score: float | None = None,
    llm_verdict: str | None = None,
    llm_reasoning: str | None = None,
    survivor_id: str | None = None,
) -> None:
    """Write an entity resolution action to the audit trail.

    Fire-and-forget — errors are logged but never propagate.
    """
    try:
        await db.execute(
            """INSERT INTO entity_resolution_audit
               (run_id, action, memory_id_a, memory_id_b, content_a, content_b,
                cosine_score, llm_verdict, llm_reasoning, survivor_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                action,
                memory_id_a,
                memory_id_b,
                (content_a or "")[:5000],
                (content_b or "")[:5000],
                cosine_score,
                llm_verdict,
                llm_reasoning,
                survivor_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        await db.commit()
    except Exception:
        logger.debug("Failed to log entity resolution action", exc_info=True)
