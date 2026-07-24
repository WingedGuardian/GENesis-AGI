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
       union of CLIENT-ATTACHED tmux pane ttys and utmp login ttys —
       WS-D2 2026-07-16: a detached tmux session's panes no longer count
       as live, else the persistent cc-N slot model would make every slot
       claude unreapable forever; an attached slot is spared indefinitely,
       and a detached one stays spared while its activity marker is fresh).

This is a strict subset of the old age-only rule (kill if age >= 7d),
so arming the new logic can never reap something the current production
reaper would have spared — it only *spares* active/attached sessions the
old rule wrongly killed (the 2026-07-11 incident: interactive sessions
killed 77 min after they went quiet).

Ships in DRY-RUN by default: it logs ``WOULD KILL`` and writes an
observation but never signals a process. It arms ONLY on an explicit
operator opt-in — ``"armed_by_operator": true`` in the state JSON (set via
``set_operator_armed``) or ``GENESIS_REAPER_ARMED=1`` in the environment.
There is no automatic time-based arming: a human reviews the dry-run
WOULD-KILL log and deliberately flips the switch. A hard env kill-switch
(``GENESIS_REAPER_KILL_DISABLED``) forces dry-run regardless of the flag.
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
_KILL_GRACE_SECS = 5  # SIGTERM → SIGKILL grace (browsers flush SQLite)

_GENESIS_DIR = Path.home() / ".genesis"
_MARKER_DIR = _GENESIS_DIR / "session-activity"
_STATE_PATH = _GENESIS_DIR / "reaper_state.json"

# Hard kill-switch: when set (to a truthy value) the reaper can never arm —
# it stays in dry-run regardless of persisted state. Owner's emergency brake.
_ENV_HARD_DISABLE = "GENESIS_REAPER_KILL_DISABLED"

# Operator opt-in to actually reap (env alternative to the state-file flag).
# Absent both → dry-run. The reaper never arms itself; a human flips this.
_ENV_ARM = "GENESIS_REAPER_ARMED"

# State-file key an operator sets (via ``set_operator_armed``) to arm.
_STATE_ARMED_KEY = "armed_by_operator"


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


def proc_starttime_ticks(pid: int) -> int | None:
    """starttime of ``pid`` (clock ticks since boot, stat field 22), or None.

    ``(pid, starttime)`` is a reuse-proof process identity: starttime never
    changes for a live process, so a matching pair proves the pid was not
    recycled.
    """
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
        return int(parts[19])
    except ValueError:
        return None


def _proc_age_secs(pid: int, uptime_secs: float, clock_ticks: int) -> float | None:
    """Age of ``pid`` in seconds from ``/proc/<pid>/stat`` field 22, or None."""
    start_ticks = proc_starttime_ticks(pid)
    if start_ticks is None:
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


def _attached_pane_ttys(listing: str) -> set[str]:
    """Pane ttys of CLIENT-ATTACHED tmux sessions from ``list-panes -a -F
    '#{session_attached} #{pane_tty}'`` output. Pure — unit-tested directly.

    A malformed line is treated as attached: on ANY parse drift the reaper
    must fail toward sparing, never toward reaping. That covers both a
    non-integer first field and a tty-only line (e.g. output shaped like
    the pre-WS-D2 ``#{pane_tty}`` format).
    """
    ttys: set[str] = set()
    for line in listing.splitlines():
        fields = line.split(None, 1)
        if not fields:
            continue
        if len(fields) == 1:
            # Single-field line: format drift back to tty-only. Fail-spare —
            # count it as attached (adding junk can only ever spare).
            norm = _normalize_tty(fields[0])
            if norm:
                ttys.add(norm)
            continue
        attached_raw, tty_raw = fields
        try:
            attached = int(attached_raw)
        except ValueError:
            attached = 1  # fail-spare: unknown format counts as attached
        if attached < 1:
            continue
        norm = _normalize_tty(tty_raw)
        if norm:
            ttys.add(norm)
    return ttys


