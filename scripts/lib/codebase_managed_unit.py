"""Stateless typed systemd contract reader for the managed query unit."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

from code_intel_cbm_admission import _mount_path, number, resolve_cgroup


def bus_call(path: str, interface: str, method: str, *args: str) -> dict:
    result = subprocess.run(
        ["/usr/bin/busctl", "--user", "--json=short", "call", "org.freedesktop.systemd1",
         path, interface, method, *args],
        capture_output=True, text=True, check=True, timeout=30,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise ValueError("invalid typed manager response")
    return value


def loaded_properties(unit: str, interface: str) -> dict:
    value = bus_call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager",
                     "LoadUnit", "s", unit)
    data = value["data"]
    if (value["type"] != "o" or not isinstance(data, list) or len(data) != 1
            or not isinstance(data[0], str)
            or not re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", data[0])):
        raise ValueError("invalid loaded unit object path")
    value = bus_call(data[0], "org.freedesktop.DBus.Properties", "GetAll", "s",
                     "org.freedesktop.systemd1." + interface)
    if (value["type"] != "a{sv}" or not isinstance(value["data"], list)
            or len(value["data"]) != 1 or not isinstance(value["data"][0], dict)):
        raise ValueError("invalid loaded unit properties")
    return value["data"][0]


def typed_property(properties: dict, name: str, signature: str):
    value = properties.get(name)
    if (not isinstance(value, dict) or set(value) != {"type", "data"}
            or value["type"] != signature):
        raise ValueError(f"invalid loaded {name}")
    result = value["data"]
    if signature == "s" and not isinstance(result, str):
        raise ValueError(f"invalid loaded {name}")
    if signature in ("t", "i"):
        low, high = (0, 2**64 - 1) if signature == "t" else (-2**31, 2**31 - 1)
        if type(result) is not int or not low <= result <= high:
            raise ValueError(f"invalid loaded {name}")
    return result


def validate_backend(config: dict, backend: str, validate_absolute, parameters=None) -> None:
    unit = loaded_properties(backend, "Unit")
    if (typed_property(unit, "LoadState", "s") != "loaded"
            or typed_property(unit, "Id", "s") != backend):
        raise ValueError("managed backend is not the loaded installed unit")
    properties = loaded_properties(backend, "Service")
    expected = {"Type": ("s", "exec"), "WorkingDirectory": ("s", "/"),
                "MemoryMax": ("t", 2 * 1024**3), "MemorySwapMax": ("t", 0),
                "OOMScoreAdjust": ("i", 500), "KillMode": ("s", "control-group"),
                "Restart": ("s", "no")}
    for name, (signature, required) in expected.items():
        if typed_property(properties, name, signature) != required:
            raise ValueError(f"loaded managed backend has incompatible {name}")
    for name, low, high in (("TasksMax", 0, 128), ("CPUQuotaPerSecUSec", 1, 2000000),
                            ("TimeoutStartUSec", 1, 120000000),
                            ("TimeoutStopUSec", 1, 30000000)):
        if not low <= typed_property(properties, name, "t") <= high:
            raise ValueError(f"loaded managed backend has incompatible {name}")
    # VENV_PATH is the installer's explicit selection. Keep its literal spelling:
    # resolving bin/python would discard the environment's identity.
    main, home, venv = parameters or (
        config["main"], str(Path.home()),
        os.environ.get("VENV_PATH") or config["main"] + "/.venv",
    )
    validate_absolute(venv)
    argv = ["/bin/sh", "-c", 'exec "$@"', "--", venv + "/bin/python", "-I",
            main + "/scripts/codebase_managed.py", "--config",
            home + "/.genesis/config/codebase-managed.json"]
    for name, role in (("ExecStartEx", "serve"), ("ExecStartPostEx", "ready"),
                       ("ExecConditionEx", None), ("ExecStartPreEx", None),
                       ("ExecReloadEx", None), ("ExecStopEx", None), ("ExecStopPostEx", None)):
        commands = typed_property(properties, name, "a(sasasttttuii)")
        if not isinstance(commands, list) or len(commands) != (1 if role else 0):
            raise ValueError(f"loaded managed backend has incompatible {name}")
        if role:
            record = commands[0]
            if (not isinstance(record, list) or len(record) != 10
                    or record[:3] != ["/bin/sh", argv + [role], ["no-env-expand"]]
                    or any(type(item) is not int for item in record[3:])
                    or any(not 0 <= item < 2**64 for item in record[3:7])
                    or not 0 <= record[7] < 2**32
                    or any(not -2**31 <= item < 2**31 for item in record[8:])):
                raise ValueError(f"loaded managed backend has incompatible {name}")


def manager_unit_paths() -> tuple[Path, ...]:
    value = bus_call("/org/freedesktop/systemd1", "org.freedesktop.DBus.Properties",
                     "Get", "ss", "org.freedesktop.systemd1.Manager", "UnitPath")
    if value["type"] != "v" or not isinstance(value["data"], list) or len(value["data"]) != 1:
        raise ValueError("invalid native UnitPath response")
    paths = typed_property({"UnitPath": value["data"][0]}, "UnitPath", "as")
    if not isinstance(paths, list) or not paths:
        raise ValueError("missing native UnitPath")
    return tuple(unit_path(path) for path in paths)


def unit_path(value: str) -> Path:
    if (not isinstance(value, str) or not value or any(c in value for c in "\n\r\x00")
            or not Path(value).is_absolute() or ".." in Path(value).parts):
        raise ValueError("unsupported canonical unit path")
    return Path(value)


def retirement_parameters(backend: str) -> tuple[str, str, str]:
    """Read inert parameters, never settings or the current virtualenv selection."""
    properties = loaded_properties(backend, "Service")
    parameters = []
    for field, role in (("ExecStartEx", "serve"), ("ExecStartPostEx", "ready")):
        records = typed_property(properties, field, "a(sasasttttuii)")
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("unsupported retirement command shape")
        record = records[0]
        if (not isinstance(record, list) or len(record) != 10 or record[0] != "/bin/sh"
                or record[2] != ["no-env-expand"] or not isinstance(record[1], list)
                or len(record[1]) != 10 or not all(isinstance(x, str) for x in record[1])):
            raise ValueError("unsupported retirement command shape")
        argv = record[1]
        if (argv[:4] != ["/bin/sh", "-c", 'exec "$@"', "--"]
                or argv[5] != "-I" or argv[7] != "--config" or argv[9] != role):
            raise ValueError("unsupported retirement command shape")
        parts = []
        for value, suffix in ((argv[6], "/scripts/codebase_managed.py"),
                              (argv[8], "/.genesis/config/codebase-managed.json"),
                              (argv[4], "/bin/python")):
            if not value.endswith(suffix):
                raise ValueError("unsupported retirement command path")
            prefix = value[:-len(suffix)]
            unit_path(prefix)
            parts.append(prefix)
        parameters.append(tuple(parts))
    if parameters[0] != parameters[1]:
        raise ValueError("retirement command parameters disagree")
    return parameters[0]


def canonical_bytes(template: Path, parameters: tuple[str, str, str] | None) -> bytes:
    body = template.read_text()
    tokens = {}
    if parameters:
        for token, value in zip(("REPO", "HOME", "VENV"), parameters, strict=True):
            unit_path(value)
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            tokens["__" + token + "_EXEC__"] = escaped
    placeholders = re.findall(r"__[A-Z_]+__", body)
    if any(token not in tokens for token in placeholders):
        raise ValueError("unresolved canonical unit template")
    return re.sub(r"__[A-Z_]+__", lambda match: tokens[match[0]], body).encode()


def source_snapshot(paths: tuple[Path, ...], unit: str, roots: tuple[Path, Path]):
    """Reject competing sources/override namespaces without resolving precedence."""
    stem, suffix = unit.rsplit(".", 1)
    scopes = [unit + ".d", suffix + ".d"]
    scopes.extend(stem[:i + 1] + "." + suffix + ".d"
                  for i, char in enumerate(stem) if char == "-")
    selected, seen = [], set()
    for root in paths:
        physical = artifact_parent(root)
        if physical in seen:
            continue  # selected directory aliases share one physical namespace
        seen.add(physical)
        for scope in scopes:
            try:
                (root / scope).lstat()
            except FileNotFoundError:
                continue
            raise ValueError(f"unsupported managed unit override namespace: {root / scope}")
        path = root / unit
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or physical not in {r.resolve() for r in roots}:
            raise ValueError(f"unsupported managed unit source: {path}")
        selected.append(path)
    if not selected:
        return None
    if len(selected) != 1:
        raise ValueError(f"competing managed unit sources: {unit}")
    path = selected[0]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("managed unit source is not regular")
        body = stream.read(65537)
    if len(body) > 65536:
        raise ValueError("managed unit source is too large")
    return path.resolve(), (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), body


def canonical_sources(script: Path, roots: tuple[Path, Path], names: tuple[str, str], config=None):
    paths = manager_unit_paths()
    # Supported native default layout, not a resolver for arbitrary manager XDG
    # or SYSTEMD_UNIT_PATH selections. The literal sibling survives root aliases.
    controls = tuple(Path(str(root) + ".control").resolve() for root in roots)
    if tuple(path.resolve() for path in paths[:2]) != controls:
        raise ValueError("unsupported native managed unit namespace")
    if not all(root.resolve() in {p.resolve() for p in paths} for root in roots):
        raise ValueError("native managed unit namespace is missing")
    snapshots, errors = {}, []
    for index, unit in enumerate(names):
        try:
            snapshot = source_snapshot(paths, unit, roots)
            parameters = None
            if snapshot:
                if index == 0:
                    parameters = ((config["main"], str(Path.home()),
                                   os.environ.get("VENV_PATH") or config["main"] + "/.venv")
                                  if config else retirement_parameters(unit))
                template = script.parent / "systemd" / (unit + ".template")
                if snapshot[2] != canonical_bytes(template, parameters):
                    raise ValueError(f"unsupported noncanonical managed unit: {unit}")
            snapshots[unit] = (snapshot, parameters)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            errors.append(f"{unit}: {error}")
    if errors:
        raise ValueError("canonical unit authority refused: " + "; ".join(errors))
    return paths, snapshots


def validate_source_identity(unit: str, snapshot) -> None:
    properties = loaded_properties(unit, "Unit")
    if (typed_property(properties, "Id", "s") != unit
            or typed_property(properties, "Names", "as") != [unit]
            or typed_property(properties, "LoadState", "s") != "loaded"
            or typed_property(properties, "SourcePath", "s") != ""
            or typed_property(properties, "DropInPaths", "as") != []
            or unit_path(typed_property(properties, "FragmentPath", "s")).resolve() != snapshot[0]):
        raise ValueError(f"unsupported refreshed managed unit identity: {unit}")


def implicit_slice_absent(unit: str) -> bool:
    """Native slices need no fragment; loaded alone is not provider presence."""
    properties = loaded_properties(unit, "Unit")
    expected = {"Id": ("s", unit), "Names": ("as", [unit]), "LoadState": ("s", "loaded"),
                "FragmentPath": ("s", ""), "SourcePath": ("s", ""),
                "DropInPaths": ("as", []), "UnitFileState": ("s", "")}
    return (all(typed_property(properties, name, signature) == value
                for name, (signature, value) in expected.items())
            and typed_property(properties, "Transient", "b") is False)


def native_install(action: str, unit: str, *, runtime: bool = False) -> None:
    """Use the inspected manager's reader; no client-side fallback or reload."""
    if action not in ("enable", "disable"):
        raise ValueError("unsupported native install operation")
    method = "EnableUnitFiles" if action == "enable" else "DisableUnitFiles"
    args = ("asbb", "1", unit, str(runtime).lower(), "false") if action == "enable" else (
        "asb", "1", unit, str(runtime).lower())
    value = bus_call("/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", method, *args)
    expected = "ba(sss)" if action == "enable" else "a(sss)"
    data = value["data"]
    if (value["type"] != expected or not isinstance(data, list)
            or len(data) != (2 if action == "enable" else 1)
            or (action == "enable" and data[0] is not True)):
        raise ValueError("invalid native installation response")
    changes = data[-1]
    if (not isinstance(changes, list) or any(not isinstance(row, list) or len(row) != 3
            or not all(isinstance(item, str) for item in row) for row in changes)):
        raise ValueError("invalid native installation changes")


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


