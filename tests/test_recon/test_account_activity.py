"""Core logic of the GitHub account-activity monitor — no live gh calls.

Exercises classification, event dedup, first-contact detection, observe-vs-live
ping gating, the durable retry state machine, the created_at watermark model,
the cursor sidecar, and first-run seeding against a real in-memory observations
store (no mocking of the crud chain).
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import observations
from genesis.outreach.types import OutreachResult, OutreachStatus
from genesis.recon.account_activity import (
    _NOTIF_CURSOR_KEY,
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


# ── account-level notifications lane (mentions + outbound-contribution responses) ──

_REASONS = {"mention", "team_mention", "author"}
_SINCE = "2026-08-06T00:00:00Z"
_WM = "2026-08-06T05:00:00Z"
_PULL = "https://api.github.com/repos/me/myrepo/pulls/7"
_ISSUE = "https://api.github.com/repos/someone/litellm/issues/5"


def _notif(
    *,
    reason="author",
    repo="someone/litellm",
    tid="t1",
    updated="2026-08-06T04:30:00Z",
    title="My upstream PR",
    latest_comment_url="LCU",
    subject_url="https://api.github.com/repos/someone/litellm/issues/5",
    stype="Issue",
):
    owner_login = repo.split("/")[0]
    subject: dict = {"title": title, "type": stype}
    if subject_url is not None:
        subject["url"] = subject_url
    if latest_comment_url is not None:
        subject["latest_comment_url"] = latest_comment_url
    return {
        "id": tid,
        "reason": reason,
        "updated_at": updated,
        "repository": {"full_name": repo, "owner": {"login": owner_login}},
        "subject": subject,
    }


def _fake_notif_gh(pages, *, actor="ext-user", actor_error=False, surfaces=None, threads=None):
    """Fake ``run_gh_checked``.

    - ``notifications…``            -> ``pages`` (list of pages, slurped)
    - ``<url>?per_page=100…``       -> ``surfaces[<url>]``, default ``[]``
    - ``<subject url>``             -> ``threads[<url>]``, default a thread authored
                                       by ``actor`` and created before the window
    ``actor_error`` fails every non-notifications call.
    """

    async def fn(*args, **kwargs):
        url = args[2]
        if url.startswith("notifications"):
            return True, json.dumps(pages)
        if actor_error:
            return False, ""
        if "?per_page=100" in url:
            return True, json.dumps((surfaces or {}).get(url.split("?")[0], []))
        # Just inside the window for every notification these tests use, so a
        # test that says nothing about the thread still models "an outsider
        # opened it during this interval".
        default = {"user": {"login": actor}, "created_at": "2026-08-06T00:00:01Z"}
        return True, json.dumps((threads or {}).get(url, default))

    return fn


def _rc(login, ts):
    """One comment-shaped event: who acted, and when."""
    return {"user": {"login": login}, "created_at": ts}


async def _human(login, denylist):
    return False


def _item(**kw):
    base = {
        "repo": "someone/litellm",
        "reason": "author",
        "thread_id": "t1",
        "updated_at": "2026-08-06T04:30:00Z",
        "actor": "maintainer",
        "number": 5,
        "title": "My PR",
        "url": "https://github.com/someone/litellm/issues/5",
        "ping": True,
    }
    base.update(kw)
    return base


async def test_notifications_reason_filter(db, monkeypatch):
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", repo="third/party", tid="m1"),
            _notif(reason="author", repo="someone/litellm", tid="a1"),
            _notif(reason="ci_activity", repo="me/own", tid="c1"),
            _notif(reason="subscribed", repo="x/y", tid="s1"),
        ]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM  # clean sweep → advance to the watermark
    assert sorted(i["reason"] for i in items) == ["author", "mention"]


async def test_notifications_author_owned_repo_dropped(db, monkeypatch):
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="author", repo="me/myrepo", tid="own"),  # owner's own → drop
            _notif(reason="author", repo="someone/litellm", tid="ext"),  # foreign → keep
        ]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert {i["thread_id"] for i in items} == {"ext"}


async def test_notifications_mention_on_owned_repo_kept(db, monkeypatch):
    """A `mention` is high-signal on ANY repo (only author/subscribed are
    owner-repo-filtered)."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [[_notif(reason="mention", repo="me/myrepo", tid="mine")]]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert {i["thread_id"] for i in items} == {"mine"}


async def test_notifications_window_excludes_out_of_range(db, monkeypatch):
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="old", updated="2026-08-05T00:00:00Z"),  # <= since
            _notif(reason="mention", tid="future", updated="2026-08-06T06:00:00Z"),  # > wm
            _notif(reason="mention", tid="in", updated="2026-08-06T04:00:00Z"),
        ]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert {i["thread_id"] for i in items} == {"in"}


