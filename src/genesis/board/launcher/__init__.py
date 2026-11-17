"""Builder launcher: a card moved to Build starts one builder in a tmux slot.

Spec E rev 5 (ticket E3a). The reconciler calls :func:`run_launcher` on every
tick. On a tick whose board read failed it passes no cards, and only the arming
record below is kept. On a good read, for every card in the Build column, keyed
``k = <item id>@<Status updatedAt>`` (one key per move: ``updatedAt`` moves on a
real Status change and nothing else, see the reconciler), the launcher:

1. does nothing unless it is ARMED (``config.builder_launch_armed``, a hand
   edit of the overlay). Arming and disarming are recorded
   (``launcher_enabled`` / ``launcher_disabled``); a move not later than the
   latest enable is never launched, and the cards already in Build at enable
   are listed to the owner once;
2. skips a move it has finished with (launched, refused, absorbed, three
   strikes), records a builder it finds up for a move whose attempt it lost
   track of, and closes an attempt a restart interrupted;
3. absorbs the move when the issue already has a live builder;
4. HOLDS on install state (``launch_queued``, keyed per move AND reason, so
   each new reason reaches the owner once): a builder already running, low free
   memory, a login that expires soon or cannot be read, the prompt template,
   slot script or builder guard missing. After the slot door refused (a slot
   taken, the box at its session cap) it asks the door's own capacity decision
   each pass and retries only once that would admit, so a long wait writes
   nothing;
5. REFUSES (``launch_refused``, final for the move) on a fact about the issue
   or the move: not an issue in the tracker repo, missing, closed, by an
   author outside OWNER / MEMBER / COLLABORATOR, edited after the move,
   premise checks incomplete, or an open closing PR already. A read that
   failed is neither: the move is retried next tick;
6. writes the issue body snapshot and the rendered prompt, records an approval
   request and resolves and consumes it as the move's own approval, writes
   ``builder_launching`` with the slot, session id, worktree and unit, and
   starts ``scripts/cc-slot.sh launch`` through ``systemd-run --user --wait``,
   so the pane outlives this server (MEASURED, spec E3-0 (a)).

The approval: a Build move IS the owner's approval for one builder run (owner
ruling ``01fc7f55``, spec E). It is recorded as an ``approval_requests`` row
resolved ``board:move:unattributed`` and consumed before the launch, which
``classify_resolver`` reads as ``unknown`` on purpose: nothing attests who moved
the card. ``autonomy/cli_policy.py`` (``manual_approval_required``) governs the
runtime's own autonomous CLI dispatch; this launcher starts an interactive slot
through the slot door instead, and the gate row is that run's approval record.
Any failure to write, resolve or consume the row means no launch.

Run state is derived: a live builder is a tmux session carrying
``GENESIS_BUILDER_KEY``; a finished one is an issue with an open closing PR
(from the board read). A builder's issue is its card's on the same board read
(the key starts with the card's item id; a builder whose card left the board
reads its issue from its own ``builder_launched`` row and its PRs live), and a
launch still starting is a live
``genesis-builder-*`` systemd unit, which systemd itself stops at
``RuntimeMaxSec``. ``board_events`` is the audit log and each move's outcome
record; no row count decides capacity.

Never raises into the reconciler: a failure is logged and returned in the
summary block (``error``), which the reconciler reports as a job failure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.board import config as board_config

logger = logging.getLogger(__name__)

# Outside readings. Tests replace them by name on this module, which reaches
# every call made from here; a probe calling another probe (each calls _exec)
# resolves it in probes, so a stub of _exec itself goes on probes.
from genesis.board.launcher.probes import (  # noqa: E402
    LAUNCH_TIMEOUT_S,
    LAUNCH_WAIT_S,  # noqa: F401  (re-exported for other board modules)
    LOCAL_TIMEOUT_S,  # noqa: F401  (re-exported for other board modules)
    PREMISE_TIMEOUT_S,  # noqa: F401  (re-exported for other board modules)
    UNIT_PREFIX,
    _exec,  # noqa: F401  (re-exported for other board modules)
    _guard_wired,
    _matcher_covers_bash,  # noqa: F401  (re-exported for other board modules)
    door_would_refuse,
    login_expiry,
    mem_available_kb,
    premise_rc,
    run_launch,
    session_issues,
    starting_units,
    stop_unit,
    tmux_sessions,
    unit_state,
)

WORKER = "board_launcher"
BUILD = "Build"
MAX_STRIKES = 3
RESOLVED_BY = "board:move:unattributed"
ALLOWED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
#: One issue's whole event history is read; a longer one fails closed.
EVENT_READ_LIMIT = 500
#: cc-slot.sh launch (spec E3c): 75 = slot taken or box at its session cap.
RC_REFUSED = 75
#: The builder guard (spec E3e). No launch until it is installed and wired.
GUARD_HOOK = Path("scripts") / "hooks" / "builder_guard.py"
_SLOT_RE = re.compile(r"^cc-([1-9][0-9]{0,3})$")
_FINAL_FOR_MOVE = ("builder_launched", "launch_refused", "move_absorbed")
_LATE_AFTER = timedelta(seconds=420)  # one tick interval plus slack


def _builders_dir() -> Path:
    return Path.home() / ".genesis" / "builders"


def _repo_root() -> Path:
    from genesis.env import repo_root

    return repo_root()


def _parse(ts: str | None) -> datetime | None:
    from genesis.observability.liveness import parse_iso_utc

    return parse_iso_utc(ts)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class _Tick:
    """One launcher pass: its database, clock, counts and notifications."""

    def __init__(self, rt, now: datetime, tracker: str):
        self.rt = rt
        self.db = rt._db
        self.now = now
        self.iso = now.isoformat()
        self.tracker = tracker
        self.settings = board_config.builder_launch_settings()
        self.out: dict = {
            "armed": True,
            "candidates": 0,
            "launched": 0,
            "held": 0,
            "refused": 0,
            "absorbed": 0,
            "failed": 0,
            "skipped": 0,
        }

    async def event(
        self,
        event: str,
        *,
        key: str,
        change_key: str,
        issue: int | None,
        item: str | None = None,
        reason: str | None = None,
        attempt: int | None = None,
        **detail,
    ) -> int | None:
        from genesis.db.crud import board as board_crud

        async with board_crud.owned_connection(self.db) as own:
            return await board_crud.append_event(
                own,
                event=event,
                now=self.iso,
                repo=self.tracker if issue is not None else None,
                issue_number=issue,
                project_item_id=item,
                attempt=attempt,
                worker=WORKER,
                reason=reason,
                observed_change_key=change_key,
                detail={"key": key, **detail},
            )

    async def notify(self, text: str, source_id: str) -> None:
        pipeline = getattr(self.rt, "_outreach_pipeline", None)
        if pipeline is None:
            logger.warning("builder launcher: no outreach pipeline; not sent: %s", text)
            return
        from genesis.outreach.types import OutreachCategory, OutreachRequest

        try:
            await pipeline.submit_raw(
                text,
                OutreachRequest(
                    category=OutreachCategory.ALERT,
                    # Outreach governance dedups on (signal_type, topic,
                    # category), not source_id, so each line carries its own.
                    topic=f"Builder launcher: {source_id}",
                    context=text,
                    salience_score=0.8,
                    signal_type="builder_launch",
                    source_id=f"builder_launch:{source_id}",
                ),
            )
        except Exception:
            logger.warning("builder launcher notification failed", exc_info=True)


async def _history(db, tracker: str, issue: int) -> list[dict] | None:
    """The issue's whole event history, newest first, or None when it is longer
    than one read (fail closed: no count is taken from a partial history)."""
    from genesis.db.crud import board as board_crud

    got = await board_crud.list_events(db, repo=tracker, issue_number=issue, limit=EVENT_READ_LIMIT)
    return got["items"] if got["listed"] >= got["total"] else None


def _of_key(history: list[dict], key: str, *events: str) -> list[dict]:
    """Rows of *history* (newest first) for the move *key*, by their change key:
    ``k`` itself or ``k#…``, so a row whose detail did not parse still counts."""
    return [
        e
        for e in history
        if (
            e.get("observed_change_key") == key
            or (e.get("observed_change_key") or "").startswith(f"{key}#")
        )
        and (not events or e["event"] in events)
    ]


