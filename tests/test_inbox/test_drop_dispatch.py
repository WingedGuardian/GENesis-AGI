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


def _ok(
    # Dispatch mechanics are under test here and this fixture uses the shipped
    # shadow mode, where a coverage miss is observed but does not block. Exact
    # parsed-identity behavior is pinned in test_monitor/test_url_failures.
    text: str = (
        "# Inbox Evaluation\n\nlinkedin example.com evaluation "
        "result body zzcoverzz"
    ),
) -> CCOutput:
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
    # Dispatch/batching mechanics are under test here, with fixture responses
    # that deliberately do not quote their URLs — so pin the coverage gate to
    # shadow now that enforce is the default. The gate itself is pinned in
    # test_url_failures.py and by the enforce-path tests at the end of this file.
    cfg = InboxConfig(
        watch_path=inbox_dir, items_per_eval=items_per_eval,
        evaluation_cooldown_seconds=0, url_coverage_mode="shadow",
    )
    return InboxMonitor(
        db=db, invoker=invoker, session_manager=sm, config=cfg,
        writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(),
    )


def _urls(n: int) -> str:
    # Distinct URLs exercise batching without relying on prose-token coverage.
    return "\n".join(f"https://example.com/a{i}-zzcoverzz" for i in range(n))


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
        url_coverage_mode="shadow",
    )
    mon = InboxMonitor(
        db=db, invoker=mock_invoker, session_manager=mock_session_manager,
        config=cfg, writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(),
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
    # 3 rows that have EXHAUSTED their retries == persistent failure. (Rows at
    # retry_count=1 are three FIRST misses, which is not persistence and must
    # not park the file — see test_first_misses_on_distinct_urls_are_not_a_storm.)
    for i in range(3):
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash=h,
            status="pending", created_at=recent,
        )
        await inbox_items.update_status(
            db, f"puf{i}", status="failed", error_message="partial_url_failure",
            retry_count=3,
        )
    r = await mon.check_once()
    assert r.items_modified == 0  # completed row keeps it known
    assert r.items_retried == 0, "storm guard must skip a persistently URL-failing file"
    assert mock_invoker.run.call_count == 0


@pytest.mark.asyncio
async def test_modified_path_respects_url_failure_storm_guard(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A file detected as MODIFIED (genuinely-new content) that persistently
    fails URL fetches (>= max_retries partial_url_failure in 48h) must NOT be
    re-dropped every scan — the modified path needs the same storm guard the
    new-file and retry paths already have. Instead of dropping, it writes a
    completing row to advance the known hash (stopping re-detection)."""
    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    max_r = mon._config.max_retries
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(3))
    new_h = compute_hash(fp)
    recent = datetime.now(UTC).isoformat()  # count_url_failures windows on REAL now
    # >= max_retries partial_url_failures pin the file as a storm. retry_count at
    # the cap makes them NON-retriable (so the file is NOT a retry candidate) yet
    # still counted by count_url_failures — and get_all_known keeps them (failed
    # at cap) at an OLD hash, so the NEW disk content is detected as MODIFIED.
    for i in range(max_r):
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash="oldhash",
            status="pending", created_at=recent,
        )
        await inbox_items.update_status(
            db, f"puf{i}", status="failed",
            error_message="partial_url_failure", retry_count=max_r,
        )

    r = await mon.check_once()

    assert r.items_modified == 1, "file must be detected as modified, not retry"
    assert r.items_retried == 0, "rows at the retry cap are not retry candidates"
    assert r.batches_dispatched == 0, "storm guard must NOT dispatch a modified drop"
    assert mock_invoker.run.call_count == 0
    # The guard advanced the known hash to stop re-detection — via a PARKED
    # row, not a "completed" one. Nothing evaluated this content, so a
    # completed row (which reads as success everywhere downstream, and carries
    # no response_path) would be a lie; a retry-exhausted failed row blocks
    # re-detection identically and says what actually happened.
    parked = await (await db.execute(
        "SELECT status, error_message, retry_count FROM inbox_items "
        "WHERE file_path=? AND content_hash=?", (str(fp), new_h))).fetchall()
    assert len(parked) == 1, "a parking row at the new hash must be written"
    assert parked[0][0] == "failed"
    assert parked[0][1] == "retry_storm_parked"
    assert parked[0][2] >= max_r, "must be at the cap so it is not a retry candidate"
    # NOT asserted here: that get_all_known now reports the new hash. It is
    # last-row-wins ordered by created_at, and this fixture deliberately mixes
    # clocks — the storm window needs REAL now for the seeded rows, while the
    # monitor stamps the parking row from its fake clock — so the parking row
    # sorts before them. That is a fixture artifact, not guard behaviour (in
    # production both come from the same advancing clock); asserting it would
    # pin the artifact.
    # No drop was queued.
    pending = await (await db.execute(
        "SELECT id FROM inbox_items WHERE file_path=? AND status IN "
        "('pending','processing')", (str(fp),))).fetchall()
    assert pending == [], "storm guard must not leave a pending/processing drop"


@pytest.mark.asyncio
async def test_modified_under_cap_still_queues(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """The modified-path storm guard is BOUNDED: an under-cap URL-failure count
    (< max_retries) must NOT suppress a genuine modification — it still drops
    and dispatches. Guards against the >= comparison over-suppressing."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=3)
    max_r = mon._config.max_retries
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(3))
    recent = datetime.now(UTC).isoformat()
    # One under-cap failure (non-retriable at the cap so the file is modified,
    # not a retry candidate); count 1 < max_retries -> guard must NOT fire.
    await inbox_items.create(
        db, id="puf0", file_path=str(fp), content_hash="oldhash",
        status="pending", created_at=recent,
    )
    await inbox_items.update_status(
        db, "puf0", status="failed",
        error_message="partial_url_failure", retry_count=max_r,
    )

    r = await mon.check_once()

    assert r.items_modified == 1
    assert r.batches_dispatched >= 1, "an under-cap modification must still drop"
    assert mock_invoker.run.call_count >= 1


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


@pytest.mark.asyncio
async def test_resume_dispatch_reads_current_file_directives(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A resumed/approved batch re-reads the CURRENT file's standing directives
    at dispatch time — so file-scoped build intent reaches the eval even on the
    resume path, without expanding the persisted delta itself."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    f = inbox_dir / "Genesis.md"
    f.write_text("[build everything here by default]\n" + _urls(20))
    import hashlib

    h = hashlib.sha256(
        "\n".join(line.rstrip() for line in f.read_text().split("\n")).encode()
    ).hexdigest()
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
        decision=AutonomousDispatchDecision(
            mode="blocked", reason="pending", approval_request_id="req-9",
        ),
        approval_by_id={"req-9": {"status": "approved"}},
    )

    await mon.check_once()

    prompt = mock_invoker.run.call_args.args[0].prompt
    assert "https://example.com/a18" in prompt  # persisted delta, unchanged
    assert "https://example.com/a0" not in prompt  # still not a full re-read
    # The fix: standing directive surfaced on the resume path too.
    assert "build everything here by default" in prompt
    assert "Standing bracketed lines" in prompt

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


def _rec(url: str, action: str, scope: str) -> str:
    return (
        f"## {url}\n\n"
        "**Classification:** Genesis-relevant | **Decision:** Research\n\n"
        "### Recommendation\n\n"
        "```yaml\n"
        f"action: {action}\n"
        f'next_step: "step for {action}"\n'
        f"effort: Small\nscope: {scope}\n"
        "confidence: high\narchitecture_impact: extends\n"
        "```\n"
    )


