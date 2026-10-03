"""Promotion (propose side): every refusal, the scan's line+scanner-only
report, and the shape of a held board row. Nothing here touches GitHub."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.board import config as board_config
from genesis.board import promotion
from genesis.contribution.findings import Finding, FindingKind, SanitizerResult, Severity
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import board as board_crud
from genesis.db.schema import create_all_tables

FOLLOW = "f0110000" + "a" * 24
LEDGER = "1edcef00" + "b" * 24
NOW = "2026-10-03T12:00:00+00:00"


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "live")
    monkeypatch.setattr("genesis.env.github_user", lambda: "owner")
    monkeypatch.setattr("genesis.env.github_public_repo", lambda: "Repo")
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.execute(
            "INSERT INTO follow_ups (id, source, content, strategy, status, created_at) "
            "VALUES (?, 'test', 'do x', 'user_input_needed', 'pending', ?)",
            (FOLLOW, NOW),
        )
        await conn.execute(
            "INSERT INTO session_ledger (id, session_id, text, created_at) VALUES (?, 's', 'row', ?)",
            (LEDGER, NOW),
        )
        await conn.commit()
        yield conn


async def _propose(db, **over):
    kw = dict(
        source=f"follow_up:{FOLLOW[:10]}",
        title="Add X",
        body="Body text.",
        acceptance_criteria=["works"],
    )
    kw.update(over)
    return await promotion.propose(db, now=NOW, **kw)


async def _holds(db):
    cur = await db.execute("SELECT * FROM pending_issue_posts WHERE source = 'board'")
    return [dict(r) for r in await cur.fetchall()]


async def test_held_row_shape_and_marker_is_opaque(db):
    out = await _propose(db)
    assert out["status"] == "held" and out["mode"] == "live" and out["repo"] == "owner/repo"
    [row] = await _holds(db)
    assert row["source_ref"] == f"follow_up:{FOLLOW}" and row["mode"] == "live"
    assert promotion.source_marker("follow_up", FOLLOW) in row["body"]
    assert FOLLOW not in row["body"] and FOLLOW[:8] not in row["body"], (
        "the private id never reaches the public body"
    )
    assert "## Acceptance criteria\n\n- [ ] works" in row["body"]
    approval = await ar.get_by_id(db, row["request_id"])
    assert approval["action_type"] == promotion.BOARD_PROMOTION_ACTION_TYPE
    assert approval["status"] == "pending", "never self-approved"
    assert json.loads(approval["context"])["scan_receipt"]["ok"] is True


async def test_off_mode_refuses_and_writes_nothing(db, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    assert (await _propose(db))["status"] == "disabled"
    assert await _holds(db) == []


async def test_propose_only_is_stamped(db, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "propose_only")
    await _propose(db)
    assert (await _holds(db))[0]["mode"] == "propose_only"


@pytest.mark.parametrize(
    "source", ["follow_up:deadbeef", "ledger:12", "issue:x", "nope", f"follow_up:{LEDGER}"]
)
async def test_unresolvable_source_is_an_error(db, source):
    out = await _propose(db, source=source)
    assert out["status"] == "error"
    assert await _holds(db) == []


async def test_ledger_source_resolves(db):
    assert (await _propose(db, source=f"ledger:{LEDGER[:8]}"))["status"] == "held"


async def test_an_open_question_block_refuses_promotion_then_releases(db):
    qid = await board_crud.raise_question(
        db, question="which?", now=NOW, blocks=[("follow_up", FOLLOW)]
    )
    out = await _propose(db)
    assert out["status"] == "refused" and out["blocking_question_ids"] == [qid]
    assert await _holds(db) == []
    refused = await board_crud.list_events(db, event="promotion_refused")
    assert refused["total"] == 1
    await board_crud.close_question(
        db, question_id=qid, status="resolved", resolution="this", now=NOW
    )
    assert (await _propose(db))["status"] == "held"


async def test_a_refusal_on_the_servers_shared_connection_is_still_logged(db):
    """board_promote runs on the server's SerializedConnection, which the board
    writers refuse; the refusal event goes through an owned connection."""
    from genesis.db.connection import SerializedConnection

    await board_crud.raise_question(db, question="which?", now=NOW, blocks=[("follow_up", FOLLOW)])
    out = await _propose(SerializedConnection(db))
    assert out["status"] == "refused"
    assert (await board_crud.list_events(db, event="promotion_refused"))["total"] == 1


async def test_duplicates_are_refused(db):
    assert (await _propose(db))["status"] == "held"
    again = await _propose(db, title="Different title")
    assert again["status"] == "duplicate"
    assert len(await _holds(db)) == 1


async def test_a_follow_up_held_in_the_contributor_lane_is_not_promoted_too(db):
    """One record, one public issue — across lanes."""
    await db.execute(
        "INSERT INTO pending_issue_posts (id, request_id, repo, title, body, source, source_ref, cell_domain, "
        "cell_verb, cell_risk_class, held_at, mode, status) VALUES ('c1', 'r1', 'owner/repo', 't', 'b', "
        "'follow_up', ?, 'github', 'issue_create', 'bulk', ?, 'live', 'held')",
        (FOLLOW, NOW),
    )
    await db.commit()
    out = await _propose(db)
    assert out["status"] == "duplicate"


async def test_the_contributor_lane_refuses_a_follow_up_already_on_the_board(db, monkeypatch):
    from genesis.mcp.health import contributor_issue as ci

    monkeypatch.setattr(ci, "effective_mode", lambda: "live")
    monkeypatch.setattr(ci, "require_approval", lambda: True)
    monkeypatch.setattr(ci, "_default_repo", lambda: "owner/repo")
    assert (await _propose(db))["status"] == "held"  # pending board promotion
    pending = await ci._impl_contributor_issue_propose(
        db,
        title="Other title",
        body="b",
        labels=None,
        repo="other/repo",
        source="follow_up",
        source_follow_up_id=FOLLOW,
    )
    assert pending["status"] in ("duplicate", "held")  # other repo: dedup is repo-scoped
    await board_crud.record_link(
        db,
        source_kind="follow_up",
        source_id=FOLLOW,
        repo="owner/repo",
        issue_number=9,
        promoted_by="dashboard",
        scan_receipt={},
        body_sha256="c" * 64,
        now=NOW,
    )
    linked = await ci._impl_contributor_issue_propose(
        db,
        title="Third title",
        body="b",
        labels=None,
        repo="other/repo",
        source="follow_up",
        source_follow_up_id=FOLLOW,
    )
    assert linked["status"] == "duplicate" and "work board" in linked["reason"]


async def test_already_linked_source_is_refused(db):
    await board_crud.record_link(
        db,
        source_kind="follow_up",
        source_id=FOLLOW,
        repo="owner/repo",
        issue_number=5,
        promoted_by="dashboard",
        scan_receipt={},
        body_sha256="c" * 64,
        now=NOW,
    )
    out = await _propose(db)
    assert out["status"] == "duplicate" and out["issue"] == "owner/repo#5"


async def test_scan_refusal_reports_line_and_scanner_only(db, monkeypatch):
    secret = "SECRET-MATCHED-TEXT"
    finding = Finding(
        kind=list(FindingKind)[0],
        severity=Severity.BLOCK,
        message=f"found {secret}",
        file="<prose>",
        line=3,
        scanner="fingerprint",
        detail=secret,
    )
    monkeypatch.setattr(
        "genesis.contribution.scan_prose",
        lambda _text, **_k: SanitizerResult(
            ok=False, findings=[finding], scanners_run=["fingerprint"]
        ),
    )
    out = await _propose(db)
    assert out["status"] == "blocked" and out["findings"] == [{"line": 3, "scanner": "fingerprint"}]
    assert secret not in json.dumps(out)
    assert await _holds(db) == []
    events = await board_crud.list_events(db, event="promotion_refused")
    assert secret not in json.dumps(events)


@pytest.mark.parametrize(
    "over",
    [
        {"title": ""},
        {"body": "  "},
        {"title": "x" * (promotion.MAX_TITLE_CHARS + 1)},
        {"acceptance_criteria": ["c"] * (promotion.MAX_CRITERIA + 1)},
        {"labels": ["l"] * (promotion.MAX_LABELS + 1)},
        {"labels": ["x" * (promotion.MAX_LABEL_CHARS + 1)]},
    ],
)
async def test_bounds_are_refused(db, over):
    assert (await _propose(db, **over))["status"] == "error"
