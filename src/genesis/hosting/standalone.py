"""Standalone hosting adapter — Genesis running without a host framework.

Creates its own Flask app, registers dashboard blueprints, serves static
assets, and optionally starts Telegram.  Modeled on the bridge process
(``genesis.channels.bridge``) which already bootstraps the full runtime.

Usage:
    python -m genesis serve
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from flask import Flask, send_from_directory

if TYPE_CHECKING:
    from genesis.runtime import GenesisRuntime

logger = logging.getLogger("genesis.hosting.standalone")

_WEBUI_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "webui"
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "templates"


class StandaloneAdapter:
    """Genesis running itself — no host framework needed.

    Bootstrap order:
    1. ``GenesisRuntime.bootstrap(mode="full")``
    2. Create Flask app with vendored static assets
    3. Register dashboard, health, and outreach blueprints
    4. Start Flask in daemon thread, asyncio in main thread
    5. Optionally start Telegram adapter
    """

    name = "Standalone"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 5000,
        no_telegram: bool = False,
    ) -> None:
        self._host = host
        self._port = port
        self._no_telegram = no_telegram
        self._app: Flask | None = None
        self._runtime: GenesisRuntime | None = None
        self._shutdown_event = asyncio.Event()
        self._telegram_adapter = None
        self._loop: asyncio.AbstractEventLoop | None = None

    async def bootstrap(self) -> None:
        """Initialize GenesisRuntime and create the Flask app."""
        from genesis.runtime import GenesisRuntime

        self._runtime = GenesisRuntime.instance()
        await self._runtime.bootstrap(mode="full")

        if not self._runtime.is_bootstrapped:
            logger.error("GenesisRuntime bootstrap failed")
            return

        self._app = self._create_flask_app()
        self._register_blueprints()
        logger.info("Standalone adapter bootstrapped")

    async def serve(self) -> None:
        """Start Flask + optional Telegram.  Blocks until shutdown."""
        if self._app is None:
            logger.error("Cannot serve — bootstrap failed or not called")
            return

        # Capture the running asyncio loop so Flask threads can submit
        # coroutines via asyncio.run_coroutine_threadsafe().
        self._loop = asyncio.get_running_loop()

        # Create shared ConversationLoop for the OpenClaw endpoint.
        # Same pattern as _start_telegram() but without channel-specific
        # wiring (TTS, reply waiter, etc.).
        self._init_openclaw_conversation_loop()

        # Flask in daemon thread
        flask_thread = threading.Thread(
            target=self._run_flask,
            daemon=True,
            name="genesis-flask",
        )
        flask_thread.start()
        logger.info(
            "Dashboard at http://%s:%d/genesis", self._host, self._port,
        )

        # Telegram if configured
        if not self._no_telegram:
            await self._start_telegram()

        # Periodic heartbeat
        start_time = time.monotonic()

        async def _heartbeat():
            while not self._shutdown_event.is_set():
                # Update status_writer so dashboard health panel has fresh data
                if self._runtime and self._runtime.status_writer:
                    self._runtime.status_writer.set_extra_data("standalone", {
                        "flask_running": self._app is not None,
                        "telegram_active": self._telegram_adapter is not None,
                        "uptime_h": round(
                            (time.monotonic() - start_time) / 3600, 2,
                        ),
                    })
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(), timeout=1800,
                    )
                    break
                except TimeoutError:
                    uptime_h = (time.monotonic() - start_time) / 3600
                    logger.info("Standalone heartbeat: uptime=%.1fh", uptime_h)

        from genesis.util.tasks import tracked_task

        heartbeat_task = tracked_task(_heartbeat(), name="standalone-heartbeat")

        # Block until shutdown
        await self._shutdown_event.wait()
        heartbeat_task.cancel()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutdown requested")
        self._shutdown_event.set()

        if self._runtime and self._runtime.awareness_loop is not None:
            self._runtime.awareness_loop.request_stop()

        if self._telegram_adapter is not None:
            try:
                await self._telegram_adapter.stop()
            except Exception:
                logger.warning("Telegram adapter stop failed", exc_info=True)

        if self._runtime:
            await self._runtime.shutdown()

        logger.info("Standalone adapter stopped")

    def get_flask_app(self) -> Flask | None:
        """Return the Flask app for external use."""
        return self._app

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _init_openclaw_conversation_loop(self) -> None:
        """Create a ConversationLoop for the OpenClaw completions endpoint.

        Stored in Flask app config so the completions blueprint can access
        it from request context.  Also stores the asyncio loop reference
        so Flask threads can submit coroutines to the main loop.
        """
        if self._app is None or self._runtime is None:
            return

        rt = self._runtime
        if rt.cc_invoker is None or rt.db is None:
            logger.warning("CC invoker or DB unavailable — OpenClaw ConversationLoop skipped")
            return

        try:
            from genesis.cc.conversation import ConversationLoop
            from genesis.cc.system_prompt import SystemPromptAssembler

            assembler = SystemPromptAssembler()

            failure_detector = None
            try:
                from genesis.learning.failure_detector import FailureDetector

                failure_detector = FailureDetector()
            except Exception:
                logger.warning("Failed to init failure detector for OpenClaw", exc_info=True)

            conversation_loop = ConversationLoop(
                db=rt.db,
                invoker=rt.cc_invoker,
                assembler=assembler,
                triage_pipeline=rt.triage_pipeline,
                context_injector=rt.context_injector,
                session_manager=rt.session_manager,
                contingency=rt.contingency_dispatcher,
                failure_detector=failure_detector,
            )

            self._app.config["OPENCLAW_CONVERSATION_LOOP"] = conversation_loop
            self._app.config["GENESIS_EVENT_LOOP"] = self._loop
            logger.info("OpenClaw ConversationLoop initialized")
        except Exception:
            logger.exception("Failed to initialize OpenClaw ConversationLoop")

    def _create_flask_app(self) -> Flask:
        """Create Flask app with vendored static assets."""
        webui_dir = _WEBUI_DIR if _WEBUI_DIR.exists() else None

        if webui_dir is None:
            logger.warning(
                "Vendored webui not found at %s — static assets won't load. "
                "Run scripts/vendor_assets.sh to fix.",
                _WEBUI_DIR,
            )

        app = Flask(
            "genesis",
            static_folder=str(webui_dir) if webui_dir else None,
            static_url_path="/",
        )

        # ── Auth: secret key + session config ─────────────────────
        from datetime import timedelta

        from genesis.dashboard.auth import get_or_create_secret_key

        app.secret_key = get_or_create_secret_key()
        app.config["SESSION_COOKIE_HTTPONLY"] = True
        app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
        app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
        app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024  # 500 MB (knowledge uploads)

        # Login page (on app, not blueprint — must be reachable before auth)
        @app.route("/genesis/login")
        def genesis_login_page():
            from genesis.dashboard.auth import (
                get_dashboard_password,
                login_page_html,
            )

            if not get_dashboard_password():
                from flask import redirect

                return redirect("/genesis")
            return login_page_html()

        # /genesis/monitor — standalone-only convenience alias.
        # /genesis, /genesis/logs, /genesis/errors are registered by the
        # dashboard blueprint (api.py, routes/events.py) — no duplication.
        @app.route("/genesis/monitor")
        def genesis_monitor_page():
            return send_from_directory(str(_TEMPLATE_DIR), "neural_monitor.html")

        # Root redirect to dashboard
        @app.route("/")
        def root_redirect():
            from flask import redirect

            return redirect("/genesis")

        return app

    def _register_blueprints(self) -> None:
        """Register all Genesis blueprints on the Flask app."""
        app = self._app
        if app is None:
            raise RuntimeError("Flask app not created — call bootstrap() first")

        # Dashboard blueprint (all /api/genesis/* routes, SSE stream, etc.)
        try:
            from genesis.dashboard.api import blueprint as dash_bp

            if "genesis_dashboard" not in app.blueprints:
                app.register_blueprint(dash_bp)
                logger.info("Dashboard blueprint registered")

                try:
                    from genesis.dashboard.heartbeat import DashboardHeartbeat

                    DashboardHeartbeat(interval_seconds=60).start()
                except Exception:
                    logger.exception("Failed to start dashboard heartbeat")
        except Exception:
            logger.exception("Failed to register dashboard blueprint")

        # Terminal WebSocket
        try:
            from genesis.dashboard.routes.terminal import register_terminal_ws

            register_terminal_ws(app)
        except Exception:
            logger.exception("Failed to register terminal WebSocket")

        # Outreach API blueprint
        try:
            from genesis.outreach.api import init_outreach_api, outreach_api
            from genesis.runtime import GenesisRuntime

            rt = GenesisRuntime.instance()
            if rt.db:
                init_outreach_api(db=rt.db)

            if "outreach_api" not in app.blueprints:
                app.register_blueprint(outreach_api)
                logger.info("Outreach API blueprint registered")
        except Exception:
            logger.exception("Failed to register outreach blueprint")

        try:
            from genesis.hosting.openclaw.adapter import OpenClawAdapter

            OpenClawAdapter().register_blueprints(app)
        except Exception:
            logger.exception("OpenClaw adapter registration failed")

    def _run_flask(self) -> None:
        """Run Flask in a thread (called from daemon thread)."""
        self._app.run(
            host=self._host,
            port=self._port,
            threaded=True,
            use_reloader=False,
        )

    async def _start_telegram(self) -> None:
        """Load and start Telegram adapter if configured.

        Reuses the loading logic from bridge.py, including TopicManager
        wiring for forum topic routing (reflection, outreach, awareness).
        """
        try:
            from genesis.channels.bridge import _load_bridge_config

            config = _load_bridge_config()
            if config is None:
                logger.info("No Telegram token — running dashboard-only")
                return

            from genesis.cc.conversation import ConversationLoop
            from genesis.cc.system_prompt import SystemPromptAssembler

            assembler = SystemPromptAssembler()

            failure_detector = None
            try:
                from genesis.learning.failure_detector import FailureDetector

                failure_detector = FailureDetector()
            except Exception:
                logger.warning("Failed to init failure detector", exc_info=True)

            rt = self._runtime
            conversation_loop = ConversationLoop(
                db=rt.db,
                invoker=rt.cc_invoker,
                assembler=assembler,
                day_boundary_hour=config["day_boundary_hour"],
                triage_pipeline=rt.triage_pipeline,
                context_injector=rt.context_injector,
                session_manager=rt.session_manager,
                contingency=rt.contingency_dispatcher,
                failure_detector=failure_detector,
            )

            # TTS
            import os

            tts_provider = None
            tts_enabled = os.environ.get(
                "TTS_ENABLED", "true",
            ).lower() not in ("false", "0", "no")
            if tts_enabled and rt.provider_registry:
                from genesis.providers.types import ProviderCategory

                tts_providers = rt.provider_registry.list_by_category(
                    ProviderCategory.TTS,
                )
                if tts_providers:
                    tts_provider = tts_providers[0]

            tts_config_loader = None
            if tts_provider:
                from genesis.channels.tts_config import TTSConfigLoader

                tts_config_loader = TTSConfigLoader()

            # Reply waiter
            from genesis.outreach.reply_waiter import ReplyWaiter

            reply_waiter = ReplyWaiter()
            if rt.outreach_pipeline:
                rt.outreach_pipeline.set_reply_waiter(reply_waiter)

            # Create adapter
            from genesis.channels.telegram.adapter_v2 import (
                TelegramAdapterV2 as AdapterCls,
            )

            # Inject the autonomous CLI approval gate so handle_callback_query
            # can resolve cli_approve/cli_approve_all inline buttons and the
            # text handler can resolve bare approve/reject typed in the
            # Approvals topic.  If the gate is None (autonomy init failed),
            # approvals still deliver but must be resolved via the dashboard
            # approvals API.  Mirrors channels/bridge.py:178-196.
            autonomous_cli_gate = rt.autonomous_cli_approval_gate
            if autonomous_cli_gate is None:
                logger.warning(
                    "standalone Telegram: autonomous_cli_approval_gate is "
                    "None — inline approval buttons will not resolve. "
                    "Dashboard-only approval fallback is still available.",
                )

            adapter = AdapterCls(
                token=config["token"],
                conversation_loop=conversation_loop,
                allowed_users=config["allowed_users"],
                whisper_model=config["whisper_model"],
                tts_provider=tts_provider,
                config_loader=tts_config_loader,
                reply_waiter=reply_waiter,
                engagement_tracker=rt.engagement_tracker,
                autonomous_cli_gate=autonomous_cli_gate,
            )
            self._telegram_adapter = adapter

            # Register channel
            recipient = (
                str(next(iter(config["allowed_users"]), ""))
                if config["allowed_users"]
                else ""
            )
            rt.register_channel("telegram", adapter, recipient=recipient)

            await adapter.start()
            logger.info("Telegram adapter started")

            # TopicManager wiring — forum topic routing for reflections,
            # outreach, and awareness.  Ported from channels/bridge.py.
            if config.get("forum_chat_id") and adapter._app:
                from genesis.channels.telegram.topics import TopicManager

                topic_manager = TopicManager(
                    adapter._app.bot,
                    config["forum_chat_id"],
                    db=rt.db,
                )
                await topic_manager.load_persisted()

                # Pre-create persistent category topics (including Approvals
                # so bare-text approval resolution in that topic works from
                # startup, not just after the first approval is delivered).
                # Mirrors channels/bridge.py:236-241.
                for cat in (
                    "conversation", "morning_report", "alert",
                    "reflection_micro", "reflection_light",
                    "reflection_deep", "reflection_strategic",
                    "surplus", "recon", "approvals",
                ):
                    await topic_manager.get_or_create_persistent(cat)

                if rt.cc_reflection_bridge:
                    rt.cc_reflection_bridge.set_topic_manager(topic_manager)
                if rt.outreach_pipeline:
                    rt.outreach_pipeline.set_topic_manager(topic_manager)
                    rt.outreach_pipeline.set_forum_chat_id(config["forum_chat_id"])
                if rt.awareness_loop:
                    rt.awareness_loop.set_topic_manager(topic_manager)
                if rt.surplus_scheduler:
                    rt.surplus_scheduler.set_topic_manager(topic_manager)

                logger.info(
                    "Forum topics enabled (chat_id=%s) — %d categories",
                    config["forum_chat_id"],
                    len(topic_manager._persistent_topics),
                )

        except Exception:
            logger.exception("Failed to start Telegram adapter")
