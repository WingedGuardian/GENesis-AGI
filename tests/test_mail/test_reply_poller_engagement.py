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


# ---------------------------------------------------------------------------
# B — marketing reply owner-ping (notify-only). A GENUINE reply from a curated
# marketing prospect pings the owner exactly once; nothing else does, and the
# best-effort ping never affects engagement recording. The discriminator is
# RECIPIENT-in-marketing_prospects (NOT the thread signal_type, which the
# owner-approval resume path overwrites to "email_gate_resume"). The auto-reply
# path is untouched by this feature.
# ---------------------------------------------------------------------------


async def _seed_prospect(db, *, email: str = "target@example.com", opted_out: bool = False) -> None:
    """Seed a curated marketing prospect (the recipient of a cold pitch)."""
    from genesis.db.crud import marketing_prospects as mp

    await mp.create(db, id=f"prospect-{email}", email=email, name="Target", source="test")
    if opted_out:
        await mp.mark_opted_out(db, f"prospect-{email}", opted_out_at="2026-09-01T00:00:00Z")


async def _run_poll_with_notify(db, *, thread_context, notify_owner, raws=None):
    tracker = ThreadTracker(db)
    await _register_thread(tracker, context=thread_context)
    poller = ReplyPoller(imap_client=_FakeImap(raws or [_reply()]), thread_tracker=tracker)
    from genesis.outreach.engagement import make_reply_engagement_bridge

    poller.set_engagement_bridge(
        make_reply_engagement_bridge(EngagementTracker(db), notify_owner=notify_owner)
    )
    return await poller.poll()


async def test_reply_from_marketing_prospect_pings_owner(db):
    await _seed_outreach(db)
    await _seed_prospect(db)  # recipient target@example.com is a curated prospect
    calls = []

    async def _notify(thread, reply):
        calls.append((thread, reply))

    stats = await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
    )
    assert stats["errors"] == 0
    assert len(calls) == 1  # genuine reply from a prospect → owner pinged exactly once
    # Engagement is still recorded — the ping is additive, not a replacement.
    outcome, signal, _ = await _engagement(db)
    assert (outcome, signal) == ("useful", "user_reply")


async def test_reply_from_non_prospect_does_not_ping(db):
    """A genuine reply whose recipient is NOT a curated marketing prospect records
    engagement but must NOT fire the marketing owner-ping."""
    await _seed_outreach(db)
    # No prospect seeded → recipient is not in marketing_prospects.
    calls = []

    async def _notify(thread, reply):
        calls.append(1)

    await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
    )
    assert calls == []  # recipient not a prospect → no ping
    outcome, _s, _r = await _engagement(db)
    assert outcome == "useful"  # ...engagement still recorded


async def test_reply_from_opted_out_prospect_still_pings(db):
    """Membership (get_by_email), NOT is_active_recipient: a contacted / opted-out
    prospect who replies is still a real human reply worth surfacing to the owner.
    Locks the inclusive discriminator against a future narrowing to active-only."""
    await _seed_outreach(db)
    await _seed_prospect(db, opted_out=True)
    calls = []

    async def _notify(thread, reply):
        calls.append(1)

    await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
    )
    assert calls == [1]  # opted-out prospect's genuine reply still pings


async def test_auto_responder_from_prospect_does_not_ping(db):
    """An OOO/auto-reply from a prospect is gated BEFORE the ping — the owner is
    not pinged for a bounce, and it is not counted as engagement."""
    await _seed_outreach(db)
    await _seed_prospect(db)
    calls = []

    async def _notify(thread, reply):
        calls.append(1)

    auto = _reply(extra_headers="Auto-Submitted: auto-replied\r\n", body="I am away.")
    await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
        raws=[auto],
    )
    assert calls == []  # auto-responder gated before the ping
    outcome, _s, _r = await _engagement(db)
    assert outcome is None  # and not counted as engagement


async def test_foreign_sender_does_not_ping(db):
    """Recipient is a prospect, but the reply comes from a foreign/spoofed sender.
    The sender!=recipient gate returns early, before the prospect lookup + ping."""
    await _seed_outreach(db)
    await _seed_prospect(db)
    calls = []

    async def _notify(thread, reply):
        calls.append(1)

    foreign = _reply(from_addr="stranger@elsewhere.com")
    await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
        raws=[foreign],
    )
    assert calls == []


