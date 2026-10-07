#!/usr/bin/env python3
"""Stage immutable Codebase settings and run its bounded native query service.

Configure does not activate providers, index repositories or remove the machine
sentinel. Serve/ready are native unit entry points requiring persistent enablement.
Configuration is published once. Native enablement owns operational state.
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
import stat
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from code_intel_cbm_admission import (  # noqa: E402
    _host_available,
    _mount_path,
    _working_charge,
    number,
    read_number,
    resolve_cgroup,
)
from code_intel_cbm_worker import BUILD  # noqa: E402
from codebase_managed_unit import loaded_properties, validate_backend  # noqa: E402,F401

SCRIPT = Path(__file__).resolve()
BACKEND = "genesis-cbm-query.service"
SLICE = "genesis-cbm-query-clients.slice"
DISABLED_KEYS = ("auto_index", "auto_watch", "watcher_enabled")
PATH_KEYS = ("main", "binary", "cache", "runtime", "sentinel")
OPEN_FLAGS = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def absolute(raw: str) -> Path:
    if not isinstance(raw, str) or not raw or any(c in raw for c in "\n\r\x00"):
        raise ValueError("invalid managed path")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError("managed path must be absolute")
    return path


def config_path(raw: str) -> Path:
    path = absolute(raw)
    if not path.name:
        raise ValueError("settings path must name a file")
    return absolute(str(path.parent.resolve() / path.name))


def units_dir() -> Path:
    return Path.home() / ".config/systemd/user"


@contextmanager
def file_lock(path: Path, *, shared: bool = False):
    """Coordinate cooperating tools; never replace/delete the lock pathname."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | OPEN_FLAGS, 0o600)
    with os.fdopen(fd, "a") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("lifecycle lock must be regular")
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        yield stream


def lifecycle_lock(*, shared: bool = False):
    return file_lock(units_dir() / ".genesis-codebase-config.lock", shared=shared)


def uninstall_lock_paths() -> tuple[Path, Path, Path]:
    main = (Path.home() / "genesis").resolve(strict=True)
    if main != SCRIPT.parent.parent or (main / ".git").is_file():
        raise ValueError("uninstall requires the installed primary checkout")
    directory = Path(os.environ.get("GENESIS_HOME") or str(Path.home() / ".genesis")) / "locks"
    absolute(str(directory))
    roots = [Path.home() / name for name in ("genesis", ".genesis", "data", ".qdrant")]
    default = Path.home() / ".genesis/locks"
    protected = [units_dir()]
    if directory == default:
        if directory.parent.is_symlink() or directory.is_symlink():
            raise ValueError("uninstall refuses symlinked default lock namespace")
    else:
        protected.append(directory)
    for location in protected:
        for root in roots:
            for namespace, deletion in ((location, root), (location.resolve(), root.resolve())):
                if namespace.is_relative_to(deletion) or deletion.is_relative_to(namespace):
                    raise ValueError("uninstall refuses lock namespace overlapping deletion roots")
    digest = hashlib.sha1(os.fsencode(main), usedforsecurity=False).hexdigest()[:16]
    return (
        directory / "code-intel-runner.lock",
        directory / f"code-intel-{digest}.lock",
        units_dir() / ".genesis-codebase-config.lock",
    )


