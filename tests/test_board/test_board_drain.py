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
def project_calls(monkeypatch):
    """Every Projects v2 adapter call, recorded (and answered with nothing):
    the drain must never make one — placing cards is the reconciler's job."""
    import inspect

    from genesis.board import projects_v2 as pv

    calls: list[str] = []

    def record(name):
        async def _f(*_a, **_k):
            calls.append(name)
            raise AssertionError(f"the drain called projects_v2.{name}")

        return _f

    for name, fn in vars(pv).items():
        if inspect.iscoroutinefunction(fn) and not name.startswith("_"):
            monkeypatch.setattr(pv, name, record(name))
    return calls


@pytest.fixture
async def db(tmp_path, monkeypatch, project_calls):
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
    events = await board_crud.list_events(db, event="promotion_refused")
    assert events["total"] == 1 and "non-human resolver" in events["items"][0]["reason"]


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
    # Permanent: the hold expires instead of retrying (3 gh calls) every tick.
    assert (await _row(db, out["pending_id"]))["status"] == "expired"
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    )
    events = await board_crud.list_events(db, event="promotion_refused")
    assert events["total"] == 1 and "another account" in events["items"][0]["reason"]


async def test_board_rows_share_the_daily_cap(db, monkeypatch):
    """A board approval is resolver-classified, which cannot prove a human, so a
    board row waits at the cap like any other post."""
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
    assert not gh.created() and (await _row(db, out["pending_id"]))["status"] == "held"


async def test_a_posted_row_missing_its_link_is_relinked_next_tick(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def fail_once(*_a, **_k):
        return False

    # Scoped to the first drain only: a bare monkeypatch.undo() would also strip
    # the fixture's project fakes and send the second drain to real GitHub.
    with monkeypatch.context() as m:
        m.setattr(ciw, "_link_board", fail_once)
        await ciw.drain_pending_issue_posts(_RT(db))
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    )
    await ciw.drain_pending_issue_posts(_RT(db))
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["issue_number"] == 77
    assert (await _row(db, out["pending_id"]))["status"] == "posted"


