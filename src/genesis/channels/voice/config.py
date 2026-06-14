"""Voice pipeline configuration.

Reads from environment variables with sensible defaults.
All voice subsystems import their config from here.
"""

from __future__ import annotations

import os


def s2s_provider() -> str:
    """Which S2S provider to use: 'openai' or 'gemini' or 'none'."""
    return os.environ.get("VOICE_S2S_PROVIDER", "openai")


def s2s_model() -> str:
    """Model name for the S2S provider."""
    provider = s2s_provider()
    if provider == "openai":
        return os.environ.get("VOICE_S2S_MODEL", "gpt-realtime-1.5")
    if provider == "gemini":
        return os.environ.get("VOICE_S2S_MODEL", "gemini-2.0-flash-live")
    return ""


def s2s_voice() -> str:
    """Voice name for the S2S model's spoken output (preset).

    Default "ash" matches the s2s_bridge add-on default so both the active
    (add-on) and fallback (in-server Wyoming) paths use the same voice.
    """
    return os.environ.get("VOICE_S2S_VOICE", "ash")


def wyoming_stt_port() -> int:
    return int(os.environ.get("VOICE_WYOMING_STT_PORT", "10300"))


def wyoming_tts_port() -> int:
    return int(os.environ.get("VOICE_WYOMING_TTS_PORT", "10301"))


def s2s_enabled() -> bool:
    """Whether S2S is available (provider set + API key present)."""
    provider = s2s_provider()
    if provider == "openai":
        return bool(os.environ.get("OPENAI_API_KEY"))
    if provider == "gemini":
        return bool(os.environ.get("GOOGLE_API_KEY"))
    return False