def verify_uninstall_locks(fds: list[int]) -> None:
    if len(set(fds)) != 3 or any(fd <= 2 for fd in fds):
        raise ValueError("uninstall requires three inherited lock descriptors")
    for fd, path in zip(fds, uninstall_lock_paths(), strict=True):
        held, named = os.fstat(fd), path.stat(follow_symlinks=False)
        if not stat.S_ISREG(held.st_mode) or (held.st_dev, held.st_ino) != (
            named.st_dev,
            named.st_ino,
        ):
            raise ValueError("uninstall lock identity mismatch")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def cgroup_empty(control: str) -> None:
    if not control:
        return
    path = absolute(control)
    if ".." in path.parts:
        raise ValueError("invalid managed ControlGroup")
    mountinfo = Path("/proc/self/mountinfo")
    _, root, version = resolve_cgroup(Path("/proc/self/cgroup"), mountinfo)
    if version != 2:
        raise ValueError("uninstall requires visible cgroup v2 state")
    mappings = []
    for row in mountinfo.read_text().splitlines():
        left, separator, right = row.partition(" - ")
        fields = left.split()
        if (
            separator
            and right.split()[:1] == ["cgroup2"]
            and len(fields) >= 5
            and _mount_path(fields[4]) == root
        ):
            mappings.append(_mount_path(fields[3]))
    if len(set(mappings)) != 1:
        raise ValueError("ambiguous managed cgroup mount")
    mounted = mappings[0]
    relative = path.relative_to(mounted) if path.is_relative_to(mounted) else path.relative_to("/")
    group = root / relative
    try:
        values = dict(line.split() for line in (group / "cgroup.events").read_text().splitlines())
    except FileNotFoundError:
        if group.exists():
            raise
        return  # stopped group has been removed from the visible hierarchy
    if values.get("populated") != "0":
        raise ValueError("managed cgroup still contains processes (including descendants)")


def require_quiescent(unit: str) -> None:
    properties = ["ActiveState", "ControlGroup"]
    if unit.endswith(".service"):
        properties.append("MainPID")
    value = show(unit, *properties)
    if value["ActiveState"] not in ("inactive", "failed") or (
        unit.endswith(".service") and number(value["MainPID"], "MainPID")
    ):
        raise ValueError(f"uninstall refuses nonquiescent {unit}")
    cgroup_empty(value["ControlGroup"])


def manager_absent(unit: str) -> bool:
    expected = {"LoadState": "not-found", "ActiveState": "inactive", "ControlGroup": ""}
    if unit.endswith(".service"):
        expected["MainPID"] = "0"
    return show(unit, *expected) == expected


def index_writer_targets_main(argv: list[str], cwd: Path, main: Path) -> bool:
    """Recognize repository targets of the existing wrapper and native modes."""
    entrypoint = main / "scripts/lib/code_intel_index.sh"
    for index, arg in enumerate(argv[:-1]):
        if Path(arg).name != "code_intel_index.sh":
            continue
        candidate = Path(arg) if Path(arg).is_absolute() else cwd / arg
        if candidate.resolve() == entrypoint and (cwd / argv[index + 1]).resolve() == main:
            return True
    # The private OOM launcher contains both wrapper and native argv. Its fixed
    # flag is not a repository argument; native classification must still run.
    if "analyze" in argv and any("gitnexus" in Path(arg).parts for arg in argv):
        tail = argv[argv.index("analyze") + 1 :]
        target = cwd / tail[0] if tail and not tail[0].startswith("-") else cwd
        return target.resolve() == main
    return False


def observed_index_writers(proc: Path = Path("/proc")) -> list[int]:
    """Observe existing lockless/scopeless writers; never signal a process."""
    main = SCRIPT.parent.parent
    writers = []
    for process in proc.iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            # proc_pid_stat(5): comm can contain ')' and arbitrary filename bytes;
            # starttime is field 22, after the final parenthesized comm delimiter.
            started = process.joinpath("stat").read_text(errors="surrogateescape").rsplit(")", 1)[1].split()[19]
            argv = [os.fsdecode(arg) for arg in process.joinpath("cmdline").read_bytes().split(b"\0") if arg]
            wrapper = any(Path(arg).name == "code_intel_index.sh" for arg in argv)
            native = "analyze" in argv and any("gitnexus" in Path(arg).parts for arg in argv)
            if not (wrapper or native):
                continue
            cwd = process.joinpath("cwd").resolve(strict=True)
            recognized = index_writer_targets_main(argv, cwd, main)
            if process.joinpath("stat").read_text(errors="surrogateescape").rsplit(")", 1)[1].split()[19] != started:
                raise ValueError(f"index writer PID {process.name} changed during observation")
            if recognized:
                writers.append(int(process.name))
        except FileNotFoundError:
            if process.exists():
                raise ValueError(f"cannot prove index writer identity for PID {process.name}") from None
            continue  # process disappeared during observation
        except (OSError, RuntimeError, IndexError) as error:
            raise ValueError(f"cannot prove index writer identity for PID {process.name}") from error
    return writers