@pytest.mark.parametrize("contributor_mode", ["off", "live"])
async def test_the_relink_pass_runs_while_the_board_is_off(db, monkeypatch, contributor_mode):
    """It writes only the local pointer for an issue already posted, so the board
    lever must not stop it: skipped, an unlinked post aged out of the cross-lane
    record and a contributor proposal could post a second issue."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await _promote(db)

    async def fail_once(*_a, **_k):
        return False

    with monkeypatch.context() as m:
        m.setattr(ciw, "_link_board", fail_once)
        await ciw.drain_pending_issue_posts(_RT(db))
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    )
    # Board off; the contributor lane off (the drain returns early) or live (the
    # drain runs to its end): either way no hold moves and the pointer is written.
    monkeypatch.setattr(board_config, "effective_mode", lambda: "off")
    monkeypatch.setattr(ciw, "effective_mode", lambda: contributor_mode)
    await ciw.drain_pending_issue_posts(_RT(db))
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["issue_number"] == 77
    assert sum(1 for c in gh.calls if c[:2] == ["issue", "create"]) == 1, "nothing re-posted"


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


async def test_board_posts_consume_the_shared_cap(db, monkeypatch):
    """One posted board row, cap 1: a contributor row then waits."""
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
    assert (await _row(db, "c-row"))["status"] == "held"


async def test_a_question_raised_after_proposal_holds_the_post(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await board_crud.raise_question(db, question="wait", now=NOW, blocks=[("follow_up", FOLLOW)])
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


# ─── round 1 (Codex at ea8329d76) ───────────────────────────────────────────


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
    assert (await _row(db, out["pending_id"]))["status"] == "expired"
    events = await board_crud.list_events(db, event="promotion_refused")
    assert events["total"] == 1 and "other lane" in events["items"][0]["reason"]


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
    # The refusal event is the BOARD's record: a contributor row ending writes none.
    assert (await board_crud.list_events(db, event="promotion_refused"))["total"] == 0


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


# ─── round 2 (Codex at db70ffbd4 + class audit) ─────────────────────────────


async def test_a_failed_viewer_lookup_is_transient_and_leaves_the_hold(db, monkeypatch):
    out = await _promote(db)
    body = (await _row(db, out["pending_id"]))["body"]
    marked = {"number": 9, "url": "u", "createdAt": NOW, "body": body, "author": {"login": "x"}}
    gh = FakeGh(search_issues=[marked], viewer=None)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_an_unreadable_board_store_holds_a_contributor_row(db, monkeypatch):
    """Fail toward not posting: the cross-lane check cannot read the board."""
    await _contributor_row(db, row_id="c-row")
    monkeypatch.setattr(ciw, "effective_mode", lambda: "live")

    async def no_tables(_db):
        return False

    monkeypatch.setattr(board_crud, "tables_available", no_tables)
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, "c-row"))["status"] == "held"


# ─── round 3 (owner-ruled class fix) ────────────────────────────────────────


async def test_a_promoted_issue_is_linked_but_never_placed(db, monkeypatch, project_calls):
    """Placement is the reconciler's job: the drain creates and links the
    issue, makes no project call, and needs no project configured."""
    monkeypatch.setattr(board_config, "project_ref", lambda: None)
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    out = await _promote(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (await _row(db, out["pending_id"]))["status"] == "posted"
    link = await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    assert link is not None and link["project_item_id"] is None
    assert project_calls == []
    assert (await board_crud.list_events(db, event="status_write"))["total"] == 0


def _labels(monkeypatch, present, *, fail=False, repo_ok=True):
    def lookup(repo, name):
        if fail:
            return 1, "HTTP 502: Bad Gateway"
        return (0, "") if name in present() else (1, "gh: Not Found (HTTP 404)")

    monkeypatch.setattr(promotion, "_label_lookup", lookup)
    monkeypatch.setattr(
        promotion, "_repo_lookup", lambda repo: (0, "") if repo_ok else (1, "Not Found (HTTP 404)")
    )


async def _promote_labelled(db, monkeypatch, labels):
    present = {"bug"}
    _labels(monkeypatch, lambda: present)
    out = await promotion.propose(
        db, source=f"follow_up:{FOLLOW}", title="Add X", body="Body.", labels=labels, now=NOW
    )
    assert out["status"] == "held", out
    await ApprovalManager(db=db).resolve(
        out["request_id"], status="approved", resolved_by="dashboard"
    )
    return out, present


async def _set_follow_up(db, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    await db.execute(f"UPDATE follow_ups SET {sets} WHERE id = ?", (*cols.values(), FOLLOW))
    await db.commit()


async def _already_linked(db):
    await board_crud.record_link(
        db,
        source_kind="follow_up",
        source_id=FOLLOW,
        repo=REPO,
        issue_number=5,
        promoted_by="dashboard",
        scan_receipt={},
        body_sha256="c" * 64,
        now=NOW,
    )


async def _deleted(db):
    await db.execute("DELETE FROM follow_ups WHERE id = ?", (FOLLOW,))
    await db.commit()


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda db: _set_follow_up(db, status="completed"), id="source-completed"),
        pytest.param(lambda db: _set_follow_up(db, status="failed"), id="source-failed"),
        pytest.param(lambda db: _set_follow_up(db, kind="tabled"), id="source-tabled"),
        pytest.param(_deleted, id="source-deleted"),
        pytest.param(_already_linked, id="already-linked"),
    ],
)
async def test_a_permanent_precondition_failure_at_post_time_ends_the_hold(db, monkeypatch, change):
    """Re-checked immediately before the create: waiting cannot clear these,
    so the hold ends (logged), nothing posts, and the owner can re-propose."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    await change(db)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "expired"
    events = await board_crud.list_events(db, event="promotion_refused")
    assert events["total"] == 1 and "at post time" in events["items"][0]["reason"]


