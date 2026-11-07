"""Live readiness reader and validation of the server's periodic sample."""

from datetime import UTC, datetime

from genesis.peers.readiness import unknown


async def sample(runtime):
    reader = getattr(runtime, "_agent_api_readiness", None)
    if reader is None:
        return unknown()
    try:
        return await reader()
    except Exception:
        return unknown("check_unavailable")


def from_status_file(value, services, *, now=None):
    """Only a fresh sample owned by the active primary server is current."""
    result = unknown("invalid_sample", source="status_file")
    result["pid"] = None
    if not isinstance(value, dict):
        return result
    server = services.get("bridge", {})
    if (server.get("service_unit") != "genesis-server.service"
            or server.get("active_state") != "active"
            or type(value.get("pid")) is not int or value["pid"] <= 0
            or str(value["pid"]) != str(server.get("main_pid"))):
        result["reasons"] = ["server_identity_unverified"]
        return result
    try:
        checked = datetime.fromisoformat(value["checked_at"])
        if checked.tzinfo is None:
            raise ValueError
        age = ((now or datetime.now(UTC)) - checked).total_seconds()
        if not 0 <= age <= 90:
            result["reasons"] = ["stale_sample"]
            return result
    except (KeyError, TypeError, ValueError):
        return result
    if (not isinstance(value.get("status"), str)
            or value["status"] not in {"healthy", "disabled", "degraded", "unknown"}
            or not isinstance(value.get("reasons"), list)
            or not all(isinstance(r, str) for r in value["reasons"])
            or type(value.get("runtime_ready")) is not bool
            or type(value.get("task_service_ready")) is not bool):
        return result
    return {key: value[key] for key in
            ("status", "reasons", "checked_at", "pid", "runtime_ready", "task_service_ready")} | {
                "source": "status_file",
            }
