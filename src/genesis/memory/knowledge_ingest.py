"""Shared knowledge-unit ingestion helper.

Used by both the ``knowledge_ingest`` / ``reference_store`` MCP tools in
``genesis.mcp.memory.knowledge`` and the automatic reference extractor in
``genesis.memory.extraction_job``. Handles upsert semantics, Qdrant point
replacement, FTS5 index consistency, and memory_class override.

This lives in ``memory/`` (not ``mcp/memory/``) so extraction_job.py can
depend on it without an upward layer dependency.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from genesis.db.crud import knowledge as knowledge_crud
from genesis.memory.classification import CLASS_WEIGHTS
from genesis.security.sanitizer import ContentSanitizer, ContentSource

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class StoredId(str):
    """The unit_id of an ingested unit (a plain str to every caller), with the
    ``concept`` it was stored under, which a caller needs to find it again
    when it is not the one asked for."""

    concept: str


#: Content or a value larger than this is split and probed off the event loop.
_LARGE = 64 * 1024
#: Times an untrusted credential write retries under a minted key.
_MINT_ATTEMPTS = 4
#: Random tags ``_mint_key`` tries before a uuid-derived one.
_MINT_TRIES = 8
#: A credential identifier is indexed up to this long (the full one is in meta).
_MAX_IDENTIFIER = 512
#: The pipeline labels a credential unit may carry; anything else reads as the
#: tool's own. A caller's free text never reaches the unit's source_pipeline.
_PIPELINES = frozenset({"reference_store", "extraction_job", "curated", "knowledge_ingest"})

# Module-level singleton (load_default_patterns() does filesystem I/O).
_SANITIZER = ContentSanitizer()


async def ingest_knowledge_unit(
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    memory_class: str | None = None,
    concept: str | None = None,
    tags_json: str | None = None,
    force_fts5_only: bool = False,
    purpose: list[str] | None = None,
    ingestion_source: str | None = None,
    collection: str = "knowledge_base",
    memory_type: str = "knowledge",
    confidence: float = 0.85,
    origin_class: str | None = None,
    insert_only: bool = False,
    mint_key: bool = False,
    held_value: str | None = None,
    held_detail: str | None = None,
    relationships: list | dict | None = None,
    caveats: list | dict | None = None,
) -> str:
    """Ingest or upsert a knowledge unit, returning the stable unit_id.

    Behaviors:
    - Idempotent on ``(project, domain, concept)``. Re-ingesting the same
      logical entry updates in place and preserves the original unit_id.
    - Writes a Qdrant point via ``store.store()`` (synchronous dedup via
      ``find_exact_duplicate``; a credential reference always gets a point of
      its own, deleted again if its unit does not end up naming it). If the
      row previously pointed at a different Qdrant ID, the stale point is
      deleted via ``store.delete()``.
    - Writes the knowledge_units + knowledge_fts pair via ``knowledge.upsert``
      inside a single deferred transaction.

    ``memory_class`` overrides the activation-weight classification
    downstream. Pass ``"fact"`` to avoid the 0.7x penalty that
    ``classify_memory`` applies to URL-bearing content.

    ``concept`` overrides the default ``content[:200]`` derivation. The
    reference store uses a structured identifier like
    ``"ExampleForum login"``.

    ``tags_json`` overrides the default tags array. Pass a pre-built JSON
    string when the caller needs reference-specific tag layout.

    ``insert_only`` (#2426; passed by ``ingest_untrusted_credential`` for uploads
    and distillation; the extraction job does not pass it yet): an existing credential
    key is never changed. The same value returns the existing unit_id and
    writes nothing; a different value raises
    ``knowledge_crud.CredentialKeyTaken`` and writes nothing, so the caller
    tries another key. A user-typed ``reference_store`` keeps upsert semantics.
    The write's origin (insert-only: untrusted) is recorded in the held meta: a
    trusted write never adopts an entry an untrusted one made.

    ``held_value`` / ``held_detail`` (#2426): the value and description held out
    of the index into ``reference_secrets``, named by the row's ``secret_ref``.
    For a credential they are the writer's own value and description, which
    the automatic split takes over what it parses from the body. For any other
    ``reference.*`` kind the caller passes a body whose value line is already
    masked, and these are stored as they are.

    The returned id is a ``StoredId``: a str, carrying the ``concept`` the unit
    was actually stored under (a credential's identifier is masked, or minted,
    when it carries its value or is missing).
    """
    # A credential write is a compare-and-swap against its own read of the key
    # (``knowledge_crud.KeyObservation``): if a same-key writer, in this process
    # or another, commits between that read and the unit's statement, nothing is
    # written and the whole ingest runs again ONCE from a fresh read (a new
    # stored secret to pair with, a new point). A second change raises
    # ``CredentialKeyChanged`` to the caller.
    kwargs = dict(
        store=store, db=db, content=content, project=project, domain=domain,
        authority=authority, provenance=provenance, memory_class=memory_class,
        concept=concept, tags_json=tags_json, force_fts5_only=force_fts5_only,
        purpose=purpose, ingestion_source=ingestion_source, collection=collection,
        memory_type=memory_type, confidence=confidence, origin_class=origin_class,
        insert_only=insert_only, mint_key=mint_key, held_value=held_value, held_detail=held_detail,
        relationships=relationships, caveats=caveats,
    )
    try:
        return await _ingest_once(**kwargs)
    except knowledge_crud.CredentialKeyChanged:
        logger.info("Credential key changed while the ingest ran; retrying once from a fresh read")
        return await _ingest_once(**kwargs)


async def _ingest_once(
    *,
    store: MemoryStore,
    db: aiosqlite.Connection,
    content: str,
    project: str,
    domain: str,
    authority: str = "unknown",
    provenance: dict | None = None,
    memory_class: str | None = None,
    concept: str | None = None,
    tags_json: str | None = None,
    force_fts5_only: bool = False,
    purpose: list[str] | None = None,
    ingestion_source: str | None = None,
    collection: str = "knowledge_base",
    memory_type: str = "knowledge",
    confidence: float = 0.85,
    origin_class: str | None = None,
    insert_only: bool = False,
    mint_key: bool = False,
    held_value: str | None = None,
    held_detail: str | None = None,
    relationships: list | dict | None = None,
    caveats: list | dict | None = None,
) -> str:
    """One ingest attempt (see ``ingest_knowledge_unit``)."""
    # A credential's value is held out of every indexed store (#2426) HERE, at the
    # chokepoint every writer passes through (the reference_store and
    # knowledge_ingest MCP tools, the extraction job, distillation, uploads,
    # migrate_reference_data): from this line on, `content` carries the header,
    # a placeholder for the description and one for the value, and the Tags
    # line; the value, the description and the caller's own metadata travel only
    # to knowledge_crud.upsert, which writes them to one reference_secrets row
    # before the unit's own row (owner ruling c91c5b74). Only the identifier and
    # the tags are indexed, both capped and with every span of the value masked;
    # nothing is refused to keep a secret out of the index (ruling 6f73fe4d).
    # Every other reference kind keeps its value in the body, unless the caller
    # holds it out itself (``held_value``).
    from genesis.memory import reference_ops

    # One spelling for the reference project, so every reader (lookup, delete,
    # the dashboard), which compares it exactly, sees what the gate saw.
    if (project or "").strip().lower() == reference_ops.REFERENCE_PROJECT:
        project = reference_ops.REFERENCE_PROJECT
    secret_value: str | None = None
    secret_detail: str | None = None
    secret_meta: str | None = None
    observed: knowledge_crud.KeyObservation | None = None
    credential = reference_ops.holds_secret(project, domain)
    if credential and memory_class is not None and memory_class not in CLASS_WEIGHTS:
        # A credential's class becomes a tag the store indexes ("class:..."):
        # only a known class name, never free text. The text is not logged.
        logger.warning("Unknown memory class on a credential reference; stored as 'fact'")
        memory_class = "fact"
    if credential:
        # One spelling for the credential domain too: the lookup's audit log and
        # the readers compare it exactly, so a mixed-case row would be revealed
        # with no audit row.
        domain = knowledge_crud.CREDENTIAL_DOMAIN
        # Card numbers in the caller's own fields are replaced with the shared
        # redactor's marker, the rest kept (owner ruling 1dedc18a), ONCE here
        # for every caller (an upload's file name and path, a distilled unit's
        # concept, tags and relationships, the MCP tools' fields), before any
        # of them is recorded, masked or indexed. The value and description
        # keep their refusal (``split_secret``).
        from genesis.security.card_redaction import redact_text

        pipeline = provenance.get("source_pipeline") if isinstance(provenance, dict) else None
        old_header = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {' '.join(concept.splitlines())}" if concept else None
        given = concept
        concept = redact_text(concept) if concept else concept
        # A name that had a card redacted names an entry only with its value.
        carded = concept != given
        if old_header and content.startswith(old_header + "\n"):
            # A body formatted with the identifier as its header line keeps it.
            content = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {' '.join(concept.splitlines())}" + content[len(old_header):]
        tags_json = redact_text(tags_json) if tags_json else tags_json
        authority, provenance, purpose, ingestion_source, relationships, caveats = (
            _redacted_like(field) for field in (
                authority, provenance, purpose, ingestion_source, relationships, caveats,
            )
        )
        # The caller's metadata, verbatim, is held with the value; the unit
        # carries neutral values in its place (below).
        meta = {
            k: v for k, v in (
                ("identifier", concept), ("tags", _tags_as_given(tags_json)),
                ("authority", authority), ("provenance", provenance), ("purpose", purpose),
                ("ingestion_source", ingestion_source), ("relationships", relationships),
                ("caveats", caveats),
            ) if v is not None
        }
        # The identifier is the body's one header line: a line break in it
        # would start a line of its own (a second "Value:" line, a "Tags:" line),
        # so it reads as a space. It is masked WHOLE and only then capped (the
        # full one is in meta), so a cut never leaves part of a secret.
        flat = " ".join(concept.splitlines()) if concept else ""
        minted: knowledge_crud.KeyObservation | None = None
        if not flat.strip():
            # The identifier is the entry's key. Derived from the body, it would
            # be the HELD body, so two credentials would share a key: a fresh,
            # FREE one is minted, and returned to the caller (StoredId.concept).
            flat, minted = await _mint_key(db, project, domain, "credential")
        header = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {flat}"
        # The value AND the free-text description come out of the body. A body
        # with no "Value:" line is free text (a distilled unit, an uploaded
        # file): its value is prose (``ValueMask``), and nothing of it is
        # indexed. Large content is split off the event loop (an upload can be
        # 2 MB).
        prose = "\n\nValue: " not in content
        split = functools.partial(
            reference_ops.split_secret, content, header=header, tags=_tag_list(tags_json),
            value=held_value, detail=held_detail,
        )
        large = len(content) + len(held_value or "") > _LARGE
        content, secret_value, secret_detail = await asyncio.to_thread(split) if large else split()
        # One read of the key: the stored secret (for a re-ingest that carries
        # a placeholder) and the observation the upsert compare-and-swaps
        # against, so what this write keeps is what the commit retains.
        concept = flat
        observed = minted or await knowledge_crud.observe_credential_key(
            db, project_type=project, domain=domain, concept=concept,
        )
        if secret_value == "" and observed.value is not None:
            # An empty value line on a key that holds one keeps it.
            secret_value = None
        known_secret = (secret_value if secret_value is not None else observed.value) or ""
        known_detail = secret_detail if secret_detail is not None else observed.detail
        entries = await knowledge_crud.masked_key_rows(db, project_type=project, domain=domain)
        for entry in entries if insert_only and secret_value is not None else ():
            # An untrusted write of the value an untrusted entry already holds
            # under this same recorded name (a re-upload) is that entry.
            if entry.origin == knowledge_crud.UNTRUSTED and entry.given == flat and entry.value == secret_value and (
                (entry.detail or "") == (secret_detail or "")
            ):
                return _stored(entry.id, entry.concept)
        # The secrets this write must not repeat beside its own: the value it
        # replaces, and those of the ONE entry this name resolves to (the entry
        # it updates or, when no trusted one is named, the first of any origin
        # that records this name or whose masked base it matches: an upload
        # under another entry's name), its value and every span its mask has
        # replaced in earlier rotations; never its description. Which entry the
        # write UPDATES is the trusted-only rule; an untrusted write adopts
        # none. Stated limit: a name that spells a secret of an entry it does
        # NOT resolve to (another prefix, a mask-only or untagged key, the words
        # reordered) is not masked with it. The pure-Python work (resolution by
        # value and probe building) runs off the event loop when any value it
        # reads is large.
        resolve = functools.partial(_resolve, entries, flat, insert_only=insert_only)
        heavy = len(known_secret) + len(known_detail or "") + sum(len(e.value or "") for e in entries)
        named_entry, resolved = await asyncio.to_thread(resolve) if heavy > _LARGE else resolve()
        also = _secrets_of(observed, [resolved] if resolved else [], known_secret)
        # Every probe of the value and of the secrets the capture rules find in
        # it and in the description, built ONCE for the identifier and the tags.
        large = (
            len(known_secret) + len(known_detail or "") + sum(len(v or "") for v, _ in also)
            + len(flat) + len(tags_json or "")
        ) > _LARGE
        prepare = functools.partial(_prepare_masks, known_secret, flat, prose=prose, detail=known_detail, also=also)
        strict, named, masked, spans = await asyncio.to_thread(prepare) if large else prepare()
        adopted = None
        cut = len(masked) > _MAX_IDENTIFIER
        if masked != flat or cut:
            # The identifier carried the value, or was cut: it is stored masked
            # and capped, and the header is rebuilt from it. The masked
            # identifier is the entry's identity (owner ruling 900f1df2): a write
            # under it updates the entry already stored as "{masked} #xxxxxx",
            # with the same or a rotated value, through the normal
            # compare-and-swap upsert. Only when there is none is a random tag
            # minted (never derived from the value). A base that is nothing but
            # the mask (an identifier that was only its value) names no entry:
            # it always gets a fresh tag. An insert-only write
            # (``ingest_untrusted_credential``: uploads, distillation) always
            # takes a fresh key, and a trusted write never adopts the entry one
            # made. A placeholder re-ingest carries the stored value and
            # description over to the new key.
            given_value = secret_value
            if secret_value is None:
                secret_value = observed.value
            if secret_detail is None:
                secret_detail = observed.detail
            base = reference_ops.cap_masked(masked, _MAX_IDENTIFIER - 8)
            names = not insert_only and bool(base.replace(reference_ops.HELD_MASK, "").strip())
            existing_key = None
            if names and named_entry is not None and (not cut or named_entry.given == flat):
                # The entry it names: by its recorded identifier (a cut one is
                # identified by its FULL text, which the meta records) or,
                # failing that, by its value.
                existing_key = named_entry.concept
                adopted = named_entry
            elif names and not cut:
                # A trusted entry under the same masked base. Its recorded spans
                # are carried as on the recorded-identity path: into this
                # write's probes (for the tags) and into the meta it writes.
                existing_key = await knowledge_crud.find_masked_key(
                    db, project_type=project, domain=domain, base=base, trusted_only=True,
                )
                adopted = next((e for e in entries if e.concept == existing_key), None)
                extra = _secrets_of(None, [adopted] if adopted else [], known_secret)
                if set(extra) - set(also):
                    also = (*also, *extra)
                    build = functools.partial(
                        reference_ops.value_masks, known_secret, prose=prose, detail=known_detail, also=also,
                    )
                    large = large or sum(len(v or "") for v, _ in extra) > _LARGE
                    strict, named = await asyncio.to_thread(build) if large else build()
            # Known limit, fixed by the credential move PR: an existing entry
            # whose key still spells a secret of this write (the value it
            # rotates to) is updated under that key as it is, and an untagged
            # entry under this very identifier is left as it is beside the new,
            # masked one.
            if existing_key:
                concept = existing_key
                observed = await knowledge_crud.observe_credential_key(
                    db, project_type=project, domain=domain, concept=concept,
                )
            if not existing_key or (carded and given_value not in (None, observed.value)):
                # A minted key is never an update target: it is free when
                # observed, and the upsert's absent compare-and-swap refuses it
                # if another write takes it first. A card-redacted name first
                # reuses the entry that records it with this very value.
                adopted = _same_value_entry(entries, flat, given_value) if carded else None
                concept, observed = await (
                    _observed_key(db, project, domain, adopted.concept) if adopted
                    else _mint_key(db, project, domain, base)
                )
        elif mint_key or (carded and not observed.absent and secret_value not in (None, observed.value)):
            # An untrusted retry whose plain identifier another value holds, or
            # a card-redacted name another value holds: the identifier with ONE
            # fresh, free tag (or, for the card name, the entry recording it
            # with this value).
            adopted = None if mint_key else _same_value_entry(entries, flat, secret_value)
            concept, observed = await (
                _observed_key(db, project, domain, adopted.concept) if adopted
                else _mint_key(db, project, domain, reference_ops.cap_masked(flat, _MAX_IDENTIFIER - 8))
            )
        # What this write knows of the entry's identity, held in meta (never
        # indexed): who wrote it, and every span the identifier's mask has
        # replaced, this time and in each earlier rotation of the entry it
        # updates, so a later write under the same identifier masks them all.
        recorded = [*((adopted.masked_spans or ()) if adopted else ()), *spans]
        meta[knowledge_crud.IDENTITY_META] = {
            "origin": knowledge_crud.UNTRUSTED if insert_only else knowledge_crud.TRUSTED,
            "masked_spans": list(dict.fromkeys(span for span in recorded if span.strip())),
        }
        secret_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False, default=str)
        content = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {concept}" + content[len(header):]
        if secret_value is None and observed.value is not None and (
            secret_detail is not None or secret_meta != observed.meta
        ):
            # A new description or new metadata with the value placeholder: the
            # stored value beside them, together, as a new secret row.
            secret_value = observed.value
        if secret_value is None and observed.value is None:
            # Nothing to keep and nothing given: an ordinary entry whose held
            # value is empty, never a refusal.
            secret_value = ""
        if secret_value is not None and secret_detail is None:
            # The stored description stays beside a new value, in the new row.
            secret_detail = observed.detail if observed.detail is not None else ""
        mask_tags = functools.partial(reference_ops.mask_value_tags, strict, tags_json, content)
        tags_json, content = await asyncio.to_thread(mask_tags) if large else mask_tags()
        # The unit's own metadata is neutral: the caller's is in the secret row.
        provenance = {"source_pipeline": pipeline if pipeline in _PIPELINES else "knowledge_ingest"}
        authority = "credential"
        purpose = ingestion_source = relationships = caveats = None
    elif held_value is not None:
        # Another reference kind whose caller holds its value out (the body's
        # value line already masked): stored as given, named by secret_ref.
        secret_value, secret_detail = held_value, held_detail

    resolved_concept = concept if concept is not None else content[:200]
    # A credential write that upsert would refuse (no value to hold, or no
    # reference_secrets table) is refused HERE, before store.store puts a
    # vector and a memory_fts row in place that no unit would name.
    if credential and observed is None:
        observed = await knowledge_crud.observe_credential_key(
            db, project_type=project, domain=domain, concept=resolved_concept,
        )
    held_by = await knowledge_crud.check_credential_write(
        db, project_type=project, domain=domain, concept=resolved_concept,
        secret_value=secret_value, insert_only=insert_only, observed=observed,
    )
    if insert_only and held_by is not None:
        # The key already holds this very value: nothing to write, and no
        # vector to store beside the one the unit already names.
        return _stored(held_by, resolved_concept)

    # Injection-pattern scan (detect-and-log, fail-open). This is the second
    # ingestion chokepoint — covering the knowledge_ingest MCP tool (allow-listed
    # into background/direct sessions), surplus intake, and reference extraction,
    # all of which bypass the orchestrator. Detection-only here: the stored body's
    # recall-side boundary-wrapping is handled by the recall-side defense (separate
    # PR); ingestion is never blocked.
    try:
        scan = _SANITIZER.sanitize(content, ContentSource.UNKNOWN)
        detected = bool(scan.detected_patterns)
    except Exception:
        detected = False
    # This chokepoint also ingests reference-store credentials, where even the
    # project/domain can be credential-derived. The detection log is therefore
    # FULLY STATIC — no content, no scan data, no identifiers — so a secret can
    # never reach the log. Detection is never blocked; the non-sensitive
    # orchestrator path carries the richer per-source detail.
    if detected:
        logger.warning(
            "Injection pattern detected in an ingested knowledge unit "
            "(see orchestrator logs for non-credential source detail)."
        )

    now_iso = datetime.now(UTC).isoformat()
    source_doc = "manual"
    if provenance and provenance.get("source_doc"):
        source_doc = provenance["source_doc"]

    if observed is not None:
        # The observation is the predecessor the compare-and-swap commits
        # against, so its point is the one this write really displaces.
        unit_id = observed.unit_id or str(uuid.uuid4())
        old_qdrant_id = observed.qdrant_id
    else:
        existing = await knowledge_crud.find_by_unique_key(
            db, project_type=project, domain=domain, concept=resolved_concept,
        )
        unit_id = existing["id"] if existing else str(uuid.uuid4())
        old_qdrant_id = existing.get("qdrant_id") if existing else None

    source_pipeline_val = (provenance or {}).get("source_pipeline", "knowledge_ingest")
    source_session_id = (provenance or {}).get("session_id")
    # WS-3: derive ONCE and pass the same value to both stores below — the
    # same both-stores-consistent discipline as the source_pipeline mirror.
    from genesis.memory.provenance import derive_origin_class

    resolved_origin = derive_origin_class(
        origin_class=origin_class,
        source_pipeline=source_pipeline_val,
        collection=collection,
    )
    # memory_type is passed by name at each call below, not in this dict: the
    # store-subsystem coverage test finds memory writers by their keywords
    # (tests/test_memory/test_store_subsystem_coverage.py).
    store_kwargs = dict(
        collection=collection,
        tags=[domain, project, authority],
        confidence=confidence,
        auto_link=False,
        source_pipeline=source_pipeline_val,
        memory_class=memory_class,
        source_session_id=source_session_id,
        force_fts5_only=force_fts5_only,
        project_type=project,
        origin_class=resolved_origin,
    )
    # ``created``: the point is this write's own (no other unit can name it), so
    # the cleanup after the upsert may delete it whenever the unit does not
    # name it. A credential write never reuses a point (dedup off): two writes
    # of the same held-out body would otherwise share one, and one write's
    # cleanup could delete the point the other's unit names (#2426). An
    # insert-only write learns whether dedup matched, so it never deletes a
    # pre-existing memory it did not create. Every other write keeps the
    # deduplicating store exactly as before.
    created = False
    if credential:
        # dedup=False: the id always names a memory this call created (the
        # store's contract), so ``created`` is known without asking.
        qdrant_memory_id = await store.store(
            content, f"knowledge:{project}/{domain}", memory_type=memory_type, dedup=False,
            **store_kwargs,
        )
        created = True
    elif insert_only:
        qdrant_memory_id, created = await store.store_reporting_creation(
            content, f"knowledge:{project}/{domain}", memory_type=memory_type, **store_kwargs,
        )
    else:
        qdrant_memory_id = await store.store(
            content, f"knowledge:{project}/{domain}", memory_type=memory_type, **store_kwargs,
        )

    embedding_model = getattr(store._embeddings, "model_name", "qwen3-embedding:0.6b-fp16")
    resolved_tags = tags_json or json.dumps([domain, project, authority])
    upsert_kwargs = dict(
        id=unit_id,
        project_type=project,
        domain=domain,
        source_doc=source_doc,
        concept=resolved_concept,
        body=content,
        tags=resolved_tags,
        ingested_at=now_iso,
        qdrant_id=qdrant_memory_id,
        confidence=confidence,
        source_platform=provenance.get("platform") if provenance else None,
        section_title=provenance.get("section_title") if provenance else None,
        source_date=provenance.get("source_date") if provenance else None,
        embedding_model=embedding_model,
        # Mirror the Qdrant write's default (audit D12): never leave the SQLite
        # knowledge_units pipeline NULL while the vector payload has a value —
        # provenance must be consistent across both stores.
        source_pipeline=source_pipeline_val,
        purpose=json.dumps(purpose) if purpose else None,
        ingestion_source=ingestion_source,
        origin_class=resolved_origin,
        relationships=json.dumps(relationships) if relationships else None,
        caveats=json.dumps(caveats) if caveats else None,
        secret_value=secret_value,
        secret_detail=secret_detail,
        secret_meta=secret_meta,
        expected=observed,
    )
    try:
        actual_id, inserted = await knowledge_crud.upsert(
            db, insert_only=insert_only, **upsert_kwargs,
        )
        if insert_only and not inserted:
            # The key was taken, with this value, between the check and the
            # write: the unit is unchanged, and so is the point it names.
            return _stored(actual_id, resolved_concept)
    finally:
        # After EVERY attempt (success, CredentialKeyTaken, a trigger's refusal,
        # a cancellation, any error): a point this write created that no unit
        # names when the attempt ends is deleted, so a refused write leaves no
        # recallable orphan. Safe because the point is this write's alone.
        if created:
            await _drop_unnamed_point(store, db, qdrant_memory_id)

    # AFTER the upsert, so a failed upsert (a refused credential write, a
    # missing reference_secrets table) never leaves the row pointing at a
    # point already deleted: the old point stays until its replacement row commits.
    # If we replaced an existing entry whose Qdrant point ID differs from
    # the new one, the old point is now orphaned — delete it so Qdrant
    # stays in sync with the knowledge_units row of record. store.delete()
    # handles metadata, FTS5, Qdrant, links, and pending_embeddings cascade.
    # store.store dedups by exact content, so two non-credential units can
    # share one point: never delete a point any unit still names, this one
    # included (a concurrent write may have pointed it back). For a credential
    # the check is reliable, not merely likely: its writes never reuse a point,
    # so once no unit names the old point, no later write can name it again.
    still_named = bool(old_qdrant_id) and await knowledge_crud.qdrant_point_named_elsewhere(
        db, old_qdrant_id, exclude_id="",
    )
    if old_qdrant_id and old_qdrant_id != qdrant_memory_id and not still_named:
        try:
            res = await store.delete(old_qdrant_id)
            if res.get("deferred"):
                # Vector store unavailable → store.delete() touched nothing, so
                # the replaced memory is still fully alive (point + metadata +
                # FTS) alongside its successor — and unlike a ghost, no cleanup
                # can classify it as an offender (it has a metadata row). No
                # sweep retries this delete either, so log LOUDLY: this is the
                # knowledge-unit stale-pair case for a cross-store reconcile.
                logger.error(
                    "Stale-point cleanup deferred for %s while upserting "
                    "knowledge unit %s: vector store unavailable — the replaced "
                    "memory remains recallable alongside its successor until "
                    "reconciled", old_qdrant_id, unit_id,
                )
        except Exception:
            logger.error(
                "Failed to clean up stale Qdrant point %s while upserting "
                "knowledge unit %s", old_qdrant_id, unit_id, exc_info=True,
            )

    logger.info(
        "Knowledge unit %s (project=%s, domain=%s)",
        "ingested" if inserted else "updated",
        project, domain,
    )
    return _stored(actual_id, resolved_concept)


def _stored(unit_id: str, concept: str) -> StoredId:
    stored = StoredId(unit_id)
    stored.concept = concept
    return stored


def _entry_named_by(entries: list, identifier: str):
    """The stored credential (a ``knowledge_crud.MaskedEntry``) a trusted write
    under ``identifier`` names, or None when there is none.

    The identity rule: the entry whose held meta records this very identifier
    as given (line breaks read as spaces), the one CREATED first when several
    do. Only an entry a trusted write made can be named (one with no origin
    recorded is legacy and counts as trusted): an upload or a distilled
    document recording the same identifier is never adopted. Failing that, any
    trusted entry ``"{base} #xxxxxx"`` (legacy or not) whose own value and
    recorded masked spans mask the identifier to ``base`` (compared as the mask
    compares text): the masked name is its identity (owner ruling
    900f1df2), so a rotation saved under the old, value-bearing spelling of
    the name updates it."""
    from genesis.memory import reference_ops

    trusted = [entry for entry in entries if entry.trusted]
    for entry in trusted:
        if entry.given == identifier:
            return entry
    for entry in trusted:
        if entry.value and _base_parts_in(entry, identifier) and _compared(
            reference_ops.value_masks(
                entry.value, detail=entry.detail,
                also=tuple((span, None) for span in entry.masked_spans or ()),
            )[1].mask(identifier)
        ) == _compared(entry.concept[:-8]):
            return entry
    return None


def _resolve(entries: list, identifier: str, *, insert_only: bool) -> tuple:
    """(the trusted entry a trusted write under ``identifier`` updates, the
    entry the name resolves to for masking). The second is the first unless a
    trusted lookup names none (always, for an insert-only write); then it is
    the first entry of any origin that records this name, or else whose masked
    base the name matches. Pure Python, so it may run off the event loop."""
    named = None if insert_only else _entry_named_by(entries, identifier)
    return named, named or _entry_named_by([e._replace(origin=None) for e in entries], identifier)


def _compared(text: str) -> str:
    """``text`` as the mask compares it, with every whitespace run one space."""
    from genesis.memory.reference_masks import _normalise

    return " ".join(_normalise(text).split())


def _base_parts_in(entry, identifier: str) -> bool:
    """Whether ``identifier`` could mask to the entry's key base: its unmasked
    pieces occur in it in order, the first at its start and the last at its
    end unless a mask stands there, compared as the mask compares text. A cheap
    test before any probe is built (a mask only ever replaces text, so a base
    with no mask matches only the very same name). A base that is only the
    mask has no pieces: it names no entry (it is never reused)."""
    from genesis.memory import reference_ops

    pieces = [_compared(piece) for piece in entry.concept[:-8].split(reference_ops.HELD_MASK)]
    if not any(pieces):
        return False
    text = _compared(identifier)
    if len(pieces) == 1:
        return text == pieces[0]
    if not (text.startswith(pieces[0]) and text.endswith(pieces[-1])):
        return False
    at = len(pieces[0])
    for piece in pieces[1:-1]:
        at = text.find(piece, at)
        if at == -1:
            return False
        at += len(piece)
    return at <= len(text) - len(pieces[-1])


def _secrets_of(observed, entries: list, known_secret: str) -> tuple:
    """(value, description) pairs a write must not repeat: those of the key it
    observed, and of ``entries`` their VALUE (never a description) with each of
    their recorded masked spans."""
    pairs = [(observed.value, observed.detail)] if observed is not None else []
    for entry in entries:
        pairs += [(entry.value, None), *((span, None) for span in entry.masked_spans or ())]
    return tuple(dict.fromkeys((v, d) for v, d in pairs if v and v != known_secret))


def _tags_as_given(tags_json: str | None) -> object:
    """The caller's tags as given (decoded when they are JSON), for meta."""
    if not tags_json:
        return None
    try:
        return json.loads(tags_json)
    except ValueError:
        return tags_json


async def ingest_untrusted_credential(**kwargs) -> str:
    """``ingest_knowledge_unit`` for a credential from a source that must never
    replace one (an upload, a distilled document): insert-only, so a key that
    already holds this value is a no-op returning its id, and a key holding a
    DIFFERENT value is never overwritten; the write retries under a minted,
    free key instead (the identifier with ONE fresh tag, ``_mint_key``; the
    identifier as given stays the entry's recorded name). Never refused for the
    key being taken (#2426, owner ruling 6f73fe4d)."""
    concept = kwargs.pop("concept", None) or "credential"
    for attempt in range(_MINT_ATTEMPTS + 1):
        try:
            return await ingest_knowledge_unit(
                concept=concept, insert_only=True, mint_key=attempt > 0, **kwargs,
            )
        except knowledge_crud.CredentialKeyTaken:
            continue
    raise knowledge_crud.CredentialKeyTaken("every minted key was taken (#2426)")


async def _mint_key(db, project: str, domain: str, base: str) -> tuple[str, knowledge_crud.KeyObservation]:
    """(a fresh key ``"{base} #xxxxxx"`` that no entry holds, the observation
    of it, absent). The one place a credential key is minted: up to
    ``_MINT_TRIES`` random tags are tried, then a uuid-derived one, which still
    ends in the six-hex tag the identity lookups match. The observation binds
    the upsert to an absent key, so a write that takes it first makes this one
    retry rather than update it."""
    for _ in range(_MINT_TRIES):
        key = f"{base} #{secrets.token_hex(3)}"
        observed = await knowledge_crud.observe_credential_key(
            db, project_type=project, domain=domain, concept=key,
        )
        if observed.absent:
            return key, observed
    key = f"{base} #{uuid.uuid4().hex[:6]}"
    return key, knowledge_crud.KeyObservation(None, None, None, None)


def _same_value_entry(entries: list, flat: str, value: str | None):
    """The trusted entry that records ``flat`` as its name and holds ``value``
    (a card-redacted name's own earlier save), or None."""
    return next(
        (e for e in entries if e.trusted and e.given == flat and value is not None and e.value == value), None,
    )


async def _observed_key(db, project: str, domain: str, key: str) -> tuple[str, knowledge_crud.KeyObservation]:
    """(``key``, its observation)."""
    return key, await knowledge_crud.observe_credential_key(db, project_type=project, domain=domain, concept=key)


def _redacted_like(field):
    """``field`` with card numbers redacted, of the same type: a mapping or a
    list the redactor withholds whole (its parts together spell a card) is
    kept as one holding the marker, so no reader of it meets a string."""
    from genesis.security.card_redaction import redact_value

    walked = redact_value(field)
    if isinstance(field, dict) and not isinstance(walked, dict):
        return {"withheld": walked}
    if isinstance(field, list | tuple) and not isinstance(walked, list | tuple):
        return [walked]
    return walked


def _prepare_masks(value: str, flat: str, *, prose: bool, detail: str | None, also: tuple) -> tuple:
    """(strict mask, identifier mask, the masked identifier, its masked spans):
    the pure-Python preparation of a credential write, in one call so it runs
    off the event loop together when any input is large."""
    from genesis.memory import reference_ops

    strict, named = reference_ops.value_masks(value, prose=prose, detail=detail, also=also)
    masked, spans = named.mask_and_spans(flat)
    return strict, named, masked, spans


def _tag_list(tags_json: str | None) -> list[str] | None:
    """The tags of ``tags_json`` as strings, or None when it is not a JSON list."""
    if not tags_json:
        return None
    try:
        tags = json.loads(tags_json)
    except ValueError:
        return None
    return [str(t) for t in tags] if isinstance(tags, list) else None


async def _drop_unnamed_point(store: MemoryStore, db: aiosqlite.Connection, qdrant_id: str | None) -> None:
    """Delete a point this write CREATED (``store_reporting_creation`` said so;
    never one dedup matched) that no unit names. Logged, never raised: the write
    it belonged to has already been decided."""
    try:
        if not qdrant_id or await knowledge_crud.qdrant_point_named_elsewhere(db, qdrant_id, exclude_id=""):
            return
        await store.delete(qdrant_id)
    except Exception:
        logger.error("Failed to clean up unnamed point %s", qdrant_id, exc_info=True)
