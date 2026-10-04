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


class FakeProjects:
    """The Projects v2 adapter, faked at its functions: one project with the
    Status field, every write recorded."""

    def __init__(self, *, prior_status=None, fail_add=False):
        self.prior_status = prior_status
        self.fail_add = fail_add
        self.adds: list[tuple[str, str]] = []
        self.status_writes: list[tuple[str, str]] = []

    def install(self, monkeypatch):
        from genesis.board import projects_v2 as pv

        status = pv.Field("F_STATUS", "Status", "single_select", {"Proposed": "OPT_P"})

        async def get_project(owner, number, *, runner=None):
            return pv.Project("PROJ", number, "Board", False, {"Status": status})

        async def issue_node_id(owner, name, number, *, runner=None):
            return f"ISSUE_{number}"

        async def add_item(project_id, content_id, *, runner=None):
            if self.fail_add:
                raise pv.ProjectsError("boom")
            self.adds.append((project_id, content_id))
            return f"ITEM_{content_id}"

        async def item_status(item_id, *, runner=None):
            return self.prior_status

        async def set_single_select(project_id, item_id, field_id, option_id, *, runner=None):
            self.status_writes.append((item_id, option_id))

        for name, fn in {
            "get_project": get_project,
            "issue_node_id": issue_node_id,
            "add_item": add_item,
            "item_status": item_status,
            "set_single_select": set_single_select,
        }.items():
            monkeypatch.setattr(pv, name, fn)
        return self


@pytest.fixture
def projects(monkeypatch):
    return FakeProjects().install(monkeypatch)


@pytest.fixture
async def db(tmp_path, monkeypatch, projects):
    monkeypatch.setattr(board_config, "effective_mode", lambda: "live")
    monkeypatch.setattr(board_config, "project_ref", lambda: ("owner", 1))
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


# ─── round 1 (Codex at ea8329d76) ───────────────────────────────────────────


async def test_a_posted_promotion_lands_on_the_board_as_proposed(db, monkeypatch, projects):
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert projects.adds == [("PROJ", "ISSUE_77")]
    assert projects.status_writes == [("ITEM_ISSUE_77", "OPT_P")]
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link["project_item_id"] == "ITEM_ISSUE_77"
    assert (await board_crud.list_events(db, event="status_write"))["total"] == 1


async def test_a_card_that_already_has_a_status_keeps_its_column(db, monkeypatch, projects):
    """Re-adding returns the existing item; Genesis never moves it out of the
    column it is in."""
    projects.prior_status = "Ready"
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert projects.adds and projects.status_writes == []
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link["project_item_id"] == "ITEM_ISSUE_77"
    assert (await board_crud.list_events(db, event="status_write"))["total"] == 0


async def test_a_failed_placement_keeps_the_link_and_is_retried_next_tick(
    db, monkeypatch, projects
):
    projects.fail_add = True
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    out = await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (await _row(db, out["pending_id"]))["status"] == "posted"
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["project_item_id"] is None
    projects.fail_add = False
    await ciw.drain_pending_issue_posts(_RT(db))
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link["project_item_id"] == "ITEM_ISSUE_77"


async def test_unplaced_links_are_not_retried_outside_live(db, monkeypatch, projects):
    projects.fail_add = True
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    projects.fail_add = False
    monkeypatch.setattr(board_config, "effective_mode", lambda: "propose_only")
    assert await ciw._place_unplaced_links(db, NOW) == 0
    assert projects.adds == []


async def _contributor_row(db, *, row_id, status_after=None, source="follow_up"):
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="contributor_issue_post",
        action_class="irreversible",
        description="d",
        context="{}",
    )
    await pip.create(
        db,
        id=row_id,
        request_id=rid,
        repo=REPO,
        title="Contributor version of the same work",
        body="b",
        source=source,
        source_ref=FOLLOW,
        cell_domain="github",
        cell_verb="issue_create",
        cell_risk_class="bulk",
        held_at=NOW,
        mode="live",
    )
    await mgr.resolve(rid, status="approved")
    if status_after == "posted":
        await pip.mark_posted(db, row_id, issue_number=5, issue_url="u", posted_at=NOW)


