"""Opt-in execution policy for externally requested peer segments.

This contains the CLI, not peer authorization. Admission, the lease broker and
result disclosure enforce grants separately. Legacy CC invocations do not use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.cc.types import CCInvocation

_PARENT_UNIT = "genesis-server.service"
_CGROUP_ROOT = Path("/sys/fs/cgroup")
_INVOCATION_LOCK = threading.Lock()
_INVOCATION_UNITS: set[str] = set()
_PROVIDER_ENV = frozenset(
    {
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "TZ",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
    }
)


@dataclass(frozen=True)
class PeerSegment:
    segment_id: str
    deadline_at: float
    tools: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", self.segment_id
        ):
            raise ValueError("peer segment identifier must be a lowercase UUID hex value")
        if isinstance(self.deadline_at, bool) or not isinstance(self.deadline_at, (int, float)):
            raise ValueError("peer segment deadline must be a finite timestamp")
        if not math.isfinite(self.deadline_at) or self.deadline_at <= 0:
            raise ValueError("peer segment deadline must be a finite timestamp")
        if (
            not isinstance(self.tools, tuple)
            or not self.tools
            or any(
                not isinstance(tool, str)
                or not re.fullmatch(r"mcp__genesis_peer__[a-z][a-z0-9_]*", tool)
                for tool in self.tools
            )
        ):
            raise ValueError("peer segment tools must be exact facade tool names")

    @property
    def unit_name(self) -> str:
        return f"genesis-peer-{self.segment_id}.scope"

    @contextlib.asynccontextmanager
    async def invocation(self):
        # All invoker instances/loops in this server share scope ownership.
        # Rejected duplicates must never enter the owner's cleanup path.
        with _INVOCATION_LOCK:
            if self.unit_name in _INVOCATION_UNITS:
                raise RuntimeError("peer segment is already active or awaiting reconciliation")
            _INVOCATION_UNITS.add(self.unit_name)
        try:
            yield
        finally:
            # Retain the claim on ANY exceptional drain outcome, including
            # cancellation of the cleanup task itself. Restart clears this
            # process-local hold; it is not a multiprocess launcher lock.
            await self.stop_and_drain()
            with _INVOCATION_LOCK:
                _INVOCATION_UNITS.remove(self.unit_name)

    def scope_args(self, resource_properties: tuple[str, ...]) -> list[str]:
        remaining = min(7200.0, self.deadline_at - time.time())
        if remaining <= 0:
            raise ValueError("peer segment deadline expired")
        args = [
            "systemd-run", "--user", "--scope", "--collect", "--quiet", f"--unit={self.unit_name}"
        ]
        for value in (
            *resource_properties,
            f"RuntimeMaxSec={remaining:.6f}s",
            "RuntimeRandomizedExtraSec=0",
            "TimeoutStopSec=10s",
            "KillMode=control-group",
            "SendSIGKILL=yes",
            f"BindsTo={_PARENT_UNIT}",
            f"After={_PARENT_UNIT}",
        ):
            args.extend(("-p", value))
        args.append("--")
        return args

    def environment(self, invocation: CCInvocation) -> dict[str, str]:
        # Keep real provider authentication and HOME. A peer's only Genesis
        # capability is its facade lease; it inherits no owner API credential,
        # shell startup override, external-client setting, or session identity.
        env = {key: value for key, value in os.environ.items() if key in _PROVIDER_ENV}
        env["GENESIS_SESSION_ORIGIN"] = "external_untrusted"
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        from genesis.cc.roster import apply_routing_env

        apply_routing_env(
            env,
            base_url=invocation.anthropic_base_url,
            auth_token=invocation.anthropic_auth_token,
            model_id=invocation.model_id_override,
        )
        return env

    def validate_facade_config(self, path: str) -> None:
        # This file is owner-created, not a peer-supplied command. Pin the one
        # stdio entry point and forbid config env/extra server overrides.
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.getuid()
                ):
                    raise ValueError
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError
            config = json.loads(raw)
            server = config["mcpServers"]["genesis_peer"]
            args = server["args"]
            if (
                config.keys() != {"mcpServers"}
                or config["mcpServers"].keys() != {"genesis_peer"}
                or server.keys() != {"command", "args"}
                or server["command"] != sys.executable
                or not isinstance(args, list)
                or len(args) != 4
                or args[:3] != ["-m", "genesis.peers.facade", "--lease-file"]
                or not isinstance(args[3], str)
                or not Path(args[3]).is_absolute()
            ):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ValueError(
                "peer execution requires a private facade-only configuration"
            ) from None

    def _show_scope(self) -> dict[str, str]:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                self.unit_name,
                "--property=LoadState",
                "--property=ActiveState",
                "--property=ControlGroup",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        values = dict(line.split("=", 1) for line in result.stdout.split("\n") if line)
        if result.returncode or set(values) != {"LoadState", "ActiveState", "ControlGroup"}:
            raise RuntimeError("peer scope state could not be confirmed")
        return values

    def _stop_scope(self) -> None:
        values = self._show_scope()
        root = _CGROUP_ROOT
        if not (root / "cgroup.controllers").is_file():
            raise RuntimeError("peer scope requires an available cgroup v2 filesystem")
        if not values["ControlGroup"] and (
            (values["LoadState"] == "not-found" and values["ActiveState"] == "inactive")
            or (values["LoadState"] == "loaded" and values["ActiveState"] in {"inactive", "failed"})
        ):
            # systemd's unit_maybe_release_cgroup checks recursive emptiness
            # before clearing the path, including failed scopes with lingering
            # descendants. Failed state alone is insufficient evidence.
            return
        control_group = values["ControlGroup"]
        group = root / control_group.lstrip("/")
        if not control_group.startswith("/") or ".." in group.parts or group.name != self.unit_name:
            raise RuntimeError("peer scope control group could not be confirmed")
        stopped = subprocess.run(
            ["systemctl", "--user", "stop", self.unit_name], capture_output=True, timeout=15
        )
        try:
            events = dict(
                line.split() for line in (group / "cgroup.events").read_text().splitlines()
            )
        except FileNotFoundError:
            return  # The kernel can remove a cgroup only after it is empty.
        if stopped.returncode or events.get("populated") != "0":
            raise RuntimeError("peer scope drain was not confirmed")

    async def stop_and_drain(self) -> None:
        # Repeated caller cancellation cannot abandon the process-tree drain.
        # Failure remains visible so the task coordinator cannot release a slot.
        cleanup = asyncio.create_task(asyncio.to_thread(self._stop_scope))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError
