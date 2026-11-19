"""The builder watch (spec E, E3b): a running builder is paused when its card
goes back to Ready or Proposed, closed an hour after its PR opens unless
someone is attached, noticed when it exits, and resumed when its card returns
to Build."""

# ruff: noqa: F811  (the launcher tests' fixtures are imported by name and used as arguments)
from __future__ import annotations

import os
import signal
from datetime import timedelta

import pytest

from genesis.board import builder_watch, launcher
from genesis.db.crud import board as board_crud

from .test_launcher import (  # noqa: F401  (fixtures are used by name)
    NOW,
    REPO,
    _arm,
    _card,
    _events,
    _key,
    _later,
    _run,
    db,
    world,
)


@pytest.fixture
def stops(world, monkeypatch, tmp_path):
    """The stop path's process side: pane pids, claude pids, signals, kill."""
    state = {"signalled": [], "killed": [], "kill_works": True}
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(builder_watch, "_logs_dir", lambda: logs)

    # Each session's tmux identity follows its name unless a test gives it a
    # new one (a reused slot name is a different session).
    state["ids"] = {}
    state["convo"] = {}

    def ids_now():
        return {n: state["ids"].get(n, f"1:${n}") for n in world.sessions}

    def slot_of(ident):
        name = next((n for n, i in ids_now().items() if i == ident), "")
        return int(name[3:]) if name.startswith("cc-") and name[3:].isdigit() else None

    async def session_ids():
        return ids_now()

    async def key_of(ident):
        return next((world.sessions[n] for n, i in ids_now().items() if i == ident), None)

    async def pane_pids(ident):
        slot = slot_of(ident)
        return [5000 + slot] if slot is not None else []

    monkeypatch.setattr(builder_watch, "session_ids", session_ids)
    monkeypatch.setattr(builder_watch, "key_of", key_of)
    monkeypatch.setattr(builder_watch, "pane_pids", pane_pids)
    monkeypatch.setattr(builder_watch, "claude_pids_under", lambda panes: [p + 1 for p in panes])
    monkeypatch.setattr(
        builder_watch, "conversations_of", lambda pid: state["convo"].get(pid, set())
    )
    monkeypatch.setattr(builder_watch, "send_term", lambda pid: state["signalled"].append(pid))
    monkeypatch.setattr(builder_watch, "alive", lambda pid: False)
    monkeypatch.setattr(builder_watch, "STOP_WAIT_S", 1)
    monkeypatch.setattr(builder_watch, "session_alive", lambda sid: False)

    async def kill_session(ident):
        slot = slot_of(ident)
        state["killed"].append(slot)
        if state["kill_works"] and slot is not None:
            world.sessions.pop(f"cc-{slot}", None)

    monkeypatch.setattr(builder_watch, "kill_session", kill_session)
    state["clients"] = False

    async def clients(ident):
        value = state["clients"]
        return value(slot_of(ident)) if callable(value) else value

    monkeypatch.setattr(builder_watch, "clients_attached", clients)
    state["logs"] = logs
    return state


async def _running(db, world, card, *, slot=9, sid="s-1", at=NOW - timedelta(hours=3)):
    """A builder the launcher started for *card*, alive in cc-<slot>."""
    key = _key(card)
    async with board_crud.owned_connection(db) as own:
        for event, ck in (("builder_launching", f"{key}#a1"), ("builder_launched", key)):
            await board_crud.append_event(
                own,
                event=event,
                now=at.isoformat(),
                repo=REPO,
                issue_number=card["content"]["number"],
                project_item_id=card["id"],
                attempt=1,
                worker="board_launcher",
                observed_change_key=ck,
                detail={"key": key, "slot": slot, "session_id": sid},
            )
    world.sessions[f"cc-{slot}"] = key
    return key


def _pr(created):
    return {
        "number": 90,
        "state": "OPEN",
        "isCrossRepository": False,
        "createdAt": created.isoformat().replace("+00:00", "Z"),
        "repository": {"nameWithOwner": REPO},
    }


def _moved(card, status, closers=()):
    """The same card, now in *status* (a new Status updatedAt)."""
    return _card(
        card["id"],
        number=card["content"]["number"],
        status=status,
        moved=NOW - timedelta(minutes=1),
        closers=closers,
    )


# ── exits ───────────────────────────────────────────────────────────────────


