"""Audio recording utility for debugging."""
import logging
import os
import struct
from datetime import datetime

logger = logging.getLogger(__name__)


class AudioRecorder:
    """Records audio to WAV files for debugging."""

    def __init__(self, output_dir: str = "recordings"):
        """
        Initialize audio recorder.

        Args:
            output_dir: Directory to save recordings
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        # File handles for recording
        self._input_file: object | None = None  # Audio from ESP32 device
        self._output_file: object | None = None  # Audio from OpenAI
        self._input_bytes = 0
        self._output_bytes = 0

    def start_recording(self, client_id: str):
        """
        Start recording audio for a client session.

        Args:
            client_id: Unique identifier for this client session
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Input audio (from ESP32 device) - 24kHz, 16-bit, mono
        input_filename = os.path.join(
            self.output_dir,
            f"input_{client_id}_{timestamp}.wav"
        )
        self._input_file = open(input_filename, "wb")  # noqa: SIM115
        self._write_wav_header(self._input_file, sample_rate=24000, channels=1, bits_per_sample=16)
        self._input_bytes = 0

        # Output audio (from OpenAI) - 24kHz, 16-bit, mono
        output_filename = os.path.join(
            self.output_dir,
            f"output_{client_id}_{timestamp}.wav"
        )
        self._output_file = open(output_filename, "wb")  # noqa: SIM115
        self._write_wav_header(self._output_file, sample_rate=24000, channels=1, bits_per_sample=16)
        self._output_bytes = 0

        logger.info(f"🎙️ Started recording: input={input_filename}, output={output_filename}")

    def record_input_audio(self, audio_bytes: bytes):
        """
        Record audio received from ESP32 device.

        Args:
            audio_bytes: PCM audio bytes (16-bit, 24kHz, mono)
        """
        if self._input_file and audio_bytes:
            # Validate audio format: 16-bit = 2 bytes per sample
            if len(audio_bytes) % 2 != 0:
                logger.warning(f"⚠️ Input audio has odd byte count: {len(audio_bytes)}, padding with zero")
                audio_bytes = audio_bytes + b'\x00'  # Pad with one zero byte
            self._input_file.write(audio_bytes)
            self._input_file.flush()  # Ensure data is written to disk
            self._input_bytes += len(audio_bytes)

    def record_output_audio(self, audio_bytes: bytes):
        """
        Record audio received from OpenAI.

        Args:
            audio_bytes: PCM audio bytes (16-bit, 24kHz, mono)
        """
        if self._output_file and audio_bytes:
            # Validate audio format: 16-bit = 2 bytes per sample
            if len(audio_bytes) % 2 != 0:
                logger.warning(f"⚠️ Output audio has odd byte count: {len(audio_bytes)}, padding with zero")
                audio_bytes = audio_bytes + b'\x00'  # Pad with one zero byte
            self._output_file.write(audio_bytes)
            self._output_file.flush()  # Ensure data is written to disk
            self._output_bytes += len(audio_bytes)

    def stop_recording(self):
        """Stop recording and finalize WAV files."""
        if self._input_file:
            # Flush any pending writes before updating header
            self._input_file.flush()
            # Update WAV header with actual data size
            # WAV format: RIFF header (12 bytes) + fmt chunk (24 bytes) + data header (8 bytes) = 44 bytes
            # File size field (position 4) = total_file_size - 8 = (44 + data_size) - 8 = 36 + data_size
            self._input_file.seek(4)
            self._input_file.write(struct.pack('<I', 36 + self._input_bytes))
            self._input_file.seek(40)
            self._input_file.write(struct.pack('<I', self._input_bytes))
            self._input_file.flush()  # Ensure header updates are written
            self._input_file.close()
            self._input_file = None
            logger.info(f"✅ Stopped input recording: {self._input_bytes} bytes")

        if self._output_file:
            # Flush any pending writes before updating header
            self._output_file.flush()
            # Update WAV header with actual data size
            # WAV format: RIFF header (12 bytes) + fmt chunk (24 bytes) + data header (8 bytes) = 44 bytes
            # File size field (position 4) = total_file_size - 8 = (44 + data_size) - 8 = 36 + data_size
            self._output_file.seek(4)
            self._output_file.write(struct.pack('<I', 36 + self._output_bytes))
            self._output_file.seek(40)
            self._output_file.write(struct.pack('<I', self._output_bytes))
            self._output_file.flush()  # Ensure header updates are written
            self._output_file.close()
            self._output_file = None
            logger.info(f"✅ Stopped output recording: {self._output_bytes} bytes")

    def _write_wav_header(self, file, sample_rate: int, channels: int, bits_per_sample: int):
        """
        Write WAV file header.

        Args:
            file: File handle
            sample_rate: Sample rate in Hz
            channels: Number of channels (1=mono, 2=stereo)
            bits_per_sample: Bits per sample (16 or 24)
        """
        byte_rate = sample_rate * channels * (bits_per_sample // 8)
        block_align = channels * (bits_per_sample // 8)

        # RIFF header
        file.write(b'RIFF')
        file.write(struct.pack('<I', 0))  # File size (will be updated later)
        file.write(b'WAVE')

        # fmt chunk
        file.write(b'fmt ')
        file.write(struct.pack('<I', 16))  # fmt chunk size
        file.write(struct.pack('<H', 1))  # Audio format (1 = PCM)
        file.write(struct.pack('<H', channels))
        file.write(struct.pack('<I', sample_rate))
        file.write(struct.pack('<I', byte_rate))
        file.write(struct.pack('<H', block_align))
        file.write(struct.pack('<H', bits_per_sample))

        # data chunk
        file.write(b'data')
        file.write(struct.pack('<I', 0))  # Data size (will be updated later)

