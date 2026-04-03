"""Guardian briefing — BOTH SIDES. Shared filesystem bridge between Genesis and Guardian.

Genesis writes curated briefings to the shared mount. Guardian reads them
before CC diagnosis, giving the investigator situational context it would
otherwise lack (incident history, service baselines, recent changes).

Three entry points:
- write_guardian_briefing() — static briefing, called on demand (container side)
- write_dynamic_guardian_briefing(db) — live briefing from DB, called every tick
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
from datetime import timedelta as _timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "BriefingContent",
    "build_dynamic_briefing",
    "read_guardian_briefing",
    "write_dynamic_guardian_briefing",
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
    # Dynamic fields (populated by build_dynamic_briefing)
    last_tick_at: str = ""
    tick_count_1h: int = 0
    active_cc_sessions: list[dict[str, str]] = field(default_factory=list)
    recent_errors: list[dict[str, str]] = field(default_factory=list)


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


async def build_dynamic_briefing(db) -> BriefingContent:
    """Build a briefing with live data from the database.

    Each query is individually wrapped — partial data is better than no briefing.
    Falls back to static baselines from _build_default_briefing() for service
    descriptions, metric ranges, and system notes.
    """
    from genesis.db.crud import awareness_ticks, cc_sessions, events, observations

    static = _build_default_briefing()
    content = BriefingContent(
        generated_at=datetime.now(UTC).isoformat(),
        genesis_version=static.genesis_version,
        service_baseline=static.service_baseline,
        metric_baselines=static.metric_baselines,
        notes=static.notes,
    )

    # Active (unresolved) observations — most recent 15
    try:
        obs_rows = await observations.query(db, resolved=False, limit=15)
        for row in obs_rows:
            text = row.get("content", "")
            if len(text) > 200:
                text = text[:197] + "..."
            source = row.get("source", "")
            priority = row.get("priority", "")
            content.active_observations.append(
                f"[{priority}] ({source}) {text}"
            )
    except Exception:
        logger.warning("Dynamic briefing: observations query failed", exc_info=True)

    # Recent errors (last 24h)
    try:
        since = (datetime.now(UTC) - _timedelta(hours=24)).isoformat()
        err_rows = await events.query(db, severity="ERROR", since=since, limit=20)
        for row in err_rows:
            content.recent_errors.append({
                "subsystem": row.get("subsystem", "unknown"),
                "message": (row.get("message", "")[:150] or "no message"),
                "when": row.get("timestamp", "unknown"),
            })
    except Exception:
        logger.warning("Dynamic briefing: events query failed", exc_info=True)

    # Last tick info
    try:
        last = await awareness_ticks.last_tick(db)
        if last:
            content.last_tick_at = last.get("created_at", "")
    except Exception:
        logger.warning("Dynamic briefing: last_tick query failed", exc_info=True)

    # Tick count in last hour (all depths)
    try:
        content.tick_count_1h = await awareness_ticks.count_in_window_all(
            db, window_seconds=3600,
        )
    except Exception:
        logger.warning("Dynamic briefing: tick count query failed", exc_info=True)

    # Active CC sessions
    try:
        sessions = await cc_sessions.query_active(db)
        for s in sessions:
            content.active_cc_sessions.append({
                "type": s.get("session_type", "unknown"),
                "model": s.get("model", "unknown"),
                "started": s.get("started_at", "unknown"),
                "source": s.get("source_tag", "unknown"),
            })
    except Exception:
        logger.warning("Dynamic briefing: cc_sessions query failed", exc_info=True)

    return content


async def write_dynamic_guardian_briefing(db) -> None:
    """Build a dynamic briefing from the DB and write to the shared mount.

    This is the function wired into the awareness loop — called every tick.
    """
    content = await build_dynamic_briefing(db)
    write_guardian_briefing(content=content)


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

    if content.last_tick_at or content.tick_count_1h:
        lines.append("### Awareness Loop Status")
        lines.append("")
        if content.last_tick_at:
            lines.append(f"- Last tick: {content.last_tick_at}")
        if content.tick_count_1h:
            lines.append(f"- Ticks in last hour: {content.tick_count_1h}")
        lines.append("")

    if content.active_cc_sessions:
        lines.append("### Active CC Sessions")
        lines.append("")
        for sess in content.active_cc_sessions:
            stype = sess.get("type", "unknown")
            model = sess.get("model", "unknown")
            started = sess.get("started", "unknown")
            source = sess.get("source", "unknown")
            lines.append(f"- [{source}] {stype} ({model}) started {started}")
        lines.append("")

    if content.recent_errors:
        lines.append("### Recent Errors (last 24h)")
        lines.append("")
        for err in content.recent_errors:
            subsys = err.get("subsystem", "unknown")
            msg = err.get("message", "no message")
            when = err.get("when", "unknown")
            lines.append(f"- [{when}] **{subsys}**: {msg}")
        lines.append("")

    if content.notes:
        lines.append("### System Notes")
        lines.append("")
        for note in content.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines)
