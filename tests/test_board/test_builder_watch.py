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

    async def pane_pids(slot):
        return [5000 + slot]

    monkeypatch.setattr(builder_watch, "pane_pids", pane_pids)
    monkeypatch.setattr(builder_watch, "claude_pids_under", lambda panes: [p + 1 for p in panes])
    monkeypatch.setattr(builder_watch, "send_term", lambda pid: state["signalled"].append(pid))
    monkeypatch.setattr(builder_watch, "alive", lambda pid: False)
    monkeypatch.setattr(builder_watch, "STOP_WAIT_S", 1)

    async def kill_session(slot):
        state["killed"].append(slot)
        if state["kill_works"]:
            world.sessions.pop(f"cc-{slot}", None)

    monkeypatch.setattr(builder_watch, "kill_session", kill_session)
    state["clients"] = False

    async def clients(slot):
        return state["clients"]

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


async def test_a_close_frees_the_place_for_the_next_card_in_the_same_pass(db, world, stops):
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


@pytest.mark.parametrize("how", ["closed", "done"])
async def test_a_builder_whose_work_finished_is_closed_and_frees_its_place(db, world, stops, how):
    """A merged PR closes the issue and drops out of the board's open closers."""
    await _arm(db)
    card = _card(moved=NOW - timedelta(hours=3))
    await _running(db, world, card)
    moved = _moved(card, "Done" if how == "done" else "In Review")
    if how == "closed":
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
    real = builder_watch._session_for
    calls = {"n": 0}

    async def flaky(tick, key, number, slot, recorded):
        calls["n"] += 1
        if number == 5:
            raise RuntimeError("bad row")
        return await real(tick, key, number, slot, recorded)

    monkeypatch.setattr(builder_watch, "_session_for", flaky)
    _, out = await _run(db, [_moved(a, "Ready"), _moved(b, "Ready")])
    assert out["watch"]["row_errors"] == 1 and out["watch"]["paused"] == 1


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


def test_session_alive_reads_command_lines(tmp_path):
    proc = tmp_path / "proc"
    (proc / "42").mkdir(parents=True)
    (proc / "42" / "cmdline").write_bytes(b"claude\0-r\0sess-x\0")
    assert builder_watch.session_alive("sess-x", proc) is True
    assert builder_watch.session_alive("sess-y", proc) is False
    assert builder_watch.session_alive(None, proc) is False
