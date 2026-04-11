"""CRUD operations for cognitive_state table."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Canonical location — also duplicated in scripts/genesis_session_end.py (keep in sync).
_PATCHES_FILE = Path.home() / ".genesis" / "session_patches.json"
_MAX_PATCHES = 20

_BOOTSTRAP = "[No cognitive state yet. This is a fresh system. Assess signals without prior context.]"


def load_session_patches(patches_file: Path | None = None) -> list[dict]:
    """Load session patches from file. Returns empty list on any error."""
    path = patches_file or _PATCHES_FILE
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data[-_MAX_PATCHES:]
        return []
    except (json.JSONDecodeError, OSError):
        logger.debug("Failed to load session patches", exc_info=True)
        return []


def clear_session_patches(patches_file: Path | None = None) -> None:
    """Remove session patches file. Called when deep reflection refreshes active_context."""
    path = patches_file or _PATCHES_FILE
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("Failed to clear session patches", exc_info=True)


async def create(
    db: aiosqlite.Connection,
    *,
    id: str,
    content: str,
    section: str,
    generated_by: str,
    created_at: str,
    expires_at: str | None = None,
) -> str:
    await db.execute(
        """INSERT INTO cognitive_state
           (id, content, section, generated_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (id, content, section, generated_by, created_at, expires_at),
    )
    await db.commit()
    return id


