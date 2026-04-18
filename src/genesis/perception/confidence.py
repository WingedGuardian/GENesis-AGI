"""Confidence gating — shared helper for observation, memory, and reflection gates.

Loads thresholds from config/confidence_gates.yaml (tunable at runtime via settings).
All gates support shadow mode: log what would be filtered without enforcing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "confidence_gates.yaml"

# Module-level cache — avoids re-reading YAML on every gate call.
# Invalidates on TTL expiry OR file mtime change (whichever comes first).
_cached_config: ConfidenceGatesConfig | None = None
_cache_ts: float = 0.0
_cached_mtime: float = 0.0
_CACHE_TTL = 60.0  # seconds


@dataclass(frozen=True)
class GateConfig:
    """Thresholds for a single gate."""

    min_confidence: float = 0.3
    shadow_mode: bool = True


@dataclass(frozen=True)
class DeepReflectionGateConfig:
    """Thresholds for deep reflection gating (includes separability)."""

    min_confidence: float = 0.3
    min_separability: float = 0.2
    shadow_mode: bool = True


@dataclass(frozen=True)
class ConfidenceGatesConfig:
    """All confidence gate configurations."""

    observation_write: GateConfig = field(default_factory=GateConfig)
    memory_upsertion: GateConfig = field(default_factory=GateConfig)
    deep_reflection: DeepReflectionGateConfig = field(default_factory=DeepReflectionGateConfig)


def load_config(path: Path | None = None) -> ConfidenceGatesConfig:
    """Load confidence gates config from YAML. Returns defaults if missing.

    Uses a 60-second module-level cache to avoid re-reading YAML on every gate
    call (multiple gates fire per awareness tick). Cache is bypassed when an
    explicit path is provided (e.g., in tests).
    """
    global _cached_config, _cache_ts, _cached_mtime  # noqa: PLW0603

    import time

    from genesis._config_overlay import local_overlay_mtime

    now = time.monotonic()
    config_path = path or _CONFIG_PATH

    # Invalidate cache if file changed (mtime check) even within TTL window
    if path is None and _cached_config is not None:
        try:
            cur_mtime = config_path.stat().st_mtime + local_overlay_mtime(config_path)
        except OSError:
            cur_mtime = 0.0
        if cur_mtime == _cached_mtime and (now - _cache_ts) < _CACHE_TTL:
            return _cached_config
    if not config_path.exists():
        result = ConfidenceGatesConfig()
    else:
        try:
            from genesis._config_overlay import merge_local_overlay

            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            raw = merge_local_overlay(raw, config_path)
            result = ConfidenceGatesConfig(
                observation_write=GateConfig(**raw.get("observation_write", {})),
                memory_upsertion=GateConfig(**raw.get("memory_upsertion", {})),
                deep_reflection=DeepReflectionGateConfig(**raw.get("deep_reflection", {})),
            )
        except Exception:
            logger.warning("Failed to load confidence_gates.yaml, using defaults", exc_info=True)
            result = ConfidenceGatesConfig()

    if path is None:
        _cached_config = result
        _cache_ts = now
        try:
            _cached_mtime = config_path.stat().st_mtime + local_overlay_mtime(config_path)
        except OSError:
            _cached_mtime = 0.0
    return result


def should_gate(
    confidence: float | None,
    gate: GateConfig | DeepReflectionGateConfig,
) -> tuple[bool, str]:
    """Check whether a value should be gated.

    Returns (should_filter, log_message).
    None confidence = caller didn't set → always passes (backward compat).
    """
    if confidence is None:
        return False, ""
    if confidence < gate.min_confidence:
        msg = f"confidence {confidence:.2f} < threshold {gate.min_confidence:.2f}"
        if gate.shadow_mode:
            return False, f"[shadow] would gate: {msg}"
        return True, msg
    return False, ""
