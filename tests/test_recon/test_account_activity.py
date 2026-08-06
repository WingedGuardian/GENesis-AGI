"""Core logic of the GitHub account-activity monitor — no live gh calls.

Exercises classification, event dedup, first-contact detection, observe-vs-live
ping gating, the durable retry state machine, the created_at watermark model,
the cursor sidecar, and first-run seeding against a real in-memory observations
store (no mocking of the crud chain).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import observations
from genesis.outreach.types import OutreachResult, OutreachStatus
from genesis.recon.account_activity import (
    AccountActivityMonitor,
    ActivityEvent,
    _actor_hash,
    _event_hash,
    _parse_paged,
    _pending_hash,
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
    """Fake outreach pipeline. ``status`` controls the delivery verdict of the
    next submit_raw (mutable between calls, to simulate a recovery)."""

    def __init__(self, status: OutreachStatus = OutreachStatus.DELIVERED) -> None:
        self.sent: list = []
        self.status = status

    async def submit_raw(self, text, request):
        self.sent.append((text, request))
        return OutreachResult(
            outreach_id="fake",
            status=self.status,
            channel="telegram",
            message_content=text,
        )


def _ev(
    *,
    actor="AyushkhatiDev",
    kind="pr",
    node="N1",
    num=1,
    repo="owner/repo",
    created_at="2026-08-06T04:00:00Z",
) -> ActivityEvent:
    return ActivityEvent(
        repo=repo,
        kind=kind,
        node_id=node,
        actor=actor,
        number=num,
        title="Fix chunk_messages docstring",
        url="https://github.com/owner/repo/pull/1",
        created_at=created_at,
    )


def _stub_gather(
    mon, monkeypatch, tmp_path, *, mode, events_by_repo, max_events=100, wm="2026-08-06T05:00:00Z"
):
    """Wire a monitor for a gather() test: stub owner/repos/poll/classifier/wm +
    a temp sidecar, so gather()'s orchestration is exercised without live gh."""
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
    monkeypatch.setattr("genesis.recon.account_activity._now_z", lambda: wm)
    mon._owner = "owner"  # skip the live gh api user lookup

    async def fake_poll(repo, since, owner):
        return True, list(events_by_repo.get(repo, []))

    async def not_automation(login, denylist):
        return False

    mon._poll_repo = fake_poll
    mon._is_automation = not_automation


def _mon(db, status: OutreachStatus = OutreachStatus.DELIVERED):
    mon = AccountActivityMonitor(db)
    pipe = _FakePipeline(status)
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
    # event recorded + actor marked seen (delivered), NO pending owed.
    assert await _has(db, _event_hash("owner/repo", "pr", "N1"))
    assert await _has(db, _actor_hash("AyushkhatiDev"))
    assert not await _has(db, _pending_hash("AyushkhatiDev"))


async def test_observe_mode_records_but_never_pings(db):
    mon, pipe = _mon(db)
    pinged = await mon._record_event(_ev(), "observe")

    assert pinged is False
    assert pipe.sent == []
    assert await _has(db, _event_hash("owner/repo", "pr", "N1"))  # still recorded
    assert await _has(db, _actor_hash("AyushkhatiDev"))  # seeded seen (no ping expected)
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


# ── P2#5 — durable retry state machine ───────────────────────────────────────


async def test_failed_ping_queues_pending_and_does_not_mark_seen(db):
    """A non-delivered first-time ping (FAILED/IGNORED) must NOT burn the
    first-contact signal: the actor stays un-seen and a pending marker is owed."""
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    pinged = await mon._record_event(_ev(), "live")

    assert pinged is False  # not delivered → not counted as a ping
    assert len(pipe.sent) == 1  # we DID attempt
    # activity recorded (durable), but actor NOT marked seen, pending IS owed.
    assert await _has(db, _event_hash("owner/repo", "pr", "N1"))
    assert not await _has(db, _actor_hash("AyushkhatiDev"))
    assert await _has(db, _pending_hash("AyushkhatiDev"))


async def test_pending_actor_not_treated_as_seen_but_not_requeued(db):
    """A second event by a still-pending actor records but neither re-pings nor
    creates a duplicate pending row (first-contact collapsed to the actor)."""
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    await mon._record_event(_ev(node="N1"), "live")  # fails → pending
    assert len(pipe.sent) == 1
    pipe.sent.clear()

    await mon._record_event(_ev(node="N2"), "live")  # same actor, still pending
    assert pipe.sent == []  # no second ping attempt
    assert await _count(db, "github_ping_pending") == 1  # not duplicated
    assert await _count(db, "github_account_activity") == 2  # both events recorded


