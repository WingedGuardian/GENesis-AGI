#!/usr/bin/env python3
"""Explicit pinned native Codebase lifecycle; no MCP or indexing broker.

The settings route is fail closed once present. Configure does not remove the
machine sentinel or start services. Internal ABI is pinned to the accepted build.
Bootstrap installs disabled native unit templates only for configured installs.
Runtime lifecycle never renders, verifies, repairs or deletes unit fragments.
Settings are updated through a verified descriptor; remove retains the route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from code_intel_cbm_admission import resolve_cgroup  # noqa: E402
from code_intel_cbm_worker import BUILD  # noqa: E402
from codebase_managed_state import lifecycle_lock, read_file, update_settings  # noqa: E402

GIB = 1024**3
DISABLED_KEYS = ("auto_index", "auto_watch", "watcher_enabled")
SCRIPT = Path(__file__).resolve()
NAME = "genesis-cbm-query"  # the installed native unit interface
BACKEND, SLICE = NAME + ".service", NAME + "-clients.slice"
# newline="" keeps bytes exact: a CRLF fragment is foreign to both readers.
UNIT_TEXT = dict(encoding="utf-8", errors="surrogateescape", newline="")


def absolute(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or any(c in raw for c in "\n\r\x00"):
        raise ValueError("invalid managed path")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("managed path must be absolute")
    return path


def config_path(raw: str) -> Path:
    # Canonical directory, literal final component: a settings symlink is still
    # refused rather than followed, while every unit records one spelling.
    path = absolute(raw)
    if not path.name:
        raise ValueError("managed settings path must name a file")
    try:
        return path.parent.resolve() / path.name
    except RuntimeError as error:  # symlink loop
        raise ValueError(f"unresolvable managed settings directory: {error}") from error


def main_script(config: dict) -> Path:
    # Units always run the configured primary checkout's script, whichever
    # checkout renders them, so the template never depends on the caller.
    return Path(config["main"]) / "scripts/codebase_managed.py"


def units_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def sentinel_armed(raw: str) -> bool:
    """False only for a definitely absent sentinel (mirrored in the shell library).

    Only ENOENT clears it, and only when no component above it is a broken link
    (a lost mount target hides a sentinel that may still be there). A dangling
    link at the sentinel itself, or any other lookup error, keeps it armed.
    """
    try:
        os.lstat(raw)
    except FileNotFoundError:
        return any(os.path.lexists(p) and not os.path.exists(p) for p in Path(raw).parents)
    except OSError:
        return True
    return True


def read_settings(path: Path, *, require_build: bool = True) -> dict:
    value = json.loads(read_file(path))
    if (
        not isinstance(value, dict)
        or type(value.get("version")) is not int
        or value.get("version") != 1
        or type(value.get("enabled")) is not bool
    ):
        raise ValueError("invalid managed settings")
    for key in ("main", "binary", "cache", "runtime", "sentinel"):
        absolute(value.get(key))
    # The pin gates running the executable. Disabling and retiring never run it,
    # so settings written by an older accepted build can still be switched off.
    if require_build and value.get("build") != BUILD:
        raise ValueError("unsupported managed build")
    return value


def require_enabled(config: dict) -> None:
    if not config["enabled"] or sentinel_armed(config["sentinel"]):
        raise ValueError("managed Codebase is disabled")


def verified_binary(path: Path):
    if not path.is_absolute() or not os.access(path, os.X_OK):
        raise ValueError("managed executable unavailable")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    stream = os.fdopen(fd, "rb")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("managed executable must be a regular file")
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
    startup = args[0] in ("start", "restart", "enable")
    try:
        return subprocess.check_output(
            ["/usr/bin/systemctl", "--user", *args], text=True,
            timeout=130 if startup else 40 if args[0] in ("stop", "disable") else 30,
        ).strip()
    except subprocess.CalledProcessError as error:
        if args[0] == "is-enabled" and error.returncode in (1, 4) and error.output.strip():
            return error.output.strip()
        raise


def show(unit: str, *properties: str) -> dict[str, str]:
    # One manager query. Explicitly requested properties are printed even when
    # empty, in the manager's own order, so parse names rather than positions.
    output = systemctl("show", unit, *(arg for name in properties for arg in ("-p", name)))
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if any(name not in values for name in properties):
        raise ValueError(f"systemd omitted requested properties for {unit}")
    return values


def check_backend(config: dict, *, starting: bool = False) -> None:
    unit = BACKEND
    if systemctl("show", unit, "-p", "ActiveState", "--value") not in (
        ("active", "activating") if starting else ("active",)
    ):
        raise ValueError("managed daemon unavailable; restore service and reconnect")
    pid = int(systemctl("show", unit, "-p", "MainPID", "--value"))
    if pid <= 0 or not os.path.samefile(f"/proc/{pid}/exe", config["binary"]):
        raise ValueError("managed daemon identity mismatch")
    leaf, _, version = resolve_cgroup(Path(f"/proc/{pid}/cgroup"), Path(f"/proc/{pid}/mountinfo"))
    if (
        version != 2
        or (leaf / "memory.max").read_text().strip() != str(2 * GIB)
        or (leaf / "memory.swap.max").read_text().strip() != "0"
    ):
        raise ValueError("managed daemon lacks required memory/zero-swap cap")
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
                pid = systemctl("show", BACKEND, "-p", "MainPID", "--value")
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
        re.escape(NAME) + r"-client-[0-9a-f]{32}\.service", unit
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
        if parent.name != SLICE or (parent / "memory.max").read_text().strip() != str(2 * GIB):
            raise ValueError("managed frontend aggregate cap unavailable")
        if (parent / "memory.swap.max").read_text().strip() != "0":
            raise ValueError("managed frontend aggregate swap cap unavailable")
    # cgroup v2 limits may be over-committed: "the sum of the limits of children
    # can exceed the amount of resource available to the parent" (kernel
    # admin-guide cgroup-v2, Resource Distribution Models: Limits). So every
    # ancestor must admit the whole 2 GiB budget: the service's own cap, or the
    # client slice's aggregate, never just one 256 MiB client. The bound is per
    # budget; the two budgets together (4 GiB) are not required of a shared
    # ancestor.
    cursor = leaf.parent
    while cursor == root or root in cursor.parents:
        try:
            maximum = (cursor / "memory.max").read_text().strip()
        except FileNotFoundError:
            # The actual cgroup v2 filesystem root has no memory.max. A
            # namespaced container root can have one, and it remains binding.
            if cursor != root:
                raise
            maximum = "max"
        if maximum != "max" and int(maximum) < 2 * GIB:
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
    os.chdir(config["main"])
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


def configure(args: argparse.Namespace, path: Path) -> None:
    main, source, state = (
        absolute(args.main).resolve(strict=True),
        absolute(args.binary),
        absolute(args.state).resolve(),
    )
    if not (main / ".git").is_dir():
        raise ValueError("managed Codebase requires the primary checkout")
    # Units pin this script's path. A linked worktree (even one nested inside the
    # primary checkout) is archived or reaped, which would strand the service.
    if main_script(dict(main=str(main))) != SCRIPT:
        raise ValueError(
            "run configure with scripts/codebase_managed.py from the configured primary checkout"
        )
    config = dict(
        version=1,
        enabled=False,
        main=str(main),
        binary=str(state / "bin" / BUILD),
        cache=str(state / "cache"),
        runtime=str(state / "runtime"),
        sentinel=str(absolute(args.sentinel)),
        build=BUILD,
    )
    default = Path.home() / ".genesis/config/codebase-managed.json"
    if path != config_path(str(default)):
        raise ValueError("configure requires the default settings path used by the installed service")
    required = (main, state, units_dir(), state / "bin", state / "cache", state / "runtime")
    if any(path == p or path in p.parents or p == state and p in path.parents for p in required):
        raise ValueError("managed settings/state directory topology overlaps; preserved")
    unit_dir = units_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    with lifecycle_lock(unit_dir):
        # lexists: a dangling link is an existing artifact, never a free path
        # (a --state link to an unmounted target would otherwise be created).
        if any(
            os.path.lexists(p)
            for p in (path, absolute(args.state), state)
        ):
            raise ValueError("existing settings/state/unit preserved; choose a fresh staging state")
        # Retain staging on failure. Pathnames may have changed ownership;
        # ordinary bootstrap owns unit installation and migration.
        try:
            with verified_binary(source) as executable:
                state.mkdir(mode=0o700, parents=True)
                (state / "bin").mkdir(mode=0o700)
                executable.seek(0)
                with Path(config["binary"]).open("xb") as destination:
                    shutil.copyfileobj(executable, destination)
                    os.fchmod(destination.fileno(), 0o500)
            cache = Path(config["cache"])
            cache.mkdir(mode=0o700)
            Path(config["runtime"]).mkdir(mode=0o700)
            with (cache / "config.json").open("x") as ui:
                ui.write(json.dumps(dict(ui_enabled=False)))
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
            path.parent.mkdir(parents=True, exist_ok=True)
            # Commit point, no-clobber and last: a file another process put there
            # meanwhile is neither replaced nor, since the path is never in
            # `created`, removed by the rollback. Should the directory sync fail
            # after the link, the published settings stay for `remove`.
            write_settings(path, config)
        except BaseException:
            print(f"Incomplete staging retained at {state}; inspect before retrying", file=sys.stderr)
            raise
    print("Configured disabled; run bootstrap to install native templates, then explicitly enable")


def write_settings(path: Path, config: dict, *, replace: bool = False) -> None:
    if replace:
        update_settings(path, config)
    else:
        atomic_write(path, json.dumps(config, indent=2) + "\n")


def atomic_write(path: Path, text: str) -> None:
    """Publish initial settings without clobbering an existing pathname."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x", **UNIT_TEXT) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def stop_backend() -> None:
    # --now does not stop if the unit-file operation fails. Always attempt the
    # runtime stop independently; retain failure of either operation.
    error = None
    for args in (("disable", "--now", BACKEND), ("stop", BACKEND)):
        try:
            systemctl(*args)
        except (OSError, subprocess.SubprocessError) as exc:
            if error is None:
                error = exc
    if error is not None:
        raise error


