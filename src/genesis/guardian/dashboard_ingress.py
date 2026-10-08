"""Operator migration of the host dashboard proxy to loopback.

Run on the host through its deployed Guardian interpreter. This changes no
Flask binding, creates no listener, and leaves peer/SAM admission disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

from genesis.guardian.config import GuardianConfig

_LISTEN = "tcp:127.0.0.1:5000"
_CONNECT = "tcp:127.0.0.1:5000"


def _device(container: str, *arguments: str) -> str:
    return subprocess.run(
        ["incus", "config", "device", *arguments[:1], container, "dashboard-proxy", *arguments[1:]],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _ready_loopback() -> None:
    # Operator preflight, not a request originating from a peer. Do not inherit
    # an HTTP proxy: the address we are proving must be the host's own socket.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for path in ("health", "auth/status"):
        with opener.open(f"http://127.0.0.1:5000/api/genesis/{path}", timeout=10) as response:
            if response.status != 200:
                raise ValueError("dashboard loopback preflight is not ready")
            if path == "auth/status":
                body = response.read(4097)
                if len(body) > 4096:
                    raise ValueError("dashboard preflight response is oversized")
                payload = json.loads(body)
                if not isinstance(payload, dict) or payload.get("enabled") is not True:
                    raise ValueError("configure dashboard authentication before migrating ingress")


def _patch_host(original: bytes, raw: dict) -> str:
    """Use YAML source marks to preserve operator comments and unrelated fields."""
    text = original.decode("utf-8")
    root = yaml.compose(text)
    if not isinstance(root, yaml.MappingNode) or root.flow_style or root.start_mark.column:
        raise ValueError("Guardian configuration must use a top-level block mapping")
    fields = [(key, value) for key, value in root.value if key.value == "health_api_host"]
    if len(fields) > 1:
        raise ValueError("duplicate Guardian HTTP host fields require operator inspection")
    if fields:
        key, value = fields[0]
        if not isinstance(value, yaml.ScalarNode) or value.start_mark.index < key.end_mark.index:
            raise ValueError(
                "aliased or structured Guardian HTTP host requires operator inspection"
            )
        replacement = '"127.0.0.1"'
        if text[value.start_mark.index - 1 : value.start_mark.index] == ":":
            replacement = " " + replacement
        patched = text[: value.start_mark.index] + replacement + text[value.end_mark.index :]
    else:
        start = root.start_mark.index
        patched = text[:start] + 'health_api_host: "127.0.0.1"\n' + text[start:]
    if yaml.safe_load(patched) != {**raw, "health_api_host": "127.0.0.1"}:
        raise ValueError("Guardian HTTP host patch would change unrelated configuration")
    return patched


def configure_loopback_health(config_path: Path, *, only_if_unset: bool = False) -> None:
    """Patch one HTTP setting, preserving comments, modes and operator values."""
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("a regular deployed Guardian configuration is required")
    original = config_path.read_bytes()
    raw = yaml.safe_load(original)
    if not isinstance(raw, dict):
        raise ValueError("Guardian configuration must be a mapping")
    if only_if_unset and raw.get("health_api_host"):
        return
    patched = _patch_host(original, raw)
    attributes = config_path.stat()
    mode = stat.S_IMODE(attributes.st_mode)
    fd, name = tempfile.mkstemp(prefix=".guardian-ingress-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            os.fchown(output.fileno(), attributes.st_uid, attributes.st_gid)
            os.fchmod(output.fileno(), mode)
            output.write(patched)
            output.flush()
            os.fsync(output.fileno())
        if config_path.read_bytes() != original:
            raise ValueError("Guardian configuration changed during preflight; retry")
        os.replace(name, config_path)
        directory = os.open(config_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def migrate(config_path: Path, *, apply: bool = False) -> dict:
    """Validate topology; opt-in apply patches preserved config before closing ingress.

    Config-first ordering keeps HTTP health reachable even if the Incus change
    fails: loopback is reachable through both old and new dashboard bindings.
    An uncertain Incus outcome never rolls back to a public listener.
    """
    if config_path.is_symlink() or not config_path.is_file():
        raise ValueError("a regular deployed Guardian configuration is required")
    original = config_path.read_bytes()
    raw = yaml.safe_load(original)
    if not isinstance(raw, dict):
        raise ValueError("Guardian configuration must be a mapping")
    container = raw.get("container_name", "genesis")
    if not isinstance(container, str) or not container:
        raise ValueError("Guardian container name is invalid")
    if raw.get("health_api_port", 5000) != 5000:
        raise ValueError("custom health ports require a separate topology migration")
    try:
        port = int(os.environ.get("GUARDIAN_HEALTH_PORT", "5000"))
    except ValueError:
        raise ValueError("invalid Guardian HTTP port environment override") from None
    if port != 5000:
        raise ValueError("remove conflicting Guardian HTTP port environment override")
    if os.environ.get("GUARDIAN_CONTAINER_NAME", container) != container:
        raise ValueError("remove conflicting Guardian container environment override")
    if os.environ.get("GUARDIAN_HEALTH_HOST", "127.0.0.1") != "127.0.0.1":
        raise ValueError("remove conflicting Guardian HTTP host environment override")
    # Refuse unrelated proxy topologies rather than guessing what to replace.
    if _device(container, "get", "connect") != _CONNECT:
        raise ValueError("dashboard proxy does not connect to container loopback")
    listener = _device(container, "get", "listen")
    if listener not in {_LISTEN, "tcp:0.0.0.0:5000"}:
        raise ValueError("dashboard proxy listener requires operator inspection")
    _ready_loopback()
    result = {
        "container": container,
        "listen": _LISTEN,
        "health_api_host": "127.0.0.1",
        "applied": False,
    }
    if not apply:
        return result
    # Check the deployed class as well as the YAML representation: old Guardian
    # code must not silently ignore the override after ingress is closed.
    if GuardianConfig(health_api_host="127.0.0.1").health_url != "http://127.0.0.1:5000":
        raise ValueError("deployed Guardian does not support the HTTP host override")
    if config_path.read_bytes() != original:
        raise ValueError("Guardian configuration changed during preflight; retry")
    configure_loopback_health(config_path)
    if listener != _LISTEN:
        _device(container, "set", "listen", _LISTEN)
    if _device(container, "get", "listen") != _LISTEN:
        raise ValueError("dashboard proxy loopback change was not confirmed")
    _ready_loopback()
    result["applied"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="patch Guardian config and close host ingress"
    )
    arguments = parser.parse_args()
    try:
        print(json.dumps(migrate(arguments.config, apply=arguments.apply)))
    except subprocess.CalledProcessError as error:
        # These commands contain only proxy settings, never credentials. Preserve
        # the real command failure for the operator instead of reporting success.
        print(f"Incus exited {error.returncode}: {error.stderr}", file=sys.stderr)
        return 1
    except yaml.YAMLError:
        print("Ingress migration failed: invalid Guardian YAML", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Ingress migration failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
