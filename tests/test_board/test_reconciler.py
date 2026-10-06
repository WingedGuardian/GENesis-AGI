"""The read-only board reconciler tick: every gate state pulses without
touching GitHub, a read publishes counts with their denominators, drags are
logged once, an error publishes no partial counts, and the module never calls
a GitHub write."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite
import pytest

from genesis.board import config as board_config
from genesis.board import projects_v2 as pv
from genesis.board import reconciler
from genesis.db.crud import board as board_crud
from genesis.db.schema import create_all_tables

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
REPO = "owner/repo"


class FakeBus:
    def __init__(self):
        self.emits = []

    async def emit(self, subsystem, severity, event_type, message, **details):
        self.emits.append(
            {
                "subsystem": subsystem,
                "severity": severity,
                "type": event_type,
                "message": message,
                "details": details,
            }
        )


class FakeRT:
    def __init__(self, db, *, paused=False):
        self._db = db
        self._event_bus = FakeBus()
        self._paused_value = paused
        self.successes: list[str] = []
        self.failures: list[tuple[str, object]] = []

    @property
    def paused(self):
        if isinstance(self._paused_value, Exception):
            raise self._paused_value
        return self._paused_value

    def record_job_success(self, name):
        self.successes.append(name)

    def record_job_failure(self, name, error=None, *, exc=None):
        self.failures.append((name, exc if exc is not None else error))


def _item(item_id, status, *, updated=None, number=1, state="OPEN", kind="Issue", repo=REPO):
    return {
        "id": item_id,
        "isArchived": False,
        "status": {"name": status, "optionId": "o", "updatedAt": updated} if status else None,
        "genesis": None,
        "note": None,
        "content": {
            "__typename": kind,
            "number": number,
            "state": state,
            "repository": {"nameWithOwner": repo},
        },
    }


class FakeBoard:
    def __init__(self, items, *, open_issues=3, open_prs=1, fail=None, closed=False):
        self.items = items
        self.open = {"issues": open_issues, "pull_requests": open_prs}
        self.fail = fail
        self.closed = closed
        self.calls = 0

    def install(self, monkeypatch):
        async def get_project(owner, number, *, runner=None):
            self.calls += 1
            return pv.Project("PROJ", number, "Board", False, {}, closed=self.closed)

        async def list_items(project_id, *, runner=None):
            self.calls += 1
            if self.fail:
                raise self.fail
            return {"items": list(self.items), "total": len(self.items)}

        async def repo_open_counts(owner, name, *, runner=None):
            self.calls += 1
            return dict(self.open)

        monkeypatch.setattr(pv, "get_project", get_project)
        monkeypatch.setattr(pv, "list_items", list_items)
        monkeypatch.setattr(pv, "repo_open_counts", repo_open_counts)
        return self


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "propose_only")
    monkeypatch.setattr(board_config, "project_ref", lambda: ("owner", 2))
    monkeypatch.setattr(board_config, "tracker_repo", lambda: ("owner", "repo"))
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


def _pulse(rt):
    assert len(rt._event_bus.emits) == 1
    e = rt._event_bus.emits[0]
    assert e["type"] == "heartbeat" and str(e["subsystem"]) == "board"
    return e["details"]


@pytest.mark.parametrize(
    ("setup", "state"),
    [
        ("off", "off"),
        ("paused", "paused"),
        ("unset", "not_set_up"),
    ],
)
async def test_gate_states_pulse_and_succeed_without_touching_github(db, monkeypatch, setup, state):
    board = FakeBoard([]).install(monkeypatch)
    rt = FakeRT(db, paused=(setup == "paused"))
    if setup == "off":
        monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    if setup == "unset":
        monkeypatch.setattr(board_config, "project_ref", lambda: None)
    out = await reconciler.run_tick(rt, now=NOW)
    assert board.calls == 0
    assert out["board_state"] == state == _pulse(rt)["board_state"]
    assert rt.successes == [reconciler.JOB_ID] and rt.failures == []


async def test_a_failed_pause_check_skips_github_and_records_a_failure(db, monkeypatch):
    board = FakeBoard([]).install(monkeypatch)
    rt = FakeRT(db, paused=RuntimeError("pause file unreadable"))
    out = await reconciler.run_tick(rt, now=NOW)
    assert board.calls == 0
    assert out["board_state"] == "pause_check_failed"
    assert rt.successes == [] and rt.failures[0][0] == reconciler.JOB_ID


async def test_a_read_publishes_counts_with_their_denominators(db, monkeypatch):
    items = [
        _item("I1", "Proposed", number=1),
        _item("I2", "Ready", number=2),
        _item("I3", None, number=3),
        _item("P1", "In Review", number=9, kind="PullRequest"),
        _item("X1", "Done", number=4, state="CLOSED"),
        _item("Z1", "Proposed", number=5, repo="someone/else"),
    ]
    FakeBoard(items, open_issues=5, open_prs=2).install(monkeypatch)
    rt = FakeRT(db)
    out = await reconciler.run_tick(rt, now=NOW)
    d = _pulse(rt)
    assert out["board_state"] == "ok" and d["board_state"] == "ok"
    assert d["items_total"] == 6
    assert sum(d["by_status"].values()) == d["items_total"]
    assert d["by_status"]["No Status"] == 1 and d["by_status"]["Proposed"] == 2
    assert sum(d["by_kind"].values()) == d["items_total"]
    # open items of the tracker repo on the board, against the repo's own open total
    assert d["coverage"] == {"on_board": 4, "open_in_repo": 7, "other_repo_items": 1}
    assert rt.successes == [reconciler.JOB_ID]


async def test_a_drag_is_logged_once_across_ticks(db, monkeypatch):
    fresh = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FakeBoard([_item("I1", "In Progress", updated=fresh, number=7)]).install(monkeypatch)
    for tick in (NOW, NOW + timedelta(minutes=5)):
        rt = FakeRT(db)
        await reconciler.run_tick(rt, now=tick)
    events = await board_crud.list_events(db, event="drag")
    assert events["total"] == 1
    ev = events["items"][0]
    assert ev["issue_number"] == 7 and ev["project_item_id"] == "I1"
    assert ev["observed_change_key"] == f"I1@{fresh}"
    assert ev["detail"]["observed_late"] is False


async def test_a_drag_older_than_the_window_is_flagged_late(db, monkeypatch):
    """Enable-time flood, re-log after prune, downtime: all read as late,
    derived from the clock, never from a previous heartbeat."""
    old = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FakeBoard([_item("I1", "In Progress", updated=old)]).install(monkeypatch)
    await reconciler.run_tick(FakeRT(db), now=NOW)
    ev = (await board_crud.list_events(db, event="drag"))["items"][0]
    assert ev["detail"]["observed_late"] is True


async def test_a_new_drag_of_the_same_card_is_a_new_event(db, monkeypatch):
    t1 = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    t2 = (NOW + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    board = FakeBoard([_item("I1", "In Progress", updated=t1)]).install(monkeypatch)
    await reconciler.run_tick(FakeRT(db), now=NOW)
    board.items = [_item("I1", "In Progress", updated=t2)]
    await reconciler.run_tick(FakeRT(db), now=NOW + timedelta(minutes=5))
    assert (await board_crud.list_events(db, event="drag"))["total"] == 2


async def test_a_drag_log_failure_keeps_the_good_read_and_records_a_failure(db, monkeypatch):
    """A LOCAL failure after both GitHub reads succeeded must not discard them."""
    fresh = (NOW - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    FakeBoard([_item("I1", "In Progress", updated=fresh)]).install(monkeypatch)

    async def locked(*_a, **_k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(board_crud, "append_event", locked)
    rt = FakeRT(db)
    out = await reconciler.run_tick(rt, now=NOW)
    d = _pulse(rt)
    assert out["board_state"] == "ok" and d["items_total"] == 1
    assert d["by_status"] == {"In Progress": 1}
    assert "database is locked" in d["drags"]["error"]
    assert rt.successes == [] and rt.failures[0][0] == reconciler.JOB_ID
    assert "drag log failed" in str(rt.failures[0][1])


async def test_a_short_read_publishes_an_error_and_no_counts(db, monkeypatch):
    FakeBoard([], fail=pv.ProjectsError("read 99 items but the project reports 100")).install(
        monkeypatch
    )
    rt = FakeRT(db)
    out = await reconciler.run_tick(rt, now=NOW)
    d = _pulse(rt)
    assert out["board_state"] == "error"
    assert "read 99 items" in d["last_error"]
    assert "by_status" not in d and "coverage" not in d and "items_total" not in d
    assert rt.successes == [] and rt.failures[0][0] == reconciler.JOB_ID


async def test_a_closed_project_publishes_an_error_and_no_counts(db, monkeypatch):
    """Setup refuses a closed project; one closed after setup is no longer the
    work board, so its items are not read or published as the board's state."""
    board = FakeBoard([_item("I1", "Ready")], closed=True).install(monkeypatch)
    rt = FakeRT(db)
    out = await reconciler.run_tick(rt, now=NOW)
    d = _pulse(rt)
    assert out["board_state"] == "error" and "is closed" in d["last_error"]
    assert "by_status" not in d and "coverage" not in d
    assert board.calls == 1, "nothing past the project read"


