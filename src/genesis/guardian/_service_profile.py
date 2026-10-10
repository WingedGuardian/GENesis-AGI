"""Read-only proof of the supported Guardian user-service migration profile."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_BUS = "org.freedesktop.systemd1"
_MANAGER = "/org/freedesktop/systemd1"
_DEPLOYMENT = Path(__file__).resolve().parents[3]
_SELECTORS = {
    "GUARDIAN_CONFIG", "GUARDIAN_HEALTH_HOST", "GUARDIAN_HEALTH_PORT",
    "GUARDIAN_CONTAINER_NAME", "PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE",
}


def _read(signature: str, *arguments: str):
    # Capture privately: even read-only D-Bus errors or properties may contain
    # credentials. Never propagate their raw stdout/stderr into diagnostics.
    try:
        result = subprocess.run(
            ["busctl", "--user", "--json=short", *arguments],
            capture_output=True, text=True, timeout=10, check=True,
        )
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError
        payload = json.loads(result.stdout)
        if payload["type"] != signature:
            raise ValueError
        return payload["data"]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError, RecursionError):
        raise ValueError("Guardian service profile could not be read") from None


def _properties(path: str, interface: str) -> dict:
    data = _read(
        "a{sv}", "call", _BUS, path, "org.freedesktop.DBus.Properties",
        "GetAll", "s", f"{_BUS}.{interface}",
    )
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("Guardian service properties are unsupported")
    return data[0]


def _property(properties: dict, name: str, signature: str, typ: type):
    value = properties.get(name)
    if not isinstance(value, dict) or value.get("type") != signature or type(value.get("data")) is not typ:
        raise ValueError(f"Guardian service property {name} is unsupported")
    return value["data"]


def _environment(manager: list, service: list, unset: list) -> dict[str, str]:
    selected = {}
    for assignment in (*manager, *service):
        if not isinstance(assignment, str) or "=" not in assignment:
            raise ValueError("Guardian service environment is unsupported")
        name, value = assignment.split("=", 1)
        if name in _SELECTORS:
            selected[name] = value
    for assignment in unset:
        if not isinstance(assignment, str):
            raise ValueError("Guardian service final environment removal is unsupported")
        name, separator, value = assignment.partition("=")
        if name in selected and (not separator or selected[name] == value):
            del selected[name]
    return selected


def _prove_unit(unit: dict) -> None:
    for name, signature, typ, expected in (
        ("Id", "s", str, "genesis-guardian.service"),
        ("LoadState", "s", str, "loaded"),
        ("Transient", "b", bool, False),
        ("NeedDaemonReload", "b", bool, False),
        ("JoinsNamespaceOf", "as", list, []),
    ):
        if _property(unit, name, signature, typ) != expected:
            raise ValueError(f"Guardian service {name} requires operator inspection")


def _prove_service(service: dict) -> None:
    if _property(service, "Type", "s", str) != "oneshot":
        raise ValueError("Guardian service type is unsupported")
    if _property(service, "ProtectHome", "s", str) != "no":
        raise ValueError("Guardian service ProtectHome requires operator inspection")
    # This is a supported-profile check, not an interpreter for arbitrary
    # wrappers, environment files, PAM, or filesystem/network remapping.
    for name, signature, typ in (
        ("EnvironmentFiles", "a(sb)", list), ("PAMName", "s", str),
        ("User", "s", str), ("Group", "s", str),
        ("RootDirectory", "s", str), ("RootImage", "s", str),
        ("BindPaths", "a(ssbt)", list), ("BindReadOnlyPaths", "a(ssbt)", list),
        ("TemporaryFileSystem", "a(ss)", list), ("MountImages", "a(ssba(ss))", list),
        ("ExtensionImages", "a(sba(ss))", list), ("ExtensionDirectories", "as", list),
        ("InaccessiblePaths", "as", list), ("ReadOnlyPaths", "as", list), ("ReadWritePaths", "as", list),
        ("NoExecPaths", "as", list), ("ExecPaths", "as", list), ("PrivateTmp", "b", bool),
        ("PrivateNetwork", "b", bool), ("NetworkNamespacePath", "s", str),
        ("PrivateMounts", "b", bool), ("PrivateUsers", "b", bool), ("DynamicUser", "b", bool),
        ("ExecStartPreEx", "a(sasasttttuii)", list),
        ("ExecConditionEx", "a(sasasttttuii)", list), ("ExecStartPostEx", "a(sasasttttuii)", list),
        ("ExecStopEx", "a(sasasttttuii)", list), ("ExecStopPostEx", "a(sasasttttuii)", list),
    ):
        if _property(service, name, signature, typ):
            raise ValueError(f"Guardian service {name} requires operator inspection")
    commands = _property(service, "ExecStartEx", "a(sasasttttuii)", list)
    interpreter = str(_DEPLOYMENT / ".venv/bin/python")
    if len(commands) != 1 or not isinstance(commands[0], list) or len(commands[0]) != 10:
        raise ValueError("Guardian service command is unsupported")
    executable, arguments, flags = commands[0][:3]
    if executable != interpreter or arguments != [interpreter, "-m", "genesis.guardian"] or flags != []:
        raise ValueError("Guardian service must use the deployed direct interpreter")
    if Path(sys.executable).absolute() != Path(interpreter):
        raise ValueError("run migration with the deployed Guardian interpreter")
    if _property(service, "WorkingDirectory", "s", str) != str(_DEPLOYMENT):
        raise ValueError("Guardian service working directory is unsupported")
    if (_DEPLOYMENT / "genesis.py").exists() or (_DEPLOYMENT / "genesis").exists():
        raise ValueError("Guardian working directory shadows the deployed package")


def _prove_target(effective: dict, config_path: Path, container: str) -> None:
    if effective.get("PYTHONPATH") != str(_DEPLOYMENT / "src") or effective.get("PYTHONHOME") or effective.get("PYTHONUSERBASE"):
        raise ValueError("Guardian service Python import environment is unsupported")
    selected = effective.get("GUARDIAN_CONFIG")
    try:
        matches = bool(selected) and Path(selected).is_absolute() and Path(selected).resolve() == config_path.resolve()
    except (OSError, ValueError, RuntimeError):
        raise ValueError("Guardian service configuration selection is unsupported") from None
    if not matches:
        raise ValueError("Guardian service selects a different configuration")
    for name, expected in (("GUARDIAN_HEALTH_HOST", "127.0.0.1"), ("GUARDIAN_CONTAINER_NAME", container)):
        if name in effective and effective[name] != expected:
            raise ValueError(f"Guardian service {name} conflicts with loopback migration")
    try:
        port = int(effective.get("GUARDIAN_HEALTH_PORT", "5000"))
    except ValueError:
        raise ValueError("Guardian service HTTP port is invalid") from None
    if port != 5000:
        raise ValueError("Guardian service HTTP port conflicts with loopback migration")


def prove_guardian_profile(config_path: Path, container: str) -> None:
    """Refuse unresolved future readers before changing config or ingress."""
    lookup = _read("o", "call", _BUS, _MANAGER, f"{_BUS}.Manager", "GetUnit", "s", "genesis-guardian.service")
    if not isinstance(lookup, list) or len(lookup) != 1 or not isinstance(lookup[0], str):
        raise ValueError("Guardian loaded service identity is unsupported")
    _prove_unit(_properties(lookup[0], "Unit"))
    service = _properties(lookup[0], "Service")
    _prove_service(service)
    manager = _read("as", "get-property", _BUS, _MANAGER, f"{_BUS}.Manager", "Environment")
    if not isinstance(manager, list):
        raise ValueError("Guardian manager environment is unsupported")
    effective = _environment(
        manager, _property(service, "Environment", "as", list),
        _property(service, "UnsetEnvironment", "as", list),
    )
    _prove_target(effective, config_path, container)
