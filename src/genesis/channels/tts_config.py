"""TTS configuration — YAML-based with mtime caching for hot-reload.

Config file is re-read only when its mtime changes, so edits take effect
on the next synthesis call without restarting the bridge.

Resolution order (3-tier): caller kwargs → config file → env vars → dataclass defaults.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "tts.yaml"


# ── Dataclasses ──────────────────────────────────────────────────────────


@dataclass
class ElevenLabsSettings:
    voice_id: str = ""
    model: str = "eleven_flash_v2_5"
    stability: float = 0.85
    similarity_boost: float = 0.7
    style: float = 0.3
    speed: float = 1.1
    use_speaker_boost: bool = True


@dataclass
class FishAudioSettings:
    voice_id: str = ""


@dataclass
class CartesiaSettings:
    voice_id: str = ""


@dataclass
class SanitizationSettings:
    strip_markdown: bool = True
    max_chars: int = 2000


@dataclass
class TTSConfig:
    provider: str = "elevenlabs"
    elevenlabs: ElevenLabsSettings = field(default_factory=ElevenLabsSettings)
    fish_audio: FishAudioSettings = field(default_factory=FishAudioSettings)
    cartesia: CartesiaSettings = field(default_factory=CartesiaSettings)
    sanitization: SanitizationSettings = field(default_factory=SanitizationSettings)
    voice_gate_background: bool = True


# ── Config loader with mtime cache ──────────────────────────────────────


class TTSConfigLoader:
    """Loads TTS config from YAML, re-reading only when the file changes."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _DEFAULT_CONFIG_PATH
        self._cached: TTSConfig | None = None
        self._cached_mtime: float = 0.0

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> TTSConfig:
        """Return current config, re-parsing from disk only if mtime changed."""
        if not self._path.exists():
            if self._cached is None:
                self._cached = _config_from_env()
                logger.info("TTS config file not found at %s, using env/defaults", self._path)
            return self._cached

        try:
            from genesis._config_overlay import local_overlay_mtime

            mtime = self._path.stat().st_mtime + local_overlay_mtime(self._path)
        except OSError:
            if self._cached is None:
                self._cached = _config_from_env()
            return self._cached

        if mtime != self._cached_mtime or self._cached is None:
            self._cached = _load_from_yaml(self._path)
            self._cached_mtime = mtime
            logger.debug("TTS config reloaded from %s (mtime=%.2f)", self._path, mtime)

        return self._cached

    def invalidate(self) -> None:
        """Force re-read on next load()."""
        self._cached = None
        self._cached_mtime = 0.0


# ── Parsing helpers ─────────────────────────────────────────────────────


def _load_from_yaml(path: Path) -> TTSConfig:
    """Parse YAML into TTSConfig, falling back to env vars for empty fields."""
    from genesis._config_overlay import merge_local_overlay

    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    raw = merge_local_overlay(raw, path)

    el_raw = raw.get("elevenlabs", {}) or {}
    fish_raw = raw.get("fish_audio", {}) or {}
    cart_raw = raw.get("cartesia", {}) or {}
    san_raw = raw.get("sanitization", {}) or {}
    vg_raw = raw.get("voice_gate", {}) or {}

    el = ElevenLabsSettings(
        voice_id=el_raw.get("voice_id") or os.environ.get("TTS_VOICE_ID_ELEVENLABS", ""),
        model=el_raw.get("model") or os.environ.get("TTS_ELEVENLABS_MODEL", "eleven_flash_v2_5"),
        stability=float(el_raw["stability"]) if "stability" in el_raw else _env_float("TTS_ELEVENLABS_STABILITY", 0.85),
        similarity_boost=float(el_raw["similarity_boost"]) if "similarity_boost" in el_raw else _env_float("TTS_ELEVENLABS_SIMILARITY", 0.7),
        style=float(el_raw["style"]) if "style" in el_raw else _env_float("TTS_ELEVENLABS_STYLE", 0.3),
        speed=float(el_raw["speed"]) if "speed" in el_raw else _env_float("TTS_ELEVENLABS_SPEED", 1.1),
        use_speaker_boost=el_raw.get("use_speaker_boost", True),
    )

    fish = FishAudioSettings(
        voice_id=fish_raw.get("voice_id") or os.environ.get("TTS_VOICE_ID_FISH", ""),
    )

    cart = CartesiaSettings(
        voice_id=cart_raw.get("voice_id") or os.environ.get("TTS_VOICE_ID_CARTESIA", ""),
    )

    san = SanitizationSettings(
        strip_markdown=san_raw.get("strip_markdown", True),
        max_chars=int(san_raw.get("max_chars", 2000)),
    )

    cfg = TTSConfig(
        provider=raw.get("provider", "elevenlabs"),
        elevenlabs=el,
        fish_audio=fish,
        cartesia=cart,
        sanitization=san,
        voice_gate_background=vg_raw.get("block_background_sessions", True),
    )
    # Warn about empty voice ID for the active provider
    active_voice_id = {
        "elevenlabs": el.voice_id, "fish_audio": fish.voice_id, "cartesia": cart.voice_id,
    }.get(cfg.provider, "")
    if not active_voice_id:
        logger.warning("TTS provider %s has no voice_id configured", cfg.provider)
    return cfg


