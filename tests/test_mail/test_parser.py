"""Tests for email MIME parser."""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from genesis.mail.parser import parse_email


def _make_plain_email(
    *,
    subject: str = "Test Subject",
    sender: str = "alice@example.com",
    body: str = "Hello world",
    message_id: str = "<test-123@example.com>",
    date: str = "Thu, 27 Mar 2026 10:00:00 +0000",
) -> bytes:
    """Build a simple text/plain email as raw bytes."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["Message-ID"] = message_id
    msg["Date"] = date
    return msg.as_bytes()


def _make_html_email(*, html: str = "<p>Hello</p>") -> bytes:
    """Build a text/html-only email."""
    msg = MIMEText(html, "html", "utf-8")
    msg["Subject"] = "HTML Only"
    msg["From"] = "bob@example.com"
    msg["Message-ID"] = "<html-1@example.com>"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    return msg.as_bytes()


def _make_multipart_email(*, plain: str = "Plain text", html: str = "<p>HTML</p>") -> bytes:
    """Build a multipart/alternative email with plain + HTML."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Multipart"
    msg["From"] = "charlie@example.com"
    msg["Message-ID"] = "<multi-1@example.com>"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    msg.attach(MIMEText(plain, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))
    return msg.as_bytes()


def _make_email_with_attachment() -> bytes:
    """Build a multipart/mixed email with text + attachment."""
    msg = MIMEMultipart("mixed")
    msg["Subject"] = "With Attachment"
    msg["From"] = "dave@example.com"
    msg["Message-ID"] = "<attach-1@example.com>"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    msg.attach(MIMEText("See attached", "plain", "utf-8"))
    att = MIMEText("file content", "plain", "utf-8")
    att.add_header("Content-Disposition", "attachment", filename="readme.txt")
    msg.attach(att)
    return msg.as_bytes()


def test_parse_plain_email():
    raw = _make_plain_email(body="Hello world")
    result = parse_email(raw, uid=42)
    assert result.message_id == "<test-123@example.com>"
    assert result.imap_uid == 42
    assert result.sender == "alice@example.com"
    assert result.subject == "Test Subject"
    assert "Hello world" in result.body
    assert result.has_attachments is False


def test_parse_html_fallback():
    raw = _make_html_email(html="<p>Important <b>news</b> here</p>")
    result = parse_email(raw, uid=1)
    assert result.message_id == "<html-1@example.com>"
    assert "Important" in result.body
    assert "news" in result.body
    assert "<p>" not in result.body  # tags stripped


def test_parse_multipart_prefers_plain():
    raw = _make_multipart_email(plain="Plain version", html="<p>HTML version</p>")
    result = parse_email(raw, uid=1)
    assert "Plain version" in result.body
    assert "<p>" not in result.body


def test_parse_detects_attachments():
    raw = _make_email_with_attachment()
    result = parse_email(raw, uid=1)
    assert result.has_attachments is True
    assert "See attached" in result.body


def test_parse_extracts_urls():
    body = "Check out https://example.com/article and http://test.org/page"
    raw = _make_plain_email(body=body)
    result = parse_email(raw, uid=1)
    assert "https://example.com/article" in result.urls
    assert "http://test.org/page" in result.urls


def test_parse_missing_message_id():
    msg = MIMEText("body", "plain", "utf-8")
    msg["Subject"] = "No ID"
    msg["From"] = "test@example.com"
    msg["Date"] = "Thu, 27 Mar 2026 10:00:00 +0000"
    # No Message-ID header
    result = parse_email(msg.as_bytes(), uid=99)
    assert result.message_id  # Should generate a fallback
    assert result.imap_uid == 99


def test_parse_truncates_long_body():
    long_body = "A" * 60_000
    raw = _make_plain_email(body=long_body)
    result = parse_email(raw, uid=1)
    assert len(result.body) <= 50_001 + 20  # 50K + truncation marker
