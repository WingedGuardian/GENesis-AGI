"""Audio recording service."""
import logging
from datetime import datetime

from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame, StartFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.audio_recorder import AudioRecorder

logger = logging.getLogger(__name__)


class AudioFrameRecorder(FrameProcessor):
    """Processor that records specific audio frame types directly."""

    def __init__(self, frame_type, audio_recorder, record_func, **kwargs):
        """
        Initialize audio frame recorder.

        Args:
            frame_type: Type of frame to record (InputAudioRawFrame or OutputAudioRawFrame)
            audio_recorder: AudioRecorder instance
            record_func: Function to call for recording (record_input_audio or record_output_audio)
        """
        super().__init__(**kwargs)
        self.frame_type = frame_type
        self.audio_recorder = audio_recorder
        self.record_func = record_func

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        # Handle StartFrame first to initialize the processor state
        # This must be done before processing any other frames
        if isinstance(frame, StartFrame):
            # Call parent to mark processor as started
            await super().process_frame(frame, direction)
            # Push frame to next processor
            await self.push_frame(frame, direction)
            return

        # Always pass all frames through to the next processor first
        await self.push_frame(frame, direction)

        # Then record if this is the right audio frame type
        if isinstance(frame, self.frame_type) and self.audio_recorder:
            try:
                audio_bytes = frame.audio
                if audio_bytes and len(audio_bytes) > 0:
                    logger.debug(f"🎙️ Recording {len(audio_bytes)} bytes of {self.frame_type.__name__}")
                    self.record_func(audio_bytes)
            except Exception as e:
                logger.warning(f"⚠️ Error recording audio: {e}")


class AudioRecordingService:
    """Service for recording audio using Pipecat's AudioBufferProcessor."""

    def __init__(
        self,
        enable_recording: bool = False,
        sample_rate: int = 24000,
        chunk_duration_seconds: int = 30,
        output_dir: str = "recordings"
    ):
        """
        Initialize audio recording service.

        Args:
            enable_recording: Whether to enable audio recording
            sample_rate: Audio sample rate in Hz (default: 24000)
            chunk_duration_seconds: Duration of audio chunks in seconds (default: 30)
            output_dir: Directory to save recordings
        """
        self.enable_recording = enable_recording
        self.sample_rate = sample_rate
        self.chunk_duration_seconds = chunk_duration_seconds
        self.output_dir = output_dir

        self.audio_recorder: AudioRecorder | None = None
        self.input_recorder: AudioFrameRecorder | None = None
        self.output_recorder: AudioFrameRecorder | None = None

        if self.enable_recording:
            self._initialize_recording()

    def _initialize_recording(self):
        """Initialize audio recording components."""
        # Create audio recorder
        self.audio_recorder = AudioRecorder(output_dir=self.output_dir)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.audio_recorder.start_recording(client_id=f"session_{timestamp}")

        # Create audio frame recorders for input and output
        self.input_recorder = AudioFrameRecorder(
            InputAudioRawFrame,
            self.audio_recorder,
            self.audio_recorder.record_input_audio
        )

        self.output_recorder = AudioFrameRecorder(
            OutputAudioRawFrame,
            self.audio_recorder,
            self.audio_recorder.record_output_audio
        )

        logger.info("✅ AudioRecordingService initialized")

    def get_input_recorder(self) -> AudioFrameRecorder | None:
        """Get the input audio recorder for pipeline integration."""
        return self.input_recorder if self.enable_recording else None

    def get_output_recorder(self) -> AudioFrameRecorder | None:
        """Get the output audio recorder for pipeline integration."""
        return self.output_recorder if self.enable_recording else None

    def start_new_session(self, client_id: str | None = None):
        """Start a new recording session."""
        if not self.enable_recording:
            return

        # Stop current recording if active
        if self.audio_recorder:
            self.audio_recorder.stop_recording()

        # Create new recorder for this session
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = client_id or f"session_{timestamp}"
        self.audio_recorder = AudioRecorder(output_dir=self.output_dir)
        self.audio_recorder.start_recording(client_id=session_id)

        # Update recorders with new audio_recorder instance
        if self.input_recorder:
            self.input_recorder.audio_recorder = self.audio_recorder
            self.input_recorder.record_func = self.audio_recorder.record_input_audio

        if self.output_recorder:
            self.output_recorder.audio_recorder = self.audio_recorder
            self.output_recorder.record_func = self.audio_recorder.record_output_audio

        logger.info(f"🎙️ Started new recording session: {session_id}")

    def stop_recording(self):
        """Stop current recording session."""
        if self.audio_recorder:
            self.audio_recorder.stop_recording()
            logger.info("🎙️ Stopped recording session")

    def cleanup(self):
        """Cleanup resources."""
        if self.audio_recorder:
            self.audio_recorder.stop_recording()
            self.audio_recorder = None