async def test_drain_pending_delivers_then_marks_seen_and_resolves(db):
    """When the pipeline recovers, the drain re-pings the pending actor exactly
    once, marks them seen, and resolves the pending row."""
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    await mon._record_event(_ev(), "live")  # fails → pending owed
    pipe.sent.clear()
    pipe.status = OutreachStatus.DELIVERED  # pipeline recovers

    drained = await mon._drain_pending("live")

    assert drained == 1
    assert len(pipe.sent) == 1  # exactly one retry ping
    assert await _has(db, _actor_hash("AyushkhatiDev"))  # now seen
    # pending row resolved (no longer unresolved).
    remaining = await observations.query(
        db, source="recon", type="github_ping_pending", resolved=False
    )
    assert remaining == []


async def test_drain_still_failing_leaves_pending(db):
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    await mon._record_event(_ev(), "live")  # fails → pending
    pipe.sent.clear()

    drained = await mon._drain_pending("live")  # still FAILED

    assert drained == 0
    remaining = await observations.query(
        db, source="recon", type="github_ping_pending", resolved=False
    )
    assert len(remaining) == 1  # still owed, will retry next tick
    assert not await _has(db, _actor_hash("AyushkhatiDev"))  # still un-seen


async def test_expired_pending_re_arms_first_contact(db):
    """BLOCKER-1 regression: after the TTL sweep RESOLVES an abandoned (never
    delivered) pending row, the actor must NOT be permanently suppressed — a
    later event re-arms the first-contact attempt (else exists_by_hash on the
    resolved pending row would read as 'already contacted' forever)."""
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    await mon._record_event(_ev(node="N1"), "live")  # fails → pending (unresolved)
    # Simulate the daily TTL sweep resolving the abandoned pending marker.
    await observations.resolve_by_content_hash(
        db,
        source="recon",
        content_hash=_pending_hash("AyushkhatiDev"),
        resolved_at="2026-08-13T00:00:00Z",
        resolution_notes="auto-expired (TTL)",
    )
    pipe.sent.clear()
    pipe.status = OutreachStatus.DELIVERED  # pipeline recovered

    pinged = await mon._record_event(_ev(node="N2"), "live")  # new event, same actor

    assert pinged is True  # re-armed, not permanently suppressed
    assert await _has(db, _actor_hash("AyushkhatiDev"))  # now delivered → seen


async def test_drain_resolves_pending_if_actor_already_seen(db):
    """Defensive: if an actor became seen by another path, a stale pending row is
    resolved WITHOUT a duplicate ping."""
    mon, pipe = _mon(db, status=OutreachStatus.FAILED)
    await mon._record_event(_ev(), "live")  # fails → pending
    # mark actor seen out-of-band (simulates a concurrent delivery)
    await observations.create(
        db,
        id="x",
        source="recon",
        type="github_actor_seen",
        content="seen:AyushkhatiDev",
        priority="low",
        created_at="2026-08-06T04:00:00Z",
        content_hash=_actor_hash("AyushkhatiDev"),
        skip_if_duplicate=True,
    )
    pipe.sent.clear()
    pipe.status = OutreachStatus.DELIVERED

    drained = await mon._drain_pending("live")

    assert pipe.sent == []  # no double-ping
    assert drained == 0
    remaining = await observations.query(
        db, source="recon", type="github_ping_pending", resolved=False
    )
    assert remaining == []  # stale pending cleared


# ── classifier ───────────────────────────────────────────────────────────────


async def test_is_automation_bot_and_denylist_need_no_gh_call(db):
    mon, _ = _mon(db)
    assert await mon._is_automation("chatgpt-codex-connector[bot]", set()) is True
    assert await mon._is_automation("SomeReviewBot", {"somereviewbot"}) is True


async def test_is_automation_resolves_type(db, monkeypatch):
    mon, _ = _mon(db)

    async def org(*args, **kwargs):
        return True, "Organization"  # e.g. dependabot

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", org)
    assert await mon._is_automation("dependabot", set()) is True

    async def human(*args, **kwargs):
        return True, "User"

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", human)
    assert await mon._is_automation("AyushkhatiDev", set()) is False


