"""Genesis channel bridge — LEGACY FALLBACK entry point.

Runs the Telegram bot with ConversationLoop routing messages through CC CLI.
Bootstraps the full GenesisRuntime so all subsystems (awareness, memory,
learning, reflection, inbox) are active during user conversation.

genesis-server hosts the same Telegram adapter plus everything else, so the
bridge only has a job when the server is NOT running. Two full runtimes on
one install means duelling getUpdates pollers (telegram.error.Conflict, split
updates, broken approval buttons), two status.json writers, and duplicate
schedulers — so the bridge YIELDS at startup (exit 200, which the systemd
unit's RestartPreventExitStatus treats as do-not-restart) whenever the
genesis-server process lock is held.

Must run outside a CC session (same constraint as terminal.py).

Usage: python -m genesis.channels.bridge
"""

import asyncio
import logging
import os
import signal
import sys
import time

from genesis.cc.conversation import ConversationLoop
from genesis.cc.system_prompt import SystemPromptAssembler
from genesis.channels.config import load_channel_defaults as _load_channel_defaults  # noqa: F401
from genesis.env import secrets_path
from genesis.runtime import GenesisRuntime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("genesis.bridge")


def _load_bridge_config() -> dict | None:
    """Parse Telegram-specific config from the configured secrets.env.

    Does NOT set os.environ — GenesisRuntime._load_secrets() handles that.
    Returns bridge-specific values, or None if Telegram is not configured
    (missing/placeholder token). Exits on missing secrets.env (broken install).
    """
    path = str(secrets_path())
    if not os.path.exists(path):
        log.error("Secrets file not found: %s — cannot start (broken install?)", path)
        sys.exit(2)

    secrets: dict[str, str] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                secrets[key.strip()] = value.strip().strip('"')

    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "PLACEHOLDER":  # noqa: S105 - sentinel placeholder, not a credential
        log.info("TELEGRAM_BOT_TOKEN not set — Telegram adapter will not start")
        return None

    allowed_users: set[int] = set()
    allowed_raw = secrets.get("TELEGRAM_ALLOWED_USERS", "")
    if allowed_raw:
        for uid in allowed_raw.split(","):
            uid = uid.strip()
            if uid.isdigit():
                allowed_users.add(int(uid))
            elif uid:
                log.warning("Invalid UID in TELEGRAM_ALLOWED_USERS: %r", uid)

    if not allowed_users:
        log.error(
            "TELEGRAM_ALLOWED_USERS is empty or has no valid user IDs — "
            "Telegram will not start. Set numeric user IDs "
            "(get yours from @userinfobot on Telegram)"
        )
        return None

    # Optional forum chat ID for per-session topics
    forum_raw = secrets.get("TELEGRAM_FORUM_CHAT_ID", "")
    forum_chat_id = int(forum_raw) if forum_raw.strip().lstrip("-").isdigit() else None

    return {
        "token": token,
        "allowed_users": allowed_users,
        "whisper_model": secrets.get("WHISPER_MODEL", "whisper-large-v3"),
        "day_boundary_hour": int(secrets.get("DAY_BOUNDARY_HOUR", "0")),
        "forum_chat_id": forum_chat_id,
    }



# _load_channel_defaults is imported from genesis.channels.config above
# and re-exported for backwards compatibility with standalone.py.


def create_telegram_adapter(
    *,
    config: dict,
    conversation_loop: "ConversationLoop",
    runtime: "GenesisRuntime",
    tts_provider: object | None = None,
    config_loader: object | None = None,
    reply_waiter: object | None = None,
):
    """Single factory for the Telegram adapter.

    Shared by both ``bridge.py`` (standalone bridge process) and
    ``standalone.py`` (integrated server).  Having one construction site
    prevents parameter divergence — a missing ``proposal_workflow`` was
    silently breaking proposal resolution (PR #471).
    """
    from genesis.channels.telegram.adapter_v2 import TelegramAdapterV2

    autonomous_cli_gate = runtime.autonomous_cli_approval_gate
    if autonomous_cli_gate is None:
        log.warning(
            "Telegram adapter: autonomous_cli_approval_gate is None — "
            "inline approval buttons will not resolve.",
        )

    return TelegramAdapterV2(
        token=config["token"],
        conversation_loop=conversation_loop,
        allowed_users=config["allowed_users"],
        whisper_model=config["whisper_model"],
        tts_provider=tts_provider,
        config_loader=config_loader,
        reply_waiter=reply_waiter,
        engagement_tracker=runtime.engagement_tracker,
        autonomous_cli_gate=autonomous_cli_gate,
        proposal_workflow=runtime._ego_proposal_workflow,
    )