async def test_external_human_in_window_is_pinged(db, monkeypatch):
    """The predicate, stated positively: somebody who is not the owner and not a
    bot did something on this thread inside the notification's window."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("outsider", "2026-08-06T02:00:00Z")]},
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_owner_only_window_is_dropped(db, monkeypatch):
    """THE REPORTED DEFECT. The owner replies on their own thread; nobody else
    does anything. Previously this produced a digest entry with no contributor
    attached — the steward telling its owner about its owner."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    _rc("outsider", "2026-08-01T00:00:00Z"),  # BEFORE the window
                    _rc("me", "2026-08-06T04:00:00Z"),  # in-window, the owner
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM  # the cursor still advances
    assert items == []


async def test_bot_only_window_is_dropped(db, monkeypatch):
    """The measured real case: a review bot is the only thing acting. Whatever it
    wrote, and whoever it named, it is not a person interacting with the owner."""
    mon, _ = _mon(db)

    async def is_auto(login, denylist):
        return login == "some-bot"

    mon._is_automation = is_auto
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    _rc("some-bot", "2026-08-06T01:00:00Z"),
                    _rc("me", "2026-08-06T04:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []


async def test_human_behind_a_bot_is_still_found(db, monkeypatch):
    """Codex round-2 finding: a bot acting LAST must not mask a human who acted
    earlier in the same window. Scan every in-window actor, not just the newest."""
    mon, _ = _mon(db)

    async def is_auto(login, denylist):
        return login == "some-bot"

    mon._is_automation = is_auto
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    _rc("outsider", "2026-08-06T01:00:00Z"),
                    _rc("some-bot", "2026-08-06T04:00:00Z"),  # newest, but automation
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_newest_human_wins_not_the_first_found(db, monkeypatch):
    """These endpoints return OLDEST first, so scanning in payload order would
    report the earliest human. Report the most recent one."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    _rc("older", "2026-08-06T01:00:00Z"),
                    _rc("newest", "2026-08-06T04:00:00Z"),
                    _rc("middle", "2026-08-06T02:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "newest"


async def test_an_edit_counts_as_acting(db, monkeypatch):
    """Codex round-2 finding: editing a comment IS acting on the thread, and
    GitHub bumps the notification for it. Keying on created_at alone would drop
    the whole class — here the comment predates the window and the edit does not."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    edited = {
        "user": {"login": "outsider"},
        "created_at": "2026-08-01T00:00:00Z",  # before the window
        "updated_at": "2026-08-06T02:00:00Z",  # edited inside it
    }
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [edited]},
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider"


