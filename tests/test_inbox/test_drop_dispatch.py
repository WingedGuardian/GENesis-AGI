"""Behavior tests for URL-level drop batching, per-batch baseline, one-approval-
per-drop, delta-correct resume, and follow-up dedup wiring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision
from genesis.cc.types import CCOutput
from genesis.db.crud import follow_ups, inbox_items
from genesis.db.schema import create_all_tables
from genesis.inbox.monitor import InboxMonitor
from genesis.inbox.types import InboxConfig
from genesis.inbox.writer import ResponseWriter


@dataclass
class _FakeClock:
    now: datetime = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.now


def _ok(text: str = "# Inbox Evaluation\n\nlinkedin evaluation result body") -> CCOutput:
    return CCOutput(
        session_id="s", text=text, model_used="sonnet", cost_usd=0.01,
        input_tokens=10, output_tokens=20, duration_ms=100, exit_code=0,
    )


def _err(msg: str = "boom") -> CCOutput:
    return CCOutput(
        session_id="", text="", model_used="sonnet", cost_usd=0.0,
        input_tokens=0, output_tokens=0, duration_ms=10, exit_code=1,
        is_error=True, error_message=msg,
    )


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def inbox_dir(tmp_path: Path) -> Path:
    d = tmp_path / "inbox"
    d.mkdir()
    return d


@pytest.fixture
def mock_invoker():
    inv = AsyncMock()
    inv.run = AsyncMock(return_value=_ok())
    return inv


@pytest.fixture
def mock_session_manager():
    sm = AsyncMock()
    sm.create_background = AsyncMock(return_value={"id": "sess-1"})
    sm.complete = AsyncMock()
    sm.fail = AsyncMock()
    return sm


def _monitor(db, inbox_dir, invoker, sm, tmp_path, *, items_per_eval=3):
    cfg = InboxConfig(
        watch_path=inbox_dir, items_per_eval=items_per_eval,
        evaluation_cooldown_seconds=0,
    )
    return InboxMonitor(
        db=db, invoker=invoker, session_manager=sm, config=cfg,
        writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(), prompt_dir=tmp_path,
    )


def _urls(n: int) -> str:
    return "\n".join(f"https://example.com/a{i}" for i in range(n))


# ── Batching (gate OFF / no dispatcher) ──────────────────────────────────


@pytest.mark.asyncio
async def test_drop_splits_into_item_batches(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    (inbox_dir / "Genesis.md").write_text(_urls(7))  # 7 URLs / 3 -> 3 batches

    result = await mon.check_once()

    assert result.batches_dispatched == 3
    assert mock_invoker.run.call_count == 3
    # One drop, three completed batch rows.
    rows = await inbox_items.get_by_file_path(db, str(inbox_dir / "Genesis.md"))
    all_rows = [dict(r) for r in await (await db.execute(
        "SELECT status, drop_id FROM inbox_items WHERE file_path LIKE '%Genesis.md'",
    )).fetchall()]
    assert len(all_rows) == 3
    assert len({r["drop_id"] for r in all_rows}) == 1
    assert all(r["status"] == "completed" for r in all_rows)
    # Baseline contains all 7 URLs.
    baseline = await inbox_items.get_evaluated_content(db, str(inbox_dir / "Genesis.md"))
    for i in range(7):
        assert f"https://example.com/a{i}" in baseline
    # Three sibling response files.
    genesis_files = list(inbox_dir.glob("Genesis-*.genesis.md"))
    assert len(genesis_files) == 3
    assert rows is not None


@pytest.mark.asyncio
async def test_partial_batch_failure_baselines_only_successes(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    # 6 URLs -> 2 batches; first succeeds, second errors.
    mock_invoker.run.side_effect = [_ok(), _err("rate limited")]
    (inbox_dir / "Genesis.md").write_text(_urls(6))

    await mon.check_once()

    baseline = await inbox_items.get_evaluated_content(db, str(inbox_dir / "Genesis.md")) or ""
    # Batch 1 (a0..a2) baselined; batch 2 (a3..a5) NOT baselined -> retriable.
    assert "https://example.com/a0" in baseline
    assert "https://example.com/a5" not in baseline
    statuses = sorted(r["status"] for r in [dict(x) for x in await (await db.execute(
        "SELECT status FROM inbox_items WHERE file_path LIKE '%Genesis.md'",
    )).fetchall()])
    assert statuses == ["completed", "failed"]


# ── One approval per drop (gate ON) ──────────────────────────────────────


def _wired(*, decision, approval_by_id=None):
    # NB: `is None` check, not `or {}` — an empty dict passed by the caller is
    # falsy and must be kept (callers mutate it after wiring to flip approval).
    if approval_by_id is None:
        approval_by_id = {}

    async def _find_site_pending(*, subsystem, policy_id):
        return None

    async def _get_by_id(request_id):
        return approval_by_id.get(request_id)

    gate = SimpleNamespace(
        find_site_pending=_find_site_pending,
        approval_manager=SimpleNamespace(
            get_by_id=_get_by_id, cancel=AsyncMock(return_value=True),
        ),
        mark_consumed=AsyncMock(return_value=True),
    )
    d = SimpleNamespace()
    d.route = AsyncMock(return_value=decision)
    d.approval_gate = gate
    return d


@pytest.mark.asyncio
async def test_one_approval_per_drop_then_resume_dispatches_all(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    approvals: dict[str, dict] = {}
    disp = _wired(
        decision=AutonomousDispatchDecision(
            mode="blocked", reason="approval requested",
            approval_request_id="req-1",
        ),
        approval_by_id=approvals,
    )
    mon._autonomous_dispatcher = disp
    (inbox_dir / "Genesis.md").write_text(_urls(6))  # 6 URLs -> 1 drop, 2 batches

    # Scan 1: ONE approval requested for the whole drop; both batches parked.
    r1 = await mon.check_once()
    assert disp.route.call_count == 1, "exactly one route() per drop"
    assert r1.batches_dispatched == 0
    assert mock_invoker.run.call_count == 0
    parked = await inbox_items.get_awaiting_approval(db)
    assert len(parked) == 2
    assert {p["drop_id"] for p in parked} == {parked[0]["drop_id"]}  # same drop

    # User approves -> scan 2 resume dispatches BOTH batches, consumes once.
    approvals["req-1"] = {"status": "approved"}
    await mon.check_once()
    assert mock_invoker.run.call_count == 2, "both batches dispatched on resume"
    assert disp.route.call_count == 1, "resume does NOT re-route"
    disp.approval_gate.mark_consumed.assert_awaited_once_with("req-1")


@pytest.mark.asyncio
async def test_gate_off_dispatches_every_batch(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    # No dispatcher wired == gate OFF: every batch dispatches directly.
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=2)
    (inbox_dir / "Genesis.md").write_text(_urls(5))  # 5 URLs / 2 -> 3 batches
    result = await mon.check_once()
    assert result.batches_dispatched == 3
    assert mock_invoker.run.call_count == 3


# ── Resume uses the persisted batch delta, not a full-file re-read ────────


@pytest.mark.asyncio
async def test_resume_uses_persisted_batch_delta_not_full_file(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """The Genesis-85 fix: an approved resume evaluates ONLY the batch's
    persisted lines, never a full re-read of the (much larger) file."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    f = inbox_dir / "Genesis.md"
    f.write_text(_urls(20))  # big file
    import hashlib
    h = hashlib.sha256(
        "\n".join(line.rstrip() for line in f.read_text().split("\n")).encode()
    ).hexdigest()
    # A parked batch that only owns 2 of the 20 URLs.
    await inbox_items.create(
        db, id="row-1", file_path=str(f), content_hash=h, status="processing",
        created_at="2026-06-30T11:00:00+00:00", drop_id="D1",
        batch_items="https://example.com/a18\nhttps://example.com/a19",
    )
    await inbox_items.update_status(
        db, "row-1", status="processing",
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-9",
    )
    mon._autonomous_dispatcher = _wired(
        decision=AutonomousDispatchDecision(mode="blocked", reason="pending", approval_request_id="req-9"),
        approval_by_id={"req-9": {"status": "approved"}},
    )

    await mon.check_once()

    assert mock_invoker.run.call_count == 1
    prompt = mock_invoker.run.call_args.args[0].prompt
    assert "https://example.com/a18" in prompt
    assert "https://example.com/a19" in prompt
    assert "https://example.com/a0" not in prompt  # NOT a full-file re-read


