"""Entity resolution for Genesis memory system.

Three capabilities:
1. **Surface form normalization** — alias expansion at ingestion time
2. **Dedup candidate discovery** — find near-duplicate memories via Qdrant
3. **Semantic overlap checking** — LLM classification of candidate pairs
4. **Audit logging** — every resolution action logged for post-hoc review

Used by the dream cycle entity resolution phase and the store pipeline.

NAMING NOTE: "entity" here means a near-duplicate MEMORY PAIR, not a
typed entity node — those live in the entity layer (``entity_registry``
/ ``db/crud/entities.py``, WS-H Pillar 2), which uses only
``normalize_content`` from this module.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
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
    """Replace known surface forms with canonical names, to a fixed point.

    Case-insensitive, whole-word matching. Returns content unchanged if
    no aliases loaded or no matches found.

    Rules apply in mapping order AND the pass repeats until a pass changes
    nothing: chained mappings (``foo -> bar``, ``bar -> baz``) converge to
    ``baz`` regardless of order, so no intermediate canonical can ever be
    stored. That convergence is what dedup relies on — an intermediate form
    like ``"bar item"`` persisting in the index would be a different content
    than the same spelling written later, and a text-equal match between the
    two silently suppresses the newer memory. Termination is guaranteed: a
    pass that produces an already-seen value (a mapping cycle) or exceeds
    ``len(aliases)`` passes stops and returns the last value.
    """
    if aliases is None:
        aliases = load_aliases()
    if not aliases:
        return content

    seen = {content}
    for _ in range(len(aliases)):
        for alias, canonical in aliases.items():
            if alias == canonical:
                continue
            # Word-boundary replacement, case-insensitive
            pattern = _alias_pattern(alias)
            if not pattern.search(canonical):
                content = pattern.sub(canonical, content)
                continue
            # Self-expanding rule ("AI" -> "AI assistant"): the replacement
            # re-supplies the alias as a whole word, so an unguarded pass
            # grows forever and idempotency — which dedup relies on — is
            # lost. Apply it only to alias occurrences NOT already covered
            # by a canonical occurrence: "AI" still converges to
            # "AI assistant", and "AI assistant" is already its own fixed
            # point, so both surface forms dedup to one memory.
            spans = [
                (m.start(), m.end())
                for m in _alias_pattern(canonical).finditer(content)
            ]
            out: list[str] = []
            cursor = 0
            for m in pattern.finditer(content):
                if any(
                    m.start() >= s and m.end() <= e for s, e in spans
                ):
                    continue
                out.append(content[cursor:m.start()])
                out.append(canonical)
                cursor = m.end()
            out.append(content[cursor:])
            content = "".join(out)
        if content in seen:
            break
        seen.add(content)
    return content


@functools.lru_cache(maxsize=8192)
def _alias_pattern(alias: str) -> re.Pattern[str]:
    """The compiled whole-word pattern for *alias*, compiled once.

    ``surface_variants`` calls ``normalize_content`` once per alias, and each
    call walks every alias, so recompiling per call is quadratic in regex
    compiles; past ``re``'s own 512-entry cache that measured 34 s for 750
    aliases on one canonical. The key is the alias text alone, so an edited
    alias file needs no invalidation: a new alias is a new key. ``maxsize``
    bounds the win: an alias file larger than it cycles the cache (every
    ``normalize_content`` walks aliases in the same order) and the quadratic
    cost returns. The live file is far below it."""
    return re.compile(r"\b" + re.escape(alias) + r"\b", re.IGNORECASE)


def _alias_version(aliases: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Hashable snapshot of an alias mapping, used to memoize derived maps.

    Order is preserved — rules apply in mapping order, so a reordering is a
    different normalization, not a reordering of the same key."""
    return tuple(aliases.items())


@functools.lru_cache(maxsize=8)
def _inverse_maps(
    alias_version: tuple[tuple[str, str], ...],
) -> tuple[dict[str, str], dict[str, str]]:
    """(forward, fixed) normalization maps for an alias file, built once.

    forward[a] = fixed point of alias ``a``; fixed[c] = fixed point of
    canonical ``c``. Building them is quadratic — every alias normalized
    against the whole dictionary — so they are cached by the file's content
    version: an edited alias file yields a new key and recomputes, while the
    steady state shares one build across every store's dedup pass."""
    aliases = dict(alias_version)
    forward = {
        a: normalize_content(a, aliases) for a in dict.fromkeys(aliases)
    }
    fixed = {
        c: normalize_content(c, aliases)
        for c in dict.fromkeys(aliases.values())
    }
    return forward, fixed


