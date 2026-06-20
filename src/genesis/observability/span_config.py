"""Load the spans config (``config/observability.yaml``) — defensive defaults.

Single source for both the bootstrap (which passes ``enabled`` to
``spans.set_writer``) and the prune job (which reads ``retention_days``). Any
read failure falls back to safe defaults (enabled, 14-day retention) — a missing
or malformed config never disables tracing or breaks the prune.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_ENABLED = True
_DEFAULT_RETENTION_DAYS = 14


def _config_path() -> Path:
    # this file: src/genesis/observability/span_config.py → repo root = parents[3]
    return Path(__file__).resolve().parents[3] / "config" / "observability.yaml"


def load_spans_config() -> tuple[bool, int]:
    """Return (enabled, retention_days), defaulting safely on any error."""
    enabled, retention = _DEFAULT_ENABLED, _DEFAULT_RETENTION_DAYS
    try:
        import yaml

        path = _config_path()
        if path.exists():
            data = yaml.safe_load(path.read_text()) or {}
            spans = data.get("spans") or {}
            enabled = bool(spans.get("enabled", _DEFAULT_ENABLED))
            retention = int(spans.get("retention_days", _DEFAULT_RETENTION_DAYS))
            if retention < 1:
                retention = _DEFAULT_RETENTION_DAYS
    except Exception:
        logger.debug("spans config load failed — using defaults", exc_info=True)
    return enabled, retention
