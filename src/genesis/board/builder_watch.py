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

Pause, close and exit detection all need a good board read (whether a builder
finished is read from it). A move that
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


async def session_ids() -> dict[str, str]:
    """Each tmux session's name with its identity, ``<server pid>:<$id>``. tmux
    never reuses a session id within one server, but a new server starts again
    at ``$0`` (MEASURED, tmux 3.4), so the server pid is part of it. No server
    reads as none; any other failure raises."""
    from genesis.board import launcher
    from genesis.board.launcher.probes import _no_server

    rc, out = await launcher._exec(
        ["tmux", "list-sessions", "-F", "#{session_name}\t#{pid}\t#{session_id}"],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    if rc != 0:
        if _no_server(out):
            return {}
        raise RuntimeError(f"tmux list-sessions failed (rc={rc}): {out.strip()[:200]}")
    ids = {}
    for line in out.splitlines():
        name, _, rest = line.strip().partition("\t")
        server, _, sid = rest.partition("\t")
        if name and server.isdigit() and sid.startswith("$"):
            ids[name] = f"{server}:{sid}"
    return ids


def _target(ident: str) -> str:
    return ident.partition(":")[2]


async def key_of(ident: str) -> str | None:
    """The builder key on the session *ident*, or None when that session is gone
    (its server too), carries no key, or cannot be read."""
    from genesis.board import launcher
    from genesis.board.launcher.probes import _KEY_VAR

    if ident not in (await session_ids()).values():
        return None
    rc, env = await launcher._exec(
        ["tmux", "show-environment", "-t", _target(ident), _KEY_VAR],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    line = env.strip()
    return line.split("=", 1)[1] if rc == 0 and line.startswith(f"{_KEY_VAR}=") else None


async def pane_pids(ident: str) -> list[int] | None:
    """The session's pane pids, or None when the read failed."""
    from genesis.board import launcher

    rc, out = await launcher._exec(
        ["tmux", "list-panes", "-s", "-t", _target(ident), "-F", "#{pane_pid}"],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    return [int(x) for x in out.split() if x.isdigit()] if rc == 0 else None


def claude_pids_under(panes: list[int], proc_root: Path = Path("/proc")) -> list[int] | None:
    """Every claude process descending from one of *panes*, nearest the pane
    first (the builder's own claude before anything it started); None when
    /proc could not be read."""
    from genesis.cc.slot_liveness import _ppid_of, claude_pids

    pids = claude_pids(proc_root)
    if pids is None:
        return None
    found = []
    for pid, _headless in pids:
        cur, hops = pid, 0
        while cur and cur > 1 and hops < 64:
            if cur in panes:
                found.append((hops, pid))
                break
            cur, hops = _ppid_of(proc_root, cur), hops + 1
    return [pid for _, pid in sorted(found)]


async def clients_attached(ident: str) -> bool | None:
    """Whether a tmux client is attached to the session (None: unknown)."""
    from genesis.board import launcher

    rc, out = await launcher._exec(
        ["tmux", "list-clients", "-t", _target(ident), "-F", "#{client_name}"],
        timeout=launcher.LOCAL_TIMEOUT_S,
    )
    if rc != 0:
        return None
    return bool(out.strip())


async def kill_session(ident: str) -> None:
    from genesis.board import launcher

    await launcher._exec(
        ["tmux", "kill-session", "-t", _target(ident)], timeout=launcher.LOCAL_TIMEOUT_S
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
    """The status of the one exit the slot's log recorded after *since*. The log
    names the slot, not the session, so with two exits since then (the slot was
    reused) the builder's cannot be told apart: None, reported as unknown."""
    from genesis.board.launcher import _parse

    try:
        lines = (_logs_dir() / f"cc_exit_{slot}.log").read_text(errors="replace").splitlines()
    except OSError:
        return None
    found = []
    for line in reversed(lines):
        m = _EXIT_LINE.match(line.strip())
        if m and int(m["slot"]) == slot:
            at = _parse(m["at"])
            if since is not None and (at is None or at < since):
                break
            found.append(m["status"])
    return found[0] if len(found) == 1 else None


# ─── the pass ───────────────────────────────────────────────────────────────


async def _stop_one(ident: str, key: str, *, detached_only: bool) -> tuple[bool, str | None]:
    """Stop the session *ident* (never another that took its slot name): SIGTERM
    its claude, SIGKILL a survivor, then close the session. Returns ``(gone,
    conversation)``: *gone* is True once that session no longer exists, and
    *conversation* is the session id its claude was running (None if unread).

    The session must still carry *key* when the stop starts and again before it
    is closed; with *detached_only*, it must also have no client attached. A
    claude that outlives SIGKILL leaves the session open, so the next pass
    retries the whole stop rather than losing sight of a live builder."""
    if ident not in (await session_ids()).values():
        # Gone before any stop: it ended by itself. Alone, that is an exit for
        # the next pass to read (with its check that no claude outlived it).
        return _GONE_BEFORE, None
    if await key_of(ident) != key:
        return False, None  # it cannot be confirmed as this builder: left alone
    if detached_only and await clients_attached(ident) is not False:
        return False, None  # someone is in the pane, or the read failed
    panes = await pane_pids(ident)
    pids = claude_pids_under(panes) if panes is not None else None
    if pids is None:
        return False, None  # what runs in it is unknown: never killed blind
    # The builder's own claude is the one nearest the pane.
    conversation = next((min(c) for p in pids if (c := conversations_of(p))), None)
    for pid in pids:
        send_term(pid)
    for _ in range(STOP_WAIT_S):
        if not any(alive(p) for p in pids):
            break
        await asyncio.sleep(1)
    survivors = [p for p in pids if alive(p)]
    for pid in survivors:
        send_kill(pid)
    for _ in range(KILL_WAIT_S):
        if not any(alive(p) for p in survivors):
            break
        await asyncio.sleep(1)
    if any(alive(p) for p in survivors):
        logger.error("builder watch: claude %s outlived SIGKILL in %s", survivors, ident)
        return False, conversation
    if ident in (await session_ids()).values():
        if await key_of(ident) != key:
            return False, conversation
        await kill_session(ident)
    return ident not in (await session_ids()).values(), conversation


_GONE_BEFORE = "gone-before"


async def _stop(idents: list[str], key: str, *, detached_only: bool = False):
    """Stop every session carrying one builder's key; attachment is checked per
    session, just before its own stop. ``(all gone, {ident: conversation})``.
    A session that ended by itself meanwhile counts as gone once another was
    actually stopped; when none was, the pass ends with no stop."""
    results = {i: await _stop_one(i, key, detached_only=detached_only) for i in idents}
    oks = [ok for ok, _ in results.values()]
    gone = bool(oks) and all(ok for ok in oks) and any(ok is True for ok in oks)
    return gone, {i: c for i, (_, c) in results.items() if c}


_SESSION_FLAGS = (b"--session-id", b"-r", b"--resume")


def _conversations(argv: list[bytes]) -> set[str]:
    """The session ids a claude command line names as its own conversation."""
    found = {v for f, v in zip(argv, argv[1:], strict=False) if f in _SESSION_FLAGS and v}
    for arg in argv:
        for flag in _SESSION_FLAGS:
            if arg.startswith(flag + b"="):
                found.add(arg[len(flag) + 1 :])
    return {v.decode(errors="replace") for v in found}


def conversations_of(pid: int, proc_root: Path = Path("/proc")) -> set[str]:
    try:
        return _conversations((proc_root / str(pid) / "cmdline").read_bytes().split(b"\0"))
    except OSError:
        return set()


def session_alive(session_id: str | None, proc_root: Path = Path("/proc")) -> bool:
    """Whether a claude process is running conversation *session_id* (its own
    ``--session-id``/``-r`` argument, whatever its tmux session did). Another
    program naming the id, such as a ``tail`` of its transcript, does not count;
    an unreadable /proc reads as alive (fail closed)."""
    from genesis.cc.slot_liveness import claude_pids

    if not session_id:
        return False
    pids = claude_pids(proc_root)
    if pids is None:
        return True
    return any(session_id in conversations_of(pid, proc_root) for pid, _ in pids)


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
    """When the card's open closing PR (from this repo) was created; None when it
    has none; ``_UNKNOWN`` when the read listed only part of its closing PRs and
    none of that part qualifies (one of the rest could)."""
    from genesis.board.launcher import _parse

    refs = ((card or {}).get("content") or {}).get("closedByPullRequestsReferences") or {}
    nodes = [n for n in refs.get("nodes") or [] if n]
    times = [
        _parse(n.get("createdAt"))
        for n in nodes
        if n.get("state") == "OPEN"
        and n.get("isCrossRepository") is False
        and ((n.get("repository") or {}).get("nameWithOwner") or "").lower() == tracker
    ]
    times = [t for t in times if t is not None]
    if times:
        return min(times)
    total = refs.get("totalCount")
    return _UNKNOWN if isinstance(total, int) and total > len(nodes) else None


_UNKNOWN = object()


async def _live_state(tick, number: int):
    """For a card no longer on the board, read live: ``(pr_opened, finished)``,
    where *pr_opened* is a time when the issue has an open closing PR (the read
    carries no PR creation time), ``_UNKNOWN`` when the read listed only part of
    them, and *finished* is True when the issue is CLOSED, the launcher's own
    rule; ``_UNKNOWN`` when the read failed."""
    from genesis.board import projects_v2 as pv

    owner, _, name = tick.tracker.partition("/")
    try:
        facts = await pv.launch_facts(owner, name, number)
    except pv.ProjectsError:
        logger.warning("builder watch: issue read failed for #%s", number, exc_info=True)
        return _UNKNOWN
    if facts["open_prs"]:
        pr_opened = tick.now
    else:
        pr_opened = _UNKNOWN if facts.get("open_prs_unresolved") else None
    return pr_opened, facts.get("state") == "CLOSED"


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

    out = {
        "watched": 0,
        "exited": 0,
        "paused": 0,
        "closed": 0,
        "withdrawn": 0,
        "settled": 0,
        "strays": 0,
    }
    active = await board_crud.active_builders(tick.db, repo=tick.tracker)
    if items is not None:
        await _settle_orphans(tick, items, active, out)
        if out["settled"]:
            active = await board_crud.active_builders(tick.db, repo=tick.tracker)
    sessions = await launcher.tmux_sessions() if active else {}
    ids = await session_ids() if active else {}
    out["watched"] = len(active)
    for row in active:
        try:
            await _one(tick, row, sessions, ids, items, out)
        except Exception:
            # One bad row must not stop the others being watched.
            logger.error("builder watch: row %s failed", row.get("id"), exc_info=True)
            out["row_errors"] = out.get("row_errors", 0) + 1
    if items is not None:
        await _withdrawn(tick, items, out)
    if out.get("row_errors"):
        out["error"] = f"{out['row_errors']} builder watch row(s) failed"
    return out


async def _settle_orphans(tick, items: list[dict], active: list[dict], out: dict) -> None:
    """Sessions that carry a builder key the watch is not watching.

    - With no ``builder_launched`` row (the post-launch tmux read failed, or a
      slow attempt came up), it is recorded from its attempt row so the watch can
      pause or close it. The launcher's own settle of the same move then finds
      that row and skips (the row is deduped on the move key, so whichever
      records first wins).
    - When the move already has that row and has ended, the session is a late
      attempt of it that nothing would watch: it is stopped (never with a client
      attached) and the owner told.

    A failed tmux read skips this pass."""
    from genesis.board import launcher
    from genesis.db.crud import board as board_crud

    try:
        sessions = await launcher.tmux_sessions()
        ids = await session_ids()
    except Exception:
        logger.warning("builder watch: tmux read for unrecorded builders failed", exc_info=True)
        return
    watched = {(r.get("detail") or {}).get("key") for r in active}
    for key in sorted({k for k in sessions.values() if k} - watched):
        attempts = await board_crud.attempts_of(tick.db, repo=tick.tracker, key=key)
        if not attempts:
            continue
        a = attempts[0]
        history = await launcher._history(tick.db, tick.tracker, a["issue_number"]) or []
        if not launcher._of_key(history, key, "builder_launched"):
            card = {"key": key, "number": a["issue_number"], "item": a.get("project_item_id")}
            if (
                await launcher._launched(tick, card, a, settled=True, settled_by="watch")
                is not None
            ):
                out["settled"] += 1
                continue
        names = sorted(n for n, k in sessions.items() if k == key and n in ids)
        gone, _ = await _stop([ids[n] for n in names], key, detached_only=True)
        if gone:
            out["strays"] += 1
            await tick.notify(
                f"Build #{a['issue_number']}: a late session of a move that already ended "
                f"({', '.join(names)}) was stopped.",
                f"stray:{key}",
            )


async def _one(tick, row: dict, sessions: dict, ids: dict, items, out: dict) -> None:
    from genesis.board.launcher import _SLOT_RE, _parse

    detail = row.get("detail") or {}
    key, number = detail.get("key"), row["issue_number"]
    if not key:
        return  # a key-less row would match the owner's own (key-less) sessions
    # Every session carrying this builder's key now, by identity: normally the
    # recorded slot, but a slow first attempt can come up in another. A stop
    # addresses that identity, never a slot name another session may take.
    carrying = sorted(
        (int(m.group(1)), ids[name])
        for name, k in sessions.items()
        if k == key and name in ids and (m := _SLOT_RE.match(name))
    )
    idents = [i for _, i in carrying]
    slot = detail.get("slot")
    if carrying and slot not in [s for s, _ in carrying]:
        slot = carrying[0][0]
    if not isinstance(slot, int):
        return
    card = _card_of(items, tick.tracker, number) if items is not None else None
    pr_opened = _open_pr_opened(card, tick.tracker) if card is not None else None
    finished = _finished(card)
    base = {"key": key, "issue": number, "item": row.get("project_item_id"), "slot": slot}

    if not carrying:
        if items is None:
            return  # whether it finished needs a good board read
        session_id, slot = await _ran(tick, key, number, slot, detail.get("session_id"))
        base["slot"] = slot
        if session_alive(session_id):
            return  # its claude outlived the session: not an exit, not resumable yet
        if card is None:
            live = await _live_state(tick, number)
            if live is _UNKNOWN:
                return
            pr_opened, finished = live
        if pr_opened is _UNKNOWN and not finished:
            return  # part of its closing PRs is unread: closed or exited is unknown
        base["session_id"] = session_id
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
    if items is None:
        return
    if card is None:
        # Off the board: closed only once its issue is CLOSED (read live); the
        # live read has no PR creation time for the one-hour rule.
        live = await _live_state(tick, number)
        if live is _UNKNOWN or not live[1]:
            return
        pr_opened, finished = None, True
    if pr_opened is _UNKNOWN and not finished:
        return  # part of its closing PRs is unread: never paused on a guess

    if pr_opened is not None or finished:
        if pr_opened is not None and not finished and tick.now - pr_opened < CLOSE_AFTER:
            return
        # Each session is checked for a client just before its own stop.
        gone, conversations = await _stop(idents, key, detached_only=True)
        conversation = _of_slot(carrying, slot, conversations)
        if gone:
            out["closed"] += 1
            why = "its issue closed" if finished else "an hour after its PR opened"
            base["session_id"] = (
                conversation or (await _ran(tick, key, number, slot, detail.get("session_id")))[0]
            )
            new = await _row(tick, "builder_closed", "c", base, reason=why)
            if new is not None:
                await tick.notify(f"Build #{number} (cc-{slot}) is closed: {why}.", f"closed:{key}")
        return

    column = (card.get("status") or {}).get("name")
    if column not in PAUSE_COLUMNS:
        return
    gone, conversations = await _stop(idents, key)
    if not gone:
        return
    conversation = _of_slot(carrying, slot, conversations)
    # The conversation the stopped claude was running is the one to resume.
    base["session_id"] = (
        conversation or (await _ran(tick, key, number, slot, detail.get("session_id")))[0]
    )
    out["paused"] += 1
    new = await _row(
        tick, "builder_paused", "p", base, column=column, reason=f"its card moved to {column}"
    )
    if new is not None:
        await tick.notify(
            f"Build #{number} (cc-{slot}) is paused: its card moved to {column}. Move "
            "it back to Build to resume the same conversation.",
            f"paused:{key}",
        )


def _of_slot(carrying: list, slot: int, conversations: dict) -> str | None:
    """The conversation of the session in the row's *slot*, else any stopped one's."""
    mine = next((i for s, i in carrying if s == slot), None)
    return conversations.get(mine) or next(iter(conversations.values()), None)


def _finished(card: dict | None) -> bool:
    """The issue is closed (a merged ``Closes`` PR closes it): the launcher's own
    finished rule, so the two never disagree. GitHub lists only OPEN closing PRs
    on the board read, so a merged one is seen this way. A card moved to Done by
    hand on an open issue is not finished."""
    if card is None:
        return False
    return (card.get("content") or {}).get("state") == "CLOSED"


async def _ran(tick, key: str, number: int, slot: int, recorded: str | None):
    """``(conversation, slot)`` of the attempt that ran, when no live claude names
    it: of this move's attempts (and the recorded one), those with a conversation
    file, preferring *slot*. An attempt that reserved a slot and lost it at the
    door never started one. With none, the recorded slot's newest attempt."""
    from genesis.board import launcher

    history = await launcher._history(tick.db, tick.tracker, number) or []
    tried = [
        (d["session_id"], d.get("slot"))
        for e in launcher._of_key(history, key, "builder_launching")
        if (d := e.get("detail") or {}).get("session_id")
    ]
    if recorded and recorded not in [sid for sid, _ in tried]:
        tried.append((recorded, slot))
    root = launcher._repo_root()
    ran = [(sid, s) for sid, s in tried if launcher._transcript(root, sid).is_file()]
    pick = next((t for t in ran if t[1] == slot), ran[0] if ran else None)
    if pick is not None:
        return pick[0], pick[1] if isinstance(pick[1], int) else slot
    return next((sid for sid, s in tried if s == slot), recorded), slot


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