async def test_an_exit_with_no_pr_is_recorded_once_with_its_status(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card)
    (stops["logs"] / "cc_exit_9.log").write_text(
        "=== 2026-11-15T08:00:00Z cc-9 claude exited status=0 ===\n"
        "=== 2026-11-15T11:30:00Z cc-9 claude exited status=137 ===\n  (SIGKILL)\n"
    )
    del world.sessions["cc-9"]
    rt, out = await _run(db, [card])
    [row] = await _events(db, "builder_exited")
    assert row["detail"]["status"] == "137" and row["observed_change_key"] == f"{key}#x"
    assert "status 137" in rt._outreach_pipeline.sent[0][0]
    assert world.launches == []  # never relaunched
    rt2, _ = await _run(db, [card], now=_later(1))
    assert len(await _events(db, "builder_exited")) == 1 and rt2._outreach_pipeline.sent == []


async def test_an_exit_older_than_the_launch_is_not_this_builders(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    (stops["logs"] / "cc_exit_9.log").write_text(
        "=== 2026-11-10T01:00:00Z cc-9 claude exited status=0 ===\n"
    )
    del world.sessions["cc-9"]
    await _run(db, [card])
    [row] = await _events(db, "builder_exited")
    assert row["detail"]["status"] is None


async def test_an_exit_after_the_pr_opened_is_a_close(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    del world.sessions["cc-9"]
    done = _moved(card, "In Review", closers=[_pr(NOW - timedelta(minutes=20))])
    rt, _ = await _run(db, [done])
    assert await _events(db, "builder_exited") == []
    [row] = await _events(db, "builder_closed")
    assert row["reason"] == "ended after its work finished"


async def test_an_exit_waits_for_a_board_read_and_is_noticed_while_disarmed(db, world, stops):
    """Whether a builder finished (an open PR) needs the board read, so an exit
    seen during a failed read waits; disarming does not stop the watch."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    del world.sessions["cc-9"]
    world.armed = (False, "builder_launch.enabled is not true")
    _, out = await _run(db, None)
    assert out["watch"]["exited"] == 0 and await _events(db, "builder_exited") == []
    _, out = await _run(db, [card], now=_later(1))
    assert out["watch"]["exited"] == 1


async def test_an_exit_of_a_builder_whose_card_left_the_board_reads_its_pr_live(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    del world.sessions["cc-9"]
    world.facts = world.facts | {"open_prs": [90]}
    await _run(db, [])
    assert len(await _events(db, "builder_closed")) == 1
    assert await _events(db, "builder_exited") == []


# ── pause ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("column", ["Ready", "Proposed"])
async def test_a_card_pulled_back_pauses_its_builder(db, world, stops, column):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card)
    rt, out = await _run(db, [_moved(card, column)])
    assert stops["signalled"] == [5010] and stops["killed"] == [9]
    [row] = await _events(db, "builder_paused")
    assert row["observed_change_key"] == f"{key}#p" and row["detail"]["column"] == column
    assert "Move it back to Build to resume" in rt._outreach_pipeline.sent[0][0]
    assert await _events(db, "builder_exited") == []  # the stop is not an exit


@pytest.mark.parametrize("column", ["In Progress", "In Review", "Build", None])
async def test_other_columns_leave_the_builder_alone(db, world, stops, column):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    moved = _moved(card, column) if column else _moved(card, "Ready")
    if column is None:
        moved["status"] = None
    await _run(db, [moved])
    assert stops["killed"] == [] and await _events(db, "builder_paused") == []


async def test_a_stop_that_did_not_take_is_retried_and_writes_nothing(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    stops["kill_works"] = False
    await _run(db, [_moved(card, "Ready")])
    assert await _events(db, "builder_paused") == []
    stops["kill_works"] = True
    await _run(db, [_moved(card, "Ready")], now=_later(1))
    assert len(await _events(db, "builder_paused")) == 1


async def test_no_pause_without_a_board_read(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    await _run(db, None)
    assert stops["killed"] == []


# ── the hour after the PR ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("opened", "clients", "closes"),
    [
        (timedelta(hours=2), False, True),
        (timedelta(minutes=50), False, False),
        (timedelta(hours=2), True, False),
        (timedelta(hours=2), None, False),  # the client read failed: wait
    ],
)
async def test_a_builder_closes_an_hour_after_its_pr_unless_attached(
    db, world, stops, opened, clients, closes
):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    stops["clients"] = clients
    await _run(db, [_moved(card, "In Review", closers=[_pr(NOW - opened)])])
    assert bool(await _events(db, "builder_closed")) is closes
    assert (stops["killed"] == [9]) is closes


async def test_a_finished_builder_does_not_hold_the_next_cards_place(db, world, stops):
    await _arm(db)
    first = _card("I1", number=5, moved=NOW - timedelta(hours=3))
    await _running(db, world, first)
    world.facts = world.facts | {"number": 8}
    nxt = _card("I2", number=8, moved=NOW - timedelta(minutes=2))
    done = _moved(first, "In Review", closers=[_pr(NOW - timedelta(hours=2))])
    _, out = await _run(db, [done, nxt])
    assert out["watch"]["closed"] == 1 and out["launched"] == 1


# ── withdrawn and resume ────────────────────────────────────────────────────


async def test_a_held_move_whose_card_left_build_is_withdrawn_once(db, world, stops):
    await _arm(db)
    world.sessions["cc-3"] = "other-key"
    card = _card()
    await _run(db, [card])
    assert len(await _events(db, "launch_queued")) == 1
    away = _moved(card, "Ready")
    await _run(db, [away], now=_later(1))
    await _run(db, [away], now=_later(2))
    [row] = await _events(db, "launch_withdrawn")
    assert row["detail"]["key"] == _key(card)


async def _paused(db, world, stops, *, transcript: bool, worktree: bool, tmp_path, monkeypatch):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card, sid="sess-old")
    await _run(db, [_moved(card, "Ready")])
    assert len(await _events(db, "builder_paused")) == 1
    convo = tmp_path / "convo.jsonl"
    if transcript:
        convo.write_text("{}\n")
    monkeypatch.setattr(launcher, "_transcript", lambda root, sid: convo)
    if worktree:
        (world.root / ".claude" / "worktrees" / "build-7").mkdir(parents=True)
    back = _card(card["id"], moved=NOW + timedelta(minutes=2))
    return await _run(db, [back], now=_later(1))


async def test_a_card_moved_back_to_build_resumes_the_same_conversation(
    db, world, stops, tmp_path, monkeypatch
):
    rt, out = await _paused(
        db, world, stops, transcript=True, worktree=True, tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    assert out["launched"] == 1
    [argv] = world.launches
    assert "--resume" in argv and argv[argv.index("--session-id") + 1] == "sess-old"
    row = (await _events(db, "builder_launched"))[-1]
    assert row["detail"]["resumed"] is True
    assert "resumed its earlier conversation" in rt._outreach_pipeline.sent[-1][0]


@pytest.mark.parametrize(
    ("transcript", "worktree", "why"),
    [(False, True, "conversation could not be found"), (True, False, "worktree build-7 is gone")],
)
async def test_a_resume_that_cannot_happen_starts_fresh_and_says_why(
    db, world, stops, tmp_path, monkeypatch, transcript, worktree, why
):
    rt, out = await _paused(
        db,
        world,
        stops,
        transcript=transcript,
        worktree=worktree,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    [argv] = world.launches
    assert "--resume" not in argv and argv[argv.index("--session-id") + 1] != "sess-old"
    assert why in rt._outreach_pipeline.sent[-1][0]


async def test_a_builder_that_finished_is_never_resumed(db, world, stops, tmp_path, monkeypatch):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card, sid="sess-old")
    await _run(db, [_moved(card, "In Review", closers=[_pr(NOW - timedelta(hours=2))])])
    assert len(await _events(db, "builder_closed")) == 1
    convo = tmp_path / "c.jsonl"
    convo.write_text("{}\n")
    monkeypatch.setattr(launcher, "_transcript", lambda root, sid: convo)
    await _run(db, [_card(card["id"], moved=NOW + timedelta(minutes=2))], now=_later(1))
    [argv] = world.launches
    assert "--resume" not in argv


# ── process safety ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("pid", [0, 1, -1])
def test_init_and_every_process_are_never_signalled(monkeypatch, pid):
    sent = []
    monkeypatch.setattr(os, "kill", lambda p, s: sent.append((p, s)))
    builder_watch.send_term(pid)
    assert sent == [] and builder_watch.alive(pid) is False


def test_a_real_pid_gets_sigterm(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda p, s: sent.append((p, s)))
    builder_watch.send_term(4242)
    assert sent == [(4242, signal.SIGTERM)]


def test_claude_pids_under_walks_parents(tmp_path):
    """A claude two levels below the pane pid is found; one elsewhere is not."""
    proc = tmp_path / "proc"

    def mk(pid, ppid, comm, cmd):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(cmd)

    mk(100, 1, "bash", b"bash\0-c\0x\0")
    mk(101, 100, "bash", b"bash\0")
    mk(102, 101, "claude", b"claude\0--remote-control\0")
    mk(200, 1, "claude", b"claude\0")
    assert builder_watch.claude_pids_under([100], proc_root=proc) == [102]


# ── review round on this change ─────────────────────────────────────────────


async def test_a_claude_that_survives_sigterm_is_killed_before_the_row(
    db, world, stops, monkeypatch
):
    killed = []
    state = {"alive": True}
    monkeypatch.setattr(builder_watch, "alive", lambda pid: state["alive"])

    def send_kill(pid):
        killed.append(pid)
        state["alive"] = False

    monkeypatch.setattr(builder_watch, "send_kill", send_kill)
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    await _run(db, [_moved(card, "Ready")])
    assert killed == [5010] and len(await _events(db, "builder_paused")) == 1


async def test_a_claude_that_survives_sigkill_writes_no_pause(db, world, stops, monkeypatch):
    monkeypatch.setattr(builder_watch, "alive", lambda pid: True)
    monkeypatch.setattr(builder_watch, "send_kill", lambda pid: None)
    monkeypatch.setattr(builder_watch, "KILL_WAIT_S", 1)
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    await _run(db, [_moved(card, "Ready")])
    assert await _events(db, "builder_paused") == []


async def test_a_stop_targets_the_slot_that_carries_the_key(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, slot=10)
    del world.sessions["cc-10"]
    world.sessions["cc-9"] = key  # the slow first attempt came up here
    world.sessions["cc-10"] = None  # an owner session took the recorded slot
    await _run(db, [_moved(card, "Ready")])
    assert stops["killed"] == [9] and "cc-10" in world.sessions


async def test_live_builders_and_open_holds_are_one_query_each(db):
    from genesis.db.crud import board as board_crud

    async with board_crud.owned_connection(db) as own:
        for n in range(600):
            key = f"old{n}"
            for event, ck in (("builder_launched", key), ("builder_closed", f"{key}#c")):
                await board_crud.append_event(
                    own,
                    event=event,
                    now=NOW.isoformat(),
                    repo=REPO,
                    issue_number=1 + n,
                    observed_change_key=ck,
                    detail={"key": key},
                )
        await board_crud.append_event(
            own,
            event="builder_launched",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=900,
            observed_change_key="live",
            detail={"key": "live", "slot": 9},
        )
        await board_crud.append_event(
            own,
            event="launch_queued",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=901,
            observed_change_key="h#q:capacity",
            detail={"key": "h"},
        )
    active = await board_crud.active_builders(db, repo=REPO)
    assert [a["detail"]["key"] for a in active] == ["live"]
    holds = await board_crud.open_holds(db, repo=REPO)
    assert [h["detail"]["key"] for h in holds] == ["h"]


# ── adversarial audit of the watch ──────────────────────────────────────────


async def test_a_builder_whose_issue_closed_is_closed_and_frees_its_place(db, world, stops):
    """A merged PR closes the issue and drops out of the board's open closers."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    moved = _moved(card, "In Review")
    moved["content"]["state"] = "CLOSED"
    rt, out = await _run(db, [moved])
    assert stops["killed"] == [9]
    [row] = await _events(db, "builder_closed")
    assert row["reason"] == "its issue closed"
    assert "is closed" in rt._outreach_pipeline.sent[-1][0]


async def test_every_session_carrying_the_key_is_stopped(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, slot=9)
    world.sessions["cc-10"] = key
    await _run(db, [_moved(card, "Ready")])
    assert sorted(stops["killed"]) == [9, 10]
    assert len(await _events(db, "builder_paused")) == 1


async def test_a_claude_still_running_its_conversation_is_not_an_exit(
    db, world, stops, monkeypatch
):
    monkeypatch.setattr(builder_watch, "session_alive", lambda sid: True)
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    del world.sessions["cc-9"]
    await _run(db, [card])
    assert await _events(db, "builder_exited") == []


async def test_one_bad_row_does_not_stop_the_others(db, world, stops, monkeypatch):
    await _arm(db)
    a = _card("I1", number=5, moved=NOW - timedelta(hours=3))
    b = _card("I2", number=6, moved=NOW - timedelta(hours=3))
    await _running(db, world, a, slot=9, sid="sa")
    await _running(db, world, b, slot=10, sid="sb")
    real = builder_watch._open_pr_opened

    def flaky(card, tracker):
        if card["content"]["number"] == 5:
            raise RuntimeError("bad row")
        return real(card, tracker)

    monkeypatch.setattr(builder_watch, "_open_pr_opened", flaky)
    _, out = await _run(db, [_moved(a, "Ready"), _moved(b, "Ready")])
    assert out["watch"]["row_errors"] == 1 and out["watch"]["paused"] == 1
    # A failed row is a failed pass for the job, not a quiet count.
    assert "1 builder watch row(s) failed" in out["error"]


async def test_a_failed_resume_starts_fresh_next_time(db, world, stops, tmp_path, monkeypatch):
    rt, out = await _paused(
        db, world, stops, transcript=True, worktree=True, tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    world.launches.clear()
    # The resumed builder never came up: two strikes so far on resumed attempts.
    world.launch_rc = 1
    back = _card("I1", moved=NOW + timedelta(minutes=9))
    del world.sessions["cc-9"]
    await _run(db, [back], now=_later(3))
    await _run(db, [back], now=_later(4))
    assert "--resume" in world.launches[0] and "--resume" not in world.launches[1]


def test_session_alive_matches_a_claude_holding_the_conversation(tmp_path):
    proc = tmp_path / "proc"
    for pid, comm, cmd in (
        (42, "claude", b"claude\0-r\0sess-x\0"),
        (43, "claude", b"claude\0--session-id\0sess-z\0"),
        (44, "tail", b"tail\0-f\0/x/sess-y.jsonl\0"),
    ):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(comm + "\n")
        (proc / str(pid) / "cmdline").write_bytes(cmd)
    assert builder_watch.session_alive("sess-x", proc) is True
    assert builder_watch.session_alive("sess-z", proc) is True
    # A program that only names the id (a tail of its transcript) holds nothing.
    assert builder_watch.session_alive("sess-y", proc) is False
    assert builder_watch.session_alive(None, proc) is False


async def test_a_card_moved_to_done_by_hand_on_an_open_issue_is_not_finished(db, world, stops):
    """The watch and the launcher share one rule: the issue CLOSED, not the column."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    _, out = await _run(db, [_moved(card, "Done")])
    assert stops["killed"] == [] and await _events(db, "builder_closed") == []


async def test_an_off_board_builder_is_closed_once_its_issue_closed(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    world.facts = {**world.facts, "state": "CLOSED"}
    _, out = await _run(db, [])  # the card left the board
    assert stops["killed"] == [9]
    [row] = await _events(db, "builder_closed")
    assert row["reason"] == "its issue closed"


async def test_an_off_board_builder_with_an_open_issue_keeps_running(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    _, out = await _run(db, [])
    assert stops["killed"] == [] and await _events(db, "builder_closed") == []


async def test_an_exit_after_the_issue_closed_off_board_is_closed_not_exited(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    world.sessions.clear()
    world.facts = {**world.facts, "state": "CLOSED"}
    _, out = await _run(db, [])
    assert await _events(db, "builder_exited") == []
    assert len(await _events(db, "builder_closed")) == 1


async def test_a_builder_with_no_launched_row_is_recorded_and_watched(db, world, stops):
    """The post-launch tmux read failed, then the card went back to Ready: the
    builder still comes up and must be paused, not left running unseen."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = _key(card)
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=3)).isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{key}#a1",
            detail={"key": key, "slot": 9, "session_id": "s-1"},
        )
    world.sessions["cc-9"] = key
    _, out = await _run(db, [_moved(card, "Ready")])
    assert len(await _events(db, "builder_launched")) == 1
    assert stops["killed"] == [9] and len(await _events(db, "builder_paused")) == 1