async def _run_headless(runtime: GenesisRuntime) -> None:
    """Keep the bridge process alive without Telegram.

    Background systems (awareness loop, learning scheduler, inbox monitor)
    were started by bootstrap() and need this process to stay alive.
    """
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received (headless)")
        stop_event.set()
        if runtime.awareness_loop is not None:
            runtime.awareness_loop.request_stop()

    ev_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        ev_loop.add_signal_handler(sig, _signal_handler)

    log.info("Bridge running headless — background systems active, waiting for signal")
    await stop_event.wait()
    await runtime.shutdown()
    log.info("Headless bridge stopped.")


async def main():
    # Bootstrap full Genesis runtime (DB, awareness, router, memory, learning, etc.)
    runtime = GenesisRuntime.instance()
    await runtime.bootstrap()

    if not runtime.is_bootstrapped or runtime.cc_invoker is None:
        log.error("GenesisRuntime bootstrap failed — cannot start bridge")
        sys.exit(1)

    # Re-probe before EITHER continuation (polling or headless): on a
    # simultaneous co-start the __main__ probe can pass before the server
    # acquires its lock, and even a headless bridge is a full duplicate
    # runtime (schedulers, status.json writer). By the end of our ~90s
    # bootstrap a co-started server definitely holds its lock.
    await _late_yield_check(runtime)

    config = _load_bridge_config()
    if config is None:
        log.info("No Telegram token configured — running headless for background systems")
        await _run_headless(runtime)
        return

    assembler = SystemPromptAssembler()
    # Inline failure detector for real-time procedure confidence updates
    failure_detector = None
    try:
        from genesis.learning.failure_detector import FailureDetector
        failure_detector = FailureDetector()
    except Exception:
        log.warning("Failed to initialize failure detector", exc_info=True)

    default_model, default_effort = _load_channel_defaults()

    conversation_loop = ConversationLoop(
        db=runtime.db,
        invoker=runtime.cc_invoker,
        assembler=assembler,
        day_boundary_hour=config["day_boundary_hour"],
        triage_pipeline=runtime.triage_pipeline,
        context_injector=runtime.context_injector,
        session_manager=runtime.session_manager,
        contingency=runtime.contingency_dispatcher,
        failure_detector=failure_detector,
        default_model=default_model,
        default_effort=default_effort,
    )

    # Resolve TTS provider (first available, if any)
    tts_provider = None
    tts_enabled = os.environ.get("TTS_ENABLED", "true").lower() not in ("false", "0", "no")
    if tts_enabled and runtime.provider_registry:
        from genesis.providers.types import ProviderCategory

        tts_providers = runtime.provider_registry.list_by_category(
            ProviderCategory.TTS
        )
        if tts_providers:
            tts_provider = tts_providers[0]
            log.info("TTS provider: %s", tts_provider.name)
    elif not tts_enabled:
        log.info("TTS disabled via TTS_ENABLED=false")

    # TTS config loader for hot-reloadable voice settings
    tts_config_loader = None
    if tts_provider:
        from genesis.channels.tts_config import TTSConfigLoader

        tts_config_loader = TTSConfigLoader()

    # Create ReplyWaiter for bidirectional outreach (send-and-wait-for-reply)
    from genesis.outreach.reply_waiter import ReplyWaiter

    reply_waiter = ReplyWaiter()
    if runtime.outreach_pipeline:
        runtime.outreach_pipeline.set_reply_waiter(reply_waiter)

    adapter = create_telegram_adapter(
        config=config,
        conversation_loop=conversation_loop,
        runtime=runtime,
        tts_provider=tts_provider,
        config_loader=tts_config_loader,
        reply_waiter=reply_waiter,
    )

    # Register adapter so outreach pipeline can deliver via Telegram
    # Recipient = first allowed user (outreach target)
    recipient = str(next(iter(config["allowed_users"]), "")) if config["allowed_users"] else ""
    runtime.register_channel("telegram", adapter, recipient=recipient)

    # Handle shutdown gracefully
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutdown signal received")
        stop_event.set()
        # Immediately signal awareness loop to stop retrying deferred work.
        # Without this, the loop can pick up a deferred item in the ~650ms
        # window between signal receipt and runtime.shutdown(), orphaning it
        # in "processing" state permanently.
        if runtime.awareness_loop is not None:
            runtime.awareness_loop.request_stop()

    ev_loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        ev_loop.add_signal_handler(sig, _signal_handler)

    await adapter.start()

    # Create TopicManager after start() — needs adapter._app.bot
    if config.get("forum_chat_id") and adapter._app:
        from genesis.channels.telegram.topics import TopicManager

        topic_manager = TopicManager(
            adapter._app.bot,
            config["forum_chat_id"],
            db=runtime.db,
        )
        await topic_manager.load_persisted()

        # Pre-create persistent category topics (including Approvals
        # so bare-text approval resolution in that topic works from
        # startup, not just after the first approval is delivered).
        for cat in (
            "conversation", "morning_report", "alert",
            "reflection_micro", "reflection_light", "reflection_deep", "reflection_strategic",
            "surplus", "recon", "approvals", "ego_proposals",
        ):
            await topic_manager.get_or_create_persistent(cat)

        # Wire into reflection bridge for output routing to topics
        if runtime.cc_reflection_bridge:
            runtime.cc_reflection_bridge.set_topic_manager(topic_manager)

        # Wire into outreach pipeline for routing messages to topics
        if runtime.outreach_pipeline:
            runtime.outreach_pipeline.set_topic_manager(topic_manager)
            runtime.outreach_pipeline.set_forum_chat_id(config["forum_chat_id"])

        # Wire into awareness loop for micro reflection posting
        if runtime.awareness_loop:
            runtime.awareness_loop.set_topic_manager(topic_manager)

        # Wire into surplus scheduler for surplus reflection posting
        if runtime.surplus_scheduler:
            runtime.surplus_scheduler.set_topic_manager(topic_manager)

        # Wire into ego proposal workflow for digest delivery
        if runtime._ego_proposal_workflow is not None:
            runtime._ego_proposal_workflow.set_topic_manager(topic_manager)

        # One-shot: close orphaned per-session topics from old code (March 24-27).
        # Checks DB for a sentinel category to avoid re-running on every restart.
        if topic_manager.get_thread_id("_orphan_cleanup_done") is None:
            valid_ids = set(topic_manager._persistent_topics.values())
            orphan_ids = set(range(10, 107)) - valid_ids
            if orphan_ids:
                closed = await topic_manager.close_orphaned_topics(orphan_ids)
                if closed:
                    log.info("Closed %d orphaned forum topics", closed)
            # Persist sentinel so this doesn't run again
            await topic_manager._persist_topic("_orphan_cleanup_done", 0)

        log.info("Forum topics enabled (chat_id=%s) — %d categories",
                 config["forum_chat_id"], len(topic_manager._persistent_topics))

    log.info("Bridge running with full Genesis runtime. Ctrl+C to stop.")

    # Periodic heartbeat so "is the bridge alive?" is answerable from the log
    _start_time = time.monotonic()

    async def _heartbeat():
        while not stop_event.is_set():
            # Update bridge health in status.json for the dashboard
            if runtime.status_writer:
                watchdog = getattr(adapter, "_watchdog", None)
                runtime.status_writer.set_extra_data("bridge", {
                    "adapter_started": getattr(adapter, "_app", None) is not None,
                    "polling_active": (
                        watchdog is not None
                        and getattr(watchdog, "is_running", False)
                    ),
                    "uptime_h": round(
                        (time.monotonic() - _start_time) / 3600, 2,
                    ),
                })
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1800)
                break  # stop_event was set
            except TimeoutError:
                uptime_h = (time.monotonic() - _start_time) / 3600
                log.info("Bridge heartbeat: uptime=%.1fh", uptime_h)

    from genesis.util.tasks import tracked_task

    heartbeat_task = tracked_task(_heartbeat(), name="bridge-heartbeat")

    await stop_event.wait()
    heartbeat_task.cancel()
    await adapter.stop()
    await runtime.shutdown()
    log.info("Bridge stopped.")


