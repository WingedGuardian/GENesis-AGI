#!/usr/bin/env python3
"""UserPromptSubmit hook: per-prompt session-state tags.

Runs before each user message is processed. Emits one-line session-state
tags to stdout (the harness prepends them to the prompt as context) and
maintains session-scoped state files:
1. Absolute timestamp / session clock (temporal awareness)
2. Charter drift tag ([Charter: … | open: N]; chartered sessions only)
3. MCP stale-code nudge — only when THIS session's MCP subprocesses run code
   OLDER than the last deploy (they snapshot code at spawn and never reload;
   there is no auto-restart, so the user is nudged to restart); throttled
4. Rolling buffer of the last few user messages (for session bookmarks)
5. /shelve|/unshelve soft hint

This hook does NOT inject Telegram-style OUTREACH alerts into the prompt —
those flooded every prompt as a duplicate channel and were removed
2026-04-09 (delivered via the outreach pipeline instead). The tags above are
lightweight session STATE, each fail-open, not alerts. Filename retained to
avoid churn in .claude/settings.json — TODO: rename to
genesis_session_state.py in a follow-up cleanup.

Reads hook input from stdin as JSON:
  {"session_id": "...", "prompt": "...", ...}

Reads session start timestamp from ~/.genesis/session_start (written by
the SessionStart hook). Falls back to a 10-minute lookback if file missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import pathname2url

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import session_path  # noqa: E402

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
_SESSION_START_FILE = Path.home() / ".genesis" / "session_start"
_GENESIS_DIR = Path.home() / ".genesis"
_FALLBACK_LOOKBACK_MINUTES = 10
_MAX_BUFFER_LINES = 5
_MAX_MSG_LENGTH = 200

_SHELVE_PATTERN = re.compile(r"/(?:shelve|unshelve)\b", re.IGNORECASE)

# Re-nudge a stale session at most this often (staleness persists until the
# session restarts, but a single scrolled-away banner is easy to miss, so a
# quiet periodic reminder is worth more than a once-ever one).
_STALENESS_COOLDOWN_S = 3 * 3600


def _get_session_start() -> str:
    """Get session start ISO timestamp. Falls back to 10 min ago."""
    if _SESSION_START_FILE.exists():
        try:
            return _SESSION_START_FILE.read_text().strip()
        except Exception:
            pass
    return (datetime.now(UTC) - timedelta(minutes=_FALLBACK_LOOKBACK_MINUTES)).isoformat()


def _format_day_time(iso: str) -> str:
    """Format ISO timestamp as 'Mon 14:32' in user's timezone."""
    try:
        from genesis.util.tz import fmt as _tz_fmt

        return _tz_fmt(iso, "%a %H:%M")
    except ImportError:
        # Fallback if genesis not importable
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%a %H:%M")
        except (ValueError, TypeError):
            return "unknown"
    except (ValueError, TypeError):
        return "unknown"


def _session_dir(session_id: str) -> Path | None:
    """The per-session state dir, or None when the id is not a safe path component.

    Every path in this module that interpolates ``session_id`` goes through here,
    so a traversal id (``../..``) cannot escape ``~/.genesis/sessions/`` — two of
    the call sites ``mkdir(parents=True)``, so an escape would CREATE directories.
    One chokepoint rather than four hand-copied checks (see hook_input).
    """
    return session_path(_GENESIS_DIR / "sessions", session_id)


def _emit_temporal_context(session_id: str, now: datetime) -> None:
    """Emit absolute timestamp context line."""
    session_start_iso = _get_session_start()
    started = _format_day_time(session_start_iso)

    # Read last prompt time from session-scoped state
    session_dir = _session_dir(session_id)
    last_prompt_file = session_dir / "last_prompt_time" if session_dir else None
    last_msg = ""
    if last_prompt_file is not None and last_prompt_file.exists():
        with contextlib.suppress(OSError):
            last_msg = _format_day_time(last_prompt_file.read_text().strip())

    try:
        from genesis.util.tz import fmt as _tz_fmt

        clock = _tz_fmt(now.isoformat())
    except ImportError:
        clock = now.strftime("%a %Y-%m-%d %H:%M UTC")
    parts = [f"Clock: {clock}", f"Session: {session_id[:8]}", f"Started: {started}"]
    if last_msg:
        parts.append(f"Last msg: {last_msg}")

    print(f"[{' | '.join(parts)}]")
    sys.stdout.flush()


