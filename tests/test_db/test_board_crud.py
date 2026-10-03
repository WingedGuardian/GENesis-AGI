"""Work-board stores: schema parity across both build paths, and every CRUD
contract the board's later consumers (promotion, reconciler, the
open-question tools) lean on."""

from __future__ import annotations

import asyncio
import importlib
import re

import aiosqlite
import pytest

from genesis.db.crud import board
from genesis.db.schema._tables import INDEXES, TABLES

MIG = importlib.import_module("genesis.db.migrations.20261003010926_board_stores")

NOW = "2026-10-03T12:00:00+00:00"
LATER = "2026-10-03T13:00:00+00:00"
REPO = "owner/repo"
SRC = "a" * 32
SRC2 = "b" * 32
SHA = "c" * 64


@pytest.fixture(autouse=True)
def _attended_session(monkeypatch):
    """Run as an owner-attended session whatever the developer's shell says: a
    test run from inside a dispatched session would otherwise inherit its
    markers and have every owner-authority call refused. Tests of the refusal
    set the markers themselves."""
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "genesis.db")) as conn:
        await MIG.up(conn)
        await conn.commit()
        yield conn


def _normalize(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


async def _schema(conn) -> dict:
    cur = await conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE tbl_name IN "
        "('board_links','open_questions','open_question_blocks','board_events') "
        "AND sql IS NOT NULL ORDER BY name"
    )
    return {name: _normalize(sql) for name, sql in await cur.fetchall()}


async def test_fresh_install_ddl_and_migration_are_the_same_schema(tmp_path):
    """schema/_tables.py builds fresh installs; the migration builds existing
    ones. Compare what SQLite itself recorded — tables AND indexes."""
    async with aiosqlite.connect(str(tmp_path / "fresh.db")) as fresh:
        for name in board.TABLES:
            await fresh.execute(TABLES[name])
        for ddl in INDEXES:
            if any(f" {t}(" in ddl or f" {t} (" in ddl for t in board.TABLES):
                await fresh.execute(ddl)
        fresh_schema = await _schema(fresh)
    async with aiosqlite.connect(str(tmp_path / "migrated.db")) as migrated:
        await MIG.up(migrated)
        migrated_schema = await _schema(migrated)
    assert fresh_schema == migrated_schema
    assert set(board.TABLES) <= set(fresh_schema)
    assert "idx_board_events_change" in fresh_schema, "the observed-change dedup is load-bearing"


async def test_migration_is_idempotent(db):
    await MIG.up(db)
    await MIG.up(db)
    assert await board.tables_available(db)


async def test_tables_available_false_before_migration(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "empty.db")) as conn:
        assert await board.tables_available(conn) is False


# ── board_links ─────────────────────────────────────────────────────────────


async def _link(db, **over):
    kw = dict(
        source_kind="follow_up",
        source_id=SRC,
        repo=REPO,
        issue_number=7,
        promoted_by="telegram:owner",
        scan_receipt={"ok": True, "scanners_run": ["portability"]},
        body_sha256=SHA,
        now=NOW,
    )
    kw.update(over)
    return await board.record_link(db, **kw)


async def test_record_link_round_trips(db):
    row = await _link(db, approval_id="appr1")
    assert row["issue_number"] == 7
    assert row["adopted"] is False
    assert row["scan_receipt"] == {"ok": True, "scanners_run": ["portability"]}
    assert (await board.get_link_by_issue(db, repo=REPO, issue_number=7))["source_id"] == SRC
    assert await board.count_links(db) == 1


async def test_record_link_retry_is_idempotent(db):
    first = await _link(db)
    again = await _link(db, now=LATER)
    assert again["id"] == first["id"]
    assert await board.count_links(db) == 1


async def test_same_source_to_a_different_issue_raises(db):
    """One private record maps to ONE issue: silently keeping the first would
    hide a duplicate issue on GitHub."""
    await _link(db)
    with pytest.raises(ValueError, match="conflicting board link"):
        await _link(db, issue_number=8)


async def test_same_issue_from_a_different_source_raises(db):
    await _link(db)
    with pytest.raises(ValueError, match="conflicting board link"):
        await _link(db, source_id=SRC2)