async def test_activity_after_the_notification_is_not_counted(db, monkeypatch):
    """The window closes at the notification's own updated_at, so something that
    lands between the poll and the resolve cannot change what this item is about."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [_rc("latecomer", "2026-08-06T04:45:00Z")]  # > updated (04:30)
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []


async def test_opening_the_thread_counts_as_acting(db, monkeypatch):
    """An outside contributor who opens a pull request and never comments has
    still acted. On a freshly-opened thread it is the ONLY event there is."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": []},
            threads={_PULL: _rc("opener", "2026-08-06T02:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "opener" and items[0]["ping"] is True


async def test_incomplete_read_never_concludes_absence(db, monkeypatch):
    """Dropping means asserting NOBODY acted. If a surface failed to read, that
    absence is unproven — keep the item as a digest row instead."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(
        pages,
        surfaces={f"{_PULL}/comments": [_rc("me", "2026-08-06T04:00:00Z")]},
        threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
    )

    async def fail_one_surface(*args, **kwargs):
        if "/reviews?" in args[2]:
            return False, ""
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", fail_one_surface)
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM  # a per-item failure still never holds the cursor
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_surfaces_are_chosen_by_subject_type_not_url_substring(db, monkeypatch):
    """Codex round-2 finding: a repository named `pulls` sends a substring test
    down the pull-request branch, which then requests a nonexistent /reviews and
    marks the read incomplete, downgrading every notification on that repo."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    issue = "https://api.github.com/repos/acme/pulls/issues/7"
    pages = [
        [_notif(reason="mention", repo="acme/pulls", tid="i", subject_url=issue, stype="Issue")]
    ]
    seen: list[str] = []
    inner = _fake_notif_gh(
        pages,
        surfaces={f"{issue}/comments": [_rc("outsider", "2026-08-06T02:00:00Z")]},
        threads={issue: _rc("me", "2026-08-01T00:00:00Z")},
    )

    async def spy(*args, **kwargs):
        seen.append(args[2])
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", spy)
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert not [u for u in seen if "/reviews" in u], "took the pull-request branch on an Issue"
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_pull_request_reads_all_three_text_surfaces(db, monkeypatch):
    """Inline review comments, conversation comments, and review summary bodies
    are three disjoint endpoints; the flagship deep-poll reads only the second."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    seen: list[str] = []
    inner = _fake_notif_gh(pages, surfaces={}, threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")})

    async def spy(*args, **kwargs):
        seen.append(args[2])
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", spy)
    await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert any("/pulls/7/comments?" in u for u in seen), "inline review comments unread"
    assert any("/issues/7/comments?" in u for u in seen), "conversation comments unread"
    assert any("/pulls/7/reviews?" in u for u in seen), "review summary bodies unread"


async def test_commit_subject_reads_its_comment_surface(db, monkeypatch):
    """Codex round-2 finding: a Commit notification has commit comments. Reading
    no surface for it meant every such item became an actor-less digest row."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    commit = "https://api.github.com/repos/me/myrepo/commits/abc123"
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="c", subject_url=commit, stype="Commit")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{commit}/comments": [_rc("outsider", "2026-08-06T02:00:00Z")]},
            threads={commit: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_unmodelled_subject_type_is_incomplete_not_empty(db, monkeypatch):
    """A subject kind whose surfaces we cannot enumerate must not be read as
    'nobody acted' — that would drop it on evidence we never gathered."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="x", subject_url="SU", stype="CheckSuite")]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


# ── acting is not only commenting ────────────────────────────────────────────
#
# The drop is an ASSERTION that nobody acted, so it is only as good as the set of
# things "acting" is taken to mean. Reading only comment surfaces, and reading an
# actor only out of `.user`, both under-answer that question — and under-answering
# it here means silently discarding real external activity.

_ISSUE_OF_PULL = _PULL.replace("/pulls/", "/issues/")


async def test_a_merge_by_an_external_human_is_not_dropped(db, monkeypatch):
    """Merging, closing, assigning, pushing — none of these writes a comment, all
    of them bump the thread, and the thread object's `user` is its AUTHOR (the
    owner). Without the timeline surface the maintainer who merged the owner's
    pull request registers as nobody, and the item is dropped as self-activity."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="author",
                repo="someone/litellm",
                tid="merged",
                subject_url=_PULL,
                stype="PullRequest",
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                # a timeline event nests its login under `actor`, not `user`
                f"{_ISSUE_OF_PULL}/timeline": [
                    {
                        "event": "merged",
                        "actor": {"login": "maintainer"},
                        "created_at": "2026-08-06T02:00:00Z",
                    }
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "maintainer" and items[0]["ping"] is True


async def test_commit_thread_actor_is_read_from_author(db, monkeypatch):
    """A commit payload carries `user: null` and nests the login under `author`.
    Reading only `.user` makes the commit's own author invisible."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    commit = "https://api.github.com/repos/me/myrepo/commits/abc123"
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="c", subject_url=commit, stype="Commit")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{commit}/comments": []},
            threads={
                commit: {
                    "user": None,
                    "author": {"login": "outsider"},
                    "created_at": "2026-08-06T02:00:00Z",
                }
            },
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_release_subject_is_incomplete_not_absent(db, monkeypatch):
    """A Release has no comment surface AND its payload carries `user: null`, so
    it can never be shown to have an external actor. That is ignorance, not
    absence — it must not be allowed to reach the drop."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    rel = "https://api.github.com/repos/me/myrepo/releases/9"
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="r", subject_url=rel, stype="Release")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, threads={rel: {"user": None, "created_at": "2026-08-06T02:00:00Z"}}),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_positive_evidence_survives_an_incomplete_read(db, monkeypatch):
    """Completeness gates the NEGATIVE conclusion only. An outsider already found
    in a surface that DID load still proves somebody acted — one flaky call out of
    five must not suppress a real contributor's ping."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(
        pages,
        surfaces={f"{_ISSUE_OF_PULL}/comments": [_rc("outsider", "2026-08-06T02:00:00Z")]},
        threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
    )

    async def fail_reviews(*args, **kwargs):
        if "/reviews?" in args[2]:
            return False, ""
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", fail_reviews)
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_an_edit_after_the_window_does_not_hide_an_in_window_creation(db, monkeypatch):
    """A comment created inside the window and edited after it must still count.
    Scoring a row by its NEWEST timestamp alone would push it out of range and
    drop a real contributor."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    {
                        "user": {"login": "outsider"},
                        "created_at": "2026-08-06T02:00:00Z",  # inside the window
                        "updated_at": "2026-08-06T04:40:00Z",  # edited after `until`
                    }
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider"


async def test_a_deleted_account_does_not_prove_absence(db, monkeypatch):
    """A deleted commenter leaves every login field null. That row cannot be
    attributed, so it must not silently count toward "nobody acted"."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    {"user": None, "created_at": "2026-08-06T02:00:00Z"},
                    _rc("outsider", "2026-08-06T01:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    # the unattributable row is skipped, the attributable one behind it still wins
    assert items[0]["actor"] == "outsider"


async def test_a_null_thread_object_does_not_prove_absence(db, monkeypatch):
    """`json.loads("null")` succeeds and yields None, so a null thread payload is
    not a parse failure — it is a successful read of nothing. Without the author
    row we cannot claim nobody acted, so it must degrade to a digest row rather
    than passing silently into the drop."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(pages, surfaces={})

    async def null_thread(*args, **kwargs):
        if args[2] == _PULL:  # the bare subject url, no query string
            return True, "null"
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", null_thread)
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM  # still never holds the cursor
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


