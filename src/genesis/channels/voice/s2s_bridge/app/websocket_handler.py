"""WebSocket handler for managing WebSocket connections and pipelines."""
import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from pipecat.frames.frames import (
    EndFrame,
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService
from pipecat.transports.websocket.server import WebsocketServerParams, WebsocketServerTransport

from app.audio_recording_service import AudioRecordingService
from app.raw_audio_serializer import RawAudioSerializer
from app.session_manager import SessionManager

logger = logging.getLogger(__name__)


class SessionActivityTracker(FrameProcessor):
    """Processor that tracks session activity by monitoring audio frames."""

    def __init__(self, activity_callback, **kwargs):
        super().__init__(**kwargs)
        self.activity_callback = activity_callback

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        if isinstance(frame, StartFrame):
            logger.debug("🎬 SessionActivityTracker: Received StartFrame")
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return
        elif isinstance(frame, EndFrame):
            logger.debug("🏁 SessionActivityTracker: Received EndFrame")
            await self.push_frame(frame, direction)
            return

        # Track activity on any audio frame
        if isinstance(frame, (InputAudioRawFrame, OutputAudioRawFrame)):
            if self.activity_callback:
                self.activity_callback()
            logger.debug(f"🎵 SessionActivityTracker: Processing {type(frame).__name__} ({len(frame.audio)} bytes)")

        # Pass frame through to next processor
        await self.push_frame(frame, direction)


class WebSocketHandler:
    """Handles WebSocket transport initialization, pipeline building, and event management."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        session_manager: SessionManager | None = None,
        audio_recording_service: AudioRecordingService | None = None,
    ):
        """
        Initialize WebSocket handler.

        Args:
            host: Host address to bind to
            port: Port to listen on
            session_manager: Session manager instance
            audio_recording_service: Audio recording service instance
        """
        self.host = host
        self.port = port
        self.session_manager = session_manager
        self.audio_recording_service = audio_recording_service

        self.transport: WebsocketServerTransport | None = None
        self.pipeline: Pipeline | None = None
        self.runner: PipelineRunner | None = None
        self.current_task: PipelineTask | None = None
        # Tracks the most recently connected client websocket. Used to detect
        # stale-socket disconnects when the ESP32 opens a replacement
        # connection (pipecat allows one client; new connections evict old).
        self._active_websocket = None

    def create_transport(self) -> WebsocketServerTransport:
        """
        Create and initialize WebSocket transport.

        Returns:
            WebsocketServerTransport instance
        """
        logger.info("Initializing WebSocket transport...")

        # Use RawAudioSerializer for binary PCM audio
        serializer = RawAudioSerializer()

        # Create WebsocketServerTransport with WebsocketServerParams
        # The transport will start its own server automatically
        self.transport = WebsocketServerTransport(
            host=self.host,
            port=self.port,
            params=WebsocketServerParams(
                serializer=serializer,
                # CRITICAL: these default to False on TransportParams (inherited,
                # not deprecated). When False, the input audio task is never
                # created and all audio frames are silently dropped
                # (base_input.py) and output audio is discarded (base_output.py).
                audio_in_enabled=True,
                audio_out_enabled=True,
            )
        )

        logger.info(f"✅ WebSocket transport created - will listen on ws://{self.host}:{self.port}/")
        return self.transport

    def build_pipeline(
        self,
        transport: WebsocketServerTransport,
        openai_service: OpenAIRealtimeLLMService,
        client_id: str,
        activity_callback: Callable[[], None] | None = None
    ) -> tuple[Pipeline, PipelineRunner, PipelineTask]:
        """
        Build pipeline for a WebSocket transport connection.

        Args:
            transport: The WebSocket transport instance
            openai_service: The OpenAI service instance
            client_id: Unique identifier for the client device
            activity_callback: Optional callback for session activity tracking

        Returns:
            Tuple of (Pipeline, PipelineRunner, PipelineTask)
        """
        logger.info(f"🔗 Building pipeline for client: {client_id}")

        if openai_service is None:
            raise RuntimeError("OpenAI service must be created before building pipeline")

        logger.info(f"🔗 Building pipeline with WebSocket transport and OpenAI service: {type(openai_service).__name__}")

        # Create activity trackers
        input_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )
        output_activity_tracker = SessionActivityTracker(
            activity_callback=activity_callback or (lambda: None)
        )

        # Create context aggregator with cached context if available
        context_aggregator = None
        context_initializer = None
        if self.session_manager:
            context_aggregator = self.session_manager.create_context_aggregator(client_id)
            context_initializer = self.session_manager.create_context_initializer(client_id, context_aggregator)

        # Build pipeline components
        pipeline_components = [
            transport.input(),
            input_activity_tracker,
        ]

        # Add input audio recorder to capture ONLY InputAudioRawFrame
        input_recorder = self.audio_recording_service.get_input_recorder() if self.audio_recording_service else None
        if input_recorder:
            pipeline_components.append(input_recorder)

        # Continue with rest of pipeline
        if context_aggregator:
            pipeline_components.extend([
                context_aggregator.user(),
                openai_service,
                context_aggregator.assistant(),
            ])
        else:
            pipeline_components.append(openai_service)

        pipeline_components.append(output_activity_tracker)

        # Add output audio recorder to capture ONLY OutputAudioRawFrame
        output_recorder = self.audio_recording_service.get_output_recorder() if self.audio_recording_service else None
        if output_recorder:
            pipeline_components.append(output_recorder)

        pipeline_components.append(transport.output())

        # Add context initializer if we have cached messages
        if context_initializer:
            pipeline_components.append(context_initializer)

        pipeline = Pipeline(pipeline_components)
        logger.info("✅ Pipeline created for WebSocket connection")

        # Audio recording is handled by AudioFrameRecorder processors in the pipeline
        if self.audio_recording_service:
            logger.info("🎙️ Audio recording enabled - will record input and output audio")

        # Create pipeline runner and task
        # Disable idle timeout - server should always stay ready for connections
        runner = PipelineRunner()
        task = PipelineTask(pipeline, idle_timeout_secs=None, cancel_on_idle_timeout=False)

        # Start pipeline in background with exception tracking
        pipeline_task = asyncio.create_task(runner.run(task))
        pipeline_task.add_done_callback(self._on_pipeline_done)
        self._pipeline_task = pipeline_task
        logger.info("✅ Pipeline started for WebSocket connection")
        logger.info("✅ Pipeline initialized successfully")

        return pipeline, runner, task

    @staticmethod
    def _on_pipeline_done(task: asyncio.Task) -> None:
        """Log exceptions from background pipeline tasks."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error("Pipeline task failed: %s", exc, exc_info=exc)

    def extract_client_id(self, websocket) -> str:
        """
        Extract client ID from websocket connection.

        Args:
            websocket: WebSocket connection object

        Returns:
            Client ID string
        """
        client_ip = None
        if hasattr(websocket, 'client') and websocket.client:
            client_ip = websocket.client.host
        elif hasattr(websocket, 'remote_address'):
            client_ip = str(websocket.remote_address[0]) if websocket.remote_address else None

        if not client_ip:
            client_ip = f"unknown_{uuid.uuid4().hex[:8]}"
            logger.warning("⚠️ Could not extract client IP, using generated ID")

        return client_ip

    def setup_event_handlers(
        self,
        transport: WebsocketServerTransport,
        on_client_connected_callback: Callable[[str], Awaitable[None]],
        on_client_disconnected_callback: Callable[[str], Awaitable[None]] | None = None,
    ):
        """
        Setup WebSocket event handlers.

        Args:
            transport: The WebSocket transport instance
            on_client_connected_callback: Async callback function(client_id) called when client connects
            on_client_disconnected_callback: Optional callback function(client_id) called when client disconnects
        """
        @transport.event_handler("on_client_connected")
        async def on_client_connected(transport: WebsocketServerTransport, websocket):
            """Handle new WebSocket client connection."""
            is_replacement = (
                self._active_websocket is not None
                and self._active_websocket is not websocket
            )
            self._active_websocket = websocket
            client_id = self.extract_client_id(websocket)
            logger.info(f"🔗 New WebSocket connection from IP: {client_id}")
            if is_replacement:
                # The previous socket's disconnect event will fire shortly —
                # the stale-socket guard below ignores it. Reuse the live
                # OpenAI session instead of resetting it mid-conversation.
                logger.info("♻️ Connection replaced an existing client — keeping session")
                return
            await on_client_connected_callback(client_id)

        if on_client_disconnected_callback:
            @transport.event_handler("on_client_disconnected")
            async def on_client_disconnected(transport: WebsocketServerTransport, websocket, *args, **kwargs):
                """Handle client disconnection."""
                if self._active_websocket is not None and self._active_websocket is not websocket:
                    # Stale socket: this client was replaced by a newer
                    # connection. Pipecat's transport already nulled the
                    # output client connection (set_client_connection(None))
                    # for the OLD socket's disconnect — restore it for the
                    # live socket, and do NOT tear down the OpenAI session.
                    logger.info("🛡️ Ignoring stale-socket disconnect (client was replaced)")
                    await transport.output().set_client_connection(self._active_websocket)
                    return
                self._active_websocket = None
                client_id = self.extract_client_id(websocket)
                if client_id:
                    logger.info(f"🔌 Client {client_id} disconnected")
                    await on_client_disconnected_callback(client_id)

    async def cleanup(self):
        """Cleanup WebSocket handler resources."""
        if self.runner:
            try:
                await self.runner.cancel()
            except Exception as e:
                logger.warning(f"⚠️ Error cancelling runner: {e}")

        if self.transport:
            try:
                if hasattr(self.transport, 'stop'):
                    await self.transport.stop()
            except Exception as e:
                logger.warning(f"⚠️ Error stopping transport: {e}")