def disable(path: Path | None) -> None:
    """Stop the installed backend even with missing, malformed or old settings.

    Systemd owns the named integration. No client-slice proof gates its stop.
    Disabled installed units remain evidence of configured managed selection.
    """
    unit_dir = units_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    with lifecycle_lock(unit_dir):
        try:
            if path is None:
                raise ValueError("managed settings directory cannot be resolved; preserved")
            config = read_settings(path, require_build=False)
        except (OSError, ValueError) as error:
            print(f"Settings unreadable; execution refuses: {error}", file=sys.stderr)
        else:
            config["enabled"] = False
            try:
                write_settings(path, config, replace=True)
            except (OSError, ValueError):
                stop_backend()
                raise
        stop_backend()
        stopped = show(BACKEND, "ActiveState", "ControlGroup")
        if stopped["ActiveState"] not in ("inactive", "failed") or stopped["ControlGroup"]:
            raise ValueError("backend still has live state; settings and cache retained")


def set_enabled(config: dict, path: Path, enabled: bool) -> None:
    if not enabled:
        disable(path)
        return
    unit_dir = units_dir()
    with lifecycle_lock(unit_dir):
        config = read_settings(path)
        if sentinel_armed(config["sentinel"]):
            raise ValueError("machine sentinel is armed; explicit supervised activation required")
        verify_cache(config)
        with verified_binary(Path(config["binary"])):
            pass
        config["enabled"] = True
        write_settings(path, config, replace=True)
        try:
            systemctl("enable", "--now", BACKEND)
            # Some managers return success for the enable operation despite a
            # failed --now start. Require actual native readiness as well.
            ready(config)
        except BaseException:
            config["enabled"] = False
            try:
                write_settings(path, config, replace=True)
            except Exception as error:  # noqa: BLE001 - original startup failure wins
                print(f"rollback could not persist enabled=false: {error}", file=sys.stderr)
            try:
                stop_backend()
            except Exception as error:  # noqa: BLE001 - independent rollback
                print(f"rollback stop failed: {error}", file=sys.stderr)
            raise