def _strikes(history: list[dict], key: str) -> int:
    return sum(
        1
        for e in _of_key(history, key, "launch_failed")
        if (e.get("detail") or {}).get("strike") is not False
    )


async def _enabled_at(tick: _Tick, armed: bool) -> datetime | None:
    """Record an arming change, and return the latest enable time while armed."""
    from genesis.db.crud import board as board_crud

    latest = None
    for name in ("launcher_enabled", "launcher_disabled"):
        got = await board_crud.list_events(tick.db, event=name, limit=1)
        if got["items"] and (latest is None or got["items"][0]["id"] > latest["id"]):
            latest = got["items"][0]
    if armed and (latest is None or latest["event"] != "launcher_enabled"):
        mark = f"enabled@{tick.iso}"
        await tick.event("launcher_enabled", key=mark, change_key=mark, issue=None)
        tick.out["enabled_now"] = True
        return tick.now
    if not armed and latest is not None and latest["event"] == "launcher_enabled":
        mark = f"disabled@{tick.iso}"
        await tick.event("launcher_disabled", key=mark, change_key=mark, issue=None)
    return _parse(latest["created_at"]) if armed else None


async def _record_disarm(tick: _Tick) -> None:
    """A disarm seen mid-pass is recorded at once, so a re-arm before the next
    tick starts a new enable cutoff (a move made while disarmed never launches)."""
    mark = f"disabled@{tick.iso}"
    await tick.event("launcher_disabled", key=mark, change_key=mark, issue=None)


