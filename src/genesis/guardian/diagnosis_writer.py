"""Write diagnosis results to shared mount for Genesis to ingest.

After Guardian diagnoses and (optionally) recovers Genesis, this module
persists the DiagnosisResult as JSON on the shared Incus mount. Genesis
reads these files on recovery via findings_ingest.py, creating observations
and events so it can learn from its own failure patterns.

Host side: write_diagnosis_result() writes to {state_dir}/shared/findings/
Container side: Genesis reads from ~/.genesis/shared/findings/
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult

logger = logging.getLogger(__name__)

__all__ = ["write_diagnosis_result"]

_FINDINGS_SUBDIR = "findings"
_MAX_FINDINGS_FILES = 20


def write_diagnosis_result(
    result: DiagnosisResult,
    config: GuardianConfig,
    *,
    outage_duration_s: float = 0.0,
) -> Path | None:
    """Serialize a DiagnosisResult to JSON on the shared mount.

    Called from check.py immediately after diagnosis_engine.diagnose().
    Atomic write (.tmp → os.replace) prevents partial reads by Genesis.
    Auto-prunes old files to keep the directory bounded.

    Returns the path written, or None on failure.
    """
    findings_dir = config.findings_path
    try:
        findings_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create findings dir %s: %s", findings_dir, exc)
        return None

    # Single timestamp for both metadata and filename
    now = datetime.now(UTC)

    # Build JSON payload — explicit .value for StrEnum fields
    data = asdict(result)
    data["recommended_action"] = result.recommended_action.value
    data["diagnosed_at"] = now.isoformat()
    data["outage_duration_s"] = round(outage_duration_s, 1)
    # Clamp confidence to valid range (LLM output can be out of bounds)
    data["confidence_pct"] = max(0, min(100, data.get("confidence_pct", 0)))

    # Timestamp with microseconds for uniqueness
    ts = now.strftime("%Y%m%dT%H%M%S%f")
    filename = f"guardian_diagnosis_{ts}.json"
    out_path = findings_dir / filename

    try:
        content = json.dumps(data, indent=2, default=str) + "\n"

        # Atomic write: .tmp then os.replace
        tmp_path = findings_dir / f".{filename}.tmp"
        tmp_path.write_text(content)
        os.replace(tmp_path, out_path)

        logger.info(
            "Diagnosis result written to %s (cause=%s, confidence=%d%%, outcome=%s)",
            out_path, result.likely_cause[:80], result.confidence_pct, result.outcome,
        )
    except OSError as exc:
        logger.error("Failed to write diagnosis result: %s", exc, exc_info=True)
        return None

    # Prune old files (keep newest N)
    _prune_old_findings(findings_dir)

    return out_path


def _prune_old_findings(
    findings_dir: Path,
    max_files: int = _MAX_FINDINGS_FILES,
) -> int:
    """Remove oldest diagnosis files beyond the retention limit.

    Only targets guardian_diagnosis_*.json files — ignores .ingested,
    .corrupt, and other files.
    """
    try:
        files = sorted(
            findings_dir.glob("guardian_diagnosis_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return 0

    pruned = 0
    for old_file in files[max_files:]:
        try:
            old_file.unlink()
            pruned += 1
        except OSError:
            pass

    if pruned:
        logger.debug("Pruned %d old diagnosis files", pruned)
    return pruned
