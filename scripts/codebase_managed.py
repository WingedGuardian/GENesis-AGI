#!/usr/bin/env python3
"""Explicit pinned native Codebase lifecycle; no MCP or indexing broker.

The settings route is fail closed once present. Configure does not remove the
machine sentinel or start services. Internal ABI is pinned to the accepted build.
Route selection is derived by scripts/lib/codebase_managed_selection.sh from the
override, the settings path and the owned unit fragments rendered below; remove
retires the route by deleting exactly those owned fragments and the settings.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
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

GIB = 1024**3
DISABLED_KEYS = ("auto_index", "auto_watch", "watcher_enabled")
# The first two lines of every generated fragment are its ownership record. The
# shell selection library matches them byte for byte; change both together.
MARKER = "# Genesis managed Codebase v1\n"
CONFIG_LINE = "# Genesis managed config: "
SCRIPT = Path(__file__).resolve()
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


def read_file(path: Path) -> tuple[str, tuple[int, int]]:
    """Text and (device, inode) of a regular file, never following a final link."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(f"managed symlink refused: {path}") from error
        raise
    with open(fd, **UNIT_TEXT) as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"managed path is not a regular file: {path}")
        return stream.read(), (info.st_dev, info.st_ino)


def units_dir() -> Path:
    return Path.home() / ".config/systemd/user"


def owned_fragment(fragment: Path, path: Path) -> tuple[str, tuple[int, int]] | None:
    """Text and identity of a regular generated fragment of this configuration."""
    try:
        text, identity = read_file(fragment)
    except (FileNotFoundError, ValueError):
        return None
    return (text, identity) if text.startswith(MARKER + CONFIG_LINE + str(path) + "\n") else None


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


def load_settings(path: Path, *, require_build: bool = True) -> tuple[dict, tuple[int, int]]:
    text, identity = read_file(path)
    value = json.loads(text)
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
    # The pin gates running the executable. Disabling and retiring never run it,
    # so settings written by an older accepted build can still be switched off.
    if require_build and value.get("build") != BUILD:
        raise ValueError("unsupported managed build")
    return value, identity


def read_settings(path: Path) -> dict:
    return load_settings(path)[0]


def require_enabled(config: dict) -> None:
    if not config["enabled"] or sentinel_armed(config["sentinel"]):
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


def show(unit: str, *properties: str) -> dict[str, str]:
    # One manager query. Explicitly requested properties are printed even when
    # empty, in the manager's own order, so parse names rather than positions.
    output = systemctl("show", unit, *(arg for name in properties for arg in ("-p", name)))
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if any(name not in values for name in properties):
        raise ValueError(f"systemd omitted requested properties for {unit}")
    return values


def same_file(loaded: str, fragment: Path) -> bool:
    try:
        return bool(loaded) and os.path.samefile(loaded, fragment)
    except OSError:
        return False


def check_backend(config: dict, *, starting: bool = False) -> None:
    unit = backend(config)
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
    # cgroup v2 limits may be over-committed: "the sum of the limits of children
    # can exceed the amount of resource available to the parent" (kernel
    # admin-guide cgroup-v2, Resource Distribution Models: Limits). So every
    # ancestor must admit the whole 2 GiB budget: the service's own cap, or the
    # client slice's aggregate, never just one 256 MiB client. The bound is per
    # budget; the two budgets together (4 GiB) are not required of a shared
    # ancestor.
    cursor = leaf.parent
    while cursor == root or root in cursor.parents:
        maximum = (cursor / "memory.max").read_text().strip()
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


