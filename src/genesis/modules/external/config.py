"""Configuration dataclasses for external program modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from genesis.modules.config_schema import ConfigField, infer_field_type


@dataclass
class HealthCheckConfig:
    """How to verify the external program is alive."""

    endpoint: str = "/health"
    interval_seconds: int = 60
    timeout_seconds: int = 10
    expected_status: int = 200


@dataclass
class IPCConfig:
    """Inter-process communication configuration."""

    method: Literal["http", "stdio", "ssh"] = "http"
    url: str | None = None
    timeout: int = 30
    # stdio-specific
    command: list[str] = field(default_factory=list)
    working_dir: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    # ssh-specific
    ssh_host: str | None = None  # user@host
    ssh_key: str | None = None  # path to identity file
    ssh_connect_timeout: int = 10
    remote_working_dir: str | None = None  # cd here before running
    remote_claude_path: str = "claude"  # claude CLI path on remote


@dataclass
class LifecycleConfig:
    """Lifecycle management for the external program."""

    ssh_host: str | None = None
    ssh_key: str | None = None
    source_dir: str | None = None
    restart_cmd: str | None = None
    logs_cmd: str | None = None


@dataclass
class EndpointConfig:
    """An endpoint is a MACHINE Genesis operates on, not a service it calls.

    One instance per machine, declared in the install overlay. The fields are
    deliberately per-machine rather than per-fleet: the archetype is cloned by
    copying a YAML, not by subclassing.

    ``machine_id`` is the stable identity and it must NOT be a hostname.
    MEASURED 2026-09-08: two distinct Windows machines were observed reporting
    the SAME ``COMPUTERNAME`` while differing in MachineGuid, BIOS serial, model
    and GPU — so anything keyed on hostname merges them and one machine's state
    silently overwrites the other's. Use a genuinely unique per-machine value
    (Windows MachineGuid, a DMI UUID, or an operator-assigned label).

    ``allowed_networks`` enforces reachability rather than assuming it: an
    endpoint may be reachable only over an overlay network, or only over the
    LAN, so "either-or" is a requirement rather than flexibility.
    """

    state_dir: str | None = None
    mission_command: str | None = None
    machine_id: str | None = None
    allowed_networks: list[str] = field(default_factory=lambda: ["lan", "tailnet"])


@dataclass
class ProgramConfig:
    """Full configuration for an external program module.

    Loaded from YAML files in config/modules/.

    Config field schema is stored in ``config_fields`` (list of ConfigField).
    Live/mutable values are stored in ``configurable`` (dict) which is
    initialized from field defaults and updated by ``update_config()``.

    Legacy YAML with ``configurable: {key: value}`` is auto-converted to
    a ``config_fields`` list with inferred types.

    New YAML with ``config_fields: [...]`` uses the full typed schema.
    """

    name: str
    description: str = ""
    # Identity / display fields (shown in dashboard)
    display_name: str = ""
    category: str = ""
    tags: list[str] = field(default_factory=list)
    version: str = ""
    # Connectivity
    ipc: IPCConfig = field(default_factory=IPCConfig)
    health_check: HealthCheckConfig | None = None
    lifecycle: LifecycleConfig | None = None
    endpoint: EndpointConfig | None = None
    research_profile: str | None = None
    enabled: bool = False
    # Typed field schema (source of truth for field metadata)
    config_fields: list[ConfigField] = field(default_factory=list)
    # Live mutable values (keyed by field name, initialized from defaults)
    configurable: dict[str, Any] = field(default_factory=dict)
    operations: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> ProgramConfig:
        """Create ProgramConfig from a parsed YAML dict."""
        ipc_data = data.get("ipc", {})
        ipc = IPCConfig(
            method=ipc_data.get("method", "http"),
            url=ipc_data.get("url"),
            timeout=ipc_data.get("timeout", 30),
            command=ipc_data.get("command", []),
            working_dir=Path(ipc_data["working_dir"]) if ipc_data.get("working_dir") else None,
            env=ipc_data.get("env", {}),
            ssh_host=ipc_data.get("ssh_host"),
            ssh_key=ipc_data.get("ssh_key"),
            ssh_connect_timeout=ipc_data.get("ssh_connect_timeout", 10),
            remote_working_dir=ipc_data.get("remote_working_dir"),
            remote_claude_path=ipc_data.get("remote_claude_path", "claude"),
        )

        hc_data = data.get("health_check")
        health_check = None
        if hc_data:
            health_check = HealthCheckConfig(
                endpoint=hc_data.get("endpoint", "/health"),
                interval_seconds=hc_data.get("interval_seconds", 60),
                timeout_seconds=hc_data.get("timeout_seconds", 10),
                expected_status=hc_data.get("expected_status", 200),
            )

        lc_data = data.get("lifecycle")
        lifecycle = None
        if lc_data:
            lifecycle = LifecycleConfig(
                ssh_host=lc_data.get("ssh_host"),
                ssh_key=lc_data.get("ssh_key"),
                source_dir=lc_data.get("source_dir"),
                restart_cmd=lc_data.get("restart_cmd"),
                logs_cmd=lc_data.get("logs_cmd"),
            )

        ep_data = data.get("endpoint")
        endpoint = None
        if ep_data:
            endpoint = EndpointConfig(
                state_dir=ep_data.get("state_dir"),
                mission_command=ep_data.get("mission_command"),
                machine_id=ep_data.get("machine_id"),
                allowed_networks=ep_data.get("allowed_networks", ["lan", "tailnet"]),
            )

        # Config fields: prefer new typed schema, fall back to legacy configurable dict
        raw_config_fields = data.get("config_fields", [])
        legacy_configurable = data.get("configurable", {})

        if raw_config_fields:
            config_fields = [ConfigField.from_dict(f) for f in raw_config_fields]
        elif legacy_configurable:
            # Auto-convert legacy {key: default_value} to ConfigField list
            config_fields = [
                ConfigField(
                    name=k,
                    type=infer_field_type(v),
                    label=k.replace("_", " ").title(),
                    default=v,
                )
                for k, v in legacy_configurable.items()
            ]
        else:
            config_fields = []

        # Initialize live values from field defaults
        configurable = {f.name: f.default for f in config_fields}

        return cls(
            name=data["name"],
            description=data.get("description", ""),
            display_name=data.get("display_name", ""),
            category=data.get("category", ""),
            tags=data.get("tags", []),
            version=data.get("version", ""),
            ipc=ipc,
            health_check=health_check,
            lifecycle=lifecycle,
            endpoint=endpoint,
            research_profile=data.get("research_profile"),
            enabled=data.get("enabled", False),
            config_fields=config_fields,
            configurable=configurable,
            operations=data.get("operations", {}),
        )