def _config_from_env() -> TTSConfig:
    """Build config purely from env vars (backward compat when no YAML file)."""
    return TTSConfig(
        elevenlabs=ElevenLabsSettings(
            voice_id=os.environ.get("TTS_VOICE_ID_ELEVENLABS", ""),
            model=os.environ.get("TTS_ELEVENLABS_MODEL", "eleven_flash_v2_5"),
            stability=_env_float("TTS_ELEVENLABS_STABILITY", 0.85),
            similarity_boost=_env_float("TTS_ELEVENLABS_SIMILARITY", 0.7),
            style=_env_float("TTS_ELEVENLABS_STYLE", 0.3),
            speed=_env_float("TTS_ELEVENLABS_SPEED", 1.1),
        ),
        fish_audio=FishAudioSettings(
            voice_id=os.environ.get("TTS_VOICE_ID_FISH", ""),
        ),
        cartesia=CartesiaSettings(
            voice_id=os.environ.get("TTS_VOICE_ID_CARTESIA", ""),
        ),
    )


def _env_float(var: str, default: float) -> float:
    raw = os.environ.get(var)
    if raw is not None:
        try:
            return float(raw)
        except ValueError:
            pass
    return default


# ── Sanitization ────────────────────────────────────────────────────────

# Markdown patterns to strip before sending text to TTS
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_ITALIC = re.compile(r"\*(.+?)\*")
_MD_CODE_BLOCK = re.compile(r"```[\s\S]*?```")
_MD_INLINE_CODE = re.compile(r"`(.+?)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BLOCKQUOTE = re.compile(r"^>\s+", re.MULTILINE)
_MD_HR = re.compile(r"^[-*_]{3,}\s*$", re.MULTILINE)
_MD_LIST_MARKER = re.compile(r"^[\s]*[-*+]\s+", re.MULTILINE)
_MD_NUMBERED_LIST = re.compile(r"^[\s]*\d+\.\s+", re.MULTILINE)
# Command injection chars (from Miessler's sanitization pattern)
_INJECTION_CHARS = re.compile(r"[<>{}\[\]|\\;$]")


def sanitize_for_speech(text: str, settings: SanitizationSettings | None = None) -> str:
    """Strip markdown and dangerous chars, truncate for TTS consumption."""
    if settings is None:
        settings = SanitizationSettings()

    if not settings.strip_markdown:
        # Still truncate even if markdown stripping is off
        return text[: settings.max_chars] if settings.max_chars else text

    # Remove code blocks entirely (not useful as speech)
    result = _MD_CODE_BLOCK.sub("", text)
    # Strip markdown formatting
    result = _MD_BOLD.sub(r"\1", result)
    result = _MD_ITALIC.sub(r"\1", result)
    result = _MD_INLINE_CODE.sub(r"\1", result)
    result = _MD_LINK.sub(r"\1", result)
    result = _MD_HEADING.sub("", result)
    result = _MD_BLOCKQUOTE.sub("", result)
    result = _MD_HR.sub("", result)
    result = _MD_LIST_MARKER.sub("", result)
    result = _MD_NUMBERED_LIST.sub("", result)
    # Strip injection chars
    result = _INJECTION_CHARS.sub("", result)
    # Collapse whitespace
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()
    # Truncate
    if settings.max_chars and len(result) > settings.max_chars:
        result = result[: settings.max_chars]

    return result