async def test_is_automation_unresolved_returns_none(db, monkeypatch):
    """BLOCKER-2: a FAILED users/{login} lookup must return None (unknown), not
    True — so the caller holds the cursor instead of silently dropping a possible
    human. An unknown verdict must NOT be cached (retry next tick)."""
    mon, _ = _mon(db)

    async def failed(*args, **kwargs):
        return False, ""  # run_gh_checked failure

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", failed)
    assert await mon._is_automation("someone", set()) is None
    assert "someone" not in mon._automation_cache  # not cached — retry later


# ── cursor sidecar + format normalization ────────────────────────────────────


async def test_cursor_sidecar_roundtrip(db, monkeypatch, tmp_path):
    mon, _ = _mon(db)
    monkeypatch.setattr("genesis.recon.account_activity.genesis_home", lambda: tmp_path)

    assert mon._load_cursors() == {}  # no file yet
    mon._save_cursors({"owner/repo": "2026-08-06T00:00:00Z"})
    assert mon._load_cursors() == {"owner/repo": "2026-08-06T00:00:00Z"}


async def test_load_cursors_normalizes_offset_format(db, monkeypatch, tmp_path):
    """A legacy cursor written as `+00:00` (isoformat) is normalized to `Z` so
    lexical comparison against Z-suffixed GitHub timestamps is correct."""
    mon, _ = _mon(db)
    monkeypatch.setattr("genesis.recon.account_activity.genesis_home", lambda: tmp_path)
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/repo": "2026-08-06T00:00:00+00:00"}}'
    )
    assert mon._load_cursors() == {"owner/repo": "2026-08-06T00:00:00Z"}


# ── paginated-baseline parsing (P2#3) ────────────────────────────────────────


async def test_parse_paged_flattens_pages():
    # --slurp wraps each page's array into an outer array.
    payload = '[[{"a": 1}], [{"a": 2}, {"a": 3}]]'
    assert _parse_paged(payload) == [{"a": 1}, {"a": 2}, {"a": 3}]
    # A non-paginated flat array is returned as-is.
    assert _parse_paged('[{"a": 1}]') == [{"a": 1}]
    assert _parse_paged("") == []


# ── first-run seeding ────────────────────────────────────────────────────────


async def test_seed_actors_marks_all_without_records(db):
    mon, _ = _mon(db)
    seeded = await mon._seed_actors([_ev(actor="alice"), _ev(actor="bob", node="N2")])

    assert seeded == 2
    assert await _has(db, _actor_hash("alice"))
    assert await _has(db, _actor_hash("bob"))
    assert await _count(db, "github_account_activity") == 0  # seeding writes no activity


# ── gather() orchestration ───────────────────────────────────────────────────


async def test_gather_advances_cursor_to_watermark(db, monkeypatch, tmp_path):
    """P2#2: after a normal (non-truncated) tick, the cursor advances to the
    watermark captured BEFORE polling — NOT to the newest event's timestamp."""
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        wm="2026-08-06T05:00:00Z",
        events_by_repo={repo: [_ev(actor="alice", node="A", created_at="2026-08-06T04:00:00Z")]},
    )
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/repo": "2026-08-06T00:00:00Z"}}'
    )

    await mon.gather()

    # Cursor == watermark, not the event's 04:00 created_at.
    assert mon._load_cursors()[repo] == "2026-08-06T05:00:00Z"


async def test_gather_filters_old_created_before_cursor(db, monkeypatch, tmp_path):
    """P2#1: an old issue re-surfaced by an edit (created_at <= cursor, but
    returned because updated_at moved) is NOT recorded or pinged."""
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        wm="2026-08-06T05:00:00Z",
        events_by_repo={
            repo: [
                # created long before the cursor — an edited old item.
                _ev(actor="olduser", node="OLD", created_at="2026-01-01T00:00:00Z"),
                # a genuinely new one after the cursor.
                _ev(actor="newuser", node="NEW", created_at="2026-08-06T04:00:00Z"),
            ]
        },
    )
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/repo": "2026-08-01T00:00:00Z"}}'
    )

    await mon.gather()

    pinged = {req.topic.split()[2] for _t, req in pipe.sent}
    assert "newuser" in pinged
    assert "olduser" not in pinged  # filtered by created_at <= cursor
    assert not await _has(db, _event_hash(repo, "pr", "OLD"))  # not even recorded
    assert await _has(db, _event_hash(repo, "pr", "NEW"))


