"""WS-3 B1 emit layer — record what an immunity gate WOULD block, in shadow.

This is the thin, best-effort bridge the recall/inject sites call. It is the
ONE place the never-block invariant + kill switch are enforced for shadow
recording:

- read :func:`genesis.security.immunity.gate_mode` LIVE — a master
  ``enabled=false`` or a per-gate ``off`` short-circuits with no write, in
  process, no restart;
- honor the never-block invariant via
  :func:`genesis.security.immunity.is_blockable` — owner/first_party origins
  NEVER produce a row, in any mode;
- write at most ONE row per recall event (the site passes a blockable-item
  count), so hot paths pay a single INSERT, not one-per-item.

Two writer flavors mirror the two runtime contexts:

- :func:`record_would_block` (async) — genesis-server recall/inject sites. If
  the caller has a live ``aiosqlite`` handle (the MCP recall tools do, via
  ``memory_mod._db``) it reuses it; the retriever-based sites (research, voice,
  cc.context_injector) pass ``db=None`` and the emit opens its own short-lived
  connection via :func:`genesis.db.connection.get_raw_db`.
- :func:`record_would_block_sync` (sync) — the ``UserPromptSubmit`` proactive
  memory hook, a foreground ``sqlite3`` process. It never auto-demotes (a hook
  subprocess must not mutate the immunity overlay on the hot path).

Auto-demote is wired but DORMANT: it only acts from ``process=="server"`` AND
only when the gate is already ``enforce`` (B4). In shadow it is a no-op.

Everything here is fail-open: any error is swallowed (logged at debug) so a
recall/inject is NEVER broken by shadow observability.

Layering: imports only ``db.crud.immunity_shadow`` + ``db.connection`` +
``security.immunity`` (+ provenance constants). It must never import the
recall/inject sites that call it (no cycle), nor ``mcp.health.settings``.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta

from genesis.db.connection import get_raw_db
from genesis.db.crud import immunity_shadow as crud
from genesis.memory.provenance import ORIGIN_EXTERNAL_UNTRUSTED, is_external
from genesis.security import immunity

logger = logging.getLogger(__name__)


# Pipelines that write genuine FIRST-PARTY content INTO the knowledge_base
# (the B0 origin_class backfill set, migration 0054). Only these flip a
# KB-collection item to first-party for the gate. A retrieval-MECHANISM tag
# such as ``drift`` (which overwrites source_pipeline on drift-fallback
# results) must NOT — otherwise drift recalls of external KB content go
# uncounted, undercounting the gate-4 blast radius this shadow measures.
_FIRST_PARTY_KB_PIPELINES = frozenset({"surplus", "reference_store", "extraction_job"})


def item_is_blockable(
    *,
    collection: str | None,
    source_pipeline: str | None,
    origin_class: str | None = None,
) -> bool:
    """True iff a recalled item would be blocked by the injection gate.

    STORED-FIRST (B4): when the item's stored ``origin_class`` (stamped at
    store time, migration 0054; plumbed through recall by this PR) is
    available, it is authoritative — ``immunity.is_blockable`` keys on it
    directly. This is a deliberate semantic WIDENING vs the old re-derivation:
    episodic ``external_untrusted`` rows (written by dispatched external
    sessions, #1021) become observable/blockable, which is exactly what the
    session-origin substrate built. It also FIXES the over-observe FP class:
    a first-party item living in the KB via a non-whitelist pipeline no
    longer counts blockable when its stored class says first_party.

    FALLBACK (stored value absent — pre-0054 row or a surface that doesn't
    plumb it): the original (collection, source_pipeline) re-derivation. The
    AUTHORITATIVE external signal there is the COLLECTION — ``knowledge_base``
    == external-world, always known at recall time (unlike the per-item
    source_pipeline, which drift recall overwrites with ``drift``). A KB item
    is first-party (not blockable) ONLY when its source_pipeline is one that
    writes first-party content INTO the KB
    (:data:`_FIRST_PARTY_KB_PIPELINES`) — never because of a
    retrieval-mechanism tag.

    Either path keeps the never-block invariant honest: owner/first_party
    stored classes are never blockable by construction
    (``immunity.is_blockable``), and the fallback only ever flags
    external-collection content.
    """
    if origin_class is not None:
        return immunity.is_blockable(origin_class)
    if not is_external(collection):
        return False
    return source_pipeline not in _FIRST_PARTY_KB_PIPELINES


def is_dispatched_session_env() -> bool:
    """True iff this process runs inside an UNSUPERVISED Genesis-dispatched CC session.

    Two env signals, both stamped by ``CCInvoker._build_env`` (inherited by the
    session's MCP servers and hooks, popped when absent, read per call):

    - ``GENESIS_SESSION_ID`` — ATTRIBUTION only. Foreground conversations
      (terminal/telegram ConversationManager) set a session id too, via
      ``observability.session_context``, so its presence alone must never be
      read as "unsupervised" (Codex P2 on #1048: enforce would have dropped
      pushed external content from the owner's own chat).
    - ``GENESIS_SESSION_SUPERVISED`` — set from ``CCInvocation.supervised``,
      True only for owner-attended interactive conversations.

    Dispatched/unsupervised = session id present AND supervised marker absent.
    Fail directions (documented): a dispatch path bypassing CCInvoker has no
    session id → reads supervised → keeps wrapped external (fail-open); a new
    foreground path missing the supervised flag drops pushed external there
    (autoimmune direction — visible in the enforce ledger + auto-demote).
    """
    import os

    return bool(os.environ.get("GENESIS_SESSION_ID")) and (
        os.environ.get("GENESIS_SESSION_SUPERVISED") != "1"
    )


def should_enforce_drop(
    *,
    gate: str,
    collection: str | None,
    source_pipeline: str | None,
    origin_class: str | None,
    pushed_surface: bool,
    unsupervised: bool,
) -> bool:
    """THE gate-4 enforce decision — pushed-surfaces cut (B4, user-decided).

    Drop ``external_untrusted`` content ONLY when ALL hold:
    - the gate is in ``enforce`` mode (live per-call YAML read),
    - the surface is PUSHED (automatic/uninvited feed — proactive hook,
      ambient/query-less MCP selection), never an explicit query,
    - the consuming session is UNSUPERVISED (dispatched CC child), and
    - the item is blockable per the stored-first classifier
      (owner/first_party never blockable, by construction).

    Explicit recalls (memory_recall/knowledge_recall/memory_expand) and every
    foreground surface keep returning WRAPPED external content in every mode.
    Fail-OPEN on any error — a provenance lookup must never break recall; the
    worst failure direction is "kept wrapped external", never a lost block
    ledger row (the caller still emits).
    """
    try:
        if not (pushed_surface and unsupervised):
            return False
        if immunity.gate_mode(gate) != "enforce":
            return False
        return item_is_blockable(
            collection=collection,
            source_pipeline=source_pipeline,
            origin_class=origin_class,
        )
    except Exception:
        logger.debug("should_enforce_drop failed open", exc_info=True)
        return False


def _build_row(
    *,
    gate: str,
    mode: str,
    origin_class: str,
    source_kind: str | None,
    source_ref: str | None,
    process: str | None,
    blockable_count: int,
    detail: dict | None,
) -> dict:
    payload = {"blockable": blockable_count}
    if detail:
        payload.update(detail)
    return {
        "id": uuid.uuid4().hex,
        "observed_at": datetime.now(UTC).isoformat(),
        "gate": gate,
        "mode": mode,
        "origin_class": origin_class,
        "would_block": True,
        "source_kind": source_kind,
        "source_ref": source_ref,
        "detail": json.dumps(payload, separators=(",", ":")),
        "process": process,
    }


def _should_record(gate: str, origin_class: str, blockable_count: int) -> str | None:
    """Return the effective mode to record under, or None to skip.

    Skips when nothing is blockable, the gate is off (kill switch), or the
    origin is not blockable (never-block-owner/first_party invariant).
    """
    if blockable_count <= 0:
        return None
    mode = immunity.gate_mode(gate)
    if mode == "off":
        return None
    if not immunity.is_blockable(origin_class):
        return None
    return mode


async def record_would_block(
    *,
    gate: str,
    source_kind: str | None,
    source_ref: str | None,
    process: str | None,
    blockable_count: int,
    origin_class: str = ORIGIN_EXTERNAL_UNTRUSTED,
    db=None,
    detail: dict | None = None,
) -> bool:
    """Record one would-block shadow row (async). Best-effort, never raises.

    Returns True iff a row was written. ``db`` is a live ``aiosqlite`` handle
    to reuse; pass None to self-resolve a short-lived connection.
    """
    try:
        mode = _should_record(gate, origin_class, blockable_count)
        if mode is None:
            return False
        row = _build_row(
            gate=gate,
            mode=mode,
            origin_class=origin_class,
            source_kind=source_kind,
            source_ref=source_ref,
            process=process,
            blockable_count=blockable_count,
            detail=detail,
        )
        if db is not None:
            wrote = await crud.record(db, **row)
            if wrote:
                await _maybe_auto_demote(db, gate=gate, mode=mode, process=process)
            return wrote
        async with get_raw_db() as conn:
            wrote = await crud.record(conn, **row)
            if wrote:
                await _maybe_auto_demote(conn, gate=gate, mode=mode, process=process)
            return wrote
    except Exception:
        logger.debug("immunity shadow emit failed (best-effort)", exc_info=True)
        return False


def record_would_block_sync(
    conn,
    *,
    gate: str,
    source_kind: str | None,
    source_ref: str | None,
    blockable_count: int,
    origin_class: str = ORIGIN_EXTERNAL_UNTRUSTED,
    process: str | None = "proactive_hook",
    detail: dict | None = None,
) -> bool:
    """Sync sibling for the proactive-memory hook (stdlib ``sqlite3``).

    Same invariant + kill-switch guards; never auto-demotes (a hook subprocess
    must not mutate the immunity overlay). Best-effort, never raises.
    """
    try:
        mode = _should_record(gate, origin_class, blockable_count)
        if mode is None:
            return False
        row = _build_row(
            gate=gate,
            mode=mode,
            origin_class=origin_class,
            source_kind=source_kind,
            source_ref=source_ref,
            process=process,
            blockable_count=blockable_count,
            detail=detail,
        )
        return crud.record_sync(conn, **row)
    except Exception:
        logger.debug("immunity shadow sync emit failed (best-effort)", exc_info=True)
        return False


# GROUNDWORK(ws3-b1-readsurface): the B1 observability read. Consumed by the
# immunity_status health MCP tool (per-gate would-block counts); crud.list_recent
# stays GROUNDWORK (no caller yet — a detail/dashboard view consumes it). Do NOT
# delete as dead code.
async def recent_summary(*, since: str | None = None, db=None) -> list[dict]:
    """Per-gate / per-site would-block rollup (COUNTS only, no content).

    The B1 observability read: how much external content reaches each
    action-capable inject site, sizing the B4 enforce blast radius. Optionally
    bounded to rows at/after ISO ``since``. Self-resolves a connection when
    ``db`` is None.

    Unlike the emit path this does NOT swallow errors — a broken read (missing
    table, wrong DB path, failed SELECT) RAISES, so a health caller reports
    "unavailable" rather than a misleading healthy zero (a swallowed failure
    would look identical to "genuinely 0 would-blocks").
    """
    if db is not None:
        return await crud.summary(db, since=since)
    async with get_raw_db() as conn:
        return await crud.summary(conn, since=since)


async def _maybe_auto_demote(db, *, gate: str, mode: str, process: str | None) -> None:
    """Demote *gate* enforce→shadow if would-blocks breach the configured
    threshold. DORMANT in shadow: only acts from the server process AND only
    when the gate is already ``enforce`` (B4). Never mutates config from a
    hook subprocess."""
    if process != "server" or mode != "enforce":
        return
    cfg = immunity.load_immunity_config().get("auto_demote", {})
    if not cfg.get("enabled", True):
        return
    window = int(cfg.get("window_minutes", 60))
    threshold = int(cfg.get("would_block_threshold", 5))
    since = (datetime.now(UTC) - timedelta(minutes=window)).isoformat()
    n = await crud.count_would_block(db, gate=gate, since=since)
    if n >= threshold:
        reason = f"auto-demote: {n} would-blocks in {window}m >= {threshold}"
        immunity.record_demotion(gate, reason)
        # B4: page-worthy — an auto-demotion means the gate fought legitimate
        # flow (a mis-STORED origin bug post-PR-1, or an external-content
        # surge). critical priority rides the existing critical-obs → Telegram
        # batch (B3's pattern). Best-effort: never break the emit path.
        try:
            import hashlib
            import uuid as _uuid

            from genesis.db.crud import observations as _obs

            await _obs.create(
                db,
                id=str(_uuid.uuid4()),
                source="ws3_auto_demote",
                type="infrastructure_alert",
                content=(
                    f"WS-3 gate '{gate}' AUTO-DEMOTED enforce->shadow: {reason}. "
                    "Content is crossing again (observe-only). Investigate the "
                    "would-block ledger before re-enforcing."
                ),
                priority="critical",
                created_at=datetime.now(UTC).isoformat(),
                content_hash=hashlib.sha256(f"ws3_auto_demote:{gate}".encode()).hexdigest(),
                skip_if_duplicate=True,
            )
        except Exception:
            logger.warning("auto-demote alert write failed", exc_info=True)
