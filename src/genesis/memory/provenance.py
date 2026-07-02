"""First-party vs external-world provenance labels for recalled memory.

Genesis distinguishes FIRST-PARTY memory (its own observations, decisions, and
conversations — the ``episodic_memory`` collection) from EXTERNAL-WORLD
knowledge (ingested docs/APIs/papers and other content pulled off the world —
the ``knowledge_base`` collection). When KB content enters an LLM context it
must be labeled as external so the self-model never mistakes scraped knowledge
for its own ground truth (audit finding D12).

The authoritative discriminator is the Qdrant **collection** a memory was
retrieved from — always known at retrieval time, unlike the per-item store-time
``source`` string. These helpers turn that signal (plus the already-stored
``source_pipeline`` provenance) into a human/LLM-readable label.
"""

from __future__ import annotations

import logging

from genesis.security.sanitizer import (
    ContentSanitizer,
    ContentSource,
    strip_boundary_markers,
)

logger = logging.getLogger(__name__)

#: The external-world knowledge collection. Everything else is first-party.
KNOWLEDGE_COLLECTION = "knowledge_base"

_EXTERNAL = "external-world knowledge"
_FIRST_PARTY = "first-party memory"

# Friendly source descriptors keyed by a substring of ``source_pipeline``.
# Order matters: more-specific keys precede the prefixes they contain.
_PIPELINE_FRIENDLY: dict[str, str] = {
    "curated": "user-curated",
    "knowledge_ingest_source": "ingested doc",
    "knowledge_ingest": "ingested doc",
    "reference_store": "saved reference",
    "extraction_job": "auto-extracted",
    "crag_web": "web",  # CRAG web-fallback augmentation — live internet content
    "recon": "recon/web",
    "surplus": "surplus insight",
}

# Terse, space-free tokens for tight contexts (the proactive-recall hook).
_PIPELINE_SHORT: dict[str, str] = {
    "curated": "curated",
    "knowledge_ingest_source": "ingested",
    "knowledge_ingest": "ingested",
    "reference_store": "ref",
    "extraction_job": "extracted",
    "crag_web": "web",
    "recon": "recon",
    "surplus": "surplus",
}

# Placeholder ``source_doc`` values that carry no real provenance.
_PLACEHOLDER_DOCS = {"", "manual"}


def is_external(collection: str | None) -> bool:
    """True when the memory came from the external-world knowledge base.

    A missing/unknown collection is treated as first-party — the conservative,
    non-alarming default (never label something external on a guess).
    """
    return collection == KNOWLEDGE_COLLECTION


def _match(table: dict[str, str], source_pipeline: str | None, default: str) -> str:
    if source_pipeline:
        for key, label in table.items():
            if key in source_pipeline:
                return label
    return default


def short_source(source_pipeline: str | None) -> str:
    """Terse, single-token external-source tag (for the proactive hook)."""
    return _match(_PIPELINE_SHORT, source_pipeline, "ext")


def provenance_descriptor(
    *,
    collection: str | None,
    source_pipeline: str | None = None,
    source_doc: str | None = None,
) -> str:
    """One-line provenance label for a recalled item.

    External → ``"external-world knowledge (source: <friendly>[, doc: <doc>])"``.
    First-party → ``"first-party memory"``.
    """
    if not is_external(collection):
        return _FIRST_PARTY
    friendly = _match(_PIPELINE_FRIENDLY, source_pipeline, "external source")
    if source_doc and source_doc not in _PLACEHOLDER_DOCS:
        return f"{_EXTERNAL} (source: {friendly}, doc: {source_doc})"
    return f"{_EXTERNAL} (source: {friendly})"


