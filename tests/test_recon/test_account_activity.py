"""Core logic of the GitHub account-activity monitor — no live gh calls.

Exercises classification, event dedup, first-time detection, observe-vs-live
ping gating, the cursor sidecar, and first-run seeding against a real in-memory
observations store (no mocking of the crud chain).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import observations
from genesis.recon.account_activity import (
    AccountActivityMonitor,
    ActivityEvent,
    _actor_hash,
    _event_hash,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db():
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    try:
        yield conn
    finally:
        await conn.close()


class _FakePipeline:
    def __init__(self) -> None:
        self.sent: list = []

    async def submit_raw(self, text, request):
        self.sent.append((text, request))
        return None


def _ev(
    *,
    actor="AyushkhatiDev",
    kind="pr",
    node="N1",
    num=1,
    repo="owner/repo",
    updated_at="2026-08-06T04:00:00Z",
) -> ActivityEvent:
    return ActivityEvent(
        repo=repo,
        kind=kind,
        node_id=node,
        actor=actor,
        number=num,
        title="Fix chunk_messages docstring",
        url="https://github.com/owner/repo/pull/1",
        updated_at=updated_at,
    )


def _stub_gather(mon, monkeypatch, tmp_path, *, mode, events_by_repo, max_events=100):
    """Wire a monitor for a gather() test: stub owner/repos/poll/classifier + a
    temp sidecar, so gather()'s orchestration is exercised without live gh."""
    import genesis.recon.github_steward_config as gsc

    repos = list(events_by_repo.keys())
    monkeypatch.setattr(gsc, "effective_mode", lambda: mode)
    monkeypatch.setattr(
        gsc,
        "load_config",
        lambda: {
            "flagship_repos": repos,
            "automation_denylist": [],
            "max_events_per_tick": max_events,
        },
    )
    monkeypatch.setattr("genesis.recon.account_activity.genesis_home", lambda: tmp_path)
    mon._owner = "owner"  # skip the live gh api user lookup

    async def fake_poll(repo, since, owner):
        return True, list(events_by_repo.get(repo, []))

    async def not_automation(login, denylist):
        return False

    mon._poll_repo = fake_poll
    mon._is_automation = not_automation


def _mon(db) -> tuple[AccountActivityMonitor, _FakePipeline]:
    mon = AccountActivityMonitor(db)
    pipe = _FakePipeline()
    mon._pipeline = lambda: pipe  # lazy resolver → fake
    return mon, pipe


async def _has(db, content_hash) -> bool:
    return await observations.exists_by_hash(db, source="recon", content_hash=content_hash)


async def _count(db, obs_type) -> int:
    cur = await db.execute("SELECT COUNT(*) FROM observations WHERE type = ?", (obs_type,))
    return (await cur.fetchone())[0]


# ── ping gating ────────────────────────────────────────────────────────────


async def test_first_time_external_pings_in_live(db):
    mon, pipe = _mon(db)
    pinged = await mon._record_event(_ev(), "live")

    assert pinged is True
    assert len(pipe.sent) == 1
    text, req = pipe.sent[0]
    assert "First-time contributor" in text and "AyushkhatiDev" in text
    assert req.channel == "telegram"
    assert req.topic.startswith("GitHub steward:")
    # event + actor both recorded
    assert await _has(db, _event_hash("owner/repo", "pr", "N1"))
    assert await _has(db, _actor_hash("AyushkhatiDev"))


async def test_observe_mode_records_but_never_pings(db):
    mon, pipe = _mon(db)
    pinged = await mon._record_event(_ev(), "observe")

    assert pinged is False
    assert pipe.sent == []
    assert await _has(db, _event_hash("owner/repo", "pr", "N1"))  # still recorded
    assert await _count(db, "github_account_activity") == 1


async def test_dedup_second_sighting_is_noop(db):
    mon, pipe = _mon(db)
    await mon._record_event(_ev(), "live")
    pipe.sent.clear()

    pinged = await mon._record_event(_ev(), "live")  # identical event
    assert pinged is False
    assert pipe.sent == []
    assert await _count(db, "github_account_activity") == 1  # not double-recorded


async def test_returning_contributor_records_without_ping(db):
    mon, pipe = _mon(db)
    await mon._record_event(_ev(node="N1"), "live")  # first-time → seen + ping
    pipe.sent.clear()

    pinged = await mon._record_event(_ev(node="N2"), "live")  # same actor, new event
    assert pinged is False  # no longer first-time
    assert pipe.sent == []
    assert await _count(db, "github_account_activity") == 2


# ── classifier ───────────────────────────────────────────────────────────────


async def test_is_automation_bot_and_denylist_need_no_gh_call(db):
    mon, _ = _mon(db)
    assert await mon._is_automation("chatgpt-codex-connector[bot]", set()) is True
    assert await mon._is_automation("SomeReviewBot", {"somereviewbot"}) is True


