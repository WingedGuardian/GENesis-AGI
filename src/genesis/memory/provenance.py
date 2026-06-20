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