# ── the negative is only as good as the inputs ───────────────────────────────
#
# Dropping asserts that NOBODY acted. Every input that cannot be interpreted is a
# way for that assertion to be wrong, so ignorance must never look like absence.


async def test_a_bumped_thread_does_not_attribute_to_its_author(db, monkeypatch):
    """THE REGRESSION THIS WHOLE CHANGE EXISTS TO PREVENT, in its subtlest form.

    An old pull request opened by an outsider; this window only the owner acts.
    The thread's `updated_at` moves because ANYBODY touched it, so scoring the
    thread row by `updated_at` puts it in-window while its author field still
    names the outsider — pinging the owner about the outsider for the owner's own
    activity. The thread row testifies to its CREATION only."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("me", "2026-08-06T04:00:00Z")]},
            threads={
                _PULL: {
                    "user": {"login": "outsider"},
                    "created_at": "2026-07-01T00:00:00Z",  # opened long ago
                    "updated_at": "2026-08-06T04:00:00Z",  # bumped by the owner just now
                }
            },
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []  # the owner's own activity, not the outsider's


async def test_an_unattributable_lone_actor_is_not_absence(db, monkeypatch):
    """A deleted account leaves every login field null. If that is the ONLY
    external in-window action, something happened and we do not know who — which
    is the opposite of evidence that nobody did."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    {"user": None, "created_at": "2026-08-06T02:00:00Z"},
                    _rc("me", "2026-08-06T03:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_ordering_uses_the_in_window_timestamp_not_the_row_maximum(db, monkeypatch):
    """A comment created in-window but edited AFTER it stays in-window on its
    creation time — and must be ordered by that, not by the later edit, or it
    outranks someone who genuinely acted later inside the window."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    {
                        "user": {"login": "edited-early"},
                        "created_at": "2026-08-06T01:00:00Z",  # in-window
                        "updated_at": "2026-08-06T04:45:00Z",  # edited after `until`
                    },
                    _rc("acted-later", "2026-08-06T03:00:00Z"),  # genuinely latest in-window
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "acted-later"


async def test_unreadable_surface_payload_is_not_an_empty_surface(db, monkeypatch):
    """`gh` can exit zero and emit a body that is not an array of rows. Reading
    that as an empty page makes an un-enumerated surface look like a surface with
    nothing on it — and the item is then dropped on that false reading."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(
        pages,
        surfaces={f"{_PULL}/comments": [_rc("me", "2026-08-06T02:00:00Z")]},
        threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
    )

    async def garbage_on_one_surface(*args, **kwargs):
        if "/reviews?" in args[2]:
            return True, '{"message": "Not Found"}'  # exit zero, not an array
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", garbage_on_one_surface)
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_a_tie_across_the_cap_still_advances_the_cursor(db, monkeypatch):
    """`since` is exclusive and these timestamps are second-precision, so if every
    processed item ties the first deferred one there is nowhere to advance to: the
    cursor holds, the same slice is re-selected every tick, dedup hides it, and the
    later tied items are never processed at all. The whole tie group is taken."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    tied = "2026-08-06T02:00:00Z"
    pages = [[_notif(reason="mention", tid=f"n{i}", updated=tied) for i in range(4)]]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert adv != _SINCE, "cursor held at `since` — the lane cannot make progress"
    assert len(items) == 4, "the tie group was split, stranding the deferred twins"


# ── unrecognised input is uncertainty, never absence ─────────────────────────


async def test_a_mentioned_timeline_row_is_not_somebody_acting(db, monkeypatch):
    """MEASURED on a live timeline: when one person writes an @-mention, GitHub
    emits a `mentioned` row per RECIPIENT one second later, each carrying the
    recipient in `.actor`. Reading those as actors means the owner mentioning a
    third party pings that third party about the owner's own comment — this
    branch's own defect, re-entering through the timeline surface."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    issue_of_pull = _PULL.replace("/pulls/", "/issues/")
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{issue_of_pull}/timeline": [
                    {
                        "event": "commented",
                        "actor": {"login": "me"},
                        "created_at": "2026-08-06T02:00:00Z",
                    },
                    {
                        "event": "mentioned",
                        "actor": {"login": "alice"},
                        "created_at": "2026-08-06T02:00:01Z",
                    },
                    {
                        "event": "subscribed",
                        "actor": {"login": "alice"},
                        "created_at": "2026-08-06T02:00:01Z",
                    },
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []  # the owner commented; alice did nothing


