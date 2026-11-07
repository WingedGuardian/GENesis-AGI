"""Tests for the container alert-queue drainer — genesis.runtime.init.alert_drain.

Covers the critical cross-module seam WARNING-2 flagged: the ``_send`` closure's
mapping of outreach status → terminal(unlink)/transient(keep), the
identity-derived topic, lazy pipeline resolution, and the unconditional ``wire``.
"""

from __future__ import annotations

import pytest

from genesis.guardian.alert import queue as q
from genesis.outreach.types import OutreachResult, OutreachStatus
from genesis.runtime.init import alert_drain


class _FakePipeline:
    """Stand-in for OutreachPipeline.submit_raw returning a scripted status."""

    def __init__(self, status: OutreachStatus) -> None:
        self._status = status
        self.calls: list = []

    async def submit_raw(self, text, request, **kw) -> OutreachResult:
        self.calls.append((text, request))
        return OutreachResult(
            outreach_id="x",
            status=self._status,
            channel="telegram",
            message_content=text,
            governance_result=None,
        )


class _RT:
    def __init__(self, pipeline=None, loop=None) -> None:
        self._outreach_pipeline = pipeline
        self._awareness_loop = loop


def _enqueue(root, **kw):
    kw.setdefault("severity", "emergency")
    kw.setdefault("source", "backup")
    kw.setdefault("title", "Backup failed")
    kw.setdefault("body", "disk full")
    q.enqueue_alert(root, **kw)


@pytest.mark.asyncio
async def test_delivered_unlinks_and_uses_identity_topic(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root, dedupe_key="backup:k1")
    pipe = _FakePipeline(OutreachStatus.DELIVERED)

    await alert_drain._make_drainer(_RT(pipeline=pipe))()

    assert len(pipe.calls) == 1
    _, req = pipe.calls[0]
    assert req.topic == "alert:backup:k1"  # identity, not bare source
    assert req.category.value == "blocker"
    assert req.signal_type == "queued_alert"
    assert q.list_queued(root) == []  # terminal → unlinked


@pytest.mark.asyncio
async def test_rejected_is_terminal_unlinks(tmp_path, monkeypatch):
    # REJECTED (outreach dedup) must NOT wedge the entry forever.
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root, dedupe_key="backup:k1")
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.REJECTED)))()
    assert q.list_queued(root) == []


@pytest.mark.asyncio
async def test_held_is_terminal_unlinks(tmp_path, monkeypatch):
    """An email-gated alert is HELD, and the hold OWNS delivery from there.

    `pipeline._deliver` records a durable pending row and returns HELD; the WS-8
    resolution watcher delivers it on approval. `submit_raw`'s dedup consults
    DELIVERED history only, so it does NOT suppress a second hold — keeping the
    entry mints a fresh pending row every tick, and approving them delivers the
    same alert once per hold. That is the same duplicate-page class as #1781,
    arriving through the gate instead of through a second retrier.

    `resilience/outreach_recovery.py` already treats HELD as terminal and its
    comment says it mirrors this drain; until now it did not.
    """
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root, dedupe_key="backup:k1")
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.HELD)))()
    assert q.list_queued(root) == []


@pytest.mark.asyncio
async def test_ignored_is_terminal_unlinks(tmp_path, monkeypatch):
    """IGNORED is the pipeline's own "permanent, stop retrying" status.

    Its comments say so twice — it returns IGNORED instead of FAILED precisely
    because "the drain treats FAILED as transient and retries it" — and
    `outreach_recovery` discards IGNORED as a permanent non-delivery. Until now
    this drain retried it anyway. On an install whose blocker channel has no
    registered adapter, every queued alert resolves IGNORED forever: an ERROR
    log per entry per tick until the queue's 14-day prune silently discarded
    the alerts with no record that they were never delivered.
    """
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root, dedupe_key="backup:k2")
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.IGNORED)))()
    assert q.list_queued(root) == []


