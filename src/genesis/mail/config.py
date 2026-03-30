"""Config loader for mail_monitor.yaml."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from genesis.mail.types import MailConfig

logger = logging.getLogger(__name__)


def load_mail_config(path: Path) -> MailConfig:
    """Load mail monitor config from YAML file."""
    import yaml

    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    section = raw.get("mail_monitor", raw)
    return MailConfig(
        enabled=section.get("enabled", True),
        cron_expression=section.get("cron_expression", "0 5 * * 0"),
        batch_size=section.get("batch_size", 10),
        model=section.get("model", "sonnet"),
        effort=section.get("effort", "medium"),
        timeout_s=section.get("timeout_s", 600),
        max_retries=section.get("max_retries", 3),
        imap_timeout_s=section.get("imap_timeout_s", 30),
        max_emails_per_run=section.get("max_emails_per_run", 50),
        timezone=section.get("timezone", os.environ.get("USER_TIMEZONE", "America/New_York")),
        min_relevance=section.get("min_relevance", 3),
    )