def _buffer_message(session_id: str, prompt: str, now: datetime) -> None:
    """Append user message to session-scoped rolling buffer."""
    session_dir = _session_dir(session_id)
    if session_dir is None:
        return
    session_dir.mkdir(parents=True, exist_ok=True)

    messages_file = session_dir / "messages.jsonl"
    last_prompt_file = session_dir / "last_prompt_time"

    # Write current timestamp for next temporal context
    with contextlib.suppress(OSError):
        last_prompt_file.write_text(now.isoformat())

    # Append truncated message to rolling buffer
    entry = json.dumps({
        "text": prompt[:_MAX_MSG_LENGTH],
        "timestamp": now.isoformat(),
    })

    try:
        # Read existing lines, keep last N-1, append new
        existing: list[str] = []
        if messages_file.exists():
            existing = messages_file.read_text().strip().splitlines()
        existing = existing[-((_MAX_BUFFER_LINES) - 1):]
        existing.append(entry)
        messages_file.write_text("\n".join(existing) + "\n")
    except OSError:
        pass


def _check_shelve_hint(prompt: str) -> None:
    """Detect /shelve or /unshelve and emit a soft hint."""
    if _SHELVE_PATTERN.search(prompt):
        print(
            "The user may be asking to bookmark this session. "
            "If that's their intent, use the bookmark_shelve or "
            "bookmark_unshelve MCP tool."
        )
        sys.stdout.flush()


def _ro_uri(db_path: Path) -> str:
    """A WAL-aware read-only SQLite URI for ``db_path``, percent-encoding the path.

    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` silently opens the WRONG
    (empty) database if the filesystem path contains a URI-special char: a ``?`` or
    ``#`` truncates the path there (verified — SQLite then reads a different file and
    the query hits "no such table"), so the read fails closed to ``None`` on an install
    whose repo path contains one. ``pathname2url`` percent-encodes the path (``?``→%3F,
    ``#``→%23, space→%20, ``%``→%25); SQLite decodes it back, so the real DB opens.
    Idempotent on an ordinary path (no special chars → unchanged).
    """
    return f"file:{pathname2url(str(db_path))}?mode=ro"


# The tag runs on EVERY prompt, so it is bounded on three axes: rows shown,
# characters per row, and total bytes. Generous enough that an ordinary ledger
# renders whole; the caps exist so a pathological one cannot flood every turn.
_TAG_MAX_ROWS = 8
_TAG_ROW_CHARS = 140
_TAG_MISSION_CHARS = 80
_TAG_MAX_BYTES = 1_500


def _escalation_dedup_key(ledger_id: str) -> str:
    """`follow_ups.dedup_key` linking a ledger row to its escalation follow-up.

    Inlined rather than imported: this hook is stdlib-only by design (a broken
    venv must never wedge a prompt). `genesis.session_awareness.ledger_escalation_link`
    owns the formula; `tests/test_scripts/test_urgent_alerts_charter_tag.py`
    asserts THIS function equals the package one, so a change there that this
    does not follow fails loudly instead of silently unlinking every row.
    """
    import hashlib

    return hashlib.sha256(f"ledger_escalation|{ledger_id}".encode()).hexdigest()