async def test_is_automation_resolves_type(db, monkeypatch):
    mon, _ = _mon(db)

    async def fake_run_gh(*args, **kwargs):
        return "Organization"  # e.g. dependabot

    monkeypatch.setattr("genesis.recon.account_activity.run_gh", fake_run_gh)
    assert await mon._is_automation("dependabot", set()) is True

    async def human(*args, **kwargs):
        return "User"

    monkeypatch.setattr("genesis.recon.account_activity.run_gh", human)
    assert await mon._is_automation("AyushkhatiDev", set()) is False


# ── cursor sidecar ───────────────────────────────────────────────────────────


async def test_cursor_sidecar_roundtrip(db, monkeypatch, tmp_path):
    mon, _ = _mon(db)
    monkeypatch.setattr("genesis.recon.account_activity.genesis_home", lambda: tmp_path)

    assert mon._load_cursors() == {}  # no file yet
    mon._save_cursors({"owner/repo": "2026-08-06T00:00:00Z"})
    assert mon._load_cursors() == {"owner/repo": "2026-08-06T00:00:00Z"}


# ── first-run seeding ────────────────────────────────────────────────────────


async def test_seed_actors_marks_all_without_records(db):
    mon, _ = _mon(db)
    seeded = await mon._seed_actors([_ev(actor="alice"), _ev(actor="bob", node="N2")])

    assert seeded == 2
    assert await _has(db, _actor_hash("alice"))
    assert await _has(db, _actor_hash("bob"))
    assert await _count(db, "github_account_activity") == 0  # seeding writes no activity


# ── gather() orchestration (the layer both review blockers lived in) ─────────


async def test_gather_baselines_cursorless_repo_per_repo(db, monkeypatch, tmp_path):
    """A repo with NO cursor is baselined (seed, no ping) even when a SIBLING
    repo already has one — per-repo, not global first-run (review BLOCKER 2)."""
    r1, r2 = "owner/has-cursor", "owner/new-repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        events_by_repo={
            r1: [_ev(actor="alice", node="A1", repo=r1)],
            r2: [_ev(actor="bob", node="B1", repo=r2)],
        },
    )
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/has-cursor": "2026-08-01T00:00:00Z"}}'
    )

    r = await mon.gather()

    pinged = {req.topic.split()[2] for _t, req in pipe.sent}
    assert "alice" in pinged  # cursored repo → processed, first-time ping
    assert "bob" not in pinged  # cursorless repo → baselined, NO ping
    assert r.errors == 0
    assert r2 in mon._load_cursors()  # new repo now baselined with a cursor


async def test_gather_truncation_holds_cursor_at_last_processed(db, monkeypatch, tmp_path):
    """With more events than the cap, the cursor advances only to the last
    PROCESSED event — the rest re-fetch next tick, never dropped (BLOCKER 1)."""
    repo = "owner/busy"
    mon, pipe = _mon(db)
    evs = [
        _ev(actor="a", node="A", repo=repo, updated_at="2026-08-06T01:00:00Z"),
        _ev(actor="b", node="B", repo=repo, updated_at="2026-08-06T02:00:00Z"),
        _ev(actor="c", node="C", repo=repo, updated_at="2026-08-06T03:00:00Z"),
    ]
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", max_events=2, events_by_repo={repo: evs})
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/busy": "2026-08-05T00:00:00Z"}}'
    )

    r = await mon.gather()

    pinged = {req.topic.split()[2] for _t, req in pipe.sent}
    assert pinged == {"a", "b"}  # only the oldest 2 processed
    assert "c" not in pinged  # the 3rd is deferred, NOT dropped
    assert r.errors == 0
    # Cursor stops at the last PROCESSED event (b @ 02:00), NOT the newest (c @ 03:00).
    assert mon._load_cursors()[repo] == "2026-08-06T02:00:00Z"


async def test_gather_truncation_boundary_tie_does_not_strand_twin(db, monkeypatch, tmp_path):
    """`since` is EXCLUSIVE: if the cap splits a same-second group, the cursor
    must stop BEFORE that second (not on it), or the deferred twin's ts would
    equal the cursor and never re-fetch."""
    repo = "owner/tie"
    mon, pipe = _mon(db)
    evs = [
        _ev(actor="a", node="A", repo=repo, updated_at="2026-08-06T01:00:00Z"),
        _ev(actor="b", node="B", repo=repo, updated_at="2026-08-06T02:00:00Z"),  # tie
        _ev(actor="c", node="C", repo=repo, updated_at="2026-08-06T02:00:00Z"),  # tie (deferred)
        _ev(actor="d", node="D", repo=repo, updated_at="2026-08-06T03:00:00Z"),
    ]
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", max_events=2, events_by_repo={repo: evs})
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/tie": "2026-08-05T00:00:00Z"}}'
    )

    await mon.gather()

    # b (02:00) was processed, but its twin c (02:00) was deferred — so the cursor
    # must hold at a (01:00), strictly before the tie second, so the next
    # exclusive `since` poll re-fetches the whole 02:00 group (b via dedup, c fresh).
    assert mon._load_cursors()[repo] == "2026-08-06T01:00:00Z"
