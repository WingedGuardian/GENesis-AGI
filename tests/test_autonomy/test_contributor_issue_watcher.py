"""Contributor Work-Log poster drain — resolves held issue posts against their
approval + the mode lever. `gh` is mocked at the `_run_gh` seam; the shadow
observation and DB transitions run for real against an in-memory schema.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.autonomy import contributor_issue_watcher as ciw
from genesis.autonomy.approval import ApprovalManager
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import pending_issue_posts as pip
from genesis.db.schema import create_all_tables

_REPO = "WingedGuardian/GENesis-AGI"


class _RT:
    def __init__(self, db):
        self._db = db


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _seed_held(
    db,
    *,
    title="Add a newcomer task",
    body="Details.",
    labels=None,
    repo=_REPO,
    mode="propose_only",
):
    """Create a held pending_issue_posts row + its linked approval (pending).

    *mode* is STAMPED on the row (the lever mode at propose time) — the drain
    honors it for the dry-run-terminal invariant, so a live-posting test must
    seed ``mode="live"`` (a propose_only-stamped row dry-runs even under a live
    lever).
    """
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="contributor_issue_post",
        action_class="irreversible",
        description="issue draft",
        context="{}",
    )
    pid = str(uuid.uuid4())
    await pip.create(
        db,
        id=pid,
        request_id=rid,
        repo=repo,
        title=title,
        body=body,
        labels=json.dumps(labels) if labels else None,
        source="codebase",
        cell_domain="github",
        cell_verb="issue_create",
        cell_risk_class="bulk",
        held_at="2026-08-07T00:00:00",
        mode=mode,
    )
    return pid, rid, mgr


class _FakeGh:
    """Stand-in for ciw._run_gh. Records calls; canned list/create responses."""

    def __init__(
        self,
        *,
        list_out="[]",
        list_rc=0,
        create_rc=0,
        create_url=f"https://github.com/{_REPO}/issues/42",
    ):
        self.list_out = list_out
        self.list_rc = list_rc
        self.create_rc = create_rc
        self.create_url = create_url
        self.calls: list[list[str]] = []

    def __call__(self, args, *, timeout=60):
        self.calls.append(args)
        if args[:2] == ["issue", "list"]:
            return (
                self.list_rc,
                self.list_out if self.list_rc == 0 else "",
                "" if self.list_rc == 0 else "list boom",
            )
        if args[:2] == ["issue", "create"]:
            if self.create_rc == 0:
                return (0, self.create_url + "\n", "")
            return (1, "", "create boom")
        return (0, "", "")

    def created(self) -> bool:
        return any(c[:2] == ["issue", "create"] for c in self.calls)


def _mode(monkeypatch, mode: str):
    monkeypatch.setattr(ciw, "effective_mode", lambda: mode)


# ── propose_only: dry-run terminal, never posts ────────────────────────────


@pytest.mark.asyncio
async def test_propose_only_approved_dry_runs(db, monkeypatch):
    _mode(monkeypatch, "propose_only")
    gh = _FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db)
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert not gh.created()  # NEVER posts in propose_only
    row = await pip.get_by_id(db, pid)
    assert row["status"] == "dry_run"
    assert row["dry_run_at"]
    assert (await ar.get_by_id(db, rid))["consumed_at"] is not None
    # a shadow observation was recorded (observe-before-enforce).
    cur = await db.execute(
        "SELECT COUNT(*) FROM capability_shadow_events WHERE cell_domain='github'"
    )
    assert (await cur.fetchone())[0] == 1


# ── mode STAMPING: flip-window invariants (Codex remediation) ───────────────


@pytest.mark.asyncio
async def test_propose_only_stamped_not_posted_after_live_flip(db, monkeypatch):
    """The locked 'dry-run is terminal' invariant, at the drain seam: a row
    PROPOSED under propose_only — approved but not yet drained — must NOT post if
    the lever flips to live before its tick. The row's STAMPED mode (not the live
    lever) decides. Without stamping this posts (the Codex BLOCKER)."""
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, mode="propose_only")  # stamped propose_only
    await mgr.resolve(rid, status="approved")
    _mode(monkeypatch, "live")  # lever flipped to live BEFORE the drain tick

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert not gh.created()  # NEVER posts — stamped propose_only is dry-run-terminal
    assert (await pip.get_by_id(db, pid))["status"] == "dry_run"


@pytest.mark.asyncio
async def test_live_stamped_paused_when_lever_flips_to_propose_only(db, monkeypatch):
    """A live-stamped row posts only while the lever is STILL live. If the owner
    flips back to propose_only (a deliberate pause), the row is left held — not
    posted, not dry-run — and resumes on the next live flip."""
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, mode="live")  # stamped live
    await mgr.resolve(rid, status="approved")
    _mode(monkeypatch, "propose_only")  # lever paused before the tick

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "held"  # paused, awaits live
    assert (await ar.get_by_id(db, rid))["consumed_at"] is None


# ── live: post ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_approved_posts_and_records_number(db, monkeypatch):
    _mode(monkeypatch, "live")
    gh = _FakeGh(list_out="[]")  # no existing issue
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, labels=["good first issue"], mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert gh.created()
    # labels were passed to gh
    create_call = next(c for c in gh.calls if c[:2] == ["issue", "create"])
    assert "--label" in create_call and "good first issue" in create_call
    row = await pip.get_by_id(db, pid)
    assert row["status"] == "posted"
    assert row["issue_number"] == 42
    assert row["issue_url"] == f"https://github.com/{_REPO}/issues/42"
    assert (await ar.get_by_id(db, rid))["consumed_at"] is not None


@pytest.mark.asyncio
async def test_live_adopts_existing_open_issue_no_repost(db, monkeypatch):
    """Crash-idempotency + dedup: a normalized-title match already open on the
    repo is adopted (mark_posted with its number), NOT re-created."""
    _mode(monkeypatch, "live")
    existing = json.dumps(
        [
            {
                "number": 7,
                "title": "add a NEWCOMER task",
                "url": f"https://github.com/{_REPO}/issues/7",
            }
        ]
    )
    gh = _FakeGh(list_out=existing)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, title="Add a newcomer task", mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert not gh.created()  # adopted, not re-created
    row = await pip.get_by_id(db, pid)
    assert row["status"] == "posted"
    assert row["issue_number"] == 7


@pytest.mark.asyncio
async def test_live_dedup_list_failure_does_not_post(db, monkeypatch):
    """If the open-issue dedup LIST fails, we must NOT post (can't verify dup) —
    the row stays held and retries next cycle."""
    _mode(monkeypatch, "live")
    gh = _FakeGh(list_rc=1)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "held"  # still held → retry


@pytest.mark.asyncio
async def test_live_create_failure_leaves_held(db, monkeypatch):
    _mode(monkeypatch, "live")
    gh = _FakeGh(list_out="[]", create_rc=1)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert gh.created()  # attempted
    row = await pip.get_by_id(db, pid)
    assert row["status"] == "held"  # retry next cycle
    assert (await ar.get_by_id(db, rid))["consumed_at"] is None  # not consumed on failure


# ── non-approved resolutions ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_marks_rejected_no_post(db, monkeypatch):
    _mode(monkeypatch, "live")
    gh = _FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db)
    await mgr.resolve(rid, status="rejected")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert not gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "rejected"


@pytest.mark.asyncio
async def test_orphaned_approval_expires(db, monkeypatch):
    _mode(monkeypatch, "live")
    monkeypatch.setattr(ciw, "_run_gh", _FakeGh())
    pid, rid, _ = await _seed_held(db)
    # delete the approval row → orphan.
    await db.execute("DELETE FROM approval_requests WHERE id = ?", (rid,))
    await db.commit()

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert (await pip.get_by_id(db, pid))["status"] == "expired"


@pytest.mark.asyncio
async def test_pending_left_held(db, monkeypatch):
    _mode(monkeypatch, "live")
    gh = _FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, _ = await _seed_held(db)  # approval left pending

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "held"


# ── mode / plumbing gates ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_off_mode_short_circuits(db, monkeypatch):
    _mode(monkeypatch, "off")
    gh = _FakeGh()
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db)
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "held"  # untouched


@pytest.mark.asyncio
async def test_no_db_returns_zero(monkeypatch):
    _mode(monkeypatch, "live")
    assert await ciw.drain_pending_issue_posts(_RT(None)) == 0


# ── cautious-rollout rate cap (max_posts_per_day) ──────────────────────────


def _cap(monkeypatch, n: int):
    """Force the per-tick post cap to *n* (drain reads it via load_config)."""
    monkeypatch.setattr(ciw, "load_config", lambda: {"max_posts_per_day": n})


async def _seed_posted(db, *, title, posted_at):
    """A row already POSTED (counts toward the rolling cap). No approval needed —
    count_posted_since counts status='posted' directly."""
    pid = str(uuid.uuid4())
    await pip.create(
        db,
        id=pid,
        request_id=str(uuid.uuid4()),
        repo=_REPO,
        title=title,
        body="b",
        source="codebase",
        cell_domain="github",
        cell_verb="issue_create",
        cell_risk_class="bulk",
        held_at="2026-08-07T00:00:00",
        mode="live",
    )
    await pip.mark_posted(db, pid, issue_number=1, issue_url="u", posted_at=posted_at)
    return pid


@pytest.mark.asyncio
async def test_rate_cap_defers_when_at_limit(db, monkeypatch):
    """At the cap, a ready live row is NOT posted — left held to retry next window."""
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 1)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await _seed_posted(db, title="Already posted today", posted_at=datetime.now(UTC).isoformat())
    pid, rid, mgr = await _seed_held(db, title="Next in line", mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()  # cap reached → no CREATE
    assert (await pip.get_by_id(db, pid))["status"] == "held"  # left held → retries
    assert (await ar.get_by_id(db, rid))["consumed_at"] is None


@pytest.mark.asyncio
async def test_rate_cap_allows_under_limit(db, monkeypatch):
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 2)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await _seed_posted(db, title="One already", posted_at=datetime.now(UTC).isoformat())
    pid, rid, mgr = await _seed_held(db, title="Under the cap", mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert gh.created()
    assert (await pip.get_by_id(db, pid))["status"] == "posted"


@pytest.mark.asyncio
async def test_rate_cap_bounds_a_single_tick(db, monkeypatch):
    """Two ready live rows in ONE tick with cap=1 → exactly ONE posts; the per-row
    count includes the one just posted this tick (mark_posted commits per row)."""
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 1)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid1, rid1, mgr = await _seed_held(db, title="First to post", mode="live")
    await mgr.resolve(rid1, status="approved")
    pid2, rid2, _ = await _seed_held(db, title="Second, over cap", mode="live")
    await mgr.resolve(rid2, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1  # only one resolved
    statuses = {
        (await pip.get_by_id(db, pid1))["status"],
        (await pip.get_by_id(db, pid2))["status"],
    }
    assert statuses == {"posted", "held"}  # one posted, one deferred


@pytest.mark.asyncio
async def test_rate_cap_ignores_dry_runs(db, monkeypatch):
    """dry_run holds (propose_only) never consume a slot — count is status='posted'."""
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 1)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    # a dry-run row exists but must NOT count against the cap
    drpid, drrid, drmgr = await _seed_held(db, title="A dry run", mode="propose_only")
    await drmgr.resolve(drrid, status="approved")
    # drain once in propose_only? no — just mark it dry_run directly to isolate the cap
    await pip.mark_dry_run(db, drpid, dry_run_at=datetime.now(UTC).isoformat())
    pid, rid, mgr = await _seed_held(db, title="A real post", mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert gh.created()  # cap not consumed by the dry_run → posts
    assert (await pip.get_by_id(db, pid))["status"] == "posted"


@pytest.mark.asyncio
async def test_adopt_not_blocked_by_cap(db, monkeypatch):
    """Crash-recovery adopt (an issue already open on the repo) must reconcile even
    at the cap — the cap gates CREATEs, not the idempotent adopt (which is BEFORE
    the cap check)."""
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 1)
    existing = json.dumps(
        [{"number": 7, "title": "adopt me", "url": f"https://github.com/{_REPO}/issues/7"}]
    )
    gh = _FakeGh(list_out=existing)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    await _seed_posted(db, title="At the cap already", posted_at=datetime.now(UTC).isoformat())
    pid, rid, mgr = await _seed_held(db, title="Adopt me", mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert not gh.created()  # adopted, not created
    row = await pip.get_by_id(db, pid)
    assert row["status"] == "posted"
    assert row["issue_number"] == 7  # adopted the existing issue despite the cap


@pytest.mark.asyncio
async def test_adopt_old_issue_does_not_consume_cap(db, monkeypatch):
    """Codex P2: adopting an OLD/human-made issue stamps ITS creation time as
    posted_at, so it does NOT burn a daily-cap slot — only issues created in the
    window count. Here an old adopt + a real create both resolve under cap=1."""
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 1)
    old = "2020-01-01T00:00:00Z"
    existing = json.dumps(
        [
            {
                "number": 7,
                "title": "old adopt",
                "url": f"https://github.com/{_REPO}/issues/7",
                "createdAt": old,
            }
        ]
    )
    gh = _FakeGh(list_out=existing)
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid1, rid1, mgr = await _seed_held(db, title="old adopt", mode="live")
    await mgr.resolve(rid1, status="approved")
    pid2, rid2, _ = await _seed_held(db, title="a real new post", mode="live")
    await mgr.resolve(rid2, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 2  # both resolve
    row1 = await pip.get_by_id(db, pid1)
    assert row1["status"] == "posted"
    # adopt stamped the OLD creation time (normalized Z -> +00:00), so it's out of window
    assert row1["posted_at"].startswith("2020-01-01T00:00:00")
    row2 = await pip.get_by_id(db, pid2)
    assert row2["status"] == "posted"  # the real create was NOT deferred by the adopt


@pytest.mark.asyncio
async def test_kill_switch_recheck_halts_mid_tick(db, monkeypatch):
    """Codex P2: a mode flip to off DURING a tick halts the create — the mode is
    re-read live immediately before the external create, not just once per tick."""
    calls = {"n": 0}

    def _flip():
        calls["n"] += 1
        return "live" if calls["n"] == 1 else "off"  # live at drain top, off at pre-create recheck

    monkeypatch.setattr(ciw, "effective_mode", _flip)
    _cap(monkeypatch, 5)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    pid, rid, mgr = await _seed_held(db, mode="live")
    await mgr.resolve(rid, status="approved")

    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 0
    assert not gh.created()  # create halted by the mid-tick kill
    assert (await pip.get_by_id(db, pid))["status"] == "held"  # left held → retries when live


# ── end-to-end: propose (auto-approve) → drain (post), no human ─────────────


@pytest.mark.asyncio
async def test_e2e_autoapprove_then_drain_posts(db, tmp_path, monkeypatch):
    """Integration of the full autonomous path: require_approval=False → the propose
    tool auto-approves its own hold → the drain posts it (live, under cap) with NO
    human in the loop. Guards against a future refactor decoupling the two halves."""
    from genesis.mcp.health import contributor_issue as ci

    # scan_prose fails closed on a missing fingerprint file — point at a present empty one.
    fp = tmp_path / "fingerprints.txt"
    fp.write_text("")
    monkeypatch.setenv("GENESIS_RELEASE_FINGERPRINTS", str(fp))

    # propose side: live + autonomous posture
    monkeypatch.setattr(ci, "effective_mode", lambda: "live")
    monkeypatch.setattr(ci, "load_config", lambda: {"max_held": 25})
    monkeypatch.setattr(ci, "require_approval", lambda: False)
    # Install-agnostic: the autonomous pin calls _default_repo(); on a fresh clone
    # (no github config) that returns a bare name and would error. Pin it to _REPO so
    # the test exercises the post path, not the empty-config edge (covered elsewhere).
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)
    res = await ci._impl_contributor_issue_propose(
        db,
        title="Autonomous newcomer task",
        body="A clean, public-safe task.",
        labels=["good first issue", "area:runtime"],
        repo=_REPO,
    )
    assert res["status"] == "held"
    assert res["auto_approved"] is True
    assert (await ar.get_by_id(db, res["request_id"]))["status"] == "approved"

    # drain side: live, cap not reached, gh mocked → posts with no human approval
    _mode(monkeypatch, "live")
    _cap(monkeypatch, 2)
    gh = _FakeGh(list_out="[]")
    monkeypatch.setattr(ciw, "_run_gh", gh)
    n = await ciw.drain_pending_issue_posts(_RT(db))
    assert n == 1
    assert gh.created()  # posted end-to-end, never touched by a human
    row = await pip.get_by_id(db, res["pending_id"])
    assert row["status"] == "posted"
    assert row["issue_number"] == 42
