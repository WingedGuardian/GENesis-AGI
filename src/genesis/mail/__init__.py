"""Genesis mail module — Gmail IMAP access, email recon, and thread tracking."""

from genesis.mail.config import load_mail_config
from genesis.mail.imap_client import IMAPClient
from genesis.mail.monitor import MailMonitor
from genesis.mail.parser import parse_email
from genesis.mail.reply_handler import ReplyHandler
from genesis.mail.reply_poller import ReplyPoller
from genesis.mail.threads import ThreadTracker
from genesis.mail.types import BatchResult, EmailBrief, MailConfig, ParsedEmail, RawEmail

__all__ = [
    "BatchResult",
    "EmailBrief",
    "IMAPClient",
    "MailConfig",
    "MailMonitor",
    "ParsedEmail",
    "RawEmail",
    "ReplyHandler",
    "ReplyPoller",
    "ThreadTracker",
    "load_mail_config",
    "parse_email",
]
