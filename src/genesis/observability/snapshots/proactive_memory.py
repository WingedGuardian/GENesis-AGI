"""Proactive memory surfacing metrics snapshot."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_METRICS_PATH = Path.home() / ".genesis" / "proactive_metrics.json"
_SESSIONS_DIR = Path.home() / ".genesis" / "sessions"
_WINDOW_DAYS = 7


def _utcnow() -> datetime:
    """Time seam — tests patch this instead of the wall clock."""
    return datetime.now(UTC)


def _overlap_7d(sessions_dir: Path | None = None, now: datetime | None = None) -> dict:
    """Aggregate injection-overlap stats from per-session injection logs.

    The proactive hook (scripts/proactive_memory_hook.py) appends one JSONL
    record per prompt-with-injection to
    ``~/.genesis/sessions/{sid}/injection_log.jsonl``. This 7-day rollup is
    the data gate for the H-1 PR2 novelty-gated-injection decision: ship the
    gate only if ``overlap_pct_7d`` shows meaningful repeat injection.
    """
    if sessions_dir is None:
        sessions_dir = _SESSIONS_DIR
    if now is None:
        now = _utcnow()
    cutoff = now - timedelta(days=_WINDOW_DAYS)
    cutoff_epoch = cutoff.timestamp()

    total_injected = 0
    total_repeats = 0
    prompts_with_injection = 0
    prompts_with_repeat = 0
    sessions = 0
    try:
        for log_path in sessions_dir.glob("*/injection_log.jsonl"):
            try:
                if log_path.stat().st_mtime < cutoff_epoch:
                    continue  # Whole file predates the window
                counted_this_session = False
                # errors="replace": invalid bytes must cost only their own
                # line (json.loads fails, line skipped), never the whole
                # file or — via the outer catch — the remaining sessions.
                text = log_path.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        ts = datetime.fromisoformat(str(rec["ts"]))
                        injected = int(rec.get("injected", 0))
                        repeats = int(rec.get("repeats", 0))
                    except (ValueError, KeyError, TypeError):
                        continue  # Garbled line — skip, keep aggregating
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    if ts < cutoff or injected <= 0:
                        continue
                    total_injected += injected
                    total_repeats += repeats
                    prompts_with_injection += 1
                    if repeats > 0:
                        prompts_with_repeat += 1
                    counted_this_session = True
                if counted_this_session:
                    sessions += 1
            except OSError:
                continue  # Unreadable file — skip
    except Exception:
        logger.debug("overlap_7d aggregation failed", exc_info=True)

    overlap_pct = (
        round(100.0 * total_repeats / total_injected, 1) if total_injected else 0.0
    )
    repeat_rate = (
        round(prompts_with_repeat / prompts_with_injection, 3)
        if prompts_with_injection
        else 0.0
    )
    return {
        "overlap_pct_7d": overlap_pct,
        "prompts_with_injection_7d": prompts_with_injection,
        "repeat_prompt_rate_7d": repeat_rate,
        "sessions_7d": sessions,
    }


def proactive_memory_metrics() -> dict:
    """Load latest proactive surfacing detail from JSON file.

    Written by the UserPromptSubmit hook (scripts/proactive_memory_hook.py)
    on each invocation. Aggregated stats are in provider_activity under
    the 'proactive_memory' provider key (via activity_log table). The
    ``overlap_7d`` key carries the 7-day injection-overlap rollup.
    """
    data: dict = {}
    try:
        if _METRICS_PATH.exists():
            loaded = json.loads(_METRICS_PATH.read_text())
            if isinstance(loaded, dict):
                data = loaded
    except Exception:
        pass
    with contextlib.suppress(Exception):
        data["overlap_7d"] = _overlap_7d()
    return data