@pytest.mark.parametrize(
    "over",
    [
        {"source_kind": "card"},
        {"source_id": "abc"},
        {"repo": "not a repo"},
        {"issue_number": 0},
        {"issue_number": True},
        {"body_sha256": "x" * 64},
        {"promoted_by": ""},
    ],
)
async def test_record_link_refuses_malformed_input(db, over):
    with pytest.raises(ValueError):
        await _link(db, **over)
    assert await board.count_links(db) == 0


async def _raw_link(db, *, source_id, issue_number, repo=REPO):
    """Insert a pointer directly — what a concurrent writer's row looks like."""
    await db.execute(
        "INSERT INTO board_links (id, source_kind, source_id, repo, issue_number, adopted, "
        "promoted_by, scan_receipt, body_sha256, created_at, updated_at) "
        "VALUES (?, 'follow_up', ?, ?, ?, 0, 'racer', '{}', ?, ?, ?)",
        ("racer-" + source_id[:6], source_id, repo, issue_number, SHA, NOW, NOW),
    )
    await db.commit()


async def test_concurrent_insert_of_the_same_pair_returns_that_row(db):
    """The race the docstring promises to survive: another writer inserted the
    same (source, issue) between our check and our write."""
    await _raw_link(db, source_id=SRC, issue_number=7)
    row = await _link(db)
    assert row["promoted_by"] == "racer"
    assert await board.count_links(db) == 1


async def test_concurrent_conflicting_insert_raises(db):
    await _raw_link(db, source_id=SRC, issue_number=8)
    with pytest.raises(ValueError, match="conflicting board link"):
        await _link(db)
    assert await board.count_links(db) == 1


async def test_repo_case_variants_are_one_pointer(db):
    """GitHub owner/name is case-insensitive: a case variant must not escape
    the one-issue-one-pointer rule."""
    first = await _link(db, repo="Owner/Repo")
    assert first["repo"] == "owner/repo"
    again = await _link(db, repo="OWNER/REPO")
    assert again["id"] == first["id"]
    assert (await board.get_link_by_issue(db, repo="OwNeR/rEpO", issue_number=7))["id"] == first[
        "id"
    ]
    with pytest.raises(ValueError, match="conflicting"):
        await _link(db, source_id=SRC2, repo="owner/REPO")


async def test_set_project_item(db):
    row = await _link(db)
    assert await board.set_project_item(db, link_id=row["id"], project_item_id="PVTI_x", now=LATER)
    assert (await board.get_link_by_source(db, source_kind="follow_up", source_id=SRC))[
        "project_item_id"
    ] == "PVTI_x"


# ── open questions ──────────────────────────────────────────────────────────


async def test_question_lifecycle_and_blocks(db):
    qid = await board.raise_question(
        db, question="Which bind?", context="ctx", raised_by="s1", now=NOW
    )
    assert await board.add_block(
        db, question_id=qid, target_kind="follow_up", target_id=SRC, now=NOW
    )
    assert not await board.add_block(
        db, question_id=qid, target_kind="follow_up", target_id=SRC, now=NOW
    )
    blocking = await board.blocking_questions(db, target_kind="follow_up", target_id=SRC)
    assert [b["id"] for b in blocking] == [qid]

    assert await board.close_question(
        db, question_id=qid, status="resolved", resolution="loopback", now=LATER
    )
    assert await board.blocking_questions(db, target_kind="follow_up", target_id=SRC) == []
    q = await board.get_question(db, qid)
    assert q["status"] == "resolved" and q["closed_at"] == LATER
    assert q["blocks"] == [{"target_kind": "follow_up", "target_id": SRC, "created_at": NOW}], (
        "closing releases the block but keeps the edge as history"
    )


async def test_close_is_guarded_on_status(db):
    """A second close is a no-op, never a clobber of the first answer."""
    qid = await board.raise_question(db, question="q", now=NOW)
    assert await board.close_question(
        db, question_id=qid, status="dropped", resolution="moot", now=NOW
    )
    assert not await board.close_question(
        db, question_id=qid, status="resolved", resolution="other", now=LATER
    )
    assert (await board.get_question(db, qid))["resolution"] == "moot"


