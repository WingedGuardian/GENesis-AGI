"""Shared knowledge-unit ingestion helper.

Used by both the ``knowledge_ingest`` / ``reference_store`` MCP tools in
``genesis.mcp.memory.knowledge`` and the automatic reference extractor in
``genesis.memory.extraction_job``. Handles upsert semantics, Qdrant point
replacement, FTS5 index consistency, and memory_class override.

This lives in ``memory/`` (not ``mcp/memory/``) so extraction_job.py can
depend on it without an upward layer dependency.
"""

from __future__ import annotations

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

# Module-level singleton (load_default_patterns() does filesystem I/O).
_SANITIZER = ContentSanitizer()
#: Fresh reads a credential write makes after a concurrent writer changed its
#: entry, before ``CredentialKeyChanged`` reaches the caller (owner ruling: every
#: writer in a race lands, so six first saves of one name all succeed).
_REREADS = 5
#: Opaque keys ``_mint_key`` tries before giving up (a clash is a 1 in 2**48 event).
_MINT_TRIES = 4


class UnknownEntryKey(ValueError):  # noqa: N818 - names the state, not an error
    """``entry_key`` names no credential entry: nothing was written."""


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

    ``insert_only`` (for auto-captured credentials, #2426; no writer passes it
    yet, a later change wires the extraction job to it): no stored credential
    is ever changed (below). A user-typed ``reference_store`` keeps upsert
    semantics.

    A credential's key (``concept``) is an opaque id, ``cred-`` and 12 hex,
    minted once; ``concept`` is the entry's NAME (#2426). A trusted write by
    name updates the earliest trusted entry whose ``name_hash``
    (``reference_ops.name_hash``) matches, else an entry with no hash recorded
    yet whose recorded name matches (and gives it the hash), else a new entry.
    An insert-only write returns an untrusted entry under that hash holding the
    same value and description, else makes a new entry; it never updates one.
    ``entry_key`` (trusted, credentials only) updates the entry stored under
    that key and keeps the name it records (``concept`` is not used); a key no
    entry holds raises ``UnknownEntryKey`` with nothing written. A name that
    reads like a key is a name like any other.
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
        insert_only=insert_only, entry_key=entry_key,
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
) -> str:
    """One ingest attempt (see ``ingest_knowledge_unit``)."""
    # A credential's value is held out of every indexed store (#2426) HERE, at the
    # chokepoint every writer passes through (the reference_store and
    # knowledge_ingest MCP tools, the extraction job, migrate_reference_data):
    # from this line on, `content` carries the header, a placeholder for the
    # description and one for the value, and the trailer; the value and the
    # description travel only to knowledge_crud.upsert, which writes them to one
    # reference_secrets row before the unit's own row. Refuses (ValueError) a
    # credential whose value cannot be located, or whose identifier, tags,
    # authority or provenance carries its value, before anything is written.
    # Every other reference kind keeps its value in the body.
    from genesis.memory import reference_ops

    # One spelling for the reference project, so every reader (lookup, delete,
    # the dashboard), which compares it exactly, sees what the gate saw.
    if (project or "").strip().lower() == reference_ops.REFERENCE_PROJECT:
        project = reference_ops.REFERENCE_PROJECT
    secret_value: str | None = None
    secret_detail: str | None = None
    observed: knowledge_crud.KeyObservation | None = None
    identity: tuple[str, str] | None = None
    credential = reference_ops.holds_secret(project, domain)
    if entry_key is not None and not credential:
        raise ValueError("entry_key addresses a credential entry only (#2426)")
    if credential:
        # One spelling for the credential domain too: the lookup's audit log and
        # the readers compare it exactly, so a mixed-case row would be revealed
        # with no audit row.
        domain = knowledge_crud.CREDENTIAL_DOMAIN
        if entry_key is not None:
            if insert_only:
                raise ValueError("entry_key is for trusted writes only (#2426)")
            observed = await knowledge_crud.observe_credential_key(
                db, project_type=project, domain=domain, concept=entry_key,
            )
            stored = None if observed.absent else await knowledge_crud.get(db, observed.unit_id)
            if stored is None:
                raise UnknownEntryKey("no credential entry has this key (#2426)")
            # A write by key keeps the name the entry records, never the key.
            name = reference_ops.recorded_name(entry_key, stored["body"])
        elif concept is None or not concept.strip():
            # The identifier is the entry's name. Derived from the body, it
            # would be the HELD body (header and placeholders), so two
            # credentials whose bodies differ only in their description and
            # value would share a name and the second would overwrite the first.
            raise ValueError(
                "a credential reference needs an explicit identifier (concept); "
                "refusing the write (#2426)"
            )
        else:
            name = concept
        # The name is the body's header line, built here from the name and
        # never from the key (a line break in it reads as a space). Only the
        # header a writer formats from the identifier is replaced, and for a
        # write by key the first line (the identifier given there is not
        # used): any other first line stays, and is checked strictly below.
        domain_header = f"[{knowledge_crud.CREDENTIAL_DOMAIN}] "
        ours = domain_header + concept + "\n" if entry_key is None else None
        if entry_key is not None:
            first, sep, rest = content.partition("\n")
            ours = first + sep if first.lower().startswith("[reference.") and sep else ""
        raw_name = name
        name = " ".join(name.splitlines())
        if content.startswith(ours):
            content = f"{domain_header}{name}\n" + content[len(ours):]
        # The value AND the free-text description come out of the body; both go
        # to one reference_secrets row. The body keeps only the header, the
        # two placeholders and the Tags/Captured trailer.
        content, secret_value, secret_detail = reference_ops.split_secret(content)
        if entry_key is not None:
            concept = entry_key
        else:
            # Which entry a write by name updates: one read of the entries the
            # name's hash may name, then one read of the chosen key, the
            # observation the upsert compare-and-swaps against.
            target, identity = await _resolve_by_name(
                db, project, domain, name, secret_value, secret_detail, insert_only=insert_only,
            )
            if insert_only and target is not None:
                # The same credential an untrusted write already stored: nothing
                # to write, and no vector to store beside the one it names.
                return target.id
            if target is not None:
                concept = target.concept
                observed = await knowledge_crud.observe_credential_key(
                    db, project_type=project, domain=domain, concept=concept,
                )
                # An update keeps the name the entry records (the same name,
                # perhaps in another case or spacing).
                recorded = reference_ops.recorded_name(target.concept, target.body)
                if recorded and content.startswith(f"{domain_header}{name}\n"):
                    name = recorded
                    content = f"{domain_header}{name}" + content[content.index("\n"):]
            else:
                concept, observed = await _mint_key(db, project, domain)
        if secret_value is None:
            if secret_detail is not None and observed.value is not None:
                # A new description with the value placeholder: the stored value
                # and the new description, together, as a new secret row.
                secret_value = observed.value
        elif secret_detail is None:
            # A new value with the description placeholder: the stored
            # description stays beside it, in the new secret row.
            secret_detail = observed.detail
        known_secret = secret_value if secret_value is not None else observed.value
        if known_secret is not None:
            # Nothing else is scrubbed: an indexed field that carries the value
            # refuses the write, before any store. The identifier first, with
            # its own message: it is the name of the entry.
            reference_ops.refuse_value_in_fields(
                known_secret, {"identifier": name, "identifier (as given)": raw_name},
                message="a credential identifier must not contain its value; refusing the write (#2426)",
                login_exempt=True,
            )
            # A caller tag that carries the value is dropped whole, from the
            # tags and from the body's Tags line; the write goes on. A tag is a
            # label, and the cost of a shape heuristic flagging an ordinary one
            # (a database name, a region) must be that tag, never the capture.
            tags_json, content = reference_ops.drop_value_tags(known_secret, tags_json, content)
            # The body header is "[reference.credentials] {identifier}": when it
            # is exactly that, it is checked by the identifier rule (a login
            # named there is not refused), and the rest of the body strictly.
            # Any other first line is checked strictly with the rest.
            header, _, rest = content.partition("\n")
            body_text = content
            if header == f"[{knowledge_crud.CREDENTIAL_DOMAIN}] {name}":
                reference_ops.refuse_value_in_fields(
                    known_secret, {"body header": header}, login_exempt=True,
                )
                body_text = rest
            # EVERY other caller field that can reach a store, whatever its
            # type, is checked, each as text (``field_text``) and leaf by leaf,
            # so a nested or non-string value hides nothing. The kwargs left out
            # are no caller surface for a credential: project and domain are
            # canonical here, origin_class is checked against its enum, and
            # collection, memory_type and confidence come only from internal
            # callers (the knowledge_ingest and reference_store tools expose
            # none of them).
            fields: dict[str, str | None] = {
                "body": body_text,
                "tags": tags_json,
                # The tags as they read once decoded: JSON escapes a non-ASCII
                # tag, which would hide a fullwidth spelling of the value.
                "tags (decoded)": _tags_text(tags_json),
            }
            for field, val in (
                ("authority", authority),
                ("provenance", provenance),
                ("memory class", memory_class),
                ("purpose", purpose),
                ("ingestion source", ingestion_source),
            ):
                fields[field] = field_text(val)
                for i, leaf in enumerate(_leaves(val)):
                    fields[f"{field} [{i}]"] = leaf
            reference_ops.refuse_value_in_fields(known_secret, fields)
        if memory_class is not None and memory_class not in CLASS_WEIGHTS:
            # A credential's memory class becomes a tag the store indexes
            # ("class:..."): only a known class name, never free text.
            raise ValueError(
                "refusing a credential reference whose memory class is not a known "
                f"class (one of {sorted(CLASS_WEIGHTS)}) (#2426)"
            )

    resolved_concept = concept if concept is not None else content[:200]
    # A credential write that upsert would refuse (no value to hold, or no
    # reference_secrets table) is refused HERE, before store.store puts a
    # vector and a memory_fts row in place that no unit would name.
    if credential and observed is None:
        observed = await knowledge_crud.observe_credential_key(
            db, project_type=project, domain=domain, concept=resolved_concept,
        )
    # An insert-only credential always writes under a freshly minted key, so the
    # check never finds it held (a re-upload returned its entry above).
    await knowledge_crud.check_credential_write(
        db, project_type=project, domain=domain, concept=resolved_concept,
        secret_value=secret_value, insert_only=insert_only, observed=observed,
    )

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
        secret_value=secret_value,
        secret_detail=secret_detail,
        expected=observed,
        name_hash=identity[0] if identity else None,
        name_origin=identity[1] if identity else None,
    )
    try:
        actual_id, inserted = await knowledge_crud.upsert(
            db, insert_only=insert_only, **upsert_kwargs,
        )
        if insert_only and not inserted:
            # The key was taken, with this value, between the check and the
            # write: the unit is unchanged, and so is the point it names.
            return actual_id
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
    return actual_id


