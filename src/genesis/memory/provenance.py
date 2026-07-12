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
from collections.abc import Iterable

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

# ── WS-3 origin_class taxonomy ────────────────────────────────────────────
# Persisted at STORE time (memory_metadata / knowledge_units / Qdrant
# payload), unlike the recall-time labels above which re-derive from the
# collection on every read. Future immunity gates key on
# external_untrusted vs not; owner and first_party are never blockable.

ORIGIN_OWNER = "owner"
ORIGIN_FIRST_PARTY = "first_party"
ORIGIN_EXTERNAL_UNTRUSTED = "external_untrusted"
ORIGIN_CLASSES = frozenset({ORIGIN_OWNER, ORIGIN_FIRST_PARTY, ORIGIN_EXTERNAL_UNTRUSTED})

# Pipelines whose CONTENT is text pulled off the world. ``curated`` is here
# deliberately: "curated" is an authority tier, not authorship — URL/file
# ingests land as curated and the body is third-party text even when the
# owner initiated the ingest (user-decided 2026-07-10; if B1 shadow logs
# show legitimate owner workflows would-block via curated units, split
# curated_upload/first_party vs curated_url/external — ingest_source
# already knows source_type). ``email``/``inbox``/``web_search``/
# ``web_fetch`` have no store() writers today; reserving them means a
# future writer is external BY DEFAULT rather than silently first-party.
_EXTERNAL_PIPELINES = frozenset(
    {
        "crag_web",
        "recon",
        "knowledge_ingest",
        "knowledge_ingest_source",
        "curated",
        "email",
        "inbox",
        "web_search",
        "web_fetch",
    }
)

# Pipelines that write Genesis's own observations/derivations or the
# owner's conversational content.
_FIRST_PARTY_PIPELINES = frozenset(
    {
        "conversation",
        "session_observer",
        "harvest",
        "synthesis",
        "event_calendar",
        "dream_cycle",
        "reflection",
        "drift",
        "extraction_job",
        "surplus",
        "reference_store",
    }
)

# Tool NAMES whose USE means a session pulled EXTERNAL-WORLD content into its
# working context — the signal WS-3 gate-1 (procedure) uses to classify a
# promoted procedure's origin (from the action spine, or an ExecutionTrace's
# ``tools_used``). CC built-ins are CamelCase; Genesis MCP tools arrive
# namespaced (``mcp__<server>__web_fetch``) and are matched on their final
# ``__``-delimited segment.
#
# Coarse-conservative BY DESIGN: "the session touched an external-ingest tool"
# over-approximates "external content induced THIS procedure" — the judge builds
# procedures from tool INPUTS plus its own reasoning, and fetched bodies live in
# tool RESULTS, which the spine/haystack do not carry. Over-observing is the
# correct SHADOW posture: the recorded rate is exactly what B4 measures before
# any flip to enforce. Enforce-grade signal needs tool_RESULT provenance
# (tracked as a WS-3 B4 follow-up).
_EXTERNAL_INGEST_TOOLS = frozenset(
    {
        # CC built-in web tools
        "WebFetch",
        "WebSearch",
        # Genesis MCP web + knowledge ingest (matched on final namespaced segment)
        "web_fetch",
        "web_search",
        "web_agent",
        "knowledge_recall",
        "knowledge_ingest",
        "knowledge_ingest_source",
        "knowledge_ingest_batch",
        "document_query",
        # Mixed-source recall that can surface external KB content
        # (memory_recall/memory_expand default to source='both'). Included per the
        # over-observe posture — a session that recalled KB then promoted a
        # procedure counts. If shadow saturates (these are common tools), the fix
        # is item-level recall provenance (B4), not a coarser net. NB: knowledge_*
        # recall above is KB-only (always external); memory_proactive runs as a
        # hook, never in the tool spine, so it can't appear here.
        "memory_recall",
        "memory_expand",
        # external recon (GitHub / model-intel / skill scans off the world)
        "recon_run_github_discovery",
        "recon_run_github_discovery_job",
        "recon_run_model_intelligence",
        "recon_run_skill_scan",
        # external social fetch
        "fetch_messages",
        "fetch_forum_threads",
        # arbitrary web navigation
        "browser_navigate",
    }
)


def origin_from_tool_names(tool_names: Iterable[str | None]) -> str:
    """Classify a session/trace origin from the NAMES of tools it used.

    Returns :data:`ORIGIN_EXTERNAL_UNTRUSTED` if any tool name signals ingest of
    external-world content (see :data:`_EXTERNAL_INGEST_TOOLS`), else
    :data:`ORIGIN_FIRST_PARTY`. MCP names are matched on their final
    ``__``-delimited segment (``mcp__genesis-health__web_fetch`` → ``web_fetch``).

    Never returns ``owner`` — owner authorship is asserted at explicit call
    sites (e.g. an explicit-teach MCP tool), never inferred from tool usage.
    """
    for name in tool_names:
        if not name:
            continue
        base = name.rsplit("__", 1)[-1]
        if name in _EXTERNAL_INGEST_TOOLS or base in _EXTERNAL_INGEST_TOOLS:
            return ORIGIN_EXTERNAL_UNTRUSTED
    return ORIGIN_FIRST_PARTY


def derive_origin_class(
    *,
    origin_class: str | None = None,
    source_pipeline: str | None = None,
    source_subsystem: str | None = None,
    collection: str | None = None,
) -> str:
    """Deterministic store-time origin classification.

    Precedence (each rule short-circuits):
      1. explicit ``origin_class`` override — validated, wins outright
      2. pipeline in the external set → external_untrusted (outranks
         source_subsystem: e.g. the recon pipeline stores web-collected
         signals WITH ``source_subsystem='triage'`` — content is external)
      3. pipeline in the first-party set → first_party
      4. any ``source_subsystem`` → first_party (internal subsystem writer)
      5. ``collection == 'knowledge_base'`` → external_untrusted (the same
         already-litigated discriminator :func:`is_external` uses)
      6. default → first_party

    This is the CONSERVATIVE store-time mapping (unknown internal writers
    stay first-party, matching :func:`is_external`'s documented stance).
    Fail-closed normalization of unknown/missing values to
    external_untrusted happens only at GATE time, in
    ``genesis.security.immunity.effective_origin_class`` — never here.
    """
    if origin_class is not None:
        if origin_class not in ORIGIN_CLASSES:
            raise ValueError(
                f"invalid origin_class {origin_class!r}; expected one of {sorted(ORIGIN_CLASSES)}"
            )
        return origin_class
    if source_pipeline in _EXTERNAL_PIPELINES:
        return ORIGIN_EXTERNAL_UNTRUSTED
    if source_pipeline in _FIRST_PARTY_PIPELINES:
        return ORIGIN_FIRST_PARTY
    if source_subsystem:
        return ORIGIN_FIRST_PARTY
    if collection == KNOWLEDGE_COLLECTION:
        return ORIGIN_EXTERNAL_UNTRUSTED
    return ORIGIN_FIRST_PARTY


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
