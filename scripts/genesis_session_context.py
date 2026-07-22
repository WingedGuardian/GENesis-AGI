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


def _routed_session_notice(model: str | None) -> str | None:
    """Markdown NOTICE when this interactive session is routed to a roster peer.

    ``model`` is ``GENESIS_ROSTER_MODEL`` (set only by ``scripts/gmodel`` for a
    peer). CC's baked-in "You are powered by …" identity text still says Claude
    when the endpoint is a peer, so the self-reported ``[model]`` header would be
    wrong — this block surfaces the true model and steers the header. Returns
    ``None`` (no block) for a native/plain session.
    """
    if not model:
        return None
    return (
        f"## ⚠ Routed session — running on {model}\n\n"
        f"This CLI session is routed to **{model}** (a non-Anthropic roster peer), "
        "NOT native Claude. Claude Code's built-in identity text still says Claude "
        f"— ignore it; the model answering you is **{model}**. Begin your status "
        f"header with `[{model} / <effort>]` accordingly. Note: Genesis MCP tools "
        "may be unavailable or limited on non-Anthropic endpoints."
    )


def _model_display_name(model_id: str) -> str | None:
    """Map a Claude Code model identifier to its `Display Name Version` form.

    Returns None when the id is empty or unrecognized — the caller then falls
    back to injecting the raw id with a mapping instruction (robust to models
    newer than this table), so a stale table degrades gracefully rather than
    emitting a wrong header. Handles a bracketed context-window suffix
    (``claude-opus-4-8[1m]``) and a trailing date stamp
    (``claude-haiku-4-5-20251001``).
    """
    if not model_id:
        return None
    import re

    mid = model_id.strip().lower()
    mid = mid.split("[", 1)[0]  # drop "[1m]"-style context-window suffix
    mid = re.sub(r"-\d{8}$", "", mid)  # drop trailing -YYYYMMDD date stamp
    table = {
        "claude-fable-5": "Fable 5",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-7": "Opus 4.7",
        "claude-sonnet-5": "Sonnet 5",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-haiku-4-5": "Haiku 4.5",
    }
    return table.get(mid)


_MODEL_CACHE_FILE = Path.home() / ".genesis" / "cc_session_model.json"


def _cache_session_model(session_id: str, model: str) -> None:
    """Persist the current session's model (single O(1) slot, no retention).

    Written whenever CC provides `model` (startup/compact) so that a later
    `claude --resume` — where CC OMITS `model` — can recover it. Keyed by
    session id: the reader only trusts the cache when the id matches, so a
    stale entry from a different session is ignored, not mis-applied.
    Fail-open: any error is swallowed (the header degrades to env derivation).
    """
    if not session_id or not model:
        return
    import contextlib
    import json

    with contextlib.suppress(OSError, ValueError):
        _MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MODEL_CACHE_FILE.write_text(json.dumps({"session_id": session_id, "model": model}))


def _cached_session_model(session_id: str) -> str:
    """Read back the cached model for `session_id`, or "" on any miss.

    Guards on the id so a resume of session A never picks up session B's model.
    """
    if not session_id:
        return ""
    import json

    try:
        data = json.loads(_MODEL_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict) and data.get("session_id") == session_id:
        return str(data.get("model") or "")
    return ""