async def test_a_long_error_is_bounded_and_says_so(db, monkeypatch):
    FakeBoard([], fail=pv.ProjectsError("x" * 5000)).install(monkeypatch)
    rt = FakeRT(db)
    await reconciler.run_tick(rt, now=NOW)
    err = _pulse(rt)["last_error"]
    assert len(err) < 700 and "omitted" in err


async def test_without_a_tracker_coverage_is_unavailable_not_zero(db, monkeypatch):
    monkeypatch.setattr(board_config, "tracker_repo", lambda: None)
    FakeBoard([_item("I1", "Proposed")]).install(monkeypatch)
    rt = FakeRT(db)
    await reconciler.run_tick(rt, now=NOW)
    cov = _pulse(rt)["coverage"]
    assert cov.get("on_board") is None and "unavailable" in cov


def test_the_reconciler_module_calls_no_github_write():
    """PR3a is read-only: no projects_v2 mutation is referenced anywhere in it."""
    import inspect

    # Derived, so a mutation helper added later is covered without a list edit.
    writes = {
        name
        for name, fn in inspect.getmembers(pv, inspect.iscoroutinefunction)
        if fn.__module__ == pv.__name__ and "mutation" in inspect.getsource(fn)
    }
    assert "set_single_select" in writes and "add_item" in writes
    src = Path(reconciler.__file__).read_text()
    names = {n.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Attribute)}
    names |= {n.id for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Name)}
    assert not (names & writes)
    assert "gh issue" not in src and "mutation" not in src
