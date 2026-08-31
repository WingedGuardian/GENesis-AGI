"""Dashboard heartbeat — emits periodic heartbeats to the event bus.

The web UI runs in the same process as all Genesis subsystems. If the Flask app
degrades (routes broken, event loop stalled) this heartbeat stops, allowing
``subsystem_heartbeats()`` to detect the issue. If the entire process dies,
external monitoring (status.json file age, systemd watchdog) covers the gap —
this heartbeat handles the *degraded-but-alive* case.

The threading, loop capture and health recording live in
``observability.heartbeat_daemon``; only the identity is here.
"""

from __future__ import annotations

from genesis.observability.heartbeat_daemon import HeartbeatDaemon


class DashboardHeartbeat(HeartbeatDaemon):
    """Emits the dashboard liveness pulse while the runtime is bootstrapped."""

    subsystem_name = "DASHBOARD"
    job_name = "dashboard_heartbeat"
    message = "Dashboard web UI alive"
    thread_name = "dashboard-heartbeat"
