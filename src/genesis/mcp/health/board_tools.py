"""Work-board MCP tools.

* ``board_promote`` — the door to :mod:`genesis.board.promotion`. Any session
  may PROPOSE; the owner approves each promotion, and only then does the drain
  post the public issue. Nothing reaches GitHub from this call.
* ``board_status`` / ``board_item`` — read-only: the board as the reconciler
  last read it (stored heartbeats), and one card read live.
"""

from __future__ import annotations

import logging
import re

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)


def _db_or_none():
    import genesis.mcp.health_mcp as health_mcp_mod

    _service = health_mcp_mod._service
    return _service._db if _service is not None else None


@mcp.tool()
async def board_promote(
    source: str,
    title: str,
    body: str,
    acceptance_criteria: list[str] | None = None,
    labels: list[str] | None = None,
) -> dict:
    """Propose turning a private ledger row or follow-up into a PUBLIC GitHub
    issue on the work board. Held for the owner's approval; nothing is posted
    by this call.

    ``source`` is ``ledger:<id>`` or ``follow_up:<id>`` (full id or a unique
    8+ hex prefix). ``title`` and ``body`` are what the PUBLIC issue will say —
    write them for a public reader: technical detail only, nothing personal or
    install-specific. ``acceptance_criteria`` become a checklist. The draft is
    privacy-scanned here; a refusal names only the line number (title = line 1)
    and the scanner, so rewrite that line.

    Refused when the board mode is off, when an unverified open question blocks
    the source (resolve it first), or when the source is already promoted or has
    a promotion pending. The same checks run again right before the issue is
    created: an approved promotion whose record has since closed, or whose
    label is gone, is ended unposted (propose it again once fixed). Returns
    ``held`` / ``disabled`` / ``refused`` / ``blocked`` / ``duplicate`` /
    ``error``.
    """
    from genesis.board import promotion

    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await promotion.propose(
        db,
        source=source,
        title=title,
        body=body,
        acceptance_criteria=acceptance_criteria,
        labels=labels,
    )


# ─── read surfaces: board_status / board_item ───────────────────────────────

# A repo is always followed by '#', so "owner/repo25" can never read as
# owner/repo2 issue 5; issue numbers start at 1.
_TARGET_RE = re.compile(r"^(?:card:)?(?:([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)#|#)?([1-9]\d*)$")


def _details(row: dict) -> dict:
    d = row.get("details")
    return d if isinstance(d, dict) else {}


def _newest(rows: list[dict], now) -> dict | None:
    """The newest row by PARSED timestamp, never a future-dated one (the
    manifest's own rule; a textual ORDER BY is not chronological)."""
    from genesis.mcp.health.manifest import _newest_valid_ts

    iso, _ = _newest_valid_ts([r.get("timestamp") for r in rows], now=now)
    return next((r for r in rows if r.get("timestamp") == iso), None) if iso else None


async def _impl_board_status(db, *, now=None) -> dict:
    """The board as the reconciler last read it. Reads the stored heartbeats
    only (never GitHub): the newest pulse says whether the job is alive and
    what it could do; the newest SUCCESSFUL pulse carries the counts."""
    from datetime import UTC, datetime

    from genesis.db.crud import events as events_crud
    from genesis.mcp.health import manifest

    now = now or datetime.now(UTC)
    limit = manifest._HEARTBEAT_SCAN_LIMIT
    try:
        rows = await events_crud.query(db, subsystem="board", event_type="heartbeat", limit=limit)
    except Exception as exc:
        logger.error("board_status: heartbeat read failed", exc_info=True)
        return {"status": "error", "reason": f"heartbeat read failed: {type(exc).__name__}"}
    verdict = await manifest.compute_heartbeat_staleness("board", db=db)
    out: dict = {
        "status": "ok",
        "heartbeat": verdict,
        "pulses_read": len(rows),
        "scan_limit": limit,
    }

    latest = _newest(rows, now)
    if latest is None:
        out["latest"] = None
        out["summary"] = None
        out["note"] = "no board heartbeat yet: the reconciler has not run on this install"
        return out
    d = _details(latest)
    out["latest"] = {
        "at": latest.get("timestamp"),
        "board_state": d.get("board_state"),
        "mode": d.get("mode"),
        "paused": d.get("paused"),
        "project": d.get("project"),
        "last_error": d.get("last_error"),
    }
    ok = _newest([r for r in rows if _details(r).get("board_state") == "ok"], now)
    if ok is None:
        out["summary"] = None
        out["note"] = f"no successful board read in the last {len(rows)} pulses"
        return out
    od = _details(ok)
    from genesis.observability.liveness import parse_iso_utc

    at = parse_iso_utc(ok.get("timestamp"))
    out["summary"] = {
        "at": ok.get("timestamp"),
        "age_seconds": int((now - at).total_seconds()) if at else None,
        "project": od.get("project"),
        "items_total": od.get("items_total"),
        "by_status": od.get("by_status"),
        "by_genesis": od.get("by_genesis"),
        "by_kind": od.get("by_kind"),
        "coverage": od.get("coverage"),
        "drags": od.get("drags"),
    }
    return out


