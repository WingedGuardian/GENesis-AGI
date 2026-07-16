"""CC Sessions dashboard routes — list detail + the per-session cockpit.

The list endpoint joins the three views of "what CC sessions exist" that
otherwise never meet: cc_sessions DB rows (registered lifecycle state), the
live /proc slot scan (actual claude processes), and session charters (what
each session is FOR — session-manager tables, migration 0058). Their
disagreements are the point: per-row discrepancy flags surface the
divergence the overview card can only hint at. It feeds BOTH the overview
modal and the Sessions tab list pane (session-manager PR-4b) — one payload,
drift bounded by construction.

The per-session endpoint is the cockpit detail: full charter (immutable
origin + living mission/pointers), full ledger rows, the deterministic
waypoint spine (this is its first reader), and repo-pulse annotations.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
from flask import jsonify

from genesis.dashboard._blueprint import _async_route, blueprint

logger = logging.getLogger(__name__)

ORIGIN_PROMPT_CAP = 4000  # chars — origins are typically short; a pasted wall is truncated
WAYPOINT_TAIL = 200  # newest waypoint lines returned (compaction cadence makes this ~weeks)

# Sessions shown in the modal: everything active plus recent history. The
# cutoff arrives as a parameter (derived from the same `now` as the age
# fields — deterministic in tests), and datetime() normalizes both sides so
# ISO-T/offset and space-form timestamps compare correctly.
_RECENT_WINDOW_SQL = (
    "SELECT * FROM cc_sessions"
    " WHERE status = 'active' OR datetime(last_activity_at) >= datetime(?)"
    " ORDER BY CASE WHEN status = 'active' THEN 0 ELSE 1 END, last_activity_at DESC"
    " LIMIT 100"
)


def _age_seconds(iso_ts: str | None, now: datetime) -> float | None:
    """Seconds since an ISO timestamp, treating naive values as UTC."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return round((now - ts).total_seconds(), 1)


async def _charter_lookup(db, cc_ids: list[str]) -> tuple[dict[str, dict], bool]:
    """Charter + open-ledger counts keyed by CC transcript session id.

    Wrapped defensively: on installs where migration 0058 has not run yet the
    tables are absent — the modal then hides the charter column via
    charters_available=False instead of erroring (surplus.py precedent for
    optional tables).
    """
    if not cc_ids:
        return {}, True
    placeholders = ", ".join("?" for _ in cc_ids)
    charters: dict[str, dict] = {}
    try:
        cursor = await db.execute(
            f"SELECT session_id, mission, compaction_count, origin_ts"
            f" FROM session_charters WHERE session_id IN ({placeholders})",  # noqa: S608 — placeholders only
            cc_ids,
        )
        for row in await cursor.fetchall():
            charters[row["session_id"]] = {
                "mission": row["mission"],
                "compaction_count": row["compaction_count"],
                "origin_ts": row["origin_ts"],
                "ledger_open": 0,
                "ledger_total": 0,
            }
        cursor = await db.execute(
            f"SELECT session_id, COUNT(*) AS total,"
            f" SUM(CASE WHEN status IN ('open','in_progress') THEN 1 ELSE 0 END) AS open_count"
            f" FROM session_ledger WHERE session_id IN ({placeholders})"  # noqa: S608 — placeholders only
            f" GROUP BY session_id",
            cc_ids,
        )
        for row in await cursor.fetchall():
            entry = charters.setdefault(
                row["session_id"],
                {
                    "mission": None,
                    "compaction_count": 0,
                    "origin_ts": None,
                    "ledger_open": 0,
                    "ledger_total": 0,
                },
            )
            entry["ledger_total"] = row["total"] or 0
            entry["ledger_open"] = row["open_count"] or 0
    except (sqlite3.Error, aiosqlite.Error) as exc:
        logger.debug("charter tables unavailable (pre-0058 install?): %s", exc)
        return {}, False
    return charters, True


