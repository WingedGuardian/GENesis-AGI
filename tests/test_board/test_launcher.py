"""The builder launcher (spec E, E3a): a card moved to Build after the launcher
is armed starts one builder, through a consumed approval row, and nothing else
ever does."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.board import config as board_config
from genesis.board import launcher
from genesis.board import projects_v2 as pv
from genesis.board.launcher import probes
from genesis.db.crud import approval_requests as approval_crud
from genesis.db.crud import board as board_crud
from genesis.db.schema import create_all_tables

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ENABLE = NOW - timedelta(hours=1)
REPO = "owner/repo"
SETTINGS = {"capacity": 1, "min_free_gb": 4, "login_margin_hours": 6, "first_slot": 9}


class FakeOutreach:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.topics: list[str] = []

    async def submit_raw(self, text, request, **_):
        self.sent.append((text, request.source_id))
        self.topics.append(request.topic)


class FakeRT:
    def __init__(self, db):
        self._db = db
        self._outreach_pipeline = FakeOutreach()


def _card(
    item_id="I1",
    *,
    number=7,
    moved=NOW - timedelta(minutes=3),
    kind="Issue",
    repo=REPO,
    status="Build",
    closers=(),
):
    content = {
        "__typename": kind,
        "number": number,
        "state": "OPEN",
        "repository": {"nameWithOwner": repo},
    }
    if kind == "Issue":
        content["closedByPullRequestsReferences"] = {
            "totalCount": len(closers),
            "nodes": list(closers),
        }
    return {
        "id": item_id,
        "isArchived": False,
        "status": {"name": status, "updatedAt": moved.isoformat().replace("+00:00", "Z")},
        "content": content,
    }


def _key(card):
    return f"{card['id']}@{card['status']['updatedAt']}"


def _facts(**over):
    facts = {
        "number": 7,
        "state": "OPEN",
        "author_association": "OWNER",
        "last_edited_at": None,
        "created_at": "2026-11-01T00:00:00Z",
        "body": "Build the thing.\n",
        "open_prs": [],
        "open_prs_unresolved": False,
    }
    facts.update(over)
    return facts


class World:
    """Every outside reading the launcher makes, controllable per test."""

    def __init__(self, tmp_path):
        self.root = tmp_path / "repo"
        for sub in ("config", "scripts/hooks", ".claude"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / "config" / "BUILDER_PROMPT.md").write_text(
            "Build #{issue} from {snapshot} in {worktree}. {not_a_field}\n"
        )
        (self.root / "scripts" / "cc-slot.sh").write_text("#!/bin/sh\n")
        (self.root / launcher.GUARD_HOOK).write_text("# guard\n")
        (self.root / ".claude" / "settings.json").write_text(
            '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", '
            '"command": "x/.claude/hooks/genesis-hook hooks/builder_guard.py"}]}]}}'
        )
        self.builders = tmp_path / "builders"
        self.reset()

    def reset(self):
        self.sessions: dict[str, str | None] = {"cc-1": None, "lobby": None}
        self.facts = _facts()
        self.premise = 0
        self.premise_repos: list[str] = []
        self.mem_kb = 8 * 1024 * 1024
        self.expiry = NOW + timedelta(days=3)
        self.armed = (True, "armed")
        self.launch_rc = 0
        self.launches: list[list[str]] = []
        self.calls: list[str] = []
        self.comes_up = True
        self.active_units: set[str] = set()
        self.stopped: list[str] = []
        self.unit_default = "gone"
        self.units_unreadable = False
        #: The issue the slot door pinned on each session (GENESIS_BUILDER_ISSUE).
        self.session_issue: dict[str, int] = {}
        self.settings = dict(SETTINGS)

    async def tmux_sessions(self):
        return dict(self.sessions)

    async def run_launch(self, argv):
        self.calls.append("launch")
        self.launches.append(argv)
        if self.comes_up and self.launch_rc in (0, 124):
            slot = argv[argv.index("--slot") + 1]
            self.sessions[f"cc-{slot}"] = argv[argv.index("--key") + 1]
            self.session_issue[f"cc-{slot}"] = int(argv[argv.index("--issue") + 1])
        return self.launch_rc, "out"

    async def premise_rc(self, issue, repo):
        self.calls.append("premise")
        self.premise_repos.append(repo)
        return self.premise

    async def launch_facts(self, owner, name, number):
        self.calls.append("facts")
        if isinstance(self.facts, BaseException):
            raise self.facts
        return self.facts

    async def unit_state(self, unit):
        return "active" if unit in self.active_units else self.unit_default

    async def stop_unit(self, unit):
        self.stopped.append(unit)

    async def session_issues(self, sessions):
        return {n: self.session_issue.get(n) for n, k in sessions.items() if k}

    async def starting_units(self):
        if self.units_unreadable:
            raise RuntimeError("systemctl list-units exited 1")
        return len(self.active_units)


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    monkeypatch.setattr(board_config, "builder_launch_armed", lambda: w.armed)
    monkeypatch.setattr(board_config, "builder_launch_settings", lambda: dict(w.settings))
    monkeypatch.setattr(board_config, "tracker_repo", lambda: ("owner", "repo"))
    monkeypatch.setattr(launcher, "_repo_root", lambda: w.root)
    monkeypatch.setattr(launcher, "_builders_dir", lambda: w.builders)
    monkeypatch.setattr(launcher, "_utcnow", lambda: NOW)
    for name in (
        "tmux_sessions",
        "run_launch",
        "premise_rc",
        "unit_state",
        "stop_unit",
        "starting_units",
        "session_issues",
    ):
        monkeypatch.setattr(launcher, name, getattr(w, name))
    monkeypatch.setattr(launcher, "mem_available_kb", lambda: w.mem_kb)
    monkeypatch.setattr(launcher, "login_expiry", lambda: w.expiry)
    monkeypatch.setattr(pv, "launch_facts", w.launch_facts)
    return w


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _arm(db, at=ENABLE):
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="launcher_enabled",
            now=at.isoformat(),
            worker="board_launcher",
            observed_change_key=f"enabled@{at.isoformat()}",
            detail={"key": f"enabled@{at.isoformat()}"},
        )


async def _events(db, event=None):
    got = await board_crud.list_events(db, event=event, limit=500)
    return list(reversed(got["items"]))


async def _kinds(db):
    return [e["event"] for e in await _events(db) if e["event"] != "launcher_enabled"]


async def _approvals(db):
    cur = await db.execute("SELECT * FROM approval_requests ORDER BY created_at")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r, strict=True)) for r in await cur.fetchall()]


async def _run(db, items, now=NOW):
    rt = FakeRT(db)
    out = await launcher.run_launcher(rt, items, now)
    return rt, out


def _later(n):
    return NOW + timedelta(minutes=5 * n)


# ── the happy path ──────────────────────────────────────────────────────────


async def test_a_build_move_launches_one_builder_through_a_consumed_gate(db, world):
    await _arm(db)
    card = _card()
    rt, out = await _run(db, [card])
    assert out["launched"] == 1, out
    [argv] = world.launches
    assert argv[:6] == ["systemd-run", "--user", "--collect", "--wait", "--quiet", "--pipe"]
    # systemd bounds the unit itself, so a server restart cannot leave it running.
    assert f"--property=RuntimeMaxSec={launcher.LAUNCH_TIMEOUT_S}" in argv
    assert launcher.LAUNCH_WAIT_S > launcher.LAUNCH_TIMEOUT_S
    assert argv[argv.index("--key") + 1] == _key(card)
    assert argv[argv.index("--slot") + 1] == "9"  # builders start at first_slot
    assert argv[argv.index("--issue") + 1] == "7"
    with open(argv[argv.index("--prompt-file") + 1]) as fh:
        text = fh.read()
    # Fields are filled by plain replacement: an unknown brace stays as written.
    assert "Build #7 from " in text and "build-7" in text and "{not_a_field}" in text
    [row] = await _approvals(db)
    assert (row["status"], row["resolved_by"]) == ("approved", "board:move:unattributed")
    assert row["action_class"] == "costly_reversible" and row["consumed_at"] is not None
    assert approval_crud.classify_resolver(row["resolved_by"]) == "unknown"
    launching = (await _events(db, "builder_launching"))[0]["detail"]
    assert launching["approval_id"] == row["id"] and launching["slot"] == 9
    assert launching["unit"] in argv[6]
    assert await _kinds(db) == ["builder_launching", "builder_launched"]
    assert world.premise_repos == [REPO]
    assert [s for _, s in rt._outreach_pipeline.sent] == [f"builder_launch:launched:{_key(card)}"]


async def test_the_gate_is_consumed_before_the_launch_starts(db, world, monkeypatch):
    seen = {}

    async def spying_launch(argv):
        seen["rows"] = await _approvals(db)
        return 0, ""

    monkeypatch.setattr(launcher, "run_launch", spying_launch)
    await _arm(db)
    await _run(db, [_card()])
    [row] = seen["rows"]
    assert row["status"] == "approved" and row["consumed_at"] is not None


@pytest.mark.parametrize("broken", ["request", "resolve", "consume"])
async def test_a_gate_failure_launches_nothing(db, world, monkeypatch, broken):
    from genesis.autonomy.approval import ApprovalManager

    if broken == "request":

        async def boom(self, **_):
            raise RuntimeError("db gone")

        monkeypatch.setattr(ApprovalManager, "request_approval", boom)
    elif broken == "resolve":

        async def no(self, *a, **k):
            return False

        monkeypatch.setattr(ApprovalManager, "resolve", no)
    else:

        async def used(*a, **k):
            return False

        monkeypatch.setattr(approval_crud, "mark_consumed", used)
    await _arm(db)
    _, out = await _run(db, [_card()])
    assert world.launches == [] and out["launched"] == 0
    assert await _kinds(db) == ["launch_failed"]
    [failed] = await _events(db, "launch_failed")
    assert failed["detail"]["strike"] is True and "approval gate" in failed["reason"]


async def test_a_second_tick_does_not_launch_again(db, world):
    await _arm(db)
    card = _card()
    await _run(db, [card])
    _, out = await _run(db, [card], now=_later(1))
    assert len(world.launches) == 1 and out["skipped"] == 1


# ── arming ──────────────────────────────────────────────────────────────────


async def test_nothing_happens_while_disarmed(db, world):
    world.armed = (False, "builder_launch.enabled is not true")
    _, out = await _run(db, [_card()])
    assert out == {"armed": False, "reason": "builder_launch.enabled is not true"}
    assert world.launches == [] and world.calls == []
    assert await _events(db) == []


async def test_a_failed_board_read_launches_nothing_but_records_arming(db, world):
    _, out = await _run(db, None)
    assert out["board_read"] == "failed" and world.launches == []
    assert [e["event"] for e in await _events(db)] == ["launcher_enabled"]


async def test_cards_in_build_at_enable_are_listed_once_and_never_launched(db, world):
    old = _card("I1", number=3, moved=NOW - timedelta(days=1))
    rt, out = await _run(db, [old])  # the first armed tick
    assert out.get("enabled_now") and world.launches == []
    assert "#3" in rt._outreach_pipeline.sent[0][0]
    rt2, out = await _run(db, [old], now=_later(1))
    assert world.launches == [] and out["pre_enable"] == 1 and rt2._outreach_pipeline.sent == []
    assert [e["event"] for e in await _events(db)] == ["launcher_enabled", "launch_queued"]
    [row] = await _events(db, "launch_queued")
    assert row["detail"]["hold"] == "pre_enable"


async def test_a_first_armed_tick_with_no_board_read_still_lists_the_backlog(db, world):
    old = _card("I1", number=3, moved=NOW - timedelta(days=1))
    rt, _ = await _run(db, None)
    assert rt._outreach_pipeline.sent == []
    rt, _ = await _run(db, [old], now=_later(1))
    assert "#3" in rt._outreach_pipeline.sent[0][0] and world.launches == []


async def test_re_arming_moves_the_cutoff(db, world):
    """A card moved while the launcher was disarmed is not launched on re-arm."""
    await _arm(db, at=NOW - timedelta(days=2))
    world.armed = (False, "builder_launch.enabled is not true")
    await _run(db, None, now=NOW - timedelta(hours=2))
    moved_while_off = _card(moved=NOW - timedelta(hours=1))
    world.armed = (True, "armed")
    await _run(db, [moved_while_off], now=NOW - timedelta(minutes=30))
    await _run(db, [moved_while_off], now=NOW)
    assert world.launches == []
    assert [e["event"] for e in await _events(db)] == [
        "launcher_enabled",
        "launcher_disabled",
        "launcher_enabled",
        "launch_queued",  # listed to the owner once as already in Build at re-arm
    ]


async def test_a_card_seen_late_after_downtime_still_launches(db, world):
    await _arm(db, at=NOW - timedelta(days=2))
    rt, out = await _run(db, [_card(moved=NOW - timedelta(hours=5))])
    assert out["launched"] == 1
    assert "(seen late)" in rt._outreach_pipeline.sent[-1][0]


# ── refusals: facts about the issue or the move, final for that move ────────


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"state": "CLOSED"}, "not open"),
        ({"author_association": "CONTRIBUTOR"}, "not a maintainer"),
        ({"author_association": None}, "not a maintainer"),
        ({"last_edited_at": "2026-11-15T11:59:00Z"}, "edited after"),
        ({"open_prs": [12]}, "open PR"),
    ],
)
async def test_issue_facts_refuse_the_move_for_good(db, world, change, why):
    await _arm(db)
    world.facts = _facts(**change)
    rt, out = await _run(db, [_card()])
    assert world.launches == [] and out["refused"] == 1
    [row] = await _events(db, "launch_refused")
    assert why in row["reason"]
    assert await _approvals(db) == []
    rt2, _ = await _run(db, [_card()], now=_later(1))
    assert len(await _events(db, "launch_refused")) == 1
    assert len(rt._outreach_pipeline.sent) == 1 and rt2._outreach_pipeline.sent == []


async def test_an_edit_before_the_move_is_fine(db, world):
    await _arm(db)
    world.facts = _facts(last_edited_at="2026-11-15T11:00:00Z")
    _, out = await _run(db, [_card()])
    assert out["launched"] == 1


async def test_incomplete_premise_checks_refuse(db, world):
    await _arm(db)
    world.premise = 1
    _, out = await _run(db, [_card()])
    assert out["refused"] == 1 and world.launches == []


@pytest.mark.parametrize(
    "kind,repo", [("PullRequest", REPO), ("Issue", "other/repo"), ("DraftIssue", "")]
)
async def test_only_an_issue_in_the_tracker_repo_launches(db, world, kind, repo):
    await _arm(db)
    _, out = await _run(db, [_card(kind=kind, repo=repo)])
    assert out["refused"] == 1 and world.calls == []


async def test_a_missing_issue_refuses(db, world):
    await _arm(db)
    world.facts = pv.ProjectsError("no issue #7 in owner/repo")
    _, out = await _run(db, [_card()])
    assert out["refused"] == 1


@pytest.mark.parametrize(
    "setup",
    [
        lambda w: setattr(w, "premise", 2),
        lambda w: setattr(w, "facts", pv.RequestFailed("timeout")),
        lambda w: setattr(w, "facts", pv.ProjectsError("GraphQL error: something odd")),
        lambda w: setattr(w, "facts", _facts(open_prs_unresolved=True)),
    ],
)
async def test_a_read_that_failed_retries_next_tick_and_never_refuses(db, world, setup):
    await _arm(db)
    setup(world)
    _, out = await _run(db, [_card()])
    assert out["skipped"] == 1 and await _events(db, "launch_refused") == []
    assert world.launches == [] and await _approvals(db) == []
    world.reset()
    _, out = await _run(db, [_card()], now=_later(1))
    assert out["launched"] == 1


# ── holds: install state, re-checked every tick ─────────────────────────────


@pytest.mark.parametrize(
    ("setup", "kind", "why"),
    [
        (lambda w: setattr(w, "mem_kb", 3 * 1024 * 1024), "memory:low", "4 GB"),
        (lambda w: setattr(w, "mem_kb", None), "memory:unread", "could not be read"),
        (lambda w: setattr(w, "expiry", None), "login:unread", "expiry could not be read"),
        (lambda w: setattr(w, "expiry", NOW + timedelta(hours=2)), "login:soon", "re-login needed"),
        (
            lambda w: (w.root / "config" / "BUILDER_PROMPT.md").unlink(),
            "install:template",
            "template is missing",
        ),
        (
            lambda w: (w.root / "scripts" / "cc-slot.sh").unlink(),
            "install:script",
            "slot script is missing",
        ),
        (lambda w: (w.root / launcher.GUARD_HOOK).unlink(), "install:guard", "builder guard"),
        (
            lambda w: (w.root / ".claude" / "settings.json").write_text("{}"),
            "install:guard",
            "builder guard",
        ),
        (
            lambda w: w.sessions.update({"cc-3": "other-key"}),
            "capacity",
            "another builder is running",
        ),
    ],
)
async def test_install_state_holds_once_and_releases(db, world, setup, kind, why):
    await _arm(db)
    setup(world)
    rt, out = await _run(db, [_card()])
    assert out["held"] == 1 and world.launches == [] and await _approvals(db) == []
    assert "premise" not in world.calls and "facts" not in world.calls  # cheap checks first
    [row] = await _events(db, "launch_queued")
    assert why in row["reason"] and row["detail"]["hold"] == kind
    rt2, _ = await _run(db, [_card()], now=_later(1))
    assert len(await _events(db, "launch_queued")) == 1  # one row and one line per reason
    assert len(rt._outreach_pipeline.sent) == 1 and rt2._outreach_pipeline.sent == []
    world.__init__(world.root.parent)  # released
    _, out = await _run(db, [_card()], now=_later(2))
    assert out["launched"] == 1


async def test_a_new_hold_reason_reaches_the_owner(db, world):
    await _arm(db)
    world.sessions["cc-3"] = "other-key"
    await _run(db, [_card()])
    del world.sessions["cc-3"]
    world.expiry = NOW + timedelta(hours=1)
    rt, _ = await _run(db, [_card()], now=_later(1))
    assert [e["detail"]["hold"] for e in await _events(db, "launch_queued")] == [
        "capacity",
        "login:soon",
    ]
    assert "re-login needed" in rt._outreach_pipeline.sent[0][0]


async def test_a_builder_whose_issue_has_an_open_pr_frees_its_place(db, world):
    await _arm(db)
    first = _card("I1", number=5)
    world.facts = _facts(number=5)
    await _run(db, [first])
    assert len(world.launches) == 1
    pr = {
        "number": 9,
        "state": "OPEN",
        "isCrossRepository": False,
        "repository": {"nameWithOwner": REPO},
    }
    done = _card("I1", number=5, status="In Review", closers=[pr])
    world.facts = _facts(number=8)
    _, out = await _run(db, [done, _card("I2", number=8)], now=_later(1))
    assert out["launched"] == 1 and len(world.launches) == 2
    assert world.launches[1][world.launches[1].index("--slot") + 1] == "10"


async def test_a_door_refusal_waits_quietly_until_the_slots_change(db, world):
    """Exit 75 (a slot taken, the box at its cap) is not a strike, and while
    the tmux sessions stay as they were it writes nothing and runs no gate."""
    await _arm(db)
    world.launch_rc = launcher.RC_REFUSED
    _, out = await _run(db, [_card()])
    assert out["held"] == 1
    [failed] = await _events(db, "launch_failed")
    assert failed["detail"]["strike"] is False
    for n in range(1, 6):
        _, out = await _run(db, [_card()], now=_later(n))
        assert out["held"] == 1
    assert len(world.launches) == 1 and len(await _approvals(db)) == 1
    assert len(await _events(db)) == 4  # enabled, launching, failed, queued
    world.sessions["cc-2"] = None  # the owner opened or closed a session
    world.launch_rc = 0
    _, out = await _run(db, [_card()], now=_later(7))
    assert out["launched"] == 1 and len(await _approvals(db)) == 2


# ── absorb, strikes, settle ─────────────────────────────────────────────────


async def test_a_second_move_of_a_building_issue_is_absorbed(db, world):
    await _arm(db)
    card = _card()
    await _run(db, [card])
    again = _card(moved=NOW + timedelta(minutes=1))
    _, out = await _run(db, [again], now=_later(1))
    assert out["absorbed"] == 1 and len(world.launches) == 1
    [row] = await _events(db, "move_absorbed")
    assert row["detail"]["builder"] == _key(card)


async def test_three_strikes_stop_the_move(db, world):
    await _arm(db)
    world.launch_rc = 1
    sent = []
    for n in range(5):
        rt, _ = await _run(db, [_card()], now=_later(n))
        sent += rt._outreach_pipeline.sent
    assert len(world.launches) == 3
    assert len(await _events(db, "launch_failed")) == 3
    assert [text for text, _ in sent if "3 times" in text], sent


async def test_a_timed_out_launch_that_came_up_counts_as_launched(db, world):
    await _arm(db)
    world.launch_rc = 124
    _, out = await _run(db, [_card()])
    assert out["launched"] == 1
    assert world.stopped and world.stopped[0].startswith("genesis-builder-7-")


async def test_a_builder_that_comes_up_after_its_strike_is_recorded(db, world):
    """A slow unit: the launch timed out with no session, so it was a strike;
    the session appears later. The move's own builder is recorded, never
    counted against it as another builder."""
    await _arm(db)
    world.launch_rc, world.comes_up = 124, False
    await _run(db, [_card()])
    assert len(await _events(db, "launch_failed")) == 1
    world.sessions["cc-9"] = _key(_card())
    _, out = await _run(db, [_card()], now=_later(1))
    assert len(world.launches) == 1 and out["skipped"] == 1
    [row] = await _events(db, "builder_launched")
    assert row["detail"]["settled"] is True


async def _interrupted_attempt(db, card, unit="genesis-builder-7-abc"):
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=7,
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{_key(card)}#a1",
            detail={"key": _key(card), "slot": 4, "session_id": "s", "unit": unit},
        )


async def test_a_restart_mid_launch_is_settled_from_tmux(db, world):
    await _arm(db)
    card = _card()
    await _interrupted_attempt(db, card)
    world.sessions["cc-4"] = _key(card)
    _, out = await _run(db, [card], now=_later(1))
    assert world.launches == [] and out["skipped"] == 1
    [row] = await _events(db, "builder_launched")
    assert row["detail"]["settled"] is True and row["detail"]["slot"] == 4


async def test_an_attempt_whose_unit_still_runs_is_waited_for(db, world):
    await _arm(db)
    card = _card()
    await _interrupted_attempt(db, card)
    world.active_units.add("genesis-builder-7-abc")
    _, out = await _run(db, [card], now=_later(1))
    assert world.launches == [] and out["skipped"] == 1
    assert await _events(db, "launch_failed") == []


async def test_a_restart_mid_launch_with_no_session_is_a_strike_then_a_retry(db, world):
    await _arm(db)
    card = _card()
    await _interrupted_attempt(db, card)
    _, out = await _run(db, [card], now=_later(1))
    [failed] = await _events(db, "launch_failed")
    assert failed["attempt"] == 1 and failed["detail"]["strike"] is True
    assert out["launched"] == 1
    assert (await _events(db, "builder_launching"))[-1]["attempt"] == 2


# ── never raises ────────────────────────────────────────────────────────────


async def test_an_unexpected_error_is_reported_not_raised(db, world, monkeypatch):
    async def broken():
        raise RuntimeError("tmux exploded")

    monkeypatch.setattr(launcher, "tmux_sessions", broken)
    await _arm(db)
    _, out = await _run(db, [_card()])
    assert "tmux exploded" in out["error"] and world.launches == []


# ── the tmux reading itself ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        ([(1, "no server running on /tmp/tmux-1000/default")], {}),
        (
            [
                (0, "cc-1\ncc-9\n"),
                (1, "unknown variable: GENESIS_BUILDER_KEY"),
                (0, "GENESIS_BUILDER_KEY=k@1\n"),
            ],
            {"cc-1": None, "cc-9": "k@1"},
        ),
        ([(0, "cc-1\n"), (1, "can't find session: =cc-1")], {}),
    ],
)
async def test_tmux_sessions_reads_keys(monkeypatch, replies, expected):
    queue = list(replies)

    async def fake_exec(argv, **_):
        return queue.pop(0)

    monkeypatch.setattr(probes, "_exec", fake_exec)
    assert await launcher.tmux_sessions() == expected


@pytest.mark.parametrize(
    "replies",
    [
        [(1, "lost server")],
        [(0, "cc-1\n"), (124, "timed out after 15s")],
    ],
)
async def test_a_tmux_reading_that_did_not_happen_raises(monkeypatch, replies):
    """Never a smaller capacity count from a failed read (that would fail open)."""
    queue = list(replies)

    async def fake_exec(argv, **_):
        return queue.pop(0)

    monkeypatch.setattr(probes, "_exec", fake_exec)
    with pytest.raises(RuntimeError):
        await launcher.tmux_sessions()


@pytest.mark.parametrize(
    ("rc", "out", "expected"),
    [
        (0, "premise_gate: issue 7: ready for devin-ready\n", 0),
        (
            1,
            "missing premise answer 6: PR shape\npremise_gate: issue 7: NOT ready for devin-ready "
            "(1 of 6 missing)\n",
            1,
        ),
        # A crash exits 1 too; it is a failed read, never a refusal.
        (1, "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n", 2),
        (2, "premise_gate: issue 7: could not be read\n", 2),
        (124, "timed out after 120s", 2),
    ],
)
async def test_premise_rc_reads_the_gate_verdict(monkeypatch, tmp_path, rc, out, expected):
    seen = {}

    async def fake_exec(argv, **kw):
        seen.update(kw)
        return rc, out

    monkeypatch.setattr(probes, "_exec", fake_exec)
    monkeypatch.setattr(launcher, "_repo_root", lambda: tmp_path)
    assert await launcher.premise_rc(7, REPO) == expected
    assert seen["env"] == {"GH_REPO": REPO}


# ── review round: gate loops, unknown outcomes, starting attempts, history ──


async def test_a_gate_that_keeps_failing_reaches_three_strikes_and_tells_the_owner(
    db, world, monkeypatch
):
    from genesis.autonomy.approval import ApprovalManager

    async def no(self, *a, **k):
        return False

    monkeypatch.setattr(ApprovalManager, "resolve", no)
    await _arm(db)
    sent = []
    for n in range(6):
        rt, _ = await _run(db, [_card()], now=_later(n))
        sent += rt._outreach_pipeline.sent
    failed = await _events(db, "launch_failed")
    assert [e["attempt"] for e in failed] == [1, 2, 3]
    assert len([t for t, _ in sent if "3 times" in t]) == 1
    assert world.launches == []


async def test_a_launch_whose_outcome_cannot_be_read_holds_its_place(db, world, monkeypatch):
    """The tmux read after a launch failed: the next card must not start too."""
    await _arm(db)
    reads = {"n": 0}

    async def flaky():
        reads["n"] += 1
        if reads["n"] == 2:  # the read right after the first launch
            raise RuntimeError("tmux hiccup")
        return dict(world.sessions)

    monkeypatch.setattr(launcher, "tmux_sessions", flaky)
    world.facts = _facts(number=7)
    two = [_card("I1", number=7), _card("I2", number=8, moved=NOW - timedelta(minutes=2))]
    _, out = await _run(db, two)
    assert len(world.launches) == 1, out


async def test_an_attempt_still_starting_holds_its_place(db, world):
    await _arm(db)
    first = _card("I1", number=7)
    await _interrupted_attempt(db, first)
    world.active_units.add("genesis-builder-7-abc")
    world.facts = _facts(number=8)
    second = _card("I2", number=8, moved=NOW - timedelta(minutes=2))
    _, out = await _run(db, [first, second], now=_later(1))
    assert world.launches == [], out
    [row] = await _events(db, "launch_queued")
    assert row["issue_number"] == 8 and row["detail"]["hold"] == "capacity"


async def test_an_unreadable_unit_state_is_waited_for_not_relaunched(db, world):
    await _arm(db)
    card = _card()
    await _interrupted_attempt(db, card)
    world.unit_default = "unknown"
    _, out = await _run(db, [card], now=_later(1))
    assert world.launches == [] and await _events(db, "launch_failed") == []


async def test_two_sessions_with_one_key_are_two_builders(db, world):
    await _arm(db)
    world.sessions.update({"cc-9": "k-old", "cc-10": "k-old"})
    world.settings["capacity"] = 2
    _, out = await _run(db, [_card()])
    assert out["held"] == 1 and world.launches == []


async def test_other_issues_launch_rows_past_one_read_do_not_hold(db, world, monkeypatch):
    """No lifetime row count decides capacity: rows of other issues past the
    read limit (never pruned) once held every move forever."""
    monkeypatch.setattr(launcher, "EVENT_READ_LIMIT", 1)
    await _arm(db)
    for n in (1, 2):
        async with board_crud.owned_connection(db) as own:
            await board_crud.append_event(
                own,
                event="builder_launching",
                now=NOW.isoformat(),
                repo=REPO,
                issue_number=40 + n,
                attempt=1,
                worker="board_launcher",
                observed_change_key=f"old{n}#a1",
                detail={"key": f"old{n}"},
            )
    _, out = await _run(db, [_card()])
    assert len(world.launches) == 1, out


async def test_a_launch_history_of_its_own_issue_longer_than_one_read_holds(db, world, monkeypatch):
    monkeypatch.setattr(launcher, "EVENT_READ_LIMIT", 1)
    await _arm(db)
    card = _card()
    for n in (1, 2):
        async with board_crud.owned_connection(db) as own:
            await board_crud.append_event(
                own,
                event="move_absorbed",
                now=NOW.isoformat(),
                repo=REPO,
                issue_number=card["content"]["number"],
                worker="board_launcher",
                observed_change_key=f"old{n}",
                detail={"key": f"old{n}"},
            )
    _, out = await _run(db, [card])
    assert world.launches == []
    [row] = await _events(db, "launch_queued")
    assert row["detail"]["hold"] == "history"


@pytest.mark.parametrize(
    ("rc", "output", "expected"),
    [
        (
            0,
            "genesis-builder-7-aa.service loaded active running x\n"
            "genesis-builder-8-bb.service loaded activating start x\n"
            "genesis-builder-9-cc.service loaded failed failed x\n"
            "genesis-builder-5-dd.service loaded inactive dead x\n"
            "genesis-slot-1.scope loaded active running x\n"
            "Warning: something on stderr\n",
            2,
        ),
        (0, "", 0),
    ],
)
async def test_starting_units_counts_live_builder_units(monkeypatch, rc, output, expected):
    calls = []

    async def fake_exec(argv, **kw):
        calls.append(argv)
        return rc, output

    monkeypatch.setattr(probes, "_exec", fake_exec)
    assert await probes.starting_units() == expected
    assert calls[0][:3] == ["systemctl", "--user", "list-units"]


async def test_an_unreadable_unit_list_raises(monkeypatch):
    async def fake_exec(argv, **kw):
        return 1, "Failed to connect to bus"

    monkeypatch.setattr(probes, "_exec", fake_exec)
    with pytest.raises(RuntimeError):
        await probes.starting_units()


async def test_a_session_with_no_pinned_issue_holds_its_place(db, world):
    """A session the slot door never pinned (an older build) reads as building."""
    await _arm(db)
    world.sessions["cc-9"] = "PVTI_old@2026-11-01T00:00:00Z"
    _, out = await _run(db, [_card()])
    assert world.launches == [] and out["held"] == 1


async def test_an_off_board_builder_with_an_open_pr_frees_its_place(db, world):
    await _arm(db)
    world.sessions["cc-9"] = "PVTI_gone@2026-11-01T00:00:00Z"
    world.session_issue["cc-9"] = 41
    world.facts = {**world.facts, "open_prs": [{"number": 90}], "open_prs_unresolved": False}
    # The new card's own facts must show no PR, so they are served per issue.
    real = world.launch_facts

    async def per_issue(owner, name, number):
        f = await real(owner, name, number)
        return f if number == 41 else {**f, "open_prs": [], "open_prs_unresolved": False}

    import genesis.board.projects_v2 as pvm

    pvm.launch_facts = per_issue
    try:
        _, out = await _run(db, [_card()])
    finally:
        pvm.launch_facts = real
    assert len(world.launches) == 1, out


async def test_an_off_board_builder_without_a_pr_holds_its_place(db, world):
    await _arm(db)
    world.sessions["cc-9"] = "PVTI_gone@2026-11-01T00:00:00Z"
    world.session_issue["cc-9"] = 41
    _, out = await _run(db, [_card()])
    assert world.launches == [] and out["held"] == 1


@pytest.mark.parametrize(
    "facts",
    [RuntimeError("graphql down"), {"open_prs": [{"number": 90}], "open_prs_unresolved": True}],
)
async def test_an_off_board_builder_holds_on_a_failed_or_partial_read(db, world, facts):
    await _arm(db)
    world.sessions["cc-9"] = "PVTI_gone@2026-11-01T00:00:00Z"
    world.session_issue["cc-9"] = 41
    real = world.launch_facts

    async def per_issue(owner, name, number):
        if number == 41:
            if isinstance(facts, BaseException):
                raise facts
            return {**(await real(owner, name, number)), **facts}
        return await real(owner, name, number)

    import genesis.board.projects_v2 as pvm

    pvm.launch_facts = per_issue
    try:
        _, out = await _run(db, [_card()])
    finally:
        pvm.launch_facts = real
    assert world.launches == [] and out["held"] == 1


async def test_an_off_board_builder_is_not_read_when_capacity_is_already_full(db, world):
    await _arm(db)
    world.sessions["cc-9"] = "PVTI_gone@2026-11-01T00:00:00Z"
    world.active_units.add("genesis-builder-3-aa")
    world.session_issue["cc-9"] = 41
    world.calls.clear()
    await _run(db, [_card()])
    assert world.calls.count("facts") <= 1  # only the new card's own read, if any


async def test_a_builder_whose_issue_closed_frees_its_place(db, world):
    """Its PR merged and closed the issue, but its session is still up."""
    await _arm(db)
    done = _card("I9", number=9, status="Done")
    done["content"]["state"] = "CLOSED"
    world.sessions["cc-9"] = _key(done)
    world.session_issue["cc-9"] = 9
    _, out = await _run(db, [_card(), done])
    assert len(world.launches) == 1, out


async def test_units_are_read_before_sessions(db, world):
    """A launch finishing between the two reads must show in one of them."""
    await _arm(db)
    order = []
    real_units, real_sessions = world.starting_units, world.tmux_sessions

    async def units():
        order.append("units")
        return await real_units()

    async def sessions():
        order.append("sessions")
        return await real_sessions()

    launcher.starting_units, launcher.tmux_sessions = units, sessions
    try:
        await _run(db, [_card()])
    finally:
        launcher.starting_units, launcher.tmux_sessions = real_units, real_sessions
    assert order[:2] == ["units", "sessions"]


async def test_a_running_launch_unit_holds_a_place(db, world):
    await _arm(db)
    world.active_units.add("genesis-builder-3-abcd1234")
    _, out = await _run(db, [_card()])
    assert world.launches == [] and out["held"] == 1


async def test_an_unreadable_unit_list_launches_nothing(db, world):
    await _arm(db)
    world.units_unreadable = True
    _, out = await _run(db, [_card()])
    assert world.launches == []


async def test_a_card_from_another_repo_is_not_filed_under_a_tracker_issue(db, world):
    await _arm(db)
    rt, _ = await _run(db, [_card(repo="other/repo", number=7)])
    [row] = await _events(db, "launch_refused")
    assert row["issue_number"] is None and row["repo"] is None
    assert "card I1" in rt._outreach_pipeline.sent[0][0]


async def test_launch_facts_reads_the_issue(monkeypatch):
    async def runner(payload):
        import json as _json

        assert '"i": 7' in payload
        return (
            0,
            _json.dumps(
                {
                    "data": {
                        "repository": {
                            "issue": {
                                "number": 7,
                                "state": "OPEN",
                                "authorAssociation": "OWNER",
                                "lastEditedAt": None,
                                "createdAt": "2026-11-01T00:00:00Z",
                                "body": None,
                                "closedByPullRequestsReferences": {
                                    "totalCount": 3,
                                    "nodes": [
                                        {
                                            "number": 1,
                                            "state": "OPEN",
                                            "isCrossRepository": False,
                                            "repository": {"nameWithOwner": "Owner/Repo"},
                                        },
                                        {
                                            "number": 2,
                                            "state": "OPEN",
                                            "isCrossRepository": True,
                                            "repository": {"nameWithOwner": "owner/repo"},
                                        },
                                    ],
                                },
                            }
                        }
                    }
                }
            ),
            "",
        )

    facts = await pv.launch_facts("owner", "repo", 7, runner=runner)
    assert facts["open_prs"] == [1] and facts["open_prs_unresolved"] is True
    assert facts["body"] == "" and facts["last_edited_at"] is None


async def test_launch_facts_names_a_missing_issue():
    async def runner(payload):
        return 0, '{"data": {"repository": {"issue": null}}}', ""

    with pytest.raises(pv.ProjectsError, match="^no issue #7"):
        await pv.launch_facts("owner", "repo", 7, runner=runner)


# ── PR review round 1 ───────────────────────────────────────────────────────


async def test_each_owner_line_has_its_own_dedup_identity(db, world):
    """Outreach governance dedups on (signal_type, topic, category), not source_id."""
    await _arm(db)
    world.sessions["cc-3"] = "other-key"
    rt, _ = await _run(db, [_card("I1", number=7), _card("I2", number=8)])
    assert len(rt._outreach_pipeline.topics) == 2
    assert len(set(rt._outreach_pipeline.topics)) == 2


async def test_disarming_during_the_checks_cancels_the_launch(db, world, monkeypatch):
    await _arm(db)

    async def premise_then_disarm(issue, repo):
        world.armed = (False, "builder_launch.enabled is not true")
        return 0

    monkeypatch.setattr(launcher, "premise_rc", premise_then_disarm)
    _, out = await _run(db, [_card()])
    assert world.launches == [] and await _approvals(db) == [] and out["launched"] == 0


async def test_disarming_after_the_gate_closes_the_attempt_without_a_strike(db, world, monkeypatch):
    from genesis.autonomy.approval import ApprovalManager

    await _arm(db)
    real = ApprovalManager.resolve

    async def resolve_then_disarm(self, *a, **k):
        world.armed = (False, "builder_launch.enabled is not true")
        return await real(self, *a, **k)

    monkeypatch.setattr(ApprovalManager, "resolve", resolve_then_disarm)
    await _run(db, [_card()])
    assert world.launches == []
    [failed] = await _events(db, "launch_failed")
    assert failed["detail"]["strike"] is False and "disarmed" in failed["reason"]


@pytest.mark.parametrize(
    "settings",
    [
        '{"note": "hooks/builder_guard.py"}',
        '{"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", '
        '"command": "x/genesis-hook hooks/builder_guard.py"}]}]}}',
        '{"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", '
        '"command": "x/genesis-hook hooks/builder_guard.py"}]}]}}',
        "not json",
    ],
)
async def test_a_guard_named_but_not_wired_on_bash_holds(db, world, settings):
    await _arm(db)
    (world.root / ".claude" / "settings.json").write_text(settings)
    _, out = await _run(db, [_card()])
    assert out["held"] == 1 and world.launches == []
    [row] = await _events(db, "launch_queued")
    assert row["detail"]["hold"] == "install:guard"


@pytest.mark.parametrize("matcher", ["Bash", ".*", "", "Bash|Edit"])
def test_the_guard_counts_as_wired_on_any_matcher_that_covers_bash(tmp_path, matcher):
    f = tmp_path / "settings.json"
    f.write_text(
        '{"hooks": {"PreToolUse": [{"matcher": "%s", "hooks": [{"type": "command", '
        '"command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook hooks/builder_guard.py"}]}]}}'.replace(
            "%s", matcher
        )
    )
    assert launcher._guard_wired(f) is True


async def test_an_attempt_still_starting_for_a_card_that_left_build_holds_its_place(db, world):
    await _arm(db)
    gone = _card("I9", number=9)
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=9,
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{_key(gone)}#a1",
            detail={"key": _key(gone), "slot": 9, "session_id": "s", "unit": "genesis-builder-9-x"},
        )
    world.active_units.add("genesis-builder-9-x")
    _, out = await _run(db, [_card("I1", number=7)])
    assert world.launches == [] and out["held"] == 1


async def test_another_repos_issue_does_not_free_a_tracker_builders_place(db, world):
    await _arm(db)
    first = _card("I1", number=5)
    world.facts = _facts(number=5)
    await _run(db, [first])
    pr = {
        "number": 9,
        "state": "OPEN",
        "isCrossRepository": False,
        "repository": {"nameWithOwner": REPO},
    }
    foreign = _card("X5", number=5, repo="other/repo", status="In Review", closers=[pr])
    world.facts = _facts(number=8)
    _, out = await _run(db, [first, foreign, _card("I2", number=8)], now=_later(1))
    assert len(world.launches) == 1 and out["held"] == 1


async def test_a_third_strike_found_by_settling_tells_the_owner(db, world):
    await _arm(db)
    world.launch_rc = 1
    for n in range(2):
        await _run(db, [_card()], now=_later(n))
    card = _card()
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=NOW.isoformat(),
            repo=REPO,
            issue_number=7,
            attempt=3,
            worker="board_launcher",
            observed_change_key=f"{_key(card)}#a3",
            detail={"key": _key(card), "slot": 9, "session_id": "s", "unit": "gone-unit"},
        )
    rt, _ = await _run(db, [card], now=_later(3))
    assert any("3 times" in t for t, _ in rt._outreach_pipeline.sent)


def test_free_memory_is_the_cgroup_reading(monkeypatch):
    from genesis.hostmetrics import readings

    monkeypatch.setattr(
        readings, "read_memory", lambda: readings.Memory(18 << 30, 3 << 30, "cgroup")
    )
    assert launcher.mem_available_kb() == 3 * 1024 * 1024
    monkeypatch.setattr(readings, "read_memory", lambda: None)
    assert launcher.mem_available_kb() is None


# ── adversarial audit of the round-1 fix ────────────────────────────────────


def test_the_launchers_owner_lines_dedup_on_their_topic_alone():
    from genesis.outreach import governance

    assert "builder_launch" in governance._TOPIC_IS_IDENTITY


@pytest.mark.parametrize(
    ("matcher", "covers"),
    [
        ("*", True),
        ("", True),
        ("Bash", True),
        ("Edit, Bash", True),
        ("Edit|Bash", True),
        ("Ba.h", True),
        ("Edit", False),
        ("Bash2", False),
        ("[", False),
    ],
)
def test_matchers_follow_claude_codes_rules(matcher, covers):
    assert launcher._matcher_covers_bash(matcher) is covers


@pytest.mark.parametrize(
    ("command", "wired"),
    [
        ("x/genesis-hook hooks/builder_guard.py", True),
        ("[ -d x ] || exit 0; x/genesis-hook hooks/builder_guard.py", True),
        ("echo hooks/builder_guard.py.bak", False),
    ],
)
def test_the_guard_command_must_run_the_hook(tmp_path, command, wired):
    f = tmp_path / "s.json"
    f.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
                    ]
                }
            }
        )
    )
    assert launcher._guard_wired(f) is wired


def test_disabled_hooks_mean_no_guard(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(
        json.dumps(
            {
                "disableAllHooks": True,
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "x/genesis-hook hooks/builder_guard.py",
                                }
                            ],
                        }
                    ]
                },
            }
        )
    )
    assert launcher._guard_wired(f) is False


async def test_an_unsettled_attempt_whose_unit_is_gone_holds_no_place(db, world):
    """Places come from live units, not rows: systemd retires a launch unit at
    its RuntimeMaxSec, so a row left without an outcome holds nothing."""
    await _arm(db, at=NOW - timedelta(days=1))
    old = _card("I9", number=9)
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event="builder_launching",
            now=(NOW - timedelta(hours=2)).isoformat(),
            repo=REPO,
            issue_number=9,
            attempt=1,
            worker="board_launcher",
            observed_change_key=f"{_key(old)}#a1",
            detail={"key": _key(old), "slot": 9, "session_id": "s", "unit": "u-old"},
        )
    _, out = await _run(db, [_card("I1", number=7)])
    assert out["launched"] == 1


async def test_a_repeated_strike_row_neither_counts_nor_tells_the_owner_twice(db, world):
    await _arm(db)
    card = _card()
    tick = launcher._Tick(FakeRT(db), NOW, REPO)
    hist = []
    for _ in range(2):
        await launcher._strike(
            tick, {**card, "key": _key(card), "number": 7, "item": "I1"}, 3, "rc 1", hist, 9
        )
    assert tick.out["failed"] == 1