async def test_an_unmodelled_timeline_event_is_uncertainty(db, monkeypatch):
    """A timeline event type in neither the doer set nor the recipient set is a
    shape we have not modelled. Every payload this code has been wrong about was
    wrong by reading an unrecognised row as one that did not happen."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    issue_of_pull = _PULL.replace("/pulls/", "/issues/")
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{issue_of_pull}/timeline": [
                    {
                        "event": "some_future_event",
                        "actor": {"login": "somebody"},
                        "created_at": "2026-08-06T02:00:00Z",
                    }
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_a_commit_message_mention_is_not_dropped(db, monkeypatch):
    """A commit payload carries NO top-level created_at/updated_at/submitted_at —
    VERIFIED live; its date is at commit.author.date. Without recovering it the
    row can never enter the window, and an @-mention written in a commit MESSAGE
    is dropped as though nobody acted. The fixture models the REAL shape."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    commit = "https://api.github.com/repos/me/myrepo/commits/abc123"
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="c", subject_url=commit, stype="Commit")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{commit}/comments": []},
            threads={
                commit: {  # exactly the live shape: no top-level timestamps
                    "user": None,
                    "author": {"login": "outsider"},
                    "commit": {"author": {"date": "2026-08-06T02:00:00Z"}},
                }
            },
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider" and items[0]["ping"] is True


async def test_a_row_with_no_readable_timestamp_is_uncertainty(db, monkeypatch):
    """A row we cannot place in time is invisible to the window filter — it can
    neither prove nor disprove that somebody acted, so it must not pass silently
    into a drop."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    {"user": {"login": "outsider"}},  # no timestamp at all
                    _rc("me", "2026-08-06T02:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_a_garbled_surface_payload_is_not_an_empty_surface(db, monkeypatch):
    """Mutation survivor M08. The non-list branch was covered; the JSON DECODE
    branch was not. `gh` exiting zero with truncated output must not read as a
    surface that was successfully enumerated and found empty."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(
        pages,
        surfaces={f"{_PULL}/comments": [_rc("me", "2026-08-06T02:00:00Z")]},
        threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
    )

    async def garbled(*args, **kwargs):
        if "/reviews?" in args[2]:
            return True, '{"incomplete json'  # exits zero, does not parse
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", garbled)
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_a_deferred_twin_on_the_boundary_second_is_not_stranded(db, monkeypatch):
    """Mutation survivor M24. `since` is EXCLUSIVE, so the cursor must advance to
    the OLDEST deferred timestamp, not the newest: advancing past a deferred
    item's second strands it permanently."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="a", updated="2026-08-06T01:00:00Z"),
            _notif(reason="mention", tid="b", updated="2026-08-06T02:00:00Z"),
            _notif(reason="mention", tid="c", updated="2026-08-06T02:00:00Z"),
            _notif(reason="mention", tid="d", updated="2026-08-06T03:00:00Z"),
        ]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, _ = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert adv == "2026-08-06T01:00:00Z", "advanced past a deferred item, stranding it"


async def test_ordering_takes_the_newest_in_window_stamp_not_the_oldest(db, monkeypatch):
    """Mutation survivor M03. Among the timestamps that DO fall inside the window,
    the newest is the one that orders the row."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/comments": [
                    # created early, edited later — both inside the window
                    {
                        "user": {"login": "edited-late"},
                        "created_at": "2026-08-06T01:00:00Z",
                        "updated_at": "2026-08-06T04:00:00Z",
                    },
                    _rc("acted-midway", "2026-08-06T02:00:00Z"),
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "edited-late"


async def test_issue_subjects_read_their_timeline_too(db, monkeypatch):
    """Mutation survivor M28. Only the PullRequest timeline was pinned. An issue
    closed or assigned by an external maintainer, with no comment, is invisible
    without the Issue branch reading it as well."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    issue = "https://api.github.com/repos/me/myrepo/issues/5"
    pages = [
        [_notif(reason="mention", repo="me/myrepo", tid="i", subject_url=issue, stype="Issue")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{issue}/comments": [],
                f"{issue}/timeline": [
                    {
                        "event": "closed",
                        "actor": {"login": "maintainer"},
                        "created_at": "2026-08-06T02:00:00Z",
                    }
                ],
            },
            threads={issue: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "maintainer" and items[0]["ping"] is True


async def test_owner_match_is_case_insensitive(db, monkeypatch):
    """Mutation survivor M29. The guard that prevents self-pings must not depend
    on both sides being spelled identically."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("ME", "2026-08-06T02:00:00Z")]},
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []  # "ME" is the owner


async def test_an_oversized_tie_advances_loudly_rather_than_stalling(db, monkeypatch):
    """The tie extension exists so the cursor can move, not so one tick can
    process an unbounded batch. Past the hard cap it advances past the tied
    second and says what it skipped."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    tied = "2026-08-06T02:00:00Z"
    pages = [[_notif(reason="mention", tid=f"n{i}", updated=tied) for i in range(12)]]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert len(items) == 8, "the hard cap (max_events * 4) was not applied"
    assert adv == tied, "did not advance past the tied second; the lane would stall"


# ── window edges, and shapes the fake could not previously emit ──────────────


async def test_a_committed_row_is_understood_not_unknown(db, monkeypatch):
    """MEASURED live: a `committed` timeline row has no `.actor`, no
    `.created_at`, and an `author` of {name, email, date} with no login. Every
    real pull request on this install carries 2-16 of them. Treating that
    understood-but-identity-free shape as UNKNOWN made every pull request
    permanently uncertain and the self/bot-only drop unreachable — the fix was
    inert on precisely the notifications it was built for."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    issue_of_pull = _PULL.replace("/pulls/", "/issues/")
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{issue_of_pull}/timeline": [
                    {
                        "event": "commented",
                        "actor": {"login": "me"},
                        "created_at": "2026-08-06T02:00:00Z",
                    },
                    {  # the real shape — no actor, no created_at
                        "event": "committed",
                        "author": {
                            "name": "Someone",
                            "email": "s@example.invalid",
                            "date": "2026-08-06T02:30:00Z",
                        },
                    },
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == [], "a committed row made the window uncertain instead of dropping"


async def test_an_unreadable_thread_object_is_not_a_drop(db, monkeypatch):
    """Mutation survivors M59/M60, and the sharpest pair the battery found: they
    invert the lane's central rule. If the thread object cannot be fetched, we do
    not know who opened the thread, so we cannot claim nobody acted."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    inner = _fake_notif_gh(pages, surfaces={f"{_PULL}/comments": []})

    async def thread_fetch_fails(*args, **kwargs):
        if args[2] == _PULL:  # the bare subject url
            return False, ""
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", thread_fetch_fails)
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM  # still never holds the cursor
    assert len(items) == 1
    assert items[0]["actor"] == "" and items[0]["ping"] is False


async def test_an_action_exactly_at_the_window_open_is_excluded(db, monkeypatch):
    """`since` is EXCLUSIVE — it is the previous tick's watermark, and that tick
    already reported anything stamped on it. Including it double-reports."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("outsider", _SINCE)]},  # exactly at `since`
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []


async def test_an_action_exactly_at_the_window_close_is_included(db, monkeypatch):
    """`until` is INCLUSIVE — it is the notification's own `updated_at`, so the
    action that produced the notification sits exactly on it. Excluding it drops
    the very event being reported."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    updated = "2026-08-06T04:30:00Z"  # the _notif default
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("outsider", updated)]},  # exactly at `until`
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "outsider"


async def test_notification_exactly_at_the_cursor_is_excluded_and_at_the_watermark_kept(
    db, monkeypatch
):
    """The candidate filter's own edges: `since < updated <= wm`. One stamped at
    the cursor was covered by the previous tick; one stamped at the watermark is
    this tick's newest and must not be deferred forever.

    The cursor-edge half needs an INCOMPLETE read to be observable: with a clean
    read, a notification stamped at `since` has an empty window and drops anyway,
    so admitting it looks identical. Failing its surfaces makes the difference
    visible as a spurious digest row about something the last tick already
    handled — which is exactly the cost of getting this boundary wrong."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="at_since", updated=_SINCE),
            _notif(reason="mention", tid="at_wm", updated=_WM),
        ]
    ]
    inner = _fake_notif_gh(pages)

    async def surfaces_fail(*args, **kwargs):
        if "?per_page=100" in args[2]:
            return False, ""  # every surface unreadable -> uncertain -> digest row
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", surfaces_fail)
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert {i["thread_id"] for i in items} == {"at_wm"}