def require_no_batch() -> None:
    writers = observed_index_writers()
    if writers:
        raise ValueError(f"uninstall refuses observed index writers: {writers}")
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "list-units",
            "--all",
            "--type=scope",
            "--plain",
            "--no-legend",
            "--no-pager",
            "code-intel-*.scope",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    for row in result.stdout.splitlines():
        unit = row.split()[0]
        if not re.fullmatch(r"code-intel-[0-9a-f]{12}-(?:cbm|gitnexus)-[0-9]+\.scope", unit):
            raise ValueError("unrecognized code-intel scope; cannot prove writer absence")
        require_quiescent(unit)


def retire_managed() -> None:
    errors = []
    for argv in (
        ("disable", BACKEND),
        ("disable", "--runtime", BACKEND),
        ("stop", BACKEND),
        ("stop", SLICE),
    ):
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "--user", *argv],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode and not manager_absent(argv[-1]):
                errors.append((argv[0], result.stderr.strip()))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(("command", f"{argv[0]}: {error}"))
    for unit in (BACKEND, SLICE):
        try:
            require_quiescent(unit)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(("proof", f"{unit}: {error}"))
    try:
        state = show(BACKEND, "UnitFileState", "LoadState")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        errors.append(("proof", str(error)))
        state = {"UnitFileState": "", "LoadState": ""}
    safe = state["UnitFileState"] in ("generated", "transient", "static", "disabled", "masked")
    absent = state == {"UnitFileState": "", "LoadState": "not-found"}
    if any(action != "disable" for action, _ in errors) or not (safe or absent):
        raise ValueError(f"managed retirement failed: errors={errors}, state={state}")


def runtime_config(path: Path) -> dict:
    config = read_settings(path)
    main = absolute(config["main"]).resolve(strict=True)
    if main != SCRIPT.parent.parent or not (main / ".git").is_dir():
        raise ValueError("managed runtime requires its configured primary checkout")
    return config


def validate_loaded_backend(config: dict) -> None:
    validate_backend(config, BACKEND, absolute)


def enable_preflight(config: dict) -> None:
    verify_cache(config)
    with verified_binary(Path(config["binary"])):
        pass  # fail before changing native state when the accepted inode is invalid
    if sentinel_armed(config["sentinel"]):
        raise ValueError("managed Codebase sentinel is armed")
    properties = show(SLICE, "LoadState", "MemoryMax", "MemorySwapMax", "TasksMax")
    if properties != dict(
        LoadState="loaded", MemoryMax=str(2 * 1024**3), MemorySwapMax="0", TasksMax="512"
    ):
        raise ValueError("loaded managed client slice lacks required limits")
    validate_loaded_backend(config)


def enable(config: dict) -> None:
    enable_preflight(config)
    # Never reload a running backend then retire rejected stop commands.
    # unit_stop is a no-op for inactive/failed units; this check also proves
    # no surviving descendants before the first native enablement mutation.
    require_quiescent(BACKEND)
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "enable", BACKEND],
            capture_output=True,
            text=True,
            check=True,
            timeout=150,
        )
        installed = Path.home() / ".genesis/config/codebase-managed.json"
        if runtime_config(installed) != config:
            raise ValueError("managed settings changed during enablement")
        enable_preflight(config)  # enable reloads; validate that snapshot before start
        subprocess.run(["/usr/bin/systemctl", "--user", "start", BACKEND],
                       capture_output=True, text=True, check=True, timeout=150)
        ready(config)
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as startup:
        rollback = "complete"
        try:
            retire_managed()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            rollback = str(error)
        raise ValueError(f"native enable failed: {startup}; rollback: {rollback}") from startup


