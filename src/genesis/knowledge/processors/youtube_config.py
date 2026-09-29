"""Config lever for YouTube fetches: certificate verification.

Cloned from ``observability.mcp_staleness_guard_config``: fresh read per call,
MODES tuple, ``.local.yaml`` overlay, degrade toward the SAFEST value on damage.

Modes (``tls``):
  - ``verify``        — always verify certificates.
  - ``auto_fallback`` — (default) verify; retry once without verification on a
    certificate-verification error only.
  - ``off``           — never verify.

A missing or corrupt config degrades to DEFAULTS; an invalid ``tls`` value
degrades to ``verify``, the safest outcome.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``TLS_MODES`` from here.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

TLS_MODES = ("verify", "auto_fallback", "off")

_CONFIG_NAME = "youtube_fetch.yaml"

# audio_max_minutes: the audio-transcription fallback (for a video with no
# captions) runs only for videos at most this long, read from the metadata
# before anything is downloaded. It bounds the download and the speech-to-text
# cost of an attacker-linked multi-hour video. Owner decision 2026-09-29.
DEFAULT_AUDIO_MAX_MINUTES = 120

DEFAULTS: dict[str, Any] = {"tls": "auto_fallback", "audio_max_minutes": DEFAULT_AUDIO_MAX_MINUTES}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def _overlay_damaged(base_path: Path) -> bool:
    """True when a ``.local.yaml`` overlay exists but cannot be applied.

    ``merge_local_overlay`` ignores such an overlay and returns the base, which
    would silently drop an operator's ``tls: verify``. This check lets the
    lever fail toward verification instead (#2568 review).
    """
    # Imported here, not at module level: a module-level binding of the
    # user-config-dir resolver escapes the test suite's config isolation.
    from genesis import _config_overlay

    try:
        path = _config_overlay._resolve_overlay_path(base_path)
        if not path.exists():
            return False
        loaded = yaml.safe_load(path.read_text())
    except Exception:
        return True
    return loaded is not None and not isinstance(loaded, dict)


def load_config() -> dict[str, Any]:
    """The merged config, read fresh (defaults ← base yaml ← .local.yaml).

    ``_damaged`` is set when the base file is missing or unreadable, or an
    overlay exists but cannot be applied; ``tls_mode`` then answers ``verify``.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    damaged = False
    try:
        loaded = yaml.safe_load(base_path.read_text())
        if isinstance(loaded, dict):
            base = loaded
        else:
            damaged = True
    except Exception:
        damaged = True
        logger.warning("youtube_fetch base config missing or unreadable at %s", base_path)
    if _overlay_damaged(base_path):
        damaged = True
        logger.warning("youtube_fetch overlay for %s cannot be applied", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        damaged = True
        logger.warning("youtube_fetch overlay merge failed", exc_info=True)
    merged.update(base)
    merged["_damaged"] = damaged
    return merged


def audio_max_minutes() -> int:
    """The audio-fallback duration cap in minutes, read live.

    A damaged config or an invalid value answers 0 (no audio transcription):
    the operator's cap may be exactly what failed to load (#2568 class audit).
    """
    cfg = load_config()
    if cfg.get("_damaged"):
        return 0
    value = cfg.get("audio_max_minutes")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        logger.warning("youtube_fetch has invalid audio_max_minutes %r — audio transcription off", value)
        return 0
    return value


def tls_mode() -> str:
    """The certificate-verification mode, read live. Invalid → ``verify``."""
    cfg = load_config()
    if cfg.get("_damaged"):
        # Never fall back to the less-safe product default on a damaged
        # config: the operator's `tls: verify` may be what failed to load.
        return "verify"
    mode = cfg.get("tls")
    if mode is False:
        # Hand-edited unquoted `tls: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in TLS_MODES:
        logger.warning("youtube_fetch has invalid tls mode %r — degrading to verify", mode)
        return "verify"
    return mode
