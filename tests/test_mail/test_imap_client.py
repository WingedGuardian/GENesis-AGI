"""Tests for IMAP client — mocked imaplib."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from genesis.mail.imap_client import IMAPClient


@pytest.fixture
def client():
    return IMAPClient(address="test@gmail.com", app_password="fake-pass", timeout=5)


def _make_mock_conn(*, messages: dict[bytes, bytes] | None = None):
    """Create a mock IMAP4_SSL connection."""
    conn = MagicMock()
    conn.select.return_value = ("OK", [b"3"])

    if messages is None:
        messages = {}

    uids = b" ".join(messages.keys()) if messages else b""
    conn.search.return_value = ("OK", [uids])

    def mock_fetch(uid, parts):
        if uid in messages:
            return ("OK", [(uid + b" (RFC822 {100})", messages[uid])])
        return ("OK", [None])

    conn.fetch.side_effect = mock_fetch
    conn.store.return_value = ("OK", [])
    conn.close.return_value = ("BYE", [])
    conn.logout.return_value = ("BYE", [])
    return conn


@pytest.mark.asyncio
async def test_fetch_unread_empty(client):
    mock_conn = _make_mock_conn()
    with patch("genesis.mail.imap_client.imaplib") as mock_imap:
        mock_imap.IMAP4_SSL.return_value = mock_conn
        results = await client.fetch_unread()

    assert results == []
    mock_conn.select.assert_called_once_with("INBOX")
    mock_conn.search.assert_called_once_with(None, "UNSEEN")


@pytest.mark.asyncio
async def test_fetch_unread_returns_raw_emails(client):
    from email.mime.text import MIMEText

    msg = MIMEText("Test body", "plain", "utf-8")
    msg["Subject"] = "Test"
    msg["From"] = "sender@example.com"
    msg["Message-ID"] = "<test@example.com>"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    raw = msg.as_bytes()

    mock_conn = _make_mock_conn(messages={b"1": raw})
    with patch("genesis.mail.imap_client.imaplib") as mock_imap:
        mock_imap.IMAP4_SSL.return_value = mock_conn
        results = await client.fetch_unread()

    assert len(results) == 1
    assert results[0].uid == 1
    assert results[0].raw_bytes == raw


@pytest.mark.asyncio
async def test_fetch_unread_respects_max_count(client):
    messages = {str(i).encode(): b"data" for i in range(1, 20)}
    mock_conn = _make_mock_conn(messages=messages)
    with patch("genesis.mail.imap_client.imaplib") as mock_imap:
        mock_imap.IMAP4_SSL.return_value = mock_conn
        results = await client.fetch_unread(max_count=5)

    assert len(results) <= 5


@pytest.mark.asyncio
async def test_mark_read(client):
    mock_conn = _make_mock_conn()
    with patch("genesis.mail.imap_client.imaplib") as mock_imap:
        mock_imap.IMAP4_SSL.return_value = mock_conn
        await client.mark_read([1, 2, 3])

    assert mock_conn.store.call_count == 3


@pytest.mark.asyncio
async def test_connection_failure_returns_empty(client):
    with patch("genesis.mail.imap_client.imaplib") as mock_imap:
        mock_imap.IMAP4_SSL.side_effect = OSError("connection refused")
        results = await client.fetch_unread()

    assert results == []
