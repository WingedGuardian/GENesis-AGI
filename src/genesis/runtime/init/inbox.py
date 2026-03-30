"""Init function: _init_inbox."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


async def init(rt: GenesisRuntime) -> None:
    """Initialize inbox monitor for watching ~/inbox/ for markdown files with URLs."""
    if rt._db is None or rt._cc_invoker is None or rt._session_manager is None:
        logger.warning(
            "Inbox skipped — missing prerequisites "
            "(db=%s, invoker=%s, session_mgr=%s)",
            rt._db is not None,
            rt._cc_invoker is not None,
            rt._session_manager is not None,
        )
        return

    try:
        from genesis.env import repo_root

        config_path = repo_root() / "config" / "inbox_monitor.yaml"
        if not config_path.exists():
            logger.info("No inbox_monitor.yaml — inbox monitor not configured")
            return

        from genesis.inbox.config import load_inbox_config

        config = load_inbox_config(config_path)
        if not config.enabled:
            logger.info("Inbox monitor disabled in config")
            return

        config.watch_path.mkdir(parents=True, exist_ok=True)

        from genesis.inbox.monitor import InboxMonitor
        from genesis.inbox.writer import ResponseWriter

        writer = ResponseWriter(
            watch_path=config.watch_path,
            timezone=config.timezone,
        )

        rt._inbox_monitor = InboxMonitor(
            db=rt._db,
            invoker=rt._cc_invoker,
            session_manager=rt._session_manager,
            config=config,
            writer=writer,
            event_bus=rt._event_bus,
            triage_pipeline=rt._triage_pipeline,
        )

        await rt._inbox_monitor.start()
        logger.info("Genesis inbox monitor started (watch=%s)", config.watch_path)
    except ImportError:
        logger.warning("genesis.inbox not available")
    except Exception:
        logger.exception("Failed to initialize inbox monitor")