def artifact_snapshot(path: Path):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if not (stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode)):
        raise ValueError(f"refusing non-file managed unit artifact: {path}")
    return value.st_dev, value.st_ino, stat.S_IFMT(value.st_mode)


def parent_identity(path: Path):
    try:
        value = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError(f"refusing non-directory managed unit parent: {path}")
    return value.st_dev, value.st_ino


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


def verify_memory_ancestors(leaf: Path, root: Path) -> None:
    """Every visible finite ancestor must admit the full query aggregate."""
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
        if cursor == root:
            break
        cursor = cursor.parent


def validate_frontend_boundary(unit: str, leaf: Path, root: Path, version: int, slice_name: str) -> None:
    if not re.fullmatch(r"genesis-cbm-query-client-[0-9a-f]{32}\.service", unit):
        raise ValueError("invalid managed frontend unit")
    if version != 2 or leaf == root or leaf.name != unit or leaf.parent.name != slice_name:
        raise ValueError("managed frontend is outside its capped client slice")
    for node, memory, tasks in ((leaf, 256 * 1024**2, 32), (leaf.parent, 2 * 1024**3, 512)):
        if (
            (node / "memory.max").read_text().strip() != str(memory)
            or (node / "memory.swap.max").read_text().strip() != "0"
            or (node / "pids.max").read_text().strip() != str(tasks)
        ):
            raise ValueError("managed frontend lacks exact memory/swap/task caps")
    verify_memory_ancestors(leaf, root)