async def _impl_board_item(db, target: str) -> dict:
    """One issue's or PR's board state: a LIVE read of its cards and blockers,
    plus the local open questions blocking it and its promotion link."""
    from genesis.board import config as board_config
    from genesis.board import projects_v2 as pv
    from genesis.db.crud import board as board_crud

    # Mode off means the board does nothing (board/config.py), the kill switch
    # included, so this tool makes no GitHub read either.
    if board_config.effective_mode() == "off":
        return {"status": "unavailable", "reason": "board mode is off"}
    m = _TARGET_RE.match((target or "").strip())
    if not m:
        return {"status": "error", "reason": "target must be 'owner/repo#N', '#N' or 'N'"}
    repo, number = m.group(1), int(m.group(2))
    if repo is None:
        tracker = board_config.tracker_repo()
        if tracker is None:
            return {"status": "error", "reason": "no public tracker configured; name the repo"}
        repo = "/".join(tracker)
    owner, _, name = repo.partition("/")
    try:
        card = await pv.card_for_issue(owner, name, number)
    except pv.ProjectsError as exc:
        return {"status": "error", "reason": str(exc)}
    ref = board_config.project_ref()
    on_board = None
    if ref is not None:
        on_board = [
            c
            for c in card["cards"]
            if c["project_number"] == ref[1]
            and (c.get("project_owner") or "").lower() == ref[0].lower()
        ]
    out: dict = {
        "status": "ok",
        "target": f"{owner}/{name}#{number}",
        "kind": card["kind"],
        "state": card["state"],
        "board_card": (on_board[0] if on_board else None) if ref is not None else None,
        "board_configured": ref is not None,
        "all_cards": card["cards"],
        "cards_truncated": card["cards_truncated"],
        "blocked_by": card["blocked_by"],
        "blocked_by_total": card["blocked_by_total"],
        "blockers_truncated": card["blockers_truncated"],
    }
    if await board_crud.tables_available(db):
        out["blocking_questions"] = await board_crud.blocking_questions(
            db, target_kind="card", target_id=f"{owner}/{name}#{number}"
        )
        link = await board_crud.get_link_by_issue(db, repo=f"{owner}/{name}", issue_number=number)
        out["promoted_from"] = (
            {"kind": link["source_kind"], "approval_id": link.get("approval_id")} if link else None
        )
    else:
        out["blocking_questions"] = None
        out["promoted_from"] = None
        out["note"] = "board tables not migrated yet"
    return out


@mcp.tool()
async def board_status() -> dict:
    """The work board as the reconciler last read it: counts by column, by
    Genesis status and by kind, coverage (open repo issues and PRs on the board
    against the repo's open total), drags logged, and whether the reconciler is
    alive. Reads stored heartbeats only, never GitHub, so it is cheap and safe.

    ``latest`` is the newest pulse (mode, paused, any error); ``summary`` is the
    newest SUCCESSFUL read and its age. A board that is off or not set up has a
    pulse but no summary, and says so.
    """
    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await _impl_board_status(db)


@mcp.tool()
async def board_item(target: str) -> dict:
    """One issue or PR on the work board: its column and Genesis status (a live
    read), what blocks it (GitHub's blocked-by list, flagged when truncated),
    unverified open questions blocking it, and whether it was promoted from a
    private record.

    ``target`` is ``owner/repo#N``, or ``#N`` / ``N`` for the configured
    tracker. Read-only.
    """
    db = _db_or_none()
    if db is None:
        return {"status": "unavailable", "message": "DB not initialized"}
    return await _impl_board_item(db, target)
