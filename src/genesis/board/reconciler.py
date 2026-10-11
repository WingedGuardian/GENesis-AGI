"""Board reconciler, READ-ONLY (the first of three reconciler PRs).

Every five minutes (``CronTrigger``, ``max_instances=1``) one tick:

1. decides whether it may read the board at all (not paused, mode not
   ``off``, a project configured);
2. if so, reads the WHOLE project (``list_items`` raises on a short read) and
   the tracker repo's own open totals, and counts cards by Status, by the
   Genesis field and by kind, plus coverage (open repo issues and PRs on the
   board, against the repo's open total);
3. logs a ``drag`` event for every card in In Progress, keyed
   ``item_id@<Status updatedAt>`` so a repeat is absorbed by the unique index.
   MEASURED 2026-10-04 on a private sandbox: that ``updatedAt`` moves on a real
   Status change and on nothing else (a Genesis-field write, a reorder within
   a column, a re-sent option list). ``observed_late`` is derived from the
   clock alone (the change predates this tick by more than one interval plus
   slack), so the enable-time backlog, a re-log after a prune and downtime all
   read as late without consulting any stored state;
4. ALWAYS emits a ``board`` heartbeat whose details carry the summary, in
   every mode and while paused. A pulse that stopped when the board was off
   would read "overdue" forever: the health MCP runs in another process and
   cannot see the server's ``GENESIS_BOARD_DISABLED``, and the morning report
   never asks whether a subsystem is enabled.

It writes nothing to GitHub (a test pins that). The writes come in the next
reconciler PR, behind ``live``.

Job health: a tick with no error records a success (pulse-only ticks
included), so one transient error on an install with the board off never reads
as "has never succeeded". A read error publishes ``last_error`` and NO counts:
nothing partial is ever presented as the board.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from genesis.board import config as board_config

logger = logging.getLogger(__name__)

JOB_ID = "board_reconciler"
INTERVAL_S = 300
# A change older than one interval plus this slack was not caught by the tick
# that should have seen it (a missed tick, downtime, enable-time backlog).
_LATE_SLACK_S = 120
IN_PROGRESS = "In Progress"
NO_STATUS = "No Status"
# The heartbeat row repeats every five minutes, so a runaway error message is
# bounded here; the full text always goes to the log with its traceback.
_ERROR_CHARS = 500


def _bounded(text: str) -> str:
    if len(text) <= _ERROR_CHARS:
        return text
    return (
        f"{text[:_ERROR_CHARS]} <omitted: {len(text) - _ERROR_CHARS} chars; full text in the log>"
    )


def _parse(ts: str | None) -> datetime | None:
    from genesis.observability.liveness import parse_iso_utc

    return parse_iso_utc(ts)


def _tracker_slug() -> str | None:
    tracker = board_config.tracker_repo()
    return "/".join(tracker).lower() if tracker else None


async def _read_board(db, ref: tuple[str, int], tick_started: datetime) -> dict:
    from genesis.board import projects_v2 as pv
    from genesis.db.crud import board as board_crud

    proj = await pv.get_project(*ref)
    if proj.closed:
        # board_setup refuses a closed project; a board closed after setup is
        # no longer the work board, so its counts are not published as if it were.
        raise pv.ProjectsError(f"project {ref[0]}#{ref[1]} is closed")
    items = (await pv.list_items(proj.id))["items"]

    by_status: Counter = Counter()
    by_genesis: Counter = Counter()
    by_kind: Counter = Counter()
    tracker = _tracker_slug()
    on_board = other_repo = 0
    in_progress = []
    for it in items:
        status = (it.get("status") or {}).get("name") or NO_STATUS
        by_status[status] += 1
        by_genesis[(it.get("genesis") or {}).get("name") or "none"] += 1
        content = it.get("content") or {}
        by_kind[content.get("__typename") or "Unknown"] += 1
        repo = ((content.get("repository") or {}).get("nameWithOwner") or "").lower()
        if tracker and repo:
            if repo != tracker:
                other_repo += 1
            elif content.get("state") == "OPEN" and not it.get("isArchived"):
                on_board += 1
        if status == IN_PROGRESS:
            in_progress.append(it)

    if tracker:
        owner, _, name = tracker.partition("/")
        totals = await pv.repo_open_counts(owner, name)
        coverage = {
            "on_board": on_board,
            "open_in_repo": totals["issues"] + totals["pull_requests"],
            "other_repo_items": other_repo,
        }
    else:
        coverage = {"on_board": None, "unavailable": "no public tracker configured"}

    # The board read above succeeded; a LOCAL failure to log drags must not
    # discard it. The counts publish, and the drag error rides beside them.
    try:
        drags = await _log_drags(db, board_crud, in_progress, tick_started)
    except Exception as exc:
        logger.error("board reconciler: drag log failed", exc_info=True)
        drags = {
            "in_progress": len(in_progress),
            "error": _bounded(f"{type(exc).__name__}: {exc}"),
        }
    return {
        "project": f"{ref[0]}#{ref[1]}",
        "items_total": len(items),
        "by_status": dict(by_status),
        "by_genesis": dict(by_genesis),
        "by_kind": dict(by_kind),
        "coverage": coverage,
        "drags": drags,
    }


async def _log_drags(db, board_crud, in_progress: list[dict], tick_started: datetime) -> dict:
    """One ``drag`` event per In Progress card per Status change; returns
    ``{"in_progress", "newly_logged"}`` (or ``unavailable`` before migration)."""
    if not in_progress:
        return {"in_progress": 0, "newly_logged": 0}
    if not await board_crud.tables_available(db):
        return {"in_progress": len(in_progress), "unavailable": "board tables not migrated"}
    late_before = tick_started - timedelta(seconds=INTERVAL_S + _LATE_SLACK_S)
    logged = 0
    async with board_crud.owned_connection(db) as own:
        for it in in_progress:
            updated = (it.get("status") or {}).get("updatedAt")
            content = it.get("content") or {}
            repo = (content.get("repository") or {}).get("nameWithOwner")
            changed = _parse(updated)
            row_id = await board_crud.append_event(
                own,
                event="drag",
                now=tick_started.isoformat(),
                repo=repo,
                issue_number=content.get("number"),
                project_item_id=it["id"],
                worker="board_reconciler",
                observed_change_key=f"{it['id']}@{updated}",
                detail={
                    "status_updated_at": updated,
                    "observed_late": changed is None or changed < late_before,
                    "kind": content.get("__typename"),
                },
            )
            if row_id is not None:
                logged += 1
    return {"in_progress": len(in_progress), "newly_logged": logged}


async def run_tick(rt, *, now: datetime | None = None) -> dict:
    """One reconciler tick. Never raises; returns the summary it published."""
    tick_started = now or datetime.now(UTC)
    summary: dict = {"tick_at": tick_started.isoformat()}
    error: BaseException | None = None
    try:
        paused = bool(rt.paused)
    except Exception as exc:
        logger.warning("board reconciler: pause check failed — skipping", exc_info=True)
        paused, error = None, exc
    mode = board_config.effective_mode()
    summary.update(mode=mode, paused=paused)

    if error is not None:
        summary["board_state"] = "pause_check_failed"
        summary["last_error"] = _bounded(f"{type(error).__name__}: {error}")
    elif paused:
        summary["board_state"] = "paused"
    elif mode == "off":
        summary["board_state"] = "off"
    elif (ref := board_config.project_ref()) is None:
        summary["board_state"] = "not_set_up"
    else:
        try:
            # All-or-nothing: the counts are merged only once the WHOLE read
            # has returned, so an error can never publish a partial board.
            summary.update(await _read_board(rt._db, ref, tick_started))
            summary["board_state"] = "ok"
        except Exception as exc:
            logger.error("board reconciler read failed", exc_info=True)
            error = exc
            summary["project"] = f"{ref[0]}#{ref[1]}"
            summary["board_state"] = "error"
            summary["last_error"] = _bounded(f"{type(exc).__name__}: {exc}")

    await _pulse(rt, summary)
    drag_error = (summary.get("drags") or {}).get("error")
    if error is not None:
        rt.record_job_failure(JOB_ID, exc=error)
    elif drag_error:
        rt.record_job_failure(JOB_ID, error=f"drag log failed: {drag_error}")
    else:
        rt.record_job_success(JOB_ID)
    return summary


async def _pulse(rt, summary: dict) -> None:
    from genesis.observability.types import Severity, Subsystem

    bus = getattr(rt, "_event_bus", None)
    if bus is None:
        return
    try:
        await bus.emit(
            Subsystem.BOARD,
            Severity.DEBUG,
            "heartbeat",
            f"board {summary.get('board_state')} (mode={summary.get('mode')})",
            **summary,
        )
    except Exception:
        logger.error("board heartbeat emit failed", exc_info=True)
