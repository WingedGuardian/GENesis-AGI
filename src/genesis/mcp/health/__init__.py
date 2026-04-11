"""health-mcp package — system health monitoring.

Provides health_status, health_errors, health_alerts, and related tools
backed by the shared HealthDataService.

Implementation logic is in _impl_* functions (testable without FastMCP).

Module state is defined at the top so that submodules can access it via
``from genesis.mcp.health_mcp import _service`` inside implementation
functions (late import pattern).  Submodule imports are intentionally
placed after state definitions to avoid circular import deadlocks; the
``# noqa: E402`` comments suppress the lint warning for this deliberate
pattern.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastmcp import FastMCP

if TYPE_CHECKING:
    from genesis.observability.health_data import HealthDataService

logger = logging.getLogger(__name__)

mcp = FastMCP("genesis-health")

_service: HealthDataService | None = None
_event_bus: object | None = None
_activity_tracker: object | None = None
_job_retry_registry: object | None = None

_alert_history: dict[str, str] = {}


def init_health_mcp(
    service: HealthDataService,
    *,
    event_bus: object | None = None,
    activity_tracker: object | None = None,
    job_retry_registry: object | None = None,
) -> None:
    global _service, _event_bus, _activity_tracker, _job_retry_registry
    _service = service
    _event_bus = event_bus
    _activity_tracker = activity_tracker
    _job_retry_registry = job_retry_registry

    if activity_tracker is not None:
        from genesis.observability.mcp_middleware import InstrumentationMiddleware

        mcp.add_middleware(InstrumentationMiddleware(activity_tracker, "health"))

    logger.info("Health MCP wired to HealthDataService")


from genesis.mcp.health import browser as _browser  # noqa: E402
from genesis.mcp.health import db_schema as _db_schema  # noqa: E402
from genesis.mcp.health import errors as _errors  # noqa: E402
from genesis.mcp.health import manifest as _manifest  # noqa: E402
from genesis.mcp.health import module_ops as _module_ops  # noqa: E402
from genesis.mcp.health import provider as _provider  # noqa: E402
from genesis.mcp.health import session_control as _session_control  # noqa: E402
from genesis.mcp.health import settings as _settings  # noqa: E402
from genesis.mcp.health import status as _status  # noqa: E402
from genesis.mcp.health import task_tools as _task_tools  # noqa: E402
from genesis.mcp.health import update_history as _update_history  # noqa: E402

db_schema = _db_schema
errors = _errors
manifest = _manifest
provider = _provider
session_control = _session_control
settings = _settings
status = _status
task_tools = _task_tools
update_history = _update_history

_impl_db_schema = _db_schema._impl_db_schema
_impl_health_errors = _errors._impl_health_errors
_impl_health_alerts = _errors._impl_health_alerts
_impl_bootstrap_manifest = _manifest._impl_bootstrap_manifest
_impl_subsystem_heartbeats = _manifest._impl_subsystem_heartbeats
_impl_job_health = _manifest._impl_job_health
_impl_session_set_model = _session_control._impl_session_set_model
_impl_session_set_effort = _session_control._impl_session_set_effort
_impl_settings_list = _settings._impl_settings_list
_impl_settings_get = _settings._impl_settings_get
_impl_settings_update = _settings._impl_settings_update
_impl_health_status = _status._impl_health_status
_impl_task_submit = _task_tools._impl_task_submit
_impl_task_list = _task_tools._impl_task_list
_impl_task_detail = _task_tools._impl_task_detail
_impl_task_pause = _task_tools._impl_task_pause
_impl_task_resume = _task_tools._impl_task_resume
_impl_task_cancel = _task_tools._impl_task_cancel
_impl_module_call = _module_ops._impl_module_call
_impl_module_list = _module_ops._impl_module_list
_impl_browser_navigate = _browser._impl_browser_navigate
_impl_browser_click = _browser._impl_browser_click
_impl_browser_fill = _browser._impl_browser_fill
_impl_browser_screenshot = _browser._impl_browser_screenshot
_impl_browser_snapshot = _browser._impl_browser_snapshot
_impl_browser_run_js = _browser._impl_browser_run_js
_impl_browser_sessions = _browser._impl_browser_sessions
_impl_browser_clear_domain = _browser._impl_browser_clear_domain
_impl_update_history_recent = _update_history._impl_update_history_recent

# Re-export init function for runtime wiring
init_task_tools = _task_tools.init_task_tools

__all__ = [
    "mcp",
    "init_health_mcp",
    "init_task_tools",
    "_service",
    "_event_bus",
    "_activity_tracker",
    "_job_retry_registry",
    "_alert_history",
    "_impl_db_schema",
    "_impl_health_errors",
    "_impl_health_alerts",
    "_impl_bootstrap_manifest",
    "_impl_subsystem_heartbeats",
    "_impl_job_health",
    "_impl_session_set_model",
    "_impl_session_set_effort",
    "_impl_settings_list",
    "_impl_settings_get",
    "_impl_settings_update",
    "_impl_health_status",
    "_impl_task_submit",
    "_impl_task_list",
    "_impl_task_detail",
    "_impl_task_pause",
    "_impl_task_resume",
    "_impl_task_cancel",
    "db_schema",
    "errors",
    "manifest",
    "provider",
    "session_control",
    "settings",
    "status",
    "task_tools",
    "update_history",
    "_impl_update_history_recent",
    "_browser",
    "_impl_browser_navigate",
    "_impl_browser_click",
    "_impl_browser_fill",
    "_impl_browser_screenshot",
    "_impl_browser_snapshot",
    "_impl_browser_run_js",
    "_impl_browser_sessions",
    "_impl_browser_clear_domain",
]
