"""Telegram network configuration — retry logic, backoff, error classification.

Adapted from openclaw-python network_config.py (MIT license).
"""
from __future__ import annotations

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


def resolve_retry_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve retry configuration. Defaults if no config provided."""
    if config is None:
        config = {}
    retry_config = config.get("retry", {})
    return {
        "attempts": retry_config.get("attempts", 3),
        "min_delay_ms": retry_config.get("min_delay_ms", 1000),
        "max_delay_ms": retry_config.get("max_delay_ms", 30000),
        "jitter": retry_config.get("jitter", True),
    }


def calculate_retry_delay(
    attempt: int,
    min_delay_ms: int = 1000,
    max_delay_ms: int = 30000,
    jitter: bool = True,
) -> float:
    """Calculate retry delay with exponential backoff and optional jitter.

    Returns delay in seconds.
    """
    delay_ms = min(min_delay_ms * (2 ** attempt), max_delay_ms)
    if jitter:
        delay_ms = delay_ms * random.uniform(0.9, 1.1)
    return delay_ms / 1000
