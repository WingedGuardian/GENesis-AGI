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
from genesis.memory.provenance import (
    ORIGIN_EXTERNAL_UNTRUSTED,
    derive_origin_class,
    is_external,
)
from genesis.security import immunity

logger = logging.getLogger(__name__)


def item_is_blockable(*, collection: str | None, source_pipeline: str | None) -> bool:
    """True iff a recalled item would be blocked by the injection gate.

    An item is blockable only when it is external by collection AND its derived
    origin_class is ``external_untrusted``. First-party pipelines that live IN
    the knowledge_base (surplus / reference_store / extraction_job) are NOT
    blockable — so counting these keeps the never-block invariant honest at the
    site, before the row is even emitted.

    NOTE (B4): this re-derives origin_class from the recall dict's
    (source_pipeline, collection) because the retriever does not yet plumb the
    STORED origin_class into results. It is conservative — the rare
    subsystem-tagged first-party item in KB (no source_subsystem on the recall
    dict) derives external here, so shadow may slightly over-observe. That is
    safe in shadow (never blocks); ENFORCE (B4) must gate on the stored
    origin_class, not this re-derivation. Tracked as a B1 follow-up.
    """
    if not is_external(collection):
        return False
    return immunity.is_blockable(
        derive_origin_class(source_pipeline=source_pipeline, collection=collection)
    )


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


# GROUNDWORK(ws3-b1-readsurface): the B1 observability read. No live caller
# yet (B4 / a dashboard card consumes it — see follow-up); do NOT delete as
# dead code. crud.summary/list_recent are reached only through here.
async def recent_summary(*, since: str | None = None, db=None) -> list[dict]:
    """Per-gate / per-site would-block rollup (COUNTS only, no content).

    The B1 observability read: how much external content reaches each
    action-capable inject site, sizing the B4 enforce blast radius. Optionally
    bounded to rows at/after ISO ``since``. Self-resolves a connection when
    ``db`` is None. Best-effort: returns [] on error.
    """
    try:
        if db is not None:
            return await crud.summary(db, since=since)
        async with get_raw_db() as conn:
            return await crud.summary(conn, since=since)
    except Exception:
        logger.debug("immunity shadow summary read failed", exc_info=True)
        return []


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
        immunity.record_demotion(gate, f"auto-demote: {n} would-blocks in {window}m >= {threshold}")
