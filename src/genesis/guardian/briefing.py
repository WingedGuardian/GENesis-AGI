"""Guardian briefing — shared filesystem bridge between Genesis and Guardian.

Genesis writes curated briefings to the shared mount. Guardian reads them
before CC diagnosis, giving the investigator situational context it would
otherwise lack (incident history, service baselines, recent changes).

Phase 1: Static/semi-static briefing written on demand.
Phase 2: Awareness loop writes updated briefing every tick.

Two entry points:
- write_guardian_briefing() — called from Genesis (container side)
- read_guardian_briefing() — called from Guardian diagnosis (host side)

Both sides see the same file via Incus shared mount. Genesis writes to
~/.genesis/shared/briefing/guardian_briefing.md, which the host sees at
$STATE_DIR/shared/briefing/guardian_briefing.md.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "BriefingContent",
    "read_guardian_briefing",
    "write_guardian_briefing",
]

# Container-side default path (Genesis writes here)
_CONTAINER_BRIEFING_DIR = Path("~/.genesis/shared/briefing").expanduser()

# Structured briefing alongside the markdown
_BRIEFING_JSON_NAME = "guardian_briefing.json"
_BRIEFING_MD_NAME = "guardian_briefing.md"


@dataclass
class BriefingContent:
    """Structured briefing content for Guardian CC."""

    generated_at: str = ""
    genesis_version: str = ""
    service_baseline: dict[str, str] = field(default_factory=dict)
    recent_incidents: list[dict[str, str]] = field(default_factory=list)
    active_observations: list[str] = field(default_factory=list)
    metric_baselines: dict[str, str] = field(default_factory=dict)
    failure_modes_observed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def write_guardian_briefing(
    briefing_dir: Path | None = None,
    content: BriefingContent | None = None,
) -> Path:
    """Write a Guardian briefing to the shared filesystem.

    Called from the Genesis container. Writes both markdown (for CC prompt
    injection) and JSON (for structured consumption).

    Returns the path to the markdown briefing file.
    """
    out_dir = briefing_dir or _CONTAINER_BRIEFING_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    if content is None:
        content = _build_default_briefing()

    # Write structured JSON
    json_path = out_dir / _BRIEFING_JSON_NAME
    json_path.write_text(json.dumps(asdict(content), indent=2) + "\n")

    # Write human-readable markdown (this is what CC reads)
    md_path = out_dir / _BRIEFING_MD_NAME
    md_path.write_text(_render_briefing_markdown(content))

    logger.info("Guardian briefing written to %s", md_path)
    return md_path


def read_guardian_briefing(
    briefing_path: Path,
    max_age_s: int = 600,
) -> str | None:
    """Read Guardian briefing from the shared filesystem.

    Called from the Guardian host before CC diagnosis. Returns the markdown
    content if the file exists and is fresh enough. Returns None if:
    - File doesn't exist (mount not configured or Genesis hasn't written yet)
    - File is older than max_age_s (stale data is worse than no data)
    - File is empty or unreadable

    The caller should treat None as "no briefing available" and proceed
    without it — diagnosis works fine without context, just less informed.
    """
    if not briefing_path.exists():
        logger.debug("Briefing file not found at %s", briefing_path)
        return None

    try:
        stat = briefing_path.stat()
        age_s = time.time() - stat.st_mtime
        if age_s > max_age_s:
            logger.info(
                "Briefing file is %.0fs old (max %ds) — treating as stale",
                age_s, max_age_s,
            )
            return None

        text = briefing_path.read_text().strip()
        if not text:
            logger.debug("Briefing file is empty")
            return None

        logger.info("Loaded Guardian briefing (%.0fs old, %d chars)", age_s, len(text))
        return text

    except OSError as exc:
        logger.warning("Failed to read briefing file: %s", exc)
        return None


def _build_default_briefing() -> BriefingContent:
    """Build a default/initial briefing with static system knowledge.

    This is the Phase 1 briefing — static context that helps CC even
    without the dynamic awareness-loop integration (Phase 2).
    """
    now = datetime.now(UTC).isoformat()

    return BriefingContent(
        generated_at=now,
        genesis_version=_detect_genesis_version(),
        service_baseline={
            "genesis-bridge": "Main orchestration service. If dead, most subsystems are down.",
            "qdrant": "Vector DB at localhost:6333. Used by memory system.",
            "genesis-agent-zero": "Agent Zero web UI on port 5000. Health API lives here.",
        },
        metric_baselines={
            "memory_normal_pct": "40-65%",
            "tmp_normal_pct": "<30%",
            "disk_normal_pct": "<70%",
            "awareness_tick_interval": "5 minutes",
            "heartbeat_max_gap": "10 minutes",
        },
        failure_modes_observed=[],
        recent_incidents=[],
        active_observations=[],
        notes=[
            "Awareness loop ticks every 5 minutes — heartbeat canary is tied to it.",
            "Python venv at ~/genesis/.venv. Config at ~/genesis/config/.",
            "Database at ~/genesis/data/genesis.db (SQLite, 60+ tables).",
            "/tmp is 512MB tmpfs — fills up under heavy logging or temp file use.",
            "Cgroup memory limit is 24GiB. OOM kills target heaviest process.",
        ],
    )


def _detect_genesis_version() -> str:
    """Best-effort git version detection from within the container.

    NOTE: Must only be called from the container side (where ~/genesis is
    the Genesis repo). On the host, ~/genesis may not exist or may be the
    Guardian's code checkout, which would report the wrong commit hash.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=Path("~/genesis").expanduser(),
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return "unknown"


def _render_briefing_markdown(content: BriefingContent) -> str:
    """Render BriefingContent as markdown for CC prompt injection."""
    lines = [
        "## Genesis Briefing (from shared filesystem)",
        "",
        f"Generated: {content.generated_at}",
        f"Genesis version: {content.genesis_version}",
        "",
    ]

    if content.service_baseline:
        lines.append("### Service Baseline")
        lines.append("")
        for svc, desc in content.service_baseline.items():
            lines.append(f"- **{svc}**: {desc}")
        lines.append("")

    if content.metric_baselines:
        lines.append("### Normal Metric Ranges")
        lines.append("")
        for metric, value in content.metric_baselines.items():
            lines.append(f"- {metric}: {value}")
        lines.append("")

    if content.recent_incidents:
        lines.append("### Recent Incidents (last 7 days)")
        lines.append("")
        for incident in content.recent_incidents:
            cause = incident.get("cause", "unknown")
            resolution = incident.get("resolution", "unknown")
            when = incident.get("when", "unknown")
            lines.append(f"- [{when}] {cause} — {resolution}")
        lines.append("")

    if content.active_observations:
        lines.append("### Active Observations")
        lines.append("")
        for obs in content.active_observations:
            lines.append(f"- {obs}")
        lines.append("")

    if content.failure_modes_observed:
        lines.append("### Previously Observed Failure Modes")
        lines.append("")
        for mode in content.failure_modes_observed:
            lines.append(f"- {mode}")
        lines.append("")

    if content.notes:
        lines.append("### System Notes")
        lines.append("")
        for note in content.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)
