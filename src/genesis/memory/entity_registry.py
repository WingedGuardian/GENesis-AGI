"""Entity registry — string → entity-ID resolution with write-time tiering.

WS-H Pillar 2. Resolution never calls an LLM inline; ambiguity is
recorded (AMBIGUOUS provenance) and queued for dream-window adjudication
on the ``deferred_work_queue`` (``entity_adjudication`` rows, drained by
the entity_maintenance job).

Tiers:
1. Mechanical anchors (code_file/code_symbol/pr/commit) — norm_name is
   the literal identifier; exact match or create. Zero ambiguity.
2. Named exact — ``normalize_content(name).lower()`` match on
   (norm_name, entity_type); cross-type reuse inside the concept cluster.
3. Named fuzzy (difflib ≥ 0.85 against same-cluster norm_names) —
   create anyway, tag AMBIGUOUS, enqueue adjudication.
4. Else create, EXTRACTED.

Distinct from ``memory/entity_resolution.py`` (near-duplicate memory-pair
dedup) — that module contributes only ``normalize_content`` alias
rewriting here.
"""

from __future__ import annotations

import difflib

import aiosqlite

from genesis.db.crud import entities as entities_crud
from genesis.memory.entity_resolution import load_aliases, normalize_content

MECHANICAL_TYPES = frozenset({"code_file", "code_symbol", "pr", "commit"})

# Named types that may share identity across type labels (the LLM may
# call OMI a "product" in one extraction and a "device" in another).
_CONCEPT_CLUSTER = frozenset({"product", "device", "concept", "subsystem", "repo"})

FUZZY_THRESHOLD = 0.85


def norm(name: str, aliases: dict[str, str] | None = None) -> str:
    """Canonical lowercase form — MUST match the session-awareness
    ledger's keyword normalization (both route through
    ``normalize_content``) so ledger keys equal norm_names."""
    if aliases is None:
        aliases = load_aliases()
    return normalize_content(name.strip(), aliases).strip().lower()


async def resolve_entity(
    db: aiosqlite.Connection,
    *,
    name: str,
    entity_type: str,
    source: str = "extracted",
    aliases: dict[str, str] | None = None,
    _commit: bool = True,
) -> tuple[str, str]:
    """Resolve *name* to an entity id, creating if needed.

    Returns ``(entity_id, provenance)`` where provenance is EXTRACTED
    for confident identity and AMBIGUOUS when a fuzzy near-match was
    detected (adjudication queued).
    """
    # Tier 1 — mechanical: exact identity by construction. Literal
    # identifiers must NEVER pass through alias expansion (an alias like
    # "cc" → "claude code" would rewrite path components); lowercase-only,
    # matching what record_anchors has always written.
    if entity_type in MECHANICAL_TYPES:
        norm_name = name.strip().lower()
        if not norm_name:
            raise ValueError(f"unresolvable empty entity name {name!r}")
        entity_id = await entities_crud.create_entity(
            db, name=name, norm_name=norm_name, entity_type=entity_type,
            source=source, _commit=_commit,
        )
        return entity_id, "EXTRACTED"

    norm_name = norm(name, aliases)
    if not norm_name:
        raise ValueError(f"unresolvable empty entity name {name!r}")

    # Tier 2 — named exact (same type, then concept-cluster cross-type).
    existing = await entities_crud.get_by_norm_name(
        db, norm_name=norm_name, entity_type=entity_type,
    )
    if existing is None and entity_type in _CONCEPT_CLUSTER:
        any_type = await entities_crud.get_by_norm_name(db, norm_name=norm_name)
        if any_type is not None and any_type["entity_type"] in _CONCEPT_CLUSTER:
            existing = any_type
    if existing is not None:
        return existing["entity_id"], "EXTRACTED"

    # Tier 3 — fuzzy against the cluster (or same type for person/org).
    cluster = (
        list(_CONCEPT_CLUSTER)
        if entity_type in _CONCEPT_CLUSTER
        else [entity_type]
    )
    candidates = await entities_crud.list_norm_names(db, entity_types=cluster)
    near = _closest(norm_name, candidates)
    entity_id = await entities_crud.create_entity(
        db, name=name, norm_name=norm_name, entity_type=entity_type,
        source=source, _commit=False,
    )
    if near is not None:
        await entities_crud.enqueue_adjudication(
            db, entity_id=entity_id, similar_entity_id=near, _commit=False,
        )
        if _commit:
            await db.commit()
        return entity_id, "AMBIGUOUS"

    # Tier 4 — genuinely new.
    if _commit:
        await db.commit()
    return entity_id, "EXTRACTED"


