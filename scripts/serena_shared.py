#!/usr/bin/env python3
"""Opt-in, fixed-main Serena services; linked worktrees retain native stdio.

No provider or MCP implementation lives here. Native Serena owns HTTP and
Terse owns the stdio transport. Changes to provider snapshots require configure.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROFILES = ("claude-code", "codex")
PORTS = {"claude-code": 9165, "codex": 9166}


def project_root(start: Path) -> Path | None:
    """Use the nearest project boundary, including linked .git files."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / ".serena/project.yml").is_file():
            return candidate
    return None


def settings_path() -> Path:
    return (
        Path(os.environ.get("GENESIS_HOME", Path.home() / ".genesis")) / "config/serena-shared.json"
    )


def read_settings(path: Path) -> dict | None:
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or type(config.get("enabled")) is not bool:
        raise ValueError("invalid Serena sharing settings")
    main = config.get("main")
    if not isinstance(main, str) or not Path(main).is_absolute():
        raise ValueError("Serena main must be an absolute project path")
    return config


def binary(name: str) -> str:
    result = shutil.which(name)
    if not result:
        raise ValueError(f"{name} is not installed or is missing from PATH")
    return result


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--user", *args], check=True)


def unit_name(context: str) -> str:
    return f"genesis-serena-{context}.service"


def launch(context: str, project: Path | None) -> None:
    config = read_settings(settings_path())
    if config and config["enabled"] and project == Path(config["main"]).resolve():
        unit = unit_name(context)
        active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], check=False)
        if active.returncode:
            raise ValueError(f"{unit} unavailable; restore the managed service or disable sharing")
        command = [
            binary("terse"),
            "proxy",
            "--server-name",
            "serena",
            "--no-diff",
            "--no-join-blocks",
            "--",
            f"http://127.0.0.1:{PORTS[context]}/mcp",
        ]
    else:
        command = [binary("serena"), "start-mcp-server", "--context", context]
        command += ["--project", str(project)] if project else ["--project-from-cwd"]
    os.execv(command[0], command)  # noqa: S606 - resolved executable, argv without shell


def quote_unit(value: str) -> str:
    """systemd.syntax quoting; ExecStart uses ':' to disable $ expansion."""
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("systemd path contains a line break or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_unit(project: Path, context: str, provider_home: Path, serena: str, path: str) -> str:
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--context",
        context,
        "--project",
        str(project),
        "--binary",
        serena,
    ]
    return f"""[Unit]
Description=Genesis shared Serena ({context}, fixed main checkout)
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=exec
WorkingDirectory={str(project).replace("%", "%%")}
Environment={quote_unit("PATH=" + path)}
Environment={quote_unit("SERENA_HOME=" + str(provider_home))}
ExecStart=:{" ".join(quote_unit(x) for x in args)}
ExecStartPost=:{quote_unit(sys.executable)} {quote_unit(str(Path(__file__).resolve()))} ready --port {PORTS[context]} --binary {quote_unit(serena)}
MemoryMax=4G
MemorySwapMax=0
CPUQuota=200%
OOMScoreAdjust=500
KillMode=control-group
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def serve(project: Path, context: str, serena: str) -> None:
    version = subprocess.check_output([serena, "--version"], text=True).strip()
    if version != "Serena 1.7.0":
        raise ValueError("revalidate shared services before changing Serena 1.7.0")
    args = [
        serena,
        "start-mcp-server",
        "--context",
        context,
        "--project",
        str(project),
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(PORTS[context]),
        "--enable-web-dashboard",
        "false",
        "--enable-gui-log-window",
        "false",
        "--open-web-dashboard",
        "false",
    ]
    os.execv(serena, args)  # noqa: S606 - absolute provider executable, no shell


def owns_listener(pid: int, port: int) -> bool:
    """Read Linux socket ownership; a foreign listener cannot pass readiness."""
    inodes = set()
    for fd in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    for row in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        fields = row.split()
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A" and fields[9] in inodes:
            return True
    return False


def check_capabilities(serena: str, port: int) -> None:
    # Validate the actual native tool set, not a YAML approximation: project
    # fixed/optional tools can override the provider's single-project exclusion.
    code = """import asyncio, sys
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def check():
    async with streamablehttp_client('http://127.0.0.1:' + sys.argv[1] + '/mcp') as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            if tools.intersection({'activate_project', 'switch_modes'}):
                raise RuntimeError('Shared project switching exposed')