async def test_attempts_of_reads_one_moves_attempts_only(db):
    async with board_crud.owned_connection(db) as own:
        for ck in ("k1#a1", "k1#a2", "k10#a1", "k1"):
            await board_crud.append_event(
                own,
                event="builder_launching",
                now=NOW.isoformat(),
                repo=REPO,
                issue_number=1,
                worker="board_launcher",
                observed_change_key=ck,
                detail={"key": ck.split("#")[0]},
            )
    got = await board_crud.attempts_of(db, repo=REPO, key="k1")
    assert [r["observed_change_key"] for r in got] == ["k1#a2", "k1#a1"]


async def test_resume_uses_the_session_id_the_watch_recorded_when_it_stopped(
    db, world, stops, tmp_path, monkeypatch
):
    """The launched row can name another attempt's session; the end row names the
    session that actually ran, so resume reopens that conversation."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, sid="sess-launched")
    world.sessions.clear()
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_paused",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            worker="board_launcher",
            observed_change_key=f"{key}#p",
            detail={"key": key, "slot": 9, "session_id": "sess-real"},
        )
    (tmp_path / "sess-real.jsonl").write_text("{}\n")
    monkeypatch.setattr(launcher, "_transcript", lambda root, sid: tmp_path / f"{sid}.jsonl")
    (world.root / ".claude" / "worktrees" / "build-7").mkdir(parents=True)
    back = _card(card["id"], moved=NOW + timedelta(minutes=2))
    _, out = await _run(db, [back], now=_later(1))
    [argv] = world.launches
    assert "--resume" in argv and argv[argv.index("--session-id") + 1] == "sess-real"


# ── identity: the verified session, never the slot name ────────────────────


async def test_a_replacement_session_in_the_slot_is_never_killed(db, world, stops, monkeypatch):
    """SIGTERM ends the builder, and the owner opens a new cc-9 before the stop
    goes on: the new session is a different tmux session and is left alone."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)

    def term(pid):
        stops["signalled"].append(pid)
        world.sessions["cc-9"] = None  # the owner's own session, same name
        stops["ids"]["cc-9"] = "1:$99"

    monkeypatch.setattr(builder_watch, "send_term", term)
    await _run(db, [_moved(card, "Ready")])
    assert stops["killed"] == [] and world.sessions["cc-9"] is None
    assert len(await _events(db, "builder_paused")) == 1


