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


@pytest.mark.asyncio
async def test_partial_failure_auto_retries_without_edit(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A partially-failed drop's failed batch is auto-retried on a LATER scan
    WITHOUT the user editing the file. A completed sibling keeps the file's hash
    'known', so detection alone would never re-surface it (the stranding gap)."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(6))  # 6 URLs -> 2 batches
    mock_invoker.run.side_effect = [_ok(), _err("rate limited")]
    await mon.check_once()  # scan 1: batch1 ok, batch2 fails (stranded)
    assert sorted(r["status"] for r in [dict(x) for x in await (await db.execute(
        "SELECT status FROM inbox_items WHERE file_path=?", (str(fp),))).fetchall()]) == ["completed", "failed"]

    # Scan 2: file UNCHANGED. The stranded batch2 must auto-retry and succeed.
    mock_invoker.run.reset_mock()
    mock_invoker.run.side_effect = None
    mock_invoker.run.return_value = _ok()
    r2 = await mon.check_once()
    assert r2.items_new == 0
    assert r2.items_modified == 0, "must NOT be re-detected via hash — it's a retry, not a modification"
    assert r2.items_retried == 1
    assert r2.batches_dispatched == 1, "the stranded batch2 was re-dispatched"
    baseline = await inbox_items.get_evaluated_content(db, str(fp)) or ""
    for i in range(6):
        assert f"https://example.com/a{i}" in baseline, f"a{i} missing from baseline after retry"


@pytest.mark.asyncio
async def test_retry_is_cooldown_exempt(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A partial-failure retry fires even within the evaluation cooldown window
    — a retry is failure recovery, not a re-eval on a user edit, so the cooldown
    (which throttles re-evals of edited files) must not defer it."""
    cfg = InboxConfig(
        watch_path=inbox_dir, items_per_eval=3, evaluation_cooldown_seconds=3600,
    )
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=cfg, writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(), prompt_dir=tmp_path,
    )
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(6))
    mock_invoker.run.side_effect = [_ok(), _err("rate limited")]
    await mon.check_once()  # batch1 completes at clock T (within cooldown of T)

    mock_invoker.run.reset_mock()
    mock_invoker.run.side_effect = None
    mock_invoker.run.return_value = _ok()
    r2 = await mon.check_once()  # SAME clock -> still inside cooldown
    assert r2.items_retried == 1, "retry must be cooldown-exempt"
    assert r2.batches_dispatched == 1


@pytest.mark.asyncio
async def test_retry_is_bounded_by_retry_count(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A retry that KEEPS failing stops after retry_count hits the cap — the
    load-bearing bound is retry_count (not the URL-failure guard, which here
    never fires because the errors aren't 'partial_url_failure'). No infinite
    retry loop, no row proliferation."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    max_r = mon._config.max_retries
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(6))  # 2 batches
    # Scan 1: batch1 ok, batch2 fails (retry_count -> 1).
    mock_invoker.run.side_effect = [_ok(), _err("rate limited")]
    await mon.check_once()

    # Every subsequent scan the retry keeps failing; it MUST stop at the cap.
    mock_invoker.run.side_effect = None
    mock_invoker.run.return_value = _err("still rate limited")
    total_retries = 0
    for _ in range(max_r + 5):  # far more scans than the cap
        total_retries += (await mon.check_once()).items_retried
    assert total_retries <= max_r, (
        f"retried {total_retries}x, exceeds cap {max_r} — unbounded/proliferating"
    )
    # And it has stopped being a candidate (no further retries).
    assert (await mon.check_once()).items_retried == 0
    # No row proliferation: the file's row count stayed bounded (2 batches).
    n = (await (await db.execute(
        "SELECT COUNT(*) FROM inbox_items WHERE file_path=?", (str(fp),))).fetchone())[0]
    assert n <= 4, f"row proliferation: {n} rows for a 2-batch file"


@pytest.mark.asyncio
async def test_retry_respects_url_failure_storm_guard(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A file that persistently fails URL fetches (>= max_retries
    partial_url_failure in 48h) is NOT retried — the storm guard applies on the
    retry path just as on the new-files path."""
    from datetime import UTC, datetime

    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(3))
    h = compute_hash(fp)
    recent = datetime.now(UTC).isoformat()  # count_url_failures windows on REAL now
    # A completed row with the current hash keeps the file 'known' (undetected),
    # so it reaches the retry path rather than the new-files path.
    await inbox_items.create(
        db, id="done", file_path=str(fp), content_hash=h, status="completed",
        created_at=recent,
    )
    for i in range(3):  # 3 == max_retries -> storm
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash=h,
            status="pending", created_at=recent,
        )
        await inbox_items.update_status(
            db, f"puf{i}", status="failed", error_message="partial_url_failure",
        )
    r = await mon.check_once()
    assert r.items_modified == 0  # completed row keeps it known
    assert r.items_retried == 0, "storm guard must skip a persistently URL-failing file"
    assert mock_invoker.run.call_count == 0


