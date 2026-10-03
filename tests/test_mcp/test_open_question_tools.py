"""open_question_* MCP tools: target parsing and id-prefix resolution, the
validate-everything-before-writing rule, guarded closes, and the blocked-by
read that promotion will use."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import aiosqlite
import pytest

from genesis.mcp.health import open_question_tools as oq

MIG = importlib.import_module("genesis.db.migrations.20261003010926_board_stores")
NOW = "2026-10-03T12:00:00+00:00"
LEDGER = "1234abcd" + "0" * 24
FOLLOW = "9876fedc" + "1" * 24
FOLLOW_TWIN = "9876fedc" + "2" * 24  # shares an 8-hex prefix with FOLLOW


@pytest.fixture(autouse=True)
def _attended_session(monkeypatch):
    """Run as an owner-attended session whatever the developer's shell says
    (a dispatched session's markers would refuse every owner-authority call).
    Tests of the refusal set the markers themselves."""
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    # raise writes on a connection of its own, opened from genesis_db_path():
    # point that at this test's file so the write and these reads see one DB.
    monkeypatch.setattr("genesis.env.genesis_db_path", lambda: tmp_path / "genesis.db")
    async with aiosqlite.connect(str(tmp_path / "genesis.db")) as conn:
        await MIG.up(conn)
        # The tools only ever read these tables' ids — minimal stand-ins keep the
        # test independent of their unrelated columns.
        await conn.execute("CREATE TABLE session_ledger (id TEXT PRIMARY KEY)")
        await conn.execute("CREATE TABLE follow_ups (id TEXT PRIMARY KEY)")
        await conn.execute("INSERT INTO session_ledger VALUES (?)", (LEDGER,))
        await conn.executemany("INSERT INTO follow_ups VALUES (?)", [(FOLLOW,), (FOLLOW_TWIN,)])
        await conn.commit()
        yield conn


async def _count(db, table):
    cur = await db.execute(f"SELECT COUNT(*) FROM {table}")
    return (await cur.fetchone())[0]


async def test_raise_with_blocks_resolves_prefixes(db):
    out = await oq._impl_open_question_raise(
        db,
        question="Bind loopback or tailnet?",
        context="",
        blocks=["ledger:1234abcd", f"follow_up:{FOLLOW}", "card:owner/repo#12"],
        raised_by="session-x",
        now=NOW,
    )
    assert out["status"] == "ok"
    targets = {(b["target_kind"], b["target_id"]) for b in out["question"]["blocks"]}
    assert targets == {("ledger", LEDGER), ("follow_up", FOLLOW), ("card", "owner/repo#12")}


async def _raise(db, **kw):
    args = dict(question="q", context="", blocks=[], raised_by="", now=NOW)
    args.update(kw)
    return await oq._impl_open_question_raise(db, **args)


async def _shared_writes(db, call):
    """Run ``call()`` and return every write or commit it sent through the
    shared connection ``db`` (reads are fine there; writes are not)."""
    real_execute, real_commit = db.execute, db.commit
    seen: list[str] = []

    def recording(sql, *args):
        seen.append(sql)
        return real_execute(sql, *args)

    async def recording_commit():
        seen.append("COMMIT")
        await real_commit()

    db.execute, db.commit = recording, recording_commit
    try:
        out = await call()
    finally:
        db.execute, db.commit = real_execute, real_commit
    return out, [s for s in seen if s == "COMMIT" or not s.lstrip().upper().startswith("SELECT")]


@pytest.mark.parametrize("op", ["raise", "resolve", "block_add", "block_remove"])
async def test_no_write_goes_through_the_shared_connection(db, op):
    """Every open-question write runs on a connection the tool owns: the shared
    one only answers read-only validation, so another tool's commit or rollback
    on it (the middleware rolls it back after any failed call) can neither split
    a raise from its blocks nor discard a write already reported saved."""
    qid = (await _raise(db))["question"]["id"]
    calls = {
        "raise": lambda: _raise(db, blocks=[f"follow_up:{FOLLOW}"]),
        "resolve": lambda: oq._impl_open_question_resolve(
            db, question_id=qid, resolution="settled", status="resolved", now=NOW
        ),
        "block_add": lambda: oq._impl_open_question_block(
            db, question_id=qid, target=f"ledger:{LEDGER}", remove=False, now=NOW
        ),
        "block_remove": lambda: oq._impl_open_question_block(
            db, question_id=qid, target=f"ledger:{LEDGER}", remove=True, now=NOW
        ),
    }
    if op == "block_remove":
        await calls["block_add"]()
    out, writes = await _shared_writes(db, calls[op])
    assert out["status"] == "ok", out
    assert writes == [], f"the shared connection carried writes: {writes}"


async def test_a_failed_read_back_never_repeats_or_hides_the_write(db, monkeypatch):
    """The read-back runs after the commit and is never retried: a failing read
    reports the question saved-but-unread, and it is saved exactly once."""
    import sqlite3

    from genesis.db.crud import board as board_crud

    async def locked_read(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(board_crud, "get_question", locked_read)
    out = await _raise(db, blocks=[f"follow_up:{FOLLOW}"])
    assert out["status"] == "ok" and out["question_id"] and "reading it back failed" in out["note"]
    assert await _count(db, "open_questions") == 1
    assert await _count(db, "open_question_blocks") == 1


async def test_a_lost_write_lock_is_reported_as_nothing_changed(db, monkeypatch):
    from genesis.db.crud import board as board_crud

    async def busy(*_a, **_k):
        raise board_crud.WriteBusy("database is locked")

    monkeypatch.setattr(board_crud, "raise_question", busy)
    out = await _raise(db)
    assert out["status"] == "error" and "nothing was changed" in out["message"]


@pytest.mark.parametrize(
    "bad",
    [
        "follow_up:9876fedc",  # ambiguous prefix
        "ledger:deadbeef",  # unknown id
        "ledger:12",  # too short to resolve safely
        "card:owner/repo",  # malformed card
        "issue:owner/repo#1",  # unknown kind
        "owner/repo#1",  # missing kind
        "ledger:1234abcd' OR 1=1 --",  # never reaches SQL: not hex
    ],
)
async def test_any_bad_target_writes_nothing(db, bad):
    """Validate EVERY target before writing: a half-recorded question whose
    blocks silently dropped one target would under-block promotion."""
    out = await oq._impl_open_question_raise(
        db, question="q", context="", blocks=["card:owner/repo#1", bad], raised_by="", now=NOW
    )
    assert out["status"] == "error"
    assert await _count(db, "open_questions") == 0
    assert await _count(db, "open_question_blocks") == 0


async def test_oversized_blocks_list_is_refused_before_any_lookup(db):
    from genesis.db.crud import board

    # Unknown ledger ids: if the TOOL's cap were missing, the first per-target
    # lookup would fail ("no ledger with id …") before the CRUD cap is reached —
    # so a "limit" message proves the refusal came before any lookup.
    out = await oq._impl_open_question_raise(
        db,
        question="q",
        context="",
        blocks=[f"ledger:{i:08x}" for i in range(board.MAX_BLOCKS + 1)],
        raised_by="",
        now=NOW,
    )
    assert out["status"] == "error" and "limit" in out["message"], out
    assert await _count(db, "open_questions") == 0


async def test_list_by_target_reports_blocked_then_released(db):
    raised = await oq._impl_open_question_raise(
        db, question="q", context="", blocks=[f"follow_up:{FOLLOW}"], raised_by="", now=NOW
    )
    qid = raised["question"]["id"]
    blocked = await oq._impl_open_question_list(
        db, status="", target=f"follow_up:{FOLLOW[:10]}", limit=None
    )
    assert blocked["blocked"] is True and [b["id"] for b in blocked["blocking_questions"]] == [qid]

    done = await oq._impl_open_question_resolve(
        db, question_id=qid[:8], resolution="answered", status="resolved", now=NOW
    )
    assert done["status"] == "ok"
    released = await oq._impl_open_question_list(
        db, status="", target=f"follow_up:{FOLLOW}", limit=None
    )
    assert released["blocked"] is False


async def test_second_resolve_is_an_error_and_keeps_the_first_answer(db):
    qid = (
        await oq._impl_open_question_raise(
            db, question="q", context="", blocks=[], raised_by="", now=NOW
        )
    )["question"]["id"]
    assert (
        await oq._impl_open_question_resolve(
            db, question_id=qid, resolution="a", status="dropped", now=NOW
        )
    )["status"] == "ok"
    again = await oq._impl_open_question_resolve(
        db, question_id=qid, resolution="b", status="resolved", now=NOW
    )
    assert again["status"] == "error" and "already dropped" in again["message"]


async def test_block_add_and_remove(db):
    qid = (
        await oq._impl_open_question_raise(
            db, question="q", context="", blocks=[], raised_by="", now=NOW
        )
    )["question"]["id"]
    added = await oq._impl_open_question_block(
        db, question_id=qid, target="ledger:1234abcd", remove=False, now=NOW
    )
    assert added["status"] == "ok" and added["changed"] is True
    removed = await oq._impl_open_question_block(
        db, question_id=qid, target="ledger:1234abcd", remove=True, now=NOW
    )
    assert removed["changed"] is True and removed["question"]["blocks"] == []


async def test_list_pages_with_total(db):
    for i in range(3):
        await oq._impl_open_question_raise(
            db, question=f"q{i}", context="", blocks=[], raised_by="", now=NOW
        )
    page = await oq._impl_open_question_list(db, status="unverified", target="", limit=2)
    assert page["listed"] == 2 and page["total"] == 3 and page["next_offset"] == 2
    rest = await oq._impl_open_question_list(db, status="unverified", target="", limit=2, offset=2)
    assert rest["listed"] == 1 and rest["next_offset"] is None


async def test_list_default_page_is_bounded_and_the_cap_enforced(db):
    for i in range(oq.DEFAULT_PAGE + 3):
        await oq._impl_open_question_raise(
            db, question=f"q{i}", context="", blocks=[], raised_by="", now=NOW
        )
    page = await oq._impl_open_question_list(db, status="unverified", target="", limit=None)
    assert page["listed"] == oq.DEFAULT_PAGE and page["total"] == oq.DEFAULT_PAGE + 3
    assert page["next_offset"] == oq.DEFAULT_PAGE
    too_big = await oq._impl_open_question_list(
        db, status="unverified", target="", limit=oq.MAX_PAGE + 1
    )
    assert too_big["status"] == "error"


async def test_dispatched_session_cannot_resolve_or_unblock(db, monkeypatch):
    raised = await oq._impl_open_question_raise(
        db, question="q", context="", blocks=[f"follow_up:{FOLLOW}"], raised_by="", now=NOW
    )
    qid = raised["question"]["id"]
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)
    res = await oq._impl_open_question_resolve(
        db, question_id=qid, resolution="x", status="resolved", now=NOW
    )
    assert res["status"] == "error" and "owner's call" in res["message"]
    unb = await oq._impl_open_question_block(
        db, question_id=qid, target=f"follow_up:{FOLLOW}", remove=True, now=NOW
    )
    assert unb["status"] == "error" and "owner's call" in unb["message"]
    # raising (parking a fork) stays allowed for a dispatched session
    again = await oq._impl_open_question_raise(
        db, question="q2", context="", blocks=[], raised_by="", now=NOW
    )
    assert again["status"] == "ok"


async def test_removing_an_edge_of_a_closed_question_is_an_error(db):
    raised = await oq._impl_open_question_raise(
        db, question="q", context="", blocks=["ledger:1234abcd"], raised_by="", now=NOW
    )
    qid = raised["question"]["id"]
    await oq._impl_open_question_resolve(
        db, question_id=qid, resolution="done", status="resolved", now=NOW
    )
    out = await oq._impl_open_question_block(
        db, question_id=qid, target="ledger:1234abcd", remove=True, now=NOW
    )
    assert out["status"] == "error" and "history" in out["message"]


async def test_unavailable_before_migration_and_without_db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "bare.db")) as bare:
        out = await oq._impl_open_question_list(bare, status="unverified", target="", limit=None)
    assert out["status"] == "unavailable" and "not migrated" in out["message"]
    out = await oq._impl_open_question_raise(
        None, question="q", context="", blocks=[], raised_by="", now=NOW
    )
    assert out["status"] == "unavailable"


def test_module_never_touches_github():
    """Open questions are LOCAL ONLY. No GitHub client, no subprocess, no gh."""
    src = Path(oq.__file__).read_text()
    tree = ast.parse(src)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert not imported & {"subprocess", "requests", "httpx", "aiohttp"}
    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert not calls & {"create_subprocess_exec", "run", "Popen"}


def test_read_tool_is_on_the_reflection_read_allowlist_and_writers_are_not():
    from genesis.cc.session_config import _REFLECTION_READ_MCP

    assert "open_question_list" in _REFLECTION_READ_MCP
    assert (
        not {"open_question_raise", "open_question_resolve", "open_question_block"}
        & _REFLECTION_READ_MCP
    )
