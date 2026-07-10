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
    norm_name = norm(name, aliases)
    if not norm_name:
        raise ValueError(f"unresolvable empty entity name {name!r}")

    # Tier 1 — mechanical: exact identity by construction.
    if entity_type in MECHANICAL_TYPES:
        entity_id = await entities_crud.create_entity(
            db, name=name, norm_name=norm_name, entity_type=entity_type,
            source=source, _commit=_commit,
        )
        return entity_id, "EXTRACTED"

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
