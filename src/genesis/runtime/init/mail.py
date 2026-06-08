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

        # --- Reply poller: lightweight 4h IMAP check for thread replies ---
        session_runner = getattr(rt, "_direct_session_runner", None)
        if session_runner is not None:
            try:
                from genesis.mail.reply_handler import ReplyHandler
                from genesis.mail.reply_poller import ReplyPoller
                from genesis.mail.threads import ThreadTracker

                thread_tracker = ThreadTracker(rt._db)
                reply_handler = ReplyHandler(
                    session_runner=session_runner,
                    thread_tracker=thread_tracker,
                )
                reply_poller = ReplyPoller(
                    imap_client=imap_client,
                    thread_tracker=thread_tracker,
                    on_reply=reply_handler.handle_reply,
                    on_stale_thread=reply_handler.handle_follow_up,
                )
                rt._thread_tracker = thread_tracker
                rt._reply_poller = reply_poller

                # Register the reply poller on the mail monitor's scheduler
                _register_reply_poll_job(rt._mail_monitor, reply_poller)
                logger.info("Email reply poller registered (every 4h)")
            except Exception:
                logger.exception("Failed to initialize reply poller (mail monitor still active)")
        else:
            logger.info("Reply poller skipped — DirectSessionRunner not available")

    except ImportError:
        logger.warning("genesis.mail not available")
    except Exception:
        logger.exception("Failed to initialize mail monitor")


def _register_reply_poll_job(monitor, reply_poller) -> None:
    """Register the reply poller as a cron job on the mail monitor's scheduler."""
    if monitor._scheduler is None:
        logger.warning("Cannot register reply poller — scheduler not started")
        return

    from apscheduler.triggers.cron import CronTrigger

    from genesis.env import user_timezone

    tz = user_timezone()

    async def _poll_safe() -> None:
        try:
            await reply_poller.poll()
        except Exception:
            logger.exception("Reply poller cycle failed")

    monitor._scheduler.add_job(
        _poll_safe,
        CronTrigger(hour="*/4", minute=15, timezone=tz),  # :15 to offset from monitor
        id="mail_reply_poller",
        max_instances=1,
        misfire_grace_time=3600,  # 1h grace for 4h interval
    )