async def test_unverified_empty_sender_does_not_ping(db):
    """A reply matched to a prospect thread but with a malformed/unparsable ``From``
    (a display name with no address → empty parsed sender) slips PAST the
    sender!=recipient guard, which only rejects when BOTH addresses are truthy.
    The owner-ping must still NOT fire — we surface only a reply whose sender we
    positively verified equals the prospect recipient, never an unverified
    "reply from someone". Engagement recording keeps its pre-existing
    permissiveness (this test does not assert on it)."""
    await _seed_outreach(db)
    await _seed_prospect(db)
    calls = []

    async def _notify(thread, reply):
        calls.append(1)

    # From header present (so the reply matches + reaches the bridge) but with an
    # empty angle-address → parseaddr("Anonymous <>")[1] == "" → the mismatch guard
    # (needs BOTH addrs truthy) can't reject it, so only the ping's own sender_addr
    # check stops it. (NB: a bare quoted string like '"X"' parses AS the address, so
    # it would trip the mismatch guard instead — not the path under test.)
    no_addr = _reply(from_addr="Anonymous <>")
    await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
        raws=[no_addr],
    )
    assert calls == []  # unverified/empty sender → no owner ping


async def test_ping_failure_does_not_break_engagement_or_count_error(db):
    """The owner-ping is best-effort: if notify_owner raises, engagement is still
    recorded and the bridge does NOT report an error — reply tracking must never
    depend on the ping (the bridge's fail-safe contract)."""
    await _seed_outreach(db)
    await _seed_prospect(db)

    async def _notify(thread, reply):
        raise RuntimeError("telegram down")

    stats = await _run_poll_with_notify(
        db,
        thread_context={"outreach_id": OUTREACH_ID},
        notify_owner=_notify,
    )
    assert stats["errors"] == 0  # ping failure isolated from bridge error accounting
    outcome, signal, _ = await _engagement(db)
    assert (outcome, signal) == ("useful", "user_reply")  # engagement survived


async def test_bridge_without_notify_owner_is_unchanged(db):
    # Backward-compat: the factory's original 1-arg form still works (no ping).
    await _seed_outreach(db)
    await _seed_prospect(db)  # even with a prospect, no notify_owner → no ping path
    stats = await _run_poll(db, thread_context={"outreach_id": OUTREACH_ID}, with_bridge=True)
    assert stats["errors"] == 0
    outcome, _s, _r = await _engagement(db)
    assert outcome == "useful"


# --- the notifier factory: the actual ping construction --------------------


class _FakePipeline:
    """Records submit_raw calls, returns a canned OutreachResult status."""

    def __init__(self, status):
        self._status = status
        self.calls = []
        self.best_effort_flags = []

    async def submit_raw(self, text, request, *, best_effort=False):
        self.calls.append((text, request))
        self.best_effort_flags.append(best_effort)
        from genesis.outreach.types import OutreachResult

        return OutreachResult(
            outreach_id="x",
            status=self._status,
            channel="telegram",
            message_content=text,
        )


class _ReplyStub:
    sender = "Cole <cole@example.com>"
    subject = "Re: your pitch"
    body_preview = "Interesting -- tell me more.\nSecond line should be dropped."
    message_id = "<r-1@example.com>"


async def test_marketing_reply_notifier_builds_brief_telegram_ping():
    from genesis.outreach.engagement import make_marketing_reply_notifier
    from genesis.outreach.types import OutreachCategory, OutreachStatus

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    notifier = make_marketing_reply_notifier(pipe)

    await notifier({"id": "th-1", "recipient": "cole@example.com"}, _ReplyStub())

    assert len(pipe.calls) == 1
    text, request = pipe.calls[0]
    assert request.channel == "telegram"
    assert request.category == OutreachCategory.NOTIFICATION
    assert request.verbatim is True
    assert request.salience_score == 0.9
    assert "Cole &lt;cole@example.com&gt;" in text  # angle brackets HTML-escaped
    assert "Re: your pitch" in text
    assert "Interesting" in text
    assert "Second line should be dropped" not in text  # only the FIRST preview line
    assert pipe.best_effort_flags == [True]  # fire-and-forget: never deferred/retried


async def test_marketing_reply_notifier_escapes_attacker_html():
    """CRITICAL security regression: the outreach telegram send path delivers
    with parse_mode='HTML' and does NOT escape, so attacker-controlled reply
    fields (sender/subject/body — anyone who can email the inbox controls them)
    must be HTML-escaped here or they render as live HTML (clickable links,
    formatting) in the owner's trusted chat. Reddens if the escape is removed."""
    from genesis.outreach.engagement import make_marketing_reply_notifier
    from genesis.outreach.types import OutreachStatus

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    notifier = make_marketing_reply_notifier(pipe)

    class _Evil:
        sender = '<a href="https://evil.example">Genesis Security</a>'
        subject = '<b>urgent</b> <a href="https://evil">verify</a>'
        body_preview = '<code>x</code> click <a href="https://evil">here</a>'
        message_id = "<r-evil@example.com>"

    await notifier({"id": "th", "recipient": "p@example.com"}, _Evil())
    text, _req = pipe.calls[0]
    # No raw HTML tags survive anywhere in the delivered text.
    assert "<a href" not in text
    assert "<b>" not in text
    assert "<code>" not in text
    assert "&lt;a href" in text  # escaped form present, so content is preserved