async def _late_yield_check(runtime, pid_dir=None) -> None:
    """Second yield probe, run right after bootstrap in main().

    Closes the co-start race the __main__ probe can't see: if the server was
    started in the window since that probe, shut the runtime down cleanly and
    exit 200 BEFORE this process polls getUpdates or continues headless
    (a headless bridge is still a full duplicate runtime).
    """
    from genesis.util.process_lock import EXIT_ALREADY_RUNNING, ProcessLock

    if ProcessLock.is_locked("genesis-server", pid_dir=pid_dir):
        log.critical(
            "genesis-server started during bridge bootstrap — yielding "
            "before polling begins (exit 200)"
        )
        await runtime.shutdown()
        sys.exit(EXIT_ALREADY_RUNNING)


def _yield_to_server(pid_dir=None) -> None:
    """Exit (code 200) if genesis-server is running — it owns the full stack.

    The check covers every start path (deploy-session restarts, watchdog
    fallback, manual start): whoever starts the bridge while the server is
    alive gets a clean refusal instead of a second runtime silently fighting
    the server for getUpdates, status.json, and the schedulers. Exit 200 is
    in the unit's RestartPreventExitStatus, so systemd does not crash-loop.
    """
    from genesis.util.process_lock import EXIT_ALREADY_RUNNING, ProcessLock

    if ProcessLock.is_locked("genesis-server", pid_dir=pid_dir):
        log.critical(
            "genesis-server is running — bridge yields Telegram/runtime "
            "ownership and exits (legacy fallback runs only when the server "
            "is down)"
        )
        sys.exit(EXIT_ALREADY_RUNNING)


if __name__ == "__main__":
    from genesis.util.process_lock import ProcessLock

    _yield_to_server()
    with ProcessLock("bridge"):
        asyncio.run(main())
