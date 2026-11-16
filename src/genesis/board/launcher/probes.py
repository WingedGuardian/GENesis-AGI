"""The builder launcher's outside readings: tmux, systemd, the premise gate,
memory, the login, and whether the builder guard is wired.
:mod:`genesis.board.launcher` imports them by name, and tests replace them there;
these functions call :func:`_exec` in this module, so its stub goes here."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

#: The slot door's normal run is about 5 s (create, a 3 s settle, the capacity
#: probe). The bound is for a tmux client or capacity probe that hangs.
LAUNCH_TIMEOUT_S = 180
#: The launch unit's own deadline. systemd stops the unit at
#: :data:`LAUNCH_TIMEOUT_S` (``RuntimeMaxSec``) whatever happens to this process,
#: a server restart included; the wait here runs longer so that bound fires first.
LAUNCH_WAIT_S = LAUNCH_TIMEOUT_S + 30
#: premise_gate.py makes one gh read; this bounds a hung gh.
PREMISE_TIMEOUT_S = 120
#: A local tmux or systemctl round trip; a hang means no reachable server.
LOCAL_TIMEOUT_S = 15
#: Every launch unit is named ``<prefix><issue>-<hex>``.
UNIT_PREFIX = "genesis-builder-"

#: Read from each tmux session's own environment (a peer's, never this process's).
_KEY_VAR = "GENESIS_BUILDER_KEY"
#: The builder's issue, pinned on the same session (spec E3c, cc-slot.sh launch).
_ISSUE_VAR = "GENESIS_BUILDER_ISSUE"

logger = logging.getLogger(__name__)


def _repo_root() -> Path:
    from genesis.board import launcher

    return launcher._repo_root()


async def _exec(
    argv: list[str], *, timeout: float, cwd: str | None = None, env: dict | None = None
) -> tuple[int, str]:
    """Run *argv* (never a shell); return ``(rc, combined output)``. A timeout
    kills the process and returns rc 124."""
    from genesis.hostmetrics.jobs import systemd_env

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**systemd_env(), **(env or {})},
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timed out after {timeout}s"
    return proc.returncode or 0, out.decode(errors="replace")


async def tmux_sessions() -> dict[str, str | None]:
    """Every tmux session name, with the builder key pinned on it (or None).

    No tmux server reads as no sessions. Any other failure raises, so a
    capacity count is never taken from a reading that did not happen; a
    session that ended between the two reads is left out."""
    rc, out = await _exec(
        ["tmux", "list-sessions", "-F", "#{session_name}"], timeout=LOCAL_TIMEOUT_S
    )
    if rc != 0:
        if "no server running" in out or (
            "error connecting" in out and "No such file or directory" in out
        ):
            return {}
        raise RuntimeError(f"tmux list-sessions failed (rc={rc}): {out.strip()[:200]}")
    sessions: dict[str, str | None] = {}
    for name in (line.strip() for line in out.splitlines()):
        if not name:
            continue
        rc, env = await _exec(
            ["tmux", "show-environment", "-t", f"={name}", _KEY_VAR], timeout=LOCAL_TIMEOUT_S
        )
        line = env.strip()
        if rc == 0 and line.startswith(f"{_KEY_VAR}="):
            sessions[name] = line.split("=", 1)[1]
        elif "unknown variable" in line or line == f"-{_KEY_VAR}":
            sessions[name] = None
        elif "can't find session" in line:
            continue
        else:
            raise RuntimeError(f"tmux show-environment -t {name} failed (rc={rc}): {line[:200]}")
    return sessions


async def session_issues(sessions: dict[str, str | None]) -> dict[str, int | None]:
    """The issue pinned on each builder session (``GENESIS_BUILDER_ISSUE``, set
    by the slot door beside the key), or None where it is missing. A session
    that ended since *sessions* was read is left out; any other failure raises."""
    out: dict[str, int | None] = {}
    for name, key in sessions.items():
        if not key:
            continue
        rc, env = await _exec(
            ["tmux", "show-environment", "-t", f"={name}", _ISSUE_VAR], timeout=LOCAL_TIMEOUT_S
        )
        line = env.strip()
        if rc == 0 and line.startswith(f"{_ISSUE_VAR}="):
            value = line.split("=", 1)[1]
            out[name] = int(value) if value.isdigit() else None
        elif "unknown variable" in line or line == f"-{_ISSUE_VAR}":
            out[name] = None
        elif "can't find session" in line:
            continue
        else:
            raise RuntimeError(f"tmux show-environment -t {name} failed (rc={rc}): {line[:200]}")
    return out


async def premise_rc(issue: int, repo: str) -> int:
    """``scripts/premise_gate.py check N`` against *repo*: 0 ready, 1 answers
    missing, 2 unreadable. Anything else reads as 2, including a timeout and a
    crash: Python exits 1 on an uncaught exception too (MEASURED 2026-11-15, an
    import error), so 1 counts only with the gate's own "NOT ready" verdict."""
    root = _repo_root()
    try:
        rc, out = await _exec(
            [sys.executable, str(root / "scripts" / "premise_gate.py"), "check", str(issue)],
            timeout=PREMISE_TIMEOUT_S,
            cwd=str(root),
            # The checkout may carry several remotes; the tracker is configured.
            env={"GH_REPO": repo},
        )
    except OSError:
        logger.warning("premise gate could not run for #%s", issue, exc_info=True)
        return 2
    if rc == 1 and f"issue {issue}: NOT ready" in out:
        return 1
    return 0 if rc == 0 else 2