def frontend_command(main: Path, settings: Path, unit: str, backend: str, slice_name: str) -> list[str]:
    """Build the native frontend transport from validated literal parameters."""
    venv = os.environ.get("VENV_PATH") or str(main) + "/.venv"
    absolute(venv)
    interpreter = venv + "/bin/python"
    if not Path(interpreter).is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError("selected installed interpreter is unavailable")
    helper = str(main / "scripts/codebase_managed.py")
    command = ["/usr/bin/systemd-run", "--user", "--pipe", "--quiet", "--collect", "--wait",
               "--unit=" + unit, "--slice=" + slice_name, "--working-directory=/",
               "--setenv=HOME=" + os.environ["HOME"], "--setenv=VENV_PATH=" + venv]
    for key, value in (("PYTHON", interpreter), ("HELPER", helper),
                       ("CONFIG", str(settings)), ("UNIT", unit)):
        if key != "UNIT":
            absolute(value)
        command.append("--setenv=CBM_CLIENT_" + key + "=" + value)
    for value in ("Requisite=" + backend, "After=" + backend, "StopPropagatedFrom=" + backend,
                  "KillMode=control-group", "MemoryMax=256M", "MemorySwapMax=0",
                  "TasksMax=32", "OOMScoreAdjust=500"):
        command.extend(("-p", value))
    bridge = 'exec "$$CBM_CLIENT_PYTHON" -I "$$CBM_CLIENT_HELPER" --config "$$CBM_CLIENT_CONFIG" client --unit "$$CBM_CLIENT_UNIT"'
    command.extend(("--", "/bin/sh", "-c", bridge))
    return command