@pytest.mark.asyncio
async def test_retry_candidate_vanished_file_is_abandoned(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A retry candidate whose SOURCE FILE was deleted is abandoned — its
    stranded failed rows are flipped to approval_invalidated so it stops being a
    candidate (no 'File vanished before retry read' every scan, forever)."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    gone = inbox_dir / "Deleted.md"  # never created on disk
    # A retriable-failed row for a file that does not exist -> pure retry
    # candidate (not detected as new/modified, since it isn't on disk).
    await inbox_items.create(
        db, id="f1", file_path=str(gone), content_hash="h", status="pending",
        created_at="2026-06-30T00:00:01+00:00",
    )
    await inbox_items.update_status(db, "f1", status="failed", error_message="rate limited")
    assert await inbox_items.get_retriable_failure_files(db, max_retries=3) == [str(gone)]

    await mon.check_once()  # retry loop hits FileNotFoundError -> abandons it

    assert await inbox_items.get_retriable_failure_files(db, max_retries=3) == [], (
        "a vanished-file candidate must be abandoned, not recur every scan"
    )
    row = await inbox_items.get_by_id(db, "f1")
    # The reason flows through end-to-end (distinct from the empty-delta case).
    assert row["error_message"] == (
        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}source file deleted"
    )
    assert mock_invoker.run.call_count == 0  # nothing dispatched (file is gone)


