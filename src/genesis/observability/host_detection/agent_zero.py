"""Agent Zero host framework detector.

Uses 2-of-3 signal confirmation to detect whether Genesis is running inside
Agent Zero:

  Signal A: systemd unit ``agent-zero.service`` is active
  Signal B: TCP port 5000 responds (AZ Flask UI)
  Signal C: ``~/agent-zero`` directory exists

If detected, queries full systemd properties for uptime and restart count.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

from genesis.observability.host_detection.types import HostFrameworkStatus
from genesis.observability.service_status import (
    compute_uptime_seconds,
    parse_systemd_timestamp,
    query_systemd_unit,
)

logger = logging.getLogger(__name__)

_AZ_SERVICE_UNIT = "agent-zero.service"
_AZ_PORT = 5000
_AZ_DIR = Path.home() / "agent-zero"
_RESTART_CMD = "systemctl --user restart agent-zero.service"


class AgentZeroDetector:
    """Detect Agent Zero as the host framework."""

    @property
    def name(self) -> str:
        return "Agent Zero"

    @property
    def priority(self) -> int:
        return 10

    def detect(self) -> HostFrameworkStatus:
        """Probe for Agent Zero using 2-of-3 signal confirmation."""
        signals = 0
        details: dict = {}

        # Signal A: systemd unit
        props = query_systemd_unit(_AZ_SERVICE_UNIT)
        systemd_active = props.get("ActiveState") == "active"
        if systemd_active:
            signals += 1
            details["systemd"] = "active"
        elif props:
            details["systemd"] = props.get("ActiveState", "unknown")

        # Signal B: port check
        port_open = _check_port(_AZ_PORT)
        if port_open:
            signals += 1
            details["port"] = _AZ_PORT

        # Signal C: directory exists
        dir_exists = _AZ_DIR.is_dir()
        if dir_exists:
            signals += 1
            details["directory"] = str(_AZ_DIR)

        detected = signals >= 2
        if not detected:
            return HostFrameworkStatus(
                name=self.name,
                detected=False,
                status="unknown",
                details=details,
            )

        # Detected — gather health details
        status = "healthy" if systemd_active else ("degraded" if port_open else "down")

        uptime: float | None = None
        restart_count = 0
        if props:
            start_ts = parse_systemd_timestamp(
                props.get("ExecMainStartTimestamp", "")
            )
            uptime = compute_uptime_seconds(start_ts)
            restart_count = int(props.get("NRestarts", 0))

        details["restart_count"] = restart_count

        return HostFrameworkStatus(
            name=self.name,
            detected=True,
            status=status,
            uptime_seconds=uptime,
            restart_cmd=_RESTART_CMD,
            details=details,
        )


def _check_port(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """Check if a TCP port is open. Fast socket connect, no HTTP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