async def test_a_session_whose_key_changed_is_not_stopped(db, world, stops, monkeypatch):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)

    async def other(ident):
        return "someone-else"

    monkeypatch.setattr(builder_watch, "key_of", other)
    await _run(db, [_moved(card, "Ready")])
    assert stops["signalled"] == [] and stops["killed"] == []
    assert await _events(db, "builder_paused") == []


async def test_a_claude_that_survives_sigkill_keeps_its_session_for_a_retry(
    db, world, stops, monkeypatch
):
    state = {"alive": True}
    monkeypatch.setattr(builder_watch, "alive", lambda pid: state["alive"])
    monkeypatch.setattr(builder_watch, "send_kill", lambda pid: None)
    monkeypatch.setattr(builder_watch, "KILL_WAIT_S", 1)
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    pulled = _moved(card, "Ready")
    await _run(db, [pulled])
    # The session stays, so the next pass still sees the builder and retries.
    assert stops["killed"] == [] and "cc-9" in world.sessions
    state["alive"] = False
    await _run(db, [pulled], now=_later(1))
    assert stops["signalled"] == [5010, 5010] and stops["killed"] == [9]
    assert len(await _events(db, "builder_paused")) == 1


async def test_attachment_is_checked_just_before_each_sessions_stop(db, world, stops, monkeypatch):
    """Two sessions carry the key; the owner attaches to the second while the
    first is stopping. The second is left running and nothing is closed."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card)
    world.sessions["cc-10"] = key
    attached = set()
    stops["clients"] = lambda slot: slot in attached

    def term(pid):
        stops["signalled"].append(pid)
        attached.add(10)

    monkeypatch.setattr(builder_watch, "send_term", term)
    closed = _moved(card, "In Review", closers=[_pr(NOW - timedelta(hours=2))])
    await _run(db, [closed])
    assert stops["killed"] == [9] and "cc-10" in world.sessions
    assert await _events(db, "builder_closed") == []


async def test_a_pause_records_the_conversation_its_claude_was_running(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card, sid="s-reserved")
    stops["convo"][5010] = {"s-ran"}
    await _run(db, [_moved(card, "Ready")])
    (paused,) = await _events(db, "builder_paused")
    assert paused["detail"]["session_id"] == "s-ran"


async def test_an_exit_records_the_attempt_that_started_a_conversation(
    db, world, stops, tmp_path, monkeypatch
):
    """Attempt 2 reserved cc-9 after attempt 1 came up there, then lost the slot
    at the door: only attempt 1 has a conversation file."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, sid="s-a1")
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=2)).isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            attempt=2,
            worker="board_launcher",
            observed_change_key=f"{key}#a2",
            detail={"key": key, "slot": 9, "session_id": "s-a2"},
        )
    (tmp_path / "s-a1.jsonl").write_text("{}\n")
    monkeypatch.setattr(launcher, "_transcript", lambda root, sid: tmp_path / f"{sid}.jsonl")
    del world.sessions["cc-9"]
    await _run(db, [card])
    (exited,) = await _events(db, "builder_exited")
    assert exited["detail"]["session_id"] == "s-a1"


