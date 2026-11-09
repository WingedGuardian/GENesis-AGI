#!/usr/bin/env python3
"""Launch a Genesis MCP server for Codex: the owner's client, or an unattended run.

An owner-operated Codex session gets the same Genesis MCP servers, and the same
tools, a Claude Code session gets: the ordinary bootstrap, no allowlist. The
server is told which client it serves (``GENESIS_CALLER_PROFILE=codex``), so
its calls are recorded in ``client_tool_calls`` and its credential lookups are
stamped; the label is a record, never a permission.

A Codex run that Genesis started unattended (``genesis.util.codex_cli`` marks
those with ``GENESIS_CC_SESSION=1``) keeps today's external floor instead: the
13-tool health/memory profile, and no outreach or recon server. That mirrors
Claude Code, where an unmarked session is the owner's and a dispatched one is
restricted. The project ``.codex/config.toml`` forwards the markers this script
reads (``env_vars``); Codex passes an MCP child nothing else from its own
environment.

The markers are read BEFORE ``sanitized_environment()``, which strips them. An
ABSENT marker is the owner by design (the owner's ruling, mirroring Claude
Code), so every Codex run Genesis starts must go through
``genesis.util.codex_cli``; a launch that scrubs its environment (``env -i``, a
bare ``systemd-run``) would read as the owner. Only a PRESENT marker with an
unrecognised value fails closed, to the floor.

Owner mode runs the main checkout's server, as Claude Code does, never a
feature worktree's copy: that would be a mixed-version server (the branch's
script, the main checkout's package) with real write tools against the live
database.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_external_mcp import launcher_path, runtime_root, sanitized_environment  # noqa: E402

CLIENT_LABEL = "codex"
PROFILE_ENV = "GENESIS_CALLER_PROFILE"
# The servers the external floor admits (genesis_mcp_server.py refuses the rest).
_FLOOR_SERVERS = frozenset({"health", "memory"})
_SERVERS = frozenset({"health", "memory", "outreach", "recon"})


def is_unattended(env: Mapping[str, str]) -> bool:
    """Whether the Codex process that started this server was marked dispatched."""
    marker = env.get("GENESIS_CC_SESSION")
    return marker not in (None, "", "0") or bool(env.get("GENESIS_SESSION_ORIGIN"))


def server_name(args: list[str]) -> str:
    """The one Genesis server this launch is for, from the config's fixed arguments.

    Exactly ``--server <name>`` (or ``--server=<name>``) naming a Genesis server
    is accepted; anything else is refused rather than passed through, so no
    extra argument (``--external-client``, a second ``--server``) reaches the
    server whose own parser would read it differently.
    """
    if len(args) == 2 and args[0] == "--server":
        name = args[1]
    elif len(args) == 1 and args[0].startswith("--server="):
        name = args[0].split("=", 1)[1]
    else:
        raise SystemExit(f"usage: codex_owner_mcp.py --server <name>; got {args!r}")
    if name not in _SERVERS:
        raise SystemExit(f"unknown Genesis server {name!r}; expected one of {sorted(_SERVERS)}")
    return name


def plan(args: list[str], env: Mapping[str, str]) -> tuple[list[str], dict[str, str]] | None:
    """The launcher argv and environment to exec, or None to start nothing."""
    server = server_name(args)
    child_env = sanitized_environment(dict(env))
    child_env.pop(PROFILE_ENV, None)
    if is_unattended(env):
        if server not in _FLOOR_SERVERS:
            return None
        # Exactly what the external launcher starts (its own launcher path).
        return [str(launcher_path()), "--external-client", "--server", server], child_env
    child_env[PROFILE_ENV] = CLIENT_LABEL
    owner_launcher = runtime_root() / ".claude" / "mcp" / "run-mcp-server"
    return [str(owner_launcher), "--server", server], child_env


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    launcher = launcher_path()
    if not launcher.is_file():
        raise SystemExit(f"Genesis MCP launcher not found: {launcher}")
    planned = plan(args, os.environ)
    if planned is None:
        print(
            f"Genesis {server_name(args)} server is not available to an unattended "
            "Codex run; it gets the external health/memory floor only.",
            file=sys.stderr,
        )
        raise SystemExit(0)
    child_argv, child_env = planned
    if not Path(child_argv[0]).is_file():
        raise SystemExit(f"Genesis MCP launcher not found: {child_argv[0]}")
    os.execve(child_argv[0], child_argv, child_env)  # noqa: S606


if __name__ == "__main__":
    main()