@pytest.mark.asyncio
async def test_a_terminal_non_delivery_is_recorded_not_silent(tmp_path, monkeypatch, caplog):
    """Unlinking on IGNORED is the honest cost of stopping the retry loop, and
    review objected to it: a missing adapter now DROPS the alert where a config
    fix could previously still recover it.

    The mechanism is real, but the old behaviour did not preserve the alert
    either — it retried forever and the queue's 14-day prune deleted it anyway.
    So the choice is a silent loss in 14 days versus a loud one now, and
    `outreach_recovery` already chose loud for this same status (it calls
    `mark_discarded` with a reason). The alert queue has no such call, so the
    log is where the disposition is recorded — and `queue.drain` unlinks on
    True while saying nothing, so without this a discarded alert and a
    delivered one leave the identical trace: none.
    """
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root, dedupe_key="backup:k3")
    with caplog.at_level("WARNING"):
        await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.IGNORED)))()
    assert q.list_queued(root) == []
    assert any("discarded UNDELIVERED" in r.message for r in caplog.records), (
        f"the drop must be recorded, not silent: {[r.message for r in caplog.records]}"
    )
    # A DELIVERED entry is unlinked just as silently as before — the new line
    # must fire on non-delivery only, or it becomes noise that gets tuned out.
    # DELIVERED unlinks as silently as before, and so does REJECTED: every
    # REJECTED cause is a DECISION, not a lost page — dedup rejects because the
    # alert already delivered, governance DENY because policy said no. Warning
    # on those sends an operator hunting a page that arrived (review, at head).
    for silent in (OutreachStatus.DELIVERED, OutreachStatus.REJECTED):
        caplog.clear()
        _enqueue(root, dedupe_key=f"backup:{silent.value}")
        with caplog.at_level("WARNING"):
            await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(silent)))()
        assert not any("discarded UNDELIVERED" in r.message for r in caplog.records), (
            f"{silent.value} is a decision, not a lost page: "
            f"{[r.message for r in caplog.records]}"
        )


@pytest.mark.asyncio
async def test_failed_keeps_for_retry(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root)
    await alert_drain._make_drainer(_RT(pipeline=_FakePipeline(OutreachStatus.FAILED)))()
    assert len(q.list_queued(root)) == 1  # transient → kept


@pytest.mark.asyncio
async def test_unwired_pipeline_keeps_entry(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    _enqueue(root)
    await alert_drain._make_drainer(_RT(pipeline=None))()  # outreach not up yet
    assert len(q.list_queued(root)) == 1  # kept, never lost


@pytest.mark.asyncio
async def test_empty_queue_is_noop(tmp_path, monkeypatch):
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    # No entries — drainer runs clean, no pipeline call.
    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    await alert_drain._make_drainer(_RT(pipeline=pipe))()
    assert pipe.calls == []


def test_wire_noops_without_loop():
    alert_drain.wire(_RT(loop=None))  # must not raise


def test_wire_sets_drainer_when_loop_present():
    installed = {}

    class _Loop:
        def set_alert_queue_drainer(self, fn):
            installed["fn"] = fn

    alert_drain.wire(_RT(loop=_Loop()))
    assert callable(installed.get("fn"))


@pytest.mark.asyncio
async def test_drain_request_opts_out_of_pipeline_deferral(tmp_path, monkeypatch):
    """THE #1781 lock: the drain's request must carry defer_retry=False.

    This queue is the alert's durable retrier (14-day file queue, retried
    every awareness tick). If the pipeline ALSO defers a failed send to the
    recovery worker, two independent retriers own one delivery — MEASURED
    2026-09-05: recovery delivered the OOM alert at 08:31:57 and the kept
    queue entry resent it at 08:35:42, one alert paged twice. The inverse
    design (unlink the entry on deferral) was reviewed and REJECTED: recovery
    discards a row after ~82 minutes of backoff, so it trades the duplicate
    for a DROPPED page on any longer outage.
    """
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: tmp_path)
    _enqueue(tmp_path, dedupe_key="watchgod:oom:155")
    pipeline = _FakePipeline(OutreachStatus.FAILED)
    await alert_drain._make_drainer(_RT(pipeline=pipeline))()
    (_, request) = pipeline.calls[0]
    assert request.defer_retry is False
    # And FAILED keeps the entry — this queue stays the single owner.
    assert len(list(tmp_path.glob("*.json"))) == 1


