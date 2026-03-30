"""Tests for mail types."""

from genesis.mail.types import BatchResult, MailConfig, ParsedEmail, RawEmail


def test_mail_config_defaults():
    cfg = MailConfig()
    assert cfg.enabled is True
    assert cfg.cron_expression == "0 5 * * 0"
    assert cfg.batch_size == 10
    assert cfg.model == "sonnet"
    assert cfg.max_emails_per_run == 50


def test_raw_email_frozen():
    raw = RawEmail(uid=1, raw_bytes=b"hello")
    assert raw.uid == 1
    assert raw.raw_bytes == b"hello"


def test_parsed_email_defaults():
    email = ParsedEmail(
        message_id="<abc@example.com>",
        imap_uid=42,
        sender="test@example.com",
        subject="Test",
        date="2026-03-27T00:00:00",
        body="Hello world",
    )
    assert email.urls == []
    assert email.has_attachments is False


def test_batch_result_defaults():
    result = BatchResult()
    assert result.fetched == 0
    assert result.errors == []