def artifact_snapshot(path: Path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
        raise ValueError(f"refusing non-file managed unit artifact: {path}")
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def artifact_parent(path: Path) -> Path:
    # Directory aliases are selected native namespaces. Resolve parents only;
    # final artifact links must never resolve into their target files.
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            ancestor.resolve(strict=True)  # a dangling directory alias is not absence
    if path.is_symlink() or path.exists():
        parent = path.resolve(strict=True)
        if not parent.is_dir():
            raise ValueError(f"refusing non-directory managed unit parent: {path}")
        return parent
    return path.resolve()  # definitely absent ordinary directory: no creation


def parent_identity(path: Path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError(f"refusing non-directory managed unit parent: {path}")
    return value.st_dev, value.st_ino


def remove_unit_artifacts() -> None:
    runtime = absolute(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    candidates = []
    parents = []
    for root in (units_dir(), runtime / "systemd/user"):
        for parent in (root, root / "default.target.wants"):
            resolved = artifact_parent(parent)
            parents.append((parent, resolved, parent_identity(resolved)))
            candidates.extend(resolved / unit for unit in (BACKEND, SLICE))
    snapshots = [(path, artifact_snapshot(path)) for path in candidates]
    if any(artifact_parent(parent) != resolved or parent_identity(resolved) != identity
           for parent, resolved, identity in parents):
        raise ValueError("managed unit parent changed during removal")
    for path, snapshot in snapshots:
        current = artifact_snapshot(path)
        if current is not None and current != snapshot:
            raise ValueError(f"managed unit artifact changed during removal: {path}")
    deleted = False
    failure = None
    try:
        for path, snapshot in snapshots:
            if any(artifact_parent(parent) != resolved or parent_identity(resolved) != identity
                   for parent, resolved, identity in parents):
                raise ValueError("managed unit parent changed during removal")
            current = artifact_snapshot(path)
            if current is None:
                continue
            if current != snapshot:
                raise ValueError(f"managed unit artifact changed during removal: {path}")
            path.unlink()
            deleted = True
    except (OSError, ValueError, RuntimeError) as error:
        failure = error
    # Native manager state must be refreshed even after a partial unlink failure.
    if deleted or failure is None:
        try:
            subprocess.run(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"managed artifact removal: {failure}; reload failed: {error}") from error
    if failure is not None:
        raise ValueError(f"managed artifact removal failed (partial={deleted}): {failure}") from failure


def lifecycle_main(args: argparse.Namespace) -> int:
    try:
        with lifecycle_lock():
            if args.command == "enable":
                path = config_path(str(Path.home() / ".genesis/config/codebase-managed.json"))
                if args.config and config_path(args.config) != path:
                    raise ValueError("enable requires the installed settings path")
                enable(runtime_config(path))
            else:
                retire_managed()
                if args.command == "remove":
                    remove_unit_artifacts()
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"managed lifecycle refused: {error}", file=sys.stderr)
        return 1


def report_retained_state() -> None:
    try:
        config = read_settings(
            Path.home() / ".genesis/config/codebase-managed.json", require_build=False
        )
    except (OSError, ValueError, RuntimeError):
        config = None  # missing/bad settings never expand uninstall deletion roots
    if config:
        roots = [Path.home() / name for name in ("genesis", ".genesis", "data", ".qdrant")]
        for key in ("binary", "cache", "runtime"):
            try:
                path = absolute(config[key]).resolve()
                inside = any(path.is_relative_to(root.resolve()) for root in roots)
            except (OSError, RuntimeError) as error:
                print(f"Preserved configured {key}; path classification failed: {error}", flush=True)
                continue
            if not inside:
                print(f"Preserved configured {key}: {path}", flush=True)


def uninstall(arguments: list[str]) -> None:
    # Only the fixed sibling script is executable; no caller-supplied command.
    if any(arg not in ("--genesis-only", "--non-interactive") for arg in arguments):
        raise ValueError("guarded cleanup accepts only --genesis-only/--non-interactive")
    with ExitStack() as stack:
        locks = [stack.enter_context(file_lock(path)) for path in uninstall_lock_paths()]
        require_no_batch()  # refuse orphan workers before retiring query readers
        retire_managed()
        print("Managed Codebase query retired; cancelling removal leaves it disabled.", flush=True)
        require_no_batch()
        report_retained_state()
        fds = [stream.fileno() for stream in locks]
        for fd in fds:
            os.set_inheritable(fd, True)
        os.execv(  # noqa: S606 - fixed Bash and sibling script, validated cleanup flags
            "/bin/bash",
            [
                "/bin/bash",
                str(SCRIPT.with_name("uninstall.sh")),
                "--genesis-only",
                *arguments,
                "--managed-uninstall-fds",
                *map(str, fds),
            ],
        )


def read_document(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | OPEN_FLAGS)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("managed settings must be regular")
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("managed settings exceed 64 KiB")
    try:
        value = json.loads(payload)
    except RecursionError as error:
        raise ValueError("managed JSON nesting exceeds parser limit") from error
    if not isinstance(value, dict):
        raise ValueError("managed settings must be an object")
    return value


def read_settings(path: Path, *, require_build: bool = True) -> dict:
    value = read_document(path)
    return validate_settings(value, require_build=require_build)


def validate_settings(value: dict, *, require_build: bool = True) -> dict:
    if type(value.get("version")) is not int or value["version"] != 2 or "enabled" in value:
        raise ValueError("requires immutable schema 2; preserve old state and configure afresh")
    for key in PATH_KEYS:
        absolute(value.get(key))
    if not isinstance(value.get("build"), str) or not value["build"]:
        raise ValueError("missing managed build identity")
    if require_build and value["build"] != BUILD:
        raise ValueError("unsupported managed build; repeat acceptance before upgrading")
    return value


def verified_binary(path: Path):
    """Execute this verified inode, not a later resolution of its pathname."""
    absolute(str(path))
    fd = os.open(path, os.O_RDONLY | OPEN_FLAGS)
    stream = os.fdopen(fd, "rb")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise ValueError("managed binary must be a regular executable")
        if hashlib.file_digest(stream, "sha256").hexdigest() != BUILD:
            raise ValueError("unsupported managed executable; repeat acceptance before upgrading")
        stream.seek(0)
    except BaseException:
        stream.close()
        raise
    return stream


def sentinel_armed(raw: str) -> bool:
    try:
        os.lstat(raw)
    except FileNotFoundError:
        return any(os.path.lexists(p) and not os.path.exists(p) for p in Path(raw).parents)
    except OSError:
        return True
    return True


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
    if read_document(cache / "config.json").get("ui_enabled") is not False:
        raise ValueError("managed UI must be explicitly disabled")
    with closing(sqlite3.connect((cache / "_config.db").as_uri() + "?mode=ro", uri=True)) as db:
        values = dict(db.execute("SELECT key,value FROM config"))
    if any(values.get(key) != "false" for key in DISABLED_KEYS):
        raise ValueError("managed automatic indexing/watchers must be disabled")


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | OPEN_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def first_existing_parent(path: Path) -> Path:
    parent = path.parent
    while not parent.exists():
        parent = parent.parent
    return parent


def sync_ancestors(path: Path, stop: Path) -> None:
    """Persist each new directory's entry through its first existing parent."""
    while True:
        sync_directory(path)
        if path == stop:
            return
        path = path.parent


def sync_staging(state: Path, existing_parent: Path) -> None:
    # Native config commands have exited and the validation reader is closed.
    # File fsync alone does not persist containing entries (fsync(2)).
    def fail(error: OSError) -> None:
        raise error

    directories = []
    for root, _children, files in os.walk(state, onerror=fail):
        directories.append(Path(root))
        for name in sorted(files):
            fd = os.open(Path(root) / name, os.O_RDONLY | OPEN_FLAGS)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("staged files must be regular")
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in sorted(directories, key=lambda p: (-len(p.parts), str(p))):
        if directory == state:
            continue
        sync_directory(directory)
    sync_ancestors(state, existing_parent)


def publish_settings(path: Path, config: dict) -> None:
    """Same-directory no-clobber publication. Never rewrite existing settings."""
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | OPEN_FLAGS, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def configure(args: argparse.Namespace, path: Path) -> None:
    main = absolute(args.main).resolve(strict=True)
    state = absolute(args.state).resolve()
    source = absolute(args.binary)
    if main != SCRIPT.parent.parent or not (main / ".git").is_dir():
        raise ValueError("configure must run from its primary checkout")
    if path != config_path(str(Path.home() / ".genesis/config/codebase-managed.json")):
        raise ValueError("configure requires the default installed-service settings path")
    if (
        state == main
        or main in state.parents
        or state in main.parents
        or path == state
        or state in path.parents
        or path in state.parents
    ):
        raise ValueError("managed state/settings/main topology overlaps")
    config = dict(
        version=2,
        main=str(main),
        binary=str(state / "bin/codebase-memory-mcp"),
        build=BUILD,
        cache=str(state / "cache"),
        runtime=str(state / "runtime"),
        sentinel=str(absolute(args.sentinel)),
    )
    validate_settings(config)
    with lifecycle_lock():
        if any(os.path.lexists(p) for p in (path, absolute(args.state), state)):
            raise ValueError("existing settings/state preserved; choose a fresh staging state")
        staging_created = False
        try:
            existing_parent = first_existing_parent(state)
            with verified_binary(source) as executable:
                state.mkdir(parents=True, mode=0o700)
                staging_created = True
                (state / "bin").mkdir(mode=0o700)
                with Path(config["binary"]).open("xb") as destination:
                    shutil.copyfileobj(executable, destination)
                    destination.flush()
                    os.fchmod(destination.fileno(), 0o500)
            cache = state / "cache"
            cache.mkdir(mode=0o700)
            (state / "runtime").mkdir(mode=0o700)
            with (cache / "config.json").open("x") as ui:
                json.dump(dict(ui_enabled=False), ui)
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
            sync_staging(state, existing_parent)
            settings_parent = first_existing_parent(path.parent)
            path.parent.mkdir(parents=True, exist_ok=True)
            sync_ancestors(path.parent, settings_parent)
            publish_settings(path, config)
        except BaseException:
            if staging_created:
                print(
                    f"Incomplete staging retained at {state}; inspect before retrying", file=sys.stderr
                )
            raise
    print("Configured; no service activated. Native lifecycle integration is a separate step.")


def status(path: Path | None, path_error: str | None = None) -> dict:
    """Diagnose broken/old configuration without running its binary or requiring it."""
    result = dict(settings=None, settings_error=path_error, service=None, manager_error=None)
    try:
        if path is not None:
            value = read_document(path)
            # Diagnostics retain only known scalar metadata, never arbitrary
            # nested input whose serialization could suppress the manager check.
            result["settings"] = {
                key: item
                if item is None or type(item) in (str, int, bool)
                else f"<invalid {type(item).__name__}>"
                for key, item in value.items()
                if key in ("version", "build", "enabled", *PATH_KEYS)
            }
            validate_settings(value)
    except (OSError, ValueError) as error:
        result["settings_error"] = str(error)
    try:
        output = subprocess.check_output(
            [
                "/usr/bin/systemctl",
                "--user",
                "show",
                BACKEND,
                "-p",
                "LoadState",
                "-p",
                "ActiveState",
                "-p",
                "MainPID",
                "-p",
                "ControlGroup",
                "-p",
                "UnitFileState",
            ],
            text=True,
            timeout=30,
            stderr=subprocess.PIPE,
        )
        result["service"] = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError) as error:
        result["manager_error"] = str(error)
    return result


def show(unit: str, *properties: str, timeout: float = 30) -> dict[str, str]:
    output = subprocess.check_output(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            unit,
            *(arg for name in properties for arg in ("-p", name)),
        ],
        text=True,
        timeout=timeout,
        stderr=subprocess.PIPE,
    )
    value = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if any(name not in value for name in properties):
        raise ValueError(f"manager omitted requested properties for {unit}")
    return value


