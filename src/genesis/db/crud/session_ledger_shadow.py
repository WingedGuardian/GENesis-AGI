"""CRUD for session_ledger_shadow_runs/_events — the ambient extractor SHADOW store.

Observe-only (session-manager PR-3): rows are PROPOSED ledger items + match
telemetry, never live ledger writes. Written by the detached
``scripts/ledger_shadow_worker.py`` subprocess over a short-lived RW
connection (WAL + busy_timeout); read by ``scripts/ledger_shadow_report.py``
and (later) the dashboard.

Subprocess writers do NOT run migrations, so writers guard on table existence
(cached per-process, TRUE result only — the capability_shadow pattern) and
no-op pre-migration. Migration 0059 is the sole schema authority; nothing here
creates tables.
"""

from __future__ import annotations

import aiosqlite

RUN_STATUSES = ("ok", "failed", "timeout", "lock_busy", "empty_delta")
EVENT_KINDS = ("agreement", "pivot")
MATCH_KINDS = ("exact", "fuzzy", "none")

# Per-process cache: only the TRUE result is cached — a missing table
# (pre-migration window) is re-checked every call so a subprocess writer
# self-heals the moment the server migration lands.
_tables_verified = False


async def _tables_available(db: aiosqlite.Connection) -> bool:
    global _tables_verified
    if _tables_verified:
        return True
    cursor = await db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN "
        "('session_ledger_shadow_runs', 'session_ledger_shadow_events')"
    )
    row = await cursor.fetchone()
    exists = bool(row and row[0] == 2)
    if exists:
        _tables_verified = True
    return exists


async def record_run(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    session_id: str,
    started_at: str,
    finished_at: str | None,
    start_byte: int,
    end_byte: int,
    trigger: str,
    status: str,
    truncated: bool = False,
    n_user_turns: int = 0,
    n_proposals: int = 0,
    latency_ms: int | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    mode: str = "shadow",
    detail: str | None = None,
    events: list[dict] | None = None,
) -> bool:
    """Insert one run row + its event rows in a single transaction.

    Returns False (no-op) if the tables don't exist yet (subprocess
    pre-migration window) — the caller must then NOT advance its cursor,
    so the delta is preserved until the migration lands. Event dicts
    carry the _events columns minus run_id/session_id/mode (stamped from
    the run here); ``observed_at`` and ``id`` are per-event.
    """
    if status not in RUN_STATUSES:
        raise ValueError(f"invalid run status: {status!r}")
    # Validate EVERYTHING before the first INSERT: a mid-transaction raise
    # would strand an uncommitted run row on the connection (and shared
    # connections are never rolled back here by house rule).
    for ev in events or []:
        if ev.get("kind") not in EVENT_KINDS:
            raise ValueError(f"invalid event kind: {ev.get('kind')!r}")
        if ev.get("match_kind", "none") not in MATCH_KINDS:
            raise ValueError(f"invalid match_kind: {ev.get('match_kind')!r}")
    if not await _tables_available(db):
        return False
    await db.execute(
        "INSERT INTO session_ledger_shadow_runs "
        "(run_id, session_id, started_at, finished_at, start_byte, end_byte, "
        "trigger, status, truncated, n_user_turns, n_proposals, latency_ms, "
        "prompt_version, model, mode, detail) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            session_id,
            started_at,
            finished_at,
            start_byte,
            end_byte,
            trigger,
            status,
            1 if truncated else 0,
            n_user_turns,
            n_proposals,
            latency_ms,
            prompt_version,
            model,
            mode,
            detail,
        ),
    )
    for ev in events or []:
        await db.execute(
            "INSERT INTO session_ledger_shadow_events "
            "(id, run_id, observed_at, session_id, kind, text, turn_ref, "
            "quote_preview, quote_hash, quote_verified, match_kind, "
            "matched_item_id, match_score, duplicate_of, mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ev["id"],
                run_id,
                ev["observed_at"],
                session_id,
                ev["kind"],
                ev["text"],
                ev.get("turn_ref"),
                ev.get("quote_preview"),
                ev.get("quote_hash"),
                1 if ev.get("quote_verified") else 0,
                ev.get("match_kind", "none"),
                ev.get("matched_item_id"),
                ev.get("match_score"),
                ev.get("duplicate_of"),
                mode,
            ),
        )
    await db.commit()
    return True


