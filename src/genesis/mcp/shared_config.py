"""Stdlib-only shared-MCP configuration, also captured by the deploy helper.

This reader preserves the rollout switch's shallow per-key merge. It does not
validate service startup, provision credentials, or activate a rollout stage.
"""

import json
from pathlib import Path

CONFIG_NAME = "mcp_shared.json"


class SharedConfigError(Exception):
    """The shared-MCP configuration cannot be read as written."""


def overlay_path(env):
    home = env.get("GENESIS_HOME") or str(Path(env.get("HOME") or Path.home()) / ".genesis")
    path = Path(home).expanduser()
    if not path.is_absolute():
        raise SharedConfigError(f"GENESIS_HOME must be an absolute path (got {home!r})")
    return path / "config" / CONFIG_NAME


def _read(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SharedConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SharedConfigError(f"{path}: expected a JSON object")
    return data


def load_config(root, env):
    """Tracked defaults under <root>/config, with the install overlay merged per key."""
    config = {"stage": "off", "adapter": {"argv": []}, "servers": {}}
    for layer in (_read(Path(root) / "config" / CONFIG_NAME), _read(overlay_path(env))):
        if not layer:
            continue
        if "stage" in layer:
            config["stage"] = layer["stage"]
        if isinstance(layer.get("adapter"), dict):
            config["adapter"] = {**config["adapter"], **layer["adapter"]}
        for name, spec in (layer.get("servers") or {}).items():
            if not isinstance(spec, dict):
                raise SharedConfigError(f"servers.{name}: expected an object")
            config["servers"][name] = {**config["servers"].get(name, {}), **spec}
    return config
