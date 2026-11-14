"""Shared-only snapshot, consuming the complete existing rollout program."""

import importlib.util
import math
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from genesis.mcp.shared_http import SharedHTTPConfig

LOCAL_FRONTEND_STAGES = (
    ("health", "health-reads"),
    ("memory", "memory-floor"),
    ("memory", "health-reads"),
)


@dataclass(frozen=True)
class SharedServiceSettings:
    http: SharedHTTPConfig
    unit: str
    db_background_units: int
    db_background_jobs: int
    db_checkout_timeout_s: float
    shutdown_grace_s: float


@dataclass(frozen=True)
class LocalSelection:
    settings: SharedServiceSettings
    stage: str


def _seconds(spec, name, *, allow_zero=False):
    value = spec[name]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero)):
        raise ValueError
    return float(value)


@contextmanager
def _rollout_program(root):
    """Execute this checkout's complete trusted source, never a timestamp pyc."""
    module_name = None
    try:
        try:
            path = Path(root) / "scripts/lib/mcp_rollout.py"
            source = path.read_bytes()
            module_name = "_genesis_shared_rollout_" + uuid4().hex
            module_spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(module_spec)
            sys.modules[module_name] = module
            exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
        except Exception:
            raise ValueError("Invalid shared MCP configuration: rollout source") from None
        yield module
    finally:
        if module_name is not None:
            sys.modules.pop(module_name, None)


def _service_settings(config, service, valid_unit):
    field = "service"
    try:
        field = "service"
        spec = config["servers"][service]
        field = "unit"
        unit = spec["unit"]
        if valid_unit(unit) is not True:
            raise ValueError
        field = "HTTP limits"
        http = SharedHTTPConfig(
            service=service, url=spec["url"], db_units=spec["db_units"],
            tool_concurrency=spec["tool_concurrency"], max_sessions=spec["max_sessions"],
            session_idle_timeout_s=spec["session_idle_timeout_s"], max_body_bytes=spec["max_body_bytes"],
        )
        background = {}
        for field in ("db_background_units", "db_background_jobs"):
            value = spec[field]
            if type(value) is not int or value < 1:
                raise ValueError
            background[field] = value
        field = "db_checkout_timeout_s"
        checkout = _seconds(spec, field)
        field = "shutdown_grace_s"
        grace = _seconds(spec, field, allow_zero=True)
        return SharedServiceSettings(http=http, unit=unit, **background,
                                     db_checkout_timeout_s=checkout, shutdown_grace_s=grace)
    except Exception:
        raise ValueError(f"Invalid shared MCP configuration: {field}") from None


def load_shared_settings(root, service, env):
    """Freeze the existing service program without retaining caller state."""
    with _rollout_program(root) as module:
        try:
            config = module.load_config(Path(root), env)
        except Exception:
            raise ValueError("Invalid shared MCP configuration: rollout source") from None
        return _service_settings(config, service, module.valid_unit)


def load_local_settings(root, service, env, stdio, *, ready=None):
    """Re-decide an ordinary spawn; no inherited handoff state or ABI."""
    if service not in ("health", "memory") or env.get("GENESIS_CALLER_PROFILE"):
        return None
    try:
        with _rollout_program(root) as module:
            config = module.load_config(Path(root), env)
            ceiling = module.MAX_STAGE
            decision = module.decide(
                stdio, config=config, env=env, ready=module.unit_ready if ready is None else ready,
                ceiling=ceiling, python=stdio[0], root=Path(root),
            )
            if not decision.shared or decision.argv != list(stdio):
                return None
            _requested, stage = module.stage_selection(config, env, ceiling)
            if (service, stage) not in LOCAL_FRONTEND_STAGES:
                return None
            settings = _service_settings(config, service, module.valid_unit)
            return LocalSelection(settings=settings, stage=stage)
    except Exception:
        raise ValueError("Invalid local MCP configuration") from None
