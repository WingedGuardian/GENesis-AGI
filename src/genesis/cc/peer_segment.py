"""Opt-in execution policy for externally requested peer segments.

This contains the CLI, not peer authorization. Admission and result disclosure enforce grants separately.
The optional facade variant retains its exact future capability contract. Legacy CC invocations do not use it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.cc.exceptions import CCProcessError
from genesis.util.proc_kill import ProcessCleanupUncertain

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
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
        "SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS",
    }
)


@dataclass(frozen=True)
class PeerSegment:
    segment_id: str
    deadline_at: float
    tools: tuple[str, ...]
    mode: str = "facade"

    def __post_init__(self) -> None:
        if not isinstance(self.segment_id, str) or not re.fullmatch(
            r"[a-f0-9]{32}", self.segment_id
        ):
            raise ValueError("peer segment identifier must be a lowercase UUID hex value")
        if isinstance(self.deadline_at, bool) or not isinstance(self.deadline_at, (int, float)):
            raise ValueError("peer segment deadline must be a finite timestamp")
        if not math.isfinite(self.deadline_at) or self.deadline_at <= 0:
            raise ValueError("peer segment deadline must be a finite timestamp")
        if self.mode not in {"facade", "conversation"}:
            raise ValueError("invalid peer segment mode")
        if (
            not isinstance(self.tools, tuple)
            or (self.mode == "facade" and not self.tools)
            or (self.mode == "conversation" and bool(self.tools))
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

    def validate(self, inv: CCInvocation) -> None:
        """Construction and launch share one closed invocation contract."""
        self.__post_init__()
        if (
            inv.skip_permissions or inv.resume_session_id or not inv.strict_mcp_config
            or inv.bare or inv.safe_mode or inv.supervised or inv.append_system_prompt
            or inv.allowed_tools or inv.disallowed_tools or inv.skill_tags
            or inv.bash_allowlist or inv.env_overrides or inv.output_format != "json"
            or inv.origin != "external_untrusted"
            or not isinstance(inv.system_prompt, str) or not inv.system_prompt.strip()
            or not isinstance(inv.working_dir, str) or not Path(inv.working_dir).is_absolute()
            or type(inv.timeout_s) is not int or not 1 <= inv.timeout_s <= 7200
        ):
            raise ValueError("Invalid contained peer invocation")
        if self.mode == "conversation":
            if inv.mcp_config is not None or inv.disable_tools is not True:
                raise ValueError("Peer conversation must be tools-free without MCP")
        else:
            if not isinstance(inv.mcp_config, str) or not Path(inv.mcp_config).is_absolute():
                raise ValueError("Peer execution requires a private facade-only configuration")
            self.validate_facade_config(inv.mcp_config)

    @staticmethod
    def pin_environment(env: dict[str, str], working_dir: str) -> dict[str, str]:
        env.update(
            GENESIS_SESSION_ORIGIN="external_untrusted",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            CLAUDE_CODE_DISABLE_AUTO_MEMORY="1",
            CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
            CLAUDE_CODE_DISABLE_ATTACHMENTS="1",
        )
        env["PATH"] = PeerSegment._safe_path(env.get("PATH"), working_dir)
        return env

    @staticmethod
    def cli_flags() -> list[str]:
        return ["--tools", "", "--setting-sources", "", "--permission-mode", "dontAsk",
                "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
                "--no-session-persistence"]

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
            # Caller cancellation is not evidence of a failed drain. Own the
            # cleanup independently; only a successful result retires the claim.
            cleanup = asyncio.create_task(self.stop_and_drain())
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:
                    break  # Classify completed drain failure below; retain claim.
            if cleanup.cancelled() or cleanup.exception() is not None:
                raise ProcessCleanupUncertain("Peer scope cleanup is unconfirmed") from None
            cleanup.result()  # Only confirmed success retires the claim.
            with _INVOCATION_LOCK:
                _INVOCATION_UNITS.remove(self.unit_name)
            if cancelled:
                raise asyncio.CancelledError

    @staticmethod
    def _safe_path(path: str | None, working_dir: str) -> str:
        peer = Path(working_dir).resolve()
        directories = []
        for entry in (os.defpath if path is None else path).split(os.pathsep):
            if not entry or not Path(entry).is_absolute():
                continue
            directory = Path(entry).resolve()
            if directory == peer or peer in directory.parents:
                continue
            if str(directory) not in directories:
                directories.append(str(directory))
        return os.pathsep.join(directories)

    def launch_args(self, args: list[str], env: dict[str, str], working_dir: str,
                    resource_properties: tuple[str, ...]) -> list[str]:
        """Resolve both launch commands before subprocess applies the peer cwd."""
        path = self._safe_path(env.get("PATH"), working_dir)
        peer = Path(working_dir).resolve()

        def executable(command: str) -> str:
            candidate = command if os.path.dirname(command) else shutil.which(command, path=path) if path else None
            if candidate is None:
                raise CCProcessError("peer launch executable unavailable")
            resolved = Path(candidate).resolve()
            if (resolved == peer or peer in resolved.parents
                    or not resolved.is_file() or not os.access(resolved, os.X_OK)):
                raise CCProcessError("peer launch executable unavailable")
            return str(resolved)

        scope = self.scope_args(resource_properties)
        scope[0] = executable(scope[0])
        return [*scope, executable(args[0]), *args[1:]]

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
        self.pin_environment(env, invocation.working_dir)
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
        # `-P` keeps the peer's working directory off sys.path: `python -m`
        # otherwise prepends the cwd, so a planted `genesis/` package there
        # would be imported before the facade or its lease are validated.
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
                or len(args) != 5
                or args[:4] != ["-P", "-m", "genesis.peers.facade", "--lease-file"]
                or not isinstance(args[4], str)
                or not Path(args[4]).is_absolute()
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


async def native_invocation(db, request, session_id: str):
    """Construct only from the prepared public request, before owner builders."""
    from datetime import datetime
    from uuid import UUID

    from genesis.cc.direct_session import _bg_session_root
    from genesis.cc.managed_session import fingerprint, metadata
    from genesis.cc.types import CCInvocation
    from genesis.db.crud import cc_sessions

    row = await cc_sessions.get_by_id(db, session_id)
    meta = metadata(row) if row else {}
    if (
        not request.contained_peer or row is None or row["status"] != "active"
        or row["source_tag"] != "agent_api"
        or meta.get("managed_fingerprint") != fingerprint(request)
        or str(UUID(session_id)) != session_id
    ):
        raise ValueError("Contained prepared receipt is unavailable")
    deadline = datetime.fromisoformat(meta["deadline_at"])
    if deadline.tzinfo is None:
        raise ValueError("Contained receipt deadline is unavailable")
    root = _bg_session_root(session_id)
    return CCInvocation(
        prompt=request.prompt, system_prompt=request.system_prompt,
        model=request.model, effort=request.effort, timeout_s=request.timeout_s,
        working_dir=str(root), claude_code_tmpdir=str(root / "cc-sandbox"),
        disable_tools=True, no_session_persistence=True, confirm_cleanup=True,
        origin="external_untrusted", session_key=session_id, roster_eligible=True,
        caller_tag="direct_session.observe",
        peer_segment=PeerSegment(UUID(session_id).hex, deadline.timestamp(), (), mode="conversation"),
    )
