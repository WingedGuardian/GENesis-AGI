"""Tests for MailMonitor orchestrator (paralegal/judge architecture)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from genesis.mail.monitor import MailMonitor
from genesis.mail.types import MailConfig, RawEmail


@dataclass
class MockCCOutput:
    """Mock for CCOutput — real class has .text attribute."""

    text: str = ""
    session_id: str = "mock-session"
    model_used: str = "sonnet"
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    exit_code: int = 0
    is_error: bool = False
    error_message: str | None = None


# -- Sample data for mocks ---------------------------------------------------

SAMPLE_PARALEGAL_BRIEF = json.dumps([{
    "email_index": 1,
    "sender": "a@b.com",
    "subject": "Test",
    "classification": "AI_Agent",
    "relevance": 4,
    "key_findings": ["Found something important about agents"],
    "assessment": "Directly relevant to Genesis architecture",
    "recommendation": "Review for architectural implications",
}])

SAMPLE_PARALEGAL_BRIEF_LOW_SIGNAL = json.dumps([{
    "email_index": 1,
    "sender": "a@b.com",
    "subject": "Test",
    "classification": "Operational",
    "relevance": 1,
    "key_findings": ["Routine notification"],
    "assessment": "Not relevant to AI development",
    "recommendation": "Skip",
}])

SAMPLE_JUDGE_KEEP = json.dumps([{
    "email_index": 1,
    "decision": "KEEP",
    "rationale": "Specific findings about agent architectures",
    "refined_finding": "New agent framework released with MCP support",
}])

SAMPLE_JUDGE_DISCARD = json.dumps([{
    "email_index": 1,
    "decision": "DISCARD",
    "rationale": "Vague findings, no specific signal",
    "refined_finding": "",
}])

SAMPLE_TWO_BRIEFS = json.dumps([
    {
        "email_index": 1,
        "sender": "a@b.com",
        "subject": "Important AI News",
        "classification": "AI_Agent",
        "relevance": 5,
        "key_findings": ["Critical agent development"],
        "assessment": "Very relevant",
        "recommendation": "Keep",
    },
    {
        "email_index": 2,
        "sender": "c@d.com",
        "subject": "Spam",
        "classification": "Operational",
        "relevance": 1,
        "key_findings": ["Nothing useful"],
        "assessment": "Not relevant",
        "recommendation": "Skip",
    },
])


# -- Fixtures -----------------------------------------------------------------

@pytest_asyncio.fixture
async def db(tmp_path):
    """In-memory DB with processed_emails and observations tables."""
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_emails (
                id TEXT PRIMARY KEY, message_id TEXT NOT NULL,
                imap_uid INTEGER, sender TEXT NOT NULL,
                subject TEXT NOT NULL, received_at TEXT,
                body_preview TEXT, layer1_verdict TEXT,
                layer1_brief TEXT, layer2_decision TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending','processing','completed','skipped','failed')
                ),
                batch_id TEXT, created_at TEXT NOT NULL,
                processed_at TEXT, error_message TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                content_hash TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, source TEXT, type TEXT,
                category TEXT, content TEXT, priority TEXT,
                created_at TEXT, content_hash TEXT, resolved INTEGER DEFAULT 0,
                resolved_at TEXT, resolved_by TEXT,
                person_id TEXT, speculative INTEGER DEFAULT 0,
                expires_at TEXT, resolution_notes TEXT,
                retrieved_count INTEGER DEFAULT 0,
                influenced_action INTEGER DEFAULT 0
            )
        """)
        await conn.commit()
        yield conn


@pytest.fixture
def config():
    return MailConfig(batch_size=5, max_emails_per_run=10, min_relevance=3)


@pytest.fixture
def imap_client():
    client = AsyncMock()
    client.fetch_unread = AsyncMock(return_value=[])
    client.mark_read = AsyncMock()
    return client


@pytest.fixture
def router():
    r = AsyncMock()
    r.route = AsyncMock(return_value=SAMPLE_PARALEGAL_BRIEF)
    return r


@pytest.fixture
def invoker():
    inv = AsyncMock()
    inv.run = AsyncMock(
        return_value=MockCCOutput(text=SAMPLE_JUDGE_KEEP),
    )
    return inv


@pytest.fixture
def session_manager():
    mgr = AsyncMock()
    sess = MagicMock()
    sess.session_id = "test-session-123"
    mgr.create_background = AsyncMock(return_value=sess)
    return mgr


@pytest.fixture
def event_bus():
    bus = AsyncMock()
    bus.emit = AsyncMock()
    return bus


@pytest.fixture
def monitor(db, config, imap_client, router, invoker, session_manager, event_bus):
    return MailMonitor(
        db=db,
        config=config,
        imap_client=imap_client,
        router=router,
        invoker=invoker,
        session_manager=session_manager,
        event_bus=event_bus,
        triage_pipeline=None,
    )


def _make_raw_email(
    uid: int = 1, subject: str = "Test", sender: str = "a@b.com",
) -> RawEmail:
    from email.mime.text import MIMEText

    msg = MIMEText("Check out https://example.com/ai-news", "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-ID"] = f"<msg-{uid}@example.com>"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    return RawEmail(uid=uid, raw_bytes=msg.as_bytes())


# -- Tests --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_batch_no_emails(monitor, imap_client):
    imap_client.fetch_unread.return_value = []
    result = await monitor.run_batch()
    assert result.fetched == 0
    assert result.layer1_briefed == 0