async def test_marketing_reply_notifier_strips_control_chars():
    """Bidi-override / zero-width chars in a display string could visually
    disguise the sender/subject the owner reads -- strip Cc/Cf categories."""
    from genesis.outreach.engagement import make_marketing_reply_notifier
    from genesis.outreach.types import OutreachStatus

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    notifier = make_marketing_reply_notifier(pipe)

    class _R:
        sender = "ev\u202eil@example.com"  # U+202E RTL override (Cf)
        subject = "hi\u200bthere\x07"  # U+200B zero-width (Cf) + BEL (Cc)
        body_preview = "ok"
        message_id = "<r@example.com>"

    await notifier({"id": "th", "recipient": "p@example.com"}, _R())
    text, _req = pipe.calls[0]
    assert "\u202e" not in text  # bidi override stripped
    assert "\u200b" not in text  # zero-width space stripped
    assert "\x07" not in text  # control char stripped
    assert "evil@example.com" in text  # legible content preserved


async def test_marketing_reply_notifier_survives_non_delivered():
    from genesis.outreach.engagement import make_marketing_reply_notifier
    from genesis.outreach.types import OutreachStatus

    pipe = _FakePipeline(OutreachStatus.FAILED)
    notifier = make_marketing_reply_notifier(pipe)

    class _R:
        sender = "x@example.com"
        subject = ""
        body_preview = ""
        message_id = "<r-2@example.com>"

    # A non-DELIVERED result must NOT raise (best-effort ping).
    await notifier({"id": "th-2", "recipient": "x@example.com"}, _R())
    assert len(pipe.calls) == 1


async def test_end_to_end_genuine_marketing_reply_through_real_notifier(db):
    """Integration: a genuine marketing_cold reply drives the WHOLE chain —
    bridge gate → REAL make_marketing_reply_notifier → submit_raw — as one unit
    (stub-free notifier, so it guards the notifier↔submit_raw seam that a fake
    _notify can't)."""
    from genesis.outreach.engagement import (
        make_marketing_reply_notifier,
        make_reply_engagement_bridge,
    )
    from genesis.outreach.types import OutreachStatus

    await _seed_outreach(db)
    await _seed_prospect(db)  # recipient target@example.com is a curated prospect
    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    tracker = ThreadTracker(db)
    await _register_thread(tracker, context={"outreach_id": OUTREACH_ID})
    poller = ReplyPoller(imap_client=_FakeImap([_reply()]), thread_tracker=tracker)
    poller.set_engagement_bridge(
        make_reply_engagement_bridge(
            EngagementTracker(db), notify_owner=make_marketing_reply_notifier(pipe)
        )
    )
    stats = await poller.poll()

    assert stats["errors"] == 0
    assert len(pipe.calls) == 1  # genuine reply → real notifier → exactly one submit_raw
    text, request = pipe.calls[0]
    assert request.channel == "telegram"
    assert "target@example.com" in text  # the reply's sender, in the brief ping
    # Per-reply dedup discriminator is carried in the (non-delivered) context.
    assert request.topic != request.context
    # Engagement is still recorded alongside the ping.
    outcome, signal, _ = await _engagement(db)
    assert (outcome, signal) == ("useful", "user_reply")


async def test_marketing_reply_notifier_dedup_is_per_reply():
    """Two DISTINCT replies with identical rendered text but different
    Message-IDs must produce different dedup keys, so submit_raw's
    content-hash secondary key (outreach/governance.py::_is_duplicate) cannot
    collapse two genuine replies into a single owner ping.

    The rendered text here is LONG (>200 chars) on purpose: content_hash only
    hashes context[:200] (governance.py::content_hash), so the per-reply
    discriminator (message_id) must LEAD the context to stay inside the hashed
    prefix. Reddens if `context` appends message_id after the text (the pre-fix
    order), where both long replies share an identical first-200-char prefix."""
    from genesis.outreach.engagement import make_marketing_reply_notifier
    from genesis.outreach.governance import content_hash
    from genesis.outreach.types import OutreachStatus

    pipe = _FakePipeline(OutreachStatus.DELIVERED)
    notifier = make_marketing_reply_notifier(pipe)

    class _R1:
        sender = "same@example.com"
        subject = "Re: pitch"
        body_preview = "x" * 300  # long → any trailing discriminator falls past char 200
        message_id = "<r-A@example.com>"

    class _R2(_R1):
        message_id = "<r-B@example.com>"

    thread = {"id": "th", "recipient": "same@example.com"}
    await notifier(thread, _R1())
    await notifier(thread, _R2())

    (text1, req1), (text2, req2) = pipe.calls
    assert text1 == text2  # identical DELIVERED text...
    assert req1.context != req2.context  # ...but distinct dedup context (per-reply)
    assert req1.topic != req2.topic
    # The load-bearing lock: the discriminator participates in the hashed prefix,
    # so the secondary content-hash dedup key differs for the two replies.
    assert content_hash(req1.context) != content_hash(req2.context)