async def test_a_board_hold_is_refused_when_the_contributor_lane_already_posted(db, monkeypatch):
    """Both lanes' propose-time checks can pass concurrently; the drain, the
    only poster, is where the second issue is stopped."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await _contributor_row(db, row_id="c-row", status_after="posted")
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] != "held"
    assert (await _row(db, out["pending_id"]))["status"] != "posted"


async def test_two_approved_holds_for_one_follow_up_post_exactly_once(db, monkeypatch):
    """Board hold and contributor hold for the same follow-up, both approved:
    whichever the drain reaches first posts, the other is refused."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await _contributor_row(db, row_id="c-row")
    monkeypatch.setattr(ciw, "effective_mode", lambda: "live")
    await ciw.drain_pending_issue_posts(_RT(db))
    assert len([c for c in gh.calls if c[:2] == ["issue", "create"]]) == 1
    statuses = sorted(
        [(await _row(db, out["pending_id"]))["status"], (await _row(db, "c-row"))["status"]]
    )
    assert statuses.count("posted") == 1 and "held" not in statuses


async def test_a_question_raised_during_the_lookups_still_holds_the_post(db, monkeypatch):
    """The marker lookups await GitHub; a question raised meanwhile must be
    seen by the check made right before the create."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def lookup_then_question(repo, digest):
        await board_crud.raise_question(
            db, question="wait", now=NOW, blocks=[("follow_up", FOLLOW)]
        )
        return True, None

    monkeypatch.setattr(ciw, "_find_issue_by_marker", lookup_then_question)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_an_unavailable_block_store_counts_as_blocked(db, monkeypatch):
    async def no_tables(_db):
        return False

    monkeypatch.setattr(board_crud, "tables_available", no_tables)
    row = {"id": "r", "source_ref": f"follow_up:{FOLLOW}"}
    assert await ciw._board_blocked(db, row) is True


@pytest.mark.parametrize("lookup", ["title", "marker"])
async def test_an_exact_hit_in_a_full_search_page_is_used(monkeypatch, lookup):
    """A full page cannot prove absence, but a hit inside it is conclusive."""
    filler = [
        {"number": i, "title": f"other {i}", "url": "u", "createdAt": NOW, "body": ""}
        for i in range(ciw._SEARCH_PAGE - 1)
    ]
    hit = {
        "number": 999,
        "title": "Add X",
        "url": "u",
        "createdAt": NOW,
        "body": "<!-- genesis-board:abc123abc123abc123abc123 -->",
        "author": {"login": "owner"},
    }
    monkeypatch.setattr(ciw, "_run_gh", FakeGh(issues=[], search_issues=[*filler, hit]))
    if lookup == "title":
        ok, found = await ciw._find_open_issue_by_title(REPO, ciw.normalize_title("Add X"))
    else:
        ok, found = await ciw._find_issue_by_marker(REPO, "abc123abc123abc123abc123")
    assert ok is True and found["number"] == 999


async def test_a_codebase_row_carrying_the_follow_up_also_counts_as_posted(db, monkeypatch):
    """A contributor row of ANY source may carry the follow-up id; the check
    keys on that id, never on the source."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await _contributor_row(db, row_id="c-row", status_after="posted", source="codebase")
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] not in ("held", "posted")


async def test_a_contributor_hold_is_refused_by_the_board_pointer_after_prune(db, monkeypatch):
    """Posted board rows are pruned after 30 days; the board_links pointer is
    what still says this follow-up is on the board."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await _contributor_row(db, row_id="c-row")
    await ciw.drain_pending_issue_posts(_RT(db))  # contributor lane off: board posts
    await db.execute("DELETE FROM pending_issue_posts WHERE id = ?", (out["pending_id"],))
    await db.commit()
    monkeypatch.setattr(ciw, "effective_mode", lambda: "live")
    gh.calls.clear()
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, "c-row"))["status"] not in ("held", "posted")


async def test_a_failing_cross_lane_check_leaves_the_row_held(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def broken(_db, _row):
        raise RuntimeError("db gone")

    monkeypatch.setattr(ciw, "_other_lane_posted", broken)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"
