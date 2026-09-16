from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

import aiosqlite
from qdrant_client import QdrantClient

from genesis.db.crud import entities as entities_crud
from genesis.db.crud import memory as memory_crud
from genesis.db.crud import memory_links as memory_links_crud
from genesis.db.crud import pending_embeddings
from genesis.db.crud._id_resolve import AMBIGUOUS as _AMBIGUOUS
from genesis.db.crud._id_resolve import NOT_FOUND as _NOT_FOUND
from genesis.db.crud._id_resolve import PASSTHROUGH as _PASSTHROUGH
from genesis.memory._locks import memory_id_lock
from genesis.memory.classification import classify_memory
from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError
from genesis.memory.linker import MemoryLinker
from genesis.memory.taxonomy import WINGS
from genesis.memory.taxonomy import classify as classify_taxonomy
from genesis.observability.call_site_recorder import record_last_run
from genesis.observability.events import GenesisEventBus
from genesis.observability.provider_activity import track_operation
from genesis.observability.types import Severity, Subsystem
from genesis.qdrant.collections import delete_point, update_payload, upsert_point

# Qdrant connection errors — broad catch for any transport/protocol failure
try:
    from qdrant_client.http.exceptions import (
        ResponseHandlingException,
        UnexpectedResponse,
    )

    _QDRANT_ERRORS: tuple[type[Exception], ...] = (
        UnexpectedResponse,
        ResponseHandlingException,
        ConnectionError,
        TimeoutError,
        OSError,
    )
except ImportError:  # pragma: no cover — safety for minimal installs
    _QDRANT_ERRORS = (ConnectionError, TimeoutError, OSError)

logger = logging.getLogger(__name__)

# _id_resolve.resolve_unique_prefix reads with LIMIT 3; a result at that
# size is a truncated listing, so the candidate set may be incomplete.
_RESOLVER_MATCH_LIMIT = 3


def _strip_kv_prefix(value: str | None, key: str) -> str | None:
    """Strip a leaked ``key=`` / ``key:`` prefix from a taxonomy value.

    Synthesis (dream_cycle) prompts model tags as ``wing={wing}, room={room}``;
    the LLM occasionally echoes the literal ``wing=channels`` form into its JSON
    output. Without this guard that value is stored verbatim into the FTS5 tag,
    the Qdrant payload, and ``memory_metadata.wing`` — polluting the wing
    taxonomy and the L1 "Wings" view. Strips only a leading ``key=``/``key:``.
    """
    if not value:
        return value
    stripped = value.strip()
    for sep in ("=", ":"):
        prefix = f"{key}{sep}"
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    return stripped


_COLLECTION_MAP = {
    "episodic": "episodic_memory",
    "knowledge": "knowledge_base",  # External knowledge → knowledge_base
}


class SupersedeUnresolved(Exception):
    """A supersede could not be performed on the pair it was given.

    Either a handle named no memory or named several, or the pair cannot express
    a correction (a memory replacing itself, or a successor that is itself
    deprecated).

    Raised BEFORE anything is written, so it always describes an operation that
    did not start rather than one that half-finished. Never guess an ambiguous
    handle: two memories sharing a prefix are two different corrections, and
    deprecating the wrong one is unrecoverable without the transcript.
    """

    def __init__(
        self,
        raw_id: str,
        reason: str,
        successor_id: str | None = None,
        candidates: list[str] | None = None,
        truncated: bool = False,
        role: str = "supersedes",
        committed: bool = False,
    ):
        self.raw_id = raw_id
        # "not_found" | "ambiguous" | "self_supersede" | "successor_deprecated"
        # | "successor_expired" | "successor_vanished" | "successor_deleting"
        self.reason = reason
        # Whether a deprecation for this pair had ALREADY committed when this
        # was raised. The message asserts the durable state, so it must not
        # claim "no deprecation was performed" on a post-commit raise — that
        # sentence was hardcoded, and a caller reading it would conclude
        # nothing happened while the row said otherwise.
        self.committed = committed
        # Which parameter the bad handle came from. Without it a caller told
        # "not_found" about its SUCCESSOR would go looking at its target.
        self.role = role
        # The memory that WOULD have become the successor, when one is known.
        # Deliberately optional: validation now runs BEFORE any write, so on the
        # ``store()`` path there is usually no successor yet — and nothing
        # durable at risk, which is why this can be a plain raise.
        self.successor_id = successor_id
        self.candidates = candidates or []
        # The resolver reads with LIMIT 3, so a saturated result is a TRUNCATED
        # listing, not the complete collision set. Saying "matches: a, b, c"
        # about ten colliding memories is the repo's own truncated-read trap.
        self.truncated = truncated
        more = " (possibly more)" if truncated else ""
        detail = (
            f" (matches: {', '.join(self.candidates)}{more})" if self.candidates else ""
        )
        # States only what was CHECKED. The old wording claimed the old memory
        # "is still live in recall", which for `not_found` is exactly the thing
        # resolution just failed to establish.
        outcome = (
            "the deprecation had already committed; its mirror is still outstanding"
            if committed
            else "no deprecation was performed"
        )
        super().__init__(f"{role}={raw_id!r} is {reason}{detail}; {outcome}")


