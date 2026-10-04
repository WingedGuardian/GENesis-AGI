#!/usr/bin/env python3
"""Explicit pinned native Codebase lifecycle; no MCP or indexing broker.

The settings route is fail closed once present. Configure does not remove the
machine sentinel or start services. Internal ABI is pinned to the accepted build.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from code_intel_cbm_admission import resolve_cgroup  # noqa: E402
from code_intel_cbm_worker import BUILD  # noqa: E402

GIB = 1024**3
DISABLED_KEYS = ("auto_index", "auto_watch", "watcher_enabled")
MARKER = "# Genesis managed Codebase v1\n"


def absolute(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or any(c in raw for c in "\n\r\x00"):
        raise ValueError("invalid managed path")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("managed path must be absolute")
    return path


def read_settings(path: Path) -> dict:
    value = json.loads(path.read_text())
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or type(value.get("enabled")) is not bool
    ):
        raise ValueError("invalid managed settings")
    for key in ("main", "binary", "cache", "runtime", "sentinel"):
        absolute(value.get(key))
    if not isinstance(value.get("name"), str) or not re.fullmatch(
        r"genesis-cbm-[a-z0-9]+(?:-[a-z0-9]+)*", value["name"]
    ):
        raise ValueError("invalid managed unit name")
    if value.get("build") != BUILD:
        raise ValueError("unsupported managed build")
    return value


def require_enabled(config: dict) -> None:
    if not config["enabled"] or Path(config["sentinel"]).exists():
        raise ValueError("managed Codebase is disabled")


def verified_binary(path: Path):
    if not path.is_absolute() or not os.access(path, os.X_OK):
        raise ValueError("managed executable unavailable")
    stream = path.open("rb")
    try:
        if hashlib.file_digest(stream, "sha256").hexdigest() != BUILD:
            raise ValueError("unsupported managed executable; repeat acceptance before upgrading")
    except BaseException:
        stream.close()
        raise
    return stream


def native_env(config: dict) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CBM_")}
    env.update(
        CBM_CACHE_DIR=config["cache"],
        CBM_RUNTIME_DIR=config["runtime"],
        CBM_ALLOWED_ROOT=config["main"],
    )
    return env


def verify_cache(config: dict) -> None:
    cache = Path(config["cache"])
    ui = json.loads((cache / "config.json").read_text())
    if not isinstance(ui, dict) or ui.get("ui_enabled") is not False:
        raise ValueError("managed UI must be explicitly disabled")
    with sqlite3.connect((cache / "_config.db").as_uri() + "?mode=ro", uri=True) as db:
        values = dict(db.execute("SELECT key,value FROM config"))
    if any(values.get(k) != "false" for k in DISABLED_KEYS):
        raise ValueError("managed automatic indexing/watchers must be disabled")


def systemctl(*args: str) -> str:
    return subprocess.check_output(
        ["/usr/bin/systemctl", "--user", *args],
        text=True,
        timeout=130 if args[0] in ("start", "restart") else 40 if args[0] == "stop" else 30,
    ).strip()


def backend(config: dict) -> str:
    return config["name"] + ".service"


def check_backend(config: dict, *, starting: bool = False) -> None:
    unit = backend(config)
    if systemctl("show", unit, "-p", "ActiveState", "--value") not in (
        ("active", "activating") if starting else ("active",)
    ):
        raise ValueError("managed daemon unavailable; restore service and reconnect")
    pid = int(systemctl("show", unit, "-p", "MainPID", "--value"))
    if pid <= 0 or not os.path.samefile(f"/proc/{pid}/exe", config["binary"]):
        raise ValueError("managed daemon identity mismatch")
    membership = Path(f"/proc/{pid}/cgroup").read_text()
    if not any(
        row.startswith("0::") and Path(row[3:]).name == unit for row in membership.splitlines()
    ):
        raise ValueError("managed daemon escaped service")


def ready(config: dict) -> None:
    # Socket paths can survive a crash. Only a native connect-only status RPC
    # naming this exact managed PID establishes readiness, never path existence.
    require_enabled(config)
    deadline = time.monotonic() + 60
    with verified_binary(Path(config["binary"])) as executable:
        while time.monotonic() < deadline:
            try:
                check_backend(config, starting=True)
                response = subprocess.run(
                    [f"/proc/self/fd/{executable.fileno()}", "daemon", "status"],
                    env=native_env(config),
                    pass_fds=(executable.fileno(),),
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                pid = systemctl("show", backend(config), "-p", "MainPID", "--value")
                if (
                    response.returncode == 0
                    and "daemon: active (permanent)" in response.stdout
                    and re.search(r"^  pid: " + re.escape(pid) + r"$", response.stdout, re.M)
                    and "state: stopping" not in response.stdout
                ):
                    return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass  # bounded startup sampling; terminal error follows the deadline
            time.sleep(0.1)
    raise ValueError("managed native daemon did not become ready")


def verify_boundary(config: dict, role: str, unit: str) -> None:
    leaf, root, version = resolve_cgroup(Path("/proc/self/cgroup"), Path("/proc/self/mountinfo"))
    if role == "client" and not re.fullmatch(
        re.escape(config["name"]) + r"-client-[0-9a-f]{32}\.service", unit
    ):
        raise ValueError("invalid owned frontend unit")
    if version != 2 or leaf.name != unit:
        raise ValueError("managed process is outside expected cgroup v2 unit")
    expected = 2 * GIB if role == "serve" else GIB // 4
    if (leaf / "memory.max").read_text().strip() != str(expected) or (
        leaf / "memory.swap.max"
    ).read_text().strip() != "0":
        raise ValueError("managed process lacks exact memory/zero-swap cap")
    if role == "client":
        parent = leaf.parent
        if parent.name != config["name"] + "-clients.slice" or (
            parent / "memory.max"
        ).read_text().strip() != str(2 * GIB):
            raise ValueError("managed frontend aggregate cap unavailable")
        if (parent / "memory.swap.max").read_text().strip() != "0":
            raise ValueError("managed frontend aggregate swap cap unavailable")
    cursor = leaf.parent
    while cursor == root or root in cursor.parents:
        maximum = (cursor / "memory.max").read_text().strip()
        if maximum != "max" and int(maximum) < expected:
            raise ValueError("ancestor cap is smaller than managed requirement")
        if cursor == root:
            break
        cursor = cursor.parent


def execute_native(config: dict, role: str, unit: str) -> None:
    require_enabled(config)
    verify_cache(config)
    verify_boundary(config, role, unit)
    if role == "client":
        check_backend(config)
    args = (
        ["--cbm-daemon-internal", "--cbm-daemon-permanent"]
        if role == "serve"
        else ["--tool-profile=analysis"]
    )
    with verified_binary(Path(config["binary"])) as executable:
        if role == "serve":
            # The stock LOCAL_CLI transition seals/repairs a dead native endpoint
            # generation. Internal daemon mode alone refuses a stale socket.
            # config get starts no daemon, changes no settings and enables no UI.
            subprocess.run(
                [f"/proc/self/fd/{executable.fileno()}", "config", "get", "auto_index"],
                env=native_env(config),
                pass_fds=(executable.fileno(),),
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=45,
            )
        os.set_inheritable(executable.fileno(), True)
        os.execve(  # noqa: S606 - pinned verified inode, fixed argv
            f"/proc/self/fd/{executable.fileno()}", [config["binary"], *args], native_env(config)
        )


def quote_unit(value: str) -> str:
    absolute(value) if value.startswith("/") else None
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("invalid unit argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_units(config: dict, path: Path) -> dict[str, str]:
    command = " ".join(
        quote_unit(x)
        for x in [sys.executable, str(Path(__file__).resolve()), "--config", str(path), "serve"]
    )
    return {
        backend(config): MARKER
        + f"""[Unit]