def _emit_charter_tag(session_id: str) -> None:
    """Per-prompt ledger INVENTORY: the head line plus one line per open row.

    This used to print a COUNT — `[Charter: <label> | open: N]`. A count is
    indistinguishable from N items already handled, and that is not theoretical:
    a session ran seven days with three founding agreements open behind exactly
    that number while the model read past it every turn. The same
    count-instead-of-inventory defect had just been fixed in the merge gate.
    So every open row is NAMED here, with the id `session_ledger_update` needs.

    The label degrades honestly too. With no mission set the old tag fell back to
    the origin prompt's first 60 characters, which for a spoken prompt is a
    half-formed clause that reads as noise — so after a compaction (when the
    purpose IS knowable) it says the field is UNSET instead, and names the tool
    that fills it.

    Read-only stdlib sqlite3, mode=ro URI (WAL-aware — never immutable=1, which
    misses un-checkpointed writes), 500ms connect / 300ms busy budget: this runs
    on EVERY prompt and must never cost the user anything. Omitted entirely when
    the session has no charter row yet (pre-first-compaction — the origin is
    still in context), the DB/table is missing (un-migrated install), or the DB
    is locked. "open: 0" IS shown for a chartered session — a clear ledger is
    signal.
    """
    try:
        root = os.environ.get("GENESIS_REPO_ROOT", "")
        db = (Path(root) if root else Path.home() / "genesis") / "data" / "genesis.db"
        if not db.exists():
            return
        conn = sqlite3.connect(_ro_uri(db), uri=True, timeout=0.5)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            row = conn.execute(
                "SELECT mission, origin_prompt, compaction_count FROM session_charters"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            rows = conn.execute(
                "SELECT id, text, status FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open','in_progress') ORDER BY created_at LIMIT ?",
                (session_id, _TAG_MAX_ROWS),
            ).fetchall()
            (open_n,) = conn.execute(
                "SELECT COUNT(*) FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open','in_progress')",
                (session_id,),
            ).fetchone()
            escalations = {}
            try:
                # Own guard: an install without the follow_ups table (or without
                # the dedup index) renders exactly as it did before.
                keys = {_escalation_dedup_key(str(r[0])): str(r[0]) for r in rows}
                if keys:
                    placeholders = ",".join("?" * len(keys))
                    for fid, dk in conn.execute(
                        "SELECT id, dedup_key FROM follow_ups"  # noqa: S608
                        # Interpolates only '?' placeholders; values are bound.
                        f" WHERE dedup_key IN ({placeholders})",
                        tuple(keys),
                    ).fetchall():
                        if keys.get(dk):
                            escalations[keys[dk]] = str(fid)
            except Exception:
                escalations = {}
        finally:
            conn.close()
        mission, origin, compactions = row[0], row[1], row[2] or 0
        label = (mission or "").strip()
        first_line = next((ln for ln in (origin or "").strip().splitlines() if ln.strip()), "")
        if not label and not first_line:
            # A STUB row: the PreCompact hook creates the row and fills the
            # origin later, so "no mission" here does not mean drift \u2014 it means
            # there is no charter yet. Checked BEFORE the drift branch, which
            # would otherwise nag about an unset mission on a charter that does
            # not exist (caught by the omission-matrix test).
            return
        if label:
            label = "mission: " + label[:_TAG_MISSION_CHARS] + (
                "\u2026" if len(label) > _TAG_MISSION_CHARS else ""
            )
        elif compactions >= 1:
            label = (
                f"mission: UNSET after {compactions} compactions"
                " \u2014 session_charter_update"
            )
        else:
            snippet = first_line[:60] + ("\u2026" if len(first_line) > 60 else "")
            label = f'origin: "{snippet}"'

        head = f"[Ledger open: {open_n} | {label}]"
        body_lines = []
        for rid, text, status in rows:
            mark = " [~]" if status == "in_progress" else ""
            body = str(text or "")
            if len(body) > _TAG_ROW_CHARS:
                body = body[:_TAG_ROW_CHARS] + "\u2026"
            link = ""
            fid = escalations.get(str(rid))
            if fid:
                link = f" \u2192 escalated: follow_up {fid[:8]}"
            body_lines.append(f"- {str(rid)[:8]}{mark} {body}{link}")

        # Trim rows to fit the byte cap, then RECOMPUTE the overflow pointer for
        # what actually rendered. A first version popped lines off the end, which
        # ate the pointer FIRST and left a truncated list looking complete \u2014
        # count-instead-of-inventory reintroduced by the very cap meant to keep
        # the inventory affordable (MEASURED: 12 open rows rendered as 7, no
        # pointer). The pointer is the one line that must survive: it is what
        # says "this list is not all of it".
        def _assemble(lines: list[str]) -> str:
            shown = len(lines)
            tail = (
                [
                    f"\u2026and {open_n - shown} more \u2014 see"
                    f" ~/.genesis/sessions/{session_id}/charter.md"
                ]
                if open_n > shown
                else []
            )
            return "\n".join([head, *lines, *tail])

        tag = _assemble(body_lines)
        while len(tag.encode("utf-8")) > _TAG_MAX_BYTES and body_lines:
            body_lines.pop()
            tag = _assemble(body_lines)
        print(tag)
        sys.stdout.flush()
    except Exception:
        return  # fail-open: a tag miss must never surface as an error


def _claude_ancestor_pid() -> int | None:
    """The owning interactive ``claude`` session pid for this hook, or None.

    This hook runs as ``claude → genesis-hook launcher → python``, so the
    session's ``claude`` process is an ANCESTOR (not necessarily the direct
    parent). Walk the ppid chain (bounded, never loops on a cycle) to the first
    ``comm == "claude"``. ``/proc/<pid>/{comm,stat}`` are world-readable — no
    ptrace gate. Fail-open None on any read/parse failure (a nudge miss must
    never cost the user)."""
    pid = os.getpid()
    for _ in range(16):
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None
        if comm == "claude":
            return pid
        try:
            # After the last ')' (comm may contain spaces/')'): state=field3=idx0,
            # ppid=field4=idx1.
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if ppid <= 1 or ppid == pid:
            return None
        pid = ppid
    return None


def _last_successful_deploy(db_path: Path) -> tuple[str, str] | None:
    """``(completed_at, new_commit)`` of the newest successful deploy, or None.

    Read-only stdlib sqlite3 (``mode=ro`` URI — WAL-aware, never ``immutable=1``
    which misses un-checkpointed writes), tight budget: runs on EVERY prompt.
    Mirrors ``db.crud.update_history.last_successful_update`` exactly (same
    ``status='success'`` filter + ``datetime(completed_at)`` ordering) but
    stdlib-only so the hook never imports aiosqlite. None on any failure."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=0.5)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            row = conn.execute(
                "SELECT completed_at, new_commit FROM update_history "
                "WHERE status = 'success' ORDER BY datetime(completed_at) DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]), str(row[1])


def _staleness_message(
    spawn_commit: str, spawn_at: str, deploy: tuple[str, str] | None
) -> str | None:
    """The nudge line if this session's MCP code is BEHIND the deploy, else None.

    ``deploy`` is ``(completed_at, new_commit)``. Delegates the verdict to
    ``commit_identity.is_stale`` — the EXACT same leaf the dashboard stale-code
    badge and the Part-A guard use, so all three can never disagree (a session
    AHEAD of the last recorded deploy, e.g. a manual ``git pull``, is NOT
    flagged). Pure (no IO) so the verdict+wording is unit-testable."""
    from genesis.observability.commit_identity import is_stale

    if not deploy:
        return None
    completed_at, new_commit = deploy
    # Defense-in-depth: this value becomes LLM-visible prompt context, so only ever
    # interpolate a real short/full SHA. update_history is deploy-pipeline-only today
    # (no user-facing writer), but a hex guard removes the vector entirely rather
    # than trusting provenance. Also fail-open (skip) on a malformed commit.
    if not re.fullmatch(r"[0-9a-fA-F]{4,64}", new_commit or ""):
        return None
    if not is_stale(spawn_commit, spawn_at, completed_at, new_commit):
        return None
    date = str(completed_at)[:10]  # YYYY-MM-DD
    return (
        f"[⚠ Memory MCP stale: this session's MCP predates the {date} deploy "
        f"(now {new_commit[:8]}) — restart the session to refresh recall "
        f"+ security read-exclusions]"
    )


def _staleness_throttled(session_id: str, now: datetime) -> bool:
    """True if a staleness nudge fired within the cooldown for this session.

    Best-effort: an unreadable/absent/garbled marker → not throttled (emit),
    consistent with the fail-open posture (better a repeat nudge than a missed
    stale-code warning). A marker timestamp in the FUTURE (clock stepped back, or a
    hand-edited file) is likewise treated as NOT throttled: a negative elapsed is
    always < cooldown, which would otherwise wedge the nudge OFF until wall-clock
    catches up + a full cooldown — suppressing a genuine stale-code warning."""
    sdir = _session_dir(session_id)
    if sdir is None:
        return False
    marker = sdir / "staleness_last_nudge"
    try:
        last = datetime.fromisoformat(marker.read_text().strip())
        elapsed = (now - last).total_seconds()
    except (OSError, ValueError, TypeError):
        # unreadable / garbled / tz-naive (aware-minus-naive raises TypeError) →
        # not throttled (emit), fail-open toward surfacing the stale-code warning.
        return False
    if elapsed < 0:
        return False  # future marker → do not suppress; emit
    return elapsed < _STALENESS_COOLDOWN_S


def _record_staleness_nudge(session_id: str, now: datetime) -> bool:
    """Stamp the per-session cooldown marker. Returns True iff it persisted.

    A persistently unwritable ``~/.genesis/sessions/`` would otherwise let the
    nudge print on EVERY prompt — the absent/unreadable-marker path in
    ``_staleness_throttled`` reads as "not throttled" (fail-open toward emitting).
    Returning False lets the caller SUPPRESS instead of spam, so a broken
    filesystem degrades to silence, not a per-prompt banner."""
    sdir = _session_dir(session_id)
    if sdir is None:
        return False
    marker = sdir / "staleness_last_nudge"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now.isoformat())
        return True
    except OSError:
        return False


def _emit_staleness_nudge(session_id: str, now: datetime) -> None:
    """One-line nudge when THIS session's MCP subprocesses run code OLDER than
    the last deploy.

    Each FastMCP subprocess snapshots its code at spawn and NEVER reloads; a
    deploy landing MID-session (update.sh restarts genesis-server, NOT a live
    session's MCPs) leaves recall — and its current security read-exclusions —
    on stale code until the user restarts the session. There is no auto-restart,
    so the remedy is a nudge. Identify this session's slot by matching the
    walked ``claude`` pid against the ``mcp-spawn`` file plane (world-readable;
    pid- AND start-time-validated by ``read_spawn_identity`` against a recycled
    pid), then apply the shared ``is_stale`` verdict. Throttled per session;
    wrapped fail-open so a miss never surfaces as an error and a fresh session
    stays silent."""
    try:
        from genesis.observability.cc_slots import read_proc_start_iso
        from genesis.observability.mcp_spawn_store import (
            enumerate_spawn_slots,
            read_spawn_identity,
        )

        pid = _claude_ancestor_pid()
        if pid is None:
            return
        # Find THIS session's slot by matching its claude pid in the spawn plane
        # (env GENESIS_SLOT would also work, but a pid match can't be fooled by an
        # inherited/stale slot value and mirrors the dashboard badge's join).
        slot = next((s for s, rpid, _sat in enumerate_spawn_slots() if rpid == pid), None)
        if slot is None:
            return
        ident = read_spawn_identity(slot, pid, read_proc_start_iso(pid))
        if ident is None:
            return
        root = os.environ.get("GENESIS_REPO_ROOT", "")
        db = (Path(root) if root else Path.home() / "genesis") / "data" / "genesis.db"
        msg = _staleness_message(ident[0], ident[1], _last_successful_deploy(db))
        if not msg:
            return
        if _staleness_throttled(session_id, now):
            return
        # Persist the cooldown BEFORE printing: if the marker can't be written
        # (unwritable session dir), suppress rather than spam the banner on every
        # prompt — the absent-marker throttle path reads as "not throttled".
        if not _record_staleness_nudge(session_id, now):
            return
        print(msg)
        sys.stdout.flush()
    except Exception:
        return  # fail-open: a nudge miss must never surface as an error


_MARKER_STEM = "injection_over_budget"
_LEGACY_MARKER_DIR = _GENESIS_DIR / "session_awareness"


def _budget_marker_files(session_id: str) -> list[Path]:
    """This session's over-budget markers (one file per part; see the emitter).

    Per-(session, part) files rather than one shared JSON: the four parts run
    concurrently, so a shared dict raced — an under-budget sibling's clear could
    erase an over-budget part's entry, and a global file let one session silence
    another. Also sweeps the legacy session-less path so a marker written by an
    older emitter still screams.
    """
    found: list[Path] = []
    d = _session_dir(session_id)
    if d is not None:
        found.extend(sorted(d.glob(f"{_MARKER_STEM}*.json")))
    found.extend(sorted(_LEGACY_MARKER_DIR.glob(f"{_MARKER_STEM}*.json")))
    return found


def _emit_injection_over_budget_alert(session_id: str) -> None:
    """SCREAM, every prompt, while the SessionStart injection is over budget.

    The failure this guards is silent by construction: the harness files an
    over-cap injection and hands the model a 2 KB preview, so identity, the
    charter, and essential knowledge simply never arrive — and nothing looks
    wrong. That ran for a MONTH on this install (143 filings, 58 sessions)
    before anyone noticed. This line is deliberately loud and repeats until the
    SessionStart hook clears the marker with an under-budget run.
    """
    try:
        wiring: list[str] = []
        over: list[str] = []
        for path in _budget_marker_files(session_id):
            try:
                info = json.loads(path.read_text())
            except (OSError, ValueError):
                # An unreadable marker is still evidence something wrote one.
                over.append(f"{path.stem} (unreadable)")
                continue
            if not isinstance(info, dict):
                continue
            part = str(info.get("part") or path.stem)
            if part == "wiring":
                wiring.append(str(info.get("reason") or "unknown reason"))
            else:
                over.append(
                    f"{part} ({info.get('chars', '?')}/{info.get('budget', '?')} chars at "
                    f"{str(info.get('ts', '?'))[:16]})"
                )
        if wiring:
            print(
                f"[ALERT: session-context hook MIS-WIRED — {'; '.join(wiring)}. Only the "
                "charter part was emitted, so identity and essential knowledge are MISSING "
                "from this session. Fix the four --part SessionStart entries in "
                ".claude/settings.json, then restart the session.]"
            )
        if over:
            print(
                f"[ALERT: session-context injection OVER BUDGET — part(s) {', '.join(over)}. "
                "The harness FILES a hook's stdout above its cap and previews ~2 KB, so those "
                "parts (identity/charter/EK) may be MISSING from this window. Trim "
                "scripts/genesis_session_context.py's payload; each part's alarm clears on "
                "its next under-budget session start.]"
            )
        if wiring or over:
            sys.stdout.flush()
    except Exception:
        return  # fail-open: the alert must never break the prompt


def main() -> None:
    # Skip if Genesis context is disabled
    if not _FLAG.exists():
        return

    # Skip for Genesis-dispatched sessions (they have their own alert path)
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    prompt = hook_input.get("prompt", "")
    now = datetime.now(UTC)

    # 1. Temporal context (always, even if no session_id)
    if session_id:
        _emit_temporal_context(session_id, now)
        # 1a. Injection-over-budget / mis-wire SCREAM (every prompt until cleared)
        _emit_injection_over_budget_alert(session_id)
        # 1b. Charter drift tag (chartered sessions only; fail-open)
        _emit_charter_tag(session_id)
        # 1c. MCP stale-code nudge (only when this session's MCP is behind the
        # last deploy; throttled; fail-open)
        _emit_staleness_nudge(session_id, now)

    # 2. Buffer user message for bookmarks
    if session_id and prompt:
        _buffer_message(session_id, prompt, now)

    # 3. Shelve/unshelve hint
    if prompt:
        _check_shelve_hint(prompt)


if __name__ == "__main__":
    main()