async def test_exactly_max_events_candidates_is_not_truncation(db, monkeypatch):
    """`truncated` must be a strict `>`: at exactly the cap there is no deferred
    item, and reading `candidates[max_events]` would index off the end."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="n1", updated="2026-08-06T01:00:00Z"),
            _notif(reason="mention", tid="n2", updated="2026-08-06T02:00:00Z"),
        ]
    ]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert len(items) == 2
    assert adv == _WM  # a clean sweep, not a truncation


async def test_owned_repo_filter_is_case_insensitive(db, monkeypatch):
    """Mutation survivor M69. The `author`-on-own-repo filter is a SECOND owner
    comparison, distinct from the actor one, and a case difference there pings the
    owner about their own thread — the reported defect through a spelling."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [[_notif(reason="author", repo="ME/myrepo", tid="own")]]
    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", _fake_notif_gh(pages))
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []


async def test_a_review_row_is_readable_via_submitted_at(db, monkeypatch):
    """MEASURED live: a review carries `user` + `submitted_at` and NO
    `created_at`. The fake had never emitted that shape, so nothing exercised it."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={
                f"{_PULL}/reviews": [
                    {"user": {"login": "reviewer"}, "submitted_at": "2026-08-06T02:00:00Z"}
                ]
            },
            threads={_PULL: _rc("me", "2026-07-01T00:00:00Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items[0]["actor"] == "reviewer" and items[0]["ping"] is True


async def test_parse_paged_checked_separates_empty_from_unreadable():
    """An empty body is a legitimately empty read; anything unparseable is not.
    The `_parse_paged` wrapper discards the flag, so this is asserted directly."""
    from genesis.recon.account_activity import _parse_paged_checked

    assert _parse_paged_checked("") == ([], True)
    assert _parse_paged_checked("[]") == ([], True)
    assert _parse_paged_checked("{not json") == ([], False)
    assert _parse_paged_checked('{"message": "Not Found"}') == ([], False)
    # a slurped page list, with a non-dict row filtered out rather than kept
    assert _parse_paged_checked('[[1, {"a": 2}]]') == ([{"a": 2}], True)


async def test_comment_surfaces_are_paginated(db, monkeypatch):
    """These endpoints return OLDEST first and the conversation surface ignores
    `direction`, so a single-page read of a busy thread silently returns the
    oldest 100. Match _poll_repo's always-paginate rule."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    seen: list[tuple] = []
    inner = _fake_notif_gh(pages, surfaces={}, threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")})

    async def spy(*args, **kwargs):
        seen.append(args)
        return await inner(*args, **kwargs)

    monkeypatch.setattr("genesis.recon.account_activity.run_gh_checked", spy)
    await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    surface_calls = [a for a in seen if "?per_page=100" in a[2]]
    assert surface_calls, "the comment surfaces were never read"
    for call in surface_calls:
        assert "--paginate" in call, f"unpaginated surface read: {call[2]}"


async def test_classification_blip_surfaces_rather_than_drops(db, monkeypatch):
    """If the human/bot lookup fails, surface and ping rather than dropping — a
    classification blip must never silence a contributor."""
    mon, _ = _mon(db)

    async def is_auto_none(login, denylist):
        return None

    mon._is_automation = is_auto_none
    pages = [
        [
            _notif(
                reason="mention", repo="me/myrepo", tid="pr", subject_url=_PULL, stype="PullRequest"
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_PULL}/comments": [_rc("human1", "2026-08-06T02:00:00Z")]},
            threads={_PULL: _rc("me", "2026-08-01T00:00:00Z")},
        ),
    )
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM
    assert len(items) == 1 and items[0]["ping"] is True


async def test_record_notification_live_pings_and_records(db):
    mon, pipe = _mon(db)
    it = _item()
    assert await mon._record_notification(it, "live") is True
    assert len(pipe.sent) == 1
    h = _event_hash(it["repo"], "notification", f"{it['thread_id']}:{it['updated_at']}")
    assert await _has(db, h)


async def test_record_notification_observe_records_no_ping(db):
    mon, pipe = _mon(db)
    assert await mon._record_notification(_item(), "observe") is False
    assert pipe.sent == []
    assert await _count(db, "github_account_activity") == 1


async def test_record_notification_dedup_no_reping(db):
    mon, pipe = _mon(db)
    it = _item()
    assert await mon._record_notification(it, "live") is True
    assert await mon._record_notification(it, "live") is False  # same thread+updated
    assert len(pipe.sent) == 1
    assert await _count(db, "github_account_activity") == 1


async def test_record_notification_new_update_repings(db):
    mon, pipe = _mon(db)
    assert (
        await mon._record_notification(
            _item(thread_id="t", updated_at="2026-08-06T04:00:00Z"), "live"
        )
        is True
    )
    # same thread, NEW updated_at → distinct event → re-record + re-ping
    assert (
        await mon._record_notification(
            _item(thread_id="t", updated_at="2026-08-06T05:00:00Z"), "live"
        )
        is True
    )
    assert len(pipe.sent) == 2
    assert await _count(db, "github_account_activity") == 2


async def test_gather_notifications_baseline_first_run(db, monkeypatch, tmp_path):
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", events_by_repo={repo: []})
    # no cursor file → notifications baseline (adopt wm, never replay the inbox)
    await mon.gather()
    assert mon._load_cursors()[_NOTIF_CURSOR_KEY] == _WM
    assert pipe.sent == []  # baseline never pings


async def test_gather_notifications_live_pings_external(db, monkeypatch, tmp_path):
    repo = "owner/repo"
    mon, pipe = _mon(db)
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", events_by_repo={repo: []})
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        json.dumps(
            {
                "version": 1,
                "cursors": {
                    repo: "2026-08-06T00:00:00Z",
                    _NOTIF_CURSOR_KEY: "2026-08-06T00:00:00Z",
                },
            }
        )
    )
    pages = [
        [_notif(reason="author", repo="someone/litellm", tid="a1", updated="2026-08-06T04:00:00Z")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, actor="maintainer"),
    )
    await mon.gather()
    assert len(pipe.sent) == 1
    assert mon._load_cursors()[_NOTIF_CURSOR_KEY] == _WM


