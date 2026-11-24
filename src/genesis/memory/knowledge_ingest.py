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
from genesis.knowledge.lifetime import unreferenced, writer
from genesis.memory.classification import CLASS_WEIGHTS
from genesis.security.sanitizer import ContentSanitizer, ContentSource

if TYPE_CHECKING:
    import aiosqlite

    from genesis.memory.store import MemoryStore

logger = logging.getLogger(__name__)


# Module-level singleton (load_default_patterns() does filesystem I/O).
_SANITIZER = ContentSanitizer()
#: Fresh reads a credential write makes after a concurrent writer changed its
#: entry, before ``CredentialKeyChanged`` reaches the caller (owner ruling: every
#: writer in a race lands, so six first saves of one name all succeed).
_REREADS = 5
#: Opaque keys ``_mint_key`` tries before giving up (a clash is a 1 in 2**48 event).
_MINT_TRIES = 4
#: Content or a value larger than this is split and probed off the event loop.
_LARGE = 64 * 1024
#: A credential identifier is indexed up to this long (the full one is in meta).
_MAX_IDENTIFIER = 512
#: The pipeline labels a credential unit may carry; anything else reads as the
#: tool's own. A caller's free text never reaches the unit's source_pipeline.
_PIPELINES = frozenset({"reference_store", "extraction_job", "curated", "knowledge_ingest"})
#: Where every credential unit is stored: the collection and memory type the
#: reference_store tool passes, which reference_lookup searches.
_REFERENCE_COLLECTION = "episodic_memory"
_REFERENCE_MEMORY_TYPE = "episodic"


class UnknownEntryKey(ValueError):  # noqa: N818 - names the state, not an error
    """``entry_key`` names no credential entry: nothing was written."""