class SupersedeIncomplete(Exception):
    """The supersession HALF-finished: SQLite committed, a mirror did not.

    Raised only by the standalone ``supersede()`` path (strict propagation).
    By the time this raises, *old_id* is deprecated in SQLite with
    ``superseded_by`` recorded — but the named *stage* did not complete:

    * ``"qdrant"`` — the vector payload was not marked deprecated, so vector
      search can keep surfacing the superseded memory until repaired.
    * ``"link"`` — the ``succeeded_by`` graph edge was not created.

    Retrying the same ``supersede()`` call is SAFE and is also the repair:
    the SQLite UPDATE is idempotent, the Qdrant payload write is idempotent,
    and an already-created link is tolerated. The retry takes ``supersede()``'s
    REPAIR PATH, which skips successor validation precisely so the guarantee
    holds even if the successor expired or was deprecated in the meantime —
    that state cannot un-commit the deprecation that already landed, and
    rejecting the retry would strand the mirror this exception exists to get
    repaired. ``store(supersedes=...)`` deliberately stays best-effort — there
    the new content's fate dominates the outcome and is reported instead of
    raised.
    """

    def __init__(self, old_id: str, new_id: str, stage: str):
        self.old_id = old_id
        self.new_id = new_id
        self.stage = stage  # "qdrant" | "link"
        super().__init__(
            f"supersede {old_id!r} -> {new_id!r} half-finished: the SQLite "
            f"deprecation committed, but the {stage} update failed; retrying "
            "the same call is safe and completes the remainder"
        )


