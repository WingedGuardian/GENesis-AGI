"""Ambient-capture health monitor — read the edge bridge's health file + evaluate.

The ambient bridge runs on a separate edge VM and writes ``ambient_health.json``
every 60s. This module SSH-reads that file (connection in
``~/.genesis/ambient_remote.yaml``, mirroring ``guardian_remote.yaml``) and turns
a snapshot into an alert verdict. ``OutreachScheduler._ambient_health_job`` runs
the read + evaluate on a cadence and alerts the user on a bad-state transition;
``probe_ambient_health`` surfaces the verdict in the infrastructure snapshot; and
``bridge_snapshot`` serves the dashboard voice Bridge tab the full health payload
on demand (``GET /api/genesis/voice/bridge``).

No config file -> the monitor is a silent no-op, so installs without an ambient
edge are unaffected. The edge host/IP lives ONLY in the install-local config,
never in committed source.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("~/.genesis/ambient_remote.yaml").expanduser()
_DEFAULT_HEALTH_PATH = "~/ambient_health.json"
_DEFAULT_KEY = "~/.ssh/id_ed25519"

# A health snapshot whose heartbeat ``ts`` is older than this means the bridge
# process is dead/hung (the bridge rewrites the file every 60s).
_HEARTBEAT_STALE_S = 300.0
_SSH_TIMEOUT_S = 10.0

# RSS ceilings for the leak-class regression watch. Calibrated from the
# post-arena-off soak (closed 2026-07-06, 4.6 days): total plateau ~470 MB with
# activity bursts to ~780; diar child flat ~170 with window excursions to ~280.
# Ceilings sit ~2x the worst observed burst so workload breathing never fires
# them — only a real regression of the leak class does (pre-fix pathology was
# 1.67 GB total / 840+ MB child).
_RSS_TOTAL_ALERT_MB = 1000.0
_RSS_DIAR_CHILD_ALERT_MB = 450.0


@dataclass(frozen=True)
class AmbientRemoteConfig:
    host_ip: str
    host_user: str
    ssh_key: str = _DEFAULT_KEY
    health_path: str = _DEFAULT_HEALTH_PATH


class AmbientRemoteConfigError(Exception):
    """Raised when ``~/.genesis/ambient_remote.yaml`` is present AND enabled but
    malformed (unparseable, or missing ``host_ip``/``host_user``).

    Distinct from absent/disabled (which return ``None``) so callers can surface a
    VISIBLE "misconfigured" state — instead of a config typo silently looking
    identical to "no ambient edge configured" and disabling monitoring unnoticed."""


def load_ambient_remote_config() -> AmbientRemoteConfig | None:
    """Load ``~/.genesis/ambient_remote.yaml``.

    Returns ``None`` when the config is **absent** or **explicitly disabled**
    (``enabled: false``) — both legitimate no-ops on installs without an ambient
    edge. Raises :class:`AmbientRemoteConfigError` when the file is **present and
    enabled but malformed** (unparseable, or missing ``host_ip``/``host_user``), so
    a config typo surfaces visibly instead of silently looking like "not configured"."""
    if not _CONFIG_PATH.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
    except Exception as exc:
        raise AmbientRemoteConfigError(
            f"ambient_remote.yaml could not be parsed: {exc}"
        ) from exc
    if not data.get("enabled", True):
        return None
    host_ip = data.get("host_ip")
    host_user = data.get("host_user")
    if not host_ip or not host_user:
        raise AmbientRemoteConfigError(
            "ambient_remote.yaml is enabled but missing host_ip/host_user"
        )
    return AmbientRemoteConfig(
        host_ip=str(host_ip),
        host_user=str(host_user),
        ssh_key=str(data.get("ssh_key", _DEFAULT_KEY)),
        health_path=str(data.get("health_path", _DEFAULT_HEALTH_PATH)),
    )


async def read_edge_health(cfg: AmbientRemoteConfig) -> dict | None:
    """SSH-read the edge ``ambient_health.json``. Returns the parsed dict, or None
    on any failure (unreachable / missing / bad JSON). Mirrors
    ``GuardianRemote._ssh_command`` (BatchMode + ConnectTimeout + orphan-kill)."""
    key = str(Path(cfg.ssh_key).expanduser())
    cmd = [
        "ssh", "-i", key,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={int(_SSH_TIMEOUT_S)}",
        "-o", "BatchMode=yes",
        f"{cfg.host_user}@{cfg.host_ip}",
        f"cat {cfg.health_path}",
    ]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=_SSH_TIMEOUT_S + 5,
        )
        if proc.returncode != 0:
            logger.warning(
                "ambient health read failed (rc=%s): %s",
                proc.returncode, stderr.decode().strip()[:200],
            )
            return None
        return json.loads(stdout.decode())
    except TimeoutError:
        if proc is not None:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
        logger.warning("ambient health SSH read timed out (%s@%s)", cfg.host_user, cfg.host_ip)
        return None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning("ambient health read error: %s", exc)
        return None


@dataclass(frozen=True)
class AmbientVerdict:
    status: str  # "ok" | "degraded" | "down" | "unknown"
    reasons: list[str]


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def evaluate_ambient_health(
    data: dict | None, *, now: datetime | None = None,
) -> AmbientVerdict:
    """Turn an ``ambient_health.json`` snapshot into a verdict. Pure + testable.

    Policy: alert ONLY on a SOFTWARE failure, NEVER on the device being absent.
    - ``data is None`` (read failed / edge unreachable) -> ``unknown`` (transient;
      the caller should not flip alert state on a single unknown).
    - heartbeat ``ts`` missing or older than ``_HEARTBEAT_STALE_S`` -> ``down``
      (the bridge process is dead/hung — it stopped writing the health file).
    - ``diar_worker_alive`` False while ``diar_enabled`` -> ``degraded`` (the
      diarization worker crashed).
    - otherwise -> ``ok`` — INCLUDING when the device is offline
      (``active_connections == 0``): an absent device is not a software bug.

    Deliberately does NOT consider ``active_connections`` or ``last_ts``: a device
    that is unplugged/idle, or a quiet room with no recent utterance, is normal —
    not a fault to alert on.
    """
    if data is None:
        return AmbientVerdict("unknown", ["edge health unreachable / unreadable"])

    now = now or datetime.now(UTC)
    reasons: list[str] = []
    status = "ok"

    ts = _parse_ts(data.get("ts"))
    if ts is None:
        reasons.append("health file has no/invalid heartbeat ts")
        status = "down"
    else:
        age = (now - ts).total_seconds()
        if age > _HEARTBEAT_STALE_S:
            reasons.append(
                f"heartbeat stale ({age:.0f}s > {_HEARTBEAT_STALE_S:.0f}s) — bridge dead/hung",
            )
            status = "down"

    if data.get("diar_enabled") and not data.get("diar_worker_alive", False):
        reasons.append("diarization worker not alive")
        if status == "ok":
            status = "degraded"

    # Leak-class regression watch: RSS beyond the trusted post-arena-off
    # plateau ceiling. Null/absent keys are normal (lazy diar-pool spawn,
    # older edge builds) — never a breach. Only upgrades ok -> degraded;
    # a dead bridge stays "down" (but the reason is still appended).
    for key, ceiling, label in (
        ("rss_total_mb", _RSS_TOTAL_ALERT_MB, "total"),
        ("rss_diar_child_mb", _RSS_DIAR_CHILD_ALERT_MB, "diar child"),
    ):
        value = data.get(key)
        if isinstance(value, (int, float)) and value > ceiling:
            reasons.append(
                f"{label} RSS {value:.0f} MB exceeds the {ceiling:.0f} MB "
                "plateau ceiling — possible leak regression",
            )
            if status == "ok":
                status = "degraded"

    if status == "ok":
        reasons.append("healthy")
    return AmbientVerdict(status, reasons)


async def bridge_snapshot(*, now: datetime | None = None) -> dict:
    """One-call data contract for the dashboard voice Bridge tab.

    Composes the module's own pieces — config load, SSH read, verdict — and
    returns the FULL edge health payload under a ``health`` sub-key (no
    filtering, so future edge keys surface without a Genesis-side change).
    Deliberately separate from ``probe_ambient_health``: that shared
    infrastructure card carries only the verdict and sits behind snapshot +
    SSH-TTL caches; the cockpit tab wants the whole payload, fresh, on demand.

    Never raises. ``now`` is a test seam for verdict staleness (defaults to
    real time in :func:`evaluate_ambient_health`).
    """
    try:
        cfg = load_ambient_remote_config()
    except AmbientRemoteConfigError as exc:
        return {
            "configured": True,
            "reachable": False,
            "verdict": "misconfigured",
            "reasons": [str(exc)],
        }
    if cfg is None:
        return {"configured": False, "reason": "no ambient edge configured"}

    start = time.monotonic()
    data = await read_edge_health(cfg)  # None on any failure, never raises
    latency_ms = round((time.monotonic() - start) * 1000, 2)
    verdict = evaluate_ambient_health(data, now=now)
    out = {
        "configured": True,
        "reachable": data is not None,
        "verdict": verdict.status,
        "reasons": verdict.reasons,
        "latency_ms": latency_ms,
    }
    if data is not None:
        out["health"] = data
    return out
