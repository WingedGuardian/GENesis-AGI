"""ExternalProgramAdapter — wraps any external program as a Genesis module."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from genesis.modules.external.config import ProgramConfig
from genesis.modules.external.ipc import create_ipc_adapter

_HEALTH_CACHE_TTL_S = 60  # seconds

logger = logging.getLogger(__name__)


class ExternalProgramAdapter:
    """Wraps an external program as a Genesis CapabilityModule.

    Genesis is the nervous system; the external program is the body.
    This adapter provides:
    - Health monitoring (periodic health checks)
    - Lifecycle awareness (knows how to restart via configured commands)
    - Unified interface (shows in dashboard, enable/disable toggle)
    - Optional pipeline integration (if research_profile is set)

    The adapter does NOT manage the program's internal scheduling, LLM routing,
    or data stores. Those remain the program's responsibility.
    """

    def __init__(self, config: ProgramConfig) -> None:
        self._config = config
        self._enabled = config.enabled
        self._ipc = create_ipc_adapter(config.ipc)
        self._runtime: Any = None
        self._healthy: bool = False
        self._last_health_error: str | None = None
        self._last_health_check_at: datetime | None = None

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def healthy(self) -> bool:
        """Whether the last health check passed."""
        return self._healthy

    @property
    def last_health_error(self) -> str | None:
        return self._last_health_error

    async def check_health_cached(self) -> bool:
        """Run a live health check with TTL caching."""
        now = datetime.now(UTC)
        if (
            self._last_health_check_at is not None
            and (now - self._last_health_check_at).total_seconds() < _HEALTH_CACHE_TTL_S
        ):
            return self._healthy

        if not self._config.health_check:
            self._healthy = True
            self._last_health_error = None
            self._last_health_check_at = now
            return True

        try:
            self._healthy = await self._ipc.health_check(
                self._config.health_check.endpoint,
                self._config.health_check.expected_status,
            )
            self._last_health_error = None if self._healthy else "Health check returned unhealthy"
        except Exception as exc:
            self._healthy = False
            self._last_health_error = str(exc)

        self._last_health_check_at = now
        return self._healthy

    @property
    def config(self) -> ProgramConfig:
        return self._config

    @property
    def ipc(self):
        return self._ipc

    async def register(self, runtime: Any) -> None:
        """Start IPC connection and verify health."""
        self._runtime = runtime
        try:
            await self._ipc.start()
            if self._config.health_check:
                self._healthy = await self._ipc.health_check(
                    self._config.health_check.endpoint,
                    self._config.health_check.expected_status,
                )
                self._last_health_check_at = datetime.now(UTC)
                if self._healthy:
                    logger.info("External module '%s' registered and healthy", self.name)
                else:
                    logger.warning("External module '%s' registered but health check failed", self.name)
            else:
                self._healthy = True
                self._last_health_check_at = datetime.now(UTC)
                logger.info("External module '%s' registered (no health check configured)", self.name)
        except Exception as exc:
            self._healthy = False
            self._last_health_error = str(exc)
            logger.error("External module '%s' failed to register: %s", self.name, exc, exc_info=True)

    async def deregister(self) -> None:
        """Stop IPC connection."""
        try:
            await self._ipc.stop()
        except Exception:
            logger.warning("Error stopping IPC for '%s'", self.name, exc_info=True)
        self._healthy = False
        logger.info("External module '%s' deregistered", self.name)

    def get_research_profile_name(self) -> str | None:
        return self._config.research_profile

    async def handle_opportunity(self, opportunity: dict) -> dict | None:
        """Route an opportunity to the external program via IPC.

        The program's API decides how to handle it. If no specific endpoint
        is configured, this is a no-op.
        """
        if not self._healthy:
            logger.debug("Skipping opportunity for unhealthy module '%s'", self.name)
            return None
        try:
            result = await self._ipc.send(
                "/api/opportunity",
                data=opportunity,
                method="POST",
            )
            if result.get("error"):
                return None
            return result
        except Exception:
            logger.warning("handle_opportunity failed for '%s'", self.name, exc_info=True)
            return None

    async def record_outcome(self, outcome: dict) -> None:
        """Forward outcome to the external program if it has an outcome endpoint."""
        if not self._healthy:
            return
        try:
            await self._ipc.send("/api/outcome", data=outcome, method="POST")
        except Exception:
            logger.error("record_outcome failed for '%s'", self.name, exc_info=True)

    async def extract_generalizable(self, outcome: dict) -> list[dict] | None:
        """External programs don't generalize to Genesis core by default."""
        return None

    def configurable_fields(self) -> list[dict[str, Any]]:
        """Return user-editable fields from config, with live values."""
        result = []
        for f in self._config.config_fields:
            live_value = self._config.configurable.get(f.name, f.default)
            result.append(f.to_dict(value=live_value))
        return result

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Update configurable fields."""
        for key, value in updates.items():
            if key in self._config.configurable:
                self._config.configurable[key] = value
        return dict(self._config.configurable)

    def list_operations(self) -> dict[str, dict]:
        """Return the operations manifest from config."""
        return dict(self._config.operations)

    async def execute_operation(self, operation_name: str, params: dict | None = None) -> dict:
        """Execute a named operation from the manifest via IPC.

        Translates manifest entries (method, path, params) to IPC calls.
        HTTP operations substitute path parameters like {id} from params.
        CC/SHELL operations pass params directly to the SSH adapter.
        """
        ops = self._config.operations
        if operation_name not in ops:
            available = list(ops.keys())
            return {"error": f"Unknown operation '{operation_name}'", "available": available}

        if not self._enabled:
            return {"error": f"Module '{self.name}' is disabled"}

        if not self._healthy:
            return {"error": f"Module '{self.name}' is not healthy"}

        op = ops[operation_name]
        method = op.get("method", "GET")
        path = op.get("path", "/")
        request_params = dict(params or {})

        try:
            # SSH-based operations: pass params directly to adapter
            if method.upper() in ("CC", "SHELL"):
                return await self._ipc.send(path, data=request_params or None, method=method)

            # HTTP operations: substitute path parameters like /api/jobs/{id}
            import re
            path_params = re.findall(r"\{(\w+)\}", path)
            for pp in path_params:
                if pp in request_params:
                    path = path.replace(f"{{{pp}}}", str(request_params.pop(pp)))
                else:
                    return {"error": f"Missing required path parameter: {pp}"}

            result = await self._ipc.send(
                path,
                data=request_params if request_params else None,
                method=method,
            )
            return result
        except Exception as exc:
            logger.error(
                "Operation '%s' failed on module '%s': %s",
                operation_name, self.name, exc, exc_info=True,
            )
            return {"error": f"Operation failed: {exc}"}

    async def check_health(self) -> bool:
        """Run a health check and update internal state."""
        if not self._config.health_check:
            return self._healthy
        try:
            self._healthy = await self._ipc.health_check(
                self._config.health_check.endpoint,
                self._config.health_check.expected_status,
            )
            if self._healthy:
                self._last_health_error = None
            else:
                self._last_health_error = self._last_health_error or "health check returned unhealthy"
            return self._healthy
        except Exception as exc:
            self._healthy = False
            self._last_health_error = str(exc)
            return False
