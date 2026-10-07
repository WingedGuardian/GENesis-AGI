"""Stateless typed systemd contract reader for the managed query unit."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path


def bus_call(path: str, interface: str, method: str, *args: str) -> dict:
    result = subprocess.run(
        ["/usr/bin/busctl", "--user", "--json=short", "call", "org.freedesktop.systemd1",
         path, interface, method, *args],
        capture_output=True, text=True, check=True, timeout=30,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise ValueError("invalid typed manager response")
    return value


def loaded_properties(unit: str, interface: str) -> dict:
    value = bus_call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                     "LoadUnit", "s", unit)
    data = value["data"]
    if (value["type"] != "o" or not isinstance(data, list) or len(data) != 1
            or not isinstance(data[0], str)
            or not re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", data[0])):
        raise ValueError("invalid loaded unit object path")
    value = bus_call(data[0], "org.freedesktop.DBus.Properties", "GetAll", "s",
                     "org.freedesktop.systemd1." + interface)
    if (value["type"] != "a{sv}" or not isinstance(value["data"], list)
            or len(value["data"]) != 1 or not isinstance(value["data"][0], dict)):
        raise ValueError("invalid loaded unit properties")
    return value["data"][0]


def typed_property(properties: dict, name: str, signature: str):
    value = properties.get(name)
    if (not isinstance(value, dict) or set(value) != {"type", "data"}
            or value["type"] != signature):
        raise ValueError(f"invalid loaded {name}")
    result = value["data"]
    if signature == "s" and not isinstance(result, str):
        raise ValueError(f"invalid loaded {name}")
    if signature in ("t", "i"):
        low, high = (0, 2**64 - 1) if signature == "t" else (-2**31, 2**31 - 1)
        if type(result) is not int or not low <= result <= high:
            raise ValueError(f"invalid loaded {name}")
    return result


def validate_backend(config: dict, backend: str, validate_absolute) -> None:
    unit = loaded_properties(backend, "Unit")
    if (typed_property(unit, "LoadState", "s") != "loaded"
            or typed_property(unit, "Id", "s") != backend):
        raise ValueError("managed backend is not the loaded installed unit")
    properties = loaded_properties(backend, "Service")
    expected = {"Type": ("s", "exec"), "WorkingDirectory": ("s", "/"),
                "MemoryMax": ("t", 2 * 1024**3), "MemorySwapMax": ("t", 0),
                "OOMScoreAdjust": ("i", 500), "KillMode": ("s", "control-group"),
                "Restart": ("s", "no")}
    for name, (signature, required) in expected.items():
        if typed_property(properties, name, signature) != required:
            raise ValueError(f"loaded managed backend has incompatible {name}")
    for name, low, high in (("TasksMax", 0, 128), ("CPUQuotaPerSecUSec", 1, 2000000),
                            ("TimeoutStartUSec", 1, 120000000),
                            ("TimeoutStopUSec", 1, 30000000)):
        if not low <= typed_property(properties, name, "t") <= high:
            raise ValueError(f"loaded managed backend has incompatible {name}")
    # VENV_PATH is the installer's explicit selection. Keep its literal spelling:
    # resolving bin/python would discard the environment's identity.
    venv = os.environ.get("VENV_PATH") or config["main"] + "/.venv"
    validate_absolute(venv)
    argv = ["/bin/sh", "-c", 'exec "$@"', "--", venv + "/bin/python", "-I",
            config["main"] + "/scripts/codebase_managed.py", "--config",
            str(Path.home() / ".genesis/config/codebase-managed.json")]
    for name, role in (("ExecStartEx", "serve"), ("ExecStartPostEx", "ready"),
                       ("ExecConditionEx", None), ("ExecStartPreEx", None),
                       ("ExecReloadEx", None), ("ExecStopEx", None), ("ExecStopPostEx", None)):
        commands = typed_property(properties, name, "a(sasasttttuii)")
        if not isinstance(commands, list) or len(commands) != (1 if role else 0):
            raise ValueError(f"loaded managed backend has incompatible {name}")
        if role:
            record = commands[0]
            if (not isinstance(record, list) or len(record) != 10
                    or record[:3] != ["/bin/sh", argv + [role], ["no-env-expand"]]
                    or any(type(item) is not int for item in record[3:])
                    or any(not 0 <= item < 2**64 for item in record[3:7])
                    or not 0 <= record[7] < 2**32
                    or any(not -2**31 <= item < 2**31 for item in record[8:])):
                raise ValueError(f"loaded managed backend has incompatible {name}")
