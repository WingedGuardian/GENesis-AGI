#!/usr/bin/env python3
"""Explicit operator preparation and supervised terminal-only dry-run launch."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME / "src"))
sys.path.insert(0, str(RUNTIME / "scripts"))

from codex_validator_pilot import (  # noqa: E402
    bound_state,
    configuration,
    deployment_lock,
    finish_shutdown,
    source_hashes,
)
from codex_validator_serving import child_environment  # noqa: E402
from pr_verification import SCHEMA_TEMPLATE  # noqa: E402

from genesis.eval.qualification.evidence import (  # noqa: E402
    check_directory,
    load_json,
    private_open,
)

VERSION = "codex-cli 0.162.0"
DOCTRINE = Path(".agents/skills/validating-merges/SKILL.md")
CANONICAL_DOCTRINE = RUNTIME / ".claude/skills/validating-merges/SKILL.md"
DISABLED = (
    "plugins", "remote_plugin", "memories", "js_repl", "multi_agent", "apps",
    "connectors", "browser_use", "computer_use", "remote_control",
)
RUN_TIMEOUT = 7200.0


def policy(workspace: Path) -> str:
    command = shlex.join([
        "/usr/bin/env", f"PATH={RUNTIME / '.venv/bin'}:/usr/bin:/bin",
        "/bin/bash", str(RUNTIME / "scripts/hooks/codex-validator-guard"),
        "--runtime-root", str(RUNTIME), "--workspace-root", str(workspace),
    ])
    flags = "\n".join(f"{name} = false" for name in DISABLED)
    return f'''model = "gpt-6.1-sol"
model_provider = "openai"
approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
[projects.{json.dumps(str(workspace))}]
trust_level = "trusted"
[analytics]
enabled = false
[features]
{flags}
[features.multi_agent_v2]
enabled = false
[agents]
enabled = false
[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = {json.dumps(command)}
timeout = 15
'''


def trust_suffix(key: str, fingerprint: str) -> str:
    return f"\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = {json.dumps(fingerprint)}\n"


def private_bytes(path: Path) -> bytes:
    check_directory(path.parent)
    with os.fdopen(private_open(path, os.O_RDONLY), "rb") as handle:
        data = handle.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Validator profile exceeds its limit")
    return data


def write_new(path: Path, data: bytes):
    check_directory(path.parent)
    with os.fdopen(private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def profile_bytes(workspace: Path, *, trusted: bool) -> bytes:
    path = workspace / ".codex/client/config.toml"
    raw = private_bytes(path)
    suffix = ""
    if trusted:
        key = str(path) + ":pre_tool_use:0:0"
        states = tomllib.loads(raw.decode("utf-8"))["hooks"]["state"]
        if set(states) != {key} or set(states[key]) != {"trusted_hash"}:
            raise ValueError("Validator trust unavailable")
        fingerprint = states[key]["trusted_hash"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint):
            raise ValueError("Validator trust unavailable")
        suffix = trust_suffix(key, fingerprint)
    if raw != (policy(workspace) + suffix).encode():
        raise ValueError("Validator profile changed")
    return raw


@contextmanager
def run_lock(workspace: Path):
    check_directory(workspace / ".codex")
    fd = private_open(workspace / ".codex/run.lock", os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


async def inspect(binary: str, workspace: Path, env: dict, *, trusted: bool) -> dict:
    profile_bytes(workspace, trusted=trusted)
    proc = await asyncio.create_subprocess_exec(
        binary, "--no-daemon", "--strict-config", "-C", str(workspace),
        "app-server", "--listen", "stdio://", env=env, cwd=workspace,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=2 * 1024 * 1024,
    )
    total = 0

    async def rpc(method, params, number):
        nonlocal total
        proc.stdin.write(json.dumps({"id": number, "method": method, "params": params}).encode() + b"\n")
        await proc.stdin.drain()
        while True:
            raw = await proc.stdout.readline()
            total += len(raw)
            if not raw or total > 4 * 1024 * 1024:
                raise ValueError("Validator preflight unavailable")
            event = load_json(raw)
            if not isinstance(event, dict) or "error" in event or ("method" in event and "id" in event):
                raise ValueError("Validator preflight refused")
            if event.get("id") == number:
                return event["result"]

    try:
        async with asyncio.timeout(60):
            await rpc("initialize", {
                "clientInfo": {"name": "genesis_validator_preflight", "version": "1"},
                "capabilities": {"experimentalApi": True},
            }, 1)
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            listing = await rpc("hooks/list", {"cwds": [str(workspace)]}, 2)
            effective = (await rpc("config/read", {"includeLayers": False}, 3))["config"]
        entries = listing["data"]
        if len(entries) != 1 or entries[0]["errors"] or len(entries[0]["hooks"]) != 1:
            raise ValueError("Validator hook unavailable")
        hook = entries[0]["hooks"][0]
        config_path = workspace / ".codex/client/config.toml"
        expected = tomllib.loads(policy(workspace))
        definition = expected["hooks"]["PreToolUse"][0]["hooks"][0]
        if (
            hook["key"] != str(config_path) + ":pre_tool_use:0:0"
            or hook["eventName"] != "preToolUse" or hook["handlerType"] != "command"
            or hook["command"] != definition["command"] or hook["matcher"] != ".*"
            or hook["async"] is not False or hook["enabled"] is not True
            or hook["timeoutSec"] != 15 or hook["sourcePath"] != str(config_path)
            or hook["trustStatus"] != ("trusted" if trusted else "untrusted")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", hook["currentHash"])
            or effective.get("mcp_servers")
            or any(effective.get(key) != expected[key] for key in (
                "model", "model_provider", "approval_policy", "sandbox_mode", "web_search", "analytics"
            ))
            or effective["agents"]["enabled"] is not False
            or effective["features"]["multi_agent_v2"]["enabled"] is not False
            or any(effective["features"].get(name) is not False for name in DISABLED)
            or any(value for key, value in effective["hooks"].items() if key not in {"PreToolUse", "state"})
        ):
            raise ValueError("Validator policy unavailable")
        suffix = trust_suffix(hook["key"], hook["currentHash"]) if trusted else ""
        if private_bytes(config_path) != (policy(workspace) + suffix).encode():
            raise ValueError("Validator profile changed")
        return hook
    finally:
        await finish_shutdown(proc)


async def launch(workspace: Path, prepare: bool) -> int:
    binary = shutil.which("codex")
    if binary is None:
        raise ValueError("Codex unavailable")
    binary = str(Path(binary).resolve())
    home = workspace / ".codex/client"
    env = {**child_environment(), "HOME": str(home), "CODEX_HOME": str(home)}
    for name in ("python", "python3"):
        interpreter = RUNTIME / ".venv/bin" / name
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise ValueError("Validator interpreter unavailable")
    dependencies = await asyncio.create_subprocess_exec(
        str(RUNTIME / ".venv/bin/python"), "-I", "-c", "import aiosqlite,pydantic",
        env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(10):
            if await dependencies.wait():
                raise ValueError("Validator dependencies unavailable")
    finally:
        await finish_shutdown(dependencies)
    version = await asyncio.create_subprocess_exec(
        binary, "--version", env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True,
    )
    try:
        async with asyncio.timeout(10):
            reported = await version.stdout.read(128)
            code = await version.wait()
        if code or reported.decode().strip() != VERSION:
            raise ValueError("Codex needs requalification")
    finally:
        await finish_shutdown(version)
    config = configuration(workspace)
    for row in config["rows"]:
        if source_hashes(RUNTIME, row["recipe"]) != row["sources"]:
            raise ValueError("Validator source changed")
    if prepare:
        home.mkdir(mode=0o700)
        destination = workspace / DOCTRINE
        for directory in (workspace / ".agents", workspace / ".agents/skills", destination.parent):
            directory.mkdir(mode=0o700, exist_ok=True)
            check_directory(directory)
        write_new(destination, CANONICAL_DOCTRINE.read_bytes())
        config_path = home / "config.toml"
        write_new(config_path, policy(workspace).encode())
        hook = await inspect(binary, workspace, env, trusted=False)
        with os.fdopen(private_open(config_path, os.O_WRONLY | os.O_APPEND), "wb") as handle:
            handle.write(trust_suffix(hook["key"], hook["currentHash"]).encode())
            handle.flush()
            os.fsync(handle.fileno())
    check_directory(home)
    if private_bytes(workspace / DOCTRINE) != CANONICAL_DOCTRINE.read_bytes():
        raise ValueError("Validator doctrine changed")
    await inspect(binary, workspace, env, trusted=True)
    if prepare:
        print("Validator profile prepared; no session started")
        return 0
    prompt = (
        "Run the supervised validator dry-run. Genesis MCP and recording are unavailable. "
        "Use the validating-merges doctrine, but execute only sealed requests through "
        + shlex.join([str(RUNTIME / ".venv/bin/python"), "-I", str(RUNTIME / "scripts/codex_validator_request.py"),
                      "--workspace-root", str(workspace), "--request", str(workspace / "requests/<canonical-UUID>.json")])
        + ". Create private requests using admitted apply_patch. Exact JSON operations: "
        "{version:1,operation:pilot_packet}; {version:1,operation:pilot_probe,pr:<enrolled>}; "
        "{version:1,operation:pilot_preview,pr:<enrolled>,receipt:<probe SHA256>,evidence:<document>,note:null-or-text,park:false-or-true}. "
        "Inspect the packet, run its fixed probes, preview each row with truthful measured scope/gaps. "
        "Never invent measurements or record a verdict. Stop and report uncertainty/failure to the supervisor. "
        "Evidence template (replace placeholders): " + json.dumps(SCHEMA_TEMPLATE)
    )
    proc = await asyncio.create_subprocess_exec(
        binary, "--no-daemon", "--strict-config", "exec", "--ephemeral", "--skip-git-repo-check",
        "--json", "-C", str(workspace), "-", env=env, cwd=workspace,
        stdin=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        async with asyncio.timeout(RUN_TIMEOUT):
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            return await proc.wait()
    finally:
        await finish_shutdown(proc)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "run"))
    parser.add_argument("--workspace-root", required=True)
    args = parser.parse_args()
    mask = os.umask(0o077)
    try:
        workspace = Path(args.workspace_root)
        if (not workspace.is_absolute() or workspace.resolve() != workspace
                or workspace.is_relative_to(RUNTIME) or RUNTIME.is_relative_to(workspace)):
            raise ValueError("Unsupported validator workspace")
        for directory in (workspace, workspace / ".codex", workspace / "requests", workspace / ".codex/receipts"):
            check_directory(directory)
        with run_lock(workspace):
            if args.operation == "run":
                # Serving observation owns its event loop; run it before async
                # native preflight. Every sealed operation brackets again.
                config = configuration(workspace)
                with deployment_lock():
                    for row in config["rows"]:
                        bound_state(RUNTIME, config, row)
            return asyncio.run(launch(workspace, args.operation == "prepare"))
    except (Exception, KeyboardInterrupt):
        print("Validator terminal refused or stopped", file=sys.stderr)
        return 2
    finally:
        os.umask(mask)


if __name__ == "__main__":
    raise SystemExit(main())