@pytest.mark.asyncio
async def test_retry_candidate_empty_file_is_abandoned(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A retry candidate whose file exists but is now EMPTY is abandoned too —
    another terminal state (no content to ever retry), same as a deleted file."""
    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    empty = inbox_dir / "Emptied.md"
    empty.write_text("")
    h = compute_hash(empty)
    # A completed row with the current (empty) hash keeps the file 'known', so it
    # reaches the RETRY path (not re-detected as new/modified).
    await inbox_items.create(
        db, id="done", file_path=str(empty), content_hash=h, status="completed",
        created_at="2026-06-30T00:00:01+00:00",
    )
    await inbox_items.create(
        db, id="f1", file_path=str(empty), content_hash="oldh", status="pending",
        created_at="2026-06-30T00:00:02+00:00",
    )
    await inbox_items.update_status(db, "f1", status="failed", error_message="rate limited")
    assert await inbox_items.get_retriable_failure_files(db, max_retries=3) == [str(empty)]

    await mon.check_once()

    assert await inbox_items.get_retriable_failure_files(db, max_retries=3) == []
    row = await inbox_items.get_by_id(db, "f1")
    assert row["error_message"] == (
        f"{inbox_items.APPROVAL_INVALIDATED_PREFIX}source file is empty"
    )
    assert mock_invoker.run.call_count == 0


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
async def test_resume_claim_prevents_double_dispatch_on_crash(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, monkeypatch,
):
    """A crash between dispatch and completion must NOT duplicate the eval.

    The resume pass claims each row (awaiting_approval: -> dispatching:) BEFORE
    the CC call, so a re-run (restart) finds the rows already 'dispatching:'
    (excluded from get_awaiting_approval) and does not re-dispatch them. Without
    the claim, the rows stay 'awaiting_approval:' and the next scan re-resumes
    and re-dispatches -> duplicate eval + duplicate Genesis-N file.
    """
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

    await mon.check_once()  # scan 1: parked awaiting approval
    assert len(await inbox_items.get_awaiting_approval(db)) == 2

    # Simulate a crash AFTER dispatch but BEFORE completion: the resume loop
    # claims the row first, then calls _dispatch_one_batch — stub it so the row
    # is never marked completed (as if the process died mid-eval).
    dispatch_calls: list[str] = []

    async def _crash_dispatch(item, **kw):
        dispatch_calls.append(item.id)
        return True  # "dispatched" but leaves the row in its claimed state

    monkeypatch.setattr(mon, "_dispatch_one_batch", _crash_dispatch)

    approvals["req-1"] = {"status": "approved"}
    await mon.check_once()  # scan 2: claim + (crashed) dispatch
    assert len(dispatch_calls) == 2, "both batches dispatched once on resume"
    # Claimed rows are 'dispatching:' now -> no longer awaiting.
    assert await inbox_items.get_awaiting_approval(db) == []

    await mon.check_once()  # scan 3: restart — must NOT re-dispatch the claimed rows
    assert len(dispatch_calls) == 2, (
        "claimed rows were re-dispatched after a crash (double dispatch)"
    )


@pytest.mark.asyncio
async def test_gate_error_fails_drop_not_stuck_processing(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """If the approval gate's route() raises (transient: net/DB/timeout), the
    drop's rows are failed (retriable) — NOT left stuck in 'processing' where
    they'd be invisible to detection until expire_stuck fires."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=2)
    disp = _wired(decision=AutonomousDispatchDecision(mode="blocked", reason="x"))
    disp.route = AsyncMock(side_effect=RuntimeError("gate boom"))
    mon._autonomous_dispatcher = disp
    (inbox_dir / "Genesis.md").write_text(_urls(4))  # 2 batches

    result = await mon.check_once()

    rows = [dict(r) for r in await (await db.execute(
        "SELECT status, retry_count FROM inbox_items WHERE file_path LIKE '%Genesis.md'",
    )).fetchall()]
    assert rows and all(r["status"] == "failed" for r in rows)
    # Retriable (not permanently capped) — a transient gate error should retry.
    assert all(r["retry_count"] < mon._config.max_retries for r in rows)
    assert any("gate error" in e.lower() for e in result.errors)
    assert mock_invoker.run.call_count == 0


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


_TWO_RECS = """# Inbox Evaluation — test

## https://a.com/x

**Classification:** Genesis-relevant | **Decision:** Research

### Recommendation

```yaml
action: ADAPT
next_step: "do thing A"
effort: Small
scope: V4
confidence: high
architecture_impact: extends
```

## https://b.com/y

**Classification:** Genesis-relevant | **Decision:** Research

### Recommendation

```yaml
action: WATCH
next_step: "do thing B"
effort: Small
scope: V5
confidence: medium
architecture_impact: extends
```
"""


@pytest.mark.asyncio
async def test_followup_integrity_error_does_not_abort_remaining_recs(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, monkeypatch,
):
    """If follow_ups.create raises IntegrityError (lost a dedup_key race) on one
    recommendation, the loop must keep processing the REST of the evaluation's
    recommendations — previously the exception aborted the whole loop."""
    import sqlite3

    from genesis.db.crud import follow_ups as fu

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    # Force the create() path (bypass the exists pre-check).
    monkeypatch.setattr(fu, "exists_by_dedup_key", AsyncMock(return_value=False))
    real_create = fu.create
    calls = {"n": 0}

    async def flaky_create(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.IntegrityError(
                "UNIQUE constraint failed: follow_ups.dedup_key"
            )
        return await real_create(*a, **k)

    monkeypatch.setattr(fu, "create", flaky_create)

    created = await mon._create_follow_ups_from_eval(
        evaluation_text=_TWO_RECS, batch_id="b1",
        source_files=[str(inbox_dir / "Genesis.md")],
    )
    assert calls["n"] == 2, "both recs attempted (loop not aborted by the first IntegrityError)"
    assert created == 1, "first raised+caught, second created"


@pytest.mark.asyncio
async def test_consume_approval_returns_bool(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path):
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    # No dispatcher wired → nothing to consume → True (proceed).
    assert await mon._consume_approval("req") is True
    # mark_consumed returns False (already consumed) → False.
    mon._autonomous_dispatcher = SimpleNamespace(
        approval_gate=SimpleNamespace(mark_consumed=AsyncMock(return_value=False)),
    )
    assert await mon._consume_approval("req") is False
    # mark_consumed raises (transient) → False (not swallowed silently).
    mon._autonomous_dispatcher = SimpleNamespace(
        approval_gate=SimpleNamespace(
            mark_consumed=AsyncMock(side_effect=RuntimeError("db lock")),
        ),
    )
    assert await mon._consume_approval("req") is False


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


# ── Refresh folds parked files into one batch (oscillator regression) ────


class _StatefulGate:
    """Model the REAL gate contract for the inbox site (stable approval key):
    at most ONE pending request per site; route() while pending parks on the
    existing request; cancel() clears it; the next route() after a cancel
    creates a fresh request id (in production: a fresh Telegram message).
    """

    def __init__(self, clock):
        self._clock = clock
        self.pending_id: str | None = None
        self.created_at: str = ""
        self.approved: set[str] = set()
        self.request_count = 0
        self.cancel_calls: list[str] = []

    async def route(self, request):
        if self.pending_id is None:
            self.request_count += 1
            self.pending_id = f"req-{self.request_count}"
            self.created_at = self._clock().isoformat()
        return AutonomousDispatchDecision(
            mode="blocked", reason="approval requested",
            approval_request_id=self.pending_id,
        )

    async def find_site_pending(self, *, subsystem, policy_id):
        if self.pending_id is None:
            return None
        return {"id": self.pending_id, "created_at": self.created_at}

    async def cancel(self, request_id):
        self.cancel_calls.append(request_id)
        if request_id == self.pending_id:
            self.pending_id = None
        return True

    def approve(self):
        assert self.pending_id is not None
        self.approved.add(self.pending_id)
        self.pending_id = None

    async def get_by_id(self, request_id):
        if request_id == self.pending_id:
            return {"status": "pending"}
        if request_id in self.approved:
            return {"status": "approved"}
        return {"status": "cancelled"}


def _stateful_dispatcher(clock):
    gate = _StatefulGate(clock)
    d = SimpleNamespace()
    d.route = gate.route
    d.approval_gate = SimpleNamespace(
        find_site_pending=gate.find_site_pending,
        approval_manager=SimpleNamespace(
            get_by_id=gate.get_by_id, cancel=gate.cancel,
        ),
        mark_consumed=AsyncMock(return_value=True),
    )
    return d, gate


@pytest.mark.asyncio
async def test_refresh_folds_parked_files_no_oscillation(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """Two files parked on one approval + one file edited → the refresh must
    cancel ONCE and re-park BOTH files on the fresh request.

    Without folding the parked files into the refresh batch, the
    not-refreshed file's rows are invalidated but never re-dispatched, so it
    re-surfaces as phantom-"new" next scan and the two files leapfrog:
    cancel + recreate every scan (a Telegram message each time) with NO disk
    change — the 30-minute approval nag.
    """
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    file_a = inbox_dir / "Genesis.md"
    file_b = inbox_dir / "Capabilities.md"
    file_a.write_text(_urls(2))
    file_b.write_text("https://example.com/b0\nhttps://example.com/b1")

    # Scan 1: both drops park on the single site-stable request.
    await mon.check_once()
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_a), str(file_b)}
    assert gate.request_count == 1

    # The user edits B while the approval is pending.
    file_b.write_text("https://example.com/b9")

    # Scan 2: ONE cancel, and the refreshed request covers BOTH files.
    await mon.check_once()
    assert gate.cancel_calls == ["req-1"]
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_a), str(file_b)}, (
        "parked files must fold into the refresh batch"
    )
    assert gate.request_count == 2

    # Scans 3-4, disk unchanged: detection stays quiet — no cancel, no new
    # request, no dispatch. (Unfixed: A and B alternate forever.)
    await mon.check_once()
    await mon.check_once()
    assert gate.cancel_calls == ["req-1"], "approval churned on unchanged disk"
    assert gate.request_count == 2
    assert mock_invoker.run.call_count == 0


