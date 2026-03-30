"""Speech-to-text via Groq Whisper API."""

import asyncio
import logging
import os
import tempfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


async def transcribe(audio_bytes: bytes, model_name: str = "whisper-large-v3") -> str:
    """Transcribe audio bytes to text using Groq's Whisper API.

    Accepts any format Groq supports (OGG/OPUS, WAV, MP3, FLAC, etc.).
    """
    api_key = os.environ.get("API_KEY_GROQ", "")
    if not api_key:
        log.error("API_KEY_GROQ not set — cannot transcribe")
        return ""

    # Write to temp file — Groq API needs a file upload
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(audio_bytes)
        tmp_path = f.name

    try:
        return await asyncio.to_thread(
            _transcribe_sync, tmp_path, api_key, model_name, len(audio_bytes),
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _transcribe_sync(
    file_path: str, api_key: str, model_name: str, audio_size: int,
) -> str:
    with open(file_path, "rb") as f:
        response = httpx.post(
            _GROQ_STT_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": ("audio.ogg", f, "audio/ogg")},
            data={"model": model_name},
            timeout=120.0,  # 120s for long voice files (6+ min)
        )

    if response.status_code == 429:
        log.error("Groq STT rate limited (429) — try again later")
        return ""
    if response.status_code == 401:
        log.error("Groq STT auth failed (401) — check API_KEY_GROQ")
        return ""
    if response.status_code != 200:
        log.error("Groq STT failed (%d): %s", response.status_code, response.text[:200])
        return ""

    try:
        text = response.json().get("text", "").strip()
    except (ValueError, KeyError):
        log.error("Groq STT returned malformed JSON: %s", response.text[:200])
        return ""
    log.info("Transcribed %d bytes → %d chars via Groq", audio_size, len(text))
    return text
