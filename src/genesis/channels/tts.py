"""Text-to-speech synthesis — channel-agnostic HTTP calls to TTS providers.

Each synthesis function accepts an optional TTSConfig for hot-reloadable
settings.  When no config is provided, env vars are used (backward compat).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from genesis.channels.tts_config import TTSConfig

log = logging.getLogger(__name__)

# ── Fish Audio ───────────────────────────────────────────────────────────

_FISH_TTS_URL = "https://api.fish.audio/v1/tts"


async def synthesize_fish(
    text: str,
    voice_id: str | None = None,
    api_key: str | None = None,
    *,
    config: TTSConfig | None = None,
) -> bytes:
    """Synthesize speech via Fish Audio. Returns OGG/Opus bytes."""
    key = api_key or os.environ.get("API_KEY_FISH_AUDIO", "")
    if not key:
        raise RuntimeError("API_KEY_FISH_AUDIO not set")

    # 3-tier: caller kwarg → config → env var
    vid = voice_id or (config.fish_audio.voice_id if config else "") or os.environ.get("TTS_VOICE_ID_FISH", "")

    # Sanitize text
    text = _maybe_sanitize(text, config)

    payload: dict = {
        "text": text,
        "format": "opus",
        "latency": "balanced",
    }
    if vid:
        payload["reference_id"] = vid

    return await asyncio.to_thread(_fish_sync, key, payload)


def _fish_sync(api_key: str, payload: dict) -> bytes:
    resp = httpx.post(
        _FISH_TTS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Fish Audio TTS failed ({resp.status_code}): {resp.text[:200]}")
    log.info("Fish Audio TTS: %d bytes audio for %d chars", len(resp.content), len(payload["text"]))
    return resp.content


# ── Cartesia Sonic ───────────────────────────────────────────────────────

_CARTESIA_TTS_URL = "https://api.cartesia.ai/tts/bytes"
_CARTESIA_VERSION = "2025-04-16"
_CARTESIA_MODEL = "sonic-2"


async def synthesize_cartesia(
    text: str,
    voice_id: str | None = None,
    api_key: str | None = None,
    *,
    config: TTSConfig | None = None,
) -> bytes:
    """Synthesize speech via Cartesia Sonic. Returns WAV bytes."""
    key = api_key or os.environ.get("API_KEY_CARTESIA", "")
    if not key:
        raise RuntimeError("API_KEY_CARTESIA not set")

    vid = voice_id or (config.cartesia.voice_id if config else "") or os.environ.get("TTS_VOICE_ID_CARTESIA", "")
    if not vid:
        raise RuntimeError(
            "TTS_VOICE_ID_CARTESIA not set — Cartesia requires a voice ID"
        )

    text = _maybe_sanitize(text, config)

    payload = {
        "model_id": _CARTESIA_MODEL,
        "transcript": text,
        "voice": {"mode": "id", "id": vid},
        "output_format": {
            "container": "wav",
            "encoding": "pcm_s16le",
            "sample_rate": 24000,
        },
    }

    return await asyncio.to_thread(_cartesia_sync, key, payload)


def _cartesia_sync(api_key: str, payload: dict) -> bytes:
    resp = httpx.post(
        _CARTESIA_TTS_URL,
        headers={
            "X-API-Key": api_key,
            "Cartesia-Version": _CARTESIA_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Cartesia TTS failed ({resp.status_code}): {resp.text[:200]}")
    log.info("Cartesia TTS: %d bytes audio for %d chars", len(resp.content), len(payload["transcript"]))
    return resp.content


# ── ElevenLabs ───────────────────────────────────────────────────────────

_ELEVENLABS_TTS_BASE = "https://api.elevenlabs.io/v1/text-to-speech"
_ELEVENLABS_MODEL_DEFAULT = "eleven_flash_v2_5"
_ELEVENLABS_OUTPUT_FORMAT = "opus_48000_64"

# Env var names for explicit voice_settings overrides (backward compat).
_ELEVENLABS_SETTINGS_VARS = {
    "stability": "TTS_ELEVENLABS_STABILITY",
    "similarity_boost": "TTS_ELEVENLABS_SIMILARITY",
    "style": "TTS_ELEVENLABS_STYLE",
}
_ELEVENLABS_SPEED_VAR = "TTS_ELEVENLABS_SPEED"


def _elevenlabs_voice_settings_from_env() -> dict | None:
    """Build voice_settings from env vars, or None to use voice defaults.

    Returns None when no explicit overrides are set — this lets ElevenLabs
    use the voice's own stored settings (crucial for cloned voices).
    """
    overrides = {
        k: os.environ.get(v)
        for k, v in _ELEVENLABS_SETTINGS_VARS.items()
        if os.environ.get(v) is not None
    }
    if not overrides:
        return None
    settings: dict = {
        "stability": float(overrides["stability"]) if "stability" in overrides else 0.5,
        "similarity_boost": float(overrides["similarity_boost"]) if "similarity_boost" in overrides else 0.75,
        "style": float(overrides["style"]) if "style" in overrides else 0.0,
        "use_speaker_boost": True,
    }
    return settings


def _elevenlabs_voice_settings_from_config(config: TTSConfig) -> dict:
    """Build voice_settings from config file — always returns a dict."""
    el = config.elevenlabs
    return {
        "stability": el.stability,
        "similarity_boost": el.similarity_boost,
        "style": el.style,
        "use_speaker_boost": el.use_speaker_boost,
    }


def _elevenlabs_speed(config: TTSConfig | None = None) -> float:
    """Get speed multiplier (0.7–1.2 range supported by ElevenLabs)."""
    if config:
        return config.elevenlabs.speed
    raw = os.environ.get(_ELEVENLABS_SPEED_VAR)
    return float(raw) if raw is not None else 1.0


async def synthesize_elevenlabs(
    text: str,
    voice_id: str | None = None,
    api_key: str | None = None,
    *,
    config: TTSConfig | None = None,
) -> bytes:
    """Synthesize speech via ElevenLabs. Returns Opus audio bytes."""
    key = api_key or os.environ.get("API_KEY_ELEVENLABS", "")
    if not key:
        raise RuntimeError("API_KEY_ELEVENLABS not set")

    # 3-tier: caller kwarg → config → env var
    vid = voice_id or (config.elevenlabs.voice_id if config else "") or os.environ.get("TTS_VOICE_ID_ELEVENLABS", "")
    if not vid:
        raise RuntimeError(
            "TTS_VOICE_ID_ELEVENLABS not set — ElevenLabs requires a voice ID"
        )

    text = _maybe_sanitize(text, config)

    model = (config.elevenlabs.model if config else "") or os.environ.get("TTS_ELEVENLABS_MODEL", _ELEVENLABS_MODEL_DEFAULT)

    payload: dict = {
        "text": text,
        "model_id": model,
    }

    # Voice settings: config file provides explicit values; env-var path
    # returns None when no overrides (lets ElevenLabs use voice defaults).
    if config:
        payload["voice_settings"] = _elevenlabs_voice_settings_from_config(config)
    else:
        vs = _elevenlabs_voice_settings_from_env()
        if vs is not None:
            payload["voice_settings"] = vs

    return await asyncio.to_thread(_elevenlabs_sync, key, vid, payload, _elevenlabs_speed(config))


def _elevenlabs_sync(api_key: str, voice_id: str, payload: dict, speed: float) -> bytes:
    url = f"{_ELEVENLABS_TTS_BASE}/{voice_id}"
    params: dict = {"output_format": _ELEVENLABS_OUTPUT_FORMAT}
    if speed != 1.0:
        params["speed"] = speed
    resp = httpx.post(
        url,
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
        params=params,
        json=payload,
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs TTS failed ({resp.status_code}): {resp.text[:200]}")
    vs_desc = payload.get("voice_settings", "voice-defaults")
    log.info(
        "ElevenLabs TTS: %d bytes audio for %d chars (voice=%s, model=%s, settings=%s)",
        len(resp.content), len(payload["text"]), voice_id,
        payload.get("model_id", "?"), vs_desc,
    )
    return resp.content


# ── Sanitization helper ─────────────────────────────────────────────────


def _maybe_sanitize(text: str, config: TTSConfig | None) -> str:
    """Apply sanitization if config is present; passthrough otherwise."""
    if config is None:
        return text
    from genesis.channels.tts_config import sanitize_for_speech

    return sanitize_for_speech(text, config.sanitization)