async def test_gather_drops_events_after_watermark(db, monkeypatch, tmp_path):
    """An event created mid-poll (created_at > wm) is deferred this tick — not
    recorded — and will re-fetch next tick (exclusive `since`)."""
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        wm="2026-08-06T05:00:00Z",
        events_by_repo={repo: [_ev(actor="future", node="F", created_at="2026-08-06T05:30:00Z")]},
    )
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/repo": "2026-08-06T00:00:00Z"}}'
    )

    await mon.gather()

    assert pipe.sent == []  # nothing at/after wm processed
    assert not await _has(db, _event_hash(repo, "pr", "F"))


async def test_gather_baselines_cursorless_repo_per_repo(db, monkeypatch, tmp_path):
    """A repo with NO cursor is baselined (seed, no ping) even when a SIBLING
    repo already has one — per-repo, not global first-run."""
    r1, r2 = "owner/has-cursor", "owner/new-repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        wm="2026-08-06T05:00:00Z",
        events_by_repo={
            r1: [_ev(actor="alice", node="A1", repo=r1, created_at="2026-08-06T04:00:00Z")],
            r2: [_ev(actor="bob", node="B1", repo=r2, created_at="2026-08-06T04:00:00Z")],
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
    assert mon._load_cursors()[r2] == "2026-08-06T05:00:00Z"  # baselined to wm


async def test_gather_unresolved_automation_holds_cursor_not_drops(db, monkeypatch, tmp_path):
    """BLOCKER-2: if the human/bot verdict can't be resolved for an actor, the
    repo cursor is HELD (not advanced to wm) so the event re-fetches next tick —
    a transient classification failure must never silently drop a contributor."""
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(
        mon,
        monkeypatch,
        tmp_path,
        mode="live",
        wm="2026-08-06T05:00:00Z",
        events_by_repo={repo: [_ev(actor="maybe", node="M", created_at="2026-08-06T04:00:00Z")]},
    )

    async def unknown(login, denylist):
        return None  # verdict can't be resolved

    mon._is_automation = unknown
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/repo": "2026-08-01T00:00:00Z"}}'
    )

    await mon.gather()

    # Cursor HELD at the old value, not advanced to wm — event re-fetches next tick.
    assert mon._load_cursors()[repo] == "2026-08-01T00:00:00Z"
    assert pipe.sent == []  # not pinged (unknown), but not dropped either
    assert not await _has(db, _event_hash(repo, "pr", "M"))  # not recorded yet


async def test_gather_truncation_holds_cursor_at_last_processed(db, monkeypatch, tmp_path):
    """With more events than the cap, the cursor advances only to the last
    PROCESSED event's created_at — the rest re-fetch next tick, never dropped."""
    repo = "owner/busy"
    mon, pipe = _mon(db)
    evs = [
        _ev(actor="a", node="A", repo=repo, created_at="2026-08-06T01:00:00Z"),
        _ev(actor="b", node="B", repo=repo, created_at="2026-08-06T02:00:00Z"),
        _ev(actor="c", node="C", repo=repo, created_at="2026-08-06T03:00:00Z"),
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
    # Cursor stops at the last PROCESSED event (b @ 02:00), NOT the watermark.
    assert mon._load_cursors()[repo] == "2026-08-06T02:00:00Z"


async def test_gather_truncation_boundary_tie_does_not_strand_twin(db, monkeypatch, tmp_path):
    """`since` is EXCLUSIVE: if the cap splits a same-second group, the cursor
    must stop BEFORE that second, or the deferred twin's ts would equal the
    cursor and never re-fetch."""
    repo = "owner/tie"
    mon, pipe = _mon(db)
    evs = [
        _ev(actor="a", node="A", repo=repo, created_at="2026-08-06T01:00:00Z"),
        _ev(actor="b", node="B", repo=repo, created_at="2026-08-06T02:00:00Z"),  # tie
        _ev(actor="c", node="C", repo=repo, created_at="2026-08-06T02:00:00Z"),  # tie (deferred)
        _ev(actor="d", node="D", repo=repo, created_at="2026-08-06T03:00:00Z"),
    ]
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", max_events=2, events_by_repo={repo: evs})
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        '{"version": 1, "cursors": {"owner/tie": "2026-08-05T00:00:00Z"}}'
    )

    await mon.gather()

    # b (02:00) processed but its twin c (02:00) deferred → cursor holds at
    # a (01:00), strictly before the tie second, so the next exclusive `since`
    # poll re-fetches the whole 02:00 group (b via dedup, c fresh).
    assert mon._load_cursors()[repo] == "2026-08-06T01:00:00Z"