async def test_close_requires_a_resolution_and_a_terminal_status(db):
    qid = await board.raise_question(db, question="q", now=NOW)
    with pytest.raises(ValueError):
        await board.close_question(db, question_id=qid, status="resolved", resolution="  ", now=NOW)
    with pytest.raises(ValueError):
        await board.close_question(
            db, question_id=qid, status="unverified", resolution="x", now=NOW
        )


async def test_closed_or_unknown_question_cannot_block(db):
    qid = await board.raise_question(db, question="q", now=NOW)
    await board.close_question(db, question_id=qid, status="resolved", resolution="done", now=NOW)
    with pytest.raises(ValueError, match="only an unverified question"):
        await board.add_block(
            db, question_id=qid, target_kind="card", target_id="owner/repo#3", now=NOW
        )
    with pytest.raises(ValueError, match="unknown question"):
        await board.add_block(
            db, question_id="f" * 32, target_kind="card", target_id="owner/repo#3", now=NOW
        )


@pytest.mark.parametrize(
    "kind,target",
    [
        ("card", "owner/repo"),
        ("card", "owner/repo#0"),
        ("ledger", "abc"),
        ("follow_up", "A" * 32),
        ("issue", SRC),
    ],
)
def test_validate_target_refuses_malformed(kind, target):
    with pytest.raises(ValueError):
        board.validate_target(kind, target)


async def test_raise_with_blocks_is_all_or_nothing(db):
    qid = await board.raise_question(
        db, question="q", now=NOW, blocks=[("follow_up", SRC), ("card", "Owner/Repo#5")]
    )
    edges = {
        (b["target_kind"], b["target_id"]) for b in (await board.get_question(db, qid))["blocks"]
    }
    assert edges == {("follow_up", SRC), ("card", "owner/repo#5")}

    with pytest.raises(ValueError):
        await board.raise_question(
            db, question="bad", now=NOW, blocks=[("follow_up", SRC2), ("card", "nope")]
        )
    assert (await board.list_questions(db, status=None))["total"] == 1, (
        "nothing of the bad raise landed"
    )


@pytest.mark.parametrize("exc", [aiosqlite.OperationalError("locked"), asyncio.CancelledError()])
async def test_a_raise_interrupted_mid_write_leaves_nothing_and_no_open_transaction(db, exc):
    """Interrupt the raise AFTER its question row and first edge are written but
    before the second edge (an error, or a cancellation — CancelledError is a
    BaseException and skips `except Exception`). The transaction is rolled
    back: no question, no edge, and the connection is not left holding the
    write lock."""
    real_execute = db.execute
    edges = {"n": 0}

    async def flaky(sql, *args):
        if sql.startswith("INSERT INTO open_question_blocks"):
            edges["n"] += 1
            if edges["n"] == 2:
                raise exc
        return await real_execute(sql, *args)

    db.execute = flaky
    try:
        with pytest.raises(type(exc)):
            await board.raise_question(
                db, question="q", now=NOW, blocks=[("follow_up", SRC), ("follow_up", SRC2)]
            )
    finally:
        db.execute = real_execute
    assert edges["n"] == 2, "the failure landed mid-write, after a written edge"
    assert not db.in_transaction, "rolled back, not left open"
    await db.commit()  # whatever a later commit on this connection would persist
    assert (await board.list_questions(db, status=None))["total"] == 0
    cur = await db.execute("SELECT COUNT(*) FROM open_question_blocks")
    assert (await cur.fetchone())[0] == 0


async def test_raise_refuses_the_shared_serialized_connection(db):
    """On the shared connection another call's commit or rollback can land
    between the inserts; a raise must be handed a connection its caller owns."""
    from genesis.db.connection import SerializedConnection

    with pytest.raises(TypeError, match="owns"):
        await board.raise_question(SerializedConnection(db), question="q", now=NOW)
    assert (await board.list_questions(db, status=None))["total"] == 0