async def test_a_label_deleted_during_the_hold_ends_it_and_frees_a_re_proposal(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out, present = await _promote_labelled(db, monkeypatch, ["bug"])
    present.clear()  # the label is deleted on the tracker after approval
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "expired"
    again = await promotion.propose(
        db, source=f"follow_up:{FOLLOW}", title="Add X", body="Body.", now=NOW
    )
    assert again["status"] == "held", "an ended hold no longer blocks a corrected proposal"


@pytest.mark.parametrize(
    "outage",
    [
        pytest.param({"fail": True}, id="label-lookup-fails"),
        pytest.param({"repo_ok": False}, id="repo-unreadable"),
    ],
)
async def test_an_unreadable_tracker_at_post_time_leaves_the_hold(db, monkeypatch, outage):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out, _present = await _promote_labelled(db, monkeypatch, ["bug"])
    _labels(monkeypatch, set, **outage)  # every label now reads as missing or unknown
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_an_unavailable_board_store_at_post_time_leaves_the_hold(db, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def no_tables(_db):
        return False

    monkeypatch.setattr(board_crud, "tables_available", no_tables)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_a_source_closed_during_the_lookups_is_still_caught(db, monkeypatch):
    """The re-check runs AFTER the marker lookups that await GitHub."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)

    async def lookup_then_close(repo, digest):
        await _set_follow_up(db, status="completed")
        return True, None

    monkeypatch.setattr(ciw, "_find_issue_by_marker", lookup_then_close)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "expired"


async def test_the_pointer_and_its_event_commit_together_or_not_at_all(db, monkeypatch):
    """A failed event write rolls the pointer back too, so the re-link pass
    (which selects posted rows with NO pointer) retries both."""
    monkeypatch.setattr(ciw, "_run_gh", FakeGh())
    out = await _promote(db)

    async def broken_event(_db, _values):
        raise RuntimeError("event insert failed")

    with monkeypatch.context() as m:
        m.setattr(board_crud, "_insert_event", broken_event)
        await ciw.drain_pending_issue_posts(_RT(db))
    assert (await _row(db, out["pending_id"]))["status"] == "posted"
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW) is None
    ), "the pointer must not outlive its failed event"
    assert (await board_crud.list_events(db, event="promotion"))["total"] == 0
    await ciw.drain_pending_issue_posts(_RT(db))
    assert (
        await board_crud.get_link_by_source(db, source_kind="follow_up", source_id=FOLLOW)
    ) is not None
    assert (await board_crud.list_events(db, event="promotion"))["total"] == 1


async def test_a_full_title_search_page_is_logged_with_the_repo(monkeypatch, caplog):
    page = [{"number": i, "title": f"other {i}", "url": "u"} for i in range(ciw._SEARCH_PAGE)]
    monkeypatch.setattr(ciw, "_run_gh", FakeGh(issues=[], search_issues=page))
    with caplog.at_level("WARNING", logger=ciw.__name__):
        ok, _found = await ciw._find_open_issue_by_title(REPO, "add a thing")
    assert ok is False
    assert any(REPO in r.getMessage() and "full page" in r.getMessage() for r in caplog.records)
    assert not any("add a thing" in r.getMessage() for r in caplog.records)


# ─── round 3 audit ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("flipped_to", ["off", "propose_only"])
async def test_a_kill_switch_flipped_during_the_label_lookups_stops_the_post(
    db, monkeypatch, flipped_to
):
    """The drain checks the lever before preconditions(), whose label lookups
    then await GitHub (up to a minute each); a GENESIS_BOARD_DISABLED / mode
    flip landing meanwhile must still stop THIS post, so the lever is the last
    thing preconditions() reads."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out, _present = await _promote_labelled(db, monkeypatch, ["bug"])
    mode = {"now": "live"}
    monkeypatch.setattr(board_config, "effective_mode", lambda: mode["now"])

    def lookup_then_flip(repo, name):
        mode["now"] = flipped_to  # the kill switch lands mid-lookup
        return 0, ""

    monkeypatch.setattr(promotion, "_label_lookup", lookup_then_flip)
    await ciw.drain_pending_issue_posts(_RT(db))
    assert mode["now"] == flipped_to, "the fake lookup must have run (the flip happened)"
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"


async def test_a_board_turned_off_ends_no_hold_even_on_a_permanent_refusal(db, monkeypatch):
    """The lever's verdict outranks every other check: with the board off, a
    hold a permanent refusal would end (here, a label deleted) waits instead."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out, present = await _promote_labelled(db, monkeypatch, ["bug"])
    mode = {"now": "live"}
    monkeypatch.setattr(board_config, "effective_mode", lambda: mode["now"])

    def deleted_then_off(repo, name):
        mode["now"] = "off"
        return 1, "gh: Not Found (HTTP 404)"

    monkeypatch.setattr(promotion, "_label_lookup", deleted_then_off)
    present.clear()
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "held"
    assert (await board_crud.list_events(db, event="promotion_refused"))["total"] == 0


@pytest.mark.parametrize("markers", [0, 2])
async def test_a_body_without_exactly_one_marker_ends_the_hold_on_the_record(
    db, monkeypatch, markers
):
    """The body is fixed at propose time, so a wrong marker count can never
    heal: the hold ends (expired, nothing posted, a promotion_refused event)
    instead of logging an error every tick forever."""
    gh = FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    out = await _promote(db)
    body = (await _row(db, out["pending_id"]))["body"]
    marker = promotion.source_marker("follow_up", FOLLOW)
    assert body.count(marker) == 1  # guard: the fixture really holds one marker
    rewritten = body.replace(marker, "") if markers == 0 else body + "\n\n" + marker
    await db.execute(
        "UPDATE pending_issue_posts SET body = ? WHERE id = ?", (rewritten, out["pending_id"])
    )
    await db.commit()
    await ciw.drain_pending_issue_posts(_RT(db))
    assert not gh.created()
    assert (await _row(db, out["pending_id"]))["status"] == "expired"
    events = await board_crud.list_events(db, event="promotion_refused")
    assert events["total"] == 1 and f"{markers} board markers" in events["items"][0]["reason"]
