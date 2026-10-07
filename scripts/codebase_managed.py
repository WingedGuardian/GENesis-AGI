#!/usr/bin/env python3
"""Stage immutable, pinned Codebase configuration; diagnose without activation.

This command does not render units, start providers, index repositories or remove
the machine sentinel. Native runtime and lifecycle are separate integration steps.
Configuration is published once. Native enablement owns operational state.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from code_intel_cbm_worker import BUILD  # noqa: E402

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
    return path.parent.resolve() / path.name


def units_dir() -> Path:
    return Path.home() / ".config/systemd/user"


@contextmanager
def lifecycle_lock(*, shared: bool = False):
    """Coordinate cooperating tools; never replace/delete the lock pathname."""
    directory = units_dir()
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        directory / ".genesis-codebase-config.lock", os.O_RDWR | os.O_CREAT | OPEN_FLAGS, 0o600
    )
    with os.fdopen(fd, "a") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("lifecycle lock must be regular")
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        yield stream


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
    with lifecycle_lock():
        if any(os.path.lexists(p) for p in (path, absolute(args.state), state)):
            raise ValueError("existing settings/state preserved; choose a fresh staging state")
        try:
            existing_parent = first_existing_parent(state)
            with verified_binary(source) as executable:
                state.mkdir(parents=True, mode=0o700)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    for key in ("main", "binary", "state", "sentinel"):
        setup.add_argument("--" + key, required=True)
    commands.add_parser("status")
    args = parser.parse_args(argv)
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
        else:
            print(json.dumps(status(path), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(f"managed Codebase refused: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
