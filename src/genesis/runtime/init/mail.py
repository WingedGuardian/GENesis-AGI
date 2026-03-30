"""Init function: _init_mail."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize mail monitor for polling Gmail inbox."""
    if rt._db is None or rt._cc_invoker is None or rt._session_manager is None:
        logger.warning(
            "Mail skipped — missing prerequisites "
            "(db=%s, invoker=%s, session_mgr=%s)",
            rt._db is not None,
            rt._cc_invoker is not None,
            rt._session_manager is not None,
        )
        return

    if rt._router is None:
        logger.warning("Mail skipped — router not available (needed for Layer 1 triage)")
        return

    try:
        from genesis.env import repo_root

        config_path = repo_root() / "config" / "mail_monitor.yaml"
        if not config_path.exists():
            logger.info("No mail_monitor.yaml — mail monitor not configured")
            return

        from genesis.mail.config import load_mail_config

        config = load_mail_config(config_path)
        if not config.enabled:
            logger.info("Mail monitor disabled in config")
            return

        address = os.environ.get("GENESIS_GMAIL_ADDRESS")
        password = os.environ.get("GENESIS_GMAIL_APP_PASSWORD")
        if not address or not password:
            logger.warning(
                "Mail monitor skipped — GENESIS_GMAIL_ADDRESS or "
                "GENESIS_GMAIL_APP_PASSWORD not set in secrets.env"
            )
            return

        from genesis.mail.imap_client import IMAPClient
        from genesis.mail.monitor import MailMonitor

        imap_client = IMAPClient(
            address=address, app_password=password, timeout=config.imap_timeout_s,
        )

        rt._mail_monitor = MailMonitor(
            db=rt._db,
            config=config,
            imap_client=imap_client,
            router=rt._router,
            invoker=rt._cc_invoker,
            session_manager=rt._session_manager,
            event_bus=rt._event_bus,
            triage_pipeline=rt._triage_pipeline,
            on_job_success=rt.record_job_success,
            on_job_failure=rt.record_job_failure,
        )

        await rt._mail_monitor.start()
        logger.info("Genesis mail monitor started (cron=%s)", config.cron_expression)
    except ImportError:
        logger.warning("genesis.mail not available")
    except Exception:
        logger.exception("Failed to initialize mail monitor")
