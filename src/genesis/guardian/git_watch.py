"""Guardian-side git-health watch (F.1) — the PRIMARY git-integrity detector.

The container's awareness tick probes its own git and writes a verdict to the
shared mount. But the exact outage this guards against — rootfs read-only from
thin-pool exhaustion — is likely to take the container's own alerting chain down
with it (a degraded/dead genesis-server whose awareness loop isn't ticking). So
the guardian probes LIVE via read-only ``incus exec`` — HEAD resolvable, remote
configured, and a rootfs write-probe — and treats the shared-mount verdict only
as enrichment for the alert body.

A confirmed-unhealthy result WARNs (with realert damping); recovery sends an INFO
and clears state. A probe-exec failure = container unreachable = NO signal (that
is the state machine's job, not a git alert). Never raises into the tick.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from genesis.guardian.alert.base import Alert, AlertSeverity

# Reuse the exact incus-exec-with-stdin primitive the credential watch uses —
# same login-shell / kill-on-timeout discipline. Import is side-effect-free.
from genesis.guardian.cred_watch import EpisodeDecision, _incus_exec_stdin, _parse

logger = logging.getLogger(__name__)

_STATE_FILE = "git_alert_state.json"
# Host view of the shared mount (under state_path); the container writes the
# rich verdict here via awareness/loop._check_git_health.
_VERDICT_REL = ("shared", "guardian", "git_health.json")

# Minimal, quoting-safe git-health probe piped to `bash -s` in the container.
# Emits exactly one marker line: `GITHEALTH ok` or `GITHEALTH <space-sep failures>`.
# Kept independent of the container's git_health.py (which needs the genesis
# package / venv) so it survives a broken .venv, exactly like cred_watch.
_PROBE_SCRIPT = rb"""
set -u
REPO="$HOME/genesis"
if ! cd "$REPO" 2>/dev/null; then echo "GITHEALTH repo_missing"; exit 0; fi
fails=""
git rev-parse --verify "HEAD^{commit}" >/dev/null 2>&1 || fails="$fails head_unresolvable"
# Config parseability, NOT remote presence (a valid local clone may lack origin;
# git revert is local). `git config --list` fails only on a genuinely corrupt
# config (the incident null-filled .git/config).
git config --list >/dev/null 2>&1 || fails="$fails config_invalid"
t=".git/.gw-probe-$$"
if (echo x > "$t") 2>/dev/null; then rm -f "$t"; else fails="$fails rootfs_readonly"; fi
fails="${fails# }"
echo "GITHEALTH ${fails:-ok}"
"""


def _parse_probe(stdout: str) -> dict | None:
    """Parse the GITHEALTH marker line. None = no marker (unparseable → no signal)."""
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("GITHEALTH "):
            rest = line[len("GITHEALTH ") :].strip()
            if rest == "ok":
                return {"healthy": True, "failures": []}
            return {"healthy": False, "failures": rest.split()}
    return None


async def probe_container_git(config) -> dict | None:
    """Live git-health probe via ``incus exec``.

    Returns ``{"healthy": bool, "failures": [...]}`` or None when the container is
    unreachable / the probe can't be parsed (treated as NO signal — never a false
    alert; a down container is the state machine's concern).
    """
    cfg = config.git_health
    try:
        rc, out = await _incus_exec_stdin(
            config.container_name, "bash -s", _PROBE_SCRIPT, cfg.check_timeout_s
        )
    except (TimeoutError, OSError):
        logger.warning("git_watch probe exec failed", exc_info=True)
        return None
    if rc != 0:
        return None
    return _parse_probe(out)


def decide(is_unhealthy: bool, episode: dict | None, now: datetime, cfg) -> EpisodeDecision:
    """Pure escalation decision for the single git-health signal (unit-tested).

    Requires ``confirm_ticks`` consecutive unhealthy probes before the first WARN
    (absorbs a transient blip during, e.g., a git gc), then re-alerts on cadence.
    """
    if not is_unhealthy:
        if episode and episode.get("warned_at"):
            return EpisodeDecision("resolved", "git healthy again")
        return EpisodeDecision("none", "healthy")

    consecutive = episode.get("consecutive", 0) if episode else 0
    if consecutive < cfg.confirm_ticks:
        return EpisodeDecision("none", f"unhealthy {consecutive}/{cfg.confirm_ticks} — confirming")

    if not (episode and episode.get("warned_at")):
        return EpisodeDecision("warn", "confirmed git-unhealthy")

    last_alert = _parse(episode.get("last_alert_at"))
    if last_alert and (now - last_alert).total_seconds() < cfg.realert_hours * 3600:
        return EpisodeDecision("none", "already warned, within re-alert window")
    return EpisodeDecision("realert", "still git-unhealthy after guardian warning")


def _load_episode(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text()).get("episode", {})
    except (json.JSONDecodeError, OSError):
        return {}


def _save_episode(path: Path, episode: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 1, "episode": episode}))
    except OSError:
        logger.warning("failed to persist git alert state", exc_info=True)


def _clear_episode(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.debug("failed to clear git alert state", exc_info=True)


def _verdict_detail(config) -> str:
    """Best-effort: read the container's rich verdict for extra failure detail."""
    try:
        vpath = config.state_path.joinpath(*_VERDICT_REL)
        data = json.loads(vpath.read_text())
        failures = data.get("failures") or []
        if failures:
            return f" (container verdict: {', '.join(failures)})"
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return ""