_FOUR_LANES = (
    "# Inbox Evaluation — test\n\n"
    + _rec("https://ex.com/adopt", "ADOPT", "V4")
    + "\n" + _rec("https://ex.com/watch", "WATCH", "V4")
    + "\n" + _rec("https://ex.com/explore", "EXPLORE", "V4")
    + "\n" + _rec("https://ex.com/bookmark", "BOOKMARK", "V4")
)


@pytest.mark.asyncio
async def test_watch_bookmark_route_to_tabled_lane(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """WATCH/BOOKMARK → tabled (never actionable); ADOPT/EXPLORE → follow_up."""
    from genesis.db.crud import follow_ups

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    created = await mon._create_follow_ups_from_eval(
        evaluation_text=_FOUR_LANES, batch_id="b1",
        source_files=[str(inbox_dir / "Genesis.md")],
    )
    assert created == 4

    rows = [dict(r) for r in await (await db.execute(
        "SELECT content, kind FROM follow_ups WHERE source = 'inbox_evaluation'",
    )).fetchall()]
    by_action = {r["content"].split("]")[0].lstrip("["): r for r in rows}
    assert by_action["ADOPT"]["kind"] == "follow_up"
    assert by_action["EXPLORE"]["kind"] == "follow_up"
    assert by_action["WATCH"]["kind"] == "tabled"
    assert by_action["BOOKMARK"]["kind"] == "tabled"

    # The ego/actionable feed must never see the markers.
    actionable = " ".join(
        r["content"] for r in await follow_ups.get_actionable(db)
    )
    assert "[ADOPT]" in actionable and "[EXPLORE]" in actionable
    assert "[WATCH]" not in actionable and "[BOOKMARK]" not in actionable


@pytest.mark.asyncio
async def test_consume_approval_tri_state(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, caplog,
):
    """_consume_approval distinguishes the three outcomes (F5, red-team
    2026-08-18): consumed-now, already-consumed (sibling drop this pass OR
    cross-tick crash recovery — both proceed; the row claim is the at-most-once
    gate), and transient FAILURE (retried once, then ERROR — a stale
    approved-unconsumed request is rideable by a later NEW drop)."""
    import logging

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    # No dispatcher wired → nothing to consume → "no_gate" (proceed).
    assert await mon._consume_approval("req") == "no_gate"
    # First consume wins → "consumed".
    mon._autonomous_dispatcher = SimpleNamespace(
        approval_gate=SimpleNamespace(mark_consumed=AsyncMock(return_value=True)),
    )
    assert await mon._consume_approval("req") == "consumed"
    # mark_consumed returns False → "already_consumed" (normal multi-drop
    # fanout / crash recovery — NOT an error).
    mon._autonomous_dispatcher = SimpleNamespace(
        approval_gate=SimpleNamespace(mark_consumed=AsyncMock(return_value=False)),
    )
    assert await mon._consume_approval("req") == "already_consumed"
    # Transient raise then success → retried once → "consumed".
    flaky = AsyncMock(side_effect=[RuntimeError("db lock"), True])
    mon._autonomous_dispatcher = SimpleNamespace(
        approval_gate=SimpleNamespace(mark_consumed=flaky),
    )
    assert await mon._consume_approval("req") == "consumed"
    assert flaky.await_count == 2
    # Persistent failure → "failed" + ERROR (loud: rideable-approval risk).
    with caplog.at_level(logging.ERROR):
        mon._autonomous_dispatcher = SimpleNamespace(
            approval_gate=SimpleNamespace(
                mark_consumed=AsyncMock(side_effect=RuntimeError("db lock")),
            ),
        )
        assert await mon._consume_approval("req") == "failed"
    assert any("rideable" in r.message for r in caplog.records)


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
async def test_edit_while_pending_parks_on_same_request_no_churn(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """Idempotent-approval semantics (2026-08-18 user directive): an inbox
    approval covers everything outstanding at approval time, so new/changed
    content while the request is pending PARKS ONTO THE SAME REQUEST — no
    cancel, no fresh request, no new Telegram message, ever.

    (Replaces the old cancel-once-and-refold behavior, which still sent one
    new message per genuine edit and — via the phantom-modified storm seed —
    one every 30 minutes when the known-hash map went stale.)
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

    # Scan 2: NO cancel, NO new request — B's old parked rows are superseded
    # and its fresh delta parks on the SAME request; A stays parked untouched.
    await mon.check_once()
    assert gate.cancel_calls == [], "edit-while-pending must not cancel"
    assert gate.request_count == 1, "edit-while-pending must not re-request"
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_a), str(file_b)}
    b_parked = [p for p in parked if p["file_path"] == str(file_b)]
    # Supersession lock (F2, mutation-proven gap): exactly ONE live parked row
    # for the edited file — a surviving pre-edit row would double-evaluate on
    # approve.
    assert len(b_parked) == 1, (
        f"edited file must have exactly one parked row, got {len(b_parked)}"
    )
    assert all("req-1" in (p["error_message"] or "") for p in parked)
    assert "b9" in (b_parked[0]["batch_items"] or ""), (
        "the re-parked drop must carry the recomputed delta"
    )

    # Scans 3-4, disk unchanged: fully quiet.
    await mon.check_once()
    await mon.check_once()
    assert gate.cancel_calls == []
    assert gate.request_count == 1
    assert mock_invoker.run.call_count == 0

    # Approve once → everything outstanding dispatches — the fresh delta only,
    # never the superseded pre-edit one.
    gate.approve()
    await mon.check_once()
    assert mock_invoker.run.call_count >= 1
    prompts = " ".join(c.args[0].prompt for c in mock_invoker.run.call_args_list)
    assert "https://example.com/b9" in prompts
    assert "https://example.com/b0" not in prompts, (
        "superseded pre-edit delta must not dispatch"
    )


@pytest.mark.asyncio
async def test_sole_file_edit_while_pending_no_cancel_no_new_request(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """F3 lock: the SINGLE-file edit-while-pending case. If the resume pass
    ever goes back to invalidating a still-pending changed row, this file's
    live-row count hits 0 → the orphan guard cancels → a fresh request (one
    Telegram message per edit — the residual storm seed). With hold+supersede
    the request survives and the fresh delta re-parks on it."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    file_a = inbox_dir / "Genesis.md"
    file_a.write_text(_urls(1))  # a0

    await mon.check_once()  # parks on req-1
    assert gate.request_count == 1

    file_a.write_text(_urls(2))  # edit while pending: a0 + a1

    await mon.check_once()
    assert gate.cancel_calls == [], "sole-file edit must not cancel the request"
    assert gate.request_count == 1, "sole-file edit must not mint a new request"
    parked = await inbox_items.get_awaiting_approval(db)
    assert len(parked) == 1
    assert "req-1" in (parked[0]["error_message"] or "")
    assert "a1" in (parked[0]["batch_items"] or ""), "fresh delta must be parked"

    await mon.check_once()  # unchanged disk: quiet
    assert gate.request_count == 1
    assert gate.cancel_calls == []


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

    await mon.check_once()  # A's rows invalidated (vanished); B re-parks on req-1
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_b)}

    await mon.check_once()  # deleted file stays gone; no churn, no new request
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_b)}
    assert gate.request_count == 1, "no fresh request may be minted"
    assert gate.cancel_calls == [], "B still live — the request must survive"