asyncio.run(check())
"""
    subprocess.run([provider_python(serena), "-c", code, str(port)], check=True)


def ready(port: int, serena: str) -> None:
    # systemd supplies MAINPID to ExecStartPost and bounds startup through its
    # existing TimeoutStartSec. ActiveState stays activating until this exact
    # listener belongs to the pinned native provider, not another process.
    pid = int(os.environ["MAINPID"])
    while not owns_listener(pid, port):
        time.sleep(0.1)
    check_capabilities(serena, port)


def provider_python(serena: str) -> str:
    interpreter = Path(serena).read_text().splitlines()[0].removeprefix("#!")
    if not Path(interpreter).is_absolute() or not os.access(interpreter, os.X_OK):
        raise ValueError("Serena must have an absolute Python shebang")
    return interpreter


def snapshot_context(serena: str, context: str, destination: Path) -> None:
    # Use the installed provider's loader and YAML implementation so user
    # contexts win exactly as they do in native stdio. Keep the native name:
    # Serena selects Codex's OpenAI schema compatibility by that name.
    interpreter = provider_python(serena)
    code = """import sys, yaml, serena
from serena.config.context_mode import SerenaAgentContext
if serena.__version__ != '1.7.0':
    raise RuntimeError('Revalidate sharing before changing Serena version')
path = SerenaAgentContext.get_path(sys.argv[1])
with open(path) as f: config = yaml.safe_load(f)
config['name'] = sys.argv[1]
config['single_project'] = True
print(yaml.safe_dump(config, sort_keys=False))
"""
    content = subprocess.check_output([interpreter, "-c", code, context], text=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content)


def link_resource(destination: Path, source: Path) -> None:
    """Keep native shared resources in their original store; never discard data."""
    if destination.is_symlink() and destination.resolve() == source.resolve():
        return
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink() and not any(destination.iterdir()):
            destination.rmdir()
        else:
            raise ValueError(
                f"preserve/reconcile existing Serena resource before linking: {destination}"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.symlink_to(source, target_is_directory=True)


def configure(project: Path, enable: bool) -> None:
    config_file = settings_path()
    if project_root(project) != project or not (project / ".git").is_dir():
        raise ValueError("configure requires the canonical main checkout, not a linked worktree")
    if not enable:
        # Recovery must not depend on a working/current provider or proxy.
        write_settings(config_file, project, False)
        systemctl("disable", "--now", *(unit_name(x) for x in PROFILES))
        return
    for port in PORTS.values():
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as error:
                raise ValueError(
                    f"port {port} unavailable; stop the two owned Serena services before "
                    "refreshing snapshots, and preserve any foreign listener"
                ) from error
    serena = binary("serena")
    binary("terse")
    directory = Path.home() / ".config/systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    home_root = config_file.parent.parent / "serena-shared"
    source_home = Path(os.environ.get("SERENA_HOME") or Path.home() / ".serena").resolve()
    for context in PROFILES:
        home = home_root / context
        home.mkdir(parents=True, exist_ok=True)
        home.chmod(0o700)
        source = source_home / "serena_config.yml"
        if source.exists():
            shutil.copyfile(source, home / "serena_config.yml")
        snapshot_context(serena, context, home / "contexts" / f"{context}.yml")
        for resource in ("modes", "prompt_templates", "memories/global"):
            link_resource(home / resource, source_home / resource)
        (directory / unit_name(context)).write_text(
            render_unit(project, context, home, serena, os.environ.get("PATH", os.defpath))
        )
    systemctl("daemon-reload")
    systemctl("enable", "--now", *(unit_name(x) for x in PROFILES))
    write_settings(config_file, project, True)


def write_settings(config_file: Path, project: Path, enabled: bool) -> None:
    config_file.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=config_file.parent, prefix=config_file.name + ".")
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(json.dumps({"enabled": enabled, "main": str(project)}, indent=2) + "\n")
        temporary.replace(config_file)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    launch_parser = sub.add_parser("launch")
    launch_parser.add_argument("--context", choices=PROFILES, default="claude-code")
    launch_parser.add_argument("--project", type=Path)
    config_parser = sub.add_parser("configure")
    config_parser.add_argument("--main", type=Path, required=True)
    config_parser.add_argument("--enable", action="store_true")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--context", choices=PROFILES, required=True)
    serve_parser.add_argument("--project", type=Path, required=True)
    serve_parser.add_argument("--binary", required=True)
    ready_parser = sub.add_parser("ready")
    ready_parser.add_argument("--port", type=int, choices=tuple(PORTS.values()), required=True)
    ready_parser.add_argument("--binary", required=True)
    args = parser.parse_args()
    try:
        if args.command == "launch":
            project = args.project.resolve() if args.project else project_root(Path.cwd())
            launch(args.context, project)
        elif args.command == "configure":
            configure(args.main.resolve(), args.enable)
        elif args.command == "serve":
            serve(args.project.resolve(), args.context, args.binary)
        else:
            ready(args.port, args.binary)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"serena-shared: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
