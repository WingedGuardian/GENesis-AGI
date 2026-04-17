"""Video processor — extracts audio track via ffmpeg, then transcribes."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".mp4", ".webm", ".mkv", ".avi", ".mov"}


class VideoProcessor:
    """Extract audio from video files and transcribe via STT."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg is not installed")

        from genesis.channels.stt import transcribe

        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Video file not found: {source}")

        # Extract audio to WAV via ffmpeg (pipe to stdout)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-i", str(path),
            "-vn",  # no video
            "-acodec", "pcm_s16le",  # WAV format
            "-ar", "16000",  # 16kHz sample rate (Whisper optimal)
            "-ac", "1",  # mono
            "-f", "wav",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        audio_bytes, stderr = await proc.communicate()

        if proc.returncode != 0 or not audio_bytes:
            raise RuntimeError(
                f"ffmpeg audio extraction failed for {source}: {stderr.decode()[:300]}"
            )

        text = await transcribe(audio_bytes)
        if not text:
            raise RuntimeError(f"Transcription returned empty result for {source}")

        return ProcessedContent(
            text=text,
            metadata={
                "filename": path.name,
                "extension": path.suffix,
                "size_bytes": path.stat().st_size,
            },
            source_type="video",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() in _SUPPORTED_EXTENSIONS