def _session_config_block(effort: str, hook_model: str, roster_model: str) -> str:
    """Top Session Configuration block: effort + first-reply status-header directive.

    Model-identity precedence (highest first):
      1. ``roster_model`` (``GENESIS_ROSTER_MODEL``) — a non-Anthropic peer set
         by ``scripts/gmodel``; already a display name.
      2. ``hook_model`` — the ``model`` field from CC's SessionStart stdin JSON.
         Unlike the baked "You are powered by …" environment line, CC re-sends
         this on EVERY SessionStart including ``compact``, so it stays correct
         after a compaction (the env line is frozen at the original session
         start and goes stale after a /model switch or a compact of an
         already-switched session — the bug this fixes).
      3. Neither present (older CC with no ``model`` field) — fall back to
         env-line derivation, the legacy behavior.

    A /model switch fires NO SessionStart event, so a switch AFTER this block is
    injected can't be captured here — the precedence note covers it ("a /model
    switch after this message wins").
    """
    tmpl = (
        "Begin your first reply of this session with a one-line status header "
        f"on its own line — `[{{model}} / {effort}]` — then your normal reply."
    )
    switch_note = (
        " If you switch models with `/model` AFTER this message, use the "
        "switched-to model on your next first-of-session header instead. "
        "No emoji, no explanation.\n"
    )
    authoritative = roster_model or _model_display_name(hook_model)
    if authoritative:
        body = (
            tmpl.replace("{model}", authoritative)
            + " That model identity is authoritative for this window (from Claude "
            "Code's session-start hook input / routing, which stays correct across "
            'context compaction — unlike the "You are powered by …" environment '
            "line, which can be stale)." + switch_note
        )
    elif hook_model:
        # Present but unmapped (a model newer than the table above): inject the
        # raw id as authoritative and let the model render its own display name.
        body = (
            tmpl.replace("{model}", "<model>")
            + f" Your current model identifier is `{hook_model}` (authoritative for "
            "this window — from Claude Code's session-start hook input, which stays "
            'correct across context compaction, unlike the "You are powered by …" '
            "environment line). Map it to its display name + version (e.g. "
            "`claude-opus-4-8` → `Opus 4.8`)." + switch_note
        )
    else:
        # No model field (older CC, or absent) — legacy env-line derivation.
        body = (
            tmpl.replace("{model}", "<model>")
            + " Derive <model> from your environment's \"You are powered by the "
            'model named …" line (e.g. `Opus 4.8`), per CONVERSATION.md → Session '
            "Start. If you switched models with `/model` this session, use the "
            "switched-to model. No emoji, no explanation.\n"
        )
    return f"## Session Configuration\n\n- Thinking effort: {effort}\n\n{body}"


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

    # Hook input (session_id, source) — mirrors genesis_session_end.py. CC
    # pipes SessionStart input as stdin JSON; before this was parsed, the
    # script had no session identity and could not read per-session state.
    import json as _json_stdin

    try:
        _raw_stdin = sys.stdin.read()
        _hook_input = _json_stdin.loads(_raw_stdin) if _raw_stdin.strip() else {}
    except (_json_stdin.JSONDecodeError, OSError):
        _hook_input = {}
    _hook_session_id = str(_hook_input.get("session_id", "") or "")
    _hook_source = str(_hook_input.get("source", "") or "")
    # CC sends `model` on SessionStart for `startup` and `compact` — the two
    # events that matter here, because it means the header stays correct across a
    # compaction (the baked "You are powered by …" env line freezes at original
    # start and does not). Per CC docs, `model` is OMITTED on `resume` (session
    # recovery) and `clear`, and absent on older CC. When it is present we cache
    # it (keyed by session id); when it is absent we read that cache back so a
    # `claude --resume` of a session whose model we saw still gets the right
    # header. Cache miss (e.g. a concurrent session overwrote the single-slot
    # cache) falls through to env-line derivation — no worse than before.
    _hook_model = str(_hook_input.get("model", "") or "")
    if _hook_model:
        _cache_session_model(_hook_session_id, _hook_model)
    elif _hook_session_id:
        _hook_model = _cached_session_model(_hook_session_id)

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

        # 0.5. Session Configuration — inject the effort level AND the
        # first-reply status-header directive into the highest-salience slot
        # (the very top of the injection). The header (`[<model> / <effort>]`)
        # is fully specified in CONVERSATION.md → "Session Start", but that spec
        # sits hundreds of lines deep and gets buried under the user's first
        # task, so it fired unreliably. Echoing the directive here — where the
        # LLM reads it first — is what makes it emit. The MODEL is now injected
        # authoritatively from CC's SessionStart `model` field (re-sent on every
        # compact, unlike the "You are powered by …" env line, which freezes at
        # original session start and goes stale after a /model switch or a
        # compact) — see _session_config_block for the precedence. Effort comes
        # from the sidecar (written by the session_config MCP tool).
        effort = "high"  # default — user's preferred effort level
        if _SESSION_CONFIG.exists():
            import json

            try:
                cfg = json.loads(_SESSION_CONFIG.read_text())
                effort = cfg.get("effort", "high")
            except Exception as exc:
                print(f"[session_context] Failed to read session config: {exc}", file=sys.stderr)
        _emit(
            _session_config_block(
                effort, _hook_model, os.environ.get("GENESIS_ROSTER_MODEL", "") or ""
            )
        )
        first = False

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
        _ek_emitted = False
        if _ek_file.exists():
            try:
                ek_content = _ek_file.read_text(encoding="utf-8").strip()
                if ek_content:
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(ek_content)
                    first = False
                    _ek_emitted = True
            except OSError:
                pass  # Essential knowledge is advisory — silent failure is correct

        # In-flight working state (advisory): active autonomy tasks, live
        # worktrees, and recently-touched plan files — computed fresh here
        # because they change far faster than the L1 essential-knowledge
        # regeneration cadence. Folds directly UNDER Essential Knowledge with
        # NO "---" divider so it reads as session context for recollection, not
        # a standalone report to recite at the user. Foreground-only (this is
        # the non-genesis-session branch). Writer: genesis.memory.open_loops.
        try:
            _inflight = _load_inflight_block()
            for _chunk in _inflight_emission_chunks(_inflight, ek_emitted=_ek_emitted, first=first):
                _emit(_chunk)
            if _inflight:
                first = False
        except Exception:
            pass  # In-flight state is advisory — never block session start

        # Session charter (advisory): the immutable origin + living mission
        # persisted by the PreCompact hook (scripts/genesis_precompact.py) —
        # re-asserted into every window so recency-biased compaction can
        # never erase what this session is FOR. Foreground-only.
        try:
            _charter_block = _charter_emission_block(_hook_session_id, _hook_source)
            if _charter_block:
                if not first:
                    _emit("\n\n---\n\n")
                _emit(_charter_block)
                first = False
        except Exception:
            pass  # Charter is advisory — never block session start

        # Repo-pulse worker (advisory): fire-and-forget the global merged-PR
        # ↔ open-ledger matcher (PR-4a). The charter block above surfaces the
        # PREVIOUS completed pulse's proposals — one boundary behind at
        # worst, by design (a pulse takes ~1 gh round-trip + 1 Haiku call).
        # The helper is fail-open end-to-end; pulse must never block session
        # start.
        _spawn_repo_pulse_worker(_hook_source)

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

        # Fallback NOTICE (advisory): Genesis's server-side ConversationLoop is
        # currently running on a roster peer (e.g. GLM) because the home model
        # (Claude) is rate-limited/exhausted account-wide. This interactive CLI
        # session runs on CC-native pinned Claude and does NOT fail over itself
        # (failover is server-side, a different process) — so the [model] header
        # stays honest and we surface the server's degraded condition here as a
        # separate block. Read the cross-process state file directly (import-free,
        # fail-open), mirroring the status.json read above.
        # Writer: src/genesis/cc/fallback_state.py.
        _fallback_file = Path.home() / ".genesis" / "cc_fallback_state.json"
        if _fallback_file.exists():
            try:
                import json as _json_fb

                fb = _json_fb.loads(_fallback_file.read_text())
                if isinstance(fb, dict) and fb.get("is_fallback"):
                    peer = fb.get("fallback") or "a roster peer"
                    home = fb.get("original") or "Claude"
                    reason = (fb.get("reason") or "unknown").replace("_", " ")
                    since = str(fb.get("since") or "")
                    since_disp = (since.replace("T", " ")[:16] + " UTC") if since else "unknown"
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(
                        "## ⚠ Genesis Fallback Active\n\n"
                        f"Genesis's server-side conversation is running on **{peer}** "
                        f"because **{home}** is unavailable (reason: {reason}; since "
                        f"{since_disp}). This is account-wide. *Your* interactive session "
                        "here still runs on native Claude — this notice reflects the "
                        "Genesis server's state, not this CLI session."
                    )
                    first = False
            except (OSError, ValueError):
                pass  # Advisory only — never block session start

        # Routed-session NOTICE (advisory): surfaces when `gmodel <peer>` launched
        # this window on a non-Anthropic roster model (see _routed_session_notice).
        _routed_notice = _routed_session_notice(os.environ.get("GENESIS_ROSTER_MODEL"))
        if _routed_notice:
            if not first:
                _emit("\n\n---\n\n")
            _emit(_routed_notice)
            first = False

    # 2.5. Active procedures (advisory, silent failure is correct)
    try:
        from genesis.learning.procedural.session_inject import load_active_procedures

        _db_path = Path.home() / "genesis" / "data" / "genesis.db"
        procedures = asyncio.run(load_active_procedures(_db_path))
        if procedures:
            if not first:
                _emit("\n\n---\n\n")
            _emit(
                "## Active Procedures\n\n"
                "Learned procedures — follow these before inventing new approaches.\n\n"
                + procedures
            )
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
                    async with aiosqlite.connect(str(_db_path_l0), timeout=2) as db:
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
                        lines.append(
                            f"- **{r['package']}**: {r['modules']} modules, {r['loc']} LOC"
                        )
                    if rest:
                        rest_mods = sum(r["modules"] for r in rest)
                        rest_loc = sum(r["loc"] for r in rest)
                        lines.append(
                            f"- *{len(rest)} more packages*: {rest_mods} modules, {rest_loc} LOC"
                        )
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
        "Use session_config tool to switch model and/or effort.\n"
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
                "session_config, "
                "outreach_queue/outreach_digest, recon tools, "
                "bookmark_shelve/bookmark_unshelve.\n\n"
                "**Skill Library:** Browse `src/genesis/skills/` or "
                "`~/.genesis/skill-library/` for specialized skills "
                "(research, outreach, browser automation, etc.). "
                "The skill injection hook nudges you when one matches."
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
                    crash_entries.append(
                        {"server": crash_file.stem, "error": "unreadable crash file"}
                    )
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

    msg = f'You signaled you wanted to return to a previous session ("{signal}").'
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