@pytest.mark.asyncio
async def test_detect_leaves_parked_rows_alone_no_cancel(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """_phase_detect_changes with a pending approval + new content must NOT
    cancel the request nor touch parked rows — it returns the new files for
    normal record creation, and the resume phase alone owns vanished-file
    invalidation. (The old cancel+invalidate+fold block is gone.)"""
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
    live.write_text("https://example.com/n0")

    new_files, modified_files = await mon._phase_detect_changes(
        inbox_dir, resumed_paths=set(),
    )

    returned = {str(p) for p in [*new_files, *modified_files]}
    assert str(live) in returned
    assert str(ghost) not in returned
    row = await inbox_items.get_by_id(db, "row-g")
    assert row["status"] == "processing", (
        "detect must not invalidate parked rows (resume owns vanish checks)"
    )
    assert (row["error_message"] or "").startswith("awaiting_approval:")
    assert gate.cancel_calls == [], "detect must never cancel a live request"


@pytest.mark.asyncio
async def test_new_file_while_pending_joins_request_delta_only(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """A new file arriving while a request is pending parks on the SAME
    request (no cancel, no new message), and delta batching still applies:
    URLs already evaluated in a prior approved run are NOT re-evaluated."""
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

    # A second file arrives while req-2 is pending -> joins req-2. No cancel.
    file_b = inbox_dir / "Capabilities.md"
    file_b.write_text("https://example.com/b0")
    await mon.check_once()
    assert gate.cancel_calls == []
    assert gate.request_count == 2, "new file must join the pending request"
    parked = await inbox_items.get_awaiting_approval(db)
    assert {p["file_path"] for p in parked} == {str(file_a), str(file_b)}
    assert all("req-2" in (p["error_message"] or "") for p in parked)

    # Approve once: only the delta + the new file are evaluated.
    gate.approve()
    await mon.check_once()
    prompts = " ".join(
        c.args[0].prompt for c in mock_invoker.run.call_args_list[1:]
    )
    assert "https://example.com/a2" in prompts
    assert "https://example.com/a3" in prompts
    assert "https://example.com/b0" in prompts
    assert "https://example.com/a0" not in prompts, "re-evaluated processed URL"


@pytest.mark.asyncio
async def test_content_removed_while_pending_orphans_quietly(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """If the user empties a parked file while its approval is pending, the
    parked rows are superseded, the baseline advances, and the now-orphaned
    request is cancelled by the orphan guard — with NO replacement request
    and NO dispatch (nothing left to evaluate)."""
    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    disp, gate = _stateful_dispatcher(mon._clock)
    mon._autonomous_dispatcher = disp
    file_a = inbox_dir / "Genesis.md"
    file_a.write_text(_urls(1))

    await mon.check_once()  # parks on req-1
    assert gate.request_count == 1

    file_a.write_text("")   # user removes everything

    await mon.check_once()  # superseded + hash advanced (+ orphan may cancel)
    await mon.check_once()  # orphan guard has certainly run by now
    assert gate.pending_id is None, "orphaned request must be cancelled"
    assert gate.cancel_calls == ["req-1"]
    assert gate.request_count == 1, "no replacement request may be minted"
    parked = await inbox_items.get_awaiting_approval(db)
    assert parked == []
    assert mock_invoker.run.call_count == 0

    await mon.check_once()  # and it stays quiet
    assert gate.request_count == 1


# ── Build-lane hook end-to-end (real monitor -> handle_eval -> greenlight) ──


def _build_eval_text() -> str:
    """An evaluation whose single item carries a `build` verdict + build_spec."""
    import json as _json
    spec = {
        "requirements": ["Add widget"],
        "steps": [{"type": "code", "description": "write widget.py"}],
        "success_criteria": ["widget imports"],
        "risks": ["none material"],
        "intended_paths": ["src/genesis/skills/widget/"],
    }
    return (
        "# Inbox Evaluation\n\n"
        "## 1. Widget Skill\n\n"
        "### Recommendation\n\n"
        "```yaml\n"
        "action: BUILD\n"
        'next_step: "Build the widget skill"\n'
        "scope: V4\n"
        "confidence: high\n"
        "verdict: build\n"
        'verdict_reason: "clear fit"\n'
        f"build_spec: {_json.dumps(spec)}\n"
        "```\n"
    )


@pytest.mark.asyncio
async def test_build_verdict_flows_through_monitor_to_greenlight(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, monkeypatch,
):
    """A `build` verdict in a real eval batch must reach BuildLane.handle_eval
    via the monitor hook and produce a carded build_candidate — the wiring the
    unit tests can't prove (they call handle_eval directly)."""
    from unittest.mock import AsyncMock

    from genesis.autonomy.build_lane import BuildLane
    from genesis.db.crud import build_candidates

    monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path / "plans")

    gate = AsyncMock()
    gate.ensure_approval = AsyncMock(return_value=("pending", "req-1", "pending"))
    dispatcher = AsyncMock()
    lane = BuildLane(db=db, dispatcher=dispatcher, approval_gate=gate, enabled=True)

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=1)
    mon.set_build_lane(lane)
    mock_invoker.run.return_value = _ok(text=_build_eval_text())

    (inbox_dir / "Capabilities.md").write_text("https://example.com/widget")
    await mon.check_once()

    key = BuildLane.item_key("Widget Skill")
    row = await build_candidates.get_open_by_item_key(db, key)
    assert row is not None, "build verdict did not reach the lane via the monitor hook"
    assert row["verdict"] == "build"
    assert row["approval_request_id"] == "req-1"
    gate.ensure_approval.assert_awaited_once()
    assert gate.ensure_approval.await_args.kwargs["action_type"] == "build_greenlight"


@pytest.mark.asyncio
async def test_enabling_the_lane_retires_the_lane_off_build_fallback(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, monkeypatch,
):
    """Lane off -> a BUILD verdict becomes a pinned fallback follow-up. When the
    operator then enables the lane and the item is re-evaluated, the lane owns
    it (candidate + greenlight card), so the fallback must stop being
    actionable — otherwise the same build shows up twice, once stale."""
    from unittest.mock import AsyncMock

    from genesis.autonomy.build_lane import BuildLane
    from genesis.db.crud import build_candidates, follow_ups

    monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path / "plans")

    gate = AsyncMock()
    gate.ensure_approval = AsyncMock(return_value=("pending", "req-1", "pending"))
    lane = BuildLane(db=db, dispatcher=AsyncMock(), approval_gate=gate, enabled=False)

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path, items_per_eval=1)
    mon.set_build_lane(lane)
    mock_invoker.run.return_value = _ok(text=_build_eval_text())

    notepad = inbox_dir / "Capabilities.md"
    notepad.write_text("https://example.com/widget")
    await mon.check_once()

    def _widget_rows(rows):
        return [r for r in rows if "Widget Skill" in r["content"]]

    fallback = _widget_rows(await follow_ups.get_actionable(db))
    assert len(fallback) == 1, "precondition: lane-off fallback row was not created"
    assert fallback[0]["content"].startswith("[BUILD] Widget Skill")
    gate.ensure_approval.assert_not_awaited()

    # Operator enables the lane; new notepad content forces a re-evaluation.
    mon.set_build_lane(
        BuildLane(db=db, dispatcher=AsyncMock(), approval_gate=gate, enabled=True),
    )
    notepad.write_text("https://example.com/widget\n\nhttps://example.com/widget-v2")
    await mon.check_once()

    candidate = await build_candidates.get_open_by_item_key(
        db, BuildLane.item_key("Widget Skill"),
    )
    assert candidate is not None, "precondition: the live lane did not consume the item"
    gate.ensure_approval.assert_awaited_once()

    assert _widget_rows(await follow_ups.get_actionable(db)) == [], (
        "stale lane-off fallback still actionable beside the live greenlight card"
    )
    retired = await follow_ups.get_by_id(db, fallback[0]["id"])
    assert retired is not None, "the fallback must be retired, never deleted"
    assert retired["status"] == "completed"
    assert "build lane" in (retired["resolution_notes"] or "").lower()


