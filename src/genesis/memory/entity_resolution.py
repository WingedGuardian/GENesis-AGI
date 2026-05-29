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

    Args:
        points: List of point dicts with ``id`` and ``payload`` keys
            (from ``_scroll_and_group``).
        vectors: Pre-fetched vectors keyed by point ID.
        threshold: Minimum cosine similarity to consider as candidate.
    """
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
