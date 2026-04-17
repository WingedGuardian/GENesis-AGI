#!/usr/bin/env python3
"""SessionStart hook: inject Genesis context into CC sessions.

This script runs at CC session start (via .claude/settings.json SessionStart hook).
Its stdout becomes context visible to Claude in the session.

Each section is printed and flushed immediately so that if the hook times out
(e.g. DB query hangs), identity files (instant disk reads) are already captured.

Also writes session start timestamp to ~/.genesis/session_start for use by the
UserPromptSubmit urgent-alert hook (interactive sessions only).

For interactive (foreground) sessions: injects everything — identity files,
cognitive state, procedures, temporal context, capabilities.

For bridge-dispatched sessions (GENESIS_CC_SESSION=1): skips identity files
and cognitive state (already provided via --system-prompt), but still injects
procedures, temporal context, resume signals, and capabilities.

Skips ALL injection when:
- ~/.genesis/cc_context_enabled flag file is absent (eject lever)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Load secrets.env so USER_TIMEZONE and other env vars are available
# before any genesis module imports (which may read os.environ at import time).
_SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.env"
if _SECRETS_PATH.is_file():
    try:
        from dotenv import load_dotenv
        load_dotenv(str(_SECRETS_PATH), override=False)
    except ImportError:
        pass

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_SETUP_COMPLETE = Path.home() / ".genesis" / "setup-complete"
_IDENTITY_DIR = Path(__file__).resolve().parent.parent / "src" / "genesis" / "identity"
_IDENTITY_FILES = ["SOUL.md", "USER.md", "CONVERSATION.md", "STEERING.md"]
_SESSION_START_FILE = Path.home() / ".genesis" / "session_start"
_SESSION_CONFIG = Path.home() / ".genesis" / "session_config.json"
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "genesis" / "skills"


def _emit(text: str) -> None:
    """Print a section and flush immediately so it survives a timeout kill."""
    print(text)
    sys.stdout.flush()


def _sync_genesis_hooks() -> None:
    """Self-heal Genesis git hooks at session start.

    Invokes scripts/hooks/sync-hooks.sh --quiet to bring $GIT_COMMON_DIR/hooks
    into sync with scripts/hooks/*. This is how community users who `git pull`
    Genesis updates (without re-running bootstrap.sh) pick up new or updated
    hooks — the next CC session auto-installs them via this function.

    Fail-open: any error is swallowed silently. Hook sync must NEVER block
    session startup.

    Cost: ~50-200ms for the subprocess. Negligible in the 5000ms SessionStart
    budget. Runs once per session start.
    """
    import contextlib
    import subprocess

    sync_script = Path(__file__).resolve().parent / "hooks" / "sync-hooks.sh"
    if not sync_script.is_file():
        # sync-hooks.sh doesn't exist yet on very old Genesis installs —
        # silently skip. The install was pre-Phase-6.
        return
    # Fail-open: any error here must NEVER block session startup. CC discards
    # SessionStart stderr anyway, so silent skip is the right behavior.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, FileNotFoundError):
        subprocess.run(
            [str(sync_script), "--quiet"],
            check=False,  # exit 2 (user-modified) is fine, not a failure
            capture_output=True,
            timeout=3.0,
        )


def main() -> None:
    # Eject lever: flag file absent → no Genesis context
    if not _FLAG.exists():
        return

    # Phase 6: self-heal Genesis git hooks before doing anything else.
    # Runs on every session start so community installs auto-pick up hook
    # updates without requiring a bootstrap.sh re-run.
    _sync_genesis_hooks()

    # Bridge-dispatched sessions get identity via --system-prompt; skip those
    # sections but still inject procedures, temporal context, and capabilities.
    is_genesis_session = os.environ.get("GENESIS_CC_SESSION") == "1"

    first = True

    if not is_genesis_session:
        # Record session start time for the urgent-alert UserPromptSubmit hook
        # (bridge manages its own session tracking)
        _SESSION_START_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_START_FILE.write_text(datetime.now(UTC).isoformat())

        # 0.5. Session Configuration — inject last-known model/effort so
        # the LLM can display an accurate header. Updated by the
        # session_set_model / session_set_effort MCP tools.
        if _SESSION_CONFIG.exists():
            import json

            try:
                cfg = json.loads(_SESSION_CONFIG.read_text())
                model = cfg.get("model", "unknown")
                effort = cfg.get("effort", "unknown")
                _emit(f"## Session Configuration\n\nmodel: {model}\neffort: {effort}\n")
                first = False
            except Exception as exc:
                print(f"[session_context] Failed to read session config: {exc}", file=sys.stderr)

        # 1. Identity files (disk, always available, no external deps)
        for name in _IDENTITY_FILES:
            path = _IDENTITY_DIR / name
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(content)
                    first = False

    # 1.5. First-run onboarding detection
    # If setup-complete marker is absent and this is a foreground session,
    # inject the onboarding skill prompt so Genesis guides the user through setup.
    if not is_genesis_session and not _SETUP_COMPLETE.exists():
        onboarding_skill = _SKILLS_DIR / "onboarding" / "SKILL.md"
        if onboarding_skill.exists():
            if not first:
                _emit("\n\n---\n\n")
            _emit(
                "## FIRST-RUN ONBOARDING REQUIRED\n\n"
                "This is a fresh Genesis installation — `~/.genesis/setup-complete` "
                "does not exist. **Before doing anything else**, run the onboarding "
                "flow to configure the system.\n\n"
                "The onboarding skill is at: "
                "`src/genesis/skills/onboarding/SKILL.md`\n\n"
                "Read the skill file and follow its steps. Do not skip this — the "
                "user needs a working system before Genesis can be useful.\n\n"
                "If the user's first message is unrelated to setup, acknowledge it "
                "but explain that you need to complete onboarding first."
            )
            first = False

    # Load last session data once — used for cognitive state tier + temporal awareness
    last_session_data = _load_last_session_data()

    if is_genesis_session:
        # 2. Cognitive state from DB — for ego/background sessions only.
        # Foreground sessions get essential knowledge instead (see below).
        try:
            cog = asyncio.run(_load_cognitive_state(last_session_data))
            if cog:
                if not first:
                    _emit("\n\n---\n\n")
                _emit("## Current Cognitive State\n\n" + cog)
                first = False
        except Exception:
            if not first:
                _emit("\n\n---\n\n")
            _emit(
                "## GENESIS ALERT: Cognitive State Unavailable\n\n"
                "The database query for cognitive state failed. This may indicate "
                "a DB or system health issue.\n\n"
                "**Action:** Use the health_status MCP tool to investigate, or check "
                "`~/.genesis/status.json` for current resilience state."
            )
            first = False
    else:
        # 2. Essential knowledge for foreground sessions.
        # Replaces cognitive state — shows what Genesis knows, not system health.
        # Critical alerts only surface if genuinely user-blocking.
        _ek_file = Path.home() / ".genesis" / "essential_knowledge.md"
        if _ek_file.exists():
            try:
                ek_content = _ek_file.read_text(encoding="utf-8").strip()
                if ek_content:
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(ek_content)
                    first = False
            except OSError:
                pass  # Essential knowledge is advisory — silent failure is correct

        # Critical-only alert: surface genuinely user-blocking issues (DB down, etc.)
        _status_file = Path.home() / ".genesis" / "status.json"
        if _status_file.exists():
            try:
                import json as _json_status
                status = _json_status.loads(_status_file.read_text())
                resilience = status.get("resilience_state", "")
                if resilience in ("critical", "degraded_critical"):
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(
                        "## GENESIS ALERT: System Issue\n\n"
                        f"Resilience state: **{resilience}**. "
                        "Use `health_status` MCP tool for details."
                    )
                    first = False
            except (OSError, ValueError):
                pass  # Never block session start

    # 2.5. Active procedures (advisory, silent failure is correct)
    try:
        from genesis.learning.procedural.session_inject import load_active_procedures
        _db_path = Path.home() / "genesis" / "data" / "genesis.db"
        procedures = asyncio.run(load_active_procedures(_db_path))
        if procedures:
            if not first:
                _emit("\n\n---\n\n")
            _emit("## Active Procedures\n\n"
                  "Learned procedures — follow these before inventing new approaches.\n\n"
                  + procedures)
            first = False
    except Exception:
        pass  # Procedures are advisory; silent failure is correct

    # 2.6. Codebase L0 — package index from AST code index (advisory)
    if not is_genesis_session:
        try:
            import aiosqlite
            _db_path_l0 = Path.home() / "genesis" / "data" / "genesis.db"
            if _db_path_l0.exists():
                async def _load_l0():
                    async with aiosqlite.connect(str(_db_path_l0)) as db:
                        db.row_factory = aiosqlite.Row
                        cursor = await db.execute(
                            "SELECT package, COUNT(*) as modules, SUM(loc) as loc "
                            "FROM code_modules GROUP BY package ORDER BY loc DESC"
                        )
                        return await cursor.fetchall()
                rows = asyncio.run(_load_l0())
                if rows:
                    lines = ["## Codebase\n"]
                    # Show top 15 packages, summarize the rest
                    top = rows[:15]
                    rest = rows[15:]
                    for r in top:
                        lines.append(f"- **{r['package']}**: {r['modules']} modules, {r['loc']} LOC")
                    if rest:
                        rest_mods = sum(r['modules'] for r in rest)
                        rest_loc = sum(r['loc'] for r in rest)
                        lines.append(f"- *{len(rest)} more packages*: {rest_mods} modules, {rest_loc} LOC")
                    lines.append(
                        "\nUse `codebase_navigate` MCP tool for drill-down "
                        "(L1: modules in a package, L2: symbols in a module)."
                    )
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit("\n".join(lines))
                    first = False
        except Exception:
            pass  # Codebase index is advisory — silent failure is correct

    # 3. Previous session context (temporal awareness — uses pre-loaded data)
    try:
        prev_session = _format_previous_session(last_session_data)
        if prev_session:
            if not first:
                _emit("\n\n---\n\n")
            _emit(prev_session)
            first = False
    except Exception:
        pass  # Previous session context is advisory

    # 4. Resume signal (user signaled they want to return)
    try:
        resume_signal = _load_resume_signal()
        if resume_signal:
            if not first:
                _emit("\n\n---\n\n")
            _emit(resume_signal)
            first = False
    except Exception:
        pass  # Resume signal is advisory

    # 5. Capabilities + MCP tools (dynamic from registry, fallback to static)
    if not first:
        _emit("\n\n---\n\n")

    _cap_file = Path.home() / ".genesis" / "capabilities.json"
    _mcp_fallback = (
        "## Genesis MCP Tools Available\n\n"
        "You have genesis-health, genesis-memory, genesis-outreach, and genesis-recon MCP servers.\n"
        "Use memory tools (memory_recall, memory_store) for cross-session knowledge.\n"
        "Use health tools (health_status, health_errors, health_alerts) for system state.\n"
        "Use session tools (session_set_model, session_set_effort) to switch model/effort.\n"
        "Use outreach tools (outreach_queue, outreach_digest) to check proactive messages.\n"
        "Use recon tools for project watchlist and findings.\n"
        "Use bookmark tools (bookmark_shelve, bookmark_unshelve) to save and find sessions."
    )
    if _cap_file.exists():
        try:
            import json
            caps = json.loads(_cap_file.read_text())
            lines = ["## Genesis Capabilities\n"]
            for cname, cinfo in caps.items():
                cstatus = cinfo.get("status", "unknown")
                cdesc = cinfo.get("description", cname)
                if cstatus == "active":
                    lines.append(f"- **{cname}**: {cdesc}")
                else:
                    cerr = cinfo.get("error", "")
                    suffix = f" — Error: {cerr}" if cerr else ""
                    lines.append(f"- **{cname}** [{cstatus}]: {cdesc}{suffix}")
            lines.append(
                "\n**MCP Tools:** memory_recall/memory_store, "
                "health_status/health_errors/health_alerts, "
                "session_set_model/session_set_effort, "
                "outreach_queue/outreach_digest, recon tools, "
                "bookmark_shelve/bookmark_unshelve."
            )
            _emit("\n".join(lines))
        except Exception:
            _emit(_mcp_fallback)
    else:
        _emit(_mcp_fallback)

    # 6. MCP server crash warnings — loud alert when MCP servers failed to start
    _mcp_crash_dir = Path.home() / ".genesis" / "mcp_crashes"
    if _mcp_crash_dir.is_dir():
        try:
            import json as _json

            crash_entries = []
            for crash_file in sorted(_mcp_crash_dir.glob("*.json")):
                try:
                    info = _json.loads(crash_file.read_text())
                    crash_entries.append(info)
                except (ValueError, OSError):
                    crash_entries.append({"server": crash_file.stem, "error": "unreadable crash file"})
            if crash_entries:
                _emit("\n\n---\n\n")
                _emit("## GENESIS ALERT: MCP Server Crashes\n\n")
                _emit(
                    "The following MCP servers failed to start and their tools are "
                    "**UNAVAILABLE** in this session:\n\n"
                )
                for info in crash_entries:
                    srv = info.get("server", "unknown")
                    err = info.get("error", "unknown error")
                    ts = info.get("timestamp", "")
                    ts_note = f" (at {ts})" if ts else ""
                    _emit(f"- **genesis-{srv}**: `{err}`{ts_note}\n")
                _emit(
                    "\n**Impact:** Tools from crashed servers will not appear. "
                    "Fix the root cause and restart the session.\n"
                )
        except Exception:
            pass  # Crash reporting itself must not crash the hook


def _load_last_session_data() -> dict | None:
    """Load last foreground session JSON from disk (single read for all consumers)."""
    import json

    last_session_file = Path.home() / ".genesis" / "last_foreground_session.json"
    if not last_session_file.exists():
        return None
    try:
        return json.loads(last_session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _format_previous_session(data: dict | None) -> str | None:
    """Format previous session context for temporal awareness."""
    if not data:
        return None

    ended_at = data.get("ended_at", "")
    topic_hint = data.get("topic_hint", "")
    session_id = data.get("session_id", "")

    if not ended_at:
        return None

    try:
        from genesis.util.tz import fmt as _tz_fmt

        formatted = _tz_fmt(ended_at)
    except (ValueError, TypeError, ImportError):
        formatted = ended_at

    parts = [f"Previous session: {formatted}"]
    if session_id:
        parts.append(f"ID: {session_id[:8]}")
    if topic_hint:
        parts.append(f"Topic: {topic_hint}")

    return f"[{' | '.join(parts)}]"


def _compute_activity_tier(
    last_session_data: dict | None,
    foreground_count_24h: int = 0,
) -> str:
    """Compute activity tier from session recency and frequency.

    Returns "active", "returning", or "away".
    """
    if not last_session_data:
        return "away"

    ended_at = last_session_data.get("ended_at", "")
    if not ended_at:
        return "away"

    try:
        ended_dt = datetime.fromisoformat(ended_at)
        gap_hours = (datetime.now(UTC) - ended_dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return "away"

    if gap_hours < 2 or foreground_count_24h >= 3:
        return "active"
    elif gap_hours < 24:
        return "returning"
    else:
        return "away"


def _load_resume_signal() -> str | None:
    """Load resume signal if user signaled they want to return."""
    import json

    signal_file = Path.home() / ".genesis" / "last_resume_signal.json"
    if not signal_file.exists():
        return None

    try:
        data = json.loads(signal_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    signal = data.get("signal", "")
    session_id = data.get("session_id", "")

    if not signal:
        return None

    # Clear the signal file so it doesn't repeat
    import contextlib
    with contextlib.suppress(OSError):
        signal_file.unlink()

    msg = f"You signaled you wanted to return to a previous session (\"{signal}\")."
    if session_id:
        msg += f" Session ID: {session_id}"
    msg += " Use bookmark_unshelve to find it, or `claude --resume <id>` to resume directly."
    return msg


async def _foreground_session_count_24h(db) -> int:
    """Count foreground sessions in the last 24 hours."""
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM cc_sessions "
            "WHERE source_tag = 'foreground' "
            "AND started_at > strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-24 hours')"
        )
        row = await cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


async def _load_cognitive_state(last_session_data: dict | None = None) -> str | None:
    from genesis.db.connection import get_db
    from genesis.db.crud import cognitive_state

    db = await get_db()
    try:
        fg_count = await _foreground_session_count_24h(db)
        tier = _compute_activity_tier(last_session_data, fg_count)
        return await cognitive_state.render(db, activity_tier=tier)
    finally:
        await db.close()


if __name__ == "__main__":
    main()
