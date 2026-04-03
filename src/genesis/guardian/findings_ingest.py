"""Ingest Guardian diagnosis results from shared mount into Genesis DB.

When Genesis recovers after an outage, Guardian may have written one or more
diagnosis result files to the shared Incus mount. This module reads those
files, creates observations and events in the Genesis database, and renames
processed files to prevent re-ingestion.

Container side: reads from ~/.genesis/shared/findings/
Host side: Guardian writes to {state_dir}/shared/findings/

Called every awareness loop tick. Fast no-op when no files to process.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["ingest_guardian_findings"]

_CONTAINER_FINDINGS_DIR = Path("~/.genesis/shared/findings").expanduser()

# Required fields in the diagnosis JSON
_REQUIRED_FIELDS = {"likely_cause", "confidence_pct", "outcome"}

# Minimum confidence to ingest (low-confidence escalations are noise)
_MIN_CONFIDENCE = 50

# Outcome → event severity mapping
_SEVERITY_MAP = {
    "resolved": "info",
    "partially_resolved": "warning",
    "escalate": "error",
}


def _map_priority(confidence_pct: int) -> str:
    """Map confidence percentage to observation priority."""
    if confidence_pct >= 80:
        return "high"
    if confidence_pct >= 60:
        return "medium"
    return "low"


async def ingest_guardian_findings(
    db,
    *,
    findings_dir: Path | None = None,
) -> int:
    """Read Guardian diagnosis files and create observations + events.

    Returns the number of findings ingested. Designed to be fast on the
    common case (no files to process).
    """
    from genesis.db.crud import events, observations

    fdir = findings_dir or _CONTAINER_FINDINGS_DIR

    if not fdir.exists():
        return 0

    # Only unprocessed files (not .ingested or .corrupt)
    files = sorted(fdir.glob("guardian_diagnosis_*.json"))
    if not files:
        return 0

    ingested = 0
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Corrupt Guardian diagnosis file %s: %s", path.name, exc)
            _safe_rename(path, path.with_suffix(".json.corrupt"))
            continue

        # Validate required fields
        if not _REQUIRED_FIELDS.issubset(data.keys()):
            missing = _REQUIRED_FIELDS - data.keys()
            logger.warning(
                "Guardian diagnosis file %s missing fields %s — skipping",
                path.name, missing,
            )
            _safe_rename(path, path.with_suffix(".json.corrupt"))
            continue

        confidence = max(0, min(100, int(data.get("confidence_pct", 0))))
        if confidence < _MIN_CONFIDENCE:
            logger.debug(
                "Skipping low-confidence diagnosis %s (%d%%)",
                path.name, confidence,
            )
            _safe_rename(path, path.with_suffix(".json.skipped"))
            continue

        outcome = data.get("outcome", "escalate")
        likely_cause = data.get("likely_cause", "unknown")
        now = datetime.now(UTC).isoformat()

        # Truncate content for observation (keep full data in JSON string)
        content_str = json.dumps(data, default=str)
        if len(content_str) > 2000:
            content_str = content_str[:1997] + "..."

        # Create observation
        obs_id = f"guardian-diagnosis-{uuid.uuid4().hex[:12]}"
        try:
            await observations.create(
                db,
                id=obs_id,
                source="guardian",
                type="guardian_diagnosis",
                content=content_str,
                priority=_map_priority(confidence),
                created_at=data.get("diagnosed_at", now),
                category="system_health",
                skip_if_duplicate=True,
            )
        except Exception:
            logger.error(
                "Failed to create observation for %s", path.name, exc_info=True,
            )
            continue

        # Create event
        severity = _SEVERITY_MAP.get(outcome, "warning")
        try:
            await events.insert(
                db,
                subsystem="guardian",
                severity=severity,
                event_type="diagnosis.ingested",
                message=f"Guardian diagnosis: {likely_cause[:150]}",
                details={
                    "confidence_pct": confidence,
                    "outcome": outcome,
                    "recommended_action": data.get("recommended_action", "unknown"),
                    "source": data.get("source", "unknown"),
                    "outage_duration_s": data.get("outage_duration_s", 0),
                },
                timestamp=data.get("diagnosed_at", now),
            )
        except Exception:
            logger.error(
                "Failed to create event for %s", path.name, exc_info=True,
            )
            # Observation already created — still mark as ingested

        _safe_rename(path, path.with_suffix(".json.ingested"))
        ingested += 1

        logger.info(
            "Ingested Guardian diagnosis: %s (confidence=%d%%, outcome=%s)",
            likely_cause[:80], confidence, outcome,
        )

    return ingested


def _safe_rename(src: Path, dst: Path) -> None:
    """Rename a file, ignoring errors (best-effort)."""
    try:
        src.rename(dst)
    except OSError as exc:
        logger.warning("Failed to rename %s → %s: %s", src.name, dst.name, exc)