def remove(path: Path | None) -> None:
    disable(path)
    print("Managed runtime disabled; settings, installed templates and state retained. "
          "Raw fallback requires explicit operator maintenance")


def launch(config: dict, path: Path) -> None:
    require_enabled(config)
    verify_cache(config)
    check_backend(config)
    unit = NAME + "-client-" + uuid.uuid4().hex + ".service"
    command = [
        "/usr/bin/systemd-run",
        "--user",
        "--pipe",
        "--quiet",
        "--collect",
        "--wait",
        # Manager-side ${VAR} expansion would rewrite path arguments (v255 man
        # systemd-run, --expand-environment); argv must reach the client literally.
        "--expand-environment=no",
        "--unit=" + unit,
        "--slice=" + SLICE,
        "-p",
        "Requisite=" + BACKEND,
        "-p",
        "After=" + BACKEND,
        "-p",
        "StopPropagatedFrom=" + BACKEND,
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
        "--working-directory=/",
        "--",
        "/usr/bin/python3",
        "-I",
        str(main_script(config)),
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
    commands.add_parser("remove")
    args = parser.parse_args()
    try:
        # An empty override means unset, exactly as the shell callers treat it.
        raw = (
            str(args.config)
            if args.config is not None
            else os.environ.get("CODEBASE_MEMORY_MCP_MANAGED_CONFIG")
            or str(Path.home() / ".genesis/config/codebase-managed.json")
        )
        if args.command in ("disable", "remove"):
            try:
                path = config_path(raw)
            except (OSError, ValueError, RuntimeError) as error:
                print(f"Settings path unresolvable; preserved: {error}", file=sys.stderr)
                path = None
            (disable if args.command == "disable" else remove)(path)
            return 0
        path = config_path(raw)
        if args.command == "configure":
            configure(args, path)
            return 0
        # Route selection happens in the shell callers. Every command reached
        # here was selected or explicit, so it requires readable settings. The
        # build pin gates running the executable; disable and status never do.
        config = read_settings(path, require_build=args.command not in ("disable", "status"))
        if args.command == "launch":
            launch(config, path)
        elif args.command in ("serve", "client"):
            execute_native(config, args.command, BACKEND if args.command == "serve" else args.unit)
        elif args.command == "ready":
            ready(config)
        elif args.command == "batch":
            print("\n".join(batch_values(config, args.repo)))
        elif args.command == "enable":
            set_enabled(config, path, True)
        else:
            print(
                json.dumps(
                    dict(
                        settings=config,
                        service=systemctl("show", BACKEND, "-p", "ActiveState", "--value"),
                        enabled=systemctl("is-enabled", BACKEND),
                    ),
                    indent=2,
                )
            )
        return 0
    # RuntimeError: Path.resolve on a symlink loop (Python 3.12).
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"managed Codebase refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