async def _resolve_by_name(
    db: aiosqlite.Connection, project: str, domain: str, name: str,
    value: str | None, detail: str | None, *, insert_only: bool,
) -> tuple[knowledge_crud.CredentialEntry | None, tuple[str, str]]:
    """(the entry a write of ``name`` addresses, None for a new entry; the
    ``(name_hash, name_origin)`` the write records).

    For an insert-only write, the entry is an untrusted one under this hash
    holding the same value and description. For a trusted write it is the
    earliest trusted entry recording this hash,
    else the earliest with no hash yet whose recorded name hashes to it (the
    write then gives it the hash). A card-redacted name reuses a card-redacted
    entry only when it holds the same value."""
    from genesis.memory import reference_ops

    identity = reference_ops.name_hash(name)
    origin = reference_ops.name_origin(name, insert_only=insert_only)
    entries = []
    for entry in await knowledge_crud.named_credentials(
        db, project_type=project, domain=domain, name_hash=identity,
    ):
        if entry.name_hash is None:
            legacy, legacy_origin = reference_ops.legacy_identity(entry.concept, entry.body)
            if legacy != identity:
                continue
            entry = entry._replace(name_origin=entry.name_origin or legacy_origin)
        if entry.name_origin == origin:
            entries.append(entry)
    if insert_only:
        same = next(
            (e for e in entries if e.value is not None and e.value == value
             and (e.detail or "") == (detail or "")),
            None,
        )
        return same, (identity, origin)
    if origin == reference_ops.CARDED:
        entries = [e for e in entries if value is None or e.value == value]
    target = next((e for e in entries if e.name_hash), None) or next(iter(entries), None)
    return target, (identity, origin)


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


def field_text(value: object) -> str | None:
    """A caller field as the credential check reads it: a str as is, None
    skipped, anything else as JSON (non-ASCII kept as written, so a fullwidth
    spelling is not hidden behind an escape)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _leaves(value: object):
    """Every string (and other scalar) inside a nested dict or list, keys
    included: JSON escapes a quote or a backslash, so each leaf is checked as
    written too."""
    if isinstance(value, dict):
        for key, val in value.items():
            yield from _leaves(key)
            yield from _leaves(val)
    elif isinstance(value, (list, tuple, set)):
        for val in value:
            yield from _leaves(val)
    elif value is not None and not isinstance(value, str):
        yield str(value)
    elif isinstance(value, str):
        yield value


def _tags_text(tags_json: str | None) -> str | None:
    """The tags of ``tags_json`` joined as plain text, or None when it is not a
    JSON list (the raw string is checked on its own)."""
    if not tags_json:
        return None
    try:
        tags = json.loads(tags_json)
    except ValueError:
        return None
    return " ".join(str(t) for t in tags) if isinstance(tags, list) else None


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
