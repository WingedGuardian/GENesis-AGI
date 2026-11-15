"""Builder watch: what happens to a running builder (spec E rev 5, ticket E3b).

The launcher calls :func:`run_watch` at the start of every pass, armed or not,
with the tick's board read (None when it failed). For every builder it launched
that has not ended (a ``builder_launched`` row with no ``builder_paused``,
``builder_exited`` or ``builder_closed`` row for the same move), it:

- **notices an exit**: the builder's tmux session is gone. With an open closing
  PR on the board it is ``builder_closed`` (it finished); otherwise
  ``builder_exited``, with the status from the slot's exit log and one owner
  line. Nothing relaunches it; moving its card out of Build and back resumes it;
- **pauses** it when its card is back in Ready or Proposed and it has no open
  closing PR: SIGTERM to its claude (measured, spec E3-0 (d): the conversation
  resumes), then the session is closed, ``builder_paused`` and one owner line.
  Any other column does nothing;
- **closes** it an hour after its PR opened, unless a client is attached to its
  session (then it waits, re-checked every tick): ``builder_closed``.

Pause and close need a good board read; exit detection does not. A move that
was held (``launch_queued``) and whose card left Build is ``launch_withdrawn``,
once.

Every stop writes its row only after the session is confirmed gone, so a stop
that failed is retried next tick, and an exit seen in between is still an exit.
Never raises: a failure is returned in the summary block.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

#: Owner ruling (ego 63975057): the pane closes this long after the PR opens.
CLOSE_AFTER = timedelta(hours=1)
PAUSE_COLUMNS = ("Ready", "Proposed")
#: SIGTERM ends a builder in about a second (spec E3-0 (d)); this bounds a hang.
STOP_WAIT_S = 30
KILL_WAIT_S = 5
_EXIT_LINE = re.compile(
    r"^=== (?P<at>\S+) cc-(?P<slot>\d+) claude exited status=(?P<status>\S+) ===$"
)


def _logs_dir() -> Path:
    return Path.home() / ".genesis" / "logs"


# ─── outside readings and actions (each replaced in tests) ──────────────────


async def pane_pids(slot: int) -> list[int]:
    from genesis.board import launcher

    rc, out = await launcher._exec(
        ["tmux", "list-panes", "-s", "-t", f"=cc-{slot}", "-F", "#{pane_pid}"],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    return [int(x) for x in out.split() if x.isdigit()] if rc == 0 else []


def claude_pids_under(panes: list[int], proc_root: Path = Path("/proc")) -> list[int]:
    """Every claude process descending from one of *panes*."""
    from genesis.cc.slot_liveness import _ppid_of, claude_pids

    found = []
    for pid, _headless in claude_pids(proc_root) or []:
        cur, hops = pid, 0
        while cur and cur > 1 and hops < 64:
            if cur in panes:
                found.append(pid)
                break
            cur, hops = _ppid_of(proc_root, cur), hops + 1
    return found


async def clients_attached(slot: int) -> bool | None:
    """Whether a tmux client is attached to the builder's session (None: unknown)."""
    from genesis.board import launcher

    rc, out = await launcher._exec(
        ["tmux", "list-clients", "-t", f"=cc-{slot}", "-F", "#{client_name}"],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    if rc != 0:
        return None
    return bool(out.strip())


async def kill_session(slot: int) -> None:
    from genesis.board import launcher

    await launcher._exec(
        ["tmux", "kill-session", "-t", f"=cc-{slot}"], timeout=launcher.LOCAL_TIMEOUT_S
    )


def send_term(pid: int) -> None:
    if pid <= 1:  # never signal init, or every process (kill(-1) semantics)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)


def send_kill(pid: int) -> None:
    if pid <= 1:  # never signal init, or every process (kill(-1) semantics)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def exit_status(slot: int, since: datetime | None) -> str | None:
    """The status of the newest exit the slot's log recorded after *since*."""
    from genesis.board.launcher import _parse

    try:
        lines = (_logs_dir() / f"cc_exit_{slot}.log").read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        m = _EXIT_LINE.match(line.strip())
        if m and int(m["slot"]) == slot:
            at = _parse(m["at"])
            if since is None or (at is not None and at >= since):
                return m["status"]
            return None
    return None


# ─── the pass ───────────────────────────────────────────────────────────────


async def _stop_one(slot: int) -> bool:
    """SIGTERM the builder's claude, wait for it, close the session. True when
    the session is gone."""
    from genesis.board import launcher

    pids = claude_pids_under(await pane_pids(slot))
    for pid in pids:
        send_term(pid)
    for _ in range(STOP_WAIT_S):
        if not any(alive(p) for p in pids):
            break
        await asyncio.sleep(1)
    await kill_session(slot)
    # A claude that outlived SIGTERM and the session is killed: two processes
    # on one conversation (a resume next to a survivor) is the failure to avoid.
    survivors = [p for p in pids if alive(p)]
    for pid in survivors:
        send_kill(pid)
    for _ in range(KILL_WAIT_S):
        if not any(alive(p) for p in survivors):
            break
        await asyncio.sleep(1)
    if any(alive(p) for p in survivors):
        logger.error("builder watch: claude %s outlived SIGKILL in cc-%s", survivors, slot)
        return False
    sessions = await launcher.tmux_sessions()
    return f"cc-{slot}" not in sessions


async def _stop(slots: list[int]) -> bool:
    """Stop every session carrying one builder's key; True only when all are gone."""
    results = [await _stop_one(slot) for slot in slots]
    return bool(results) and all(results)


def session_alive(session_id: str | None, proc_root: Path = Path("/proc")) -> bool:
    """Whether any process's command line names *session_id* (a claude still
    running that conversation, whatever its tmux session did). Unreadable
    entries are skipped; an unreadable /proc reads as alive (fail closed)."""
    if not session_id:
        return False
    needle = session_id.encode()
    try:
        entries = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return True
    for entry in entries:
        try:
            if needle in (entry / "cmdline").read_bytes():
                return True
        except OSError:
            continue
    return False


def _card_of(items: list[dict], tracker: str, number: int) -> dict | None:
    for it in items:
        content = it.get("content") or {}
        repo = ((content.get("repository") or {}).get("nameWithOwner") or "").lower()
        if (
            content.get("__typename") == "Issue"
            and repo == tracker
            and content.get("number") == number
            and not it.get("isArchived")
        ):
            return it
    return None


def _open_pr_opened(card: dict | None, tracker: str):
    """When the card's open closing PR (from this repo) was created, or None."""
    from genesis.board.launcher import _parse

    nodes = (((card or {}).get("content") or {}).get("closedByPullRequestsReferences") or {}).get(
        "nodes"
    ) or []
    times = [
        _parse(n.get("createdAt"))
        for n in nodes
        if n
        and n.get("state") == "OPEN"
        and n.get("isCrossRepository") is False
        and ((n.get("repository") or {}).get("nameWithOwner") or "").lower() == tracker
    ]
    times = [t for t in times if t is not None]
    return min(times) if times else None


_UNKNOWN = object()


async def _pr_opened_live(tick, number: int):
    """For a card no longer on the board: whether the issue has an open closing
    PR, read live (a time when it does, None when it does not, _UNKNOWN when the
    read failed)."""
    from genesis.board import projects_v2 as pv

    owner, _, name = tick.tracker.partition("/")
    try:
        facts = await pv.launch_facts(owner, name, number)
    except pv.ProjectsError:
        logger.warning("builder watch: issue read failed for #%s", number, exc_info=True)
        return _UNKNOWN
    return tick.now if facts["open_prs"] else None


async def run_watch(tick, items: list[dict] | None) -> dict:
    """One watch pass inside a launcher tick. Never raises."""
    try:
        return await _watch(tick, items)
    except Exception as exc:
        logger.error("builder watch failed", exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}


async def _watch(tick, items: list[dict] | None) -> dict:
    from genesis.board import launcher
    from genesis.db.crud import board as board_crud

    out = {"watched": 0, "exited": 0, "paused": 0, "closed": 0, "withdrawn": 0}
    active = await board_crud.active_builders(tick.db, repo=tick.tracker)
    out["watched"] = len(active)
    if active:
        sessions = await launcher.tmux_sessions()
        for row in active:
            try:
                await _one(tick, row, sessions, items, out)
            except Exception:
                # One bad row must not stop the others being watched.
                logger.error("builder watch: row %s failed", row.get("id"), exc_info=True)
                out["row_errors"] = out.get("row_errors", 0) + 1
    if items is not None:
        await _withdrawn(tick, items, out)
    return out


async def _one(tick, row: dict, sessions: dict, items, out: dict) -> None:
    from genesis.board.launcher import _SLOT_RE, _parse

    detail = row.get("detail") or {}
    key, number = detail.get("key"), row["issue_number"]
    if not key:
        return  # a key-less row would match the owner's own (key-less) sessions
    # Every session carrying this builder's key now: normally the recorded slot,
    # but a slow first attempt can come up in another. A stop never touches a
    # session that does not carry the key.
    carrying = sorted(
        int(m.group(1)) for name, k in sessions.items() if k == key and (m := _SLOT_RE.match(name))
    )
    slot = detail.get("slot")
    if slot not in carrying and carrying:
        slot = carrying[0]
    if not isinstance(slot, int):
        return
    session_id = await _session_for(tick, key, number, slot, detail.get("session_id"))
    card = _card_of(items, tick.tracker, number) if items is not None else None
    pr_opened = _open_pr_opened(card, tick.tracker)
    finished = _finished(card)
    base = {
        "key": key,
        "issue": number,
        "item": row.get("project_item_id"),
        "slot": slot,
        "session_id": session_id,
    }

    if not carrying:
        if items is None:
            return  # whether it finished needs a good board read
        if session_alive(session_id):
            return  # its claude outlived the session: not an exit, not resumable yet
        if card is None:
            pr_opened = await _pr_opened_live(tick, number)
            if pr_opened is _UNKNOWN:
                return
            finished = pr_opened is not None
        if pr_opened is not None or finished:
            out["closed"] += 1
            await _row(tick, "builder_closed", "c", base, reason="ended after its work finished")
            return
        status = exit_status(slot, _parse(row.get("created_at")))
        out["exited"] += 1
        new = await _row(
            tick,
            "builder_exited",
            "x",
            base,
            status=status,
            reason=f"the session ended (status {status or 'unknown'}) with no PR",
        )
        if new is not None:
            await tick.notify(
                f"Build #{number} (cc-{slot}) ended with status {status or 'unknown'} and no "
                "PR. It is not restarted: move its card out of Build and back to resume it.",
                f"exited:{key}",
            )
        return
    if items is None or card is None:
        return

    if pr_opened is not None or finished:
        if pr_opened is not None and not finished and tick.now - pr_opened < CLOSE_AFTER:
            return
        attached = [await clients_attached(s) for s in carrying]
        if any(a is not False for a in attached):
            return  # someone is in a pane, or the read failed: check again next tick
        if await _stop(carrying):
            out["closed"] += 1
            why = "its issue closed" if finished else "an hour after its PR opened"
            new = await _row(tick, "builder_closed", "c", base, reason=why)
            if new is not None:
                await tick.notify(f"Build #{number} (cc-{slot}) is closed: {why}.", f"closed:{key}")
        return

    column = (card.get("status") or {}).get("name")
    if column in PAUSE_COLUMNS and await _stop(carrying):
        out["paused"] += 1
        new = await _row(
            tick,
            "builder_paused",
            "p",
            base,
            column=column,
            reason=f"its card moved to {column}",
        )
        if new is not None:
            await tick.notify(
                f"Build #{number} (cc-{slot}) is paused: its card moved to {column}. Move "
                "it back to Build to resume the same conversation.",
                f"paused:{key}",
            )


def _finished(card: dict | None) -> bool:
    """The card says the work is done: its issue is closed (a merged ``Closes``
    PR closes it), or it sits in Done. GitHub lists only OPEN closing PRs on the
    board read, so a merged one is seen this way, never as an open PR."""
    if card is None:
        return False
    content = card.get("content") or {}
    return content.get("state") == "CLOSED" or (card.get("status") or {}).get("name") == "Done"


async def _session_for(tick, key: str, number: int, slot: int, recorded: str | None) -> str | None:
    """The session id of the attempt that came up in *slot* (the recorded one
    unless another attempt of this move used that slot)."""
    from genesis.board.launcher import _history, _of_key

    history = await _history(tick.db, tick.tracker, number) or []
    for e in _of_key(history, key, "builder_launching"):
        d = e.get("detail") or {}
        if d.get("slot") == slot and d.get("session_id"):
            return d["session_id"]
    return recorded


async def _row(tick, event: str, suffix: str, base: dict, *, reason: str, **detail):
    return await tick.event(
        event,
        key=base["key"],
        change_key=f"{base['key']}#{suffix}",
        issue=base["issue"],
        item=base["item"],
        reason=reason,
        slot=base["slot"],
        session_id=base["session_id"],
        **detail,
    )


async def _withdrawn(tick, items: list[dict], out: dict) -> None:
    """A held move whose card is no longer in Build at that move: withdrawn, once."""
    from genesis.board.launcher import _build_cards
    from genesis.db.crud import board as board_crud

    current = {c["key"] for c in _build_cards(items)}
    done = set()
    for e in await board_crud.open_holds(tick.db, repo=tick.tracker):
        key = (e.get("detail") or {}).get("key")
        if key in current or key in done:
            continue
        done.add(key)
        out["withdrawn"] += 1
        await tick.event(
            "launch_withdrawn",
            key=key,
            change_key=key,
            issue=e["issue_number"],
            item=e.get("project_item_id"),
            reason="its card left Build while held",
        )