Description=Genesis pinned native Codebase query daemon
[Service]
Type=exec
WorkingDirectory={config["main"].replace("%", "%%")}
ExecStart=:{command}
ExecStartPost=:{command.removesuffix(quote_unit("serve")) + quote_unit("ready")}
TimeoutStartSec=120
MemoryMax=2G
MemorySwapMax=0
TasksMax=128
CPUQuota=200%
OOMScoreAdjust=500
KillMode=control-group
Restart=no
TimeoutStopSec=30
[Install]
WantedBy=default.target
""",
        config["name"] + "-clients.slice": MARKER
        + """[Unit]
Description=Genesis managed Codebase frontend aggregate
[Slice]
MemoryAccounting=yes
MemoryMax=2G
MemorySwapMax=0
TasksMax=512
""",
    }


def configure(args: argparse.Namespace, path: Path) -> None:
    main, source, state = (
        absolute(args.main).resolve(strict=True),
        absolute(args.binary),
        absolute(args.state).resolve(),
    )
    if not (main / ".git").is_dir():
        raise ValueError("managed Codebase requires the primary checkout")
    config = dict(
        version=1,
        enabled=False,
        main=str(main),
        binary=str(state / "bin" / BUILD),
        cache=str(state / "cache"),
        runtime=str(state / "runtime"),
        sentinel=str(absolute(args.sentinel)),
        build=BUILD,
        name=args.name,
    )
    # Apply the same schema before any filesystem mutation.
    if not re.fullmatch(r"genesis-cbm-[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise ValueError("invalid managed unit name")
    unit_dir = Path.home() / ".config/systemd/user"
    units = render_units(config, path)
    unit_dir.mkdir(parents=True, exist_ok=True)
    with (unit_dir / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (
            path.exists()
            or path.is_symlink()
            or state.exists()
            or any((unit_dir / u).exists() or (unit_dir / u).is_symlink() for u in units)
        ):
            raise ValueError("existing settings/state/unit preserved; choose a fresh staging state")
        if any(systemctl("show", u, "-p", "LoadState", "--value") != "not-found" for u in units):
            raise ValueError("existing loaded/vendor managed unit preserved")
        with verified_binary(source) as executable:
            state.mkdir(mode=0o700, parents=True)
            (state / "bin").mkdir(mode=0o700)
            executable.seek(0)
            with Path(config["binary"]).open("xb") as destination:
                shutil.copyfileobj(executable, destination)
            Path(config["binary"]).chmod(0o500)
        cache = Path(config["cache"])
        cache.mkdir(mode=0o700)
        Path(config["runtime"]).mkdir(mode=0o700)
        (cache / "config.json").write_text(json.dumps(dict(ui_enabled=False)))
        with verified_binary(Path(config["binary"])) as executable:
            for key in DISABLED_KEYS:
                subprocess.run(
                    [f"/proc/self/fd/{executable.fileno()}", "config", "set", key, "false"],
                    env=native_env(config),
                    pass_fds=(executable.fileno(),),
                    check=True,
                    timeout=30,
                    stdout=subprocess.DEVNULL,
                )
        verify_cache(config)
        for unit, text in units.items():
            (unit_dir / unit).write_text(text)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_settings(path, config)
        systemctl("daemon-reload")
    print("Configured; sentinel unchanged, services not started")


def write_settings(path: Path, config: dict) -> None:
    # Readers see one complete document; never follow/overwrite a config symlink.
    if path.is_symlink():
        raise ValueError("managed settings symlink preserved")
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def set_enabled(config: dict, path: Path, enabled: bool) -> None:
    unit_dir = Path.home() / ".config/systemd/user"
    with (unit_dir / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = read_settings(path)
        if enabled:
            if Path(config["sentinel"]).exists():
                raise ValueError(
                    "machine sentinel is armed; explicit supervised activation required"
                )
            verify_cache(config)
            with verified_binary(Path(config["binary"])):
                pass
        config["enabled"] = enabled
        write_settings(path, config)
        try:
            systemctl("start" if enabled else "stop", backend(config))
        except subprocess.SubprocessError:
            if enabled:
                config["enabled"] = False
                write_settings(path, config)
                try:
                    systemctl("stop", backend(config))
                except subprocess.SubprocessError as stop_error:
                    print(f"rollback stop failed: {stop_error}", file=sys.stderr)
            raise


def launch(config: dict, path: Path) -> None:
    require_enabled(config)
    verify_cache(config)
    check_backend(config)
    unit = config["name"] + "-client-" + uuid.uuid4().hex + ".service"
    command = [
        "/usr/bin/systemd-run",
        "--user",
        "--pipe",
        "--quiet",
        "--collect",
        "--wait",
        "--unit=" + unit,
        "--slice=" + config["name"] + "-clients.slice",
        "-p",
        "Requisite=" + backend(config),
        "-p",
        "After=" + backend(config),
        "-p",
        "StopPropagatedFrom=" + backend(config),
        "-p",
        "KillMode=control-group",
        "-p",
        "MemoryMax=256M",
        "-p",
        "MemorySwapMax=0",
        "-p",
        "TasksMax=32",
        "-p",
        "OOMScoreAdjust=500",
        "--working-directory=" + config["main"],
        "--",
        sys.executable,
        str(Path(__file__).resolve()),
        "--config",
        str(path),
        "client",
        "--unit",
        unit,
    ]
    os.execv(command[0], command)  # noqa: S606 - fixed systemd executable, argv without shell


def batch_values(config: dict, repo: str) -> list[str]:
    require_enabled(config)
    if absolute(repo).resolve(strict=True) != Path(config["main"]):
        raise ValueError("managed indexing is limited to the configured main checkout")
    verify_cache(config)
    with verified_binary(Path(config["binary"])):
        pass
    return [config["binary"], config["cache"], config["runtime"], "8G"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=absolute, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    for key in ("main", "binary", "state", "sentinel"):
        setup.add_argument("--" + key, required=True)
    setup.add_argument("--name", default="genesis-cbm-query")
    commands.add_parser("launch")
    commands.add_parser("serve")
    commands.add_parser("ready")
    child = commands.add_parser("client")
    child.add_argument("--unit", required=True)
    batch = commands.add_parser("batch")
    batch.add_argument("--repo", required=True)
    commands.add_parser("status")
    commands.add_parser("disable")
    commands.add_parser("enable")
    args = parser.parse_args()
    try:
        path = args.config or absolute(
            os.environ.get("CODEBASE_MEMORY_MCP_MANAGED_CONFIG")
            or str(Path.home() / ".genesis/config/codebase-managed.json")
        )
        if args.command == "configure":
            configure(args, path)
            return 0
        if not path.exists() and not path.is_symlink():
            return 2  # absent integration; malformed/unavailable settings are never absent
        config = read_settings(path)
        if args.command == "launch":
            launch(config, path)
        elif args.command in ("serve", "client"):
            execute_native(
                config, args.command, backend(config) if args.command == "serve" else args.unit
            )
        elif args.command == "ready":
            ready(config)
        elif args.command == "batch":
            print("\n".join(batch_values(config, args.repo)))
        elif args.command in ("enable", "disable"):
            set_enabled(config, path, args.command == "enable")
        else:
            print(
                json.dumps(
                    dict(
                        settings=config,
                        service=systemctl("show", backend(config), "-p", "ActiveState", "--value"),
                    ),
                    indent=2,
                )
            )
        return 0
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"managed Codebase refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