def require_enabled(config: dict, *, timeout: float = 30) -> None:
    if show(BACKEND, "UnitFileState", timeout=timeout)["UnitFileState"] != "enabled":
        raise ValueError("managed query service must be persistently enabled")
    if sentinel_armed(config["sentinel"]):
        raise ValueError("managed Codebase sentinel is armed")


def verify_query_boundary(pid: str, *, startup: bool = False) -> None:
    leaf, root, version = resolve_cgroup(
        Path(f"/proc/{pid}/cgroup"), Path(f"/proc/{pid}/mountinfo")
    )
    if version != 2 or leaf == root or leaf.name != BACKEND:
        raise ValueError("managed daemon is outside its cgroup v2 service")
    if (leaf / "memory.max").read_text().strip() != str(2 * 1024**3) or (
        leaf / "memory.swap.max"
    ).read_text().strip() != "0":
        raise ValueError("managed daemon lacks exact memory/zero-swap cap")
    cpu = (leaf / "cpu.max").read_text().split()
    if len(cpu) != 2:
        raise ValueError("managed daemon lacks an enforced CPU ceiling")
    quota, period = (number(value, "cpu.max") for value in cpu)
    if quota == 0 or period == 0 or quota > 2 * period:
        raise ValueError("managed daemon exceeds the two-core CPU ceiling")
    if number((leaf / "pids.max").read_text().strip(), "pids.max") > 128:
        raise ValueError("managed daemon exceeds the 128-task ceiling")
    cursor = leaf.parent
    while cursor == root or root in cursor.parents:
        try:
            limit = (cursor / "memory.max").read_text().strip()
        except FileNotFoundError:
            if cursor != root:
                raise
            limit = "max"  # true cgroup filesystem root has no memory.max
        if limit != "max" and number(limit, "ancestor memory.max") < 2 * 1024**3:
            raise ValueError("ancestor cap is smaller than managed query budget")
        if startup and limit != "max":
            charge = _working_charge(
                read_number(cursor / "memory.current"), cursor / "memory.stat", 2 * 1024**3, 2
            )
            # Admission includes this small staging process; do not subtract
            # raw leaf usage from a cache-discounted ancestor charge. This is
            # a startup snapshot, not a reservation or recurring RPC gate.
            if number(limit, "ancestor memory.max") - charge < 2 * 1024**3:
                raise ValueError("insufficient ancestor headroom for managed query budget")
        if cursor == root:
            break
        cursor = cursor.parent
    if startup and _host_available(Path("/proc/meminfo")) < 2 * 1024**3:
        raise ValueError("insufficient host available memory for managed query budget")


