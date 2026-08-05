"""Re-embed procedures whose ``principle_embedding`` is STALE (pre-#1277 repair).

Before #1277, ``store_procedure_checked``'s refine branch overwrote a matched
row's ``principle``/``steps`` but LEFT ``principle_embedding`` describing the
PREVIOUS principle. On a row refined more than once (``version > 1``) the stored
vector can therefore describe an older lesson than the row's current text.

That stale vector is not cosmetic: the #1277 identity fix keys "same procedure"
off ``principle_embedding`` cosine, so a legacy row whose embedding still points
at an old principle can mis-match an incoming DISTINCT lesson and overwrite it
(the very data loss #1277 set out to stop) until a post-fix refine happens to
heal the vector. #1277 fixed the MECHANISM going forward; this heals the rows the
mechanism already left stale — data repair made durable as a data-migration so
every lagging install self-heals on its next boot.

Scope: only ``version > 1`` rows can be stale — a ``version == 1`` row was never
refined, so its creation-time embedding matches its principle. A NULL embedding
is the promoter's hourly ``backfill_missing_embeddings`` job, not this one.

Staleness is detected by RE-EMBEDDING the current principle and comparing to the
stored vector: identical text through the same provider yields cosine ~1.0
(deterministic / cached), so anything below ``FRESH_MATCH_THRESHOLD`` means the
stored vector describes a different (older) principle.

Sync (the data-migration runner offloads via ``asyncio.to_thread``); opens its
OWN sqlite connections. Network embeds run FIRST, then quick batched UPDATEs, so
the embed round-trips never interleave with a held write lock on the shared
server connection. Fail-CLOSED on an embedder outage: raises, so the one-time
heal is retried next boot rather than recording success while rows stay stale
(the opposite of the hourly NULL-backfill, which fails open).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from genesis.db.data_migrations._util import commit_in_batches
from genesis.env import genesis_db_path
from genesis.learning.procedural.embedding import (
    cosine_similarity,
    pack_embedding,
    unpack_embedding,
)

logger = logging.getLogger(__name__)

# A freshly-embedded principle vs its OWN stored vector: same text, same
# provider => cosine ~1.0. Below this the stored vector describes a different
# (older) principle => stale. Well clear of the ~0.85 same-procedure bar and of
# genuine distinct-lesson cosines, so it cleanly separates fresh from stale.
FRESH_MATCH_THRESHOLD = 0.99


def _candidates(conn: sqlite3.Connection) -> list[tuple[str, str, bytes | None]]:
    """Rows a pre-fix refine could have left stale: ``version > 1`` with a
    non-empty principle. Returns ``(id, principle, stored_embedding)``.

    Skips ``deprecated`` rows (permanently dead — never a match candidate) but
    INCLUDES quarantined ones: quarantine is reversible, and an un-quarantined
    row rejoins the identity-match set (``list_by_task_type`` gates on both
    flags), so its embedding must not be left stale. A one-time migration won't
    re-run, so healing them now closes that gap up front."""
    cur = conn.execute(
        "SELECT id, principle, principle_embedding FROM procedural_memory "
        "WHERE version > 1 AND deprecated = 0 "
        "AND principle IS NOT NULL AND TRIM(principle) != ''"
    )
    return [(r[0], r[1], r[2]) for r in cur.fetchall()]


async def _aclose_provider(provider) -> None:
    """Best-effort close of a migration-local provider's backend httpx clients on
    THIS loop (the composite ``EmbeddingProvider`` has no ``aclose()``). Defensive:
    a no-op if the internal shape changes, and never fails the migration."""
    for backend in getattr(provider, "_backends", None) or []:
        aclose = getattr(getattr(backend, "_client", None), "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - cleanup must never fail the migration
                logger.debug("reembed_stale: backend client close failed", exc_info=True)


def _embed_texts(texts: list[str], provider) -> list[list[float]]:
    """Embed all texts on ONE ``asyncio.run`` loop.

    With no injected provider, construct a migration-LOCAL ``EmbeddingProvider``
    created, used, AND closed on THIS loop — never the shared
    ``get_embedding_provider()`` singleton, whose httpx connection pools are bound
    to the server's main loop: driving it from this throwaway worker-thread loop
    (or leaving a pooled connection bound to it) would break the migration or a
    later live embed (Codex P2 on #1286). Fail-CLOSED: an unavailable embedder
    surfaces as ``RuntimeError``.
    """
    if not texts:
        return []
    from genesis.memory.embeddings import EmbeddingProvider, EmbeddingUnavailableError

    async def _run() -> list[list[float]]:
        prov = provider
        owns = prov is None
        if owns:
            prov = EmbeddingProvider()
        try:
            return [await prov.embed(t) for t in texts]
        finally:
            if owns:
                await _aclose_provider(prov)

    try:
        return asyncio.run(_run())
    except EmbeddingUnavailableError as e:
        raise RuntimeError(f"embedder unavailable; cannot re-embed stale procedures: {e}") from e


def _classify(candidates, provider) -> tuple[list[tuple[str, bytes, bytes | None]], list[str]]:
    """Split candidates into (writable-stale, unresolvable).

    - ``writable`` = ``(id, fresh_blob, stored_blob)`` for rows whose stored
      vector is missing or below ``FRESH_MATCH_THRESHOLD`` vs a fresh embed of
      the CURRENT principle. ``stored_blob`` is the T0 read value, carried so the
      write can compare-and-swap against it (see ``reembed_...``).
    - ``unresolvable`` = ids whose fresh vector has an unexpected dimensionality
      (``pack_embedding`` raised). We refuse to write a bad blob, but such a row
      must NOT be treated as fresh — it stays counted as stale so ``verify()``
      keeps failing (visible, retried) instead of silently marking done.

    Embeds every candidate principle up front (network I/O). Fail-CLOSED: an
    ``EmbeddingUnavailableError`` propagates out of ``asyncio.run`` so the caller
    aborts rather than half-healing.
    """
    if not candidates:
        return [], []
    texts = [c[1] for c in candidates]
    vectors = _embed_texts(texts, provider)
    writable: list[tuple[str, bytes, bytes | None]] = []
    unresolvable: list[str] = []
    for (pid, _principle, stored), vec in zip(candidates, vectors, strict=True):
        try:
            fresh_blob = pack_embedding(vec)
        except ValueError:
            logger.warning("reembed_stale: bad vector dim for %s; cannot heal/verify", pid)
            unresolvable.append(pid)
            continue
        stored_vec = unpack_embedding(stored)
        if stored_vec is None or cosine_similarity(stored_vec, list(vec)) < FRESH_MATCH_THRESHOLD:
            writable.append((pid, fresh_blob, stored))
    return writable, unresolvable


def count_stale_procedure_embeddings(conn: sqlite3.Connection, provider=None) -> int:
    """Count ``version > 1`` rows whose stored embedding is stale/missing/unverifiable.

    Used by the migration's ``verify()``. Unresolvable (bad-dimension) rows are
    counted as stale so ``verify()`` does not pass while they remain unverified.
    Raises ``RuntimeError`` if no embedder is configured — the caller cannot
    confirm freshness and should treat the migration as not-yet-done.
    """
    candidates = _candidates(conn)
    if not candidates:
        return 0
    writable, unresolvable = _classify(candidates, provider)
    return len(writable) + len(unresolvable)


def reembed_stale_procedure_embeddings(
    db_path: str | None = None, *, provider=None, dry_run: bool = False
) -> dict:
    """Re-embed ``version > 1`` procedures whose stored embedding is stale.

    Sync; opens its own read + write sqlite connections. Clean no-op (no provider
    needed) when there are no ``version > 1`` rows. Otherwise fail-CLOSED: raises
    ``RuntimeError`` if the embedder is unavailable, so a one-time heal is retried
    next boot rather than recording success with rows left stale.

    Concurrency-safe against a live refine: the write is a compare-and-swap on the
    T0 stored embedding, so if a genuine refine lands on a targeted row between the
    candidate read and this write, its fresh (correct) embedding is left intact and
    this stale-principle write is skipped instead of clobbering it.
    """
    db_path = db_path or genesis_db_path()

    read = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        candidates = _candidates(read)
    finally:
        read.close()

    if not candidates:
        return {"targeted": 0, "stale": 0, "unresolvable": 0, "reembedded": 0}

    writable, unresolvable = _classify(candidates, provider)  # migration-local embeds happen here
    summary = {
        "targeted": len(candidates),
        "stale": len(writable),
        "unresolvable": len(unresolvable),
        "reembedded": 0,
    }
    if dry_run or not writable:
        return summary

    changed = [0]
    write = sqlite3.connect(db_path, timeout=30.0)
    try:

        def _apply(conn: sqlite3.Connection, item: tuple[str, bytes, bytes | None]) -> None:
            # Compare-and-swap on the T0 stored blob (``IS`` is NULL-safe): if a
            # concurrent refine changed the row since it was read, its fresh
            # embedding is already correct — 0 rows match, and this stale-principle
            # write is skipped rather than clobbering it.
            cur = conn.execute(
                "UPDATE procedural_memory SET principle_embedding = ? "
                "WHERE id = ? AND principle_embedding IS ?",
                (item[1], item[0], item[2]),
            )
            changed[0] += cur.rowcount

        commit_in_batches(write, writable, _apply)
    finally:
        write.close()

    summary["reembedded"] = changed[0]
    logger.info(
        "reembed_stale_procedure_embeddings: %d/%d re-embedded "
        "(%d skipped by concurrent refine, %d unresolvable)",
        changed[0],
        summary["targeted"],
        len(writable) - changed[0],
        len(unresolvable),
    )
    return summary