async def _live_ttys() -> set[str]:
    """Union of ATTACHED tmux pane ttys and utmp (``who``) login ttys.

    This is the live-terminal discriminator: a process whose controlling
    tty is in this set has a human plausibly looking at it (verified
    2026-07-11 — interactive sessions in-set, reparented zombies
    out-of-set). WS-D2 (2026-07-16): a tmux pane counts only while its
    session has an attached client — under the persistent cc-N slot model
    every interactive claude lives in tmux forever, so bare pane existence
    spared everything and idle detached slots could never be reaped. A
    detached slot mid-long-turn is still spared by its fresh activity
    marker (checked before the tty guard).
    """
    ttys: set[str] = set()
    # tmux panes of attached sessions only
    with contextlib.suppress(Exception):
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            "list-panes",
            "-a",
            "-F",
            "#{session_attached} #{pane_tty}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        ttys |= _attached_pane_ttys(stdout.decode())
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


_ENV_AFFIRMATIVE = frozenset({"1", "true", "yes", "on"})


def _operator_armed(state: dict) -> bool:
    """True only if a human explicitly opted in to real kills — via the
    ``armed_by_operator`` state flag or ``GENESIS_REAPER_ARMED`` set to an
    explicit affirmative (``1``/``true``/``yes``/``on``).

    The env value is parsed strictly: ``GENESIS_REAPER_ARMED=0`` / ``false`` —
    a deployment documenting that the reaper is OFF — must NOT arm it (generic
    truthiness would treat any non-empty string, including ``"0"``, as opt-in).
    """
    if state.get(_STATE_ARMED_KEY):
        return True
    return os.environ.get(_ENV_ARM, "").strip().lower() in _ENV_AFFIRMATIVE


def set_operator_armed(armed: bool) -> None:
    """Operator switch: arm (real kills) or disarm (dry-run) the reaper.

    Read-modify-writes the state JSON so the change takes effect on the next
    pass (within the hour) with no server restart. Arming is a deliberate
    human action taken after reviewing the dry-run WOULD-KILL log; disarming
    clears the flag. The hard ``GENESIS_REAPER_KILL_DISABLED`` env stays an
    independent emergency brake that overrides an armed flag.
    """
    state = _load_state()
    if armed:
        state[_STATE_ARMED_KEY] = True
    else:
        state.pop(_STATE_ARMED_KEY, None)
    _save_state(state)


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
    """One reaper pass. Dry-run unless an operator has explicitly armed it
    (``armed_by_operator`` state flag or ``GENESIS_REAPER_ARMED`` env); the
    hard kill-switch overrides. There is no automatic arming.

    ``now`` is injectable (epoch secs) for deterministic tests.
    """
    from genesis.browser.types import BROWSER_PGREP_PATTERNS

    now = now if now is not None else datetime.now(UTC).timestamp()
    clock_ticks = os.sysconf("SC_CLK_TCK")

    hard_disabled = bool(os.environ.get(_ENV_HARD_DISABLE))
    state = _load_state()
    # Arm ONLY on explicit operator opt-in (state flag or env). The hard
    # kill-switch overrides both. There is no automatic time-based arming.
    dry_run = not (_operator_armed(state) and not hard_disabled)

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
                'To disarm, remove "armed_by_operator" from '
                "~/.genesis/reaper_state.json (or call set_operator_armed(False)); "
                f"to hard-stop immediately, export {_ENV_HARD_DISABLE}=1.",
            )
        rt.record_job_success("process_reaper")
    except Exception as exc:  # noqa: BLE001 — job boundary
        rt.record_job_failure("process_reaper", exc=exc)
        logger.exception("Process reaper failed")


def _summarize(candidates: list[tuple[int, str, str, bool, list[int]]]) -> str:
    return ", ".join(f"{root}({label}/{reason})" for root, label, reason, _, _ in candidates)


async def _notify_owner(rt: GenesisRuntime, message: str) -> bool:
    """Send a verbatim ALERT to the owner. Returns True only if delivered.

    Used to notify the owner when the (operator-armed) reaper actually kills
    a detached claude process.
    """
    if rt._outreach_pipeline is None:
        return False
    from genesis.outreach.types import (
        OutreachCategory,
        OutreachRequest,
        OutreachStatus,
    )

    req = OutreachRequest(
        category=OutreachCategory.ALERT,
        topic="Process reaper",
        context=message,
        salience_score=0.9,
        signal_type="process_reaper",
        channel="telegram",
        verbatim=True,
    )
    try:
        result = await rt._outreach_pipeline.submit_urgent(req)
    except Exception:
        logger.warning("Process reaper owner notification failed", exc_info=True)
        return False
    return getattr(result, "status", None) in (
        OutreachStatus.DELIVERED,
        OutreachStatus.ENGAGED,
    )


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