# ── Parking is visible (975ca61b precondition) + #1952 + #1766 inbox leg ────


def _queued_alerts():
    from genesis.env import alert_queue_root
    from genesis.guardian.alert.queue import list_queued

    return [entry for _path, entry in list_queued(alert_queue_root())]


def _monitor_retries(db, inbox_dir, invoker, sm, *, max_retries):
    cfg = InboxConfig(
        watch_path=inbox_dir, items_per_eval=1, evaluation_cooldown_seconds=0,
        max_retries=max_retries,
    )
    return InboxMonitor(
        db=db, invoker=invoker, session_manager=sm, config=cfg,
        writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(),
    )


@pytest.mark.asyncio
async def test_retry_lane_storm_parks_the_file_and_alerts(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """#1952: the retry-lane storm site only logged and `continue`d, so the file
    stayed a retry candidate and was re-checked and re-skipped EVERY scan. It
    must park like the other two sites — and tell the owner."""
    from datetime import UTC, datetime

    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(2))
    h = compute_hash(fp)
    recent = datetime.now(UTC).isoformat()
    await inbox_items.create(
        db, id="done", file_path=str(fp), content_hash=h, status="completed",
        created_at=recent,
    )
    for i in range(3):  # persistent (retry-exhausted, opaque) failures
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash=h,
            status="failed", created_at=recent,
            error_message="partial_url_failure", retry_count=3,
        )
    # ...plus one still-RETRIABLE row, which is what makes the file a retry
    # candidate at all (a file whose rows are all at the cap never is).
    await inbox_items.create(
        db, id="live", file_path=str(fp), content_hash=h, status="failed",
        created_at=recent, error_message="partial_url_failure", retry_count=1,
    )
    assert str(fp) in await inbox_items.get_retriable_failure_files(db, max_retries=3)

    await mon.check_once()

    assert mock_invoker.run.call_count == 0
    assert str(fp) not in await inbox_items.get_retriable_failure_files(
        db, max_retries=3,
    ), "a storm-parked file must stop being a retry candidate"
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    assert "Genesis.md" in alerts[0]["title"] + alerts[0]["body"]

    await mon.check_once()  # next scan: parked, not re-checked, no duplicate alert
    assert mock_invoker.run.call_count == 0
    assert len([a for a in _queued_alerts() if a.get("source") == "inbox"]) == 1


@pytest.mark.asyncio
async def test_modified_path_storm_alerts_the_owner(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    from datetime import UTC, datetime

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(2))
    recent = datetime.now(UTC).isoformat()
    for i in range(3):
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash="oldhash",
            status="failed", created_at=recent,
            error_message="partial_url_failure", retry_count=3,
        )
    await mon.check_once()
    assert mock_invoker.run.call_count == 0
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1 and "Genesis.md" in alerts[0]["title"] + alerts[0]["body"]


