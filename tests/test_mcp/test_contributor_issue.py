"""contributor_issue_propose MCP tool core (_impl) — the Contributor Work-Log
PROPOSE door: server-side sanitize → hold + linked approval, with dedup /
backpressure / mode gating. Mirrors the WS-8 email-gate two-row shape.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.db.crud import approval_requests as ar
from genesis.db.crud import pending_issue_posts as pip
from genesis.db.schema import create_all_tables
from genesis.mcp.health import contributor_issue as ci

_REPO = "WingedGuardian/GENesis-AGI"


@pytest.fixture(autouse=True)
def _fingerprints_present(tmp_path, monkeypatch):
    """scan_prose fails closed on a MISSING fingerprint file (terminal egress
    guard). CI has no ~/.genesis fingerprint file, so point scan_prose at a
    present (empty) one — the propose path then depends only on the input, not
    on the ambient install."""
    fp = tmp_path / "fingerprints.txt"
    fp.write_text("")
    monkeypatch.setenv("GENESIS_RELEASE_FINGERPRINTS", str(fp))


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await conn.commit()
        yield conn


@pytest.fixture
def live(monkeypatch):
    """Force mode=live with a small max_held so backpressure is testable."""
    monkeypatch.setattr(ci, "effective_mode", lambda: "live")
    monkeypatch.setattr(ci, "load_config", lambda: {"max_held": 3})


async def _propose(db, **kw):
    kw.setdefault("repo", _REPO)
    # Default to a policy-valid label set (area:* + a difficulty label) so tests
    # exercising OTHER concerns (dedup, backpressure, mode) reach the code path
    # they target. Tests of the label policy itself pass `labels=` explicitly.
    kw.setdefault("labels", ["good first issue", "area:runtime"])
    return await ci._impl_contributor_issue_propose(db, **kw)


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_held_creates_row_and_approval(db, live):
    res = await _propose(
        db,
        title="Add a test for parser.chunk empty input",
        body="No test covers the empty-input branch of chunk(); add one.",
        labels=["good first issue", "area:runtime"],
        source="follow_up",
        source_follow_up_id="fu-123",
    )
    assert res["status"] == "held"
    assert res["repo"] == _REPO
    assert res["mode"] == "live"

    row = await pip.get_by_id(db, res["pending_id"])
    assert row["status"] == "held"
    assert row["title"] == "Add a test for parser.chunk empty input"
    assert row["source"] == "follow_up"
    assert row["source_ref"] == "fu-123"
    assert json.loads(row["labels"]) == ["good first issue", "area:runtime"]
    assert row["cell_domain"] == "github"
    assert row["request_id"] == res["request_id"]
    assert row["mode"] == "live"  # lever mode STAMPED at propose time (dry-run-terminal invariant)

    # linked approval row, correct type/class/context.
    appr = await ar.get_by_id(db, res["request_id"])
    assert appr is not None
    assert appr["action_type"] == "contributor_issue_post"
    assert appr["action_class"] == "irreversible"
    assert appr["status"] == "pending"
    ctx = json.loads(appr["context"])
    assert ctx["repo"] == _REPO
    assert ctx["labels"] == ["good first issue", "area:runtime"]
    assert ctx["source_follow_up_id"] == "fu-123"
    assert ctx["cell"] == ["github", "issue_create", "bulk"]


# ── source_follow_up_id resolution (close-loop ref integrity) ──────────────


async def _seed_follow_up(db, fid):
    from genesis.db.crud import follow_ups as fu

    await fu.create(db, content="c", source="test", strategy="surplus_task", id=fid)


@pytest.mark.asyncio
async def test_source_follow_up_prefix_resolves_to_canonical(db, live):
    # A tagged/prefix handle must be normalized to the canonical full id at propose
    # time, else the close-loop join (which keys canonical full ids) silently misses.
    canonical = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"  # 32-hex follow_up id
    await _seed_follow_up(db, canonical)
    res = await _propose(
        db,
        title="Harden the retry loop",
        body="Add a bounded retry to the widget fetcher.",
        source="follow_up",
        source_follow_up_id="a1b2c3d4e5f6",  # a UNIQUE 12-char prefix
    )
    assert res["status"] == "held"
    row = await pip.get_by_id(db, res["pending_id"])
    assert row["source_ref"] == canonical  # stored the canonical id, not the prefix
    ctx = json.loads((await ar.get_by_id(db, res["request_id"]))["context"])
    assert ctx["source_follow_up_id"] == canonical


@pytest.mark.asyncio
async def test_source_follow_up_ambiguous_prefix_rejected(db, live):
    # A prefix matching >1 follow_up must be rejected, never guessed — persisting a
    # ref the close-loop can't uniquely match is worse than refusing the proposal.
    await _seed_follow_up(db, "deadbeef" + "00" * 12)  # 32 hex, shared prefix
    await _seed_follow_up(db, "deadbeef" + "11" * 12)
    res = await _propose(
        db,
        title="Ambiguous ref",
        body="This proposal cites an ambiguous follow_up prefix.",
        source="follow_up",
        source_follow_up_id="deadbeef",  # matches BOTH
    )
    assert res["status"] == "error"
    assert "ambiguous" in res["reason"]
    assert await pip.list_held(db) == []  # nothing persisted


@pytest.mark.asyncio
async def test_source_follow_up_unknown_prefix_rejected(db, live):
    # A prefix-shaped ref that matches no follow_up is rejected (not stored verbatim).
    res = await _propose(
        db,
        title="Unknown ref",
        body="This proposal cites a follow_up prefix that does not exist.",
        source="follow_up",
        source_follow_up_id="cafef00d",  # prefix-shaped hex, no match
    )
    assert res["status"] == "error"
    assert "not_found" in res["reason"]
    assert await pip.list_held(db) == []


@pytest.mark.asyncio
async def test_propose_only_message_hints_dry_run(db, monkeypatch):
    monkeypatch.setattr(ci, "effective_mode", lambda: "propose_only")
    monkeypatch.setattr(ci, "load_config", lambda: {"max_held": 25})
    res = await _propose(db, title="Improve the README quickstart", body="Clarify setup steps.")
    assert res["status"] == "held"
    assert res["mode"] == "propose_only"
    assert "dry-run" in res["message"].lower()


# ── autonomous posture: require_approval=False auto-resolves at propose ─────


@pytest.mark.asyncio
async def test_auto_approved_when_require_approval_false(db, live, monkeypatch):
    """With the human gate off (this install's opt-in), propose resolves its OWN
    approval server-side so the drain treats the hold as approved without a human.
    scan_prose still ran (the row exists = it passed). The approval never sits
    `pending`, so it never surfaces as an approval request."""
    monkeypatch.setattr(ci, "require_approval", lambda: False)
    # Install-agnostic: autonomous mode pins repo to _default_repo(); on a config-less
    # CI clone that's a bare name and would error. Pin it to a valid slug.
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)
    res = await _propose(db, title="Add a util test", body="Cover the empty-input case.")
    assert res["status"] == "held"
    assert res["auto_approved"] is True
    assert "auto-approved" in res["message"].lower()

    appr = await ar.get_by_id(db, res["request_id"])
    assert appr["status"] == "approved"
    assert appr["resolved_by"] == "genesis:contributor-worklog"


@pytest.mark.asyncio
async def test_requires_approval_leaves_pending(db, live):
    """Default posture (require_approval True via the `live` fixture's config,
    which omits the key → default True): the approval stays pending for a human."""
    res = await _propose(db, title="Another task", body="Needs owner sign-off.")
    assert res["status"] == "held"
    assert res["auto_approved"] is False
    assert (await ar.get_by_id(db, res["request_id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_auto_approved_propose_only_is_dry_run_terminal(db, monkeypatch):
    """Auto-approve in propose_only: the approval is resolved, but the ROW is
    stamped propose_only → the drain will dry-run it (terminal, 0 posts). Lets a
    propose_only curator run self-complete for inspection with no human, no post."""
    monkeypatch.setattr(ci, "effective_mode", lambda: "propose_only")
    monkeypatch.setattr(ci, "load_config", lambda: {"max_held": 25})
    monkeypatch.setattr(ci, "require_approval", lambda: False)
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # install-agnostic (see above)
    res = await _propose(db, title="Dry-run task", body="Should not post.")
    assert res["status"] == "held"
    assert res["auto_approved"] is True
    assert res["mode"] == "propose_only"
    assert "dry-run" in res["message"].lower()
    row = await pip.get_by_id(db, res["pending_id"])
    assert row["mode"] == "propose_only"  # dry-run-terminal stamp preserved
    assert (await ar.get_by_id(db, res["request_id"]))["status"] == "approved"


@pytest.mark.asyncio
async def test_autonomous_pins_repo_to_default(db, live, monkeypatch):
    """Under autonomous posting the UNTRUSTED curator cannot redirect the post to
    another repo — the destination is pinned to this install's configured repo
    (Genesis vets destination, not just content)."""
    monkeypatch.setattr(ci, "require_approval", lambda: False)
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)
    res = await ci._impl_contributor_issue_propose(
        db,
        title="Redirect attempt",
        body="A clean body.",
        labels=["good first issue", "area:runtime"],
        repo="attacker/other-repo",
    )
    assert res["status"] == "held"
    assert res["repo"] == _REPO  # pinned, not attacker/other-repo
    # FIX 4 stores the repo in canonical lowercase (gh is case-insensitive; the
    # close-loop reads join COLLATE NOCASE). The pin still holds — the destination
    # is this install's configured repo, never the attacker's.
    assert (await pip.get_by_id(db, res["pending_id"]))["repo"] == _REPO.lower()


@pytest.mark.asyncio
async def test_autonomous_bare_default_repo_errors(db, live, monkeypatch):
    """Codex P2 regression: if the configured owner is absent, _default_repo() is a
    bare name (no owner/); the autonomous pin must NOT create a hold with a malformed
    --repo — the final repo value is validated AFTER pinning, so this errors with no
    row (else the drain retries an invalid `gh --repo` forever)."""
    monkeypatch.setattr(ci, "require_approval", lambda: False)
    monkeypatch.setattr(ci, "_default_repo", lambda: "GENesis-AGI")  # bare, owner absent
    res = await ci._impl_contributor_issue_propose(
        db, title="x title", body="y body", repo="WingedGuardian/GENesis-AGI"
    )
    assert res["status"] == "error"
    assert await pip.list_held(db) == []
    cur = await db.execute("SELECT COUNT(*) FROM approval_requests")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_human_posture_keeps_curator_repo(db, live, monkeypatch):
    """With the human gate ON (default), a curator-supplied repo is kept — a human
    reviews the destination before it posts, so no pin is needed."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)
    res = await ci._impl_contributor_issue_propose(
        db,
        title="Other repo task",
        body="A clean body.",
        labels=["good first issue", "area:runtime"],
        repo="WingedGuardian/GENesis-Voice",
    )
    assert res["status"] == "held"
    assert res["repo"] == "WingedGuardian/GENesis-Voice"  # not pinned under human review


# ── fail-closed sanitizer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_blocked_private_ip_creates_no_row(db, live):
    res = await _propose(
        db,
        title="Fix the timeout on the box",
        body="On the host at 192.168.1.42 the poller hangs; raise the timeout.",
    )
    assert res["status"] == "blocked"
    assert res["reasons"]  # non-empty findings
    # NO row, NO approval created.
    assert await pip.list_held(db) == []
    cur = await db.execute("SELECT COUNT(*) FROM approval_requests")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_blocked_private_ip_in_label_creates_no_row(db, live):
    """Labels egress to the public repo via `gh issue create --label`, so they
    are sanitized too — a private identifier in a LABEL must block, not just in
    the body."""
    res = await _propose(
        db,
        title="A perfectly clean title",
        body="A perfectly clean body with nothing sensitive.",
        labels=["good first issue", "seen-on-10.0.0.5"],
    )
    assert res["status"] == "blocked"
    assert res["reasons"]
    assert await pip.list_held(db) == []
    cur = await db.execute("SELECT COUNT(*) FROM approval_requests")
    assert (await cur.fetchone())[0] == 0


# ── dedup + backpressure ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_title_blocked(db, live):
    a = await _propose(db, title="Add retries to the fetch helper", body="It fails on flaky nets.")
    assert a["status"] == "held"
    # same title, different case/whitespace → duplicate.
    b = await _propose(db, title="  add   RETRIES to the fetch helper ", body="Different body.")
    assert b["status"] == "duplicate"
    assert b["existing_id"] == a["pending_id"]


@pytest.mark.asyncio
async def test_duplicate_source_ref_blocked(db, live):
    a = await _propose(db, title="First title", body="Body one.", source_follow_up_id="fu-9")
    assert a["status"] == "held"
    b = await _propose(
        db, title="Completely different title", body="Body two.", source_follow_up_id="fu-9"
    )
    assert b["status"] == "duplicate"


@pytest.mark.asyncio
async def test_dry_run_does_not_block_reproposal(db, live):
    """A dry-run hold is re-proposed under 'live' to actually post — so it must
    NOT count as a dedup collision."""
    a = await _propose(db, title="Port the config loader", body="Move it off the old path.")
    await pip.mark_dry_run(db, a["pending_id"], dry_run_at="2026-08-07T00:00:00")
    b = await _propose(db, title="Port the config loader", body="Move it off the old path.")
    assert b["status"] == "held"
    assert b["pending_id"] != a["pending_id"]


@pytest.mark.asyncio
async def test_backpressure_at_max_held(db, live):
    # max_held=3 (from the `live` fixture).
    for i in range(3):
        r = await _propose(db, title=f"Task number {i}", body=f"Body {i}.")
        assert r["status"] == "held"
    over = await _propose(db, title="One too many", body="Should be refused.")
    assert over["status"] == "backpressure"
    assert len(await pip.list_held(db)) == 3


# ── gating / validation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_off_disabled(db, monkeypatch):
    monkeypatch.setattr(ci, "effective_mode", lambda: "off")
    res = await _propose(db, title="anything", body="anything")
    assert res["status"] == "disabled"
    assert await pip.list_held(db) == []


@pytest.mark.asyncio
async def test_missing_fields_error(db, live):
    assert (await _propose(db, title="", body="b"))["status"] == "error"
    assert (await _propose(db, title="t", body=""))["status"] == "error"


@pytest.mark.asyncio
async def test_bad_source_error(db, live):
    res = await _propose(db, title="t", body="b", source="bogus")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_bad_repo_error(db, live):
    res = await _propose(db, title="t", body="b", repo="not-a-repo")
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_malformed_repo_components_rejected(db, live):
    """repo must have non-empty owner AND name — a slug with an empty component
    ('owner/', '/repo', '/', 'a//b') errors with NO row, so the drain never retries
    an invalid `gh --repo` forever (Codex round-3 P2)."""
    for bad in ("owner/", "/repo", "/", "a//b"):
        res = await ci._impl_contributor_issue_propose(db, title="t title", body="b body", repo=bad)
        assert res["status"] == "error", bad
    assert await pip.list_held(db) == []


# ── label policy (fail-closed): area:* + difficulty/env on every proposal ──


@pytest.mark.asyncio
async def test_rejected_missing_area_label(db, live, monkeypatch):
    """A proposal with a difficulty label but NO area:* domain label is rejected
    with no row — the public tracker must stay navigable by domain."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # teeth scoped to the tracker
    res = await ci._impl_contributor_issue_propose(
        db, title="A clean title", body="A clean body.", labels=["good first issue"], repo=_REPO
    )
    assert res["status"] == "rejected"
    assert "area" in res["reason"].lower()
    assert await pip.list_held(db) == []
    cur = await db.execute("SELECT COUNT(*) FROM approval_requests")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_rejected_missing_env_label(db, live, monkeypatch):
    """An area:* label but NO difficulty/environment label is rejected — every
    issue must declare a lane (good first issue / needs-genesis-instance / …)."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # teeth scoped to the tracker
    res = await ci._impl_contributor_issue_propose(
        db, title="A clean title", body="A clean body.", labels=["area:runtime"], repo=_REPO
    )
    assert res["status"] == "rejected"
    assert "difficulty" in res["reason"].lower() or "environment" in res["reason"].lower()
    assert await pip.list_held(db) == []
    cur = await db.execute("SELECT COUNT(*) FROM approval_requests")
    assert (await cur.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_rejected_no_labels(db, live, monkeypatch):
    """No labels at all → rejected (missing area is reported first)."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # teeth scoped to the tracker
    res = await ci._impl_contributor_issue_propose(
        db, title="A clean title", body="A clean body.", labels=[], repo=_REPO
    )
    assert res["status"] == "rejected"
    assert await pip.list_held(db) == []


@pytest.mark.asyncio
async def test_area_other_is_a_valid_domain(db, live, monkeypatch):
    """area:other is the escape hatch for a genuinely cross-cutting issue, so a
    well-formed proposal is never wrongly rejected."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # teeth scoped to the tracker
    res = await ci._impl_contributor_issue_propose(
        db,
        title="A cross-cutting clean title",
        body="A clean body.",
        labels=["good first issue", "area:other"],
        repo=_REPO,
    )
    assert res["status"] == "held"


@pytest.mark.asyncio
async def test_label_policy_scoped_to_tracker_repo(db, live, monkeypatch):
    """The label teeth is scoped to the configured tracker (`_default_repo()`),
    where the area:* taxonomy exists. A human-approved post to a DIFFERENT repo is
    NOT subjected to the policy (those labels may not exist there) — so an unlabeled
    cross-repo proposal is held, not rejected."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)
    res = await ci._impl_contributor_issue_propose(
        db,
        title="A clean cross-repo task",
        body="A clean body.",
        labels=[],  # no area/difficulty labels ...
        repo="WingedGuardian/GENesis-Voice",  # ... but a NON-tracker repo → teeth skipped
    )
    assert res["status"] == "held"


@pytest.mark.asyncio
async def test_label_policy_matches_tracker_by_canonical_repo(db, live, monkeypatch):
    """The scope check normalizes the repo (drops a gh `host/` prefix, casefolds), so
    a case/host VARIANT of the tracker is still policed — it can't skip the teeth by
    using a non-canonical slug for the same repo (Kimi review, 2026-08-31)."""
    monkeypatch.setattr(ci, "_default_repo", lambda: _REPO)  # WingedGuardian/GENesis-AGI
    res = await ci._impl_contributor_issue_propose(
        db,
        title="A clean title",
        body="A clean body.",
        labels=[],  # unlabeled ...
        repo="github.com/wingedguardian/genesis-agi",  # ... host-prefixed + lowercase variant → still the tracker
    )
    assert res["status"] == "rejected"
    assert await pip.list_held(db) == []


@pytest.mark.asyncio
async def test_privacy_block_precedes_label_policy(db, live):
    """Placement invariant: the label policy runs AFTER the privacy scan, so a
    proposal that is BOTH label-invalid AND carries a private identifier reports
    `blocked` (the security verdict), never `rejected` (the policy one)."""
    res = await ci._impl_contributor_issue_propose(
        db,
        title="Fix the box",
        body="On the host at 192.168.1.42 the poller hangs.",
        labels=[],  # also label-invalid, but security must win
        repo=_REPO,
    )
    assert res["status"] == "blocked"
    assert await pip.list_held(db) == []


# ── batch approve-all must NEVER sweep a held issue (public-repo write) ─────


@pytest.mark.asyncio
async def test_approve_all_pending_excludes_contributor_issue(db, live):
    """A single 'approve all' click (dashboard or Telegram cli_approve_all) must
    NOT bulk-approve held contributor-issue drafts — each is one public-repo
    post and must be approved individually. Mirrors the WS-8 email exclusion."""
    from unittest.mock import MagicMock

    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
    from genesis.db.crud import approval_requests as ar

    # A held contributor-issue draft (creates its own approval row) ...
    res = await _propose(db, title="Contributor task", body="A newcomer-friendly task.")
    issue_rid = res["request_id"]
    # ... alongside an ordinary CLI-fallback approval.
    mgr = ApprovalManager(db=db)
    cli_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="reversible",
        description="cli action",
    )
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    n = await gate.approve_all_pending(resolved_by="user")
    assert n == 1  # only the CLI approval was swept
    assert (await ar.get_by_id(db, issue_rid))["status"] == "pending"  # still held
    assert (await ar.get_by_id(db, cli_rid))["status"] == "approved"