def surface_variants(
    content: str,
    aliases: dict[str, str] | None = None,
    *,
    limit: int = 64,
) -> list[str]:
    r"""Return the raw spellings *content* could have been stored under.

    The inverse of :func:`normalize_content`: for each alias whose canonical
    form appears in *content* (same whole-word, case-insensitive matching),
    produce the spelling with the alias substituted back in. A row written
    before an alias existed — or while normalization was failing — holds the
    raw surface form, and the shipped seed maps BOTH ``"CC"`` and
    ``"claude-code"`` to ``"Claude Code"``, so the current write's own raw
    spelling is not the only one a legacy row can carry.

    Each canonical occurrence is a SLOT, enumerated independently — a legacy
    row can mix spellings (``"CC reviews claude-code"``). A slot's candidate
    spellings are the aliases whose own normalization converges to the same
    fixed point as that canonical: ``normalize_content`` decides, so no
    spelling is offered that the write path could not itself produce. Because
    a canonical can itself be an alias (``CC -> Claude Code`` plus
    ``Claude -> Anthropic`` leaves nothing holding ``Claude Code``), the
    enumeration recurses breadth-first over the variants it emits — each hop
    walks one chain link back, so a row spelled at any depth is reached.
    Boundaries are ``(?<!\w)``/``(?!\w)`` lookarounds, not ``\b``, so
    a canonical starting or ending with punctuation (``"C++"``) still matches.

    Homogeneous forms (every slot carrying the same alias) are emitted FIRST,
    before the mixed enumeration, so the common legacy shape cannot be priced
    out of *limit* by intermediate combinations. Every candidate is verified
    by re-running ``normalize_content`` on it — the enumeration is only ever
    as precise as the inverse, and the check keeps it honest. The check is
    EXACT: normalization writes the canonical as spelled in the alias file, so
    a candidate that only normalizes to a different casing of *content* is a
    spelling the write path could never have stored under this content. A
    casefolded check accepted them, and with a short canonical that merges
    unrelated rows (``"US"`` makes ``"Talk to USA tomorrow"`` a variant of
    ``"Talk to us tomorrow"``).
    Capped at *limit* (each result costs one indexed equality lookup in the
    dedup path); variants are emitted with fewest substitutions first — every
    single-substitution form precedes any pair, and so on — so the cap cuts
    the least plausible spellings first. Best-effort like
    ``normalize_content`` — ``[]`` on any failure.
    """
    if aliases is None:
        aliases = load_aliases()
    if not aliases:
        return []

    import re
    from itertools import combinations, islice, product

    def _bounded(term: str) -> re.Pattern:
        return re.compile(
            r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE
        )

    results: list[str] = []
    seen = {content}

    def _emit(text: str) -> None:
        if len(results) >= limit or text in seen:
            return
        if normalize_content(text, aliases) != content:
            return  # not a spelling this normalization could have produced
        seen.add(text)
        results.append(text)

    canonicals = list(dict.fromkeys(aliases.values()))
    forward: dict[str, str] | None = None
    fixed: dict[str, str] | None = None

    _CANDIDATE_BUDGET = 512
    _SET_BUDGET = 64
    _SCAN_BUDGET = 8192

    def _expand(text: str) -> None:
        """Emit every single-hop inverse spelling of *text*: substitutes a
        whole-word canonical occurrence with an alias whose own normalization
        reaches the same fixed point as that canonical."""
        nonlocal forward, fixed
        # Canonical scan BEFORE the inverse maps: unrelated text — the common
        # case — must not pay the quadratic forward/fixed construction at all.
        present: list[str] = []
        positions: list[tuple[int, int, str]] = []
        for canonical in canonicals:
            matches = list(_bounded(canonical).finditer(text))
            if not matches:
                continue
            present.append(canonical)
            positions.extend(
                (m.start(), m.end(), canonical) for m in matches
            )
        if not present:
            return
        if forward is None:
            forward, fixed = _inverse_maps(_alias_version(aliases))

        # A spelling reaches a slot when its normalization CONVERGES to the
        # same fixed point as the canonical — `normalize(a) == fixed(c)`, not
        # `== c`. A canonical that is itself an alias ("Claude Code" under a
        # "Claude" -> "Anthropic" rule) never survives normalization, so it
        # cannot appear in *text*; its spellings ("CC") only surface when a
        # LATER hop exposes them on an emitted intermediate like "Claude Code".
        spellings: dict[str, list[str]] = {
            canonical: [
                alias
                for alias, target in forward.items()
                if alias != canonical and target == fixed[canonical]
            ]
            for canonical in present
        }
        positions.sort(key=lambda p: (p[0], -p[1]))

        for canonical, names in spellings.items():
            if not names:
                continue
            pattern = _bounded(canonical)
            for alias in names:
                _emit(pattern.sub(alias, text))

        def _enumerate(
            slot_list: list[tuple[int, int, str]],
        ) -> None:
            """Product over one mutually-disjoint slot set; every slot takes
            an alias (an unset slot is a subset that doesn't contain it)."""
            choice_lists = [spellings[c] for (_, _, c) in slot_list]
            for picks in islice(product(*choice_lists), _CANDIDATE_BUDGET):
                out: list[str] = []
                cursor = 0
                for (start, end, _c), pick in zip(
                    slot_list, picks, strict=True
                ):
                    out.append(text[cursor:start])
                    out.append(pick)
                    cursor = end
                out.append(text[cursor:])
                _emit("".join(out))
                if len(results) >= limit:
                    return

        # Overlapping canonicals ({"X": "Claude", "CC": "Claude Code"}, or
        # {"WHOLE": "Alpha Beta Gamma", "A": "Alpha", "G": "Gamma"}) cannot be
        # reduced to one kept set: a legacy row can substitute disjoint
        # dropped spans together ("A Beta G") or a shorter span inside a
        # longer one ("X Code"). Enumerate mutually-disjoint subsets of
        # matching spans by ASCENDING SIZE: a mixed legacy row differs from
        # the canonical form at a few slots, so the sets most likely to exist
        # — one or two substitutions anywhere in the text — must be
        # enumerated before any large set. An exclude-first DFS starves
        # exactly those: with enough occurrences its first _SET_BUDGET sets
        # all omit the earliest slot, so a stored "CC / claude-code / Claude
        # Code / …" is never tried. Bounded by _SET_BUDGET sets and
        # _SCAN_BUDGET combo inspections (overlapping spans can make most
        # combinations non-disjoint), correctness by _emit's
        # normalize_content check.
        span_slots = [p for p in positions if spellings.get(p[2])]
        span_slots.sort(key=lambda p: (p[0], p[1]))
        sets_enumerated = 0
        scans = 0
        for size in range(1, len(span_slots) + 1):
            if sets_enumerated >= _SET_BUDGET or len(results) >= limit:
                break
            for combo in combinations(span_slots, size):
                scans += 1
                if scans > _SCAN_BUDGET:
                    break
                covered_end = -1
                for start, end, _c in combo:
                    if start < covered_end:
                        break
                    covered_end = end
                else:
                    sets_enumerated += 1
                    _enumerate(list(combo))
                if sets_enumerated >= _SET_BUDGET or len(results) >= limit:
                    break
            else:
                continue
            break

    # The inverse of a fixed-point map is transitive: a legacy row can hold a
    # spelling several chain links back ("CC owns" under {"CC": "Claude
    # Code", "Claude": "Anthropic"} normalizes to "Anthropic Code owns" but no
    # single substitution of a canonical in that text produces it — it takes
    # "Anthropic" -> "Claude" exposing "Claude Code", then "Claude Code" ->
    # "CC"). Breadth-first over emitted variants, depth-bounded by the longest
    # possible chain — one alias consumed per hop.
    queue: list[tuple[str, int]] = [(content, 0)]
    while queue and len(results) < limit:
        text, depth = queue.pop(0)
        if depth > len(aliases):
            continue
        before = len(results)
        _expand(text)
        for variant in results[before:]:
            queue.append((variant, depth + 1))

    return results


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
