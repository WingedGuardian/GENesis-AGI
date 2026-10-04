"""board_status (stored heartbeats only, never GitHub) and board_item (one card
read live, plus the local blocks and promotion link)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.board import config as board_config
from genesis.board import projects_v2 as pv
from genesis.db.crud import board as board_crud
from genesis.db.crud import events as events_crud
from genesis.db.schema import create_all_tables
from genesis.mcp.health import board_tools as bt
from genesis.mcp.health import manifest

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(manifest, "_read_global_paused", lambda: False)
    monkeypatch.setattr(board_config, "project_ref", lambda: ("owner", 2))
    monkeypatch.setattr(board_config, "tracker_repo", lambda: ("owner", "repo"))
    monkeypatch.setattr(board_config, "effective_mode", lambda: "propose_only")
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _pulse(db, minutes_ago, **details):
    await events_crud.insert(
        db,
        subsystem="board",
        severity="DEBUG",
        event_type="heartbeat",
        message="board",
        details=details,
        timestamp=(NOW - timedelta(minutes=minutes_ago)).isoformat(),
    )
    await db.commit()


async def test_no_heartbeat_yet_says_so(db):
    out = await bt._impl_board_status(db, now=NOW)
    assert out["status"] == "ok" and out["latest"] is None and out["summary"] is None
    assert "has not run" in out["note"]


async def test_latest_pulse_and_newest_successful_summary_are_both_reported(db):
    await _pulse(
        db,
        12,
        board_state="ok",
        mode="propose_only",
        items_total=3,
        by_status={"Proposed": 3},
        coverage={"on_board": 3, "open_in_repo": 5},
    )
    await _pulse(db, 2, board_state="error", mode="propose_only", last_error="ProjectsError: x")
    out = await bt._impl_board_status(db, now=NOW)
    assert out["latest"]["board_state"] == "error" and out["latest"]["last_error"]
    assert out["summary"]["items_total"] == 3
    assert out["summary"]["coverage"] == {"on_board": 3, "open_in_repo": 5}
    assert out["summary"]["age_seconds"] == 12 * 60
    assert out["pulses_read"] == 2 and out["scan_limit"] == manifest._HEARTBEAT_SCAN_LIMIT


async def test_pulses_without_a_read_give_no_summary_and_say_why(db):
    await _pulse(db, 5, board_state="off", mode="off")
    await _pulse(db, 0, board_state="off", mode="off")
    out = await bt._impl_board_status(db, now=NOW)
    assert out["latest"]["board_state"] == "off" and out["summary"] is None
    assert "no successful board read in the last 2 pulses" in out["note"]


async def test_a_future_dated_pulse_is_never_taken_as_the_latest(db):
    await _pulse(db, 3, board_state="ok", items_total=1)
    await _pulse(db, -600, board_state="error", last_error="bogus")  # ten hours ahead
    out = await bt._impl_board_status(db, now=NOW)
    assert out["latest"]["board_state"] == "ok"


@pytest.fixture
def card(monkeypatch):
    seen = {}

    async def card_for_issue(owner, name, number, *, runner=None):
        seen["args"] = (owner, name, number)
        return {
            "kind": "Issue",
            "number": number,
            "state": "OPEN",
            "cards": [
                {"project_owner": "someone", "project_number": 2, "status": "Done"},
                {"project_owner": "Owner", "project_number": 2, "status": "Ready"},
            ],
            "cards_truncated": False,
            "blocked_by": [{"number": 3, "state": "OPEN", "repo": "owner/repo"}],
            "blocked_by_total": 21,
            "blockers_truncated": True,
        }

    monkeypatch.setattr(pv, "card_for_issue", card_for_issue)
    return seen


@pytest.mark.parametrize("target", ["owner/repo#7", "#7", "7", "card:owner/repo#7"])
async def test_board_item_target_forms(db, card, target):
    out = await bt._impl_board_item(db, target)
    assert out["status"] == "ok" and card["args"] == ("owner", "repo", 7)


async def test_board_item_matches_the_configured_project_by_owner_and_number(db, card):
    out = await bt._impl_board_item(db, "#7")
    assert out["board_card"]["status"] == "Ready", "another owner's project #2 is not the board"
    assert out["blockers_truncated"] is True and out["blocked_by_total"] == 21


async def test_board_item_reports_open_questions_blocking_the_card(db, card):
    qid = await board_crud.raise_question(
        db, question="ship it?", now=NOW.isoformat(), blocks=[("card", "owner/repo#7")]
    )
    out = await bt._impl_board_item(db, "#7")
    assert [q["id"] for q in out["blocking_questions"]] == [qid]


@pytest.mark.parametrize("target", ["", "repo#7", "owner/repo#x", "#-1", "owner/repo#7; rm", "owner/repo7", "owner/repo#0", "0"])
async def test_board_item_refuses_a_malformed_target(db, card, target):
    out = await bt._impl_board_item(db, target)
    assert out["status"] == "error" and "args" not in card


async def test_board_item_turns_a_github_error_into_an_answer(db, monkeypatch):
    async def boom(*_a, **_k):
        raise pv.ProjectsError("no issue or PR #9 in owner/repo")

    monkeypatch.setattr(pv, "card_for_issue", boom)
    out = await bt._impl_board_item(db, "#9")
    assert out == {"status": "error", "reason": "no issue or PR #9 in owner/repo"}


async def test_board_item_reads_nothing_while_the_board_is_off(db, card, monkeypatch):
    """Mode off (the kill switch included) means the board does nothing."""
    monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    out = await bt._impl_board_item(db, "#7")
    assert out["status"] == "unavailable" and "args" not in card


def test_neither_board_tool_calls_a_github_write():
    """The tools are reads: no projects_v2 function whose body is a GraphQL
    mutation is referenced in either implementation."""
    import ast
    import inspect

    writes = {
        name
        for name, fn in inspect.getmembers(pv, inspect.iscoroutinefunction)
        if fn.__module__ == pv.__name__ and "mutation" in inspect.getsource(fn)
    }
    assert writes, "the write set must be derived, never empty"
    for impl in (bt._impl_board_status, bt._impl_board_item):
        tree = ast.parse(inspect.getsource(impl).lstrip())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert not (names & writes), impl.__name__