def _build_cards(items: list[dict]) -> list[dict]:
    cards = []
    for it in items:
        status = it.get("status") or {}
        if status.get("name") != BUILD or it.get("isArchived"):
            continue
        content = it.get("content") or {}
        cards.append(
            {
                "item": it["id"],
                "moved": _parse(status.get("updatedAt")),
                "key": f"{it['id']}@{status.get('updatedAt')}",
                "kind": content.get("__typename"),
                "number": content.get("number"),
                "repo": ((content.get("repository") or {}).get("nameWithOwner") or "").lower(),
            }
        )
    # Oldest move first: the queue drains in the order the owner moved cards.
    return sorted(cards, key=lambda c: c["moved"] or datetime.max.replace(tzinfo=UTC))


def _finished_numbers(items: list[dict], tracker: str) -> set[int]:
    """Tracker issues whose builder is done on this read: CLOSED (its PR merged,
    or it was closed by hand), or with an open closing PR from this repo. A
    builder's session can outlive its PR, so an OPEN-PR rule alone would count a
    merged build busy again."""
    out = set()
    for it in items:
        content = it.get("content") or {}
        if content.get("__typename") != "Issue":
            continue
        if ((content.get("repository") or {}).get("nameWithOwner") or "").lower() != tracker:
            continue  # another repo's issue N says nothing about tracker issue N
        if content.get("state") == "CLOSED":
            out.add(content.get("number"))
            continue
        nodes = (content.get("closedByPullRequestsReferences") or {}).get("nodes") or []
        if any(
            n
            and n.get("state") == "OPEN"
            and n.get("isCrossRepository") is False
            and ((n.get("repository") or {}).get("nameWithOwner") or "").lower() == tracker
            for n in nodes
        ):
            out.add(content.get("number"))
    return out