def _inflight_emission_chunks(inflight: str, *, ek_emitted: bool, first: bool) -> list[str]:
    """Emission chunks for the in-flight block (pure — drives the foreground branch).

    Folds directly under Essential Knowledge with NO "---" divider when EK was
    already emitted (so it reads as one continuous context block); otherwise it
    stands alone, preceded by the standard divider only when it is not the first
    block. Returns [] for an empty block (nothing to emit). Extracted so the
    fold/divider logic is unit-testable without a full subprocess run.
    """
    if not inflight:
        return []
    if ek_emitted:
        return ["\n\n" + inflight]  # fold under EK, no divider
    chunks: list[str] = []
    if not first:
        chunks.append("\n\n---\n\n")
    chunks.append(inflight)
    return chunks


def _load_inflight_block() -> str:
    """Fresh in-flight working-state block for the foreground session context.

    Opens its own short-timeout connection (mirroring the "2.6 Codebase L0"
    block above — isolated, never touches the shared runtime connection) and
    delegates assembly to genesis.memory.open_loops.build_inflight_block.
    Fail-open: any error returns "" so session start is never blocked.
    """
    try:
        import aiosqlite

        db_path = Path.home() / "genesis" / "data" / "genesis.db"
        if not db_path.exists():
            return ""
        repo_root = Path.home() / "genesis"
        plans_dir = Path.home() / ".claude" / "plans"

        async def _run() -> str:
            from genesis.memory.open_loops import build_inflight_block

            async with aiosqlite.connect(str(db_path), timeout=2) as db:
                db.row_factory = aiosqlite.Row
                return await build_inflight_block(db, repo_root=repo_root, plans_dir=plans_dir)

        return asyncio.run(_run())
    except Exception:
        return ""  # Advisory — never block session start


