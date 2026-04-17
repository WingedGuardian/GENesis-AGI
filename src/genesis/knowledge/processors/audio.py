"""Audio processor wrapping the existing Groq Whisper STT."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.knowledge.processors.base import ProcessedContent

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".webm"}


class AudioProcessor:
    """Transcribe audio files using Groq Whisper."""

    async def process(self, source: str, **kwargs: object) -> ProcessedContent:
        from genesis.channels.stt import transcribe

        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found: {source}")

        audio_bytes = path.read_bytes()
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
            source_type="audio",
            source_path=source,
        )

    def can_handle(self, source: str) -> bool:
        return Path(source).suffix.lower() in _SUPPORTED_EXTENSIONS