async def _off_board_finished(tick: _Tick, number: int) -> bool:
    """True when a builder's issue *number*, whose card is not on this read, is
    done: CLOSED, or with an open closing PR (read live). Any failed read is
    False: the builder holds its place."""
    from genesis.board import projects_v2 as pv

    try:
        owner, name = tick.tracker.split("/", 1)
        facts = await pv.launch_facts(owner, name, number)
    except Exception:
        logger.warning("builder launcher: off-board issue #%s unread", number, exc_info=True)
        return False
    if facts.get("state") == "CLOSED":
        return True
    return bool(facts.get("open_prs")) and not facts.get("open_prs_unresolved")


def _board_numbers(items: list[dict], tracker: str) -> set[int]:
    """Tracker issue numbers with a card on this read."""
    return {
        (it.get("content") or {}).get("number")
        for it in items
        if (it.get("content") or {}).get("__typename") == "Issue"
        and (((it.get("content") or {}).get("repository") or {}).get("nameWithOwner") or "").lower()
        == tracker
    }


async def run_launcher(rt, items: list[dict] | None, now: datetime) -> dict:
    """One launcher pass. *items* is the tick's board read, or None when the
    read failed (only the arming record is kept then). Never raises."""
    try:
        return await _run(rt, items, now)
    except Exception as exc:
        logger.error("builder launcher failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}


async def _run(rt, items: list[dict] | None, now: datetime) -> dict:
    from genesis.db.crud import board as board_crud

    armed, why = board_config.builder_launch_armed()
    tracker_pair = board_config.tracker_repo()
    if not await board_crud.tables_available(rt._db):
        return {"armed": armed, "unavailable": "board tables not migrated"}
    if tracker_pair is None:
        return {"armed": armed, "unavailable": "no public tracker configured"}
    tick = _Tick(rt, now, "/".join(tracker_pair).lower())
    enabled_at = await _enabled_at(tick, armed)
    if not armed:
        return {"armed": False, "reason": why}
    if items is None:
        tick.out["board_read"] = "failed"
        return tick.out

    cards = _build_cards(items)
    tick.out["candidates"] = len(cards)
    fresh = [c for c in cards if c["moved"] is not None and c["moved"] > enabled_at]
    tick.out["pre_enable"] = len(cards) - len(fresh)
    await _list_backlog(tick, [c for c in cards if c not in fresh])
    if not fresh:
        return tick.out

    # Units BEFORE sessions: a launch that finishes between the two reads is
    # then in at least one of them (a unit seen and its session seen counts
    # twice for one tick, never zero times). An attempt still starting holds a
    # place whether or not its card is still in Build.
    starting = await starting_units()
    sessions = await tmux_sessions()
    # Each builder's issue is pinned on its own tmux session by the slot door.
    issues = await session_issues(sessions)
    finished = _finished_numbers(items, tick.tracker)
    on_board = _board_numbers(items, tick.tracker)
    busy = starting
    off_board = []
    # Per session, not per key: two sessions carrying one key are two builders.
    for name, key in sessions.items():
        if not key:
            continue
        number = issues.get(name)
        if number is None or (number in on_board and number not in finished):
            busy += 1  # no pinned issue reads as building (fail closed)
        elif number not in on_board:
            off_board.append(number)
    # A builder whose card left the board holds its place unless its issue is
    # done (read live). Read only when the answer can free a place.
    capacity = board_config.builder_launch_settings()["capacity"]
    for number in off_board:
        if busy >= capacity or not await _off_board_finished(tick, number):
            busy += 1

    for card in fresh:
        if tick.out.get("disarmed"):
            break  # a disarm seen mid-pass ends it: no further checks or launches
        # True when the move now holds a place: launched, or launched with an
        # outcome nobody could read.
        if await _one(tick, card, sessions, issues, busy):
            busy += 1
            sessions = await tmux_sessions()
            issues = await session_issues(sessions)
    return tick.out


async def _hold(tick: _Tick, card: dict, kind: str, reason: str, **detail) -> None:
    """Hold the move: one row and one owner line per move and reason kind."""
    tick.out["held"] += 1
    row = await tick.event(
        "launch_queued",
        key=card["key"],
        change_key=f"{card['key']}#q:{kind}",
        issue=card["number"],
        item=card["item"],
        reason=reason,
        hold=kind,
        **detail,
    )
    if row is not None:
        await tick.notify(
            f"Build #{card['number']} is waiting: {reason}.", f"held:{kind}:{card['key']}"
        )


async def _refuse(tick: _Tick, card: dict, reason: str) -> None:
    tick.out["refused"] += 1
    label = f"#{card['number']}" if card["number"] else "card " + card["item"]
    row = await tick.event(
        "launch_refused",
        key=card["key"],
        change_key=card["key"],
        issue=card["number"],
        item=card["item"],
        reason=reason,
    )
    if row is not None:
        await tick.notify(
            f"Build {label} was not launched: {reason}. Move the card again to retry.",
            f"refused:{card['key']}",
        )


async def _launched(tick: _Tick, card: dict, attempt_row: dict | None, **detail) -> None:
    info = (attempt_row or {}).get("detail") or {}
    await tick.event(
        "builder_launched",
        key=card["key"],
        change_key=card["key"],
        issue=card["number"],
        item=card["item"],
        slot=info.get("slot"),
        session_id=info.get("session_id"),
        **detail,
    )


async def _settle(tick: _Tick, card: dict, history: list[dict], sessions: dict) -> str | None:
    """Close out what an earlier attempt left. Returns ``"done"`` when the move
    now has its builder, ``"wait"`` while an attempt is still starting, else None."""
    key = card["key"]
    attempts = _of_key(history, key, "builder_launching")
    if key in sessions.values():
        # Up, with no builder_launched: a restart or a slow unit lost track of it.
        await _launched(tick, card, attempts[0] if attempts else None, settled=True)
        return "done"
    if not attempts:
        return None
    last = attempts[0]
    attempt = last.get("attempt") or len(attempts)
    if any(e.get("attempt") == attempt for e in _of_key(history, key, "launch_failed")):
        return None
    unit = (last.get("detail") or {}).get("unit")
    if unit and await unit_state(unit) != "gone":
        return "wait"
    await _strike(
        tick,
        card,
        attempt,
        "no builder session after the launch",
        history,
        (last.get("detail") or {}).get("slot"),
    )
    return None


def _door_refused(history: list[dict], key: str) -> bool:
    """True when this move's last attempt was a slot-door refusal (exit 75)."""
    for e in _of_key(history, key, "launch_failed", "builder_launched"):
        detail = e.get("detail") or {}
        return detail.get("strike") is False and "sessions" in detail
    return False


async def _one(tick: _Tick, card: dict, sessions: dict, issues: dict, busy: int) -> bool:
    """Handle one fresh Build move. True when it holds a builder place now."""
    key, number = card["key"], card["number"]
    if card["kind"] != "Issue" or card["repo"] != tick.tracker or not number:
        await _refuse(
            tick, {**card, "number": None}, "the card is not an issue in the tracker repo"
        )
        return False
    history = await _history(tick.db, tick.tracker, number)
    if history is None:
        await _hold(tick, card, "history", "its launch history is longer than one read")
        return False
    if _of_key(history, key, *_FINAL_FOR_MOVE):
        tick.out["skipped"] += 1
        return False
    settled = await _settle(tick, card, history, sessions)
    if settled is not None:
        tick.out["skipped"] += 1
        return False  # a starting attempt's place was counted up front
    history = await _history(tick.db, tick.tracker, number) or history
    if _strikes(history, key) >= MAX_STRIKES:
        tick.out["skipped"] += 1
        return False

    # Absorb: the issue already has a live builder (another move's key).
    live_for_issue = [k for n, k in sessions.items() if k and k != key and issues.get(n) == number]
    if live_for_issue:
        tick.out["absorbed"] += 1
        await tick.event(
            "move_absorbed",
            key=key,
            change_key=key,
            issue=number,
            item=card["item"],
            builder=live_for_issue[0],
        )
        return False
    # After a door refusal, ask the door's own capacity decision live and wait
    # quietly (no rows) while it would still refuse: each retry writes a gate
    # row and events, and a stored snapshot of the sessions goes stale.
    if _door_refused(history, key) and await door_would_refuse(
        sum(1 for n in sessions if _SLOT_RE.match(n))
    ):
        tick.out["held"] += 1
        return False

    # Install state first: it is local and cheap, and a held move should not
    # re-read GitHub every tick.
    hold = _install_hold(tick, busy)
    if hold:
        await _hold(tick, card, *hold)
        return False

    rc = await premise_rc(number, tick.tracker)
    if rc == 1:
        await _refuse(tick, card, "its premise checks are not complete")
        return False
    if rc != 0:
        logger.warning("builder launcher: premise gate could not read #%s; retrying", number)
        tick.out["skipped"] += 1
        return False

    from genesis.board import projects_v2 as pv

    owner, _, name = tick.tracker.partition("/")
    # Read after the premise gate, so an edit between the two reads shows as
    # an edit after the move, and the snapshot is never older than the check.
    try:
        facts = await pv.launch_facts(owner, name, number)
    except pv.ProjectsError as exc:
        if str(exc).startswith("no issue"):
            await _refuse(tick, card, "the issue does not exist")
        else:
            logger.warning("builder launcher: issue read failed for #%s", number, exc_info=True)
            tick.out["skipped"] += 1
        return False
    if facts["state"] != "OPEN":
        await _refuse(tick, card, "the issue is not open")
        return False
    if facts["author_association"] not in ALLOWED_ASSOCIATIONS:
        await _refuse(tick, card, f"its author is {facts['author_association']}, not a maintainer")
        return False
    edited = _parse(facts["last_edited_at"])
    if edited is not None and edited > card["moved"]:
        await _refuse(tick, card, "the issue was edited after the card was moved")
        return False
    if facts["open_prs"]:
        await _refuse(tick, card, f"it already has an open PR (#{facts['open_prs'][0]})")
        return False
    if facts["open_prs_unresolved"]:
        logger.warning("builder launcher: #%s has more closing PRs than one read", number)
        tick.out["skipped"] += 1
        return False
    return await _launch(tick, card, facts, sessions, history)


def _install_hold(tick: _Tick, busy: int) -> tuple[str, str] | None:
    """``(kind, reason)`` of the first install-state hold, or None."""
    s = tick.settings
    if busy >= s["capacity"]:
        return "capacity", "another builder is running"
    mem = mem_available_kb()
    if mem is None:
        return "memory:unread", "free memory could not be read"
    if mem < s["min_free_gb"] * 1024 * 1024:
        return "memory:low", f"the box has under {s['min_free_gb']} GB of free memory"
    expiry = login_expiry()
    if expiry is None:
        return "login:unread", "the Claude Code login expiry could not be read"
    if expiry < _utcnow() + timedelta(hours=s["login_margin_hours"]):
        return "login:soon", (
            f"the Claude Code login expires within {s['login_margin_hours']} hours: re-login needed"
        )
    root = _repo_root()
    if not (root / "config" / "BUILDER_PROMPT.md").is_file():
        return "install:template", "the builder prompt template is missing"
    if not (root / "scripts" / "cc-slot.sh").is_file():
        return "install:script", "the slot script is missing"
    if not ((root / GUARD_HOOK).is_file() and _guard_wired(root / ".claude" / "settings.json")):
        return "install:guard", "the builder guard is not installed"
    return None


async def _list_backlog(tick: _Tick, cards: list[dict]) -> None:
    """Tell the owner once which cards were already in Build when the launcher
    was armed: one row per card (``launch_queued``, hold ``pre_enable``), so a
    first armed tick whose board read failed still lists them on the next."""
    from genesis.db.crud import board as board_crud

    listed = []
    for card in cards:
        if card["kind"] != "Issue" or card["repo"] != tick.tracker or not card["number"]:
            continue
        seen = await board_crud.list_events(
            tick.db, repo=tick.tracker, issue_number=card["number"], limit=EVENT_READ_LIMIT
        )
        if _of_key(seen["items"], card["key"], *_FINAL_FOR_MOVE, "launch_queued"):
            continue  # launched, refused, absorbed or already listed under this move
        row = await tick.event(
            "launch_queued",
            key=card["key"],
            change_key=f"{card['key']}#q:pre_enable",
            issue=card["number"],
            item=card["item"],
            hold="pre_enable",
            reason="it was in Build when the launcher was armed",
        )
        if row is not None:
            listed.append(f"#{card['number']}")
    if listed:
        await tick.notify(
            f"Builder launcher armed. Cards already in Build are not launched: "
            f"{', '.join(listed)}. Move one out of Build and back to launch it.",
            f"backlog:{tick.iso}",
        )


def _render(template: str, *, issue: int, snapshot: Path, worktree: Path) -> str:
    return (
        template.replace("{issue}", str(issue))
        .replace("{snapshot}", str(snapshot))
        .replace("{worktree}", str(worktree))
    )


def _free_slot(sessions: dict, first: int) -> int:
    taken = {int(m.group(1)) for name in sessions if (m := _SLOT_RE.match(name))}
    slot = first
    while slot in taken:
        slot += 1
    return slot


async def _strike(
    tick: _Tick, card: dict, attempt: int, reason: str, history: list[dict], slot: int | None
) -> None:
    row = await tick.event(
        "launch_failed",
        key=card["key"],
        change_key=f"{card['key']}#a{attempt}",
        issue=card["number"],
        item=card["item"],
        attempt=attempt,
        reason=reason[:400],
        strike=True,
    )
    if row is None:
        return  # this attempt's outcome was already recorded
    tick.out["failed"] += 1
    strikes = _strikes(history, card["key"]) + 1
    if strikes >= MAX_STRIKES:
        log = f" See ~/.genesis/logs/cc_exit_{slot}.log." if slot else ""
        await tick.notify(
            f"Build #{card['number']} failed to launch {strikes} times and will not be "
            f"retried. Last: {reason[:200]}.{log} Move the card again to retry.",
            f"strikes:{card['key']}",
        )


async def _launch(tick: _Tick, card: dict, facts: dict, sessions: dict, history) -> bool:
    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.types import ActionClass, ApprovalStatus
    from genesis.db.crud import approval_requests as approval_crud

    key, number = card["key"], card["number"]
    root = _repo_root()
    body = facts["body"]
    sha = hashlib.sha256(body.encode()).hexdigest()
    folder = _builders_dir() / str(number)
    snapshot = folder / f"issue-{sha[:12]}.md"
    prompt = folder / f"prompt-{sha[:12]}.txt"
    worktree = root / ".claude" / "worktrees" / f"build-{number}"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(body)
        template = (root / "config" / "BUILDER_PROMPT.md").read_text()
        prompt.write_text(_render(template, issue=number, snapshot=snapshot, worktree=worktree))
    except OSError as exc:
        await _hold(
            tick, card, "install:snapshot", f"the snapshot could not be written ({exc.strerror})"
        )
        return False

    # Every attempt uses its own number, whether it failed at the gate (no
    # builder_launching row) or later, so no strike is absorbed by the dedupe.
    attempt = 1 + max(
        (
            e.get("attempt") or 0
            for e in _of_key(history, key, "builder_launching", "launch_failed")
        ),
        default=0,
    )
    slot = _free_slot(sessions, tick.settings["first_slot"])
    session_id = str(uuid.uuid4())
    unit = f"{UNIT_PREFIX}{number}-{uuid.uuid4().hex[:8]}"
    # The checks above took network round trips; a disarm (or the kill switch)
    # in the meantime cancels the launch before its gate row exists.
    if not board_config.builder_launch_armed()[0]:
        tick.out["skipped"] += 1
        tick.out["disarmed"] = True
        await _record_disarm(tick)
        return False

    # The gate: one approval row per attempt that runs, resolved as the move's
    # own approval and consumed BEFORE anything starts. Any failure: no launch.
    approval = ApprovalManager(db=tick.db)
    try:
        request_id = await approval.request_approval(
            action_type="builder_launch",
            action_class=ActionClass.COSTLY_REVERSIBLE,  # a builder pushes code
            description=f"Start one builder for issue #{number} (card moved to Build)",
            context=json.dumps({"key": key, "issue": number, "sha256": sha}),
            timeout_seconds=3600,
        )
        if not await approval.resolve(
            request_id, status=ApprovalStatus.APPROVED, resolved_by=RESOLVED_BY
        ):
            raise RuntimeError("the approval row was not pending")
        if not await approval_crud.mark_consumed(tick.db, request_id, consumed_at=tick.iso):
            raise RuntimeError("the approval row was already consumed")
    except Exception as exc:
        logger.error("builder launcher: gate failed for #%s", number, exc_info=True)
        await _strike(tick, card, attempt, f"approval gate failed: {exc}", history, None)
        return False

    row = await tick.event(
        "builder_launching",
        key=key,
        change_key=f"{key}#a{attempt}",
        issue=number,
        item=card["item"],
        attempt=attempt,
        slot=slot,
        session_id=session_id,
        unit=unit,
        worktree=str(worktree),
        approval_id=request_id,
        sha256=sha,
        snapshot=str(snapshot),
    )
    if row is None:
        # Attempt numbers collided (two passes at once): this one never started.
        logger.error("builder launcher: attempt %s of %s already recorded", attempt, key)
        tick.out["skipped"] += 1
        return False
    argv = [
        "systemd-run",
        "--user",
        "--collect",
        "--wait",
        "--quiet",
        "--pipe",
        f"--unit={unit}",
        f"--property=RuntimeMaxSec={LAUNCH_TIMEOUT_S}",
        f"--working-directory={root}",
        str(root / "scripts" / "cc-slot.sh"),
        "launch",
        "--issue",
        str(number),
        "--key",
        key,
        "--session-id",
        session_id,
        "--slot",
        str(slot),
        "--prompt-file",
        str(prompt),
    ]
    if not board_config.builder_launch_armed()[0]:
        # Disarmed after the gate: nothing has started, so this attempt closes
        # as a failure that is not a strike.
        tick.out["skipped"] += 1
        tick.out["disarmed"] = True
        await _record_disarm(tick)
        await tick.event(
            "launch_failed",
            key=key,
            change_key=f"{key}#a{attempt}",
            issue=number,
            item=card["item"],
            attempt=attempt,
            reason="the launcher was disarmed",
            strike=False,
        )
        return False
    rc, output = await run_launch(argv)
    if rc == 124:
        await stop_unit(unit)
    try:
        now_sessions = await tmux_sessions()
    except Exception:
        logger.warning("builder launcher: tmux read after launch failed", exc_info=True)
        now_sessions = None
    if now_sessions is not None and key in now_sessions.values():
        await _launched(tick, card, {"detail": {"slot": slot, "session_id": session_id}}, rc=rc)
        tick.out["launched"] += 1
        late = " (seen late)" if card["moved"] < tick.now - _LATE_AFTER else ""
        await tick.notify(
            f"Build #{number} started in cc-{slot}{late}. It answers through Remote Control "
            f"as 'build #{number}', or attach to cc-{slot}.",
            f"launched:{key}",
        )
        return True
    if now_sessions is None:
        # Unknown whether it came up: it holds a place until the next pass
        # settles it from tmux.
        tick.out["skipped"] += 1
        return True
    if rc == RC_REFUSED:
        await tick.event(
            "launch_failed",
            key=key,
            change_key=f"{key}#a{attempt}",
            issue=number,
            item=card["item"],
            attempt=attempt,
            reason="the slot door refused",
            strike=False,
            sessions=sorted(now_sessions),
        )
        await _hold(
            tick,
            card,
            "door",
            "the slot door refused (a slot was taken or the box is at its session cap)",
        )
        return False
    await _strike(tick, card, attempt, f"rc {rc}: {output.strip()[-300:]}", history, slot)
    return False