async def test_a_closed_questions_edges_are_history_and_cannot_be_removed(db):
    qid = await board.raise_question(db, question="q", now=NOW, blocks=[("follow_up", SRC)])
    await board.close_question(db, question_id=qid, status="resolved", resolution="r", now=NOW)
    assert (
        await board.remove_block(db, question_id=qid, target_kind="follow_up", target_id=SRC)
        is False
    )
    assert (await board.get_question(db, qid))["blocks"] != []


@pytest.mark.parametrize(
    "act",
    [
        lambda db, qid: board.close_question(
            db, question_id=qid, status="resolved", resolution="r", now=NOW
        ),
        lambda db, qid: board.remove_block(
            db, question_id=qid, target_kind="follow_up", target_id=SRC
        ),
    ],
    ids=["resolve", "unblock"],
)
async def test_owner_authority_is_refused_for_a_dispatched_session(db, monkeypatch, act):
    from genesis.security.immunity_shadow import DispatchGateRefused

    qid = await board.raise_question(db, question="q", now=NOW, blocks=[("follow_up", SRC)])
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.delenv("GENESIS_SESSION_SUPERVISED", raising=False)
    with pytest.raises(DispatchGateRefused):
        await act(db, qid)
    q = await board.get_question(db, qid)
    assert q["status"] == "unverified" and q["blocks"] != []
    monkeypatch.setenv("GENESIS_SESSION_SUPERVISED", "1")  # owner-attended: allowed
    await act(db, qid)


async def test_list_questions_pages_with_offset_and_batches_blocks(db):
    ids = []
    for i in range(5):
        ids.append(
            await board.raise_question(
                db,
                question=f"q{i}",
                now=f"2026-10-03T12:0{i}:00+00:00",
                blocks=[("card", f"o/r#{i + 1}")],
            )
        )
    first = await board.list_questions(db, limit=2)
    second = await board.list_questions(db, limit=2, offset=2)
    tail = await board.list_questions(db, offset=4)
    assert [q["question"] for q in first["items"]] == ["q4", "q3"]
    assert [q["question"] for q in second["items"]] == ["q2", "q1"]
    assert [q["question"] for q in tail["items"]] == ["q0"] and tail["total"] == 5
    assert all(len(q["blocks"]) == 1 for q in first["items"] + second["items"] + tail["items"])
    assert first["items"][0]["blocks"][0]["target_id"] == "o/r#5", (
        "each page's blocks belong to its own rows"
    )
    with pytest.raises(ValueError):
        await board.list_questions(db, offset=-1)


async def test_an_unpaged_list_fetches_blocks_in_bounded_chunks(db, monkeypatch):
    """The block fetch is chunked, so an unpaged read never outgrows SQLite's
    bound-variable limit; every row still gets exactly its own blocks."""
    monkeypatch.setattr(board, "_IN_CHUNK", 2)
    for i in range(5):
        await board.raise_question(
            db,
            question=f"q{i}",
            now=f"2026-10-03T12:0{i}:00+00:00",
            blocks=[("card", f"o/r#{i + 1}")],
        )
    listing = await board.list_questions(db, limit=None)
    assert listing["listed"] == 5
    assert {q["question"]: [b["target_id"] for b in q["blocks"]] for q in listing["items"]} == {
        f"q{i}": [f"o/r#{i + 1}"] for i in range(5)
    }


async def test_raise_refuses_a_connection_with_a_transaction_open(db):
    """A raise owns its transaction; it never folds in (or commits) a caller's
    pending work."""
    await db.execute("INSERT INTO board_events (event, created_at) VALUES ('override', ?)", (NOW,))
    with pytest.raises(ValueError, match="idle connection"):
        await board.raise_question(db, question="q", now=NOW)
    await db.rollback()
    assert (await board.list_events(db))["total"] == 0, (
        "the caller's pending write was not committed"
    )
    assert (await board.list_questions(db, status=None))["total"] == 0


async def test_card_targets_are_case_insensitive(db):
    qid = await board.raise_question(db, question="q", now=NOW)
    assert await board.add_block(
        db, question_id=qid, target_kind="card", target_id="Owner/Repo#5", now=NOW
    )
    assert not await board.add_block(
        db, question_id=qid, target_kind="card", target_id="owner/REPO#5", now=NOW
    )
    assert [
        q["id"]
        for q in await board.blocking_questions(db, target_kind="card", target_id="OWNER/repo#5")
    ] == [qid]
    assert await board.remove_block(
        db, question_id=qid, target_kind="card", target_id="OWNER/REPO#5"
    )


