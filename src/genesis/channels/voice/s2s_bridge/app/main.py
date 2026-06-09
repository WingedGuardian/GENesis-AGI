"""Main application entry point using Pipecat.

Genesis voice S2S bridge — connects Voice PE to OpenAI Realtime API
with Genesis tool dispatch.  Forked from fjfricke/ha-openai-realtime,
adapted to call Genesis HTTP endpoints instead of HA MCP.
"""
import asyncio
import json
import logging
import os
import sys

import dotenv
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.websocket.server import WebsocketServerTransport

from app.audio_recording_service import AudioRecordingService
from app.disconnect_tool import create_disconnect_tool_handler, get_disconnect_tool_definition
from app.genesis_tool_service import GenesisToolService
from app.session_manager import SessionManager
from app.websocket_handler import WebSocketHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Reduce verbosity of noisy loggers
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("__main__").setLevel(logging.INFO)

dotenv.load_dotenv()


class Application:
    """Main application class using Pipecat."""

    def __init__(self):
        """Initialize application."""
        self.pipeline: Pipeline | None = None
        self.runner: PipelineRunner | None = None
        self.websocket_handler: WebSocketHandler | None = None
        self.websocket_transport: WebsocketServerTransport | None = None
        self.openai_service: OpenAIRealtimeLLMService | None = None
        self.genesis_tool_service: GenesisToolService | None = None
        self.audio_recording_service: AudioRecordingService | None = None
        self.session_manager: SessionManager | None = None
        self.current_task: PipelineTask | None = None
        self._pipeline_lock: asyncio.Lock | None = None

    async def initialize(self) -> None:
        """Initialize all components."""
        # Get configuration from environment
        openai_api_key = os.environ.get("OPENAI_API_KEY")
        websocket_port = int(os.environ.get("WEBSOCKET_PORT", "8080"))
        websocket_host = os.environ.get("WEBSOCKET_HOST", "0.0.0.0")

        # Get turn detection settings with defaults
        vad_threshold = float(os.environ.get("VAD_THRESHOLD", "0.5"))
        vad_prefix_padding_ms = int(os.environ.get("VAD_PREFIX_PADDING_MS", "300"))
        vad_silence_duration_ms = int(os.environ.get("VAD_SILENCE_DURATION_MS", "500"))

        # Get recording setting (optional, defaults to false)
        enable_recording = os.environ.get("ENABLE_RECORDING", "false").lower() == "true"

        # Get session reuse timeout and initialize session manager
        session_reuse_timeout = float(os.environ.get("SESSION_REUSE_TIMEOUT_SECONDS", "300"))
        self.session_manager = SessionManager(reuse_timeout=session_reuse_timeout)
        logger.info(f"Session reuse timeout: {session_reuse_timeout} seconds")

        if not openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

        # Initialize Genesis tool service — fetches tools + system prompt
        genesis_url = os.environ.get("GENESIS_URL", "http://localhost:5000")
        genesis_token = os.environ.get("GENESIS_TOKEN", "")
        self.genesis_tool_service = GenesisToolService(genesis_url, genesis_token)

        # Fetch system prompt and tool declarations from Genesis
        instructions = ""
        self._genesis_tools: list[dict] = []
        try:
            instructions = await self.genesis_tool_service.get_system_prompt()
            self._genesis_tools = await self.genesis_tool_service.get_tool_declarations()
            logger.info(
                "Genesis tools loaded: %s",
                [t.get("name") for t in self._genesis_tools],
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Genesis tools/prompt: {e}")
            instructions = os.environ.get(
                "INSTRUCTIONS",
                "You are Genesis, a cognitive AI partner.",
            )

        # Initialize audio recording service before WebSocket handler (handler needs it)
        self.audio_recording_service = AudioRecordingService(
            enable_recording=enable_recording,
            sample_rate=24000,
            chunk_duration_seconds=30,
            output_dir="recordings"
        )

        # Initialize WebSocket handler
        self.websocket_handler = WebSocketHandler(
            host=websocket_host,
            port=websocket_port,
            session_manager=self.session_manager,
            audio_recording_service=self.audio_recording_service
        )
        self.websocket_transport = self.websocket_handler.create_transport()

        # Store configuration for session creation
        self.openai_api_key = openai_api_key
        self.vad_threshold = vad_threshold
        self.vad_prefix_padding_ms = vad_prefix_padding_ms
        self.vad_silence_duration_ms = vad_silence_duration_ms
        self.instructions = instructions

        logger.info("✅ Application initialized - ready to accept WebSocket connections")

    def _build_pipeline_for_transport(self, transport: WebsocketServerTransport, client_id: str):
        """
        Build pipeline for a WebSocket transport connection.

        Args:
            transport: The WebSocket transport instance
            client_id: Unique identifier for the client device
        """
        # Ensure OpenAI service exists
        if self.openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")

        # Use WebSocket handler to build pipeline
        self.pipeline, self.runner, self.current_task = self.websocket_handler.build_pipeline(
            transport=transport,
            openai_service=self.openai_service,
            client_id=client_id,
            activity_callback=self._update_session_activity
        )

    def _update_session_activity(self):
        """Update session activity timestamp (called by SessionActivityTracker)."""
        pass

    async def _ensure_openai_service(self, client_id: str | None = None):
        """Ensure the OpenAI service is ready for a client.

        On first call (startup): creates a new service instance, registers tools.
        On subsequent calls (client connect): resets the existing service's
        conversation to get a fresh OpenAI session (refreshes the 60-min clock)
        while keeping the same service object in the pipeline.

        Args:
            client_id: Optional client ID for session management
        """
        if self._pipeline_lock is None:
            self._pipeline_lock = asyncio.Lock()

        async with self._pipeline_lock:
            # If service already exists, reset its conversation to get a fresh
            # OpenAI session. This is much lighter than creating a new service
            # (which would orphan the pipeline's reference to the old one).
            if self.openai_service is not None and client_id is not None:
                logger.info(f"🔄 Resetting OpenAI session for client {client_id}...")
                try:
                    self.session_manager.cleanup_before_new_session(client_id)
                except Exception as e:
                    logger.warning(f"⚠️ Error caching context: {e}")

                try:
                    await self.openai_service.reset_conversation()
                    logger.info(f"✅ OpenAI session reset for client {client_id}")
                except Exception as e:
                    logger.warning(f"⚠️ Session reset failed, creating new service: {e}")
                    # Fall through to create a new service
                    self.openai_service = None

                if self.openai_service is not None:
                    # Register service with session manager
                    self.session_manager.set_current_service(client_id, self.openai_service)
                    return self.openai_service

            # First call or reset failed — create a brand new service
            if client_id:
                logger.info(f"🆕 Creating new OpenAI service for client {client_id}...")
            else:
                logger.info("🆕 Creating new OpenAI service (initial)...")

            # Create session properties with audio configuration
            from pipecat.services.openai.realtime.events import (
                AudioConfiguration,
                AudioInput,
                AudioOutput,
                SessionProperties,
                TurnDetection,
            )

            # Collect tool definitions: disconnect + Genesis tools
            disconnect_tool_def = get_disconnect_tool_definition()
            all_tools = [disconnect_tool_def] + list(self._genesis_tools)

            session_properties = SessionProperties(
                instructions=self.instructions,
                audio=AudioConfiguration(
                    input=AudioInput(
                        turn_detection=TurnDetection(
                            type="server_vad",
                            threshold=self.vad_threshold,
                            prefix_padding_ms=self.vad_prefix_padding_ms,
                            silence_duration_ms=self.vad_silence_duration_ms
                        )
                    ),
                    output=AudioOutput(voice="marin")
                ),
                tools=all_tools
            )

            logger.info(f"🔧 Creating session with {len(all_tools)} tools: {[tool.get('name', 'unknown') for tool in all_tools]}")

            # Create new service instance
            self.openai_service = OpenAIRealtimeLLMService(
                api_key=self.openai_api_key,
                model="gpt-realtime",
                session_properties=session_properties,
                start_audio_paused=False
            )
            logger.info(f"✅ OpenAI Service created: {type(self.openai_service).__name__}")

            # Register disconnect tool handler
            disconnect_tool_handler = create_disconnect_tool_handler(self.websocket_transport)
            self.openai_service.register_function("disconnect_client", disconnect_tool_handler)
            logger.info("Registered disconnect tool handler")

            # Register Genesis tool handlers — dispatch via HTTP to Genesis
            for tool_def in self._genesis_tools:
                tool_name = tool_def.get("name", "")
                if not tool_name:
                    continue

                async def genesis_tool_handler(params):
                    """Dispatch tool call to Genesis via HTTP."""
                    try:
                        result = await self.genesis_tool_service.call_tool(
                            params.function_name, params.arguments,
                        )
                        await params.result_callback(json.dumps(result))
                    except Exception as exc:
                        logger.error("Genesis tool %s failed: %s", params.function_name, exc)
                        await params.result_callback(
                            json.dumps({"error": str(exc)}),
                        )

                self.openai_service.register_function(tool_name, genesis_tool_handler)

            logger.info(
                "Registered %d Genesis tools: %s",
                len(self._genesis_tools),
                [t.get("name") for t in self._genesis_tools],
            )

            # Register service with session manager
            if client_id:
                self.session_manager.set_current_service(client_id, self.openai_service)

            logger.info("✅ New OpenAI Session created")
            return self.openai_service

    async def run(self) -> None:
        """Run the application."""
        await self.initialize()

        # Create initial OpenAI service (will be replaced per connection)
        await self._ensure_openai_service()

        # Build pipeline - based on pipecat-examples, one pipeline handles all connections
        # The transport manages multiple connections internally
        self._build_pipeline_for_transport(self.websocket_transport, "server")

        # Setup WebSocket event handlers
        async def on_client_connected(client_id: str):
            """Handle new client connection."""
            await self._ensure_openai_service(client_id=client_id)
            if self.audio_recording_service:
                self.audio_recording_service.start_new_session(client_id)

        def on_client_disconnected(client_id: str):
            """Handle client disconnection."""
            if self.session_manager:
                self.session_manager.handle_client_disconnect(client_id, self.openai_service)
            if self.audio_recording_service:
                self.audio_recording_service.stop_recording()

        # Function to get OpenAI service for a client
        def get_openai_service_for_client(client_id: str) -> OpenAIRealtimeLLMService | None:
            """Get OpenAI service for a specific client."""
            if self.session_manager:
                return self.session_manager.get_current_service(client_id)
            return self.openai_service

        self.websocket_handler.setup_event_handlers(
            transport=self.websocket_transport,
            on_client_connected_callback=on_client_connected,
            on_client_disconnected_callback=on_client_disconnected,
            openai_service_getter=get_openai_service_for_client
        )

        try:
            # Start the pipeline runner - this will start the WebSocket server
            # Based on pipecat-examples: PipelineRunner.run() starts the transport server
            logger.info("✅ Starting WebSocket server and pipeline...")
            await self.runner.run(self.current_task)
        except KeyboardInterrupt:
            logger.info("Received keyboard interrupt")
        except Exception as e:
            logger.error(f"Fatal error: {e}", exc_info=True)
            raise
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        logger.info("Cleaning up application...")

        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")

        if self.websocket_handler:
            try:
                await self.websocket_handler.cleanup()
            except Exception as e:
                logger.warning(f"⚠️ Error cleaning up WebSocket handler: {e}")

        if self.audio_recording_service:
            self.audio_recording_service.cleanup()

        logger.info("✅ Application cleanup complete")


async def main() -> None:
    """Main entry point."""
    app = Application()

    try:
        await app.run()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