async def test_notifications_cfg_damage_tolerant():
    import genesis.recon.github_steward_config as gsc

    default = {"mention", "team_mention", "author"}
    assert gsc.notifications_cfg(gsc.DEFAULTS)["reasons"] == default
    assert gsc.notifications_cfg({})["reasons"] == default  # missing sub-dict
    corrupt = gsc.notifications_cfg({"notifications": {"enabled": False, "reasons": "bad"}})
    assert corrupt["enabled"] is False
    assert corrupt["reasons"] == default  # corrupt reasons → defaults
    custom = gsc.notifications_cfg({"notifications": {"reasons": ["mention"]}})
    assert custom["reasons"] == {"mention"}


async def test_api_to_html_url_issue_and_pr():
    from genesis.recon.account_activity import _api_to_html_url

    assert (
        _api_to_html_url("https://api.github.com/repos/o/r/issues/5")
        == "https://github.com/o/r/issues/5"
    )
    # PR subjects use /pulls/<n> in the API; the browser path is /pull/<n>.
    assert (
        _api_to_html_url("https://api.github.com/repos/o/r/pulls/9")
        == "https://github.com/o/r/pull/9"
    )
    assert _api_to_html_url("") == ""


async def test_notifications_discussion_recorded_digest_only(db, monkeypatch):
    """Discussion subjects are GraphQL-only (no REST actor); they're recorded
    digest-only (no ping, not held, not dropped) so a cross-repo discussion
    mention still surfaces in the 6h digest."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    disc = _notif(reason="mention", tid="disc")
    disc["subject"]["type"] = "Discussion"
    pages = [[disc, _notif(reason="author", tid="iss")]]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, actor="human1"),
    )
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert adv == _WM
    by_id = {i["thread_id"]: i for i in items}
    assert set(by_id) == {"disc", "iss"}
    assert by_id["disc"]["ping"] is False  # discussion → digest-only
    assert by_id["iss"]["ping"] is True


async def test_notifications_cap_truncates_and_advances_to_boundary(db, monkeypatch):
    """More in-window items than the per-tick cap → process the OLDEST N, defer the
    newer rest, and advance the cursor to the last-processed timestamp (not wm)."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="n1", updated="2026-08-06T01:00:00Z"),
            _notif(reason="mention", tid="n2", updated="2026-08-06T02:00:00Z"),
            _notif(reason="mention", tid="n3", updated="2026-08-06T03:00:00Z"),
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, actor="human1"),
    )
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert {i["thread_id"] for i in items} == {"n1", "n2"}  # oldest 2; n3 deferred
    assert adv == "2026-08-06T02:00:00Z"  # boundary, strictly before the deferred n3


