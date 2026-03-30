"""Genesis mail module — Gmail IMAP access and email recon batch processing."""

from genesis.mail.config import load_mail_config
from genesis.mail.imap_client import IMAPClient
from genesis.mail.monitor import MailMonitor
from genesis.mail.parser import parse_email
from genesis.mail.types import BatchResult, EmailBrief, MailConfig, ParsedEmail, RawEmail

__all__ = [
    "BatchResult",
    "EmailBrief",
    "IMAPClient",
    "MailConfig",
    "MailMonitor",
    "ParsedEmail",
    "RawEmail",
    "load_mail_config",
    "parse_email",
]
