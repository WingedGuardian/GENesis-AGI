"""The board lane of the shared issue drain (contributor_issue_watcher), and
the title-search fallback it gave the contributor lane. ``gh`` is faked at
the ``_run_gh`` seam; the DB transitions run for real."""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.autonomy import approval_gate
from genesis.autonomy import contributor_issue_watcher as ciw
from genesis.autonomy.approval import ApprovalManager
from genesis.board import config as board_config
from genesis.board import promotion
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import board as board_crud
from genesis.db.crud import pending_issue_posts as pip
from genesis.db.schema import create_all_tables

REPO = "owner/repo"
FOLLOW = "f0110000" + "a" * 24
NOW = "2026-10-03T12:00:00+00:00"


class _RT:
    def __init__(self, db):
        self._db = db


class FakeGh:
    def __init__(self, *, issues=(), search_issues=(), viewer="owner", create_number=77):
        self.issues = list(issues)
        self.search_issues = list(search_issues)
        self.viewer = viewer
        self.create_number = create_number
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout=60):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return 0, json.dumps(self.search_issues if "--search" in args else self.issues), ""
        if args[:2] == ["issue", "create"]:
            return 0, f"https://github.com/{REPO}/issues/{self.create_number}\n", ""
        if args[:2] == ["api", "user"]:
            return (0, f"{self.viewer}\n", "") if self.viewer else (1, "", "no auth")
        return 0, "", ""

    def created(self):
        return any(c[:2] == ["issue", "create"] for c in self.calls)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "live")
    monkeypatch.setattr(ciw, "effective_mode", lambda: "off")  # contributor lane paused
    monkeypatch.setattr("genesis.env.github_user", lambda: "owner")
    monkeypatch.setattr("genesis.env.github_public_repo", lambda: "repo")
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.execute(
            "INSERT INTO follow_ups (id, source, content, strategy, status, created_at) "
            "VALUES (?, 'test', 'x', 'user_input_needed', 'pending', ?)",
            (FOLLOW, NOW),
        )
        await conn.commit()
        yield conn


async def _promote(db, *, resolved_by="dashboard"):
    out = await promotion.propose(
        db, source=f"follow_up:{FOLLOW}", title="Add X", body="Body.", now=NOW
    )
    assert out["status"] == "held", out
    if resolved_by is not None:
        await ApprovalManager(db=db).resolve(
            out["request_id"], status="approved", resolved_by=resolved_by
        )
    return out


async def _row(db, pending_id):
    return await pip.get_by_id(db, pending_id)


async def test_human_approved_board_row_posts_and_links(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    assert await ciw.drain_pending_issue_posts(_RT(db)) == 1
    assert gh.created()
    row = await _row(db, out["pending_id"])
    assert row["status"] == "posted" and row["issue_number"] == 77
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert (
        link["issue_number"] == 77
        and link["promoted_by"] == "dashboard"
        and link["adopted"] is False
    )
    assert link["scan_receipt"]["ok"] is True
    assert (await board_crud.list_events(db, event="promotion"))["total"] == 1
    assert (await ar.get_by_id(db, out["request_id"]))["consumed_at"] is not None


async def test_on_the_servers_shared_connection_the_link_is_still_written(db, monkeypatch):
    """Production hands the drain the server's SerializedConnection, which the
    board writers refuse; the link and its event must go through a connection
    the drain owns, on the same file, and land."""
    from genesis.db.connection import SerializedConnection

    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    shared = SerializedConnection(db)
    await _promote(shared)
    assert await ciw.drain_pending_issue_posts(_RT(shared)) == 1
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["issue_number"] == 77
    assert (await board_crud.list_events(db, event="promotion"))["total"] == 1


@pytest.mark.parametrize("resolver", ["genesis:contributor-worklog", "system", None, "mystery-bot"])
async def test_non_human_resolver_is_refused_never_posted(db, monkeypatch, resolver):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db, resolved_by=None)
    await db.execute(
        "UPDATE approval_requests SET status='approved', resolved_by=? WHERE id=?",
        (resolver, out["request_id"]),
    )
    await db.commit()
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "expired"


async def test_approve_all_never_sweeps_a_board_promotion(db):
    out = await _promote(db, resolved_by=None)
    from unittest.mock import MagicMock

    gate = approval_gate.AutonomousCliApprovalGate(
        runtime=MagicMock(), approval_manager=ApprovalManager(db=db)
    )
    await gate.approve_all_pending(resolved_by="dashboard")
    assert (await ar.get_by_id(db, out["request_id"]))["status"] == "pending"


async def test_propose_only_stamp_dry_runs_even_under_live(db, monkeypatch):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "propose_only")
    out = await _promote(db)
    monkeypatch.setattr(board_config, "effective_mode", lambda: "live")
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "dry_run"