async def test_two_exits_in_the_slot_since_the_launch_read_as_unknown(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    (stops["logs"] / "cc_exit_9.log").write_text(
        f"=== {(NOW - timedelta(hours=1)).isoformat()} cc-9 claude exited status=137 ===\n"
        f"=== {(NOW - timedelta(minutes=5)).isoformat()} cc-9 claude exited status=0 ===\n"
    )
    del world.sessions["cc-9"]
    await _run(db, [card])
    (exited,) = await _events(db, "builder_exited")
    assert exited["detail"]["status"] is None


def _fork_pr(n):
    return {**_pr(NOW - timedelta(hours=2)), "number": n, "isCrossRepository": True}


async def test_a_partial_closing_pr_list_never_pauses(db, world, stops):
    """Ten fork PRs were listed and an eleventh, this repo's, was not."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    pulled = _moved(card, "Ready", closers=[_fork_pr(n) for n in range(10)])
    pulled["content"]["closedByPullRequestsReferences"]["totalCount"] = 11
    await _run(db, [pulled])
    assert stops["killed"] == [] and await _events(db, "builder_paused") == []


async def test_a_late_session_of_an_earlier_move_is_settled_by_the_watch(db, world, stops):
    """The card is back in Build under a new move: the launcher settles only the
    new move, so the watch records the earlier one's unrecorded session."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = _key(card)
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=3)).isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{key}#a1",
            detail={"key": key, "slot": 9, "session_id": "s-1"},
        )
    world.sessions["cc-9"] = key
    again = _moved(card, "Build")
    assert _key(again) != key
    _, out = await _run(db, [again])
    launched = await _events(db, "builder_launched")
    assert key in [e["detail"]["key"] for e in launched] and out["watch"]["settled"] == 1