async def _collect_detail(db, slots: list[dict], now: datetime | None = None) -> dict:
    """Assemble the modal payload. `slots` is passed in so tests inject fakes
    instead of scanning /proc."""
    now = now or datetime.now(UTC)
    db.row_factory = aiosqlite.Row

    cutoff = (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = await db.execute(_RECENT_WINDOW_SQL, (cutoff,))
    rows = [dict(r) for r in await cursor.fetchall()]

    cc_ids = sorted({r["cc_session_id"] for r in rows if r.get("cc_session_id")})
    charters, charters_available = await _charter_lookup(db, cc_ids)

    # Live-proc merge by pid: query order is active/newest-first, so a
    # recycled pid attaches to the most plausible row; each slot is consumed
    # at most once.
    slot_by_pid = {s["pid"]: dict(s) for s in slots if s.get("pid") is not None}
    consumed: set[int] = set()

    sessions = []
    stats = {
        "db_active": 0,
        "live_procs": len(slots),
        "discrepant": 0,
        "completed_24h": 0,
        "failed_24h": 0,
    }
    for row in rows:
        live = None
        pid = row.get("pid")
        if pid in slot_by_pid and pid not in consumed:
            s = slot_by_pid[pid]
            live = {
                "slot": s.get("slot"),
                "pid": s.get("pid"),
                "rss_mb": s.get("rss_mb"),
                "slot_status": s.get("status"),
            }
            consumed.add(pid)

        flags: list[str] = []
        if row["status"] == "active" and live is None:
            flags.append("db_active_no_proc")
        if row["status"] != "active" and live is not None:
            flags.append("proc_but_db_inactive")

        if row["status"] == "active":
            stats["db_active"] += 1
        elif row["status"] == "completed":
            stats["completed_24h"] += 1
        elif row["status"] == "failed":
            stats["failed_24h"] += 1
        if flags:
            stats["discrepant"] += 1

        sessions.append(
            {
                "id": row["id"],
                "cc_session_id": row.get("cc_session_id"),
                "session_type": row["session_type"],
                "status": row["status"],
                "model": row.get("model"),
                "channel": row.get("channel"),
                "source_tag": row.get("source_tag"),
                "pid": pid,
                "started_at": row.get("started_at"),
                "last_activity_at": row.get("last_activity_at"),
                "completed_at": row.get("completed_at"),
                "age_s": _age_seconds(row.get("started_at"), now),
                "idle_s": _age_seconds(row.get("last_activity_at"), now),
                "cost_usd": row.get("cost_usd"),
                "live": live,
                "charter": charters.get(row.get("cc_session_id")),
                "flags": flags,
            }
        )

    unmatched_slots = [
        dict(s) for s in slots if s.get("pid") is not None and s["pid"] not in consumed
    ]
    stats["discrepant"] += len(unmatched_slots)

    return {
        "sessions": sessions,
        "unmatched_slots": unmatched_slots,
        "stats": stats,
        "charters_available": charters_available,
    }


@blueprint.route("/api/genesis/cc-sessions/detail")
@_async_route
async def cc_sessions_detail():
    """Per-session detail for the CC Sessions modal: DB rows × live procs ×
    charters, with discrepancy flags."""
    from genesis.observability.cc_slots import enumerate_cc_slots
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503

    slots = enumerate_cc_slots()
    return jsonify(await _collect_detail(rt.db, slots))


# ── per-session cockpit detail (session-manager PR-4b) ──────────────────────


def _load_waypoints(cc_session_id: str, sessions_dir: Path | None = None) -> dict:
    """Deterministic waypoint spine from ~/.genesis/sessions/<sid>/waypoints.jsonl.

    This is the file's FIRST reader (the PreCompact hook only appends).
    Newest WAYPOINT_TAIL lines, per-line corrupt-skip — one torn line must
    not hide the rest of the spine.
    """
    base = sessions_dir or (Path.home() / ".genesis" / "sessions")
    path = base / cc_session_id / "waypoints.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"available": False, "truncated": False, "items": []}
    items = []
    for line in lines[-WAYPOINT_TAIL:]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            items.append(
                {
                    "ts": entry.get("ts"),
                    "trigger": entry.get("trigger"),
                    "transcript_bytes": entry.get("transcript_bytes"),
                }
            )
    return {"available": True, "truncated": len(lines) > WAYPOINT_TAIL, "items": items}


async def _pulse_lookup(db, cc_session_id: str) -> dict:
    """Repo-pulse annotations for this session's ledger items (PR-4a store).

    Defensive like _charter_lookup: pre-0062 installs have no pulse tables —
    the panel then shows available=False instead of erroring. This degrade
    path must survive forever (independent-mergeability regression contract).
    """
    try:
        from genesis.db.crud.repo_pulse import list_annotations, summary

        annotations = await list_annotations(db, session_id=cc_session_id, limit=100)
        health = await summary(db)
    except Exception as exc:
        logger.debug("pulse tables unavailable (pre-0062 install?): %s", exc)
        return {"available": False, "annotations": [], "health": None}
    return {"available": True, "annotations": annotations, "health": health}


async def _collect_session_detail(
    db,
    cc_session_id: str,
    *,
    sessions_dir: Path | None = None,
    now: datetime | None = None,
) -> dict | None:
    """Assemble the cockpit payload for one CC session, or None (→ 404) when
    neither a cc_sessions row nor a charter exists for the id."""
    from genesis.db.crud import session_charters as charter_crud

    now = now or datetime.now(UTC)
    db.row_factory = aiosqlite.Row

    cursor = await db.execute(
        "SELECT * FROM cc_sessions WHERE cc_session_id = ? ORDER BY datetime(started_at) DESC",
        (cc_session_id,),
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    session = None
    if rows:
        newest = rows[0]
        session = {
            "id": newest["id"],
            "cc_session_id": cc_session_id,
            "session_type": newest.get("session_type"),
            "status": newest.get("status"),
            "model": newest.get("model"),
            "channel": newest.get("channel"),
            "started_at": newest.get("started_at"),
            "last_activity_at": newest.get("last_activity_at"),
            "age_s": _age_seconds(newest.get("started_at"), now),
            "idle_s": _age_seconds(newest.get("last_activity_at"), now),
            "cost_usd": newest.get("cost_usd"),
            "session_row_count": len(rows),
        }

    charter = None
    ledger: dict = {"items": [], "counts": {}}
    charters_available = True
    try:
        charter_row = await charter_crud.get(db, cc_session_id)
        if charter_row is not None:
            charter = dict(charter_row)
            # crud.get already JSON-decodes pointers; guard the shape only.
            if not isinstance(charter.get("pointers"), list):
                charter["pointers"] = []
            origin = str(charter.get("origin_prompt") or "")
            charter["origin_truncated"] = len(origin) > ORIGIN_PROMPT_CAP
            charter["origin_prompt"] = origin[:ORIGIN_PROMPT_CAP]
        ledger["items"] = await charter_crud.ledger_list(db, cc_session_id)
        ledger["counts"] = await charter_crud.ledger_counts(db, cc_session_id)
    except (sqlite3.Error, aiosqlite.Error) as exc:
        logger.debug("charter tables unavailable (pre-0058 install?): %s", exc)
        charters_available = False

    if session is None and charter is None:
        return None

    return {
        "session": session,
        "charters_available": charters_available,
        "charter": charter,
        "ledger": ledger,
        "waypoints": _load_waypoints(cc_session_id, sessions_dir),
        "pulse": await _pulse_lookup(db, cc_session_id),
    }


@blueprint.route("/api/genesis/cc-sessions/<cc_session_id>/charter")
@_async_route
async def cc_session_charter(cc_session_id: str):
    """Full cockpit detail for one CC session: charter (immutable origin +
    living mission/pointers), ledger rows, waypoint timeline, pulse panel."""
    from genesis.runtime import GenesisRuntime

    rt = GenesisRuntime.instance()
    if not rt.is_bootstrapped or rt.db is None:
        return jsonify({"error": "not bootstrapped"}), 503
    # Traversal guard (mirrors the PreCompact hook): the id names a directory
    # under ~/.genesis/sessions.
    if "/" in cc_session_id or ".." in cc_session_id:
        return jsonify({"error": "invalid session id"}), 400

    detail = await _collect_session_detail(rt.db, cc_session_id)
    if detail is None:
        return jsonify({"error": "unknown session"}), 404
    return jsonify(detail)