def check_backend(config: dict, *, starting: bool = False, timeout: float = 30) -> str:
    value = show(BACKEND, "ActiveState", "MainPID", timeout=timeout)
    if value["ActiveState"] not in (("active", "activating") if starting else ("active",)):
        raise ValueError("managed native daemon is unavailable")
    pid = value["MainPID"]
    if number(pid, "MainPID") == 0 or not os.path.samefile(f"/proc/{pid}/exe", config["binary"]):
        raise ValueError("managed native daemon identity mismatch")
    verify_query_boundary(pid)
    return pid


def ready(config: dict) -> None:
    deadline = time.monotonic() + 120
    native_deadline = None
    require_enabled(config)

    def manager_timeout() -> float:
        remaining = (native_deadline or deadline) - time.monotonic()
        if remaining <= 0:
            raise ValueError("managed native daemon readiness deadline expired")
        return min(30, remaining)

    with verified_binary(Path(config["binary"])) as executable:
        while time.monotonic() < (native_deadline or deadline):
            try:
                pid = check_backend(config, starting=True, timeout=manager_timeout())
                now = time.monotonic()
                if native_deadline is None:
                    native_deadline = min(deadline, now + 60)
                remaining = native_deadline - now
                if remaining <= 0:
                    break
                response = subprocess.run(
                    [f"/proc/self/fd/{executable.fileno()}", "daemon", "status"],
                    env=native_env(config),
                    pass_fds=(executable.fileno(),),
                    capture_output=True,
                    text=True,
                    timeout=remaining,
                )
                if (
                    response.returncode == 0
                    and "daemon: active (permanent)" in response.stdout
                    and re.search(r"^  pid: " + re.escape(pid) + r"$", response.stdout, re.M)
                    and "state: stopping" not in response.stdout
                    and check_backend(config, starting=True, timeout=manager_timeout()) == pid
                ):
                    require_enabled(config, timeout=manager_timeout())
                    if time.monotonic() < native_deadline:
                        return
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass  # bounded startup polling; deadline is a terminal refusal
            time.sleep(0.1)
    raise ValueError("managed native daemon did not become ready")


