"""Shared-only snapshot, consuming the complete existing rollout program."""

import importlib.util
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from genesis.mcp.shared_http import SharedHTTPConfig


@dataclass(frozen=True)
class SharedServiceSettings:
    http: SharedHTTPConfig
    unit: str
    db_background_units: int
    db_background_jobs: int
    db_checkout_timeout_s: float
    shutdown_grace_s: float


def _seconds(spec, name, *, allow_zero=False):
    value = spec[name]
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero)):
        raise ValueError
    return float(value)


def load_shared_settings(root, service, env):
    """Read current trusted source bytes; never invoke CLI or package loaders.

    Every returned field is a generation snapshot. No caller/credential state is
    retained and legacy rollout loading/merging semantics are unchanged.
    """
    module_name, field = None, "rollout source"
    try:
        root = Path(root)
        path = root / "scripts/lib/mcp_rollout.py"
        source = path.read_bytes()
        module_name = "_genesis_shared_rollout_" + uuid4().hex
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        # Execute this checkout's trusted CLI source, never request/config text.
        exec(compile(source, str(path), "exec"), module.__dict__)  # noqa: S102
        config = module.load_config(root, env)
        field = "service"
        spec = config["servers"][service]
        field = "unit"
        unit = spec["unit"]
        if module.valid_unit(unit) is not True:
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
    finally:
        if module_name is not None:
            sys.modules.pop(module_name, None)