@pytest.mark.asyncio
async def test_item_that_exhausts_its_retries_alerts_the_owner(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Retry exhaustion used to notify nobody: under enforce, an evaluation that
    never quotes its URL would park after max_retries in total silence."""
    mock_invoker.run.return_value = _ok(
        "# Inbox Evaluation\n\nA thorough answer about something else. " + "x" * 300
    )
    mon = _monitor_retries(db, inbox_dir, mock_invoker, mock_session_manager,
                           max_retries=1)
    (inbox_dir / "Genesis.md").write_text("https://example.com/only-item-9f2k\n")
    await mon.check_once()
    rows = await (await db.execute(
        "SELECT status, retry_count FROM inbox_items WHERE file_path LIKE '%Genesis.md'"
    )).fetchall()
    assert [tuple(r) for r in rows] == [("failed", 1)]
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    assert "Genesis.md" in alerts[0]["title"] + alerts[0]["body"]


@pytest.mark.asyncio
async def test_first_failure_below_the_cap_does_not_alert(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Negative control: a retriable failure is not a parked one."""
    mock_invoker.run.return_value = _ok(
        "# Inbox Evaluation\n\nA thorough answer about something else. " + "x" * 300
    )
    mon = _monitor_retries(db, inbox_dir, mock_invoker, mock_session_manager,
                           max_retries=3)
    (inbox_dir / "Genesis.md").write_text("https://example.com/only-item-9f2k\n")
    await mon.check_once()
    assert [a for a in _queued_alerts() if a.get("source") == "inbox"] == []


@pytest.mark.asyncio
async def test_network_offline_does_not_burn_a_retry(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """#1766 (inbox leg): an outage is not the item's failure. It must leave the
    retry budget untouched, or a ~90-minute outage parks in-flight items."""
    from genesis.cc.exceptions import CCNetworkOfflineError

    mock_invoker.run.side_effect = CCNetworkOfflineError("offline")
    mon = _monitor_retries(db, inbox_dir, mock_invoker, mock_session_manager,
                           max_retries=1)
    (inbox_dir / "Genesis.md").write_text("https://example.com/only-item-9f2k\n")
    await mon.check_once()
    rows = await (await db.execute(
        "SELECT status, retry_count FROM inbox_items WHERE file_path LIKE '%Genesis.md'"
    )).fetchall()
    assert len(rows) == 1 and rows[0]["retry_count"] == 0, [dict(r) for r in rows]
    assert str(inbox_dir / "Genesis.md") in await inbox_items.get_retriable_failure_files(
        db, max_retries=1,
    ), "the item must stay retriable after an outage"
    assert [a for a in _queued_alerts() if a.get("source") == "inbox"] == []


@pytest.mark.asyncio
async def test_retry_lane_park_spares_non_url_failures(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """Review S1: the storm park must abandon only the URL-failure rows. A
    retriable failure of a different kind (a crashed CC call) in the same file is
    not part of the storm and must stay retriable."""
    from datetime import UTC, datetime

    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(2))
    h = compute_hash(fp)
    recent = datetime.now(UTC).isoformat()
    await inbox_items.create(db, id="done", file_path=str(fp), content_hash=h,
                             status="completed", created_at=recent)
    for i in range(3):
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash=h, status="failed",
            created_at=recent, error_message="partial_url_failure", retry_count=3,
        )
    await inbox_items.create(
        db, id="urlfail", file_path=str(fp), content_hash=h, status="failed",
        created_at=recent, error_message="partial_url_failure", retry_count=1,
    )
    await inbox_items.create(
        db, id="crash", file_path=str(fp), content_hash=h, status="failed",
        created_at=recent, error_message="CC invocation failed: boom", retry_count=1,
    )
    await mon.check_once()
    rows = {r["id"]: r["error_message"] for r in await (await db.execute(
        "SELECT id, error_message FROM inbox_items")).fetchall()}
    assert rows["urlfail"].startswith("approval_invalidated:")
    assert not rows["crash"].startswith("approval_invalidated:"), rows["crash"]


@pytest.mark.asyncio
async def test_every_parked_item_in_one_file_is_named_in_one_alert(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Review S2+S3: N items exhausting in one scan produced ONE alert naming
    none of them (per-file dedupe key hid items 2..N). One alert per file per
    scan must name every parked item, with query strings redacted."""
    mock_invoker.run.return_value = _ok(
        "# Inbox Evaluation\n\nA thorough answer about something else. " + "x" * 300
    )
    mon = _monitor_retries(db, inbox_dir, mock_invoker, mock_session_manager,
                           max_retries=1)
    (inbox_dir / "Genesis.md").write_text(
        "https://example.com/first-item?token=SECRETQ\n\n"
        "https://example.com/second-item\n"
    )
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    text = alerts[0]["title"] + alerts[0]["body"]
    assert "example.com — line 1 (url#" in text and "example.com — line 3 (url#" in text, text
    assert "SECRETQ" not in text and "first-item" not in text
    assert "2" in alerts[0]["title"]


@pytest.mark.asyncio
async def test_a_later_scan_parking_another_item_alerts_again(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Review S2: a second item parked in a later scan must not be swallowed by
    the first alert's dedupe key."""
    mock_invoker.run.return_value = _ok(
        "# Inbox Evaluation\n\nA thorough answer about something else. " + "x" * 300
    )
    mon = _monitor_retries(db, inbox_dir, mock_invoker, mock_session_manager,
                           max_retries=1)
    f = inbox_dir / "Genesis.md"
    f.write_text("https://example.com/first-item\n")
    await mon.check_once()
    f.write_text("https://example.com/first-item\n\nhttps://example.com/second-item\n")
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 2, alerts
    assert "example.com — line 3" in alerts[1]["title"] + alerts[1]["body"]


# ── Review round 1 on #2447: alert identity ──────────────────────────────────

_UNCOVERED = "# Inbox Evaluation\n\nA thorough answer about something else. " + "x" * 300


def _monitor_cfg(db, inbox_dir, invoker, sm, **overrides):
    cfg = InboxConfig(
        watch_path=inbox_dir, evaluation_cooldown_seconds=0,
        **{"items_per_eval": 1, "max_retries": 1, **overrides},
    )
    return InboxMonitor(
        db=db, invoker=invoker, session_manager=sm, config=cfg,
        writer=ResponseWriter(watch_path=inbox_dir, timezone="UTC"),
        clock=_FakeClock(),
    )


@pytest.mark.asyncio
async def test_items_sharing_a_host_and_path_are_counted_separately(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Codex + Devin: two presigned links to one path differ only in the query,
    which the label drops. Deduplicating on the label reported one item where
    two had stopped."""
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager)
    (inbox_dir / "Genesis.md").write_text(
        "https://example.com/watch?v=AAAA\n\nhttps://example.com/watch?v=BBBB\n"
    )
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    assert "2 item" in alerts[0]["title"], alerts[0]["title"]
    body = alerts[0]["body"]
    assert body.count("example.com — line") == 2, body
    assert "AAAA" not in body and "BBBB" not in body


@pytest.mark.asyncio
async def test_every_url_of_a_multi_item_batch_is_named(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Codex: with items_per_eval > 1 one exhausted batch holds several items,
    and only the first URL was named."""
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager,
                       items_per_eval=2)
    (inbox_dir / "Genesis.md").write_text(
        "https://example.com/first-item\n\nhttps://example.com/second-item\n"
    )
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    text = alerts[0]["body"]
    assert "example.com — line 1 (url#" in text and "example.com — line 3 (url#" in text, text
    assert "2 item" in alerts[0]["title"], alerts[0]["title"]
    assert text.count("(url#") == 2, text


@pytest.mark.asyncio
async def test_path_text_never_reaches_the_alert(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Devin: a share link can carry its token in the PATH, not the query.
    No path or query text reaches the alert at all: each item is its host, its
    line in the file and its url# id (#2533, after three masking heuristics)."""
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager)
    (inbox_dir / "Genesis.md").write_text(
        "https://files.example.com/s/a8F3kLm29QzX7pRtW4/quarterly-report\n\n"
        "https://share.example.com/f/Xy7_Kp2-Qw9_Rt4-Zm1_Bn8/2026-09-26-notes\n\n"
        "https://api.example.com/k/ghp_AbCdEfGhIjKlMnOpQr/view\n"
    )
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    text = alerts[0]["title"] + alerts[0]["body"]
    for secret in ("a8F3kLm29QzX7pRtW4", "Xy7_Kp2", "AbCdEfGhIjKlMnOpQr",
                   "quarterly-report", "2026-09-26-notes"):
        assert secret not in text, (secret, text)
    for expected in ("- files.example.com — line 1 (url#",
                     "- share.example.com — line 3 (url#",
                     "- api.example.com — line 5 (url#"):
        assert expected in text, (expected, text)


@pytest.mark.asyncio
async def test_same_named_files_in_different_folders_are_told_apart(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Codex: under recursive scanning the alert showed only the basename, so
    two Genesis.md files in different folders read the same."""
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager,
                       recursive=True)
    for sub in ("alpha", "beta"):
        (inbox_dir / sub).mkdir()
        (inbox_dir / sub / "Genesis.md").write_text(f"https://example.com/{sub}-item\n")
    await mon.check_once()
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    titles = sorted(a["title"] for a in alerts)
    assert len(titles) == 2, alerts
    assert "alpha/Genesis.md" in titles[0] and "beta/Genesis.md" in titles[1], titles


@pytest.mark.asyncio
async def test_offline_failure_survives_a_failed_row_read(
    db, inbox_dir, mock_invoker, mock_session_manager, monkeypatch,
):
    """CodeRabbit: the offline branch read the row before failing it. If that
    read raised, the row stayed `processing` until the stuck-row sweep, and a
    missing row would have written retry_count 0 over a real count."""
    from genesis.cc.exceptions import CCNetworkOfflineError

    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager,
                       max_retries=3)
    (inbox_dir / "Genesis.md").write_text("https://example.com/only-item-9f2k\n")
    await mon.check_once()  # an ordinary failure: retry_count 1
    rows = await (await db.execute("SELECT retry_count FROM inbox_items")).fetchall()
    assert [r["retry_count"] for r in rows] == [1]

    async def _boom(*_a, **_k):
        raise RuntimeError("db read failed")

    monkeypatch.setattr(inbox_items, "get_by_id", _boom)
    mock_invoker.run.side_effect = CCNetworkOfflineError("offline")
    await mon.check_once()  # the retry lane re-dispatches; the network is down
    rows = await (await db.execute(
        "SELECT status, retry_count, error_message FROM inbox_items")).fetchall()
    assert [(r["status"], r["retry_count"]) for r in rows] == [("failed", 1)]
    assert mock_invoker.run.await_count == 2, "the retry must actually have run"
    assert rows[0]["error_message"].startswith("CC invocation deferred"), rows[0]["error_message"]


@pytest.mark.asyncio
async def test_ended_approval_is_asked_again_when_a_sibling_holds_the_hash(
    db, inbox_dir, mock_invoker, mock_session_manager,
):
    """Internal review of #2447: an approval that ended left its row
    ``approval_invalidated:``, which the retry lane never retries. When a
    completed sibling holds the file's current hash, detection never re-surfaces
    the file either, so the item was stranded silently. An ended approval must
    leave the row retriable, so the SAME row is asked about again."""
    from tests.test_inbox.test_monitor import (
        _error_output,
        _make_wired_dispatcher,
        _success_output,
    )

    good = (
        "# Inbox Evaluation\n## A\n**Source:** https://a.example.com/alpha\n\n"
        + "An evaluation body. " * 30
    )

    async def _run(inv):
        if "https://b.example.com/beta" in inv.prompt:
            return _error_output("boom")
        return _success_output(good)

    mock_invoker.run.side_effect = _run
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager, max_retries=3)
    (inbox_dir / "Genesis.md").write_text(
        "https://a.example.com/alpha\n\nhttps://b.example.com/beta\n"
    )
    await mon.check_once()  # no gate: alpha completes, beta fails (retry 1)

    approvals = {"req-1": {"id": "req-1", "status": "pending"}}
    from genesis.autonomy.autonomous_dispatch import AutonomousDispatchDecision

    disp = _make_wired_dispatcher(
        decision=AutonomousDispatchDecision(
            mode="blocked", reason="approval requested", approval_request_id="req-1",
        ),
        approval_by_id=approvals,
    )
    mon._autonomous_dispatcher = disp
    await mon.check_once()  # the retry lane parks beta on approval req-1
    parked = await (await db.execute(
        "SELECT id, retry_count FROM inbox_items WHERE error_message LIKE 'awaiting_approval:%'"
    )).fetchall()
    assert len(parked) == 1, "fixture: beta must be parked awaiting approval"
    beta_id, beta_retries = parked[0]["id"], parked[0]["retry_count"]
    asked = disp.route.await_count

    approvals["req-1"]["status"] = "expired"
    disp.route.return_value = AutonomousDispatchDecision(
        mode="blocked", reason="approval requested", approval_request_id="req-2",
    )
    await mon.check_once()  # the approval ended
    await mon.check_once()  # ...and the item must be asked about again

    assert disp.route.await_count > asked, "the item was stranded, never re-asked"
    rows = {r["id"]: dict(r) for r in await (await db.execute(
        "SELECT id, status, retry_count, error_message FROM inbox_items")).fetchall()}
    assert len(rows) == 2, rows  # the same row is reused, never duplicated
    assert rows[beta_id]["retry_count"] == beta_retries, rows[beta_id]
    assert rows[beta_id]["error_message"] == "awaiting_approval:req-2", rows[beta_id]


@pytest.mark.asyncio
async def test_retry_lane_storm_alert_does_not_repeat_every_scan(
    db, inbox_dir, mock_invoker, mock_session_manager, tmp_path,
):
    """Internal review of #2447: with a non-URL retriable row left behind, the
    storm guard trips again on every scan. The alert must go out when URL
    retries are actually parked, not again on each scan after the queue drains."""
    from datetime import UTC, datetime

    from genesis.env import alert_queue_root
    from genesis.guardian.alert.queue import list_queued
    from genesis.inbox.scanner import compute_hash

    mon = _monitor(db, inbox_dir, mock_invoker, mock_session_manager, tmp_path)
    fp = inbox_dir / "Genesis.md"
    fp.write_text(_urls(2))
    h = compute_hash(fp)
    recent = datetime.now(UTC).isoformat()
    await inbox_items.create(db, id="done", file_path=str(fp), content_hash=h,
                             status="completed", created_at=recent)
    for i in range(3):
        await inbox_items.create(
            db, id=f"puf{i}", file_path=str(fp), content_hash=h, status="failed",
            created_at=recent, error_message="partial_url_failure", retry_count=3,
        )
    await inbox_items.create(
        db, id="urlfail", file_path=str(fp), content_hash=h, status="failed",
        created_at=recent, error_message="partial_url_failure", retry_count=1,
    )
    await inbox_items.create(
        db, id="crash", file_path=str(fp), content_hash=h, status="failed",
        created_at=recent, error_message="CC invocation failed: boom", retry_count=1,
    )
    per_scan = []
    for _ in range(3):
        await mon.check_once()
        queued = [(p, e) for p, e in list_queued(alert_queue_root())
                  if e.get("source") == "inbox"]
        per_scan.append(len(queued))
        for p, _e in queued:  # the awareness tick drains the queue between scans
            p.unlink()
    assert per_scan == [1, 0, 0], per_scan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "must_show"),
    [
        # Two URL-free notes batched together: the flattened text named only
        # the first note's first line.
        ("Look into the new retrieval paper\n\nCompare our reranker to theirs\n",
         ["a note — line 1", "a note — line 3"]),
        # Two annotated items sharing one URL: URL extraction deduplicated them.
        ("https://example.com/shared-post why it matters for memory\n\n"
         "https://example.com/shared-post the pricing section\n",
         ["example.com — line 1", "example.com — line 3"]),
    ],
)
async def test_every_logical_item_of_a_batch_gets_its_own_line(
    db, inbox_dir, mock_invoker, mock_session_manager, content, must_show,
):
    """Codex (#2447 round 2): labels were derived from the flattened batch text,
    so a batch of URL-free notes, or of annotated items sharing a URL,
    reported fewer items than stopped."""
    from tests.test_inbox.test_monitor import _error_output

    mock_invoker.run.return_value = _error_output("boom")
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager,
                       items_per_eval=2)
    (inbox_dir / "Genesis.md").write_text(content)
    await mon.check_once()
    rows = await (await db.execute(
        "SELECT status, retry_count FROM inbox_items")).fetchall()
    assert [(r["status"], r["retry_count"]) for r in rows] == [("failed", 1)], (
        "fixture: both items must sit in ONE exhausted row"
    )
    alerts = [a for a in _queued_alerts() if a.get("source") == "inbox"]
    assert len(alerts) == 1, alerts
    assert "2 item" in alerts[0]["title"], alerts[0]["title"]
    body = alerts[0]["body"]
    lines = [ln for ln in body.splitlines() if ln.startswith("- ")]
    assert len(lines) == 2, body
    for expected, line in zip(must_show, lines, strict=True):
        assert expected in line, (expected, line)


@pytest.mark.asyncio
async def test_a_failed_alert_enqueue_logs_what_stopped(
    db, inbox_dir, mock_invoker, mock_session_manager, monkeypatch, caplog,
):
    """#2447 class audit: the park buffer is cleared before the enqueue, and a
    row at the cap is never re-dispatched, so if the enqueue fails the log line
    is the only record of what stopped. It must name the items."""
    import genesis.guardian.alert.queue as alert_queue

    def _lost(*_a, **_k):
        # The real contract: enqueue_alert never raises; a failed write
        # returns False (Codex, #2447 round 3).
        return False

    monkeypatch.setattr(alert_queue, "enqueue_alert", _lost)
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager)
    (inbox_dir / "Genesis.md").write_text("https://example.com/lost-item-7c1\n")
    with caplog.at_level("ERROR"):
        await mon.check_once()
    errors = [r for r in caplog.records if r.levelname == "ERROR"
              and "parked-alert enqueue failed" in r.getMessage()]
    assert len(errors) == 1, [r.getMessage() for r in caplog.records]
    assert "example.com — line 1" in errors[0].getMessage()
    assert "Genesis.md" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_a_deduplicated_alert_is_not_logged_as_lost(
    db, inbox_dir, mock_invoker, mock_session_manager, caplog,
):
    """Negative control: False also means "already queued under this key", which
    is not a loss and must not be reported as one."""
    mock_invoker.run.return_value = _ok(_UNCOVERED)
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager)
    (inbox_dir / "Genesis.md").write_text("https://example.com/dup-item-4d2\n")
    await mon.check_once()
    assert len([a for a in _queued_alerts() if a.get("source") == "inbox"]) == 1
    key = next(a for a in _queued_alerts() if a.get("source") == "inbox")["dedupe_key"]
    # Re-flush the same parked set: the live entry dedupes it.
    mon._park_buffer = {}
    row = (await (await db.execute("SELECT id FROM inbox_items")).fetchone())["id"]
    mon._alert_parked(
        str(inbox_dir / "Genesis.md"), reason="retries_exhausted", detail="d",
        item_id=row, labels=["x"],
    )
    with caplog.at_level("ERROR"):
        mon._flush_park_alerts()
    assert len([a for a in _queued_alerts() if a.get("source") == "inbox"]) == 1
    assert not [r for r in caplog.records if "enqueue failed" in r.getMessage()], key


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [1, 3])
async def test_offline_failure_keeps_a_row_below_a_lowered_cap(
    db, inbox_dir, mock_invoker, mock_session_manager, max_retries,
):
    """#2447 review: a row whose count dates from an older, higher cap must stay
    retriable when an outage fails it, including at max_retries=1 (count 0)."""
    from genesis.cc.exceptions import CCNetworkOfflineError
    from genesis.inbox.types import InboxItem

    mock_invoker.run.side_effect = CCNetworkOfflineError("offline")
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager,
                       max_retries=max_retries)
    f = inbox_dir / "Genesis.md"
    f.write_text("https://example.com/old-cap-item\n")
    await inbox_items.create(
        db, id="old", file_path=str(f), content_hash="h", status="processing",
        created_at="2026-09-01T00:00:00+00:00", retry_count=max_retries + 2,
    )
    item = InboxItem(
        id="old", file_path=str(f), content="https://example.com/old-cap-item",
        content_hash="h", detected_at="2026-09-01T00:00:00+00:00",
    )
    await mon._run_one_batch(
        item, model="sonnet", effort="high", system_prompt="",
        now_iso="2026-09-01T00:00:00+00:00", errors=[],
    )
    row = await inbox_items.get_by_id(db, "old")
    assert row["status"] == "failed", row
    assert row["retry_count"] == max_retries - 1, row


def test_item_labels_never_carry_path_or_query_text():
    """No part of a URL but its host reaches the label, whatever the path holds."""
    content = "https://drive.example.com/d/1BxiMVs0XRA5nFMdKvB/edit?usp=sharing&tok=Z9\n"
    lines = ["# notes", "https://drive.example.com/d/1BxiMVs0XRA5nFMdKvB/edit?usp=sharing&tok=Z9"]
    (label,) = InboxMonitor._item_labels(content, lines)
    assert label.startswith("drive.example.com — line 2 (url#"), label
    for fragment in ("/d/", "1BxiMVs0", "edit", "usp", "tok", "Z9"):
        assert fragment not in label, (fragment, label)


@pytest.mark.parametrize(
    ("content", "lines", "expected"),
    [
        # A short note that is a substring of an EARLIER line: exact match wins.
        ("ai", ["# old", "ai is cool https://news.example.com/abc", "", "ai"], "line 4"),
        # A URL that prefixes an earlier, longer URL: exact line wins.
        ("https://x.example.com/post/123",
         ["https://x.example.com/post/12345 read", "", "https://x.example.com/post/123"],
         "line 3"),
        # Identical text twice: the later copy is the new, parked one.
        ("read later", ["read later", "", "read later"], "line 3"),
        # The exact line is EARLIER than a later line that merely contains it:
        # exact still wins over the later substring.
        ("ai", ["# old", "ai", "", "ai is cool https://news.example.com/abc"], "line 2"),
        # No exact line (the file was edited since): the LAST containing line.
        ("https://x.example.com/a",
         ["see https://x.example.com/a", "", "later https://x.example.com/a again"],
         "line 3"),
    ],
)
def test_the_line_found_is_the_item_not_an_earlier_lookalike(content, lines, expected):
    """Internal review (#2533): a first-substring search sent the owner to an
    earlier, different line, and the alert tells them to edit that line."""
    (label,) = InboxMonitor._item_labels(content, lines)
    assert f"— {expected} (" in label, label


def test_items_sharing_an_annotation_each_get_their_own_line():
    """Round 2 (#2533, Codex + Devin): two items that open with the same
    annotation but carry different links were both sent to the later
    annotation's line, because only the first line located the item."""
    lines = ["read later", "https://a.example.com/1", "", "read later", "https://b.example.com/2"]
    (first,) = InboxMonitor._item_labels("read later\nhttps://a.example.com/1", lines)
    (second,) = InboxMonitor._item_labels("read later\nhttps://b.example.com/2", lines)
    assert first.startswith("a.example.com — line 1 ("), first
    assert second.startswith("b.example.com — line 4 ("), second


def test_an_edited_annotation_falls_back_to_the_link_line():
    """The whole block no longer matches (its annotation was edited since):
    the link's own line locates it, not a lookalike annotation elsewhere."""
    lines = ["read later", "https://a.example.com/1", "", "read later!", "https://b.example.com/2"]
    (label,) = InboxMonitor._item_labels("read later\nhttps://b.example.com/2", lines)
    assert label.startswith("b.example.com — line 5 ("), label


def test_a_legacy_whole_file_row_locates_each_url_on_its_own_line():
    """A legacy row can hold a whole file; each URL must point at its own line,
    not all at the file's first line."""
    content = "intro\nhttps://a.example.com/1\n\nhttps://b.example.com/2"
    lines = content.split("\n")
    labels = InboxMonitor._item_labels(content, lines, legacy=True)
    assert labels[0].startswith("a.example.com — line 2 ("), labels
    assert labels[1].startswith("b.example.com — line 4 ("), labels


def test_notes_stay_distinguishable_when_the_file_cannot_be_read():
    one = InboxMonitor._item_labels("look into retrieval", [])
    two = InboxMonitor._item_labels("compare rerankers", [])
    assert one != two and all("(note#" in label for label in one + two), (one, two)


def test_a_schemeless_url_with_a_scheme_in_its_query_keeps_its_host():
    (label,) = InboxMonitor._item_labels(
        "example.com/r?u=https://other.example.com", ["example.com/r?u=https://other.example.com"],
    )
    assert label.startswith("example.com — line 1 ("), label


def test_notes_sharing_a_first_line_get_distinct_ids():
    """Round 3 (#2533, Codex): the note id hashed only the first line."""
    (one,) = InboxMonitor._item_labels("Read this\nalpha", [])
    (two,) = InboxMonitor._item_labels("Read this\nbeta", [])
    assert one != two, one


def test_a_multi_link_item_uses_its_own_block_not_an_earlier_lone_link():
    """Round 3 (#2533, Codex): a line with several links located each link
    separately, so an older line holding one of them alone won."""
    lines = ["https://a.example.com/x", "", "compare https://a.example.com/x https://b.example.com/y"]
    labels = InboxMonitor._item_labels(lines[2], lines)
    assert all("— line 3 (" in label for label in labels), labels


def test_identical_items_parked_together_get_distinct_lines_in_file_order():
    """Round 3 (#2533, premise check): two parked items with identical text
    were both sent to the last copy's line."""
    claimed: set[int] = set()
    lines = ["read later", "", "read later"]
    second = InboxMonitor._item_labels("read later", lines, claimed=claimed)
    first = InboxMonitor._item_labels("read later", lines, claimed=claimed)
    assert "— line 3 (" in second[0] and "— line 1 (" in first[0], (first, second)


@pytest.mark.asyncio
async def test_the_flush_reads_each_inbox_file_once(db, inbox_dir, mock_invoker, mock_session_manager, monkeypatch):
    """Round 3 (#2533, Codex): the file was re-read once per parked batch."""
    mon = _monitor_cfg(db, inbox_dir, mock_invoker, mock_session_manager)
    f = inbox_dir / "Genesis.md"
    f.write_text("https://a.example.com/1\n\nhttps://b.example.com/2\n")
    reads: list[str] = []
    real = Path.read_bytes

    def counting(self):
        reads.append(str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counting)
    mon._park_buffer = {}
    for rid, text in (("r1", "https://a.example.com/1"), ("r2", "https://b.example.com/2")):
        mon._alert_parked(str(f), reason="retries_exhausted", detail="d", item_id=rid, texts=[text])
    resolved = mon._resolve_park_labels(str(f), mon._park_buffer[(str(f), "retries_exhausted")]["items"])
    assert [p for p in reads if p == str(f)] == [str(f)]
    assert "— line 1 (" in resolved["r1"][0] and "— line 3 (" in resolved["r2"][0], resolved


def test_identical_rows_parked_in_one_file_get_lines_in_row_order(tmp_path):
    """The earlier-parked row gets the earlier copy: rows are labelled newest
    first, each taking the last line still free."""
    f = tmp_path / "Genesis.md"
    f.write_text("read later\n\nread later\n")
    mon = InboxMonitor.__new__(InboxMonitor)
    items = {"r1": {"texts": ["read later"], "legacy": False},
             "r2": {"texts": ["read later"], "legacy": False}}
    resolved = mon._resolve_park_labels(str(f), items)
    assert "— line 1 (" in resolved["r1"][0] and "— line 3 (" in resolved["r2"][0], resolved


def test_a_loose_match_never_takes_a_line_from_an_exact_owner(tmp_path):
    """Round 3 audit: a stale note matching a URL line by substring claimed it,
    and the URL item still in the file lost its line."""
    f = tmp_path / "Genesis.md"
    f.write_text("https://ai.example.com/x\n")
    mon = InboxMonitor.__new__(InboxMonitor)
    items = {"r1": {"texts": ["https://ai.example.com/x"], "legacy": False},
             "r2": {"texts": ["ai"], "legacy": False}}
    resolved = mon._resolve_park_labels(str(f), items)
    assert "— line 1 (" in resolved["r1"][0], resolved


def test_two_links_on_one_line_of_a_legacy_row_both_get_that_line():
    """Round 3 audit: inside one legacy item, the first link's claim blocked a
    second link on the same line."""
    lines = ["compare https://a.example.com/1 https://b.example.com/2", "https://c.example.com/3"]
    labels = InboxMonitor._item_labels("\n".join(lines), lines, legacy=True)
    assert [lbl.split(" (")[0] for lbl in labels] == [
        "a.example.com — line 1", "b.example.com — line 1", "c.example.com — line 2"], labels


def test_a_legacy_row_with_one_link_points_at_the_link_line():
    lines = ["intro", "https://a.example.com/1"]
    (label,) = InboxMonitor._item_labels("\n".join(lines), lines, legacy=True)
    assert label.startswith("a.example.com — line 2 ("), label


def test_a_claimed_block_reserves_every_line_it_covers(tmp_path):
    """Round 4 (Codex): an older bare URL was sent to a line inside a newer
    multi-line item that repeats it."""
    f = tmp_path / "Genesis.md"
    f.write_text("https://a.example.com/x\n\nread later\nhttps://a.example.com/x\n")
    mon = InboxMonitor.__new__(InboxMonitor)
    items = {"old": {"texts": ["https://a.example.com/x"], "legacy": False},
             "new": {"texts": ["read later\nhttps://a.example.com/x"], "legacy": False}}
    resolved = mon._resolve_park_labels(str(f), items)
    assert "— line 3 (" in resolved["new"][0] and "— line 1 (" in resolved["old"][0], resolved


def test_notes_differing_only_in_indentation_stay_distinct():
    """Round 4 (Codex): stripping every line made two notes identical."""
    lines = ["Example", "  indented", "", "Example", " indented"]
    (label,) = InboxMonitor._item_labels("Example\n  indented", lines)
    assert "— line 1 (" in label, label


def test_a_copy_below_the_evaluated_prefix_is_never_the_parked_item(tmp_path):
    """Round 4 (Devin): evaluation reads the first 50 KB; the locator read the
    whole file and preferred a later copy beyond it."""
    f = tmp_path / "Genesis.md"
    filler = "x" * 100 + "\n"
    # The blank line makes the late copy a valid item start, so only the
    # 50 KB limit can exclude it.
    f.write_text("https://a.example.com/x\n" + filler * 600 + "\nhttps://a.example.com/x\n")
    mon = InboxMonitor.__new__(InboxMonitor)
    resolved = mon._resolve_park_labels(
        str(f), {"r": {"texts": ["https://a.example.com/x"], "legacy": False}})
    assert "— line 1 (" in resolved["r"][0], resolved


def test_line_numbers_count_only_real_newlines(tmp_path):
    """Round 4 (Codex): a NEL or U+2028 inside prose shifted every later line."""
    f = tmp_path / "Genesis.md"
    f.write_bytes("some\u0085prose here\n\nhttps://a.example.com/x\n".encode("utf-8"))
    mon = InboxMonitor.__new__(InboxMonitor)
    resolved = mon._resolve_park_labels(
        str(f), {"r": {"texts": ["https://a.example.com/x"], "legacy": False}})
    assert "— line 3 (" in resolved["r"][0], resolved


def test_a_one_line_item_never_matches_inside_a_larger_item(tmp_path):
    """Round 4 audit: a bare URL matched the last line of an annotated item
    and claimed it, so the two items swapped lines."""
    f = tmp_path / "Genesis.md"
    f.write_text("https://a.example.com/x\n\nread later\nhttps://a.example.com/x\n")
    mon = InboxMonitor.__new__(InboxMonitor)
    items = {"old": {"texts": ["read later\nhttps://a.example.com/x"], "legacy": False},
             "new": {"texts": ["https://a.example.com/x"], "legacy": False}}
    resolved = mon._resolve_park_labels(str(f), items)
    assert "— line 3 (" in resolved["old"][0] and "— line 1 (" in resolved["new"][0], resolved


@pytest.mark.parametrize("sep", ["\x85", "\x0c", " ", "\x0b"])
def test_a_separator_ending_a_line_keeps_the_physical_numbers(tmp_path, sep):
    """Round 4 audit: a separator at a line's end discarded the whole map."""
    f = tmp_path / "Genesis.md"
    f.write_bytes(f"some prose{sep}\n\nhttps://a.example.com/x\n".encode())
    mon = InboxMonitor.__new__(InboxMonitor)
    resolved = mon._resolve_park_labels(
        str(f), {"r": {"texts": ["https://a.example.com/x"], "legacy": False}})
    assert "— line 3 (" in resolved["r"][0], resolved
