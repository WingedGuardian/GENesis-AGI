"""Daily auth canary for the Guardian's recovery brain.

The recovery brain (``diagnosis.DiagnosisEngine``) only runs when Genesis is
down, so a brain that cannot authenticate used to be discovered mid-outage, as
"all automated recovery failed". The Guardian's other auth check reads only
whether a token exists and how old it is, never whether it works.

Once per ``cc.canary_interval_h``, on a tick whose probe found Genesis healthy,
this makes one minimal real ``claude -p`` call through the diagnosis's OWN
credential path (``DiagnosisEngine._resolve_cc_env``) and error reader
(``diagnosis._cc_error_reason``). A failure pages once per episode with Claude
Code's own reason; the first success after that pages an all-clear.

State lives in ``<state_dir>/cc_canary.json``. Before a run it records the
attempt, and after the run its result. A run that never finishes (the tick was
killed) or whose result could not be saved is retried, at most once per
``_ATTEMPT_SPACING``. A page counts as delivered when it was sent or queued for
the host queue drain to replay. Never raises into the tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from genesis.cc.child_env import pin_dispatched_env
from genesis.guardian.alert.base import Alert, AlertSeverity
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import (
    DiagnosisEngine,
    _cc_error_reason,
    _ensure_no_mcp_config,
    _ensure_work_dir,
)
from genesis.util.atomic import atomic_write_text
from genesis.util.proc_kill import kill_process_group, reap_bounded

logger = logging.getLogger(__name__)

_STATE_FILE = "cc_canary.json"
# One tiny turn: node startup plus a single round-trip. Generous for a slow
# host, far below the tick's own timer, and bounded so a wedged CLI is reaped.
_CANARY_TIMEOUT_S = 180
_PROMPT = "Reply with the single word ok."
# Least time between two attempts whatever the interval, so a run that keeps
# being killed, or a result that keeps failing to save, cannot start a real
# call on every 30 s tick.
_ATTEMPT_SPACING = timedelta(hours=1)


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _age(state: dict, key: str, now: datetime) -> timedelta | None:
    value = state.get(key)
    if not isinstance(value, str):
        return None
    try:
        return now - datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _due(state: dict, interval_h: int, now: datetime) -> bool:
    # A time "in the future" means the clock went backwards: treat it as old
    # rather than stay silent until the clock catches up.
    attempt = _age(state, "attempt_at", now)
    if attempt is not None and timedelta(0) <= attempt < _ATTEMPT_SPACING:
        return False
    age = _age(state, "last_run_at", now)
    return age is None or age < timedelta(0) or age >= timedelta(hours=max(1, interval_h))


async def _run_once(config: GuardianConfig, engine: DiagnosisEngine) -> tuple[bool, str]:
    """(ok, reason) for one minimal real call through the diagnosis's credentials."""
    cc_path = str(Path(config.cc.path).expanduser())
    work_dir = _ensure_work_dir(Path(config.cc.work_dir).expanduser())
    # The configured model, as the diagnosis runs: an entitlement that covers the
    # default model but not this one must fail here, not mid-outage.
    cmd = [cc_path, "-p", "--model", config.cc.model, "--output-format", "json", "--max-turns", "1"]
    # The diagnosis's own empty MCP config, created here when no diagnosis has
    # run yet, so the canary never starts the host's configured MCP servers.
    cmd += ["--mcp-config", _ensure_no_mcp_config(work_dir), "--strict-mcp-config"]
    cmd.append(_PROMPT)
    env = await engine._resolve_cc_env(cc_path)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env=pin_dispatched_env(dict(env or os.environ)),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CANARY_TIMEOUT_S)
    except TimeoutError:
        kill_process_group(proc)
        await reap_bounded(proc)
        return False, f"timed out after {_CANARY_TIMEOUT_S}s"
    except asyncio.CancelledError:
        kill_process_group(proc)
        await reap_bounded(proc)
        raise
    out = stdout.decode("utf-8", errors="replace")
    try:
        envelope = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        envelope = None
    failed = proc.returncode != 0 or (isinstance(envelope, dict) and envelope.get("is_error"))
    if failed:
        return False, f"exit {proc.returncode}: " + _cc_error_reason(
            out,
            stderr.decode("utf-8", errors="replace"),
        )
    return True, "ok"


async def maybe_run_cc_canary(
    config: GuardianConfig,
    engine: DiagnosisEngine,
    dispatcher,
    *,
    now: datetime | None = None,
) -> None:
    """Run the canary when it is due; page on a failure episode and its end."""
    if not (config.cc.enabled and config.cc.canary_enabled):
        return
    now = now or datetime.now(UTC)
    path = config.state_path / _STATE_FILE
    state = _load(path)
    if not _due(state, config.cc.canary_interval_h, now):
        return
    # The state file is what rate-limits the canary. If it cannot be written,
    # every 30 s tick would be "due" and run (and page) again, so do not run.
    try:
        atomic_write_text(path, json.dumps({**state, "attempt_at": now.isoformat()}, indent=2))
    except OSError:
        logger.warning("cannot write %s; skipping the auth canary this tick", path, exc_info=True)
        return
    try:
        ok, reason = await _run_once(config, engine)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # spawn failure, missing binary, unwritable work dir
        ok, reason = False, f"{type(exc).__name__}: {exc}"[:300]

    was_alerted = bool(state.get("alerted"))
    new_state = {
        "last_run_at": now.isoformat(),
        "ok": ok,
        "reason": reason,
        "alerted": was_alerted,
    }
    if ok:
        logger.info("Guardian recovery-brain auth canary: ok")
        if was_alerted:
            sent = await dispatcher.send_or_queue(
                Alert(
                    severity=AlertSeverity.INFO,
                    title="Guardian recovery brain can authenticate again",
                    body="The daily auth canary succeeded; the Guardian can diagnose an outage again.",
                )
            )
            new_state["alerted"] = not sent
    else:
        logger.error("Guardian recovery-brain auth canary FAILED: %s", reason)
        if not was_alerted:
            sent = await dispatcher.send_or_queue(
                Alert(
                    severity=AlertSeverity.WARNING,
                    title="Guardian recovery brain cannot authenticate",
                    body=(
                        "The daily auth canary failed, so if Genesis goes down the Guardian "
                        "cannot diagnose or repair it, only page.\n"
                        f"Claude Code said: {reason}"
                    ),
                )
            )
            new_state["alerted"] = bool(sent)
    try:
        atomic_write_text(path, json.dumps(new_state, indent=2))
    except OSError:
        logger.warning("could not write %s", path, exc_info=True)