def mem_available_kb() -> int | None:
    """Usable memory in KiB, capped by the container's cgroup limit: inside a
    memory-limited container /proc/meminfo can describe the host's headroom."""
    from genesis.hostmetrics.readings import read_memory

    try:
        mem = read_memory()
    except Exception:
        logger.warning("builder launcher: memory read failed", exc_info=True)
        return None
    return None if mem is None else mem.available // 1024


def login_expiry() -> datetime | None:
    from genesis.cc.login_health import refresh_token_expiry

    return refresh_token_expiry()


async def run_launch(argv: list[str]) -> tuple[int, str]:
    return await _exec(argv, timeout=LAUNCH_WAIT_S, cwd=str(_repo_root()))


async def unit_state(unit: str) -> str:
    """``active``, ``gone`` or ``unknown``. MEASURED (systemd 255): is-active
    exits 0 for an active unit, 3 for an inactive one and 4 for one that no
    longer exists (a ``--collect`` unit that finished). Anything else (a hung
    systemctl, a bus error) is unknown, which the caller treats as active."""
    rc, _ = await _exec(
        ["systemctl", "--user", "is-active", "--quiet", unit], timeout=LOCAL_TIMEOUT_S
    )
    return "active" if rc == 0 else "gone" if rc in (3, 4) else "unknown"


async def starting_units() -> int:
    """Launch units still running, each an attempt whose builder may not be up
    yet. systemd retires them itself (``--collect``, and ``RuntimeMaxSec`` at
    :data:`LAUNCH_TIMEOUT_S`), so nothing here expires and no history is read.
    MEASURED (systemd 255): ``list-units --all`` prints ``name load active sub
    ...`` per loaded unit and exits 0 with none; a stopped ``--collect`` unit is
    gone. Raises when systemctl fails, so the pass launches nothing."""
    rc, out = await _exec(
        [
            "systemctl",
            "--user",
            "list-units",
            f"{UNIT_PREFIX}*",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        timeout=LOCAL_TIMEOUT_S,
    )
    if rc != 0:
        raise RuntimeError(f"systemctl list-units exited {rc}")
    return sum(
        1
        for line in out.splitlines()
        if len(f := line.split()) >= 3
        and f[0].startswith(UNIT_PREFIX)
        and f[2] not in ("inactive", "failed")
    )


async def stop_unit(unit: str) -> None:
    await _exec(["systemctl", "--user", "stop", unit], timeout=LOCAL_TIMEOUT_S)


# ─── whether the builder guard is wired ─────────────────────────────────────

#: A Claude Code hook matcher made only of these is an exact list of tool names
#: split on ``|`` or ``,``; anything else is a regex tested unanchored (READ:
#: Claude Code hooks documentation, "matcher").
_PLAIN_MATCHER = re.compile(r"[\w\- ,|]+")


def _matcher_covers_bash(matcher: str) -> bool:
    if matcher in ("", "*"):
        return True
    if _PLAIN_MATCHER.fullmatch(matcher):
        return "Bash" in {part.strip() for part in re.split(r"[|,]", matcher)}
    try:
        return re.search(matcher, "Bash") is not None
    except re.error:
        return False


def _guard_wired(settings: Path) -> bool:
    """True when the settings run the builder guard as a PreToolUse hook on Bash:
    a ``command`` hook whose command has a ``hooks/builder_guard.py`` word, in a
    block whose matcher covers ``Bash`` by Claude Code's rules, with hooks not
    disabled. A mention anywhere else does not count."""
    import shlex

    try:
        data = json.loads(settings.read_text())
        blocks = data["hooks"]["PreToolUse"]
    except (OSError, ValueError, KeyError, TypeError):
        return False
    if not isinstance(data, dict) or data.get("disableAllHooks") is True:
        return False
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict) or not _matcher_covers_bash(block.get("matcher") or ""):
            continue
        for hook in block.get("hooks") or []:
            if not isinstance(hook, dict) or hook.get("type") != "command":
                continue
            try:
                words = shlex.split(str(hook.get("command", "")))
            except ValueError:
                continue
            if "hooks/builder_guard.py" in words:
                return True
    return False