@pytest.mark.asyncio
async def test_run_batch_processes_email(monitor, imap_client, router, invoker):
    """Full pipeline: fetch -> paralegal -> judge -> observation."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_KEEP)

    result = await monitor.run_batch()
    assert result.fetched == 1
    assert result.layer1_briefed == 1
    assert result.layer2_kept == 1
    assert result.layer2_discarded == 0


@pytest.mark.asyncio
async def test_run_batch_skips_known_emails(monitor, imap_client, db):
    """Emails already in DB are not re-processed."""
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO processed_emails "
        "(id, message_id, sender, subject, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), "<msg-1@example.com>", "a@b.com", "Test", "completed", now),
    )
    await db.commit()

    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    result = await monitor.run_batch()
    assert result.already_known == 1
    assert result.layer1_briefed == 0


@pytest.mark.asyncio
async def test_run_batch_marks_all_read(monitor, imap_client, router, invoker):
    imap_client.fetch_unread.return_value = [
        _make_raw_email(uid=1),
        _make_raw_email(uid=2, subject="Other"),
    ]
    router.route.return_value = SAMPLE_TWO_BRIEFS
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_KEEP)

    await monitor.run_batch()
    imap_client.mark_read.assert_called_once()
    uids = imap_client.mark_read.call_args[0][0]
    assert set(uids) == {1, 2}


@pytest.mark.asyncio
async def test_low_signal_filtered_before_judge(
    monitor, imap_client, router, invoker, db,
):
    """Relevance=1 emails never reach the judge."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF_LOW_SIGNAL

    result = await monitor.run_batch()
    assert result.layer1_low_signal == 1
    assert result.layer1_briefed == 0
    assert result.layer2_kept == 0

    # Judge should never have been called
    invoker.run.assert_not_called()

    # DB should show skipped with low_signal verdict
    cursor = await db.execute(
        "SELECT status, layer1_verdict FROM processed_emails WHERE message_id = ?",
        ("<msg-1@example.com>",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "skipped"
    assert row["layer1_verdict"] == "low_signal"


@pytest.mark.asyncio
async def test_judge_keep_creates_observation(monitor, imap_client, router, invoker, db):
    """KEEP decisions create observations with judge's refined finding."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_KEEP)

    await monitor.run_batch()

    cursor = await db.execute(
        "SELECT content, source, category FROM observations "
        "WHERE source = 'recon' AND category = 'email_recon'",
    )
    row = await cursor.fetchone()
    assert row is not None
    assert "New agent framework released with MCP support" in row["content"]
    assert row["source"] == "recon"
    assert row["category"] == "email_recon"


@pytest.mark.asyncio
async def test_judge_discard_no_observation(monitor, imap_client, router, invoker, db):
    """DISCARD decisions do NOT create observations but DO store rationale."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_DISCARD)

    result = await monitor.run_batch()
    assert result.layer2_discarded == 1

    # No observation created
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM observations")
    row = await cursor.fetchone()
    assert row["cnt"] == 0

    # But layer2_decision IS stored in processed_emails
    cursor = await db.execute(
        "SELECT layer2_decision, status FROM processed_emails WHERE message_id = ?",
        ("<msg-1@example.com>",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row["status"] == "skipped"
    decision = json.loads(row["layer2_decision"])
    assert decision["decision"] == "DISCARD"
    assert "Vague findings" in decision["rationale"]


@pytest.mark.asyncio
async def test_layer1_brief_stored_in_db(monitor, imap_client, router, invoker, db):
    """Paralegal briefs are stored in processed_emails.layer1_brief."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_KEEP)

    await monitor.run_batch()

    cursor = await db.execute(
        "SELECT layer1_brief FROM processed_emails WHERE message_id = ?",
        ("<msg-1@example.com>",),
    )
    row = await cursor.fetchone()
    assert row is not None
    brief = json.loads(row["layer1_brief"])
    assert brief["classification"] == "AI_Agent"
    assert brief["relevance"] == 4
    assert len(brief["key_findings"]) == 1


@pytest.mark.asyncio
async def test_gemini_parse_failure_sends_all_to_judge(
    monitor, imap_client, router, invoker,
):
    """When Gemini returns garbage, all emails go to judge via fallback briefs."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = "NOT VALID JSON AT ALL"
    invoker.run.return_value = MockCCOutput(text=SAMPLE_JUDGE_KEEP)

    result = await monitor.run_batch()
    # Fallback briefs have relevance=3 which passes min_relevance=3
    assert result.layer1_briefed == 1
    assert result.layer2_kept == 1
    # Judge was called
    invoker.run.assert_called_once()


@pytest.mark.asyncio
async def test_judge_parse_failure_keeps_all(
    monitor, imap_client, router, invoker, db,
):
    """When CC returns unparseable output, all are treated as KEEP."""
    imap_client.fetch_unread.return_value = [_make_raw_email(uid=1)]
    router.route.return_value = SAMPLE_PARALEGAL_BRIEF
    invoker.run.return_value = MockCCOutput(text="I am a judge and I think...")

    result = await monitor.run_batch()
    assert result.layer2_kept == 1
    assert result.layer2_discarded == 0

    # Observation should exist (fallback KEEP)
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM observations")
    row = await cursor.fetchone()
    assert row["cnt"] == 1


@pytest.mark.asyncio
async def test_mixed_relevance_filtering(monitor, imap_client, router, invoker):
    """Two emails: one relevant (rel=5), one low-signal (rel=1). Only relevant goes to judge."""
    imap_client.fetch_unread.return_value = [
        _make_raw_email(uid=1),
        _make_raw_email(uid=2, subject="Other"),
    ]
    router.route.return_value = SAMPLE_TWO_BRIEFS
    # Judge only sees email 1 (rel=5), so its response references email_index=1
    invoker.run.return_value = MockCCOutput(text=json.dumps([{
        "email_index": 1,
        "decision": "KEEP",
        "rationale": "Critical finding",
        "refined_finding": "Important agent development",
    }]))

    result = await monitor.run_batch()
    assert result.layer1_briefed == 1
    assert result.layer1_low_signal == 1
    assert result.layer2_kept == 1