@pytest.mark.asyncio
async def test_refresh_does_not_resurrect_deleted_parked_file(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """End-to-end: a parked file deleted from disk stays out of the refresh
    batch and does not oscillate afterwards.

    (In the full check_once flow the resume phase's vanished-file check
    invalidates the row before the fold runs — the fold's own exists() guard
    is isolated in test_fold_skips_parked_path_missing_from_disk.)
    """
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    file_a = inbox_dir / "Genesis.md"
    file_b = inbox_dir / "Capabilities.md"
    file_a.write_text(_urls(2))
    file_b.write_text("https://example.com/b0")

    await mon.check_once()  # both park on req-1
    file_a.unlink()
    file_b.write_text("https://example.com/b9")

    await mon.check_once()  # refresh: only B re-parks
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_b)}

    await mon.check_once()  # deleted file stays gone; no churn
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_b)}
    assert gate.request_count == 2


@pytest.mark.asyncio
async def test_fold_skips_parked_path_missing_from_disk(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """Isolates the fold's exists() guard in _phase_detect_changes: a parked
    row whose file is gone by the time the refresh folds (deleted mid-cycle,
    after the resume phase's own vanished-file check already ran) must be
    invalidated but NOT folded into the refresh batch."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    gate.pending_id = "req-1"
    gate.created_at = mon._clock().isoformat()

    ghost = inbox_dir / "Ghost.md"  # parked row exists; file never on disk
    await inbox_items.create(
        db, id="row-g", file_path=str(ghost), content_hash="h",
        status="processing", created_at="2026-06-30T11:00:00+00:00",
        drop_id="DG", batch_items="https://example.com/g0",
    )
    await inbox_items.update_status(
        db, "row-g", status="processing",
        error_message=f"{inbox_items.AWAITING_APPROVAL_PREFIX}req-1",
    )
    live = inbox_dir / "Live.md"
    live.write_text("https://example.com/n0")  # triggers the refresh

    new_files, modified_files = await mon._phase_detect_changes(
        inbox_dir, resumed_paths=set(),
    )

    returned = {str(p) for p in [*new_files, *modified_files]}
    assert str(live) in returned
    assert str(ghost) not in returned, "vanished parked file was folded"
    row = await inbox_items.get_by_id(db, "row-g")
    assert row["status"] == "failed"  # still invalidated, just not re-dispatched
    assert gate.cancel_calls == ["req-1"]


@pytest.mark.asyncio
async def test_folded_file_reevaluates_only_unprocessed_delta(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A folded parked file must go through the same delta batching as any
    modified file: URLs already evaluated in a prior approved run are NOT
    re-evaluated when the file is folded into a refresh batch."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    file_a = inbox_dir / "Genesis.md"
    file_a.write_text(_urls(2))  # a0, a1

    await mon.check_once()          # A parks on req-1
    gate.approve()
    await mon.check_once()          # resume: a0/a1 evaluated (baseline)
    assert mock_invoker.run.call_count == 1

    # User appends 2 new URLs -> parks on req-2 (delta batch only).
    file_a.write_text(_urls(4))     # a0..a3
    await mon.check_once()
    assert gate.request_count == 2

    # A second file arrives while req-2 is pending -> refresh folds A.
    file_b = inbox_dir / "Capabilities.md"
    file_b.write_text("https://example.com/b0")
    await mon.check_once()
    assert gate.cancel_calls == ["req-2"]
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_a), str(file_b)}

    # Approve the merged request: only the delta + the new file are evaluated.
    gate.approve()
    await mon.check_once()
    prompts = " ".join(
        c.args[0].prompt for c in mock_invoker.run.call_args_list[1:]
    )
    assert "https://example.com/a2" in prompts
    assert "https://example.com/a3" in prompts
    assert "https://example.com/b0" in prompts
    assert "https://example.com/a0" not in prompts, "re-evaluated processed URL"
