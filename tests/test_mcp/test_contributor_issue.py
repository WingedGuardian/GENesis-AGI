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
    return await ci._impl_contributor_issue_propose(db, **kw)


# ── happy path ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_held_creates_row_and_approval(db, live):
    res = await _propose(
        db,
        title="Add a test for parser.chunk empty input",
        body="No test covers the empty-input branch of chunk(); add one.",
        labels=["good first issue", "help wanted"],
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
    assert json.loads(row["labels"]) == ["good first issue", "help wanted"]
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
    assert ctx["labels"] == ["good first issue", "help wanted"]
    assert ctx["source_follow_up_id"] == "fu-123"
    assert ctx["cell"] == ["github", "issue_create", "bulk"]


@pytest.mark.asyncio
async def test_propose_only_message_hints_dry_run(db, monkeypatch):
    monkeypatch.setattr(ci, "effective_mode", lambda: "propose_only")
    monkeypatch.setattr(ci, "load_config", lambda: {"max_held": 25})
    res = await _propose(db, title="Improve the README quickstart", body="Clarify setup steps.")
    assert res["status"] == "held"
    assert res["mode"] == "propose_only"
    assert "dry-run" in res["message"].lower()


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