class StoredId(str):
    """The unit_id of an ingested unit (a plain str to every caller), with the
    ``concept`` (key) it was stored under and, for a credential, ``name``: the
    searchable name its body header carries (masked and capped), None when the
    write found an existing entry and wrote nothing."""

    concept: str
    name: str | None
    #: The keys of the entries a value-less write's name matched when it could
    #: not tell them apart (a card-redacted name): it was stored as a new entry,
    #: and the caller passes ``entry_key`` to update one of these instead.
    candidates: tuple[str, ...] = ()


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
    entry_key: str | None = None,
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
    and distillation; the extraction job does not pass it yet): no stored
    credential is ever changed. An untrusted entry holding the same name, value
    and description returns its unit_id and writes nothing; anything else is a
    new entry. A user-typed ``reference_store`` keeps upsert semantics. The
    write's origin (insert-only: untrusted) is recorded: a trusted write never
    adopts an entry an untrusted one made.

    A credential's key (``concept``) is an opaque id, ``cred-`` and 12 hex,
    minted once; ``concept`` is the entry's NAME (#2426). A trusted write by
    name updates the earliest trusted entry whose ``name_hash``
    (``reference_ops.name_hash``) matches, else an entry with no hash recorded
    yet whose recorded name matches (and gives it the hash), else a new entry;
    an update keeps the full name the entry records (``reference_secrets.meta``).
    ``entry_key`` (trusted, credentials only) updates the entry stored under
    that key and keeps the name it records (``concept`` is not used); a key no
    entry holds raises ``UnknownEntryKey`` with nothing written. A name that
    reads like a key is a name like any other.

    ``held_value`` / ``held_detail`` (#2426): the value and description held out
    of the index into ``reference_secrets``, named by the row's ``secret_ref``.
    For a credential they are the writer's own value and description, which
    the automatic split takes over what it parses from the body. For any other
    ``reference.*`` kind the caller passes a body whose value line is already
    masked, and these are stored as they are.

    The returned id is a ``StoredId``: a str, carrying the ``concept`` the unit
    was stored under (a credential's opaque key) and the credential's ``name``
    as searched (its identifier masked and capped).
    """
    # A credential write is a compare-and-swap against its own read of the key
    # (``knowledge_crud.KeyObservation``): if a same-key writer, in this process
    # or another, commits between that read and the unit's statement (or a
    # concurrent first save takes the name), nothing is written and the whole
    # ingest runs again from a fresh read (a new stored secret to pair with, a
    # new point), up to ``_REREADS`` times; one more change raises
    # ``CredentialKeyChanged`` to the caller.
    kwargs = dict(
        store=store, db=db, content=content, project=project, domain=domain,
        authority=authority, provenance=provenance, memory_class=memory_class,
        concept=concept, tags_json=tags_json, force_fts5_only=force_fts5_only,
        purpose=purpose, ingestion_source=ingestion_source, collection=collection,
        memory_type=memory_type, confidence=confidence, origin_class=origin_class,
        insert_only=insert_only, entry_key=entry_key, held_value=held_value, held_detail=held_detail,
        relationships=relationships, caveats=caveats,
    )
    for attempt in range(_REREADS + 1):
        try:
            return await _ingest_once(**kwargs)
        except knowledge_crud.CredentialKeyChanged:
            if attempt == _REREADS:
                raise
            logger.info("Credential entry changed while the ingest ran; retrying from a fresh read")
    raise AssertionError("unreachable")  # pragma: no cover


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
    entry_key: str | None = None,
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
    candidates: tuple[str, ...] = ()
    secret_detail: str | None = None
    secret_meta: str | None = None
    observed: knowledge_crud.KeyObservation | None = None
    credential = reference_ops.holds_secret(project, domain)
    # The collection the caller named decides the derived origin class, as it
    # always has; a credential is STORED where reference_lookup searches, from
    # every writer (an upload, a distilled unit, the knowledge_ingest tool).
    origin_collection = collection
    if credential:
        collection, memory_type = _REFERENCE_COLLECTION, _REFERENCE_MEMORY_TYPE
    if entry_key is not None and not credential:
        raise ValueError("entry_key addresses a credential entry only (#2426)")
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
        # full one is in meta), so a cut never leaves part of a secret; the cut
        # is checked against the value again, since it can complete a word.
        flat = " ".join(concept.splitlines()) if concept else ""
        # The caller's header line, which the split drops (the body is built).
        header = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {flat}"
        # Identity (#2426): the key is an opaque id minted once, and an entry is
        # found by the hash of its full name, never by its masked or capped
        # key. A write by key (``entry_key``, trusted callers only) updates that
        # entry and keeps the name it records.
        keyed: knowledge_crud.KeyObservation | None = None
        if entry_key is not None:
            if insert_only:
                raise ValueError("entry_key is for trusted writes only (#2426)")
            keyed = await knowledge_crud.observe_credential_key(
                db, project_type=project, domain=domain, concept=entry_key,
            )
            stored = None if keyed.absent else await knowledge_crud.get(db, keyed.unit_id)
            if stored is None:
                raise UnknownEntryKey("no credential entry has this key (#2426)")
            # The full name the entry records (its meta), never the key.
            flat = reference_ops.recorded_name(entry_key, stored["body"], keyed.meta)
            carded = False
            meta.pop("identifier", None)
            if flat:
                meta["identifier"] = flat
            # The identifier given with a key is not used: a first line the
            # writer formatted as a reference header is dropped with it.
            first, sep, _ = content.partition("\n")
            if first.lower().startswith("[reference.") and sep:
                header = first
        # The indexed body is BUILT here (header and placeholders; the Tags
        # line later, from the masked tags_json): every word of the caller's
        # text after the header is held, never indexed (owner ruling 9f9d0b53).
        # A body with no "Value:" line is free text (a distilled unit, an
        # uploaded file): its value is prose (``ValueMask``). Large content is
        # split off the event loop (an upload can be 2 MB).
        prose = "\n\nValue: " not in content
        given = content
        split = functools.partial(
            reference_ops.split_secret, content, header=header, value=held_value, detail=held_detail,
        )
        large = len(content) + len(held_value or "") > _LARGE
        content, secret_value, secret_detail = await asyncio.to_thread(split) if large else split()
        # A write with no identifier is a new entry, named "credential" (#4416:
        # minted, never refused).
        nameless = not flat.strip()
        target, identity, entries = (None, reference_ops.name_hash(flat), []) if nameless else (
            await _resolve_by_name(db, project, domain, flat, secret_value, carded=carded, insert_only=insert_only)
        )
        candidates = _ambiguous(entries, secret_value, carded=carded, insert_only=insert_only)
        first, sep, _ = given.partition("\n")
        if sep and first != header and any(
            first == (entry.body or "").partition("\n")[0] for entry in entries
        ):
            # A body read back and saved again starts with the header STORED
            # for the entry its name resolves to (its name masked): that line
            # is the header, never description text (comment 4579950912). The
            # value is the same either way, so the resolution stands.
            header = first
            split = functools.partial(split, header=header)
            content, secret_value, secret_detail = await asyncio.to_thread(split) if large else split()
        if insert_only:
            # The value this write would store: an upload whose whole text is
            # the placeholder holds an empty value, and is compared as that.
            stored_value = secret_value if secret_value is not None else ""
            for entry in entries:
                # An untrusted write of the credential an untrusted entry already
                # holds (a re-upload) is that entry: the one sameness rule.
                if knowledge_crud.same_untrusted(entry, stored_value, secret_detail, json.dumps({"identifier": flat})):
                    return _stored(entry.id, entry.concept)
        if keyed is not None:
            concept, observed = entry_key, keyed
        elif target is not None:
            concept = target.concept
            observed = await knowledge_crud.observe_credential_key(
                db, project_type=project, domain=domain, concept=concept,
            )
            # An update keeps the full name the entry records (the same name,
            # perhaps in another case or spacing), from its meta.
            recorded = reference_ops.recorded_name(target.concept, target.body, target.meta)
            if recorded:
                flat = meta["identifier"] = recorded
        else:
            concept, observed = await _mint_key(db, project, domain)
        write_identity = None if keyed is not None else (
            identity,
            reference_ops.UNTRUSTED if insert_only else reference_ops.UNNAMED if nameless
            else reference_ops.CARDED if carded else reference_ops.TRUSTED,
        )
        if secret_value == "" and observed.value is not None:
            # An empty value line on a key that holds one keeps it.
            secret_value = None
        known_secret = (secret_value if secret_value is not None else observed.value) or ""
        known_detail = secret_detail if secret_detail is not None else observed.detail
        # Whether the value the probes are built from was free prose: this
        # write's, or the stored one it keeps.
        value_prose = prose if secret_value is not None else knowledge_crud._meta_fields(observed.meta)[3]
        # The secrets this write must not repeat beside its own: the value it
        # replaces and every value and recorded masked span of the entries the
        # name resolves to (any origin), never a description. Stated limit: a
        # name that spells a secret of an entry under another name is not
        # masked with it. The pure-Python work runs off the event loop when any
        # value it reads is large.
        also = _secrets_of(observed, entries, known_secret, value_prose)
        # Every probe of the value and of the secrets the capture rules find in
        # it and in the description, built ONCE for the identifier and the tags.
        large = (
            len(known_secret) + len(known_detail or "") + sum(len(item[0] or "") for item in also)
            + len(flat) + len(tags_json or "")
        ) > _LARGE
        prepare = functools.partial(
            _prepare_masks, known_secret, flat, prose=value_prose, detail=known_detail, also=also,
        )
        strict, named, masked, spans = await asyncio.to_thread(prepare) if large else prepare()
        # The searchable name: the identifier masked, then capped. It carries
        # the body header; the key never does.
        name = reference_ops.cap_masked(masked, _MAX_IDENTIFIER, named) if not nameless else "credential"
        # What this write knows of the entry's identity, held in meta (never
        # indexed): who wrote it, every span the identifier's mask has replaced,
        # this time and in each earlier rotation of the entry it updates, so a
        # later write masks them all, and whether its value is prose.
        recorded = [*(knowledge_crud._meta_fields(observed.meta)[2] or ()), *spans]
        meta[knowledge_crud.IDENTITY_META] = {
            "origin": knowledge_crud.UNTRUSTED if insert_only else knowledge_crud.TRUSTED,
            "masked_spans": list(dict.fromkeys(span for span in recorded if span.strip())),
            "value_is_prose": value_prose,
        }
        secret_meta = json.dumps(meta, sort_keys=True, ensure_ascii=False, default=str)
        content = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {name}" + content[len(header):]
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
        known = isinstance(pipeline, str) and pipeline in _PIPELINES
        provenance = {"source_pipeline": pipeline if known else "knowledge_ingest"}
        authority = "credential"
        purpose = ingestion_source = relationships = caveats = None
    elif held_value is not None:
        # Another reference kind whose caller holds its value out (the body's
        # value line already masked): stored as given, named by secret_ref.
        secret_value, secret_detail = held_value, held_detail

    resolved_concept = concept if concept is not None else content[:200]
    if not credential:
        name = None
        write_identity = None
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
        secret_detail=secret_detail, secret_meta=secret_meta,
    )
    if insert_only and held_by is not None:
        # The key already holds this very value: nothing to write, and no
        # vector to store beside the one the unit already names.
        return _stored(held_by, resolved_concept, name, candidates)

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

    # Hold the knowledge writer lock from the predecessor read through cleanup, so
    # no other publisher can dedup onto, or retire, a point between this write's
    # store, its reference commit and its cleanup.
    async with writer(db):
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
            collection=origin_collection,
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
            name_hash=write_identity[0] if write_identity else None,
            name_origin=write_identity[1] if write_identity else None,
        )
        try:
            actual_id, inserted = await knowledge_crud.upsert(
                db, insert_only=insert_only, **upsert_kwargs,
            )
            if insert_only and not inserted:
                # The key was taken, with this value, between the check and the
                # write: the unit is unchanged, and so is the point it names.
                return _stored(actual_id, resolved_concept, name, candidates)
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
        # ``unreferenced`` is that global check; a failed lookup logs and skips the
        # cleanup rather than deleting a point it could not prove unnamed.
        if (old_qdrant_id and old_qdrant_id != qdrant_memory_id
                and await unreferenced(db, [old_qdrant_id])):
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
        return _stored(actual_id, resolved_concept, name, candidates)


def _stored(unit_id: str, concept: str, name: str | None = None, candidates: tuple[str, ...] = ()) -> StoredId:
    stored = StoredId(unit_id)
    stored.concept = concept
    stored.name = name
    stored.candidates = candidates
    return stored


async def _resolve_by_name(
    db: aiosqlite.Connection, project: str, domain: str, name: str,
    value: str | None, *, carded: bool, insert_only: bool,
) -> tuple[knowledge_crud.CredentialEntry | None, str, list[knowledge_crud.CredentialEntry]]:
    """(the entry a trusted write of ``name`` updates, None for a new entry;
    the name's hash; every stored entry the name names, any origin, oldest
    first).

    The entries are those recording this hash, and those recording none yet
    whose recorded name hashes to it (the write then gives it the hash). A
    trusted write updates the earliest trusted one, preferring one with the
    hash recorded. A card-redacted name reuses a card-redacted entry only when
    it holds the same value. An insert-only write updates none."""
    from genesis.memory import reference_ops

    identity = reference_ops.name_hash(name)
    entries = []
    for entry in await knowledge_crud.named_credentials(
        db, project_type=project, domain=domain, name_hash=identity,
    ):
        if entry.name_hash is None:
            legacy, legacy_origin = reference_ops.legacy_identity(entry.concept, entry.body, entry.meta)
            if legacy != identity:
                continue
            entry = entry._replace(name_origin=entry.name_origin or legacy_origin)
        entries.append(entry)
    if insert_only:
        return None, identity, entries
    origin = reference_ops.CARDED if carded else reference_ops.TRUSTED
    trusted = [e for e in entries if e.name_origin == origin]
    if carded:
        trusted = [e for e in trusted if value is None or e.value == value]
    if carded and value is None and len(trusted) > 1:
        # A value-less write cannot tell carded entries of one name apart: it is
        # a new entry, never an update of the wrong one (owner 2026-11-18).
        return None, identity, entries
    target = next((e for e in trusted if e.name_hash), None) or next(iter(trusted), None)
    return target, identity, entries


def _ambiguous(
    entries: list[knowledge_crud.CredentialEntry], value: str | None, *, carded: bool, insert_only: bool,
) -> tuple[str, ...]:
    """The keys a value-less carded write could have meant (``_resolve_by_name``
    then stores it as a new entry), or ()."""
    from genesis.memory import reference_ops

    if insert_only or not carded or value is not None:
        return ()
    carded = [e.concept for e in entries if e.name_origin == reference_ops.CARDED]
    # Only opaque keys are named: a key stored before them is a raw name.
    keys = tuple(k for k in carded if reference_ops.OPAQUE_KEY.fullmatch(k or ""))
    return keys if len(carded) > 1 else ()


async def _mint_key(
    db: aiosqlite.Connection, project: str, domain: str,
) -> tuple[str, knowledge_crud.KeyObservation]:
    """(a fresh opaque key ``cred-<12 hex>`` that no entry holds, its
    observation, absent). The one place a credential key is minted, never from
    the name or the value. The observation binds the upsert to an absent key,
    so a write that takes it first makes this one retry rather than update it."""
    for _ in range(_MINT_TRIES):
        key = f"cred-{secrets.token_hex(6)}"
        observed = await knowledge_crud.observe_credential_key(
            db, project_type=project, domain=domain, concept=key,
        )
        if observed.absent:
            return key, observed
    raise RuntimeError("no free credential key could be minted (#2426)")


def _secrets_of(observed, entries: list, known_secret: str, known_prose: bool) -> tuple:
    """(value, description, prose) of each secret a write must not repeat: the
    key it observed (its value, and its recorded masked spans), and of
    ``entries`` their VALUE (never a description) with each of their recorded
    masked spans. ``prose``: the value was free prose (``value_is_prose``); a
    span never is, so it is masked even in an upload's prose. A copy of the
    write's own value is left out only when its prose flag is the write's
    (``known_prose``): a typed copy of a prose value keeps its stricter probes."""
    triples = []
    if observed is not None:
        fields = knowledge_crud._meta_fields(observed.meta)
        triples = [(observed.value, observed.detail, fields[3]), *((span, None, False) for span in fields[2] or ())]
    for entry in entries:
        triples += [(entry.value, None, entry.prose), *((span, None, False) for span in entry.masked_spans or ())]
    return tuple(dict.fromkeys(
        t for t in triples if t[0] and (t[0] != known_secret or bool(t[2]) != bool(known_prose))
    ))


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
    replace one (an upload, a distilled document): insert-only, so the same
    credential an untrusted write already stored under this name is a no-op
    returning its id, and anything else is a new entry under a fresh opaque
    key; no stored entry is ever overwritten, and the write is never refused
    for its name being taken (#2426, owner ruling 6f73fe4d)."""
    return await ingest_knowledge_unit(insert_only=True, **kwargs)


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
