"""Proactive memory surfacing metrics snapshot."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def proactive_memory_metrics() -> dict:
    """Load latest proactive surfacing detail from JSON file.

    Written by the UserPromptSubmit hook (scripts/proactive_memory_hook.py)
    on each invocation. Aggregated stats are in provider_activity under
    the 'proactive_memory' provider key (via activity_log table).
    """
    path = Path.home() / ".genesis" / "proactive_metrics.json"
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}