async def test_notifications_truncation_tie_safe_boundary(db, monkeypatch):
    """If the cap boundary splits a same-second group, the cursor advances to the
    newest processed ts STRICTLY BEFORE that second — the deferred same-second
    twin is not stranded by the exclusive `since` (Codex BLOCKER)."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(reason="mention", tid="n1", updated="2026-08-06T01:00:00Z"),
            _notif(reason="mention", tid="n2", updated="2026-08-06T02:00:00Z"),
            _notif(reason="mention", tid="n3", updated="2026-08-06T02:00:00Z"),  # ties the deferred
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, actor="human1"),
    )
    adv, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 2)
    assert {i["thread_id"] for i in items} == {"n1", "n2"}
    # boundary ties n2's second → advance only to n1 (01:00) so n3 re-fetches next tick.
    assert adv == "2026-08-06T01:00:00Z"


async def test_notifications_author_self_reply_skipped(db, monkeypatch):
    """reason=author on a foreign repo where the owner is the resolved (latest)
    actor = the owner replied to their own upstream thread → skipped entirely
    (self-activity, not surfaced)."""
    mon, _ = _mon(db)
    mon._is_automation = _human
    pages = [
        [
            _notif(
                reason="author",
                repo="someone/litellm",
                tid="a",
                subject_url=_ISSUE,
            )
        ]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(
            pages,
            surfaces={f"{_ISSUE}/comments": [_rc("me", "2026-08-06T02:00:00Z")]},
            threads={_ISSUE: _rc("me", "2026-08-06T00:00:01Z")},
        ),
    )
    _, items = await mon._poll_notifications("me", _SINCE, _WM, _REASONS, set(), 100)
    assert items == []


async def test_gather_notifications_runs_with_no_flagship_repos(db, monkeypatch, tmp_path):
    """Regression: the account-level notifications lane runs even when NO flagship
    repos resolve — it is account-level, not repo-scoped."""
    mon, pipe = _mon(db)
    _stub_gather(mon, monkeypatch, tmp_path, mode="live", events_by_repo={})

    async def no_repos(cfg, owner):
        return []

    mon._resolve_flagship_repos = no_repos
    (tmp_path / "github_steward").mkdir(parents=True, exist_ok=True)
    (tmp_path / "github_steward" / "cursors.json").write_text(
        json.dumps({"version": 1, "cursors": {_NOTIF_CURSOR_KEY: "2026-08-06T00:00:00Z"}})
    )
    pages = [
        [_notif(reason="author", repo="someone/litellm", tid="a1", updated="2026-08-06T04:00:00Z")]
    ]
    monkeypatch.setattr(
        "genesis.recon.account_activity.run_gh_checked",
        _fake_notif_gh(pages, actor="maintainer"),
    )
    await mon.gather()
    assert len(pipe.sent) == 1  # notifications lane pinged despite zero flagship repos
    assert mon._load_cursors()[_NOTIF_CURSOR_KEY] == _WM
