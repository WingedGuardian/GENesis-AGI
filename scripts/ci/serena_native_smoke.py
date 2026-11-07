#!/usr/bin/env python3
"""Run with Serena 2.1's Python; exercise a branch against disposable projects."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import serena
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def load_shared(path):
    spec = importlib.util.spec_from_file_location("scratch_shared", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result_payload(result):
    data = result.model_dump(by_alias=True)
    if data.get("isError"):
        raise AssertionError(data)
    return json.dumps(data)


async def initialize(session, *, editable=True):
    data = (await session.initialize()).model_dump(by_alias=True)
    if not (data["serverInfo"]["version"] == "2.1.0"):
        raise AssertionError(data)
    names = {tool.name for tool in (await session.list_tools()).tools}
    required = {"get_symbols_overview", "find_referencing_symbols"}
    if editable:
        required.add("replace_symbol_body")
    if not required <= names:
        raise AssertionError(f"native tools missing: {required - names}")
    if not editable and "replace_symbol_body" in names:
        raise AssertionError("read-only project exposes a write tool")
    if names.intersection({"activate_project", "switch_modes", "serena_repl"}):
        raise AssertionError(
            'native check failed: not names.intersection({"activate_project", "switch_modes", "serena_repl"})'
        )
    return len(names)


async def queries(session):
    overview = result_payload(
        await session.call_tool("get_symbols_overview", {"relative_path": "sample.py", "depth": 0})
    )
    if "sample_function" not in overview:
        raise AssertionError('native check failed: "sample_function" in overview')
    references = result_payload(
        await session.call_tool(
            "find_referencing_symbols",
            {"name_path": "sample_function", "relative_path": "sample.py"},
        )
    )
    if "caller" not in references:
        raise AssertionError(references)


async def check_clients(url, proxy, env, project, restart):
    async with (
        streamable_http_client(url) as (read, write),
        ClientSession(read, write) as direct,
    ):
        count = await initialize(direct)
        await queries(direct)
        parameters = StdioServerParameters(
            command=str(proxy),
            env=env,
            args=[
                "proxy",
                "--server-name",
                "serena",
                "--no-diff",
                "--no-join-blocks",
                "--",
                url,
            ],
        )
        async with (
            stdio_client(parameters) as (proxy_read, proxy_write),
            ClientSession(proxy_read, proxy_write) as proxied,
        ):
            if not (await initialize(proxied) == count):
                raise AssertionError("native check failed: await initialize(proxied) == count")
            await queries(proxied)
            previous = (project / "sample.py").read_bytes()
            increment = 3 if restart else 2
            body = f"def sample_function(value: int) -> int:\n    return value + {increment}"
            result_payload(
                await proxied.call_tool(
                    "replace_symbol_body",
                    {
                        "name_path": "sample_function",
                        "relative_path": "sample.py",
                        "body": body,
                    },
                )
            )
            current = (project / "sample.py").read_bytes()
            if current == previous or f"return value + {increment}".encode() not in current:
                raise AssertionError("native edit did not change the disposable source")
        await queries(direct)  # The other client's disconnect must not break this one.
    return count


@asynccontextmanager
async def running_provider(args, profile, root, log):
    shared_path = args.root / "scripts/serena_shared.py"
    provider = args.provider
    project, home = root / "project", root / profile
    env = dict(
        os.environ,
        HOME=str(home),
        SERENA_HOME=str(home / "provider"),
        XDG_CACHE_HOME=str(home / "cache"),
        XDG_CONFIG_HOME=str(home / "config"),
    )
    with socket.socket() as pick:
        pick.bind(("127.0.0.1", 0))
        port = pick.getsockname()[1]
    launcher = (
        "import importlib.util,sys;from pathlib import Path;"
        "s=importlib.util.spec_from_file_location('shared',sys.argv[1]);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "m.PORTS[sys.argv[2]]=int(sys.argv[3]);"
        "m.serve(Path(sys.argv[4]),sys.argv[2],sys.argv[5])"
    )
    child = subprocess.Popen(
        [
            str(Path(provider).parent / "python"),
            "-c",
            launcher,
            str(shared_path),
            profile,
            str(port),
            str(project),
            str(provider),
        ],
        cwd=project,
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    try:
        # A broken provider must not strand this disposable smoke test indefinitely.
        # The initial two-profile feasibility run completed within 90 s/profile.
        for _ in range(120):
            if child.poll() is not None:
                raise AssertionError(f"provider exited {child.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.25)
        else:
            raise TimeoutError("disposable provider did not listen within 30 seconds")
        shared = load_shared(shared_path)
        if not (shared.owns_listener(child.pid, port)):
            raise AssertionError("native check failed: shared.owns_listener(child.pid, port)")
        yield shared, child.pid, port, env
    finally:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        except ProcessLookupError:
            child.wait(timeout=5)


async def run_provider(args, profile, root, log, restart):
    async with running_provider(args, profile, root, log) as (shared, pid, port, env):
        with patch.dict(os.environ, MAINPID=str(pid)):
            shared.ready(port, str(args.provider))
        count = await check_clients(
            f"http://127.0.0.1:{port}/mcp", args.proxy, env, root / "project", restart
        )
        print(
            json.dumps(
                {
                    "profile": profile,
                    "restart": restart,
                    "native_and_proxy_tools": count,
                    "symbol_references_edit_disconnect": "passed",
                }
            ),
            flush=True,
        )


async def check_project_controls(args, profile, root):
    project = root / "project"
    settings = project / ".serena/project.yml"
    original = settings.read_bytes()
    source = (project / "sample.py").read_bytes()
    cases = {
        "read-only": {"read_only": True},
        "project-repl": {"agent_interface": "REPL"},
        "project-switching": {"fixed_tools": ["activate_project", "switch_modes", "find_symbol"]},
    }
    try:
        for case, changes in cases.items():
            config = yaml.safe_load(original)
            config.update(changes)
            settings.write_text(yaml.safe_dump(config))
            with (root / f"{profile}-{case}.log").open("w+") as log:
                try:
                    async with running_provider(args, profile, root, log) as (
                        shared,
                        pid,
                        port,
                        env,
                    ):
                        if case == "read-only":
                            with patch.dict(os.environ, MAINPID=str(pid)):
                                shared.ready(port, str(args.provider))
                            async with (
                                streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (r, w),
                                ClientSession(r, w) as session,
                            ):
                                await initialize(session, editable=False)
                                await queries(session)
                        else:
                            # The production CLI intentionally accepts only its fixed ports.
                            # Call the same readiness function on this owned scratch listener.
                            checker = (
                                "import importlib.util,sys;"
                                "s=importlib.util.spec_from_file_location('shared',sys.argv[1]);"
                                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                                "m.ready(int(sys.argv[2]),sys.argv[3])"
                            )
                            result = subprocess.run(
                                [
                                    str(args.provider.parent / "python"),
                                    "-c",
                                    checker,
                                    str(args.root / "scripts/serena_shared.py"),
                                    str(port),
                                    str(args.provider),
                                ],
                                env=dict(env, MAINPID=str(pid)),
                                capture_output=True,
                                text=True,
                            )
                            if (
                                result.returncode == 0
                                or "Shared project switching or REPL exposed" not in result.stderr
                            ):
                                raise AssertionError(
                                    f"wrong native refusal for {case}: {result.stderr}"
                                )
                        if (project / "sample.py").read_bytes() != source:
                            raise AssertionError("native project control changed source")
                        print(
                            json.dumps({"profile": profile, "control": case, "status": "passed"}),
                            flush=True,
                        )
                except BaseException:
                    log.seek(0)
                    print(log.read(), flush=True)
                    raise
    finally:
        settings.write_bytes(original)


async def exercise(args, root):
    shared_path = args.root / "scripts/serena_shared.py"
    project = root / "project"
    project.mkdir()
    (project / "sample.py").write_text(
        "def sample_function(value: int) -> int:\n    return value + 1\n\ndef caller():\n    return sample_function(1)\n"
    )
    for profile in ("claude-code", "codex"):
        home = root / profile
        provider_home = home / "provider"
        (provider_home / "contexts").mkdir(parents=True)
        (provider_home / "serena_config.yml").write_text(
            yaml.safe_dump(
                {
                    "web_dashboard": False,
                    "gui_log_window": False,
                    "agent_interface": "tools",
                    "trusted_project_path_patterns": [str(project)],
                    "projects": [],
                }
            )
        )
        stock = Path(serena.__file__).parent / "resources/config/contexts" / f"{profile}.yml"
        context = yaml.safe_load(stock.read_text())
        context["description"] = "disposable custom context preserved"
        (provider_home / "contexts" / f"{profile}.yml").write_text(yaml.safe_dump(context))
        # Run the branch's real snapshotter with an isolated native provider home.
        snapshot = (
            "import importlib.util,sys;from pathlib import Path;"
            "s=importlib.util.spec_from_file_location('shared',sys.argv[1]);"
            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "m.snapshot_context(sys.argv[2],sys.argv[3],Path(sys.argv[4]),Path(sys.argv[5]))"
        )
        subprocess.run(
            [
                str(Path(args.provider).parent / "python"),
                "-c",
                snapshot,
                str(shared_path),
                str(args.provider),
                profile,
                str(provider_home / "contexts" / f"{profile}.yml"),
                str(project),
            ],
            env=dict(os.environ, SERENA_HOME=str(provider_home)),
            cwd=project,
            check=True,
        )
        context = yaml.safe_load((provider_home / "contexts" / f"{profile}.yml").read_text())
        if (
            context["description"] != "disposable custom context preserved"
            or not context["single_project"]
        ):
            raise AssertionError("native snapshot lost context customization or fixed-project mode")
        for restart in (False, True):
            with (root / f"{profile}-{restart}.log").open("w+") as log:
                try:
                    await asyncio.wait_for(
                        run_provider(args, profile, root, log, restart),
                        timeout=180,
                    )
                except BaseException:
                    log.seek(0)
                    print(log.read(), flush=True)
                    raise
        await asyncio.wait_for(check_project_controls(args, profile, root), timeout=360)
    print(
        json.dumps(
            {
                "status": "passed",
                "shared_source_sha256": hashlib.sha256(shared_path.read_bytes()).hexdigest(),
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--provider", type=Path, required=True)
    parser.add_argument("--proxy", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="serena210-e2e-", dir=Path.home() / "tmp") as directory:
        asyncio.run(exercise(args, Path(directory)))


if __name__ == "__main__":
    main()