async def test_add_block_insert_itself_refuses_a_question_closed_after_the_check(db, monkeypatch):
    """Close lands between add_block's status check and its write: the insert's
    own EXISTS guard must keep the edge off the closed question."""
    qid = await board.raise_question(db, question="q", now=NOW)
    stale = await board.get_question(db, qid)  # observed while unverified
    await board.close_question(db, question_id=qid, status="dropped", resolution="moot", now=NOW)

    async def stale_get(_db, _qid):
        return stale

    monkeypatch.setattr(board, "get_question", stale_get)
    assert (
        await board.add_block(db, question_id=qid, target_kind="follow_up", target_id=SRC, now=NOW)
        is False
    )
    cur = await db.execute("SELECT COUNT(*) FROM open_question_blocks")
    assert (await cur.fetchone())[0] == 0


async def test_question_summary_counts_only_unverified(db):
    assert await board.question_summary(db) == {"unverified": 0, "oldest_created_at": None}
    await board.raise_question(db, question="older", now="2026-09-01T00:00:00+00:00")
    await board.raise_question(db, question="newer", now=NOW)
    closed = await board.raise_question(db, question="closed", now="2026-01-01T00:00:00+00:00")
    await board.close_question(db, question_id=closed, status="resolved", resolution="r", now=NOW)
    assert await board.question_summary(db) == {
        "unverified": 2,
        "oldest_created_at": "2026-09-01T00:00:00+00:00",
    }


async def test_question_text_bounds_are_refused_not_cut(db):
    with pytest.raises(ValueError):
        await board.raise_question(db, question="x" * (board.MAX_QUESTION_CHARS + 1), now=NOW)
    with pytest.raises(ValueError):
        await board.raise_question(db, question="", now=NOW)


async def test_list_questions_reports_listed_and_total(db):
    for i in range(3):
        await board.raise_question(db, question=f"q{i}", now=f"2026-10-03T12:0{i}:00+00:00")
    page = await board.list_questions(db, status="unverified", limit=2)
    assert page["listed"] == 2 and page["total"] == 3
    assert [q["question"] for q in page["items"]] == ["q2", "q1"]  # newest first
    every = await board.list_questions(db, status=None)
    assert every["total"] == 3


# ── board_events ────────────────────────────────────────────────────────────


async def test_event_vocabulary_is_closed(db):
    with pytest.raises(ValueError, match="event must be one of"):
        await board.append_event(db, event="dispatch", now=NOW)


async def test_observed_change_is_deduped_per_event_type(db):
    key = "PVTI_1@2026-10-03T01:00:28Z"
    first = await board.append_event(
        db, event="drag", now=NOW, observed_change_key=key, repo=REPO, issue_number=1
    )
    again = await board.append_event(db, event="drag", now=LATER, observed_change_key=key)
    other_type = await board.append_event(
        db, event="status_write", now=LATER, observed_change_key=key
    )
    assert first is not None and again is None and other_type is not None
    unkeyed = [await board.append_event(db, event="override", now=NOW) for _ in range(2)]
    assert all(e is not None for e in unkeyed), "events without a key are never deduped"


async def test_list_events_filters_and_totals(db):
    await board.append_event(
        db, event="drag", now=NOW, repo=REPO, issue_number=1, detail={"to": "In Progress"}
    )
    await board.append_event(db, event="drag", now=LATER, repo=REPO, issue_number=2)
    await board.append_event(db, event="override", now=LATER, repo=REPO, issue_number=1)
    one = await board.list_events(db, repo=REPO, issue_number=1, limit=1)
    assert one["listed"] == 1 and one["total"] == 2
    assert one["items"][0]["event"] == "override"  # newest first
    drags = await board.list_events(db, event="drag")
    assert drags["total"] == 2
    assert drags["items"][-1]["detail"] == {"to": "In Progress"}