async def test_board_lane_off_leaves_the_row_held_while_contributor_lane_runs(db, monkeypatch):
    out = await _promote(db)
    monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    monkeypatch.setattr(ciw, "effective_mode", lambda: "live")
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_marked_issue_by_this_account_is_adopted_not_recreated(db, monkeypatch):
    out = await _promote(db)
    body = (await _row(db, out["pending_id"]))["body"]
    gh = FakeGh(
        issues=[
            {"number": 9, "url": "u", "createdAt": NOW, "body": body, "author": {"login": "owner"}}
        ]
    )
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    row = await _row(db, out["pending_id"])
    assert row["status"] == "posted" and row["issue_number"] == 9 and row["adopted"] == 1
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link["issue_number"] == 9 and link["adopted"] is True


async def test_marked_issue_by_someone_else_is_never_adopted(db, monkeypatch):
    out = await _promote(db)
    body = (await _row(db, out["pending_id"]))["body"]
    gh = FakeGh(
        search_issues=[
            {
                "number": 9,
                "url": "u",
                "createdAt": NOW,
                "body": body,
                "author": {"login": "stranger"},
            }
        ]
    )
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    )


async def test_board_rows_are_exempt_from_the_contributor_daily_cap(db, monkeypatch):
    monkeypatch.setattr(ciw, "knob_int", lambda _cfg, _key: 1)
    # One contributor post already in the window: the cap (1) is full.
    await db.execute(
        "INSERT INTO pending_issue_posts (id, request_id, repo, title, body, source, cell_domain, cell_verb, "
        "cell_risk_class, held_at, mode, status, posted_at) VALUES ('c1', 'r-c1', ?, 't', 'b', 'codebase', "
        "'github', 'issue_create', 'bulk', ?, 'live', 'posted', ?)",
        (REPO, NOW, ciw.datetime.now(ciw.UTC).isoformat()),
    )
    await db.commit()
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert gh.created() and (await _row(db, out["pending_id"]))["status"] == "posted"


async def test_a_posted_row_missing_its_link_is_relinked_next_tick(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def fail_once(*_a, **_k):
        return False

    monkeypatch.setattr(ciw, "_link_board", fail_once)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    )
    monkeypatch.undo()
    monkeypatch.setattr(board_config, "effective_mode", lambda: "live")
    monkeypatch.setattr(ciw, "effective_mode", lambda: "off")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["issue_number"] == 77
    assert (await _row(db, out["pending_id"]))["status"] == "posted"


async def test_a_failed_marker_lookup_never_posts(db, monkeypatch):
    out = await _promote(db)

    def gh(args, *, timeout=60):
        if args[:2] == ["issue", "list"]:
            return 1, "", "rate limited"
        raise AssertionError("must not reach create")

    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_title_lookup_falls_back_to_a_quoted_search_beyond_the_recent_window(monkeypatch):
    """The contributor lane's dedup: a matching title OUTSIDE the 200-issue
    recent window is found by the search read, which is a QUOTED phrase."""
    old = {"number": 3, "title": 'fix(x): Add a "Thing"', "url": "u", "createdAt": NOW}
    gh = FakeGh(issues=[], search_issues=[old])
    monkeypatch.setattr(ciw, "_run_gh", gh)
    ok, found = await ciw._find_open_issue_by_title(REPO, ciw.normalize_title(old["title"]))
    assert ok and found["number"] == 3
    search_arg = next(c[c.index("--search") + 1] for c in gh.calls if "--search" in c)
    assert search_arg.startswith('"') and search_arg.endswith('" in:title'), search_arg
    assert search_arg.count('"') == 2, "an embedded quote cannot break out of the phrase"
    gh2 = FakeGh(issues=[], search_issues=[{"number": 4, "title": "Add a Thing extra", "url": "u"}])
    monkeypatch.setattr(ciw, "_run_gh", gh2)
    ok, found = await ciw._find_open_issue_by_title(REPO, "add a thing")
    assert ok and found is None, "the fuzzy search hit is re-checked exactly"


async def test_a_saturated_search_is_unverified_not_absent(monkeypatch):
    page = [{"number": i, "title": f"other {i}", "url": "u"} for i in range(ciw._SEARCH_PAGE)]
    monkeypatch.setattr(ciw, "_run_gh", FakeGh(issues=[], search_issues=page))
    ok, found = await ciw._find_open_issue_by_title(REPO, "add a thing")
    assert ok is False and found is None


async def test_board_posts_do_not_consume_the_contributor_cap(db, monkeypatch):
    """One posted board row, cap 1: a contributor row must still post."""
    monkeypatch.setattr(ciw, "knob_int", lambda _cfg, _key: 1)
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))  # board row posts (#77)
    monkeypatch.setattr(ciw, "effective_mode", lambda: "live")
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="contributor_issue_post",
        action_class="irreversible",
        description="d",
        context="{}",
    )
    await pip.create(
        db,
        id="c-row",
        request_id=rid,
        repo=REPO,
        title="Contributor task",
        body="b",
        source="codebase",
        cell_domain="github",
        cell_verb="issue_create",
        cell_risk_class="bulk",
        held_at=NOW,
        mode="live",
    )
    await mgr.resolve(rid, status="approved")
    gh.create_number = 78
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (await _row(db, "c-row"))["status"] == "posted"


async def test_a_question_raised_after_proposal_holds_the_post(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await board_crud.raise_question(db, question="wait", now=NOW, blocks=[("follow_up", FOLLOW)])
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"
