#!/usr/bin/env python3
"""Owner-started interactive validator; no host installation or lifecycle enrollment."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RUNTIME / "src"))
sys.path.insert(0, str(RUNTIME / "scripts"))
sys.path.insert(0, str(RUNTIME / "scripts/hooks"))

from codex_external_mcp import _SESSION_CONTEXT  # noqa: E402
from codex_validator_adapter import hook_command  # noqa: E402

from genesis.eval.qualification.evidence import (  # noqa: E402
    check_directory,
    load_json,
    private_open,
)
from genesis.mcp.external_profiles import profile_tools  # noqa: E402
from genesis.util.proc_kill import confirm_process_exit  # noqa: E402
from genesis.util.tasks import tracked_task  # noqa: E402

VERSION = "codex-cli 0.166.0"
HOOK_TREES = ("scripts/hooks", ".claude/hooks")
ENTRY_SOURCES = frozenset({"scripts/codex-validator", "scripts/codex_validator_launcher.py"})
EXTRA_SOURCES = (
    "scripts/codex-validator",
    "scripts/codex_validator_launcher.py",
    "scripts/codex_external_mcp.py",
    "scripts/genesis_mcp_server.py",
    ".claude/mcp/run-mcp-server",
    "src/genesis/__init__.py",
    "src/genesis/mcp/__init__.py",
    "src/genesis/mcp/external_profiles.py",
    "src/genesis/eval/__init__.py",
    "src/genesis/eval/qualification/__init__.py",
    "src/genesis/eval/qualification/evidence.py",
    "src/genesis/util/__init__.py",
    "src/genesis/util/proc_kill.py",
    "src/genesis/util/process_lock.py",
    "src/genesis/util/systemd.py",
    "src/genesis/util/tasks.py",
)
DEPLOY_PATTERN = r"(^|/)(deploy_code_only|update|genesis-update-[[:alnum:]]{6})[.]sh([[:space:]]|$)"


def environment(runtime: Path, home: Path) -> dict[str, str]:
    # Reuse the validated root; do not resolve it through inherited Git state.
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_CONTEXT}
    env = {k: v for k, v in env.items() if not k.startswith("GIT_")}
    env.pop("GENESIS_HOOK_DEV_LOCAL", None)
    env.update(
        HOME=pwd.getpwuid(os.getuid()).pw_dir,
        CODEX_HOME=str(home),
        GENESIS_REPO_ROOT=str(runtime),
        PATH=str(runtime / ".venv/bin") + ":/usr/bin:/bin",
    )
    return env


def policy(runtime: Path, model: str) -> str:
    servers = ""
    for server in ("health", "memory"):
        servers += f"""
[mcp_servers.genesis_validator_{server}]
command = {json.dumps(str(runtime / ".venv/bin/python"), ensure_ascii=False)}
args = {json.dumps([str(runtime / "scripts/codex_external_mcp.py"), "validator", "--server", server], ensure_ascii=False)}
cwd = {json.dumps(str(runtime), ensure_ascii=False)}
required = true
startup_timeout_sec = 30
enabled_tools = {json.dumps(sorted(profile_tools(server, "validator")), ensure_ascii=False)}
default_tools_approval_mode = "approve"
"""
    return (
        f"""model = {json.dumps(model, ensure_ascii=False)}