async def test_an_unrecorded_builder_in_build_is_settled_while_disarmed(db, world, stops):
    card = _card(moved=NOW - timedelta(hours=3))
    key = _key(card)
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=3)).isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{key}#a1",
            detail={"key": key, "slot": 9, "session_id": "s-1"},
        )
    world.sessions["cc-9"] = key
    world.armed = (False, "test: disarmed")
    _, out = await _run(db, [card])
    assert out["watch"]["settled"] == 1


async def test_a_late_session_of_an_ended_move_is_stopped(db, world, stops):
    """Attempt 1 was paused; a slow attempt 2 of the same move came up later.
    Nothing else would watch it, so it is stopped and the owner told."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card)
    pulled = _moved(card, "Ready")
    await _run(db, [pulled])
    assert len(await _events(db, "builder_paused")) == 1
    world.sessions["cc-11"] = key
    rt, out = await _run(db, [pulled], now=_later(1))
    assert out["watch"]["strays"] == 1 and "cc-11" not in world.sessions
    assert any("late session" in text for text, _ in rt._outreach_pipeline.sent)


async def test_a_failed_resume_gets_one_more_try_on_a_later_move(
    db, world, stops, tmp_path, monkeypatch
):
    await _paused(
        db, world, stops, transcript=True, worktree=True, tmp_path=tmp_path, monkeypatch=monkeypatch
    )
    world.launches.clear()
    world.launch_rc = 1
    del world.sessions["cc-9"]
    # Move B's resume strikes; the owner moves the card out and back (move C)
    # before B retries: C tries the resume once more, and it strikes too; the
    # move after that (D) starts fresh rather than repeating it.
    await _run(db, [_card("I1", moved=NOW + timedelta(minutes=9))], now=_later(3))
    await _run(db, [_card("I1", moved=NOW + timedelta(minutes=30))], now=_later(4))
    await _run(db, [_card("I1", moved=NOW + timedelta(minutes=50))], now=_later(5))
    resumed = ["--resume" in argv for argv in world.launches]
    assert resumed == [True, True, False]


async def test_a_late_session_of_an_exited_move_still_in_build_is_stopped(db, world, stops):
    """The move is fresh and still in Build, so the launcher is left that key;
    it already has its builder_launched row, so the launcher skips it and the
    watch must stop the late session itself."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(minutes=30))
    key = await _running(db, world, card)
    del world.sessions["cc-9"]
    await _run(db, [card])
    assert len(await _events(db, "builder_exited")) == 1
    world.sessions["cc-11"] = key
    _, out = await _run(db, [card], now=_later(1))
    assert out["watch"]["strays"] == 1 and "cc-11" not in world.sessions