def quote_unit(value: str) -> str:
    absolute(value) if value.startswith("/") else None
    if any(c in value for c in "\n\r\x00"):
        raise ValueError("invalid unit argument")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_units(config: dict, path: Path) -> dict[str, str]:
    # Set the exact checkout directory in execute_native via os.chdir. Unit
    # path parsing must not change whitespace/backslash spellings.
    command = " ".join(
        quote_unit(x)
        for x in [
            "/usr/bin/python3",
            "-I",
            str(main_script(config)),
            "--config",
            str(path),
            "serve",
        ]
    )
    owner = MARKER + CONFIG_LINE + str(path) + "\n"
    return {
        backend(config): owner
        + f"""[Unit]
Description=Genesis pinned native Codebase query daemon
[Service]
Type=exec
WorkingDirectory=/
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
        config["name"] + "-clients.slice": owner
        + """[Unit]
Description=Genesis managed Codebase frontend aggregate
[Slice]
MemoryAccounting=yes
MemoryMax=2G
MemorySwapMax=0
TasksMax=512
""",
    }


def unit_available(unit: str) -> bool:
    # A running unit can become not-found after deletion and manager reload.
    if systemctl("show", unit, "-p", "ActiveState", "--value") != "inactive" or systemctl(
        "show", unit, "-p", "ControlGroup", "--value"
    ):
        return False
    if systemctl("show", unit, "-p", "LoadState", "--value") == "not-found":
        return True
    # systemd synthesizes empty slice units on lookup. Preserve every explicit,
    # active or modified unit, while allowing this inactive default-only object.
    defaults = {
        "FragmentPath": "",
        "DropInPaths": "",
        "Transient": "no",
        "ActiveState": "inactive",
        "ControlGroup": "",
        "MemoryMax": "infinity",
        "MemorySwapMax": "infinity",
        "TasksMax": "infinity",
        "CPUQuotaPerSecUSec": "infinity",
    }
    return unit.endswith(".slice") and all(
        systemctl("show", unit, "-p", key, "--value") == value for key, value in defaults.items()
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
        name=args.name,
    )
    # Apply the same schema before any filesystem mutation.
    if not re.fullmatch(r"genesis-cbm-[a-z0-9]+(?:-[a-z0-9]+)*", args.name):
        raise ValueError("invalid managed unit name")
    unit_dir = units_dir()
    units = render_units(config, path)
    unit_dir.mkdir(parents=True, exist_ok=True)
    with (unit_dir / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # lexists: a dangling link is an existing artifact, never a free path
        # (a --state link to an unmounted target would otherwise be created).
        if any(
            os.path.lexists(p)
            for p in (path, absolute(args.state), state, *(unit_dir / u for u in units))
        ):
            raise ValueError("existing settings/state/unit preserved; choose a fresh staging state")
        if any(not unit_available(u) for u in units):
            raise ValueError("existing loaded/vendor managed unit preserved")
        # Everything below was verified absent under this lock, so a failure
        # removes exactly what this run created: no partial configuration may
        # select the managed route or block a retry with the same arguments.
        created: list[Path] = []
        try:
            with verified_binary(source) as executable:
                state.mkdir(mode=0o700, parents=True)
                created.append(state)
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
                with (unit_dir / unit).open("x", **UNIT_TEXT) as stream:
                    created.append(unit_dir / unit)  # only once this run owns it
                    stream.write(text)
            systemctl("daemon-reload")
            path.parent.mkdir(parents=True, exist_ok=True)
            # Commit point, no-clobber and last: a file another process put there
            # meanwhile is neither replaced nor, since the path is never in
            # `created`, removed by the rollback. Should the directory sync fail
            # after the link, the published settings stay for `remove`.
            write_settings(path, config)
        except BaseException:
            rollback_configure(created, unit_dir, state)
            raise
    print("Configured; sentinel unchanged, services not started")


def rollback_configure(created: list[Path], unit_dir: Path, state: Path) -> None:
    for item in reversed(created):
        try:
            if item == state and item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)  # created by this run with mkdir, never pre-existing
            elif item.is_file() and not item.is_symlink():
                item.unlink()
            elif item.exists() or item.is_symlink():
                # This run creates no links or other directories; another actor did.
                print(f"configure rollback preserved unexpected {item}", file=sys.stderr)
        except OSError as error:
            print(f"configure rollback could not remove {item}: {error}", file=sys.stderr)
    if any(item.parent == unit_dir for item in created):
        try:
            systemctl("daemon-reload")
        except (OSError, subprocess.SubprocessError) as error:
            print(f"configure rollback daemon-reload failed: {error}", file=sys.stderr)


def write_settings(path: Path, config: dict, expected=None) -> tuple[int, int]:
    return atomic_write(path, json.dumps(config, indent=2) + "\n", expected)


def atomic_write(path: Path, text: str, expected=None) -> tuple[int, int]:
    """Publish one complete document and return its (device, inode).

    With no ``expected`` identity the path must not exist: the link fails
    rather than clobber a file created concurrently. Otherwise the path must
    still be the inode that was verified. A symlink is never followed.
    """
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        with temporary.open("x", **UNIT_TEXT) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
            info = os.fstat(stream.fileno())
        if expected is None:
            os.link(temporary, path)
        else:
            current = os.lstat(path)
            if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != expected:
                raise ValueError(f"{path} changed after it was verified; preserved")
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return info.st_dev, info.st_ino


def remove_verified(path: Path, identity: tuple[int, int]) -> None:
    current = os.lstat(path)
    if (current.st_dev, current.st_ino) != identity:
        raise ValueError(f"{path} changed after it was verified; preserved")
    path.unlink()


def verify_units(config: dict, path: Path, *, exact: bool = True, only=None) -> dict:
    """Prove each generated unit is ours and is what the manager loaded.

    ``exact`` additionally requires the current template, which only ``enable``
    asks for. ``only`` limits the proof to those units. Returns each fragment's
    verified (device, inode).
    """
    unit_dir = units_dir()
    identities = {}
    for unit, expected in render_units(config, path).items():
        if only is not None and unit not in only:
            continue
        fragment = unit_dir / unit
        owned = owned_fragment(fragment, path)
        if not owned:
            raise ValueError(f"{unit} is not a generated fragment of this configuration")
        if exact and owned[0] != expected:
            raise ValueError(
                f"{unit} differs from this checkout's template; from the primary "
                "checkout run `codebase_managed.py disable` then `codebase_managed.py repair-units`"
            )
        loaded = show(unit, "FragmentPath", "DropInPaths", "NeedDaemonReload")
        if not same_file(loaded["FragmentPath"], fragment):
            raise ValueError(f"{unit} loaded fragment is not the generated fragment")
        if loaded["DropInPaths"]:
            raise ValueError(f"{unit} has unverified drop-ins")
        if loaded["NeedDaemonReload"] != "no":
            raise ValueError(f"{unit} load is stale; run systemctl --user daemon-reload")
        identities[unit] = owned[1]
    return identities


def template_drift(config: dict, path: Path) -> dict[str, str]:
    """Per unit: current, drift or not owned. Reported by status, never fatal."""
    report = {}
    for unit, expected in render_units(config, path).items():
        try:
            owned = owned_fragment(units_dir() / unit, path)
        except OSError as error:
            report[unit] = f"unreadable: {error.strerror}"
            continue
        if not owned:
            report[unit] = "not owned"
        elif owned[0] == expected:
            report[unit] = "current"
        else:
            report[unit] = "drift: enable refuses until disable, then repair-units"
    return report


def set_enabled(config: dict, path: Path, enabled: bool) -> None:
    with (units_dir() / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config, identity = load_settings(path, require_build=enabled)
        if not enabled:
            # The operator's stop lever: persist first, so launch and batch
            # refuse even when the unit cannot be proven ours and is not stopped.
            config["enabled"] = False
            write_settings(path, config, identity)
            try:
                verify_units(config, path, exact=False)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise ValueError(
                    f"settings disabled, but {backend(config)} was NOT stopped: {error}. "
                    "Inspect the unit before stopping it by hand"
                ) from error
            systemctl("stop", backend(config))
            return
        verify_units(config, path)
        if sentinel_armed(config["sentinel"]):
            raise ValueError("machine sentinel is armed; explicit supervised activation required")
        verify_cache(config)
        with verified_binary(Path(config["binary"])):
            pass
        config["enabled"] = True
        identity = write_settings(path, config, identity)
        try:
            systemctl("start", backend(config))
        except BaseException:
            # Each rollback step is attempted whatever the other does, and the
            # start failure is what the caller sees.
            config["enabled"] = False
            try:
                write_settings(path, config, identity)
            except Exception as error:  # noqa: BLE001 - reported; start error wins
                print(f"rollback could not persist enabled=false: {error}", file=sys.stderr)
            try:
                systemctl("stop", backend(config))
            except Exception as stop_error:  # noqa: BLE001 - reported; start error wins
                print(f"rollback stop failed: {stop_error}", file=sys.stderr)
            raise


def repair_units(path: Path) -> None:
    """Re-render owned fragments after a template change; never adopts others."""
    unit_dir = units_dir()
    with (unit_dir / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = read_settings(path)
        # Units pin the primary checkout's script, but the template text comes
        # from whichever copy runs this; a branch copy must not rewrite them.
        if main_script(config) != SCRIPT:
            raise ValueError("run repair-units from the configured primary checkout")
        units = render_units(config, path)
        state = show(backend(config), "ActiveState", "ControlGroup")
        # Restart=no leaves a crashed or rolled-back daemon "failed", which stop
        # does not clear; with no control group left it is as idle as inactive.
        if state["ActiveState"] not in ("inactive", "failed") or state["ControlGroup"]:
            raise ValueError(f"{backend(config)} must be inactive; run disable first")
        owned = {}
        for unit in units:  # verify every fragment before rewriting any
            fragment = unit_dir / unit
            owned[unit] = owned_fragment(fragment, path)
            if not owned[unit]:
                raise ValueError(f"{unit} is not a generated fragment of this configuration")
            if not same_file(show(unit, "FragmentPath")["FragmentPath"], fragment):
                raise ValueError(f"{unit} loaded fragment is not the generated fragment")
        for unit, text in units.items():
            if owned[unit][0] != text:  # replaces only the inode verified above
                atomic_write(unit_dir / unit, text, owned[unit][1])
        systemctl("daemon-reload")
    print("Units repaired; services not started")


def remove(path: Path) -> None:
    """Retire the managed route: owned units, then settings. State and sentinel stay.

    Settings are disabled first, so launch and batch refuse whatever follows.
    Nothing that cannot be proven this configuration's is stopped or deleted.
    Re-runnable after an interruption: fragments already gone must belong to
    units the manager no longer runs, and only the remaining ones are acted on.
    """
    unit_dir = units_dir()
    with (unit_dir / ".genesis-codebase-config.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        config, identity = load_settings(path, require_build=False)
        config["enabled"] = False
        identity = write_settings(path, config, identity)
        units = render_units(config, path)
        present = [unit for unit in units if os.path.lexists(unit_dir / unit)]
        if len(present) < len(units):
            systemctl("daemon-reload")  # forget fragments deleted before this run
            if not all(unit_available(unit) for unit in units if unit not in present):
                raise ValueError("a managed unit whose fragment is gone is still loaded or running")
        if present:
            fragments = verify_units(config, path, exact=False, only=present)
            if backend(config) in present:
                systemctl("disable", backend(config))  # drops any boot enablement link
            systemctl("stop", *present)
            for unit, fragment_identity in fragments.items():
                remove_verified(unit_dir / unit, fragment_identity)
            systemctl("daemon-reload")
        remove_verified(path, identity)
    print(f"Managed route removed; state under {Path(config['cache']).parent} and sentinel kept")


def launch(config: dict, path: Path) -> None:
    require_enabled(config)
    # Ownership, loaded fragment, drop-ins and load freshness, not the exact
    # template: a template change must not take every configured MCP down.
    # The limits are proven live by check_backend here and verify_boundary in
    # the client (memory.max, swap and cgroup membership).
    verify_units(config, path, exact=False)
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
    commands.add_parser("repair-units")
    commands.add_parser("remove")
    args = parser.parse_args()
    try:
        # An empty override means unset, exactly as the shell callers treat it.
        path = config_path(
            str(args.config)
            if args.config is not None
            else os.environ.get("CODEBASE_MEMORY_MCP_MANAGED_CONFIG")
            or str(Path.home() / ".genesis/config/codebase-managed.json")
        )
        if args.command == "configure":
            configure(args, path)
            return 0
        if args.command == "repair-units":
            repair_units(path)
            return 0
        if args.command == "remove":
            remove(path)
            return 0
        # Route selection happens in the shell callers. Every command reached
        # here was selected or explicit, so it requires readable settings.
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
                        units=template_drift(config, path),
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