class MemoryStore:
    """Full store pipeline: embed -> Qdrant -> FTS5 -> auto-link."""

    def __init__(
        self,
        *,
        embedding_provider: EmbeddingProvider,
        qdrant_client: QdrantClient,
        db: aiosqlite.Connection,
        linker: MemoryLinker | None = None,
        event_bus: GenesisEventBus | None = None,
    ) -> None:
        self._embeddings = embedding_provider
        self._qdrant = qdrant_client
        self._db = db
        # Cache for _anchor_target(); resolved once, lazily, from `db` itself.
        self._anchor_target_cache: tuple[bool, str | None] | None = None
        self._linker = linker
        self._event_bus = event_bus

    async def _anchor_target(self) -> tuple[bool, str | None]:
        """Which database the anchor writer should own a connection to.

        Returns ``(should_write, db_path)``.

        `record_anchors` deliberately takes a PATH rather than this store's
        connection — owning its own connection is what keeps it off the shared
        `SerializedConnection`. But it resolves `genesis_db_path()` when given
        no target, so a store bound to a NON-default database silently enriched
        the live default entity graph while the database the caller named
        stayed unenriched: dangling mentions in one, missing anchors in the
        other, both halves quiet. (Codex P1, PR #1653.)

        The target is DERIVED from the connection rather than passed in
        alongside it. A parameter would be a convention every one of the 24
        construction sites has to remember, and the two that were wrong are
        proof of how that ends; asking the connection cannot drift from it.
        MEASURED on the real `SerializedConnection`: `PRAGMA database_list`
        answers in ~0.32 ms, and it is cached here after the first call.

        An in-memory database reports `''` — no file another connection could
        open — so anchoring is SKIPPED rather than redirected. Falling back to
        `genesis_db_path()` there would write into a database the caller never
        named, which is the whole defect this path exists to prevent.

        UNKNOWN and IN-MEMORY are different answers and degrade differently.
        A reported `''` is knowledge — there is no file — and anchoring is
        skipped. A pragma that fails, or a row this cannot read, is the
        ABSENCE of knowledge, and falls back to `(True, None)`: the historical
        behaviour, so an unexpected shape degrades to what shipped before
        rather than silently dropping every anchor. Collapsing the two is what
        the first version of this did, and it disarmed the seam test above by
        routing its mock connection into the skip branch.

        The unknown answer is NOT cached: a transient failure — a SQLITE_BUSY
        that outlives the retry, a call mid-reconnect — must not pin the
        degraded target for a store that lives as long as the process.
        """
        if self._anchor_target_cache is not None:
            return self._anchor_target_cache

        resolved: str | None = None
        try:
            cursor = await self._db.execute("PRAGMA database_list")
            row = await cursor.fetchone()
            # (seq, name, file). Anything that is not a readable row of that
            # shape is an unknown target, never an in-memory one — note a
            # MagicMock satisfies `len(row)` as 0, which is exactly how the
            # collapsed version routed mocks into "skip".
            if row is not None and len(row) >= 3 and isinstance(row[2], str):
                resolved = row[2]
        except Exception:
            logger.debug(
                "could not resolve the store's database path for anchors; "
                "falling back to the default target",
                exc_info=True,
            )

        if resolved is None:
            return (True, None)

        self._anchor_target_cache = (bool(resolved), resolved or None)
        return self._anchor_target_cache

    @property
    def qdrant_client(self) -> QdrantClient:
        """Public accessor for the QdrantClient instance."""
        return self._qdrant

    @property
    def embedding_provider(self) -> EmbeddingProvider:
        """Public accessor for the EmbeddingProvider instance."""
        return self._embeddings

    @property
    def linker(self) -> MemoryLinker | None:
        """Public access to the memory linker for extraction typed links."""
        return self._linker

    async def store(self, content: str, source: str, **kwargs) -> str:
        """Full store pipeline. Returns memory_id.

        Thin wrapper over :meth:`store_reporting_creation`, kept because the
        memory_id is all that ~47 call sites want and widening a return value
        changes every reader of it. Takes ``**kwargs`` rather than restating
        thirty keyword-only parameters, which would be a second copy to drift.

        Callers that must know whether the id names a NEWLY created memory or
        one deduplication matched — anything that may later COMPENSATE for the
        write — must use :meth:`store_reporting_creation` instead. Deleting a
        deduplicated id destroys a pre-existing memory that other rows point
        at (Codex P1, PR #1653).
        """
        memory_id, _created = await self.store_reporting_creation(
            content, source, **kwargs
        )
        return memory_id

    async def store_reporting_creation(
        self,
        content: str,
        source: str,
        *,
        memory_type: str = "episodic",
        collection: str | None = None,
        tags: list[str] | None = None,
        confidence: float | None = None,
        auto_link: bool = True,
        memory_class: str | None = None,
        source_session_id: str | None = None,
        transcript_path: str | None = None,
        source_line_range: tuple[int, int] | None = None,
        extraction_timestamp: str | None = None,
        source_pipeline: str | None = None,
        wing: str | None = None,
        room: str | None = None,
        force_fts5_only: bool = False,
        valid_at: str | None = None,
        invalid_at: str | None = None,
        source_subsystem: str | None = None,
        life_domain: str | None = None,
        project_type: str | None = None,
        supersedes: str | None = None,
        origin_class: str | None = None,
        speech_act: str | None = None,
        speech_act_confidence: float | None = None,
        assertion_provenance: str | None = None,
        durability: str | None = None,
        expires_at: str | None = None,
        preference_domain: str | None = None,
    ) -> tuple[str, bool]:
        """Full store pipeline: embed -> Qdrant -> FTS5 -> auto-link.

        Returns ``(memory_id, created)``. ``created`` is False when exact-content
        deduplication matched an EXISTING memory, in which case the id names a
        row this call did not write and does not own — compensating for it would
        delete a memory other rows already reference.

        Args:
            collection: Explicit Qdrant collection override. If provided, bypasses
                ``_COLLECTION_MAP`` lookup. Default routing: ``episodic`` types →
                ``episodic_memory``, ``knowledge`` types → ``knowledge_base``.
            source_subsystem: For INTERNAL Genesis-subsystem writers only (ego,
                triage, reflection, autonomy). Forces the write FTS5+metadata-only
                (no Qdrant embed) and excludes it from default recall. Do NOT set
                it for user-sourced content, nor for ``modules/**`` writers — a
                module is an external capability, never a subsystem (see
                modules/base.py; enforced by test_store_subsystem_coverage).
            origin_class: WS-3 provenance taxonomy
                (owner/first_party/external_untrusted). Explicit value wins
                and is validated; when omitted it is DERIVED from
                source_pipeline/source_subsystem/collection via
                ``provenance.derive_origin_class`` and stamped into the
                Qdrant payload, memory_metadata, and (for KB ingest paths)
                knowledge_units.
        """
        # Validate life_domain if explicitly provided
        if life_domain is not None:
            from genesis.memory.taxonomy import LIFE_DOMAINS
            if life_domain not in LIFE_DOMAINS:
                raise ValueError(
                    f"life_domain must be one of {sorted(LIFE_DOMAINS)}, "
                    f"got {life_domain!r}"
                )

        # Dedup: skip if exact content already stored (any collection)
        try:
            existing = await memory_crud.find_exact_duplicate(
                self._db, content=content,
            )
            if existing:
                logger.debug("Skipping duplicate memory store: %s", existing)
                # NOT created by this call: the caller must not compensate it.
                return (existing, False)
        except Exception:
            # Dedup check is best-effort — never block a store on lookup failure
            logger.warning("Dedup check failed, proceeding with store", exc_info=True)

        # Surface form normalization: expand known aliases before embedding
        try:
            from genesis.memory.entity_resolution import normalize_content

            content = normalize_content(content)
        except Exception:
            pass  # best-effort — never block a store on normalization failure

        # Confidence gate: low-confidence → FTS5 only, skip Qdrant
        # Deferred import to break circular: memory.store ↔ perception
        from genesis.perception.confidence import load_config as load_confidence_config
        from genesis.perception.confidence import should_gate

        # force_fts5_only param skips embedding; confidence gate can also set it
        cfg = load_confidence_config()
        gated, gate_msg = should_gate(confidence, cfg.memory_upsertion)
        if gate_msg:
            logger.info("Memory confidence gate: %s", gate_msg)
        if gated:
            force_fts5_only = True

        # Phase 1.5e: automated-subsystem writes (ego corrections, triage
        # signals, reflection observations) bypass Qdrant entirely. They
        # have no current vector-search consumer — default recall filters
        # them out everywhere; explicit `only_subsystem` paths already work
        # via FTS5 alone (verified on prod 2026-05-11). Skipping the embed
        # also avoids queuing for retry — these writes are permanently
        # FTS5+metadata only, not "embed when capacity returns."
        is_subsystem_write = source_subsystem is not None
        if is_subsystem_write:
            force_fts5_only = True

        # Resolve the supersede target BEFORE anything is written. An
        # unresolvable target then costs nothing and leaves nothing behind, and
        # the raise reaches the caller with no half-done operation attached.
        # It also has to be before ``create_metadata``: resolving afterwards let
        # a short prefix match the row this call had just written, deprecating
        # the new memory as its own successor.
        resolved_supersedes = (
            await self._resolve_supersede_target(supersedes) if supersedes else None
        )

        memory_id = str(uuid.uuid4())
        now_iso = datetime.now(UTC).isoformat()
        resolved_tags = tags or []
        resolved_collection = collection or _COLLECTION_MAP.get(memory_type, "episodic_memory")
        # WS-3 origin classification — deferred import matches this
        # function's cycle-avoidance style; validates an explicit override.
        from genesis.memory.provenance import derive_origin_class

        resolved_origin = derive_origin_class(
            origin_class=origin_class,
            source_pipeline=source_pipeline,
            source_subsystem=source_subsystem,
            collection=resolved_collection,
        )
        resolved_class = memory_class or classify_memory(
            content, source=source, source_pipeline=source_pipeline or "",
        )
        # Append class tag for FTS5 discoverability
        class_tag = f"class:{resolved_class}"
        if class_tag not in resolved_tags:
            resolved_tags = [*resolved_tags, class_tag]

        # Defensive: strip a leaked ``wing=`` / ``room=`` prefix before the value
        # fans out to the FTS5 tag, the Qdrant payload, and memory_metadata.
        wing = _strip_kv_prefix(wing, "wing")
        room = _strip_kv_prefix(room, "room")

        # An explicit wing outside the controlled vocabulary is DROPPED, not
        # stored. Until now this branch only tested falsiness, so any string
        # sailed through into the FTS5 `wing:` tag, the Qdrant payload and
        # memory_metadata.wing — and classify_life_domain() silently returns
        # "personal" for an unknown wing, so one bad value corrupted the life
        # domain too. essential_knowledge.py filters junk wings on READ; that
        # hid the problem instead of preventing it.
        #
        # COERCE rather than raise. NB the two sibling controlled-vocabulary
        # fields in this same function RAISE — life_domain just above, and
        # origin_class via derive_origin_class(). The divergence is deliberate:
        # those two are effectively never passed by live callers (life_domain is
        # DERIVED from wing; almost nothing sets it explicitly), whereas `wing`
        # genuinely arrives from model output on live paths (dream_cycle), where
        # raising would abort a whole synthesis run over one bad token. Falling
        # back to auto-classification yields a VALID wing instead of a poisoned
        # one. The agent-facing MCP tools raise instead — a caller that can be
        # told the valid set should be. `room` is deliberately NOT enforced:
        # measured, 18% of live rows have a room outside ROOMS[wing] and the
        # shipped ego prompts instruct room="ego", so enforcing it symmetrically
        # would coerce a fifth of writes and break the ego. It is descriptive,
        # has no read-side filter, and gets no FTS tag — unlike wing, whose bad
        # value also corrupts the derived life_domain.
        if wing and wing not in WINGS:
            logger.warning(
                "Ignoring unknown wing %r (not in the controlled vocabulary); "
                "falling back to auto-classification. Valid wings: %s",
                wing, sorted(WINGS),
            )
            wing = None

        # Taxonomy classification — auto-classify if not explicitly provided
        if not wing or not room:
            taxo = classify_taxonomy(
                content, tags=resolved_tags,
                source=source, source_pipeline=source_pipeline or "",
            )
            wing = wing or taxo.wing
            room = room or taxo.room
            if not life_domain:
                life_domain = taxo.life_domain
        # Derive life_domain from wing if still not set
        if not life_domain:
            from genesis.memory.taxonomy import classify_life_domain
            life_domain = classify_life_domain(wing, tags=resolved_tags)
        # Append wing tag for FTS5 keyword searchability
        wing_tag = f"wing:{wing}"
        if wing_tag not in resolved_tags:
            resolved_tags = [*resolved_tags, wing_tag]
        # Append life_domain tag for FTS5 searchability
        ld_tag = f"life_domain:{life_domain}"
        if ld_tag not in resolved_tags:
            resolved_tags = [*resolved_tags, ld_tag]
        # Append project_type tag (when set) so the FTS5-only fallback path
        # preserves it — the recovery worker re-hydrates the Qdrant payload key
        # from this tag on re-embed (see embedding_recovery.py). Mirrors the
        # wing/life_domain tags above; #975 indexed project_type for faceted
        # recall, so a re-embedded point lacking it would silently drop out of
        # project_type= filtered recall. The payload key below is still set on
        # the happy path; the tag is the breadcrumb for the fallback path.
        if project_type:
            pt_tag = f"project_type:{project_type}"
            if pt_tag not in resolved_tags:
                resolved_tags = [*resolved_tags, pt_tag]

        embedding_ok = not force_fts5_only
        if embedding_ok:
            try:
                enriched = EmbeddingProvider.enrich(content, memory_type, resolved_tags)
                vector = await self._embeddings.embed(enriched)

                await record_last_run(
                    self._db, "21_embeddings",
                    provider="embedding", model_id="qwen3-embedding",
                    response_text=f"Embedded {len(enriched)} chars → {len(vector)}d vector",
                )

                with track_operation(self._embeddings.tracker, "qdrant.upsert"):
                    payload = {
                        "content": content,
                        "source": source,
                        "memory_type": memory_type,
                        "tags": resolved_tags,
                        "confidence": confidence if confidence is not None else 0.5,
                        "created_at": now_iso,
                        "retrieved_count": 0,
                        "source_type": "memory",
                        "scope": "external" if resolved_collection == "knowledge_base" else "user",
                        "memory_class": resolved_class,
                        "wing": wing,
                        "room": room,
                        "life_domain": life_domain,
                        # Always non-None by construction (derived above) —
                        # lives in the base dict, not the conditional block.
                        "origin_class": resolved_origin,
                    }
                    # Provenance fields — trace memory back to source conversation
                    if source_session_id:
                        payload["source_session_id"] = source_session_id
                    if transcript_path:
                        payload["transcript_path"] = transcript_path
                    if source_line_range:
                        payload["source_line_range"] = list(source_line_range)
                    if extraction_timestamp:
                        payload["extraction_timestamp"] = extraction_timestamp
                    if source_pipeline:
                        payload["source_pipeline"] = source_pipeline
                    if source_subsystem:
                        payload["source_subsystem"] = source_subsystem
                    if project_type:
                        payload["project_type"] = project_type

                    # Sync Qdrant HTTP call — off the event loop so a slow
                    # round-trip on this hot store() path doesn't stall every
                    # other coroutine (background paths already do this; see
                    # memory/health.py, dream_cycle.py, entity_resolution.py).
                    await asyncio.to_thread(
                        upsert_point,
                        self._qdrant,
                        collection=resolved_collection,
                        point_id=memory_id,
                        vector=vector,
                        payload=payload,
                    )
            except EmbeddingUnavailableError:
                embedding_ok = False
                logger.warning(
                    "Embedding unavailable for memory %s, falling back to FTS5-only storage",
                    memory_id,
                )
            except _QDRANT_ERRORS:
                embedding_ok = False
                logger.error(
                    "Qdrant connection error storing memory %s — falling back to FTS5-only",
                    memory_id,
                    exc_info=True,
                )
            except Exception:
                embedding_ok = False
                logger.error(
                    "Unexpected error during vector storage for memory %s — falling back to FTS5-only",
                    memory_id,
                    exc_info=True,
                )

        # Always write to FTS5 — include tags for keyword searchability
        await memory_crud.upsert(
            self._db,
            memory_id=memory_id,
            content=content,
            source_type="memory",
            tags=" ".join(resolved_tags) if resolved_tags else "",
            collection=resolved_collection,
        )

        # Write companion metadata (timestamps, confidence, embedding status).
        # Subsystem writes get embedding_status='fts5_only' to distinguish them
        # from confidence-gated 'pending' rows that should be retried later.
        if is_subsystem_write:
            embed_status = "fts5_only"
        elif embedding_ok:
            embed_status = "embedded"
        else:
            embed_status = "pending"
        await memory_crud.create_metadata(
            self._db,
            memory_id=memory_id,
            created_at=now_iso,
            collection=resolved_collection,
            confidence=confidence,
            embedding_status=embed_status,
            memory_class=resolved_class,
            wing=wing,
            room=room,
            valid_at=valid_at,
            invalid_at=invalid_at,
            source_subsystem=source_subsystem,
            origin_class=resolved_origin,
            # MW-1 Tier-0 judgment axes — write-only to memory_metadata
            # (consumers MW-4/MW-5). NOT denormalized into the Qdrant payload:
            # the field is reachable at recall via the search_ranked metadata
            # join (as origin_class's FTS fallback is), and a consumer that
            # needs vector-path parity owns adding it to the payload + the
            # re-embed recovery path together.
            # GROUNDWORK(mw-4-provenance-weight / mw-4-durability-ttl
            # / mw-4-preference-domain / mw-5-speech-act-protection)
            speech_act=speech_act,
            speech_act_confidence=speech_act_confidence,
            assertion_provenance=assertion_provenance,
            durability=durability,
            expires_at=expires_at,
            preference_domain=preference_domain,
        )

        # Mechanical code anchors (entity layer) — regex-only, every write
        # path. Failure-isolated: a broken anchor write must never break
        # the store itself. record_anchors owns its own connection + txn (it no
        # longer writes on self._db), so the metadata write above must already be
        # committed here — it is (create_metadata commits) — or the owned conn's
        # BEGIN IMMEDIATE would deadlock against this coroutine's own open txn.
        #
        # Owning the connection means it also needs the TARGET — see
        # _anchor_target(), which derives it from `self._db` so it cannot drift
        # from the database this store is actually bound to.
        try:
            from genesis.memory.entity_anchors import record_anchors

            should_anchor, anchor_db_path = await self._anchor_target()
            if should_anchor:
                await record_anchors(memory_id, content, db_path=anchor_db_path)
            else:
                logger.debug(
                    "skipping anchors for %s: the store's database reports no "
                    "file the anchor writer could own (an in-memory database)",
                    memory_id,
                )
        except Exception:
            logger.debug(
                "entity anchor extraction failed for %s",
                memory_id, exc_info=True,
            )

        if not embedding_ok and not is_subsystem_write:
            # Queue for later embedding — preserve provenance so the recovery
            # worker can reconstruct the full Qdrant payload.
            # Subsystem writes are FTS5-only permanently, never queue.
            await pending_embeddings.create(
                self._db,
                id=str(uuid.uuid4()),
                memory_id=memory_id,
                content=content,
                memory_type=memory_type,
                collection=resolved_collection,
                created_at=now_iso,
                tags=",".join(resolved_tags) if resolved_tags else None,
                source=source,
                confidence=confidence,
                source_session_id=source_session_id,
                transcript_path=transcript_path,
                source_line_range=(
                    f"{source_line_range[0]},{source_line_range[1]}"
                    if source_line_range else None
                ),
                extraction_timestamp=extraction_timestamp,
                source_pipeline=source_pipeline,
                source_subsystem=source_subsystem,
            )
            if self._event_bus:
                await self._event_bus.emit(
                    Subsystem.MEMORY,
                    Severity.WARNING,
                    "memory.embedding_skipped",
                    f"Embedding unavailable, memory {memory_id} stored FTS5-only",
                    memory_id=memory_id,
                )
        elif embedding_ok and auto_link and self._linker:
            await self._linker.auto_link(memory_id, vector, collection=resolved_collection)
        # else: subsystem write — no pending_embeddings, no auto_link
        # (vector wasn't computed; link graph isn't consumed for filtered content)

        # Supersession: mark old memory as deprecated, link to this one. The
        # target was resolved and confirmed to exist before any of the writes
        # above, so anything that fails here is an infrastructure fault rather
        # than a bad handle.
        if resolved_supersedes:
            try:
                await self._mark_superseded(resolved_supersedes, memory_id, now_iso)
            except Exception:
                logger.warning(
                    "Failed to mark memory %s as superseded by %s",
                    resolved_supersedes, memory_id, exc_info=True,
                )

        return (memory_id, True)

    async def supersede(
        self,
        old_handle: str,
        new_handle: str,
        *,
        timestamp: str | None = None,
    ) -> None:
        """Deprecate *old_handle*, recording *new_handle* as its correction.

        The supersede as its own operation, rather than a side effect of
        storing. That distinction is what makes this simple: BOTH ids are named
        by the caller, so both can be resolved and validated up front, and there
        is no content being written whose fate has to be reported alongside the
        outcome. Every REJECTION is a precondition failure — nothing is
        mutated, and the caller can retry for free — so this raises rather
        than returning a verdict to interpret.

        The one non-precondition failure is ``SupersedeIncomplete``: the
        SQLite deprecation committed but a mirror (the Qdrant payload, the
        ``succeeded_by`` link) did not. This path RAISES it rather than
        swallowing it the way ``store(supersedes=...)`` must, because here the
        caller named both ids and retrying the same call is both safe and the
        repair. Both ids are locked (sorted order) from validation through the
        mirror writes, so a concurrent delete or supersession of the successor
        cannot slip between the check and the write.

        A retry takes the REPAIR PATH: when SQLite already records this exact
        supersession, the successor is NOT re-validated. Validation guards the
        creation of a supersession, and re-applying it to one that already
        committed would reject the retry whenever the successor expired or was
        deprecated in between — leaving the stale mirror in place, which is the
        opposite of what the retry is for.

        Contrast ``store(supersedes=...)``, where the successor is whatever that
        call produces: the caller never names it, cannot see it, and a failure
        after the content lands leaves a genuinely partial outcome.
        """
        old_id = await self._resolve_supersede_target(old_handle)
        try:
            new_id = await self._resolve_supersede_target(new_handle, role="new_id")
        except SupersedeUnresolved:
            # A committed supersession still owes its mirror, and that debt does
            # NOT depend on the successor still resolving. Resolution runs
            # before the repair-path check below, so without this the documented
            # repair is unreachable in exactly the case it exists for: the
            # successor row disappears between the failed attempt and the retry,
            # and the retry dies here — stranding the vector permanently, since
            # `integrity.py` counts `deprecated_divergence` but nothing repairs
            # it. Recover the successor from the row that already committed.
            existing = await memory_crud.get_metadata(self._db, old_id)
            if not (existing and existing["deprecated"] and existing["superseded_by"]):
                raise
            new_id = existing["superseded_by"]
        # Self-check BEFORE the locks: sorted() of an equal pair would acquire
        # the same asyncio lock twice and deadlock. _validate_supersede_pair
        # re-checks harmlessly (reads only).
        if old_id == new_id:
            raise SupersedeUnresolved(old_id, "self_supersede", new_id)
        # Both ids, in sorted order — a total acquisition order cannot cycle,
        # which is the one sanctioned relaxation of _locks.py's
        # one-lock-per-holder invariant (see that module's docstring). Holding
        # both through validate + mark closes the validate→mark window: a
        # delete or supersession of new_id can no longer commit in between and
        # leave this supersession pointing at a missing/deprecated successor.
        first, second = sorted((old_id, new_id))
        async with memory_id_lock(first), memory_id_lock(second):
            # REPAIR PATH. If this exact supersession already committed in
            # SQLite, this call is a RETRY after a mirror failure, and
            # re-validating the successor would strand the very mirror the
            # retry exists to repair: a successor that expired or was
            # deprecated since the first attempt cannot un-commit what already
            # landed, but it WOULD fail validation. The repair guarantee that
            # SupersedeIncomplete states has to hold unconditionally, or it is
            # not a guarantee — so the check that guards a NEW supersession is
            # skipped for one that is merely being completed.
            existing = await memory_crud.get_metadata(self._db, old_id)
            resuming = (
                existing is not None
                and bool(existing["deprecated"])
                and existing["superseded_by"] == new_id
            )
            if resuming:
                # Keep the ORIGINAL timestamp: the supersession happened when
                # it first committed. A retry repairs mirrors; it does not
                # re-date the event.
                stamp = existing["superseded_at"] or (
                    timestamp or datetime.now(UTC).isoformat()
                )
            else:
                await self._validate_supersede_pair(old_id, new_id)
                stamp = timestamp or datetime.now(UTC).isoformat()
            await self._mark_superseded(old_id, new_id, stamp, strict=True)

    async def _validate_supersede_pair(self, old_id: str, new_id: str) -> None:
        """Reject a pair that cannot express a correction. Reads only.

        Both checks are about the SUCCESSOR, and both are pre-write:

        * A memory cannot replace itself. It would deprecate the only copy and
          record it as its own correction — the content survives in the table
          but is filtered out of recall, and nothing in the result says so.
        * The successor cannot itself be deprecated. Normal recall filters
          deprecated rows, so superseding onto one deprecates the target and
          leaves the correction unreachable: both halves gone.
        * The successor cannot already be temporally invalid. Recall's
          bitemporal filter (``invalid_at IS NULL OR invalid_at > as_of``,
          crud/memory.py) hides an expired memory exactly like a deprecated
          one — same unreachable-correction outcome, different column.

        Successor failures are attributed to ``new_id`` (the argument at
        fault), not to the old handle the caller got right.
        """
        if old_id == new_id:
            raise SupersedeUnresolved(old_id, "self_supersede", new_id)
        meta = await memory_crud.get_metadata(self._db, new_id)
        if meta is None:
            # NOT the same state as deprecated, and it has a different remedy:
            # the row was resolved before the lock and is gone now, so it
            # disappeared underneath this call rather than being a bad choice
            # of successor. Same misattribution class the `role` field fixed.
            raise SupersedeUnresolved(
                new_id, "successor_vanished", new_id, role="new_id"
            )
        if meta["deprecated"]:
            raise SupersedeUnresolved(
                new_id, "successor_deprecated", new_id, role="new_id"
            )
        # A successor awaiting a DEFERRED delete passes every column check: the
        # deferred path returns before `delete_metadata`, leaving the row intact
        # with deprecated=0 and invalid_at NULL, and only the open tombstone as
        # the signal. Superseding onto it would deprecate the target and then
        # lose the successor when the reconcile lane drains — the exact
        # both-halves-gone harm these checks exist to prevent.
        # `embedding_recovery` already refuses to act on a tombstoned memory.
        from genesis.memory.delete_tombstones import has_open_tombstone

        if await has_open_tombstone(self._db, memory_id=new_id):
            raise SupersedeUnresolved(
                new_id, "successor_deleting", new_id, role="new_id"
            )
        invalid_at = meta["invalid_at"]
        if invalid_at is not None and invalid_at <= datetime.now(UTC).isoformat():
            raise SupersedeUnresolved(
                new_id, "successor_expired", new_id, role="new_id"
            )

    async def _resolve_supersede_target(
        self, handle: str, *, role: str = "supersedes"
    ) -> str:
        """Resolve a memory handle to exactly one EXISTING memory id.

        *role* names the parameter in the error, so a caller told
        ``new_id=... is not_found`` is not left thinking its supersede target
        was the problem.

        Reads only — it writes nothing and must be called BEFORE the store does,
        so an unresolvable target costs nothing and leaves nothing behind. That
        ordering is load-bearing twice over:

        * The old code resolved AFTER ``create_metadata`` had written the new
          row, so a short prefix could match the memory being written and the
          call would deprecate it as its own successor. Resolving first makes
          that unrepresentable — the row does not exist yet — rather than
          needing a guard against it.
        * Nothing durable is at risk when this raises, so the caller can simply
          be told, instead of being handed a half-completed operation to
          reason about.

        The proactive hook prints memories as ``id:<8-char>`` and
        ``memory_expand`` resolves those on the read side, so the ecosystem
        teaches the short form; this path used to feed it straight into an
        exact-match UPDATE that matched nothing.
        """
        matches, outcome = await memory_crud.resolve_id(self._db, handle)
        if outcome == _AMBIGUOUS:
            raise SupersedeUnresolved(
                handle, "ambiguous", candidates=matches,
                truncated=len(matches) >= _RESOLVER_MATCH_LIMIT, role=role,
            )
        if outcome == _NOT_FOUND:
            raise SupersedeUnresolved(handle, "not_found", role=role)
        resolved = matches[0]

        # PASSTHROUGH ids (full length, or non-hex) are returned unverified by
        # the shared resolver — it never looked them up. Confirm existence HERE
        # so every rejection happens before the write; otherwise a full-length
        # id naming no memory would only be caught by the UPDATE's rowcount,
        # which is after the new memory has been stored.
        if outcome == _PASSTHROUGH and await memory_crud.get_metadata(
            self._db, resolved
        ) is None:
            raise SupersedeUnresolved(handle, "not_found", role=role)
        return resolved

    async def _mark_superseded(
        self,
        old_id: str,
        new_id: str,
        timestamp: str,
        *,
        strict: bool = False,
    ) -> None:
        """Mark *old_id* as superseded by *new_id* in both SQLite and Qdrant.

        *strict* chooses what a MIRROR failure (Qdrant payload, succeeded_by
        link) does after the SQLite deprecation has committed: ``False`` (the
        ``store(supersedes=...)`` path) logs and swallows it — the new
        content's fate dominates that call's outcome; ``True`` (the standalone
        ``supersede()`` path) raises ``SupersedeIncomplete``, because there
        the caller named both ids and a retry of the same call is the repair.
        Success must not be reported over a stale vector mirror — recall
        excludes a memory from vector search only through the Qdrant
        ``deprecated`` payload.

        *old_id* must ALREADY be resolved and known to exist — see
        ``_resolve_supersede_target``, which the caller runs before any write.

        Sets ``deprecated=1``, ``superseded_by``, and ``superseded_at`` in
        SQLite.  Sets ``deprecated=True`` and ``merged_into`` in the Qdrant
        payload.  Creates a ``succeeded_by`` link from old to new.
        """
        # SQLite: mark deprecated + record successor (via CRUD module).
        # The return value says whether the row was FOUND — discarding it was
        # how an unresolvable id became a silent no-op. `_resolve_supersede_target`
        # has already established that the row exists, so reaching this branch
        # means it was deleted in the window between the two — a race, not a bad
        # handle. Kept because a silent no-op here is the original defect.
        if not await memory_crud.mark_superseded(self._db, old_id, new_id, timestamp):
            raise SupersedeUnresolved(old_id, "not_found", new_id)

        # Qdrant: LOCATE the point, then update where it actually lives.
        # Only touch Qdrant when a vector exists at all (status 'embedded').
        # 'fts5_only' (subsystem write), 'pending' (embed queued), and 'failed'
        # (embed gave up) rows have NO point — the old `!= "fts5_only"` form
        # fired a doomed update_payload on 'pending'/'failed' rows every time.
        #
        # The metadata `collection` column is UNRELIABLE (crud/memory.py:72-73),
        # and `delete()` has always known it: it locates the point by id across
        # both collections rather than trusting the column (see its docstring
        # and step 1). Trusting it here meant an embedded point living in the
        # other collection got a set_payload to the stale name, which SUCCEEDS
        # as a no-op on a nonexistent point — so supersede reported success
        # while the memory stayed eligible for vector recall. A write that
        # cannot fail is not evidence that it landed.
        meta = await memory_crud.get_metadata(self._db, old_id)
        if meta and meta["embedding_status"] == "embedded":
            try:
                present: list[str] = []
                for coll in ("episodic_memory", "knowledge_base"):
                    got = await asyncio.to_thread(
                        self._qdrant.retrieve,
                        collection_name=coll,
                        ids=[old_id],
                        with_payload=False,
                        with_vectors=False,
                    )
                    if got:
                        present.append(coll)
                for coll in present:
                    await asyncio.to_thread(
                        update_payload,
                        self._qdrant,
                        collection=coll,
                        point_id=old_id,
                        payload={"deprecated": True, "merged_into": new_id},
                    )
            except Exception as qdrant_exc:
                logger.warning(
                    "Qdrant mirror failed for superseded memory %s",
                    old_id, exc_info=True,
                )
                if strict:
                    raise SupersedeIncomplete(
                        old_id, new_id, "qdrant"
                    ) from qdrant_exc

        # Create succeeded_by link for graph traversal
        try:
            await memory_links_crud.create(
                self._db,
                source_id=old_id,
                target_id=new_id,
                link_type="succeeded_by",
                strength=1.0,
                created_at=timestamp,
            )
        except Exception as link_exc:
            # PK collision is fine (link already exists); log unexpected errors
            if "UNIQUE constraint" not in str(link_exc):
                logger.warning(
                    "Failed to create succeeded_by link %s → %s: %s",
                    old_id, new_id, link_exc,
                )
                if strict:
                    raise SupersedeIncomplete(
                        old_id, new_id, "link"
                    ) from link_exc
        else:
            # The CRUD create does not invalidate (its callers do, by
            # convention) — and this caller previously didn't either, so every
            # supersede left the cached graph missing the succeeded_by edge
            # until some unrelated write invalidated it. One of the two known
            # invalidation gaps from the graph-store consumer map (issue #1641).
            # Scope: this flips THIS process's projection (the MCP child, which
            # hosts the traverse readers most affected); cross-process readers
            # rebuild on their own invalidations — the DB-generation token that
            # closes that fully is future seam work (#1641).
            try:
                from genesis.memory.graph import invalidate_graph_cache

                invalidate_graph_cache()
            except ImportError:
                pass

    async def delete(self, memory_id: str) -> dict:
        """Delete a memory from all layers. Returns per-layer status.

        **Point-first, fail-closed, atomic on the point's real collection.** The
        metadata ``collection`` column is unreliable (memory.py:72-73), so the
        point is first LOCATED by a by-id retrieve against both collections, then
        deleted only from the collection(s) that actually hold it — never a blind
        delete against both (a transient failure on the collection that did NOT
        hold the point would otherwise strand the one that did). A ``retrieve`` or
        ``delete`` that raises means Qdrant is unreachable → **defer**: touch no
        SQLite layer and return ``deferred=True`` so the memory stays coherent and
        retryable rather than leaving an orphaned "ghost" point. A memory that
        never had a vector (``fts5_only``/``pending``/``failed``) locates to
        nothing and deletes cleanly.

        Serialized per ``memory_id`` against the re-embed/reconcile-requeue paths
        (``memory/_locks.py``): the whole locate→delete→cascade sequence holds the
        id-lock, so a concurrent ``EmbeddingRecoveryWorker`` upsert or reconcile
        requeue of the same memory cannot interleave and resurrect a vector the
        user just deleted.
        """
        async with memory_id_lock(memory_id):
            return await self._delete_locked(memory_id)

    async def _delete_locked(self, memory_id: str) -> dict:
        results: dict[str, bool | int] = {}

        # 0. WRITE-AHEAD delete intent (Codex #1270 P1): record the tombstone
        # BEFORE touching Qdrant, not just on the defer path. The intent stays
        # open across the whole point-delete→cascade span, so the recovery
        # worker's tombstone check (SQLite, cross-process) blocks a concurrent
        # re-embed from resurrecting the vector even when THIS delete succeeds
        # — the process-local id-lock cannot serialize the MCP process. Also
        # crash-safe: a process dying mid-cascade leaves the intent for the
        # nightly drain to complete. Best-effort — a failed write never blocks
        # the delete itself. Step 7 closes it on success; a defer leaves it
        # open as the durable retry record.
        from genesis.memory.delete_tombstones import (
            complete_open_tombstones,
            enqueue_tombstone,
        )

        tombstoned = await enqueue_tombstone(
            self._db, memory_id=memory_id, reason="delete_in_progress"
        )

        # 1. Locate the point (unreliable collection column → check both) and
        # delete only where it lives. retrieve/delete raise on a real Qdrant error
        # → defer; an empty locate means the point is absent (nothing to delete).
        try:
            present: list[str] = []
            for coll in ("episodic_memory", "knowledge_base"):
                got = await asyncio.to_thread(
                    self._qdrant.retrieve,
                    collection_name=coll,
                    ids=[memory_id],
                    with_payload=False,
                    with_vectors=False,
                )
                if got:
                    present.append(coll)
            for coll in present:  # atomic on the point's real collection
                await asyncio.to_thread(
                    delete_point, self._qdrant, collection=coll, point_id=memory_id,
                )
        except Exception:
            logger.error(
                "Qdrant unavailable during delete of %s — deferring to keep stores "
                "consistent (no orphan)", memory_id, exc_info=True,
            )
            # The write-ahead tombstone (step 0) stays OPEN — it is the durable
            # retry record: visible to every process, it blocks requeue/re-embed
            # of this memory and is drained (delete re-attempted) by the nightly
            # reconcile lane.
            results["deferred"] = True
            results["tombstoned"] = tombstoned
            results["metadata"] = False
            results["fts5"] = False
            return results
        results["qdrant_deleted"] = len(present)

        # 2. memory_metadata companion table (point confirmed gone → safe)
        results["metadata"] = await memory_crud.delete_metadata(
            self._db, memory_id=memory_id,
        )

        # 3. FTS5 text index
        results["fts5"] = await memory_crud.delete(
            self._db, memory_id=memory_id,
        )

        # 4. Cascade: memory_links
        results["links_deleted"] = await memory_links_crud.delete_by_memory(
            self._db, memory_id=memory_id,
        )
        if results["links_deleted"]:
            from genesis.memory.graph import invalidate_graph_cache
            invalidate_graph_cache()

        # 5. Cascade: pending_embeddings
        results["pending_deleted"] = await pending_embeddings.delete_by_memory(
            self._db, memory_id=memory_id,
        )

        # 6. Cascade: entity_mentions (written keyed by memory_id on the store
        # path via record_anchors -> upsert_mention; without this a deleted
        # memory leaves dangling mentions pointing at a gone memory_id).
        try:
            results["mentions_deleted"] = await entities_crud.delete_mentions_by_memory(
                self._db, memory_id=memory_id,
            )
        except Exception:
            logger.error(
                "entity_mentions delete failed for %s", memory_id, exc_info=True,
            )
            results["mentions_deleted"] = False

        # 7. Close every open delete-intent tombstone: the cascade completed
        # (this covers step 0's write-ahead intent, any earlier deferred
        # attempt's row, and multi-process duplicates), so the recorded intent
        # is satisfied — leaving it open would only make the reconcile lane
        # re-attempt a no-op delete. Best-effort.
        try:
            await complete_open_tombstones(self._db, memory_id=memory_id)
        except Exception:
            logger.warning(
                "tombstone close-out failed for %s", memory_id, exc_info=True,
            )

        return results