async def list_runs(
    db: aiosqlite.Connection,
    session_id: str | None = None,
    *,
    limit: int = 200,
) -> list[dict]:
    """Runs newest first, optionally per-session. Assumes a Row factory."""
    lim = max(1, min(int(limit), 1000))
    if session_id is None:
        cursor = await db.execute(
            "SELECT * FROM session_ledger_shadow_runs ORDER BY started_at DESC LIMIT ?",
            (lim,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM session_ledger_shadow_runs WHERE session_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (session_id, lim),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def list_events(
    db: aiosqlite.Connection,
    session_id: str | None = None,
    *,
    limit: int = 500,
) -> list[dict]:
    """Proposal events oldest first, optionally per-session.

    Deliberately NOT the shadow report's read: the report selects events BY
    its chosen run window (``list_events_for_runs``) and reads attribution by
    key (``list_promoted_events_with_runs``) — an independent row-count cap
    here cannot promise to cover the same span as the runs query. This stays
    the worker's read, where oldest-first is load-bearing: the dedup pool's
    duplicate_of chains root on the earliest occurrence.
    """
    lim = max(1, min(int(limit), 2000))
    if session_id is None:
        cursor = await db.execute(
            "SELECT * FROM session_ledger_shadow_events ORDER BY observed_at ASC LIMIT ?",
            (lim,),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM session_ledger_shadow_events WHERE session_id = ? "
            "ORDER BY observed_at ASC LIMIT ?",
            (session_id, lim),
        )
    return [dict(r) for r in await cursor.fetchall()]


async def summary(db: aiosqlite.Connection) -> dict:
    """Counts-only health rollup: run status histogram + event totals."""
    out: dict = {"runs": {}, "events": {}}
    cursor = await db.execute(
        "SELECT status, COUNT(*) AS n FROM session_ledger_shadow_runs GROUP BY status"
    )
    for row in await cursor.fetchall():
        out["runs"][row[0]] = row[1]
    cursor = await db.execute(
        "SELECT kind, match_kind, COUNT(*) AS n FROM session_ledger_shadow_events "
        "GROUP BY kind, match_kind"
    )
    for row in await cursor.fetchall():
        out["events"][f"{row[0]}/{row[1]}"] = row[2]
    return out


async def prune_session_ledger_shadow(
    db: aiosqlite.Connection,
    *,
    older_than_days: int = 45,
    now: str,
) -> int:
    """Delete runs+events older than *older_than_days* relative to ISO ``now``.

    Retention for the unbounded shadow store (wired into disk_hygiene.sh),
    mirroring ``prune_capability_shadow_events``. ``now`` is injected (never
    wall-clock here) so the cutover is deterministic and testable. No-ops
    before migration 0059; never creates tables. Returns rows deleted.

    PROMOTED events — and the runs they belong to — are EXEMPT, deliberately
    and without a secondary age cap (owner decision, 2026-09-05). A promoted
    event is the attribution record the shadow report's leak invariant reads:
    it is the only thing that says WHICH run, in WHICH mode, wrote a given
    extractor ledger row. Deleting it by age turns a legitimate 46-day-old
    promotion into an "unattributed leak" and the report into a permanent
    false VIOLATED — absence of provenance must never read as violation when
    the absence is this function's own doing. The exempt set is bounded in
    practice, not by this code: promotions happen only in live mode, at most
    PROMOTION_CAP (5) per compaction, so the survivors accrue at tens-to-
    hundreds of rows per year, each one line of text.
    """
    if not await _tables_available(db):
        return 0
    cutoff = _iso_days_before(now, older_than_days)
    deleted = 0
    # The exemption column arrives with a LATER migration than the tables
    # themselves (0059 made the tables; the ambient-extractor migration added
    # `promoted_item_id`). Retention must keep its no-crash contract on a DB
    # sitting between the two — there is nothing to exempt there anyway, since
    # promotion cannot have written without the column.
    cursor = await db.execute("PRAGMA table_info(session_ledger_shadow_events)")
    has_promoted = any(r[1] == "promoted_item_id" for r in await cursor.fetchall())
    if has_promoted:
        cursor = await db.execute(
            "DELETE FROM session_ledger_shadow_events "
            "WHERE observed_at < ? AND promoted_item_id IS NULL",
            (cutoff,),
        )
        deleted += cursor.rowcount or 0
        # A run row is the mode half of the attribution (event -> run_id ->
        # mode), so it survives exactly as long as a promoted event points at it.
        cursor = await db.execute(
            "DELETE FROM session_ledger_shadow_runs WHERE started_at < ? "
            "AND run_id NOT IN (SELECT run_id FROM session_ledger_shadow_events "
            "                   WHERE promoted_item_id IS NOT NULL)",
            (cutoff,),
        )
        deleted += cursor.rowcount or 0
    else:
        cursor = await db.execute(
            "DELETE FROM session_ledger_shadow_events WHERE observed_at < ?", (cutoff,)
        )
        deleted += cursor.rowcount or 0
        cursor = await db.execute(
            "DELETE FROM session_ledger_shadow_runs WHERE started_at < ?", (cutoff,)
        )
        deleted += cursor.rowcount or 0
    await db.commit()
    return deleted


def _iso_days_before(now_iso: str, days: int) -> str:
    """Return the ISO8601 timestamp *days* before ``now_iso``."""
    from datetime import datetime, timedelta

    dt = datetime.fromisoformat(now_iso)
    return (dt - timedelta(days=days)).isoformat()


async def list_promoted_events_with_runs(
    db: aiosqlite.Connection,
) -> tuple[list[dict], list[dict]]:
    """The COMPLETE attribution set: every promoted event, plus its run.

    The leak invariant's verifier must never read attribution through a
    windowed scan — retention exempts promoted events precisely so the
    verifier can always see them, and a capped newest-first read would age
    them out of the verifier's own LIMIT while the report still said HELD.
    Query by the attribution key instead. Unbounded on the same argument as
    the prune exemption (live-mode-only promotion, PROMOTION_CAP per
    compaction: tens-to-hundreds of one-line rows per year), with a generous
    ceiling that is a tripwire, not a budget: hitting it means the premise
    broke, and the caller is told loudly rather than handed a silent subset.
    """
    if not await _tables_available(db):
        return [], []
    cursor = await db.execute(
        "SELECT * FROM session_ledger_shadow_events "
        "WHERE promoted_item_id IS NOT NULL ORDER BY observed_at ASC LIMIT 10001"
    )
    events = [dict(r) for r in await cursor.fetchall()]
    if len(events) > 10000:
        raise RuntimeError(
            "attribution set exceeds 10,000 promoted events — the "
            "bounded-by-promotion-cap premise is broken; refusing to hand the "
            "leak invariant a silent subset"
        )
    run_ids = sorted({e["run_id"] for e in events})
    runs: list[dict] = []
    for i in range(0, len(run_ids), 500):  # SQLite parameter-count safety
        chunk = run_ids[i : i + 500]
        placeholders = ", ".join("?" for _ in chunk)
        cursor = await db.execute(
            f"SELECT * FROM session_ledger_shadow_runs WHERE run_id IN ({placeholders})",  # noqa: S608 — placeholders only
            chunk,
        )
        runs.extend(dict(r) for r in await cursor.fetchall())
    return events, runs


async def list_events_for_runs(
    db: aiosqlite.Connection,
    run_ids: list[str],
    *,
    max_events: int = 24000,
) -> list[dict]:
    """Every event belonging to *run_ids*, oldest first — ONE joined window.

    The shadow report's precision populations need events selected BY the run
    window it already chose, not by an independent row-count cap: two
    independent newest-N caps stop covering the same span the moment runs
    average more proposals than the ratio of the caps, and runs whose events
    fell off then read as swept-with-no-proposals — charged false negatives.
    *max_events* is a resource tripwire sized from the schema's own worst case
    (a run stores at most 12 proposals; 1000 runs -> 12,000, doubled for
    slack), not a window: exceeding it raises rather than silently narrowing
    the population this exists to keep whole.
    """
    if not await _tables_available(db) or not run_ids:
        return []
    out: list[dict] = []
    for i in range(0, len(run_ids), 500):  # SQLite parameter-count safety
        chunk = run_ids[i : i + 500]
        placeholders = ", ".join("?" for _ in chunk)
        cursor = await db.execute(
            f"SELECT * FROM session_ledger_shadow_events WHERE run_id IN ({placeholders})",  # noqa: S608 — placeholders only
            chunk,
        )
        out.extend(dict(r) for r in await cursor.fetchall())
        if len(out) > max_events:
            raise RuntimeError(
                f"events for {len(run_ids)} runs exceed {max_events} — the "
                "per-run proposal bound is broken; refusing to hand the report "
                "a silently narrowed population"
            )
    out.sort(key=lambda e: e.get("observed_at") or "")
    return out
