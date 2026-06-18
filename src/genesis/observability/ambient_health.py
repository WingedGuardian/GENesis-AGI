"""Ambient-capture health monitor — read the edge bridge's health file + evaluate.

The ambient bridge runs on a separate edge VM and writes ``ambient_health.json``
every 60s. This module SSH-reads that file (connection in
``~/.genesis/ambient_remote.yaml``, mirroring ``guardian_remote.yaml``) and turns
a snapshot into an alert verdict. ``OutreachScheduler._ambient_health_job`` runs
the read + evaluate on a cadence and alerts the user on a bad-state transition.

No config file -> the monitor is a silent no-op, so installs without an ambient
edge are unaffected. The edge host/IP lives ONLY in the install-local config,
never in committed source.
"""
from __future__ import annotations

import asyncio
import json
import logging
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


@dataclass(frozen=True)
class AmbientRemoteConfig:
    host_ip: str
    host_user: str
    ssh_key: str = _DEFAULT_KEY
    health_path: str = _DEFAULT_HEALTH_PATH


def load_ambient_remote_config() -> AmbientRemoteConfig | None:
    """Load ``~/.genesis/ambient_remote.yaml``; return None if absent, disabled,
    or invalid (monitor then no-ops). Never raises."""
    if not _CONFIG_PATH.exists():
        return None
    try:
        import yaml

        data = yaml.safe_load(_CONFIG_PATH.read_text()) or {}
        if not data.get("enabled", True):
            return None
        host_ip = data.get("host_ip")
        host_user = data.get("host_user")
        if not host_ip or not host_user:
            logger.warning("ambient_remote.yaml missing host_ip/host_user — monitor disabled")
            return None
        return AmbientRemoteConfig(
            host_ip=str(host_ip),
            host_user=str(host_user),
            ssh_key=str(data.get("ssh_key", _DEFAULT_KEY)),
            health_path=str(data.get("health_path", _DEFAULT_HEALTH_PATH)),
        )
    except Exception:
        logger.warning("Failed to load ambient_remote.yaml — monitor disabled", exc_info=True)
        return None


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

    - ``data is None`` (read failed / edge unreachable) -> ``unknown`` (transient;
      the caller should not flip alert state on a single unknown).
    - heartbeat ``ts`` missing or older than ``_HEARTBEAT_STALE_S`` -> ``down``
      (bridge process dead/hung).
    - ``active_connections == 0`` -> ``down`` (device disconnected; ``0`` is a
      reliable negative).
    - ``diar_worker_alive`` False while ``diar_enabled`` -> ``degraded``.
    - otherwise -> ``ok``.

    Deliberately does NOT consider ``last_ts`` (last captured utterance): a quiet
    room naturally has an old ``last_ts``, which is not a fault.
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

    if data.get("active_connections", 0) == 0:
        reasons.append("device not connected (active_connections=0)")
        status = "down"

    if data.get("diar_enabled") and not data.get("diar_worker_alive", False):
        reasons.append("diarization worker not alive")
        if status == "ok":
            status = "degraded"

    if status == "ok":
        reasons.append("healthy")
    return AmbientVerdict(status, reasons)