async def _send(dispatcher, severity: AlertSeverity, title: str, body: str) -> None:
    try:
        await dispatcher.send(Alert(severity=severity, title=title, body=body))
    except Exception:
        logger.warning("git_watch alert dispatch failed", exc_info=True)


async def check_container_git_and_alert(config, dispatcher) -> None:
    """Guardian tick: live-probe the container's git and alert per the ladder.

    Never raises into the tick. Alerts go through the host dispatcher (survives a
    dead container). Probe failure = container unreachable = no signal.
    """
    try:
        cfg = config.git_health
        if not getattr(cfg, "enabled", True):
            return

        probe = await probe_container_git(config)
        if probe is None:
            return  # container unreachable — the state machine handles "down"

        state_path = config.state_path / _STATE_FILE
        episode = _load_episode(state_path)
        now = datetime.now(UTC)
        is_unhealthy = not probe["healthy"]

        # Update the consecutive-unhealthy counter BEFORE deciding.
        episode["consecutive"] = (episode.get("consecutive", 0) + 1) if is_unhealthy else 0

        decision = decide(is_unhealthy, episode, now, cfg)

        if decision.action in ("warn", "realert"):
            episode["warned_at"] = episode.get("warned_at") or now.isoformat()
            episode["last_alert_at"] = now.isoformat()
            failures = ", ".join(probe["failures"]) or "unknown"
            severity = (
                AlertSeverity.WARNING if decision.action == "warn" else AlertSeverity.CRITICAL
            )
            await _send(
                dispatcher,
                severity,
                "Container git repository unhealthy",
                (
                    f"The container's local git is unhealthy ({failures}){_verdict_detail(config)}. "
                    "REVERT_CODE recovery is disabled until this is repaired — see the "
                    "recovery runbook (docs/reference/recovery-and-portability-workflow.md)."
                ),
            )
            _save_episode(state_path, episode)
        elif decision.action == "resolved":
            await _send(
                dispatcher,
                AlertSeverity.INFO,
                "Container git repository healthy again",
                "The container's local git recovered; REVERT_CODE is available again.",
            )
            _clear_episode(state_path)
        else:  # none — persist the consecutive counter (and any confirming state)
            _save_episode(state_path, episode)
    except Exception:
        logger.debug("git_watch check failed", exc_info=True)