# ── Follow-up dedup wiring ───────────────────────────────────────────────


_REC_OUTPUT = """# Inbox Evaluation — test

## https://example.com/a0

**Classification:** Genesis-relevant | **Decision:** Research

### Recommendation

```yaml
action: ADAPT
next_step: "Wire a held-out regression gate into skill_evolution"
effort: Medium
scope: V4
confidence: high
architecture_impact: extends
```
"""


@pytest.mark.asyncio
async def test_follow_up_dedup_same_rec_created_once(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    mock_invoker.run.return_value = _ok(_REC_OUTPUT)
    # Two different files producing the SAME recommendation.
    (inbox_dir / "Genesis.md").write_text("https://example.com/a0")
    (inbox_dir / "Other.md").write_text("https://example.com/a0")

    await mon.check_once()

    rows = [dict(r) for r in await (await db.execute(
        "SELECT id FROM follow_ups WHERE source = 'inbox_evaluation'",
    )).fetchall()]
    assert len(rows) == 1, f"expected 1 deduped follow-up, got {len(rows)}"


@pytest.mark.asyncio
async def test_follow_up_dedup_key_persisted(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    mock_invoker.run.return_value = _ok(_REC_OUTPUT)
    (inbox_dir / "Genesis.md").write_text("https://example.com/a0")
    await mon.check_once()
    row = [dict(r) for r in await (await db.execute(
        "SELECT dedup_key FROM follow_ups WHERE source = 'inbox_evaluation'",
    )).fetchall()]
    assert len(row) == 1
    assert row[0]["dedup_key"]  # non-null
    assert await follow_ups.exists_by_dedup_key(db, row[0]["dedup_key"]) is True
