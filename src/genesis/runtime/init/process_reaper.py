"""Process reaper — reap leaked/orphaned processes past their thresholds.

Extracted from ``learning.py`` into a testable seam (cf.
``_wire_drip_retention_jobs``). Non-``claude`` targets (opencode-ai, browser
helpers) keep the original raw-age policy. ``claude`` TUI processes use an
IDLE policy instead:

  A ``claude`` process is a reap candidate ONLY when it is *all* of:
    1. past the age floor (7 days since start), AND
    2. idle beyond the activity window (no per-PID activity marker written
       by the session-activity hook within the last 7 days), AND
    3. detached from any live terminal (its controlling tty is not in the
       union of tmux pane ttys and utmp login ttys).

This is a strict subset of the old age-only rule (kill if age >= 7d),
so arming the new logic can never reap something the current production
reaper would have spared — it only *spares* active/attached sessions the
old rule wrongly killed (the 2026-07-11 incident: interactive sessions
killed 77 min after they went quiet).

Ships in DRY-RUN by default: it logs ``WOULD KILL`` and writes an
observation but never signals a process. After a clean observation window
(``_DRY_RUN_ARM_AFTER_SECS``) with the job running successfully, it
auto-arms and sends the owner a Telegram summary (with disable
instructions). State persists in a human-editable JSON file so the owner
can force dry-run back by hand or a hard env kill-switch can veto arming.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")

# ── Policy constants ────────────────────────────────────────────────────
_CLAUDE_AGE_FLOOR_SECS = 168 * 3600  # 7d — floor before a claude proc is even considered
_CLAUDE_IDLE_WINDOW_SECS = 168 * 3600  # 7d — "active within" window (marker freshness)
_DRY_RUN_ARM_AFTER_SECS = 3 * 86400  # arm after 3 clean dry-run days
_KILL_GRACE_SECS = 5  # SIGTERM → SIGKILL grace (browsers flush SQLite)

_GENESIS_DIR = Path.home() / ".genesis"
_MARKER_DIR = _GENESIS_DIR / "session-activity"
_STATE_PATH = _GENESIS_DIR / "reaper_state.json"

# Hard kill-switch: when set (to a truthy value) the reaper can never arm —
# it stays in dry-run regardless of persisted state. Owner's emergency brake.
_ENV_HARD_DISABLE = "GENESIS_REAPER_KILL_DISABLED"


# ── Pure decision core (fully unit-testable) ────────────────────────────
def classify_claude_pid(
    *,
    age_secs: float,
    now: float,
    marker_mtime: float | None,
    controlling_tty: str | None,
    live_ttys: set[str],
    age_floor_secs: float = _CLAUDE_AGE_FLOOR_SECS,
    idle_window_secs: float = _CLAUDE_IDLE_WINDOW_SECS,
) -> tuple[bool, str]:
    """Decide whether a ``claude`` process is a reap candidate.

    Returns ``(should_reap, reason)``. Spares (``should_reap=False``) win
    on the first matching guard, in priority order: young → fresh marker →
    live terminal. Only a process that clears all three is a candidate.
    """
    if age_secs <= age_floor_secs:
        return False, "young"
    if marker_mtime is not None and (now - marker_mtime) < idle_window_secs:
        return False, "fresh-marker"
    if controlling_tty is not None and controlling_tty in live_ttys:
        return False, "live-tty"
    return True, "stale-detached"


# ── I/O primitives (module-level so tests can monkeypatch them) ──────────
async def _pgrep(flag: str, pattern: str) -> list[int]:
    """Return PIDs matching ``pgrep <flag> <pattern>`` (empty on no match)."""
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        flag,
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return []
    return [int(p) for p in stdout.decode().split() if p.strip().isdigit()]


def _read_uptime() -> float:
    with open("/proc/uptime") as f:
        return float(f.read().split()[0])


def _proc_age_secs(pid: int, uptime_secs: float, clock_ticks: int) -> float | None:
    """Age of ``pid`` in seconds from ``/proc/<pid>/stat`` field 22, or None."""
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        raw = stat_path.read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm field (field 2) is parenthesised and may contain spaces / ')';
    # split after the LAST ')'. field 22 (start_time) is index 19 of the
    # remainder (fields 3..N).
    after_comm = raw[raw.rfind(")") + 2 :]
    parts = after_comm.split()
    if len(parts) < 20:
        return None
    try:
        start_ticks = int(parts[19])
    except ValueError:
        return None
    return uptime_secs - (start_ticks / clock_ticks)


def _marker_mtime(pid: int) -> float | None:
    """mtime (epoch secs) of the activity marker for ``pid``, or None."""
    marker = _MARKER_DIR / str(pid)
    try:
        return marker.stat().st_mtime
    except (FileNotFoundError, NotADirectoryError):
        return None


def _normalize_tty(raw: str) -> str | None:
    """Normalise a tty spec to ``pts/N`` / ``ttyN`` form, or None if not a tty."""
    t = raw.strip()
    if not t or t == "?":
        return None
    if t.startswith("/dev/"):
        t = t[len("/dev/") :]
    return t or None


async def _process_tty(pid: int) -> str | None:
    """Controlling terminal of ``pid`` (``pts/5``) via ``ps``, or None."""
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "tty=",
        "-p",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    return _normalize_tty(stdout.decode())


async def _live_ttys() -> set[str]:
    """Union of tmux pane ttys and utmp (``who``) login ttys, normalised.

    This is the live-terminal discriminator: a process whose controlling
    tty is in this set is attached to a real session (verified 2026-07-11 —
    interactive sessions in-set, reparented zombies out-of-set).
    """
    ttys: set[str] = set()
    # tmux panes
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{pane_tty}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            norm = _normalize_tty(line)
            if norm:
                ttys.add(norm)
    # utmp login ttys
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "who",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode().splitlines():
            fields = line.split()
            if len(fields) >= 2:
                norm = _normalize_tty(fields[1])
                if norm:
                    ttys.add(norm)
    return ttys


async def _get_descendants(pid: int, depth: int = 0) -> list[int]:
    """Return all descendant PIDs (children-first / bottom-up)."""
    if depth >= 10:
        return []
    proc = await asyncio.create_subprocess_exec(
        "pgrep",
        "-P",
        str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if not stdout.strip():
        return []
    children = [int(p) for p in stdout.decode().split() if p.strip().isdigit()]
    result: list[int] = []
    for child in children:
        result.extend(await _get_descendants(child, depth + 1))
    result.extend(children)
    return result


def _signal(pid: int, sig: int) -> None:
    # Defence-in-depth: never signal pid<=1 (0/-1 fan out to every process in
    # the container; 1 is init). Callers already guard, but a mock/parse slip
    # must never reach os.kill with a fan-out target.
    if pid <= 1:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, sig)


# ── Persistent dry-run / arm state ──────────────────────────────────────
def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError, NotADirectoryError):
        return {}


def _save_state(state: dict) -> None:
    with contextlib.suppress(OSError):
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(_STATE_PATH)


def _gc_markers(live_pids: set[int]) -> None:
    """Delete activity markers for PIDs that are no longer alive."""
    with contextlib.suppress(FileNotFoundError, NotADirectoryError):
        for marker in _MARKER_DIR.iterdir():
            if not marker.name.isdigit():
                continue
            if int(marker.name) not in live_pids:
                with contextlib.suppress(OSError):
                    marker.unlink()


# ── Orchestrator ────────────────────────────────────────────────────────
async def run_reaper(rt: GenesisRuntime, *, now: float | None = None) -> None:
    """One reaper pass. Dry-run by default; auto-arms after a clean window.

    ``now`` is injectable (epoch secs) for deterministic tests.
    """
    from genesis.browser.types import BROWSER_PGREP_PATTERNS

    now = now if now is not None else datetime.now(UTC).timestamp()
    clock_ticks = os.sysconf("SC_CLK_TCK")

    hard_disabled = bool(os.environ.get(_ENV_HARD_DISABLE))
    state = _load_state()
    # Effective dry-run for THIS pass: persisted flag OR the hard kill-switch.
    dry_run = bool(state.get("dry_run", True)) or hard_disabled

    my_pid = os.getpid()
    protected = {my_pid, os.getppid()}

    # (pgrep_flag, pattern, max_age_hours, label, is_claude)
    targets: list[tuple[str, str, int, str, bool]] = [
        ("-f", "opencode-ai", 24, "opencode-ai", False),
        ("-x", "claude", 168, "claude", True),
    ]
    for bp in BROWSER_PGREP_PATTERNS:
        targets.append(("-f", bp, 4, f"browser:{bp}", False))

    try:
        uptime_secs = _read_uptime()
        live_ttys = await _live_ttys()
        all_live_pids: set[int] = set()
        # Proof-of-life for the hook: did we see ANY live claude PID with a
        # fresh activity marker this pass? The marker is the sole trustworthy
        # "user is active here" signal (process structure can't distinguish a
        # live session from an orphan — verified 2026-07-11: all sessions had
        # live bash/tmux parents). Auto-arm is gated on this, so the reaper
        # never arms while its load-bearing signal is absent (hook not firing).
        saw_fresh_marker = False
        # candidates: (root_pid, label, reason, is_claude, tree)
        candidates: list[tuple[int, str, str, bool, list[int]]] = []

        for flag, pattern, max_age_h, label, is_claude in targets:
            pids = await _pgrep(flag, pattern)
            max_age = max_age_h * 3600
            for pid in pids:
                all_live_pids.add(pid)
                if pid <= 1 or pid in protected:
                    continue
                age = _proc_age_secs(pid, uptime_secs, clock_ticks)
                if age is None:
                    continue
                if is_claude:
                    tty = await _process_tty(pid)
                    marker = _marker_mtime(pid)
                    if marker is not None and (now - marker) < _CLAUDE_IDLE_WINDOW_SECS:
                        saw_fresh_marker = True
                    should_reap, reason = classify_claude_pid(
                        age_secs=age,
                        now=now,
                        marker_mtime=marker,
                        controlling_tty=tty,
                        live_ttys=live_ttys,
                    )
                else:
                    should_reap = age > max_age
                    reason = "stale" if should_reap else "young"
                if not should_reap:
                    continue
                tree = await _get_descendants(pid)
                tree.append(pid)
                candidates.append((pid, label, reason, is_claude, tree))

        _gc_markers(all_live_pids)

        if not candidates:
            await _maybe_arm(
                state,
                now,
                dry_run,
                hard_disabled,
                rt,
                hook_active=saw_fresh_marker,
                armed_summary=None,
            )
            rt.record_job_success("process_reaper")
            return

        claude_hit = any(is_claude for _, _, _, is_claude, _ in candidates)

        if dry_run:
            for root, label, reason, _is_claude, tree in candidates:
                logger.warning(
                    "Process reaper DRY-RUN: WOULD KILL pid %d (%s, reason=%s, tree=%s)",
                    root,
                    label,
                    reason,
                    tree,
                )
            await _record_observation(rt, candidates, dry_run=True)
            await _maybe_arm(
                state,
                now,
                dry_run,
                hard_disabled,
                rt,
                hook_active=saw_fresh_marker,
                armed_summary=_summarize(candidates),
            )
            rt.record_job_success("process_reaper")
            return

        # ── Armed: actually reap ────────────────────────────────────────
        killed: list[int] = []
        for _root, _label, _reason, _is_claude, tree in candidates:
            for p in tree:
                if p <= 1 or p in protected:
                    continue
                _signal(p, 15)  # SIGTERM
                killed.append(p)
        if killed:
            await asyncio.sleep(_KILL_GRACE_SECS)
            for p in killed:
                _signal(p, 9)  # SIGKILL
        logger.info(
            "Process reaper: killed %d process(es) across %d tree(s): %s",
            len(killed),
            len(candidates),
            _summarize(candidates),
        )
        await _record_observation(rt, candidates, dry_run=False, claude_hit=claude_hit)
        if claude_hit and rt._outreach_pipeline is not None:
            await _notify_owner(
                rt,
                "⚠️ Process reaper killed a detached claude process "
                f"(idle >7d, no live terminal): {_summarize(candidates)}. "
                "If this was a live session, disable the reaper by setting "
                '"dry_run": true in ~/.genesis/reaper_state.json.',
            )
        rt.record_job_success("process_reaper")
    except Exception as exc:  # noqa: BLE001 — job boundary
        rt.record_job_failure("process_reaper", str(exc))
        logger.exception("Process reaper failed")


def _summarize(candidates: list[tuple[int, str, str, bool, list[int]]]) -> str:
    return ", ".join(f"{root}({label}/{reason})" for root, label, reason, _, _ in candidates)


async def _maybe_arm(
    state: dict,
    now: float,
    dry_run: bool,
    hard_disabled: bool,
    rt: GenesisRuntime,
    *,
    hook_active: bool,
    armed_summary: str | None,
) -> None:
    """Advance the dry-run→armed lifecycle. Never arms this pass; the flip
    takes effect on the NEXT pass so the owner gets the notification + a
    veto window before anything is signalled.

    Auto-arm requires BOTH the elapsed dry-run window AND proof-of-life for
    the hook (``hook_active`` — a fresh marker was seen at least once). The
    marker is the reaper's only trustworthy activity signal, so arming while
    it has never appeared would reap on the unreliable tty backstop alone.
    ``hook_verified`` latches once true so a transient markerless pass (e.g.
    all sessions momentarily idle) can't un-verify a hook already proven live.
    """
    if not dry_run or hard_disabled:
        return  # already armed, or hard kill-switch engaged
    changed = False
    if not state.get("dry_run_since"):
        state["dry_run_since"] = now
        changed = True
    if hook_active and not state.get("hook_verified"):
        state["hook_verified"] = True
        changed = True
    elapsed = now - float(state["dry_run_since"])
    if (
        elapsed >= _DRY_RUN_ARM_AFTER_SECS
        and state.get("hook_verified")
        and not state.get("armed_at")
    ):
        state["dry_run"] = False
        state["armed_at"] = now
        changed = True
        logger.warning(
            "Process reaper auto-armed after %.1f clean dry-run days; "
            "future passes will reap detached idle claude processes.",
            elapsed / 86400,
        )
        if rt._outreach_pipeline is not None:
            msg = (
                "🔫 Process reaper auto-armed after "
                f"{elapsed / 86400:.1f} clean dry-run days. It will now reap "
                "claude processes that are >7d old AND idle >7d AND detached "
                "from any live terminal (a strict subset of the old age-only "
                "rule). Last dry-run would-kill set: "
                f"{armed_summary or 'none'}. To keep it disabled, set "
                '"dry_run": true in ~/.genesis/reaper_state.json or export '
                f"{_ENV_HARD_DISABLE}=1."
            )
            await _notify_owner(rt, msg)
    if changed:
        _save_state(state)


async def _notify_owner(rt: GenesisRuntime, message: str) -> None:
    """Send a verbatim ALERT to the owner via the outreach pipeline."""
    from genesis.outreach.types import OutreachCategory, OutreachRequest

    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Process reaper",
        context=message,
        salience_score=0.9,
        signal_type="process_reaper",
        channel="telegram",
        verbatim=True,
    )
    with contextlib.suppress(Exception):
        await rt._outreach_pipeline.submit_urgent(req)


async def _record_observation(
    rt: GenesisRuntime,
    candidates: list[tuple[int, str, str, bool, list[int]]],
    *,
    dry_run: bool,
    claude_hit: bool = False,
) -> None:
    if rt._db is None:
        return
    with contextlib.suppress(Exception):
        from uuid import uuid4

        from genesis.db.crud import observations

        priority = "high" if (claude_hit and not dry_run) else "low"
        await observations.create(
            rt._db,
            id=f"reaper-{uuid4().hex[:8]}",
            source="process_reaper",
            type="process_reaper_would_kill" if dry_run else "process_reaper_kill",
            priority=priority,
            content=json.dumps(
                {
                    "dry_run": dry_run,
                    "count": len(candidates),
                    "processes": [
                        {"pid": root, "label": label, "reason": reason}
                        for root, label, reason, _, _ in candidates
                    ],
                }
            ),
            created_at=datetime.now(UTC).isoformat(),
        )


def _wire_process_reaper(scheduler, rt) -> None:
    """Register the hourly process reaper on the learning scheduler.

    CronTrigger (not IntervalTrigger): IntervalTrigger resets on server
    restart, so the reaper would never fire if the server restarts within
    the hour. Runs at :15 past the hour to avoid hour-boundary collisions.
    """
    from apscheduler.triggers.cron import CronTrigger

    async def _reap_stale_processes() -> None:
        await run_reaper(rt)

    scheduler.add_job(
        _reap_stale_processes,
        CronTrigger(minute=15),
        id="process_reaper",
        max_instances=1,
        misfire_grace_time=600,
    )