model_provider = "openai"
approval_policy = "never"
sandbox_mode = "danger-full-access"
cli_auth_credentials_store = "file"
[projects.{json.dumps(str(runtime), ensure_ascii=False)}]
trust_level = "untrusted"
[analytics]
enabled = false
[features]
plugins = false
remote_plugin = false
[features.multi_agent_v2]
enabled = false
[agents]
enabled = false
[[hooks.PreToolUse]]
matcher = ".*"
[[hooks.PreToolUse.hooks]]
type = "command"
command = {json.dumps(hook_command(runtime), ensure_ascii=False)}
timeout = 15
"""
        + servers
    )


def trust_suffix(key: str, fingerprint: str) -> str:
    return f"\n[hooks.state.{json.dumps(key, ensure_ascii=False)}]\ntrusted_hash = {json.dumps(fingerprint, ensure_ascii=False)}\n"


def private_bytes(path: Path) -> bytes:
    check_directory(path.parent)
    with os.fdopen(private_open(path, os.O_RDONLY), "rb") as handle:
        raw = handle.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("Validator profile exceeds its limit")
    return raw


def write_new(path: Path, data: bytes) -> None:
    check_directory(path.parent)
    with os.fdopen(private_open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def profile_bytes(home: Path, runtime: Path, *, trusted: bool) -> bytes:
    if (home / "hooks.json").exists() or (home / "hooks.json").is_symlink():
        raise ValueError("Unexpected validator hook source")
    path = home / "config.toml"
    raw = private_bytes(path)
    data = tomllib.loads(raw.decode())
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Validator model unavailable")
    suffix = ""
    if trusted:
        key = str(path) + ":pre_tool_use:0:0"
        states = data["hooks"]["state"]
        if set(states) != {key} or set(states[key]) != {"trusted_hash"}:
            raise ValueError("Validator trust unavailable")
        fingerprint = states[key]["trusted_hash"]
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", fingerprint
        ):
            raise ValueError("Validator trust unavailable")
        suffix = trust_suffix(key, fingerprint)
    if raw != (policy(runtime, model) + suffix).encode():
        raise ValueError("Validator profile changed")
    return raw


def git(runtime: Path, *args: str) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "--no-replace-objects", "-c", "core.fsmonitor=false", "-C", str(runtime), *args],
        env=env,
        check=True,
        capture_output=True,
    )
    return result.stdout


def check_sources(runtime: Path, qualified: str, *, include_entry: bool = True) -> None:
    if (
        not re.fullmatch(r"[0-9a-f]{40}", qualified)
        or git(runtime, "cat-file", "-t", qualified) != b"commit\n"
    ):
        raise ValueError("Qualified commit unavailable")
    if git(runtime, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked main checkout is not clean")
    extras = tuple(p for p in EXTRA_SOURCES if include_entry or p not in ENTRY_SOURCES)
    paths = (*HOOK_TREES, *extras)
    rows = git(runtime, "ls-tree", "-rz", qualified, "--", *paths).split(b"\0")
    names = set()
    for row in filter(None, rows):
        metadata, name = row.split(b"\t", 1)
        mode, kind, blob = metadata.split()
        rel = os.fsdecode(name)
        path = runtime / rel
        info = path.lstat()
        if kind != b"blob" or path.resolve() != path or not stat.S_ISREG(info.st_mode):
            raise ValueError("Qualified source type changed")
        actual_mode = b"100755" if info.st_mode & stat.S_IXUSR else b"100644"
        if mode != actual_mode or path.read_bytes() != git(
            runtime, "cat-file", "blob", blob.decode()
        ):
            raise ValueError("Qualified physical source changed")
        names.add(rel)
    if not set(extras) <= names or any(
        not any(n.startswith(d + "/") for n in names) for d in HOOK_TREES
    ):
        raise ValueError("Qualification source inventory incomplete")
    for directory in HOOK_TREES:
        for path in (runtime / directory).rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            if (
                "__pycache__" not in path.parts
                and path.relative_to(runtime).as_posix() not in names
            ):
                raise ValueError("Unqualified hook input present")


def execution_root(owner_branch_test: bool) -> Path:
    common = Path(os.fsdecode(git(RUNTIME, "rev-parse", "--path-format=absolute", "--git-common-dir")).strip()).resolve()
    runtime = common.parent
    if not owner_branch_test:
        if runtime != RUNTIME:
            raise ValueError("Validator must launch from the deployed main checkout")
    elif (runtime == RUNTIME or common.name != ".git" or not (RUNTIME / ".git").is_file()
          or Path(os.fsdecode(git(runtime, "rev-parse", "--show-toplevel")).strip()).resolve() != runtime):
        raise ValueError("Owner branch test requires a linked worktree of the main checkout")
    return runtime


def check_qualification(runtime: Path, qualified: str) -> None:
    check_sources(RUNTIME, qualified)
    if runtime != RUNTIME:
        check_sources(runtime, qualified, include_entry=False)


def check_no_deploy() -> None:
    presence = subprocess.run(["/usr/bin/pgrep", "-f", DEPLOY_PATTERN], capture_output=True)
    if presence.returncode != 1:
        raise ValueError("Deployment is active or its absence could not be established")


async def stop_child(proc) -> None:
    await confirm_process_exit(proc)


async def spawn(argv: list[str], runtime: Path, env: dict):
    creation = tracked_task(
        asyncio.create_subprocess_exec(
            *argv,
            cwd=runtime,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=2 * 1024 * 1024,
        ),
        name="validator-preflight-create",
    )
    try:
        return await asyncio.shield(creation)
    except BaseException:
        while not creation.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(creation)
        if not creation.cancelled() and creation.exception() is None:
            await finish_child(creation.result())
        raise


async def finish_child(proc) -> None:
    cleanup = tracked_task(stop_child(proc), name="validator-preflight-stop")
    interrupted = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            interrupted = True
    cleanup.result()
    if interrupted:
        raise asyncio.CancelledError


async def inspect(binary: str, runtime: Path, home: Path, env: dict, *, trusted: bool) -> dict:
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        for sig in previous:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(task.cancel))
        return await _inspect(binary, runtime, home, env, trusted=trusted)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


async def _inspect(binary: str, runtime: Path, home: Path, env: dict, *, trusted: bool) -> dict:
    original = profile_bytes(home, runtime, trusted=trusted)
    expected = tomllib.loads(original.decode())
    proc = await spawn(
        [
            binary,
            "--no-daemon",
            "--strict-config",
            "--disable",
            "apps",
            "-C",
            str(runtime),
            "app-server",
            "--listen",
            "stdio://",
        ],
        runtime,
        env,
    )
    total = 0

    async def rpc(method, params, number):
        nonlocal total
        proc.stdin.write(json.dumps(dict(id=number, method=method, params=params)).encode() + b"\n")
        await proc.stdin.drain()
        while True:
            raw = await proc.stdout.readline()
            total += len(raw)
            if not raw or total > 4 * 1024 * 1024:
                raise ValueError("Validator preflight unavailable")
            event = load_json(raw)
            if (
                not isinstance(event, dict)
                or "error" in event
                or ("method" in event and "id" in event)
            ):
                raise ValueError("Validator preflight refused")
            if event.get("id") == number:
                return event["result"]

    try:
        async with asyncio.timeout(60):
            await rpc(
                "initialize",
                {
                    "clientInfo": {"name": "genesis_validator_preflight", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
                1,
            )
            proc.stdin.write(b'{"method":"initialized","params":{}}\n')
            listing = await rpc("hooks/list", {"cwds": [str(runtime)]}, 2)
            effective = (await rpc("config/read", {"includeLayers": False}, 3))["config"]
            status = await rpc("mcpServerStatus/list", {}, 4)
        entries = listing["data"]
        if len(entries) != 1 or entries[0]["errors"] or len(entries[0]["hooks"]) != 1:
            raise ValueError("Validator hook unavailable")
        hook = entries[0]["hooks"][0]
        definition = expected["hooks"]["PreToolUse"][0]["hooks"][0]
        if (
            hook["key"] != str(home / "config.toml") + ":pre_tool_use:0:0"
            or hook["eventName"] != "preToolUse"
            or hook["handlerType"] != "command"
            or hook["command"] != definition["command"]
            or hook["matcher"] != ".*"
            or hook["async"] is not False
            or hook["enabled"] is not True
            or hook["timeoutSec"] != 15
            or hook["sourcePath"] != str(home / "config.toml")
            or hook["trustStatus"] != ("trusted" if trusted else "untrusted")
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", hook["currentHash"])
        ):
            raise ValueError("Validator hook changed")
        for key in (
            "model",
            "model_provider",
            "approval_policy",
            "sandbox_mode",
            "analytics",
            "mcp_servers",
            "projects",
        ):
            if key == "mcp_servers":
                if set(effective[key]) != set(expected[key]):
                    raise ValueError("Validator servers changed")
                for name, server in expected[key].items():
                    if any(effective[key][name].get(k) != v for k, v in server.items()):
                        raise ValueError("Validator server policy changed")
            elif effective.get(key) != expected[key]:
                raise ValueError("Validator effective policy changed")
        if (
            effective["agents"]["enabled"] is not False
            or effective["features"]["multi_agent_v2"]["enabled"] is not False
            or any(effective["features"].get(k) is not False for k in ("apps", "plugins", "remote_plugin"))
            or any(v for k, v in effective["hooks"].items() if k not in {"PreToolUse", "state"})
        ):
            raise ValueError("Unexpected validator capability")
        rows = status["data"]
        if (
            status.get("nextCursor") is not None
            or len(rows) != 2
            or {r["name"] for r in rows} != set(expected["mcp_servers"])
        ):
            raise ValueError("Validator server discovery incomplete")
        for row in rows:
            server = row["name"].rsplit("_", 1)[1]
            if row.get("toolsError") or not set(profile_tools(server, "validator")) <= set(
                row["tools"]
            ):
                raise ValueError("Validator server tools unavailable")
        suffix = trust_suffix(hook["key"], hook["currentHash"]) if trusted else ""
        if (
            profile_bytes(home, runtime, trusted=trusted)
            != (policy(runtime, expected["model"]) + suffix).encode()
        ):
            raise ValueError("Validator profile changed during inspection")
        return hook
    finally:
        await finish_child(proc)


def check_binary(binary: str) -> None:
    result = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True)
    reported = result.stdout.strip()
    if not re.fullmatch(r"codex-cli [0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", reported):
        raise ValueError("Unrecognized Codex version")
    if reported != VERSION:
        print(
            f"Warning: {reported}; tested reference is {VERSION}. "
            "Tool dispatch behavior on this version is unqualified; startup inspects policy and trust only.",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("operation", choices=("prepare", "login", "check", "run"))
    parser.add_argument("--home", type=Path, default=Path.home() / ".codex-validator")
    parser.add_argument("--model")
    parser.add_argument("--qualified-commit")
    parser.add_argument("--owner-branch-test", action="store_true")
    parser.add_argument("--device-auth", action="store_true", help="Use device-code authentication for login")
    args = parser.parse_args()
    if args.device_auth and args.operation != "login":
        parser.error("--device-auth is only valid with login")
    mask = os.umask(0o077)
    try:
        home = args.home.expanduser().absolute()
        if home.resolve() != home or home == Path.home() / ".codex" or not home.is_absolute():
            raise ValueError("Dedicated canonical validator home required")
        if os.environ.get("GENESIS_CC_SESSION"):
            raise ValueError("Inherited Genesis session context is not permitted")
        runtime = execution_root(args.owner_branch_test)
        if args.owner_branch_test:
            if not args.qualified_commit:
                raise ValueError("Owner branch test requires its exact qualified commit")
            check_qualification(runtime, args.qualified_commit)
            check_no_deploy()
        binary = shutil.which("codex")
        if binary is None:
            raise ValueError("Codex unavailable")
        binary = str(Path(binary).resolve())
        check_binary(binary)
        env = environment(runtime, home)
        if args.operation == "prepare":
            if not args.model or not args.model.strip():
                raise ValueError("Preparation requires the owner-selected model")
            home.mkdir(mode=0o700)
            write_new(home / "config.toml", policy(runtime, args.model).encode())
            hook = asyncio.run(inspect(binary, runtime, home, env, trusted=False))
            if args.owner_branch_test:
                check_qualification(runtime, args.qualified_commit)
            with os.fdopen(
                private_open(home / "config.toml", os.O_WRONLY | os.O_APPEND), "wb"
            ) as handle:
                handle.write(trust_suffix(hook["key"], hook["currentHash"]).encode())
                handle.flush()
                os.fsync(handle.fileno())
            asyncio.run(inspect(binary, runtime, home, env, trusted=True))
            if args.owner_branch_test:
                check_qualification(runtime, args.qualified_commit)
            print("Validator profile prepared; no session started or credentials copied")
            return 0
        profile_bytes(home, runtime, trusted=True)
        if args.operation == "login":
            if args.owner_branch_test:
                check_qualification(runtime, args.qualified_commit)
            login_argv = [binary, "--no-daemon", "login"]
            if args.device_auth:
                login_argv.append("--device-auth")
            os.execve(binary, login_argv, env)  # noqa: S606 - resolved client, fixed argv
        if not args.qualified_commit:
            raise ValueError("Name the exact commit from the native qualification receipt")
        check_qualification(runtime, args.qualified_commit)
        check_no_deploy()
        asyncio.run(inspect(binary, runtime, home, env, trusted=True))
        check_qualification(runtime, args.qualified_commit)
        profile_bytes(home, runtime, trusted=True)
        if args.operation == "check":
            print("Validator preflight passed; no model session started")
            return 0
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ValueError("Validator run requires a supervised interactive terminal")
        prompt = (
            "Load .claude/skills/validating-merges/SKILL.md and its Running under Codex section. "
            "Follow its risk-tier sampling policy. Write progress to "
            "~/.genesis/output/validator-runs/<UTC-date>.md. "
            "Every shell call starts with a literal absolute cd /directory || exit, newline, then body. "
            "A denial means stop and raise it with the owner. Never deploy or restart services. "
            "Use the validator MCP tools; verdict closes remain server-gated until calibration. "
            "Do not register this conversation, its transcript, a ledger or charter in Genesis."
        )
        os.execve(  # noqa: S606 - resolved client, fixed argv
            binary,
            [binary, "--no-daemon", "--strict-config", "--disable", "apps", "-C", str(runtime), prompt],
            env,
        )
    except (Exception, KeyboardInterrupt, asyncio.CancelledError):
        print(
            "Validator refused or stopped; inspect the policy and qualification with the owner",
            file=sys.stderr,
        )
        return 2
    finally:
        os.umask(mask)


if __name__ == "__main__":
    raise SystemExit(main())