def serve(config: dict) -> None:
    require_enabled(config)
    verify_cache(config)
    verify_query_boundary("self")
    os.chdir(config["main"])
    # No shared lifecycle lock here: enable will hold exclusive while it waits
    # for ExecStartPost readiness. Native startup must not deadlock against it.
    with verified_binary(Path(config["binary"])) as executable:
        # The pinned local CLI repairs a dead endpoint generation. Internal
        # daemon startup alone refuses stale sockets after a prior SIGKILL.
        subprocess.run(
            [f"/proc/self/fd/{executable.fileno()}", "config", "get", "auto_index"],
            env=native_env(config),
            pass_fds=(executable.fileno(),),
            stdout=subprocess.DEVNULL,
            check=True,
            timeout=45,
        )
        require_enabled(config)
        verify_cache(config)
        verify_query_boundary("self", startup=True)
        os.set_inheritable(executable.fileno(), True)
        os.execve(  # noqa: S606 - accepted inode and fixed stock daemon argv
            f"/proc/self/fd/{executable.fileno()}",
            [config["binary"], "--cbm-daemon-internal", "--cbm-daemon-permanent"],
            native_env(config),
        )


def uninstall_main(args: argparse.Namespace) -> int:
    try:
        if args.command == "uninstall":
            arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
            uninstall(arguments)
        else:
            verify_uninstall_locks(args.fds)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"managed uninstall refused: {error}", file=sys.stderr)
        return 1


def parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    for key in ("main", "binary", "state", "sentinel"):
        setup.add_argument("--" + key, required=True)
    commands.add_parser("status")
    commands.add_parser("serve")
    commands.add_parser("ready")
    for command in ("enable", "disable", "remove"):
        commands.add_parser(command)
    teardown = commands.add_parser("uninstall")
    teardown.add_argument("arguments", nargs=argparse.REMAINDER)
    verification = commands.add_parser("verify-uninstall-locks")
    verification.add_argument("fds", type=int, nargs=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(argv)
    if args.command in ("uninstall", "verify-uninstall-locks"):
        return uninstall_main(args)
    if args.command in ("enable", "disable", "remove"):
        return lifecycle_main(args)
    raw = (
        args.config
        or (
            os.environ.get("CODEBASE_MEMORY_MCP_MANAGED_CONFIG")
            if args.command == "status"
            else None
        )
        or str(Path.home() / ".genesis/config/codebase-managed.json")
    )
    try:
        try:
            path = config_path(raw)
        except (OSError, ValueError, RuntimeError) as error:
            if args.command != "status":
                raise
            print(json.dumps(status(None, str(error)), indent=2))
            return 0
        if args.command == "configure":
            configure(args, path)
        elif args.command == "status":
            print(json.dumps(status(path), indent=2))
        else:
            config = runtime_config(path)
            (serve if args.command == "serve" else ready)(config)
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"managed Codebase refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
