"""The board reconciler's write half (#4400 Part 1): it fills an empty Status
column, adds missing open items once, records each card's id on its link, and
spends a bounded hourly budget, but only in ``live``. In every other mode it
writes nothing and reports what it would have done."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.board import config as board_config
from genesis.board import projects_v2 as pv
from genesis.board import reconciler
from genesis.db.crud import board as board_crud
from genesis.db.schema import create_all_tables

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
REPO = "owner/repo"
OPTS = {name: f"opt-{name}" for name in pv.STATUS_OPTIONS}


class FakeBus:
    def __init__(self):
        self.emits = []

    async def emit(self, subsystem, severity, event_type, message, **details):
        self.emits.append(details)


class FakeRT:
    def __init__(self, db):
        self._db = db
        self._event_bus = FakeBus()
        self.paused = False
        self.successes: list[str] = []
        self.failures: list[tuple[str, object]] = []

    def record_job_success(self, name):
        self.successes.append(name)

    def record_job_failure(self, name, error=None, *, exc=None):
        self.failures.append((name, exc if exc is not None else error))


def _closer(number, *, state="OPEN", cross=False, repo=REPO):
    return {
        "number": number,
        "state": state,
        "createdAt": "2026-11-13T10:00:00Z",
        "isCrossRepository": cross,
        "author": {"login": "someone"},
        "repository": {"nameWithOwner": repo},
    }


def _item(
    item_id,
    *,
    status=None,
    number=1,
    kind="Issue",
    state="OPEN",
    repo=REPO,
    archived=False,
    closers=(),
    closers_total=None,
    content_id=None,
):
    content = {
        "__typename": kind,
        "id": content_id or f"C-{item_id}",
        "number": number,
        "state": state,
        "repository": {"nameWithOwner": repo},
    }
    if kind == "Issue":
        content["closedByPullRequestsReferences"] = {
            "totalCount": len(closers) if closers_total is None else closers_total,
            "nodes": list(closers),
        }
    if kind == "DraftIssue":
        content = {"__typename": "DraftIssue"}
    return {
        "id": item_id,
        "isArchived": archived,
        "status": {"name": status, "optionId": OPTS.get(status), "updatedAt": "t"}
        if status
        else None,
        "genesis": None,
        "note": None,
        "content": content,
    }


class FakeGitHub:
    """Every projects_v2 call the reconciler can make, recorded. A mutation is
    anything that reaches ``add_item`` or ``set_single_select``."""

    def __init__(self, items, *, open_content=None, open_total=None):
        self.items = items
        self.open_content = open_content
        count = open_total if open_total is not None else len(open_content or [])
        self.open = {"issues": count, "pull_requests": 0}
        self.mutations: list[tuple] = []
        self.fail_on: dict[int, BaseException] = {}  # mutation index -> error
        self.on_mutation = None
        self.content_reads = 0
        # Status a re-read returns, by item id; anything absent is still empty.
        self.live_status: dict[str, str] = {}
        self.status_reads: list[str] = []

    def install(self, monkeypatch):
        status = pv.Field("F-STATUS", pv.STATUS_FIELD, "single_select", dict(OPTS))

        async def get_project(owner, number, *, runner=None):
            return pv.Project("PROJ", number, "Board", False, {pv.STATUS_FIELD: status})

        async def list_items(project_id, *, runner=None):
            return {"items": list(self.items), "total": len(self.items)}

        async def repo_open_counts(owner, name, *, runner=None):
            return dict(self.open)

        async def open_content(owner, name, *, runner=None):
            self.content_reads += 1
            return list(self.open_content or [])

        async def mutate(kind, *args):
            index = len(self.mutations)
            if self.on_mutation is not None:
                await self.on_mutation(index, kind, args)
            if index in self.fail_on:
                self.mutations.append((kind, *args, "FAILED"))
                raise self.fail_on[index]
            self.mutations.append((kind, *args))

        async def add_item(project_id, content_id, *, runner=None):
            await mutate("add", content_id)
            return f"ITEM-{content_id}"

        async def set_single_select(project_id, item_id, field_id, option_id, *, runner=None):
            await mutate("status", item_id, option_id)

        async def item_status(item_id, *, runner=None):
            self.status_reads.append(item_id)
            return self.live_status.get(item_id)

        monkeypatch.setattr(pv, "get_project", get_project)
        monkeypatch.setattr(pv, "list_items", list_items)
        monkeypatch.setattr(pv, "repo_open_counts", repo_open_counts)
        monkeypatch.setattr(pv, "open_content", open_content)
        monkeypatch.setattr(pv, "add_item", add_item)
        monkeypatch.setattr(pv, "set_single_select", set_single_select)
        monkeypatch.setattr(pv, "item_status", item_status)
        return self


@pytest.fixture
def mode(monkeypatch):
    state = {"mode": "live"}
    monkeypatch.setattr(board_config, "effective_mode", lambda: state["mode"])
    monkeypatch.setattr(board_config, "project_ref", lambda: ("owner", 2))
    monkeypatch.setattr(board_config, "tracker_repo", lambda: ("owner", "repo"))
    monkeypatch.setattr(reconciler, "WRITE_GAP_S", 0)
    monkeypatch.setattr(reconciler, "_QUIET", {})
    return state


@pytest.fixture
async def db(tmp_path, mode):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _tick(db, now=NOW):
    rt = FakeRT(db)
    summary = await reconciler.run_tick(rt, now=now)
    return rt, summary


async def _events(db, event=None):
    return (await board_crud.list_events(db, event=event, limit=1000))["items"]


# ─── no write outside live ───────────────────────────────────────────────────


@pytest.mark.parametrize("current", ["off", "propose_only"])
async def test_no_mode_but_live_sends_any_mutation(db, mode, monkeypatch, current):
    mode["mode"] = current
    gh = FakeGitHub(
        [_item("A"), _item("B", kind="PullRequest", number=2)],
        open_content=[{"id": "C-new", "number": 9, "kind": "Issue"}],
        open_total=3,
    ).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == []
    assert await _events(db) == []
    if current == "off":
        assert "writes" not in summary
    else:
        assert summary["writes"] == {
            "dry_run": True,
            "would_set_status": 2,
            "would_add_at_most": 1,
            "would_link": 0,
        }
        # a dry run reads no repository listing: it costs no extra API calls
        assert gh.content_reads == 0


async def test_a_paused_runtime_writes_nothing(db, monkeypatch):
    gh = FakeGitHub([_item("A")]).install(monkeypatch)
    rt = FakeRT(db)
    rt.paused = True
    await reconciler.run_tick(rt, now=NOW)
    assert gh.mutations == []


async def test_writes_allowed_is_re_read_before_every_write(db, mode, monkeypatch):
    gh = FakeGitHub([_item("A"), _item("B", number=2), _item("C", number=3)]).install(monkeypatch)

    async def flip(index, kind, args):
        if index == 0:
            mode["mode"] = "propose_only"

    gh.on_mutation = flip
    _, summary = await _tick(db)
    assert len(gh.mutations) == 1
    assert summary["writes"]["stopped_on"] == "mode"


# ─── the column fill ─────────────────────────────────────────────────────────


async def test_the_fill_sets_the_desired_column_for_each_empty_card(db, monkeypatch):
    items = [
        _item("plain", number=1),
        _item("pr", kind="PullRequest", number=2),
        _item("has-pr", number=3, closers=[_closer(30)]),
        _item("fork-pr-only", number=4, closers=[_closer(40, cross=True)]),
        _item("merged-pr-only", number=5, closers=[_closer(50, state="MERGED")]),
        _item("other-repo-pr-only", number=6, closers=[_closer(60, repo="owner/sibling")]),
    ]
    gh = FakeGitHub(items, open_total=6).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == [
        ("status", "plain", OPTS["Proposed"]),
        ("status", "pr", OPTS["In Review"]),
        ("status", "has-pr", OPTS["In Review"]),
        ("status", "fork-pr-only", OPTS["Proposed"]),
        ("status", "merged-pr-only", OPTS["Proposed"]),
        ("status", "other-repo-pr-only", OPTS["Proposed"]),
    ]
    assert summary["writes"]["status_set"] == 6


@pytest.mark.parametrize(
    "item",
    [
        _item("set", status="Ready"),
        _item("dragged-back", status="Proposed", closers=[_closer(7)]),
        _item("in-progress", status="In Progress"),
        _item("archived", archived=True),
        _item("closed", state="CLOSED"),
        _item("other-repo", repo="someone/else"),
        _item("draft", kind="DraftIssue"),
    ],
    ids=lambda it: it["id"],
)
async def test_the_fill_leaves_every_other_card_alone(db, monkeypatch, item):
    gh = FakeGitHub([item], open_total=0).install(monkeypatch)
    await _tick(db)
    assert gh.mutations == []


async def test_a_truncated_closer_list_with_no_open_same_repo_pr_is_not_guessed(db, monkeypatch):
    item = _item("many", closers=[_closer(1, cross=True)], closers_total=40)
    gh = FakeGitHub([item], open_total=1).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == []
    assert summary["writes"]["unresolved"] == 1


async def test_no_card_is_ever_written_in_progress(db, monkeypatch):
    items = [
        _item(f"i{n}", number=n, kind=kind, closers=closers)
        for n, (kind, closers) in enumerate(
            [("Issue", ()), ("PullRequest", ()), ("Issue", [_closer(9)])], start=1
        )
    ]
    gh = FakeGitHub(items, open_total=3).install(monkeypatch)
    await _tick(db)
    assert gh.mutations
    assert all(m[2] != OPTS["In Progress"] for m in gh.mutations)


async def test_each_status_write_is_audited_before_its_request(db, monkeypatch):
    gh = FakeGitHub([_item("A", number=7)], open_total=1).install(monkeypatch)
    seen_at_request = []

    async def check(index, kind, args):
        seen_at_request.append(len(await _events(db, "status_write")))

    gh.on_mutation = check
    await _tick(db)
    assert seen_at_request == [1]
    (row,) = await _events(db, "status_write")
    assert row["observed_change_key"] == f"A:Proposed:{NOW.isoformat()}"
    assert row["project_item_id"] == "A" and row["issue_number"] == 7
    assert row["repo"] == REPO and row["detail"]["to"] == "Proposed"


async def test_a_failed_status_write_is_retried_on_the_next_tick(db, monkeypatch):
    gh = FakeGitHub([_item("A")], open_total=1).install(monkeypatch)
    gh.fail_on = {0: pv.ProjectsError("GraphQL error: boom")}
    rt, summary = await _tick(db)
    assert summary["writes"]["failed"] == 1 and "boom" in summary["last_error"]
    assert rt.failures and not rt.successes
    gh.fail_on = {}
    _, summary = await _tick(db, now=NOW + timedelta(minutes=5))
    assert summary["writes"]["status_set"] == 1
    assert len(await _events(db, "status_write")) == 2  # both attempts are on record


# ─── the backfill ────────────────────────────────────────────────────────────


async def test_missing_open_items_are_added_once_with_their_card_id(db, monkeypatch):
    content = [
        {"id": "C-on", "number": 1, "kind": "Issue"},
        {"id": "C-new-issue", "number": 2, "kind": "Issue"},
        {"id": "C-new-pr", "number": 3, "kind": "PullRequest"},
    ]
    gh = FakeGitHub(
        [_item("on", number=1, status="Ready", content_id="C-on")], open_content=content
    ).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == [("add", "C-new-issue"), ("add", "C-new-pr")]
    assert summary["writes"]["added"] == 2
    rows = sorted(await _events(db, "item_added"), key=lambda r: r["issue_number"])
    assert [(r["issue_number"], r["project_item_id"]) for r in rows] == [
        (2, "ITEM-C-new-issue"),
        (3, "ITEM-C-new-pr"),
    ]
    assert rows[0]["observed_change_key"] == f"C-new-issue:{NOW.isoformat()}"


async def test_an_archived_card_counts_as_on_the_board(db, monkeypatch):
    content = [{"id": "C-arch", "number": 1, "kind": "Issue"}]
    gh = FakeGitHub(
        [_item("arch", number=1, archived=True, content_id="C-arch")], open_content=content
    ).install(monkeypatch)
    await _tick(db)
    assert gh.mutations == []


async def test_a_card_the_owner_removed_is_never_re_added(db, monkeypatch):
    await board_crud_append(db, "item_added", repo=REPO, issue_number=4, key="C-gone:earlier")
    content = [{"id": "C-gone", "number": 4, "kind": "Issue"}]
    gh = FakeGitHub([], open_content=content).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == []
    assert summary["writes"]["excluded"] == 1


async def test_a_failed_add_is_not_recorded_so_the_next_tick_retries_it(db, monkeypatch):
    content = [{"id": "C-x", "number": 5, "kind": "Issue"}]
    gh = FakeGitHub([], open_content=content).install(monkeypatch)
    gh.fail_on = {0: pv.ProjectsError("GraphQL error: transient")}
    await _tick(db)
    assert await _events(db, "item_added") == []
    gh.fail_on = {}
    await _tick(db, now=NOW + timedelta(minutes=5))
    assert gh.mutations[-1] == ("add", "C-x")
    assert len(await _events(db, "item_added")) == 1


async def test_no_repository_listing_when_coverage_is_complete(db, monkeypatch):
    gh = FakeGitHub([_item("A", status="Ready")], open_total=1).install(monkeypatch)
    gh.open_content = [{"id": "C-A", "number": 1, "kind": "Issue"}]
    await _tick(db)
    assert gh.content_reads == 0


async def test_the_fill_runs_before_the_backfill(db, monkeypatch):
    gh = FakeGitHub(
        [_item("empty", number=1, content_id="C-1")],
        open_content=[
            {"id": "C-1", "number": 1, "kind": "Issue"},
            {"id": "C-2", "number": 2, "kind": "Issue"},
        ],
    ).install(monkeypatch)
    await _tick(db)
    assert [m[0] for m in gh.mutations] == ["status", "add"]


# ─── the budget ──────────────────────────────────────────────────────────────


async def board_crud_append(db, event, *, repo=REPO, issue_number=1, key=None, at=NOW):
    async with board_crud.owned_connection(db) as own:
        await board_crud.append_event(
            own,
            event=event,
            now=at.isoformat(),
            repo=repo,
            issue_number=issue_number,
            observed_change_key=key,
        )


async def test_one_tick_spends_at_most_its_share_of_the_hour(db, monkeypatch):
    items = [_item(f"i{n}", number=n) for n in range(1, 41)]
    gh = FakeGitHub(items, open_total=40).install(monkeypatch)
    _, summary = await _tick(db)
    assert len(gh.mutations) == reconciler.WRITES_PER_TICK == 16
    assert summary["writes"]["stopped_on"] == "budget"
    assert summary["writes"]["budget_left"] == 0


async def test_writes_in_the_last_hour_count_against_the_budget(db, monkeypatch):
    for n in range(reconciler.WRITES_PER_HOUR - 3):
        await board_crud_append(db, "status_write", key=f"x{n}", at=NOW - timedelta(minutes=30))
    for n in range(50):  # older than an hour: not counted
        await board_crud_append(db, "item_added", key=f"old{n}", at=NOW - timedelta(minutes=61))
    await board_crud_append(db, "drag", key="d1", at=NOW - timedelta(minutes=1))  # not a write
    items = [_item(f"i{n}", number=n) for n in range(1, 11)]
    gh = FakeGitHub(items, open_total=10).install(monkeypatch)
    _, summary = await _tick(db)
    assert len(gh.mutations) == 3
    assert summary["writes"]["stopped_on"] == "budget"


async def test_a_rate_limit_stops_the_tick_and_shows_in_the_heartbeat(db, monkeypatch):
    items = [_item(f"i{n}", number=n) for n in range(1, 6)]
    gh = FakeGitHub(items, open_total=5).install(monkeypatch)
    gh.fail_on = {1: pv.RateLimited("GraphQL error: You have exceeded a secondary rate limit")}
    rt, summary = await _tick(db)
    assert len(gh.mutations) == 2  # the one that hit the limit, then nothing
    assert summary["writes"]["stopped_on"] == "rate_limited"
    assert "secondary rate limit" in summary["last_error"]
    assert summary["board_state"] == "ok" and summary["items_total"] == 5
    assert rt.successes == ["board_reconciler"] and rt.failures == []
    assert rt._event_bus.emits[0]["writes"]["stopped_on"] == "rate_limited"


async def test_mutations_are_spaced_apart(db, monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(reconciler, "WRITE_GAP_S", 1)
    monkeypatch.setattr(reconciler.asyncio, "sleep", fake_sleep)
    items = [_item(f"i{n}", number=n) for n in range(1, 4)]
    FakeGitHub(items, open_total=3).install(monkeypatch)
    await _tick(db)
    assert sleeps == [1, 1]


# ─── links ───────────────────────────────────────────────────────────────────


async def _link(db, number, source_id):
    async with board_crud.owned_connection(db) as own:
        return await board_crud.record_link(
            own,
            source_kind="follow_up",
            source_id=source_id,
            repo=REPO,
            issue_number=number,
            promoted_by="owner",
            scan_receipt={},
            body_sha256="0" * 64,
            now=NOW.isoformat(),
        )


async def test_a_link_without_its_card_id_gets_it(db, monkeypatch):
    link = await _link(db, 7, "a" * 32)
    await _link(db, 8, "b" * 32)  # not on the board yet: left alone
    FakeGitHub([_item("CARD7", number=7, status="Ready")], open_total=1).install(monkeypatch)
    _, summary = await _tick(db)
    got = await board_crud.get_link_by_issue(db, repo=REPO, issue_number=7)
    assert got["id"] == link["id"] and got["project_item_id"] == "CARD7"
    assert (await board_crud.get_link_by_issue(db, repo=REPO, issue_number=8))[
        "project_item_id"
    ] is None
    assert summary["writes"]["links_set"] == 1


async def test_a_dry_run_counts_the_links_it_would_set(db, mode, monkeypatch):
    mode["mode"] = "propose_only"
    await _link(db, 7, "a" * 32)
    FakeGitHub([_item("CARD7", number=7, status="Ready")], open_total=1).install(monkeypatch)
    _, summary = await _tick(db)
    assert summary["writes"]["would_link"] == 1
    got = await board_crud.get_link_by_issue(db, repo=REPO, issue_number=7)
    assert got["project_item_id"] is None


# ─── the summary ─────────────────────────────────────────────────────────────


async def test_a_live_tick_reports_every_write_count(db, monkeypatch):
    FakeGitHub([_item("A", status="Ready")], open_total=1).install(monkeypatch)
    _, summary = await _tick(db)
    assert summary["writes"] == {
        "added": 0,
        "status_set": 0,
        "links_set": 0,
        "failed": 0,
        "unresolved": 0,
        "excluded": 0,
        "moved": 0,
        "budget_left": reconciler.WRITES_PER_TICK,
        "stopped_on": None,
    }


async def test_unmigrated_tables_skip_the_writes_and_say_so(db, monkeypatch):
    async def no_tables(_db):
        return False

    monkeypatch.setattr(board_crud, "tables_available", no_tables)
    gh = FakeGitHub([_item("A")], open_total=1).install(monkeypatch)
    _, summary = await _tick(db)
    assert gh.mutations == []
    assert summary["writes"] == {"unavailable": "board tables not migrated"}


# ─── review fixes ────────────────────────────────────────────────────────────


async def test_a_board_missing_a_column_spends_nothing_and_still_backfills(db, monkeypatch):
    """A refused Status write is caught before its audit row and its budget, so
    it cannot eat the hour or starve the backfill."""
    items = [_item(f"i{n}", number=n) for n in range(1, 21)]
    content = [{"id": f"C-i{n}", "number": n, "kind": "Issue"} for n in range(1, 21)]
    content.append({"id": "C-new", "number": 99, "kind": "Issue"})
    gh = FakeGitHub(items, open_content=content).install(monkeypatch)
    real_get_project = pv.get_project

    async def no_proposed(owner, number, *, runner=None):
        proj = await real_get_project(owner, number)
        proj.fields[pv.STATUS_FIELD].options.pop("Proposed")
        return proj

    monkeypatch.setattr(pv, "get_project", no_proposed)
    rt, summary = await _tick(db)
    assert await _events(db, "status_write") == []
    assert gh.mutations == [("add", "C-new")]
    assert summary["writes"]["failed"] == 20 and summary["writes"]["added"] == 1
    assert "no 'Proposed' option" in summary["last_error"]
    assert rt.failures


@pytest.mark.parametrize("archived", [True, False], ids=["archived", "removed-by-owner"])
async def test_a_permanent_gap_is_not_re_listed_every_tick(db, monkeypatch, archived):
    if archived:
        board = [_item("arch", number=1, archived=True, content_id="C-1")]
    else:
        await board_crud_append(db, "item_added", issue_number=1, key="C-1:earlier")
        board = []
    gh = FakeGitHub(board, open_content=[{"id": "C-1", "number": 1, "kind": "Issue"}]).install(
        monkeypatch
    )
    for minutes in (0, 5, 10):
        _, summary = await _tick(db, now=NOW + timedelta(minutes=minutes))
    assert gh.mutations == []
    # an archived card is on the board: no listing at all; a removed one is
    # listed once, then not again while the gap holds
    assert gh.content_reads == (0 if archived else 1)
    assert summary["writes"]["excluded"] == (0 if archived else 1)
    if not archived:
        await _tick(db, now=NOW + timedelta(minutes=70))
        assert gh.content_reads == 2  # re-listed hourly


async def test_a_new_item_is_found_once_the_gap_changes(db, monkeypatch):
    await board_crud_append(db, "item_added", issue_number=1, key="C-1:earlier")
    gh = FakeGitHub([], open_content=[{"id": "C-1", "number": 1, "kind": "Issue"}]).install(
        monkeypatch
    )
    await _tick(db)
    gh.open_content.append({"id": "C-2", "number": 2, "kind": "Issue"})
    gh.open["issues"] = 2
    await _tick(db, now=NOW + timedelta(minutes=5))
    assert gh.mutations == [("add", "C-2")]


async def test_a_failed_request_stops_the_tick_whatever_its_wording(db, monkeypatch):
    items = [_item(f"i{n}", number=n) for n in range(1, 6)]
    gh = FakeGitHub(items, open_total=5).install(monkeypatch)
    gh.fail_on = {0: pv.RequestFailed("gh api graphql failed (rc=1): gh: Server Error (HTTP 502)")}
    rt, summary = await _tick(db)
    assert len(gh.mutations) == 1
    assert summary["writes"]["stopped_on"] == "request_failed"
    assert "HTTP 502" in summary["last_error"]
    assert rt.failures


async def test_a_switch_out_of_live_during_the_gap_stops_the_next_write(db, mode, monkeypatch):
    async def flip_during_gap(seconds):
        mode["mode"] = "propose_only"

    monkeypatch.setattr(reconciler, "WRITE_GAP_S", 1)
    monkeypatch.setattr(reconciler.asyncio, "sleep", flip_during_gap)
    gh = FakeGitHub([_item("A"), _item("B", number=2)], open_total=2).install(monkeypatch)
    _, summary = await _tick(db)
    assert len(gh.mutations) == 1 and summary["writes"]["stopped_on"] == "mode"


async def test_a_crash_mid_phase_keeps_the_counts_of_writes_made(db, monkeypatch):
    gh = FakeGitHub(
        [_item("A", number=1, content_id="C-1")],
        open_content=[
            {"id": "C-1", "number": 1, "kind": "Issue"},
            {"id": "C-2", "number": 2, "kind": "Issue"},
        ],
    ).install(monkeypatch)

    async def broken_listing(owner, name, *, runner=None):
        raise KeyError("pullRequests")

    monkeypatch.setattr(pv, "open_content", broken_listing)
    rt, summary = await _tick(db)
    assert summary["writes"]["status_set"] == 1
    assert "KeyError" in summary["last_error"] and rt.failures
    assert gh.mutations == [("status", "A", OPTS["Proposed"])]


async def test_a_card_placed_during_the_tick_is_not_overwritten(db, monkeypatch):
    """The fill re-reads each card just before writing it: a Status set since
    the tick's read (the owner placing it) wins, at no budget and no row."""
    gh = FakeGitHub([_item("A"), _item("B", number=2)], open_total=2).install(monkeypatch)
    gh.live_status = {"A": "Ready"}
    _, summary = await _tick(db)
    assert gh.mutations == [("status", "B", OPTS["Proposed"])]
    assert gh.status_reads == ["A", "B"]
    assert [r["project_item_id"] for r in await _events(db, "status_write")] == ["B"]
    assert summary["writes"]["moved"] == 1
    assert summary["writes"]["budget_left"] == reconciler.WRITES_PER_TICK - 1
