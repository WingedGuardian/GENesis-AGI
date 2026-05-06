"""Data types for the mail module."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MailConfig:
    """Configuration for the mail monitor."""

    enabled: bool = True
    cron_expression: str = "0 5 * * 0"  # Sunday 05:00 UTC
    batch_size: int = 10  # Max emails per Layer 2 CC session
    model: str = "sonnet"  # Layer 2 model
    effort: str = "medium"  # Layer 2 effort
    timeout_s: int = 600  # Layer 2 CC session timeout
    max_retries: int = 3
    imap_timeout_s: int = 30
    max_emails_per_run: int = 50  # Safety cap per batch run
    # timezone removed — uses genesis.env.user_timezone()
    min_relevance: int = 3  # Layer 1 filter: only relevance >= this goes to judge


@dataclass(frozen=True)
class RawEmail:
    """Raw email bytes fetched from IMAP."""

    uid: int  # IMAP UID
    raw_bytes: bytes  # Full RFC 2822 message bytes


@dataclass(frozen=True)
class ParsedEmail:
    """Parsed email with extracted fields."""

    message_id: str  # RFC 2822 Message-ID (dedup key)
    imap_uid: int  # IMAP UID (for marking read)
    sender: str  # Decoded From header
    subject: str  # Decoded Subject header
    date: str  # Date header, ISO format
    body: str  # Extracted text body
    urls: list[str] = field(default_factory=list)
    has_attachments: bool = False


@dataclass(frozen=True)
class EmailBrief:
    """Layer 1 (paralegal) output — structured findings from Gemini."""

    email_index: int  # 1-based index matching input order
    sender: str
    subject: str
    classification: str  # AI_Agent, Competitive, Research, Newsletter, Operational
    relevance: int  # 1-5
    key_findings: list[str] = field(default_factory=list)
    assessment: str = ""
    recommendation: str = ""


@dataclass
class BatchResult:
    """Result of a single batch run."""

    fetched: int = 0
    already_known: int = 0
    layer1_briefed: int = 0
    layer1_low_signal: int = 0
    layer2_kept: int = 0
    layer2_discarded: int = 0
    layer2_failed: int = 0
    errors: list[str] = field(default_factory=list)