class _GovernedPipeline:
    """submit_raw stand-in that runs the REAL GovernanceGate dedup against a
    real DB and, on delivery, writes the same history row the pipeline writes
    (outreach/pipeline.py: signal_type, topic, category, content_hash(context),
    delivered_at). Exercises the drain's real request through the real gate."""

    def __init__(self, db) -> None:
        from genesis.outreach.config import OutreachConfig, QuietHours
        from genesis.outreach.governance import GovernanceGate

        self._db = db
        self._gate = GovernanceGate(
            OutreachConfig(
                quiet_hours=QuietHours(start="22:00", end="07:00"),
                channel_preferences={"default": "telegram"},
                thresholds={},
                max_daily=50,
                surplus_daily=1,
                content_daily=3,
                notification_daily=50,
                morning_report_time="07:00",
                engagement_timeout_hours=24,
                engagement_poll_minutes=60,
            ),
            db,
        )
        self.statuses: list[OutreachStatus] = []

    async def submit_raw(self, text, request, **kw) -> OutreachResult:
        from datetime import UTC, datetime

        from genesis.db.crud import outreach as outreach_crud
        from genesis.outreach.governance import content_hash

        if await self._gate.is_duplicate(request):
            status = OutreachStatus.REJECTED
        else:
            now = datetime.now(UTC).isoformat()
            rid = f"r{len(self.statuses)}"
            await outreach_crud.create(
                self._db,
                id=rid,
                signal_type=request.signal_type or request.category.value,
                topic=request.topic,
                category=request.category.value,
                salience_score=request.salience_score,
                channel="telegram",
                message_content=text,
                created_at=now,
                content_hash=content_hash(request.context),
            )
            await outreach_crud.record_delivery(self._db, rid, delivered_at=now)
            status = OutreachStatus.DELIVERED
        self.statuses.append(status)
        return OutreachResult(
            outreach_id="x", status=status, channel="telegram",
            message_content=text, governance_result=None,
        )


@pytest.fixture
async def outreach_db():
    import aiosqlite

    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


# Shaped like a real queued backup page: the drain prepends the title, so the
# per-run lines sit past character 200 and two DIFFERENT failures that share
# their opening share a content hash.
_LONG_TITLE = "Backup alert (Telegram send failed)"
_LONG_BODY = (
    "Genesis backup FAILED on the host. Reason: the encrypted push to the "
    "backups repository was rejected by the remote after three attempts, so "
    "this run's snapshot is local only and the off-site copy is stale. "
)


@pytest.mark.asyncio
async def test_distinct_keys_sharing_a_prefix_both_deliver(tmp_path, monkeypatch, outreach_db):
    from genesis.outreach.governance import content_hash

    a = f"{_LONG_TITLE}\n\n{_LONG_BODY}Detail: first failure"
    b = f"{_LONG_TITLE}\n\n{_LONG_BODY}Detail: second, different failure"
    assert content_hash(a) == content_hash(b)  # guard-the-guard: hashes collide

    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    pipe = _GovernedPipeline(outreach_db)
    _enqueue(root, dedupe_key="backup:aaaaaaaaaaaa", title=_LONG_TITLE,
             body=_LONG_BODY + "Detail: first failure")
    await alert_drain._make_drainer(_RT(pipeline=pipe))()
    _enqueue(root, dedupe_key="backup:bbbbbbbbbbbb", title=_LONG_TITLE,
             body=_LONG_BODY + "Detail: second, different failure")
    await alert_drain._make_drainer(_RT(pipeline=pipe))()

    assert pipe.statuses == [OutreachStatus.DELIVERED, OutreachStatus.DELIVERED]


@pytest.mark.asyncio
async def test_same_key_repeat_is_still_suppressed(tmp_path, monkeypatch, outreach_db):
    root = tmp_path / "queue"
    monkeypatch.setattr("genesis.env.alert_queue_root", lambda: root)
    pipe = _GovernedPipeline(outreach_db)
    for _ in range(2):
        _enqueue(root, dedupe_key="backup:aaaaaaaaaaaa", title=_LONG_TITLE,
                 body=_LONG_BODY + "Detail: same failure")
        await alert_drain._make_drainer(_RT(pipeline=pipe))()

    assert pipe.statuses == [OutreachStatus.DELIVERED, OutreachStatus.REJECTED]
    assert q.list_queued(root) == []  # REJECTED stays terminal