def _charter_db_path() -> Path:
    """genesis.db location, GENESIS_REPO_ROOT-aware (same resolution as the
    PreCompact hook so reader and writer always agree)."""
    import os

    root = os.environ.get("GENESIS_REPO_ROOT", "")
    base = Path(root) if root else Path.home() / "genesis"
    return base / "data" / "genesis.db"


def _spawn_repo_pulse_worker(source: str) -> None:
    """Fire-and-forget the detached repo-pulse worker (session-manager PR-4a).

    GLOBAL, not per-session: the worker enumerates merged PRs since its own
    cursor and matches them against open ledger rows across ALL sessions;
    its internal 30-minute debounce makes redundant spawns exit in ~100ms
    (spawn-then-exit, the ledger-shadow settings posture). Foreground
    boundaries only, never on clear (/clear is a fresh start). Fail-open
    end-to-end — cost to this hook is one Popen; a pulse cannot run in-hook
    because one gh round-trip (30s budget) exceeds the hook's whole budget.
    """
    import subprocess

    try:
        if os.environ.get("GENESIS_REPO_PULSE_DISABLED") == "1":
            return
        if source == "clear":
            return
        script = Path(__file__).resolve().parent / "repo_pulse_worker.py"
        err_log = Path.home() / ".genesis" / "session_awareness" / "repo_pulse_err.log"
        err_log.parent.mkdir(parents=True, exist_ok=True)
        with err_log.open("ab") as err_fh:
            subprocess.Popen(  # noqa: S603 — fixed argv, sys.executable
                [
                    sys.executable,
                    str(script),
                    "--trigger",
                    "session_start",
                    # The hook's home-anchored DB resolution is the source of
                    # truth: a worktree session's worker must not fall back
                    # to genesis.env's repo-anchored default (worktree/data/
                    # is a void — silent no-op coverage loss).
                    "--db-path",
                    str(_charter_db_path()),
                ],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err_fh,
            )
    except Exception:
        pass  # fail-open: pulse is advisory, session start is not