async def record_extraction(
    db: aiosqlite.Connection,
    memory_id: str,
    extraction,
    *,
    aliases: dict[str, str] | None = None,
) -> dict:
    """Give an extraction's entities/relationships identity (E3 wiring).

    Runs after ``linker.create_typed_links`` in the extraction cycle —
    the memory_links path stays intact in parallel; zero new LLM calls.
    Named entities resolve as type ``concept``; concept-cluster
    cross-type reuse folds them onto seeded/typed entities (extraction
    calling OMI a concept still lands on the seeded device). Mechanical
    anchors are NOT handled here — the ``MemoryStore.store()`` seam
    covers every write path including this one.

    Returns counts. Commits once at the end (the surrounding
    ``store.store()`` already committed the memory row itself).
    """
    from genesis.db.crud import entities as entities_crud

    if aliases is None:
        aliases = load_aliases()
    counts = {"mentions": 0, "links": 0, "ambiguous": 0}
    cache: dict[str, tuple[str, str] | None] = {}

    async def _resolve(name: str) -> tuple[str, str] | None:
        if name not in cache:
            try:
                cache[name] = await resolve_entity(
                    db, name=name, entity_type="concept",
                    source="extracted", aliases=aliases, _commit=False,
                )
            except ValueError:
                cache[name] = None
        return cache[name]

    for name in extraction.entities or []:
        pair = await _resolve(name)
        if pair is None:
            continue
        entity_id, provenance = pair
        if provenance == "AMBIGUOUS":
            counts["ambiguous"] += 1
        await entities_crud.upsert_mention(
            db, memory_id=memory_id, entity_id=entity_id,
            provenance=provenance, confidence=extraction.confidence,
            source="llm_extraction", _commit=False,
        )
        counts["mentions"] += 1

    for rel in extraction.relationships or []:
        from_name, to_name = rel.get("from", ""), rel.get("to", "")
        link_type = rel.get("type", "")
        if not from_name or not to_name or not link_type:
            continue
        source = await _resolve(from_name)
        target = await _resolve(to_name)
        if source is None or target is None or source[0] == target[0]:
            continue
        provenance = (
            "AMBIGUOUS"
            if rel.get("ambiguous") or "AMBIGUOUS" in (source[1], target[1])
            else "EXTRACTED"
        )
        try:
            confidence = float(rel.get("confidence", extraction.confidence))
        except (TypeError, ValueError):
            confidence = extraction.confidence
        # LLM output despite the prompt's [0,1] instruction: clamp, or
        # path-confidence products go >1 / negative downstream.
        confidence = min(1.0, max(0.0, confidence))
        await entities_crud.upsert_link(
            db,
            source_id=source[0],
            target_id=target[0],
            link_type=link_type,
            provenance=provenance,
            confidence=confidence,
            evidence_memory_id=memory_id,
            valid_at=extraction.temporal,
            _commit=False,
        )
        counts["links"] += 1

    await db.commit()
    return counts


def _closest(
    norm_name: str, candidates: list[tuple[str, str, str]]
) -> str | None:
    """entity_id of the nearest same-cluster norm_name above threshold."""
    best_id, best_ratio = None, FUZZY_THRESHOLD
    for cand_norm, cand_id, _cand_type in candidates:
        if cand_norm == norm_name:
            continue
        ratio = difflib.SequenceMatcher(None, norm_name, cand_norm).ratio()
        if ratio >= best_ratio:
            best_id, best_ratio = cand_id, ratio
    return best_id