@pytest.mark.parametrize(
    "over",
    [
        {"issue_number": 0},
        {"issue_number": True},
        {"attempt": -1},
        {"repo": "no slash"},
        {"worker": "w" * (board.MAX_LABEL_CHARS + 1)},
        {"observed_change_key": "k" * (board.MAX_LABEL_CHARS + 1)},
        {"project_item_id": "p" * (board.MAX_LABEL_CHARS + 1)},
        {"reason": "r" * (board.MAX_REASON_CHARS + 1)},
    ],
)
async def test_append_event_bounds_every_column(db, over):
    with pytest.raises(ValueError):
        await board.append_event(db, event="drag", now=NOW, **over)
    assert (await board.list_events(db))["total"] == 0


async def test_append_event_repo_is_stored_lowercased_and_found_either_case(db):
    await board.append_event(db, event="drag", now=NOW, repo="Owner/Repo", issue_number=3)
    assert (await board.list_events(db, repo="OWNER/repo"))["items"][0]["repo"] == "owner/repo"


async def test_raise_bounds_raised_by_and_block_count(db):
    with pytest.raises(ValueError, match="raised_by"):
        await board.raise_question(
            db, question="q", now=NOW, raised_by="x" * (board.MAX_LABEL_CHARS + 1)
        )
    with pytest.raises(ValueError, match="blocks in one raise"):
        await board.raise_question(
            db,
            question="q",
            now=NOW,
            blocks=[("card", f"o/r#{i}") for i in range(1, board.MAX_BLOCKS + 2)],
        )
    assert (await board.list_questions(db, status=None))["total"] == 0


@pytest.mark.parametrize("now", [None, ""])
async def test_missing_timestamp_raises_never_reads_as_a_dedup_hit(db, now):
    """`INSERT OR IGNORE` would swallow the NOT NULL failure and return the same
    None as a genuine dedup hit; the targeted conflict clause must not."""
    with pytest.raises(ValueError, match="now is required"):
        await board.append_event(db, event="drag", now=now, observed_change_key="k")


async def test_non_dedup_constraint_failures_still_raise(db):
    """Bypass the Python check to prove the SQL itself does not swallow it."""
    with pytest.raises(aiosqlite.IntegrityError):
        await db.execute(
            "INSERT INTO board_events (event, created_at) VALUES ('drag', NULL) "
            "ON CONFLICT (event, observed_change_key) WHERE observed_change_key IS NOT NULL DO NOTHING"
        )


async def test_oversized_detail_is_refused(db):
    with pytest.raises(ValueError, match="detail is"):
        await board.append_event(
            db, event="override", now=NOW, detail={"x": "y" * board.MAX_DETAIL_BYTES}
        )


# ── retention ───────────────────────────────────────────────────────────────


async def test_prune_counts_real_deletions_and_spares_open_work(db):
    old = "2026-01-01T00:00:00+00:00"
    closed_old = await board.raise_question(db, question="old closed", now=old)
    await board.add_block(
        db, question_id=closed_old, target_kind="card", target_id="owner/repo#1", now=old
    )
    await board.close_question(
        db, question_id=closed_old, status="resolved", resolution="r", now=old
    )
    open_old = await board.raise_question(db, question="old but unverified", now=old)
    closed_new = await board.raise_question(db, question="new closed", now=NOW)
    await board.close_question(
        db, question_id=closed_new, status="dropped", resolution="r", now=NOW
    )
    await _link(db, now=old)
    await board.append_event(db, event="drag", now=old)
    await board.append_event(db, event="drag", now=NOW)

    result = await board.prune(db, now=NOW, question_days=90, event_days=180)

    assert result == {"questions": 1, "question_blocks": 1, "events": 1}
    assert await board.get_question(db, closed_old) is None
    assert (await board.get_question(db, open_old))["status"] == "unverified", (
        "open work is never pruned"
    )
    assert await board.get_question(db, closed_new) is not None
    assert await board.count_links(db) == 1, "promotion pointers are never pruned"
    assert (await board.list_events(db))["total"] == 1


async def test_prune_rejects_non_positive_windows(db):
    with pytest.raises(ValueError):
        await board.prune(db, now=NOW, question_days=0)