def _pulse_floor() -> float:
    """``inject_confidence_floor`` from the merged repo_pulse config (base
    yaml ← ``~/.genesis/config/repo_pulse.local.yaml`` overlay), defaulting
    to 0.7 on any damage. Flat local merge — this hook stays free of
    genesis imports by design (dependency-light session start).
    """
    floor = 0.7
    try:
        import yaml

        root = os.environ.get("GENESIS_REPO_ROOT", "")
        base_dir = Path(root) if root else Path.home() / "genesis"
        merged: dict = {}
        for path in (
            base_dir / "config" / "repo_pulse.yaml",
            Path.home() / ".genesis" / "config" / "repo_pulse.local.yaml",
        ):
            try:
                loaded = yaml.safe_load(path.read_text())
                if isinstance(loaded, dict):
                    merged.update(loaded)
            except Exception:
                continue
        value = merged.get("inject_confidence_floor", floor)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            floor = float(value)
    except Exception:
        pass
    return floor


def _load_charter_db(session_id: str, db_path: Path | None) -> tuple[dict | None, list[dict]]:
    """Charter row + open/in_progress ledger items from the canonical DB.

    Read-only WAL-aware connection (mode=ro — never immutable=1, which would
    miss un-checkpointed writes). Any failure — missing DB, missing table on
    a not-yet-migrated install, lock — returns (None, []) so the caller falls
    back to the legacy charter.json.
    """
    import json

    try:
        import aiosqlite

        db_file = db_path or _charter_db_path()
        if not db_file.exists():
            return None, []

        async def _run() -> tuple[dict | None, list[dict]]:
            async with aiosqlite.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT * FROM session_charters WHERE session_id = ?",
                    (session_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None, []
                charter = dict(row)
                try:
                    charter["pointers"] = json.loads(charter.get("pointers") or "[]")
                except (ValueError, TypeError):
                    charter["pointers"] = []
                cur = await db.execute(
                    "SELECT id, text, status FROM session_ledger"
                    " WHERE session_id = ? AND status IN ('open','in_progress')"
                    " ORDER BY created_at LIMIT 6",
                    (session_id,),
                )
                items = [dict(r) for r in await cur.fetchall()]
                cur = await db.execute(
                    "SELECT status, COUNT(*) FROM session_ledger"
                    " WHERE session_id = ? GROUP BY status",
                    (session_id,),
                )
                charter["_ledger_counts"] = {r[0]: r[1] for r in await cur.fetchall()}
                try:
                    # Repo-pulse proposals (PR-4a) — own guard: pre-0062
                    # installs have no pulse tables and the charter block
                    # must render byte-identically without them.
                    cur = await db.execute(
                        "SELECT item_id, item_text, pr_number, pr_title"
                        " FROM repo_pulse_annotations"
                        " WHERE item_session_id = ? AND status = 'proposed'"
                        " AND (confidence IS NULL OR confidence >= ?)"
                        " ORDER BY observed_at DESC LIMIT 3",
                        (session_id, _pulse_floor()),
                    )
                    charter["_pulse_proposals"] = [dict(r) for r in await cur.fetchall()]
                except Exception:
                    charter["_pulse_proposals"] = []
                return charter, items

        return asyncio.run(_run())
    except Exception:
        return None, []


def _load_charter_file(session_id: str, sessions_dir: Path | None) -> dict | None:
    """Legacy fallback: pre-0058 charter.json (still on disk for sessions the
    one-off backfill has not imported, or when the DB is unreachable)."""
    import json

    base = sessions_dir or (Path.home() / ".genesis" / "sessions")
    charter_file = base / session_id / "charter.json"
    try:
        return json.loads(charter_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _charter_emission_block(
    session_id: str,
    source: str,
    *,
    sessions_dir: Path | None = None,
    db_path: Path | None = None,
) -> str:
    """Session-charter block for the foreground context.

    DB-first (session_charters + open ledger items, migration 0058), falling
    back to the legacy charter.json. Emitted on startup/resume/compact so a
    chartered session gets its origin AND its open ledger back in EVERY
    window — but NOT on clear: /clear is an explicit fresh start, and
    re-asserting the old origin would fight the user.

    Returns "" when there is no charter, the session_id is missing/unsafe,
    or nothing is readable (fail-open — charter is advisory).
    """
    if not session_id or source == "clear":
        return ""
    if "/" in session_id or ".." in session_id:
        return ""
    charter, ledger = _load_charter_db(session_id, db_path)
    if charter is None:
        charter = _load_charter_file(session_id, sessions_dir)
        ledger = []
    if charter is None:
        return ""
    origin = str(charter.get("origin_prompt") or "").strip()
    if not origin:
        return ""
    if len(origin) > 1200:
        origin = origin[:1200] + " …[truncated — full text in charter.md]"
    origin_quoted = "\n".join(f"> {line}" for line in origin.splitlines())

    lines = [
        "## Session Charter (persists across compaction)",
        "",
        f"**Origin — the prompt this session was born from"
        f" ({charter.get('origin_ts') or 'time unknown'}):**",
        origin_quoted,
    ]
    mission = str(charter.get("mission") or "").strip()
    if mission:
        lines += ["", f"**Mission:** {mission[:200]}"]
    pointers = charter.get("pointers") or []
    if pointers:
        lines += ["", "**Pointers:**"]
        lines += [f"- {str(p)[:100]}" for p in pointers[:6]]
    if ledger:
        lines += ["", "**Ledger (open) — close via session_ledger_update:**"]
        for item in ledger:
            mark = "~" if item.get("status") == "in_progress" else " "
            lines.append(f"- [{mark}] {str(item.get('text', ''))[:120]} (id: {item.get('id', '')})")
    proposals = charter.get("_pulse_proposals") or []
    if proposals:
        # Repo-pulse fuzzy/bare-hex proposals (PR-4a): the exact marker tier
        # needs no line here — an absorb shrinks the open list above and its
        # evidence column tells the story.
        lines += ["", "**Pulse (proposed — confirm or ignore):**"]
        for p in proposals:
            # The hint carries the PR as evidence so a user-confirmed absorb
            # reconciles to 'confirmed' (same-PR attribution guard) instead
            # of 'superseded' — confirmed proposals ARE the precision metric.
            lines.append(
                f"- ~ '{str(p.get('item_text') or '')[:60]}' looks shipped by"
                f" PR #{p.get('pr_number')} '{str(p.get('pr_title') or '')[:50]}'"
                f" — confirm: session_ledger_update('{p.get('item_id') or ''}',"
                f" status='absorbed', evidence='PR #{p.get('pr_number')}')"
            )
    count = charter.get("compaction_count", 0)
    counts = charter.get("_ledger_counts") or {}
    footer = f"_Compactions: {count}"
    if counts:
        open_n = counts.get("open", 0) + counts.get("in_progress", 0)
        closed_n = sum(counts.values()) - open_n
        footer += f" · ledger: {open_n} open / {closed_n} closed"
    footer += f" · full charter: ~/.genesis/sessions/{session_id}/charter.md_"
    lines += ["", footer]
    block = "\n".join(lines)
    # ~600-token ceiling (char proxy): bounded by construction in the typical
    # case; the guard only bites on pathological field contents.
    if len(block) > 2800:
        block = block[:2800] + "\n_…[truncated — full charter in charter.md]_"
    return block


if __name__ == "__main__":
    main()
