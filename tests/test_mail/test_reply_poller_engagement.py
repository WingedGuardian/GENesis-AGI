"""WS-1: a matched email reply must write engagement back to ``outreach_history``.

Before this bridge, ``ReplyPoller`` only updated ``email_threads`` on a reply, so
a real influencer reply never registered as engagement — and the WS-2 ledger's
``reply_received`` prediction resolved to 0/no-reply (silence) forever.

RED-then-GREEN: ``test_reply_without_bridge_leaves_engagement_null`` documents the
pre-fix behaviour (passes on current code); the bridge tests fail until the
``set_engagement_bridge`` / ``record_reply_if_pending`` / ``make_reply_engagement_bridge``
plumbing exists.
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import outreach as outreach_crud
from genesis.mail.reply_poller import ReplyPoller
from genesis.mail.threads import ThreadTracker
from genesis.mail.types import RawEmail
from genesis.outreach.engagement import EngagementTracker

pytestmark = pytest.mark.asyncio

SENT_MID = "<sent-abc@mx.google.com>"
OUTREACH_ID = "outreach-123"


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


class _FakeImap:
    """Minimal IMAP stub — returns canned raw emails, records mark_read calls."""

    def __init__(self, raws: list[RawEmail]) -> None:
        self._raws = raws
        self.marked: list[int] = []

    async def fetch_unread(self, max_count: int = 20) -> list[RawEmail]:
        return self._raws

    async def mark_read(self, uids: list[int]) -> None:
        self.marked.extend(uids)


def _reply(
    *,
    in_reply_to: str = SENT_MID,
    from_addr: str = "target@example.com",
    msg_id: str = "<reply-1@example.com>",
    subject: str = "Re: your pitch",
    body: str = "Interesting, tell me more.",
    uid: int = 1,
    extra_headers: str = "",
) -> RawEmail:
    raw = (
        f"From: {from_addr}\r\n"
        f"To: agent@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {msg_id}\r\n"
        f"In-Reply-To: {in_reply_to}\r\n"
        f"{extra_headers}"
        f"\r\n{body}\r\n"
    ).encode()
    return RawEmail(uid=uid, raw_bytes=raw)


async def _seed_outreach(
    db,
    *,
    engagement_outcome: str | None = None,
    engagement_signal: str = "manual",
) -> None:
    now = datetime.now(UTC).isoformat()
    await outreach_crud.create(
        db,
        id=OUTREACH_ID,
        signal_type="marketing",
        topic="pitch",
        category="content",
        salience_score=0.5,
        channel="email",
        message_content="hi there",
        created_at=now,
        delivery_id=SENT_MID,
    )
    if engagement_outcome is not None:
        await outreach_crud.record_engagement(
            db,
            OUTREACH_ID,
            engagement_outcome=engagement_outcome,
            engagement_signal=engagement_signal,
        )


async def _register_thread(tracker: ThreadTracker, *, context: dict | None) -> str:
    return await tracker.register(
        message_id=SENT_MID,
        recipient="target@example.com",
        subject="your pitch",
        context=context,
    )


async def _run_poll(db, *, thread_context: dict | None, with_bridge: bool, raws=None):
    tracker = ThreadTracker(db)
    await _register_thread(tracker, context=thread_context)
    poller = ReplyPoller(imap_client=_FakeImap(raws or [_reply()]), thread_tracker=tracker)
    if with_bridge:
        from genesis.outreach.engagement import make_reply_engagement_bridge

        poller.set_engagement_bridge(make_reply_engagement_bridge(EngagementTracker(db)))
    return await poller.poll()


async def _engagement(db) -> tuple[str | None, str | None, str | None]:
    cur = await db.execute(
        "SELECT engagement_outcome, engagement_signal, user_response "
        "FROM outreach_history WHERE id = ?",
        (OUTREACH_ID,),
    )
    row = await cur.fetchone()
    return (row["engagement_outcome"], row["engagement_signal"], row["user_response"])


# ---------------------------------------------------------------------------
# Baseline (pre-fix behaviour) — passes on current code, documents the bug
# ---------------------------------------------------------------------------


async def test_reply_without_bridge_leaves_engagement_null(db):
    await _seed_outreach(db)
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=False)

    assert stats["matched"] == 1  # the reply DID match the thread
    outcome, _signal, _resp = await _engagement(db)
    assert outcome is None  # ...but engagement never registered — this is the bug


# ---------------------------------------------------------------------------
# The fix
# ---------------------------------------------------------------------------


async def test_matched_reply_records_useful_engagement(db):
    await _seed_outreach(db)
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)

    assert stats["matched"] == 1
    assert stats["errors"] == 0
    outcome, signal, resp = await _engagement(db)
    assert outcome == "useful"
    assert signal == "user_reply"
    assert resp and "Interesting" in resp


async def test_bridge_does_not_clobber_stronger_outcome(db):
    # A prior, richer signal (dashboard '/engage' → 'acted_on') must survive.
    await _seed_outreach(db, engagement_outcome="acted_on")
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)

    assert stats["errors"] == 0
    outcome, _signal, _resp = await _engagement(db)
    assert outcome == "acted_on"  # NULL-guarded write is a no-op, not a downgrade


async def test_late_reply_overrides_timeout_marker(db):
    """A reply landing after the 24h timeout job already stamped
    'ignored'/'timeout' must still register — the timeout is a mechanical
    default, not a human verdict. (Reply poller runs every 4h; the 24-72h
    delayed-reply window is a large share of real cold-outreach replies.)"""
    await _seed_outreach(db, engagement_outcome="ignored", engagement_signal="timeout")
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)

    assert stats["errors"] == 0
    outcome, signal, resp = await _engagement(db)
    assert outcome == "useful"
    assert signal == "user_reply"
    assert resp and "Interesting" in resp


async def test_reply_overrides_mechanical_ambivalent(db):
    # implicit_activity / auto_digest are machine-set weak signals — a real
    # reply is strictly stronger evidence and must upgrade them.
    await _seed_outreach(
        db,
        engagement_outcome="ambivalent",
        engagement_signal="implicit_activity",
    )
    await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)
    outcome, signal, _resp = await _engagement(db)
    assert (outcome, signal) == ("useful", "user_reply")


async def test_dashboard_verdict_on_stale_timeout_signal_is_protected(db):
    """The dashboard /engage route rewrites engagement_outcome but never
    touches engagement_signal — so a human 'not_useful' verdict can sit on a
    stale 'timeout' signal. A late reply must NOT clobber that human verdict:
    the override guard requires the outcome to still be mechanically PAIRED
    (ignored/ambivalent), not just a mechanical signal."""
    await _seed_outreach(db, engagement_outcome="not_useful", engagement_signal="timeout")
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)

    assert stats["errors"] == 0
    outcome, signal, _resp = await _engagement(db)
    assert outcome == "not_useful"  # human verdict survives
    assert signal == "timeout"  # row untouched


async def test_auto_responder_reply_does_not_count_as_engagement(db):
    """An out-of-office / auto-reply carries our Message-ID in In-Reply-To and
    matches the thread — but RFC-3834 auto markers must keep it from inflating
    the reply rate. Thread is still recorded; engagement is NOT."""
    await _seed_outreach(db)
    auto = _reply(extra_headers="Auto-Submitted: auto-replied\r\n", body="I am on vacation.")
    stats = await _run_poll(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        with_bridge=True,
        raws=[auto],
    )
    assert stats["matched"] == 1  # thread matched + recorded
    assert stats["errors"] == 0
    outcome, _signal, _resp = await _engagement(db)
    assert outcome is None  # ...but not counted as engagement


async def test_precedence_bulk_reply_does_not_count(db):
    await _seed_outreach(db)
    bulk = _reply(extra_headers="Precedence: bulk\r\n")
    await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True, raws=[bulk])
    outcome, _s, _r = await _engagement(db)
    assert outcome is None


async def test_foreign_sender_reply_does_not_count(db):
    """A message that merely carries our Message-ID but comes from someone other
    than the address we mailed (spoof / list echo) must not count as engagement."""
    await _seed_outreach(db)
    foreign = _reply(from_addr="stranger@elsewhere.com")
    await _run_poll(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        with_bridge=True,
        raws=[foreign],
    )
    outcome, _s, _r = await _engagement(db)
    assert outcome is None


async def test_genuine_reply_with_display_name_still_counts(db):
    """The sender check compares the bare address, so a reply from the real
    recipient with a display name ('Target Person <target@example.com>') still
    registers — guards against a naive full-header string compare."""
    await _seed_outreach(db)
    named = _reply(from_addr="Target Person <target@example.com>")
    await _run_poll(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        with_bridge=True,
        raws=[named],
    )
    outcome, signal, _r = await _engagement(db)
    assert (outcome, signal) == ("useful", "user_reply")


async def test_wave1_thread_without_outreach_id_degrades(db):
    # Historical foreground sends: thread has no outreach_id in context.
    await _seed_outreach(db)
    stats = await _run_poll(db, thread_context={"signal_type": "marketing"}, with_bridge=True)

    assert stats["matched"] == 1
    assert stats["errors"] == 0  # graceful skip, never an error
    outcome, _signal, _resp = await _engagement(db)
    assert outcome is None  # nothing to bridge → left untouched, no crash


async def test_reply_poller_module_does_not_import_outreach():
    # Layering guard: genesis.mail must stay outreach-agnostic (§3 of the design).
    import inspect

    from genesis.mail import reply_poller

    src = inspect.getsource(reply_poller)
    assert "genesis.outreach" not in src
    assert "from genesis.outreach" not in src


# ---------------------------------------------------------------------------
# E2E — the load-bearing outcome: the WS-2 ledger reply-prediction now resolves
# ---------------------------------------------------------------------------


async def test_e2e_reply_prediction_resolves_after_bridge(db):
    """WS-2 ledger lane: a bridged reply grades ``reply_received=1``; an
    unbridged send grades 0 at deadline — the exact pre-fix failure mode."""
    from datetime import timedelta

    from genesis.ledger import writers as ledger_writers
    from genesis.ledger.grader import grade_due_predictions

    now = datetime.now(UTC)

    # Send A: gets a reply, bridged. Send B: silence (the contrast case).
    await _seed_outreach(db)
    other_id = "outreach-silent"
    await outreach_crud.create(
        db,
        id=other_id,
        signal_type="marketing",
        topic="pitch2",
        category="content",
        salience_score=0.5,
        channel="email",
        message_content="hello",
        created_at=now.isoformat(),
        delivery_id="<sent-def@mx.google.com>",
    )
    # Mirror pipeline._deliver: write reply_received/positive_engagement predictions.
    await ledger_writers.on_outreach_delivered(db, outreach_id=OUTREACH_ID, category="content", channel="email")
    await ledger_writers.on_outreach_delivered(db, outreach_id=other_id, category="content", channel="email")

    # Bridge the reply on send A → engagement 'useful' / signal 'user_reply'.
    tracker = ThreadTracker(db)
    await _register_thread(tracker, context={"outreach_id": OUTREACH_ID})
    from genesis.outreach.engagement import make_reply_engagement_bridge

    poller = ReplyPoller(imap_client=_FakeImap([_reply()]), thread_tracker=tracker)
    poller.set_engagement_bridge(make_reply_engagement_bridge(EngagementTracker(db)))
    await poller.poll()

    # Grade past every deadline (injectable clock — no wall-clock dependence).
    await grade_due_predictions(db, now=now + timedelta(days=365))

    async def _reply_pred(subject_id: str) -> tuple[str, int | None]:
        cur = await db.execute(
            "SELECT status, outcome_value FROM ledger_predictions "
            "WHERE subject_ref_id = ? AND metric = 'reply_received'",
            (subject_id,),
        )
        row = await cur.fetchone()
        assert row is not None, f"no reply_received prediction for {subject_id}"
        return row["status"], row["outcome_value"]

    status_a, value_a = await _reply_pred(OUTREACH_ID)
    assert value_a == 1, f"bridged reply must grade 1, got {value_a} ({status_a})"
    status_b, value_b = await _reply_pred(other_id)
    assert value_b == 0, f"silent send must grade 0 at deadline, got {value_b} ({status_b})"
