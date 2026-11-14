"""Board reconciler: reads the work board every tick, and in ``live`` writes it.

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
4. after a good read, the write phase (#4400 Part 1), in this order:

   * records the card id on every ``board_links`` row whose issue now has a
     card (a local write);
   * fills an EMPTY Status column as desired state: In Review for an open PR,
     and for an open issue with an open same-repo closing PR; Proposed for any
     other open issue. Only a Status this tick read as empty is written, and
     each card is re-read just before its write, so a card someone moved, or
     dragged back, stays where they put it (skipped ones count as ``moved``);
   * when open repo items are missing from the board (archived cards count as
     on it), lists the repo's open issues and PRs and adds each one Genesis
     has never added before, so a card the owner removed stays removed (its
     ``item_added`` row is never pruned). A listing that finds nothing to add
     is not repeated while the gap is unchanged, for up to an hour.

   Every write happens only in ``live``, re-checked immediately before it. A
   Status change is validated first (no request, no row, no budget), then
   audited BEFORE its request (``status_write``, keyed ``<item>:<to>:<tick>``);
   an add is recorded once it succeeds (``item_added``, keyed
   ``<content id>:<tick>``), so a failed add is retried next tick. GitHub
   writes go :data:`WRITE_GAP_S` apart (GitHub asks for one second between
   writes), at most :data:`WRITES_PER_TICK` per tick, which is what binds on a
   normal schedule; :data:`WRITES_PER_HOUR`, counted from those rows, bounds
   extra ticks (a restart, catch-up). A failed REQUEST (an HTTP error, a
   timeout, a rate limit) stops the tick's writes; an error about one item
   skips that item. Outside ``live`` nothing is written and the summary
   carries would-write counts instead (``dry_run``);
5. ALWAYS emits a ``board`` heartbeat whose details carry the summary, in
   every mode and while paused. A pulse that stopped when the board was off
   would read "overdue" forever: the health MCP runs in another process and
   cannot see the server's ``GENESIS_BOARD_DISABLED``, and the morning report
   never asks whether a subsystem is enabled.

No path writes In Progress: ``projects_v2.set_status`` refuses it.

Job health: a tick with no error records a success (pulse-only ticks
included), so one transient error on an install with the board off never reads
as "has never succeeded". A rate-limited tick is a success too: it stopped on
purpose and resumes next tick. Any other write failure is a job failure, with
the read's counts and the writes already made kept. A read error publishes
``last_error`` and NO counts: nothing partial is ever presented as the board.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import UTC, datetime, timedelta

from genesis.board import config as board_config

logger = logging.getLogger(__name__)

JOB_ID = "board_reconciler"
INTERVAL_S = 300
#: GitHub writes per rolling hour. A chosen number (#4400): under GitHub's
#: documented 500-per-hour content-creation limit, which every other session's
#: gh writes on the same account share.
WRITES_PER_HOUR = 200
#: The hour spread evenly over the ticks in it.
WRITES_PER_TICK = WRITES_PER_HOUR // (3600 // INTERVAL_S)
#: GitHub's GraphQL docs ask for at least one second between write requests.
WRITE_GAP_S = 1
_WRITE_EVENTS = ("item_added", "status_write")
_UNRESOLVED = "unresolved"
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


async def _read_board(db, ref: tuple[str, int], tick_started: datetime) -> tuple[dict, tuple]:
    """The summary, plus ``(project, items)`` for the write phase."""
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
    }, (proj, items)


def _fill_target(item: dict, tracker: str | None) -> str | None:
    """The column an EMPTY card belongs in, None to leave it alone, or
    :data:`_UNRESOLVED` when its closing PRs could not all be read."""
    if item.get("isArchived") or (item.get("status") or {}).get("name"):
        return None
    content = item.get("content") or {}
    kind = content.get("__typename")
    if kind not in ("Issue", "PullRequest") or content.get("state") != "OPEN":
        return None
    if not tracker or _repo_of(content) != tracker:
        return None
    if kind == "PullRequest":
        return "In Review"
    refs = content.get("closedByPullRequestsReferences")
    if not isinstance(refs, dict):
        return _UNRESOLVED
    nodes = [n for n in refs.get("nodes") or [] if n]
    # Only an open PR IN the tracker repo, from a branch of it, counts: a fork
    # PR (isCrossRepository) or a PR in another repo that names the issue is
    # not work in review here.
    if any(
        n.get("state") == "OPEN" and n.get("isCrossRepository") is False and _repo_of(n) == tracker
        for n in nodes
    ):
        return "In Review"
    if (refs.get("totalCount") or 0) > len(nodes):
        return _UNRESOLVED
    return "Proposed"


def _repo_of(node: dict) -> str:
    return ((node.get("repository") or {}).get("nameWithOwner") or "").lower()


#: tracker -> (gap, when, excluded) of the last repository listing that found
#: nothing to add. Coverage stays short for good when the owner removes a card
#: (the backfill never re-adds it), so without this every tick would re-list
#: the whole repo for nothing. The listing re-runs when the gap changes, and at
#: least hourly so an issue opened while another closed is still found.
#: In memory only: a restart costs one extra listing.
_QUIET: dict[str, tuple[int, datetime, int]] = {}
_QUIET_FOR = timedelta(hours=1)


async def _write_phase(db, project, items: list[dict], summary: dict, now: datetime) -> dict:
    """Write the board in ``live``; count what would be written otherwise.
    Returns the summary's ``writes`` block, plus private ``_error`` /
    ``_rate_limited`` keys the caller pops."""
    from genesis.board import projects_v2 as pv
    from genesis.db.crud import board as board_crud

    tracker = _tracker_slug()
    fills, unresolved = [], 0
    for it in items:
        target = _fill_target(it, tracker)
        if target == _UNRESOLVED:
            unresolved += 1
        elif target is not None:
            fills.append((it, target))
    if not await board_crud.tables_available(db):
        return {"unavailable": "board tables not migrated"}
    card_by_number: dict[int, str] = {}
    open_on_board = 0  # archived cards included: they are on the board
    for it in items:
        content = it.get("content") or {}
        if not tracker or _repo_of(content) != tracker:
            continue
        if content.get("state") == "OPEN":
            open_on_board += 1
        if content.get("number") and not it.get("isArchived"):
            card_by_number[content["number"]] = it["id"]
    links = await board_crud.links_without_item(db, repo=tracker) if tracker else []
    linkable = [
        (ln, card_by_number[ln["issue_number"]])
        for ln in links
        if ln["issue_number"] in card_by_number
    ]
    open_in_repo = (summary.get("coverage") or {}).get("open_in_repo")
    gap = max(0, open_in_repo - open_on_board) if isinstance(open_in_repo, int) else 0

    if not board_config.writes_allowed():
        return {
            "dry_run": True,
            "would_set_status": len(fills),
            "would_add_at_most": gap,
            "would_link": len(linkable),
        }

    tick = now.isoformat()
    used = await board_crud.count_events_since(
        db, events=_WRITE_EVENTS, since=(now - timedelta(hours=1)).isoformat()
    )
    budget = max(0, min(WRITES_PER_TICK, WRITES_PER_HOUR - used))
    out: dict = {
        "added": 0,
        "status_set": 0,
        "links_set": 0,
        "failed": 0,
        "unresolved": unresolved,
        "excluded": 0 if gap == 0 else None,
        "moved": 0,
        "budget_left": budget,
        "stopped_on": None,
    }
    errors: list[str] = []
    sent = 0

    async def may_send() -> bool:
        nonlocal sent
        if sent:
            await asyncio.sleep(WRITE_GAP_S)
        # Checked AFTER the gap, so a switch out of live during it still stops.
        if sent >= budget:
            out["stopped_on"] = "budget"
        elif not board_config.writes_allowed():
            out["stopped_on"] = "mode"
        if out["stopped_on"]:
            return False
        sent += 1
        return True

    def failed(exc: BaseException) -> None:
        if isinstance(exc, pv.RequestFailed):
            # The request itself failed: stop, whether or not it named a limit.
            out["stopped_on"] = (
                "rate_limited" if isinstance(exc, pv.RateLimited) else "request_failed"
            )
        else:
            out["failed"] += 1
        logger.warning("board reconciler write failed: %s", exc, exc_info=exc)
        text = _bounded(f"{type(exc).__name__}: {exc}")
        if isinstance(exc, pv.RateLimited):
            out["_rate_limited"] = text
        else:
            errors.append(text)

    try:
        async with board_crud.owned_connection(db) as own:
            for link, card in linkable:
                if not board_config.writes_allowed():
                    out["stopped_on"] = "mode"
                    break
                if await board_crud.set_project_item(
                    own, link_id=link["id"], project_item_id=card, now=tick
                ):
                    out["links_set"] += 1

            for it, target in fills:
                if out["stopped_on"]:
                    break
                try:
                    # Validated before it is audited or spends budget: a board
                    # missing a column refuses here at no cost.
                    pv.status_option(project, None, target)
                except pv.ProjectsError as exc:
                    failed(exc)
                    continue
                if not await may_send():
                    break
                # Re-read just before writing (owner ruling 2026-11-13): a card
                # placed since the tick's read keeps its column. Its slot was
                # not spent on a write, so it goes back to the budget.
                try:
                    current = await pv.item_status(it["id"])
                except pv.ProjectsError as exc:
                    sent -= 1
                    failed(exc)
                    continue
                if current is not None:
                    sent -= 1
                    out["moved"] += 1
                    continue
                # The re-read was an await: the mode may have left live during it.
                # Checked before the audit row (a local write), so a stop here
                # leaves no row for a write that never happens.
                if not board_config.writes_allowed():
                    sent -= 1
                    out["stopped_on"] = "mode"
                    break
                content = it["content"]
                await board_crud.append_event(
                    own,
                    event="status_write",
                    now=tick,
                    repo=tracker,
                    issue_number=content.get("number"),
                    project_item_id=it["id"],
                    worker=JOB_ID,
                    observed_change_key=f"{it['id']}:{target}:{tick}",
                    detail={"from": None, "to": target, "kind": content.get("__typename")},
                )
                try:
                    await pv.set_status(project, it["id"], None, target)
                    out["status_set"] += 1
                except pv.ProjectsError as exc:
                    failed(exc)

            quiet = _QUIET.get(tracker or "")
            if quiet and quiet[0] == gap and now - quiet[1] < _QUIET_FOR:
                out["excluded"] = quiet[2]
            elif not out["stopped_on"] and gap > 0:
                owner, _, name = tracker.partition("/")
                content_list = await pv.open_content(owner, name)
                present = {(it.get("content") or {}).get("id") for it in items}
                added_before = await board_crud.added_numbers(db, repo=tracker)
                missing = [c for c in content_list if c["id"] not in present]
                fresh = [c for c in missing if c["number"] not in added_before]
                out["excluded"] = len(missing) - len(fresh)
                if fresh:
                    _QUIET.pop(tracker, None)
                else:
                    _QUIET[tracker] = (gap, now, out["excluded"])
                for c in fresh:
                    if out["stopped_on"] or not await may_send():
                        break
                    try:
                        card = await pv.add_item(project.id, c["id"])
                    except pv.ProjectsError as exc:
                        failed(exc)
                        continue
                    await board_crud.append_event(
                        own,
                        event="item_added",
                        now=tick,
                        repo=tracker,
                        issue_number=c["number"],
                        project_item_id=card,
                        worker=JOB_ID,
                        observed_change_key=f"{c['id']}:{tick}",
                        detail={"kind": c["kind"]},
                    )
                    out["added"] += 1
    except pv.ProjectsError as exc:
        failed(exc)  # the repository listing: a request failure stops the tick
    except Exception as exc:
        # Anything else (a local write, an unexpected shape) ends the phase but
        # keeps the counts of the writes that did happen.
        logger.error("board reconciler write phase failed", exc_info=True)
        errors.append(_bounded(f"{type(exc).__name__}: {exc}"))

    out["budget_left"] = budget - sent
    if errors:
        more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        out["_error"] = errors[0] + more
    return out


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
    write_error: str | None = None
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
            read, board = await _read_board(rt._db, ref, tick_started)
            summary.update(read)
            summary["board_state"] = "ok"
        except Exception as exc:
            logger.error("board reconciler read failed", exc_info=True)
            error = exc
            board = None
            summary["project"] = f"{ref[0]}#{ref[1]}"
            summary["board_state"] = "error"
            summary["last_error"] = _bounded(f"{type(exc).__name__}: {exc}")
        if board is not None:
            # The read succeeded; a failure while writing keeps its counts.
            try:
                writes = await _write_phase(rt._db, *board, summary, tick_started)
            except Exception as exc:
                logger.error("board reconciler write phase failed", exc_info=True)
                writes = {"_error": _bounded(f"{type(exc).__name__}: {exc}")}
            write_error = writes.pop("_error", None)
            rate_limited = writes.pop("_rate_limited", None)
            summary["writes"] = writes
            if write_error or rate_limited:
                summary["last_error"] = write_error or rate_limited

    await _pulse(rt, summary)
    drag_error = (summary.get("drags") or {}).get("error")
    if error is not None:
        rt.record_job_failure(JOB_ID, exc=error)
    elif drag_error:
        rt.record_job_failure(JOB_ID, error=f"drag log failed: {drag_error}")
    elif write_error:
        rt.record_job_failure(JOB_ID, error=f"board write failed: {write_error}")
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