async def get_by_id(db: aiosqlite.Connection, id: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE id = ?", (id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_by_section(db: aiosqlite.Connection, section: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE section = ? ORDER BY created_at DESC",
        (section,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_current(db: aiosqlite.Connection, section: str) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM cognitive_state WHERE section = ? ORDER BY created_at DESC LIMIT 1",
        (section,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def render(
    db: aiosqlite.Connection,
    *,
    activity_tier: str = "away",
    patches_file: Path | None = None,
) -> str:
    """Render cognitive state sections, gated by activity tier.

    Tiers control verbosity:
    - ``"active"``: health flags + session patches only (user has context)
    - ``"returning"``: flags + patches + pending_actions + focus (skip narrative)
    - ``"away"``: full render — everything (catch-up briefing)

    A **fresh-narrative guard** ensures that if deep reflection wrote a new
    ``active_context`` *after* the most recent session patch, it is shown
    regardless of tier (the user hasn't seen it yet).

    State flags are **computed from real system metrics** so they auto-clear
    when conditions resolve.
    """
    # Fetch all data upfront — avoid redundant DB queries and file reads.
    ac_row = await get_current(db, "active_context")
    pending_row = await get_current(db, "pending_actions")
    computed_flags = await compute_state_flags(db)
    patches = load_session_patches(patches_file)

    # Fresh-narrative guard: if active_context is newer than the most recent
    # session patch, the user hasn't seen it — show it regardless of tier.
    # When there are no patches, tier will be "away" (no last_session_data
    # means _compute_activity_tier returns "away"), so this branch is safe.
    narrative_is_fresh = False
    if activity_tier != "away" and ac_row and patches:
        last_patch_time = patches[-1].get("ended_at", "")
        narrative_is_fresh = ac_row.get("created_at", "") > last_patch_time

    include_active_context = activity_tier == "away" or narrative_is_fresh
    include_pending = activity_tier in ("away", "returning")
    include_focus = activity_tier in ("away", "returning")

    sections: dict[str, dict] = {}
    if include_active_context and ac_row:
        sections["active_context"] = ac_row
    if include_pending and pending_row:
        sections["pending_actions"] = pending_row

    # Focus directive — shown for returning + away.
    focus_row = None
    if include_focus:
        focus_row = await get_current(db, "state_flags")

    if not sections and not computed_flags and not focus_row and not patches:
        return _BOOTSTRAP

    parts = []

    # --- Activity tier label ---
    tier_labels = {
        "active": "You have been active recently. Showing health flags + session patches only.",
        "returning": "Returning after a break. Showing actions + patches (skipping full narrative).",
    }
    label = tier_labels.get(activity_tier)
    if label:
        parts.append(f"*{label}*")

    # --- Reflection insights (timestamped — may be stale) ---
    try:
        from genesis.util.tz import fmt as _tz_fmt
    except (ImportError, KeyError):
        def _tz_fmt(s: str, _fmt: str = "") -> str:  # type: ignore[misc]
            return s  # fallback: return raw ISO string

    def _relative_age(iso_ts: str) -> str:
        """Return human-readable age like '2h ago' or '3d ago'."""
        try:
            from datetime import UTC, datetime

            ts_dt = datetime.fromisoformat(iso_ts)
            if ts_dt.tzinfo is None:
                ts_dt = ts_dt.replace(tzinfo=UTC)
            delta = datetime.now(UTC) - ts_dt
            total_seconds = int(delta.total_seconds())
            if total_seconds < 60:
                return "just now"
            if total_seconds < 3600:
                return f"{total_seconds // 60}m ago"
            if total_seconds < 86400:
                return f"{total_seconds // 3600}h ago"
            return f"{total_seconds // 86400}d ago"
        except Exception:
            return ""

    if "active_context" in sections:
        raw_ts = sections["active_context"].get("created_at", "unknown")
        ts = _tz_fmt(raw_ts)
        age = _relative_age(raw_ts)
        age_prefix = f"{age} — " if age else ""
        parts.append(
            f"## Active Context ({age_prefix}generated {ts})\n"
            + sections["active_context"]["content"]
        )
    if "pending_actions" in sections:
        raw_ts = sections["pending_actions"].get("created_at", "unknown")
        ts = _tz_fmt(raw_ts)
        age = _relative_age(raw_ts)
        age_prefix = f"{age} — " if age else ""
        parts.append(
            f"## Pending Actions ({age_prefix}generated {ts})\n"
            + sections["pending_actions"]["content"]
        )

    # --- Computed health flags (live, auto-clearing) ---
    flag_parts = []
    if computed_flags:
        flag_parts.append(
            "## Computed Health Flags (auto-clear when conditions resolve)\n"
            + computed_flags
        )
    if focus_row:
        raw_focus_ts = focus_row.get("created_at", "unknown")
        focus_ts = _tz_fmt(raw_focus_ts)
        focus_age = _relative_age(raw_focus_ts)
        focus_age_prefix = f"{focus_age} — " if focus_age else ""
        flag_parts.append(
            f"## Reflection Focus Directive ({focus_age_prefix}generated {focus_ts})\n"
            + focus_row["content"]
        )
    if flag_parts:
        parts.append("\n\n".join(flag_parts))

    # --- Session patches (accumulated since last deep reflection) ---
    if patches:
        patch_lines = ["## Session Activity Since Last Reflection\n"]
        patch_lines.append(
            "*These sessions ran since the last cognitive state refresh. "
            "Their work may supersede stale claims above.*\n"
        )
        for p in patches:
            sid = p.get("session_id", "???")[:8]
            ended = p.get("ended_at", "")
            topic = p.get("topic", "no topic recorded")
            msgs = p.get("message_count", 0)
            try:
                ts_str = _tz_fmt(ended, "%H:%M %Z")
            except (ValueError, TypeError):
                ts_str = ended[:16] if ended else "unknown"
            patch_lines.append(
                f"- **{ts_str}** (session {sid}, {msgs} msgs): {topic}"
            )
        parts.append("\n".join(patch_lines))

    return "\n\n".join(parts)


async def compute_state_flags(db: aiosqlite.Connection) -> str:
    """Compute state flags from real system metrics.

    Each flag queries the actual database state. When a condition resolves
    (e.g., retrieved_count > 0), its flag automatically stops appearing.
    """
    flags: list[str] = []

    try:
        # 1. Memory retrieval: broken if observations exist but none retrieved.
        cur = await db.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN retrieved_count > 0 THEN 1 ELSE 0 END) as retrieved "
            "FROM observations WHERE resolved = 0"
        )
        row = await cur.fetchone()
        if row:
            total, retrieved = row[0] or 0, row[1] or 0
            if total > 0 and retrieved == 0:
                flags.append(
                    "- [CRITICAL] MEMORY RETRIEVAL FAILURE: "
                    f"{total} unresolved observations, 0 retrieved. "
                    "Cognitive feedback loop is broken."
                )

        # (Removed 2026-04-11) Memory-backlog WARNING flag: the metric was
        # actionless from a starting session's perspective, and its message
        # ("consolidation may not be keeping pace") referred to a job that
        # does not exist. The real retrieval-coverage signal was also
        # removed from the awareness pipeline in the same sweep because it
        # was being misinterpreted downstream as reflection urgency.

        # 2. Job health: any scheduled job with consecutive failures.
        try:
            cur = await db.execute(
                "SELECT job_name, consecutive_failures, last_error "
                "FROM job_health WHERE consecutive_failures > 0"
            )
            failed_jobs = await cur.fetchall()
            for job_row in failed_jobs:
                name, fails, error = job_row[0], job_row[1], job_row[2] or "unknown"
                flags.append(
                    f"- [CRITICAL] JOB FAILURE: {name} — {fails} consecutive failures. "
                    f"Last error: {error[:80]}"
                )
        except Exception:
            # job_health table may not exist yet on fresh installs.
            logger.debug("job_health table not available for flag computation", exc_info=True)

        # 4. Outreach engagement: check recent message acknowledgment rate.
        try:
            cur = await db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN status IN ('acknowledged', 'actioned') THEN 1 ELSE 0 END) as acked "
                "FROM message_queue "
                "WHERE created_at > strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-7 days') AND target = 'telegram'"
            )
            row = await cur.fetchone()
            if row:
                total_msgs, acked = row[0] or 0, row[1] or 0
                if total_msgs >= 5:
                    rate = (acked / total_msgs) * 100
                    if rate < 15:
                        flags.append(
                            f"- [WARNING] OUTREACH ENGAGEMENT: {rate:.0f}% acknowledgment rate "
                            f"({acked}/{total_msgs} messages in last 7 days)."
                        )
        except Exception:
            # message_queue table may not exist yet on fresh installs.
            logger.debug("message_queue table not available for flag computation", exc_info=True)

    except Exception:
        logger.error("Failed to compute state flags", exc_info=True)

    return "\n".join(flags)


async def replace_section(
    db: aiosqlite.Connection,
    *,
    section: str,
    id: str,
    content: str,
    generated_by: str,
    created_at: str,
    expires_at: str | None = None,
) -> str:
    """Delete all rows for a section, then insert a new one."""
    await db.execute(
        "DELETE FROM cognitive_state WHERE section = ?", (section,),
    )
    await db.execute(
        """INSERT INTO cognitive_state
           (id, content, section, generated_by, created_at, expires_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (id, content, section, generated_by, created_at, expires_at),
    )
    await db.commit()
    return id


async def delete(db: aiosqlite.Connection, id: str) -> bool:
    cursor = await db.execute(
        "DELETE FROM cognitive_state WHERE id = ?", (id,),
    )
    await db.commit()
    return cursor.rowcount > 0