def label_result_dicts(
    dicts: list[dict],
    *,
    default_collection: str = "episodic_memory",
) -> list[dict]:
    """Stamp ``collection`` + ``provenance`` onto a list of recall result dicts.

    Applied as a FINAL pass at MCP return points — AFTER any corrective-retrieval
    (CRAG) augmentation — so original, relaxed/raw-KB-augmented, AND web-fallback
    items are all labeled (audit D12). Idempotent and best-effort: a re-labeled
    item gets the same value; sentinel dicts (e.g. ``{"not_found": [...]}``) pass
    through untouched. CRAG web items (``origin='web'`` / ``source_pipeline=
    'crag_web'``) are unambiguously external-world web content.
    """
    for d in dicts:
        if not isinstance(d, dict):
            continue
        if "memory_id" not in d and "unit_id" not in d:
            continue  # sentinel row — leave alone
        payload = d.get("payload") or {}
        sp = d.get("source_pipeline") or payload.get("source_pipeline")
        if d.get("origin") == "web" or sp == "crag_web":
            coll = KNOWLEDGE_COLLECTION
        else:
            coll = d.get("collection") or payload.get("collection") or default_collection
        d["collection"] = coll
        d["provenance"] = provenance_descriptor(
            collection=coll,
            source_pipeline=sp,
            source_doc=d.get("source_doc") or d.get("source") or payload.get("source"),
        )
    return dicts


# ---------------------------------------------------------------------------
# Recall-side injection defense (PR2, sibling to the #809 ingestion scan).
#
# External-world content recalled from the KB is wrapped in <external-content>
# boundary markers at INJECT time, so the model structurally treats it as data
# rather than as Genesis's own trustworthy instructions. The soft `KB·source`
# provenance label is not enough on its own — an injection payload inside an
# ingested doc otherwise reaches the prompt looking first-party. First-party
# memory is NEVER wrapped (it's Genesis's own observations, not the threat
# vector). Detect-and-delimit, fail-open: a wrap failure returns the content
# unchanged so recall/inject never breaks.
# ---------------------------------------------------------------------------

#: Lazily-constructed wrapper sanitizer. wrap_content() needs no injection
#: patterns, but ContentSanitizer loads them on init; defer that one-time FS
#: read to the first external recall rather than paying it at import time
#: (provenance is imported widely, including the proactive hook).
_WRAP_SANITIZER: ContentSanitizer | None = None


def _wrap_sanitizer() -> ContentSanitizer:
    global _WRAP_SANITIZER
    if _WRAP_SANITIZER is None:
        _WRAP_SANITIZER = ContentSanitizer()
    return _WRAP_SANITIZER


def _source_for(source_pipeline: str | None) -> ContentSource:
    """Map a stored ``source_pipeline`` to the sanitizer risk tier for the tag.

    Live web-fallback (CRAG) content is a fresh off-the-web fetch, not settled
    KB, so it keeps WEB_FETCH's higher risk; recon findings keep RECON; every
    other KB recall is already-ingested content → MEMORY. The ``risk`` attribute
    is informational (no blocking) but should not understate a fresh fetch.
    """
    if source_pipeline:
        if "crag_web" in source_pipeline:
            return ContentSource.WEB_FETCH
        if "recon" in source_pipeline:
            return ContentSource.RECON
    return ContentSource.MEMORY


def wrap_external_recall(content: str, *, source_pipeline: str | None = None) -> str:
    """Wrap external-world recalled content in ``<external-content>`` markers.

    Call at INJECT points for content whose provenance is external-world (the
    caller has already decided this via ``is_external(collection)`` or a
    knowledge_base-only recall). Strips any pre-existing markers first, so
    content that leaked an upstream wrapper is never double-wrapped (idempotent).
    Fail-open: any error returns the original content so recall never breaks.
    """
    try:
        if not isinstance(content, str) or not content:
            return content
        stripped = strip_boundary_markers(content)
        return _wrap_sanitizer().wrap_content(stripped, _source_for(source_pipeline))
    except Exception:
        logger.warning("wrap_external_recall failed; returning unwrapped", exc_info=True)
        return content