async def test_an_exit_in_another_slot_records_that_slot_and_its_status(
    db, world, stops, tmp_path, monkeypatch
):
    """The record names cc-10 (attempt 2, which never started a conversation);
    attempt 1 ran in cc-9 and crashed there."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, slot=10, sid="s-a2")
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=4)).isoformat(),
            repo=REPO,
            issue_number=card["content"]["number"],
            project_item_id=card["id"],
            attempt=9,
            worker="board_launcher",
            observed_change_key=f"{key}#a9",
            detail={"key": key, "slot": 9, "session_id": "s-a1"},
        )
    (tmp_path / "s-a1.jsonl").write_text("{}\n")
    monkeypatch.setattr(launcher, "_transcript", lambda root, sid: tmp_path / f"{sid}.jsonl")
    at = (NOW - timedelta(minutes=5)).isoformat()
    (stops["logs"] / "cc_exit_9.log").write_text(f"=== {at} cc-9 claude exited status=137 ===\n")
    (stops["logs"] / "cc_exit_10.log").write_text(f"=== {at} cc-10 claude exited status=0 ===\n")
    del world.sessions["cc-10"]
    await _run(db, [card])
    (exited,) = await _events(db, "builder_exited")
    d = exited["detail"]
    assert (d["session_id"], d["slot"], d["status"]) == ("s-a1", 9, "137")


async def test_a_failed_pane_read_never_kills_the_session(db, world, stops, monkeypatch):
    async def failed(ident):
        return None

    monkeypatch.setattr(builder_watch, "pane_pids", failed)
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    await _run(db, [_moved(card, "Ready")])
    assert stops["killed"] == [] and await _events(db, "builder_paused") == []


def test_claude_pids_under_lists_the_nearest_claude_first(tmp_path):
    proc = tmp_path / "proc"

    def mk(pid, ppid, comm, cmd):
        d = proc / str(pid)
        d.mkdir(parents=True)
        (d / "stat").write_text(f"{pid} ({comm}) S {ppid} 0 0\n")
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(cmd)

    mk(100, 1, "bash", b"bash\0")
    mk(103, 102, "claude", b"claude\0--resume\0s-child\0")
    mk(102, 100, "claude", b"claude\0-r\0s-builder\0")
    assert builder_watch.claude_pids_under([100], proc) == [102, 103]
    assert builder_watch.conversations_of(102, proc) == {"s-builder"}


def _exec_replies(monkeypatch, replies):
    calls = []

    async def fake_exec(argv, **_):
        calls.append(argv)
        return replies.pop(0)

    monkeypatch.setattr(launcher, "_exec", fake_exec)
    return calls


async def test_session_ids_read_the_server_pid_and_session_id(monkeypatch):
    _exec_replies(monkeypatch, [(0, "cc-9\t4242\t$3\nmain\t4242\t$0\nbad line\n")])
    assert await builder_watch.session_ids() == {"cc-9": "4242:$3", "main": "4242:$0"}
    _exec_replies(monkeypatch, [(1, "no server running on /tmp/tmux-1000/default")])
    assert await builder_watch.session_ids() == {}
    _exec_replies(monkeypatch, [(1, "server exploded")])
    with pytest.raises(RuntimeError):
        await builder_watch.session_ids()


async def test_key_of_reads_the_session_by_its_id_only_on_the_same_server(monkeypatch):
    calls = _exec_replies(monkeypatch, [(0, "cc-9\t4242\t$3\n"), (0, "GENESIS_BUILDER_KEY=K1\n")])
    assert await builder_watch.key_of("4242:$3") == "K1"
    assert calls[1][-3:] == ["-t", "$3", "GENESIS_BUILDER_KEY"]
    # A new server reuses $3: the old identity is gone, and nothing is read.
    calls = _exec_replies(monkeypatch, [(0, "cc-9\t5151\t$3\n")])
    assert await builder_watch.key_of("4242:$3") is None and len(calls) == 1


async def test_a_pause_of_two_sessions_records_the_recorded_slots_conversation(db, world, stops):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, slot=9)
    world.sessions["cc-10"] = key
    stops["convo"][5010] = {"s-z"}  # cc-9, the recorded slot
    stops["convo"][5011] = {"s-a"}  # cc-10, which sorts first
    await _run(db, [_moved(card, "Ready")])
    (paused,) = await _events(db, "builder_paused")
    assert paused["detail"]["session_id"] == "s-z"


async def test_a_session_that_ended_during_a_two_session_pause_still_pauses(
    db, world, stops, monkeypatch
):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    key = await _running(db, world, card, slot=9)
    world.sessions["cc-10"] = key

    def term(pid):
        stops["signalled"].append(pid)
        world.sessions.pop("cc-10", None)  # it ends by itself meanwhile

    monkeypatch.setattr(builder_watch, "send_term", term)
    await _run(db, [_moved(card, "Ready")])
    assert len(await _events(db, "builder_paused")) == 1
    await _run(db, [_moved(card, "Ready")], now=_later(1))
    assert await _events(db, "builder_exited") == []


async def test_a_lone_session_that_ended_before_its_stop_is_an_exit_not_a_pause(
    db, world, stops, monkeypatch
):
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    real = builder_watch.session_ids
    calls = {"n": 0}

    async def ending():
        calls["n"] += 1
        ids = await real()
        if calls["n"] > 2:  # the pass's own reads saw it; the stop's does not
            ids.pop("cc-9", None)
        return ids

    monkeypatch.setattr(builder_watch, "session_ids", ending)
    await _run(db, [_moved(card, "Ready")])
    assert stops["signalled"] == [] and await _events(db, "builder_paused") == []
    del world.sessions["cc-9"]
    await _run(db, [_moved(card, "Ready")], now=_later(1))
    assert len(await _events(db, "builder_exited")) == 1
