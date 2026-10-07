"""Guarded cleanup holds real locks and executes the actual script in owned homes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_codebase_managed_config import ROOT, managed

guard = managed


@pytest.fixture
def supported_retirement(guard, monkeypatch):
    namespace = guard.retire_managed.__globals__
    sources = ((), {guard.BACKEND: ("source", None), guard.SLICE: ("source", None)})
    monkeypatch.setitem(namespace, "inspect_sources", lambda *a, **k: sources)
    monkeypatch.setitem(namespace, "refresh_sources", lambda *a, **k: None)
    monkeypatch.setitem(namespace, "native_install", lambda *a, **k: None)


@pytest.mark.parametrize("layout", ["default", "empty", "external", "data", "repo", "qdrant", "nested", "default-alias", "locks-alias", "external-alias"])
def test_namespace_layout_before_mutation(guard, transaction, monkeypatch, layout):
    home = transaction
    for name in (".genesis/locks", "data", ".qdrant", "outside"):
        (home / name).mkdir(parents=True)
    if layout == "empty":
        monkeypatch.setenv("GENESIS_HOME", "")
    elif layout in ("external", "data", "repo", "qdrant", "nested"):
        name = {"external": "outside", "data": "data", "repo": "genesis", "qdrant": ".qdrant", "nested": ".genesis/custom"}[layout]
        monkeypatch.setenv("GENESIS_HOME", str(home / name))
    elif layout == "default-alias":
        (home / ".genesis/locks").rmdir()
        (home / ".genesis").rmdir()
        (home / ".genesis").symlink_to(home / "outside", target_is_directory=True)
    elif layout == "locks-alias":
        (home / ".genesis/locks").rmdir()
        (home / ".genesis/locks").symlink_to(home / "outside", target_is_directory=True)
    elif layout == "external-alias":
        (home / "alias").symlink_to(home / "data", target_is_directory=True)
        monkeypatch.setenv("GENESIS_HOME", str(home / "alias"))
    if layout in ("default", "empty", "external"):
        paths = guard.uninstall_lock_paths()
        assert len(paths) == 3 and all(path.is_absolute() for path in paths)
    else:
        with pytest.raises(ValueError, match="lock namespace"):
            guard.uninstall_lock_paths()
    assert not (home / "outside/locks").exists()


@pytest.mark.parametrize("root", ["genesis", ".genesis", "data", ".qdrant"])
@pytest.mark.parametrize("alias", ["directory", "ancestor"])
def test_lifecycle_namespace_inside_deletion_roots_refuses(guard, transaction, root, alias):
    home = transaction
    target = home / root / "units"
    target.mkdir(parents=True)
    if alias == "directory":
        (home / ".config/systemd").mkdir(parents=True)
        (home / ".config/systemd/user").symlink_to(target, target_is_directory=True)
    else:
        (target / "systemd/user").mkdir(parents=True)
        (home / ".config").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="lock namespace overlapping"):
        guard.uninstall_lock_paths()
    assert not (home / ".genesis/locks").exists()
    assert not (home / ".config/systemd/user/.genesis-codebase-config.lock").exists()


def test_lifecycle_external_alias_preserves_supported_namespace(guard, transaction):
    home = transaction
    target = home / "outside-units"
    target.mkdir()
    (home / ".config/systemd").mkdir(parents=True)
    (home / ".config/systemd/user").symlink_to(target, target_is_directory=True)
    paths = guard.uninstall_lock_paths()
    assert paths[2].parent.resolve() == target
    assert not (home / ".genesis/locks").exists()


def test_lifecycle_alias_into_retained_runner_namespace_refuses(guard, transaction):
    home = transaction
    target = home / ".genesis/locks"
    target.mkdir(parents=True)
    (home / ".config/systemd").mkdir(parents=True)
    (home / ".config/systemd/user").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="lock namespace overlapping"):
        guard.uninstall_lock_paths()
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("state", ["enabled", "enabled-runtime", "linked", "indirect", "alias", "unknown", ""])
def test_retirement_rejects_surviving_enablement_after_success(guard, monkeypatch, state, supported_retirement):
    g = guard.retire_managed.__globals__
    monkeypatch.setattr(g["subprocess"], "run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setitem(g, "require_quiescent", lambda unit: None)
    monkeypatch.setitem(g, "show", lambda *a: {"UnitFileState": state, "LoadState": "loaded"})
    with pytest.raises(ValueError, match="retirement failed"):
        guard.retire_managed()


@pytest.mark.parametrize("state,load", [("disabled", "loaded"), ("masked", "loaded"), ("static", "loaded"), ("generated", "loaded"), ("transient", "loaded"), ("", "not-found")])
def test_retirement_accepts_proved_nonenabled_state(guard, monkeypatch, state, load, supported_retirement):
    g = guard.retire_managed.__globals__
    monkeypatch.setattr(g["subprocess"], "run", lambda *a, **kw: SimpleNamespace(returncode=0, stderr=""))
    monkeypatch.setitem(g, "require_quiescent", lambda unit: None)
    monkeypatch.setitem(g, "show", lambda *a: {"UnitFileState": state, "LoadState": load})
    if state in ("disabled", ""):
        guard.retire_managed()
    else:
        with pytest.raises(ValueError, match="retirement failed"):
            guard.retire_managed()


@pytest.mark.parametrize("key", ["binary", "cache", "runtime"])
def test_retained_diagnostics_tolerate_each_symlink_loop(guard, tmp_path, monkeypatch, capsys, key):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    config = {name: str(tmp_path / name) for name in ("binary", "cache", "runtime")}
    config[key] = str(loop)
    monkeypatch.setitem(guard.report_retained_state.__globals__, "read_settings", lambda *a, **kw: config)
    guard.report_retained_state()
    output = capsys.readouterr().out
    assert f"Preserved configured {key}; path classification failed" in output
    assert sum("Preserved configured" in line for line in output.splitlines()) == 3


@pytest.mark.parametrize("kind,expected", [("native", True), ("heap", True), ("wrapper", True), ("private-launch", True), ("query", False), ("foreign-cwd", False), ("foreign-path", False), ("unrelated", False), ("foreign-wrapper", False)])
def test_existing_fallback_writer_population(guard, transaction, tmp_path, kind, expected):
    repo = transaction / "genesis"
    other = tmp_path / "other"
    other.mkdir()
    process = tmp_path / "proc/123"
    process.mkdir(parents=True)
    index = "/opt/gitnexus/dist/cli/index.js"
    argv = ["node", index, "analyze"]
    cwd = repo
    if kind == "heap":
        argv.insert(1, "--max-old-space-size=8192")
    elif kind in ("wrapper", "foreign-wrapper"):
        argv = ["bash", str(repo / "scripts/lib/code_intel_index.sh"), str(other if kind == "foreign-wrapper" else repo), "gitnexus", "fast"]
    elif kind == "private-launch":
        argv = ["/bin/bash", str(repo / "scripts/lib/code_intel_index.sh"), "--exec-indexer-with-oom-adj", index, "analyze"]
    elif kind == "query":
        argv[-1] = "mcp"
    elif kind == "foreign-cwd":
        cwd = other
    elif kind == "foreign-path":
        argv.append(str(other))
    elif kind == "unrelated":
        argv[1] = "/opt/another/dist/cli/index.js"
    (process / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
    (process / "stat").write_text("123 (worker) " + " ".join(["S"] + ["0"] * 18 + ["10"]))
    (process / "cwd").symlink_to(cwd, target_is_directory=True)
    assert guard.observed_index_writers(process.parent) == ([123] if expected else [])


def test_fallback_writer_observation_refuses_unreadable_identity(guard, tmp_path):
    process = tmp_path / "proc/123"
    process.mkdir(parents=True)
    with pytest.raises(ValueError, match="cannot prove index writer identity"):
        guard.observed_index_writers(process.parent)


def test_pid_reuse_during_writer_observation_refuses(guard, transaction, tmp_path, monkeypatch):
    process = tmp_path / "proc/123"
    process.mkdir(parents=True)
    (process / "cmdline").write_bytes(b"node\0/opt/gitnexus/dist/cli/index.js\0analyze\0")
    (process / "cwd").symlink_to(transaction / "genesis", target_is_directory=True)
    actual = Path.read_text
    generation = iter(("10", "11"))
    def read_stat(path, *a, **kw):
        if path == process / "stat":
            return "123 (worker) " + " ".join(["S"] + ["0"] * 18 + [next(generation)])
        return actual(path, *a, **kw)
    monkeypatch.setattr(Path, "read_text", read_stat)
    with pytest.raises(ValueError, match="changed during observation"):
        guard.observed_index_writers(process.parent)


def test_real_proc_writer_observation_does_not_terminate(guard, transaction):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "/opt/gitnexus/dist/cli/index.js", "analyze"], cwd=transaction / "genesis")
    try:
        assert child.pid in guard.observed_index_writers()
        assert child.poll() is None
    finally:
        child.terminate()  # only this disposable control, after observation
        child.wait(timeout=5)


@pytest.fixture
def transaction(guard, tmp_path, monkeypatch):
    home = tmp_path / "home $%&λ "
    repo = home / "genesis"
    (repo / "scripts").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GENESIS_HOME", raising=False)
    monkeypatch.setitem(
        guard.uninstall_lock_paths.__globals__, "SCRIPT", repo / "scripts/codebase_managed.py"
    )
    return home


@pytest.mark.parametrize("index", range(3))
def test_every_lock_refuses_busy_writer_before_retirement(guard, transaction, monkeypatch, index):
    paths = guard.uninstall_lock_paths()
    calls = []
    monkeypatch.setitem(
        guard.uninstall.__globals__, "require_no_batch", lambda: calls.append("retire")
    )
    with guard.file_lock(paths[index]), pytest.raises(BlockingIOError):
        guard.uninstall([])
    assert not calls


@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo"])
def test_nonregular_lock_refuses_without_overwriting(guard, transaction, kind):
    path = guard.uninstall_lock_paths()[0]
    path.parent.mkdir(parents=True)
    target = transaction / "foreign"
    target.write_text("preserved")
    if kind in ("symlink", "dangling"):
        path.symlink_to(target if kind == "symlink" else transaction / "missing")
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        guard.uninstall([])
    assert target.read_text() == "preserved"


def test_reentry_requires_same_regular_inodes_and_exclusive_locks(guard, transaction):
    paths = guard.uninstall_lock_paths()
    from contextlib import ExitStack

    with ExitStack() as stack:
        fds = [stack.enter_context(guard.file_lock(path)).fileno() for path in paths]
        guard.verify_uninstall_locks(fds)
        with pytest.raises(ValueError):
            guard.verify_uninstall_locks([fds[0]] * 3)
        paths[1].unlink()
        paths[1].write_text("replacement")
        with pytest.raises(ValueError, match="identity"):
            guard.verify_uninstall_locks(fds)


@pytest.mark.parametrize(
    "state,pid", [("active", "0"), ("activating", "1"), ("inactive", "1"), ("deactivating", "0")]
)
def test_nonquiescent_manager_state_refuses(guard, monkeypatch, state, pid):
    monkeypatch.setitem(
        guard.require_quiescent.__globals__,
        "show",
        lambda *a: dict(ActiveState=state, MainPID=pid, ControlGroup=""),
    )
    with pytest.raises(ValueError, match="nonquiescent"):
        guard.require_quiescent(guard.BACKEND)


@pytest.mark.parametrize("population", ["0", "1", "missing", "unreadable", "gone"])
def test_empty_cgroup_proof_includes_descendants(guard, tmp_path, monkeypatch, population):
    root = tmp_path / "cg"
    group = root / "user.slice/unit.service"
    group.mkdir(parents=True)
    mount = tmp_path / "mountinfo"
    mount.write_text(f"1 0 0:1 / {root} rw - cgroup2 cgroup rw\n")
    actual_path = guard.cgroup_empty.__globals__["Path"]
    monkeypatch.setitem(
        guard.cgroup_empty.__globals__,
        "Path",
        lambda value: mount if value == "/proc/self/mountinfo" else actual_path(value),
    )
    monkeypatch.setitem(
        guard.cgroup_empty.__globals__, "resolve_cgroup", lambda *a: (root, root, 2)
    )
    if population == "gone":
        group.rmdir()
    elif population == "unreadable":
        (group / "cgroup.events").mkdir()
    elif population != "missing":
        (group / "cgroup.events").write_text(f"populated {population}\nfrozen 0\n")
    if population in ("0", "gone"):
        guard.cgroup_empty("/user.slice/unit.service")
    else:
        with pytest.raises((OSError, ValueError)):
            guard.cgroup_empty("/user.slice/unit.service")


@pytest.fixture
def owned_install(tmp_path):
    home = tmp_path / "home"
    repo = home / "genesis"
    scripts = repo / "scripts"
    (scripts / "lib").mkdir(parents=True)
    tools = home / "tools"
    tools.mkdir()
    calls = home / "calls"
    systemctl = tools / "systemctl"
    systemctl.write_text("""#!/usr/bin/python3
import os,sys
from pathlib import Path
args=sys.argv[1:]
with Path(os.environ["CALLS"]).open("a") as out:out.write(" ".join(args)+"\\n")
if "list-units" in args:
 if os.environ.get("MANAGER_FAIL"):sys.exit(1)
 if os.environ.get("ORPHAN"):print("code-intel-012345abcdef-cbm-123.scope loaded active running worker")
elif "show" in args:
 values=dict(LoadState="not-found",ActiveState="inactive",MainPID="0",ControlGroup="",UnitFileState="")
 if os.environ.get("STOP_FAIL"):values["LoadState"]="loaded"
 if any(a.endswith((".slice",".scope")) for a in args):values.pop("MainPID")
 if any(a.endswith(".scope") for a in args):values["ActiveState"]=os.environ.get("ORPHAN_STATE","active")
 if "--value" not in args:
  for i,a in enumerate(args):
   if (a=="--property" or a=="-p") and args[i+1] in values:print(args[i+1]+"="+values[args[i+1]])
elif "stop" in args and os.environ.get("STOP_FAIL"):sys.exit(1)
""")
    systemctl.chmod(0o755)
    busctl = tools / "busctl"
    busctl.write_text("""#!/usr/bin/python3
import json,os,sys
from pathlib import Path
home=Path(os.environ["HOME"])
roots=[home/".config/systemd/user",home/"runtime/systemd/user"]
if "Get" in sys.argv and "UnitPath" in sys.argv:
 print(json.dumps({"type":"v","data":[{"type":"as","data":[str(root)+".control" for root in roots]+[str(root) for root in roots]}]}))
else:
 sys.exit("unexpected typed native operation in absent-unit fixture")
""")
    busctl.chmod(0o755)
    for name in ("sleep", "sudo", "ss", "systemd-detect-virt"):
        path = tools / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    remover = tools / "rm"
    remover.write_text("""#!/usr/bin/python3
import fcntl,os,sys
from pathlib import Path
parent=Path("/proc")/str(os.getppid())
args=(parent/"cmdline").read_bytes().split(b"\\0")
index=args.index(b"--managed-uninstall-fds")
for raw in args[index+1:index+4]:
 fd=os.open(parent/"fd"/raw.decode(),os.O_RDWR)
 named=os.readlink(parent/"fd"/raw.decode())
 identity=os.fstat(fd)
 current=os.stat(named)
 assert (identity.st_dev,identity.st_ino)==(current.st_dev,current.st_ino), "lock namespace changed"
 contender=os.open(named,os.O_RDWR)
 try:fcntl.flock(contender,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:pass
 else:sys.exit("new opener escaped original lock")
 os.close(contender)
 try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:pass
 else:sys.exit("cleanup lost an inherited lock")
 os.close(fd)
with Path(os.environ["CALLS"]).open("a") as out:out.write("LOCKS_HELD rm "+" ".join(sys.argv[1:])+"\\n")
os.execv("/usr/bin/rm",["rm",*sys.argv[1:]])
""")
    remover.chmod(0o755)
    helper = (
        (ROOT / "scripts/codebase_managed.py")
        .read_text()
        .replace('"/usr/bin/systemctl"', json.dumps(str(systemctl)))
    )
    (scripts / "codebase_managed.py").write_text(helper)
    shutil.copy2(ROOT / "scripts/uninstall.sh", scripts / "uninstall.sh")
    for name in ("code_intel_cbm_worker.py", "code_intel_cbm_admission.py", "codebase_managed_unit.py"):
        content = (ROOT / "scripts/lib" / name).read_text()
        (scripts / "lib" / name).write_text(content.replace('"/usr/bin/busctl"', json.dumps(str(busctl))))
    for name in (
        ".genesis/config",
        "data",
        ".qdrant",
        ".config/systemd/user",
        "runtime/systemd/user",
    ):
        (home / name).mkdir(parents=True)
    (home / ".bashrc").write_text("# preserved\n")
    env = dict(
        os.environ,
        HOME=str(home),
        PATH=f"{tools}:/usr/bin:/bin",
        CALLS=str(calls),
        XDG_RUNTIME_DIR=str(home / "runtime"),
        GENESIS_ROOT_WATCHDOG_PREFIX=str(home / "root"),
    )
    for key in (
        "GENESIS_HOME",
        "CODEBASE_MEMORY_MCP_MANAGED_CONFIG",
        "ORPHAN",
        "STOP_FAIL",
        "MANAGER_FAIL",
        "ORPHAN_STATE",
    ):
        env.pop(key, None)
    return home, scripts, env


@pytest.mark.parametrize("settings", ["missing", "malformed", "schema1", "stale"])
@pytest.mark.parametrize("unsupported_fragment", [False, True])
def test_actual_direct_cleanup_does_not_depend_on_settings(owned_install, settings, unsupported_fragment):
    home, scripts, env = owned_install
    external = home / "preserved-state"
    external.mkdir()
    if settings != "missing":
        document = (
            "{"
            if settings == "malformed"
            else json.dumps(
                dict(
                    version=1 if settings == "schema1" else 2,
                    build="old",
                    main=str(home / "genesis"),
                    binary=str(external / "binary"),
                    cache=str(external / "cache"),
                    runtime=str(external / "runtime"),
                    sentinel=str(home / "sentinel"),
                )
            )
        )
        (home / ".genesis/config/codebase-managed.json").write_text(document)
    foreign = home / "foreign"
    foreign.write_text("preserved")
    for root in (home / ".config/systemd/user", home / "runtime/systemd/user"):
        (root / "default.target.wants").mkdir()
        for unit in ("genesis-cbm-query.service", "genesis-cbm-query-clients.slice"):
            if unsupported_fragment:
                (root / unit).symlink_to(foreign)
            (root / "default.target.wants" / unit).symlink_to(root / unit)
        (root / "unrelated.slice").write_text("preserved")
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh"), "--genesis-only", "--non-interactive"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    if unsupported_fragment:
        assert result.returncode != 0 and "authority refused" in result.stderr
        assert all((home / name).is_dir() for name in ("genesis", ".genesis", "data", ".qdrant"))
        assert foreign.read_text() == "preserved" and "LOCKS_HELD rm" not in (home/"calls").read_text()
        assert all((root / "genesis-cbm-query.service").is_symlink() for root in
                   (home / ".config/systemd/user", home / "runtime/systemd/user"))
        return
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("genesis", "data", ".qdrant"):
        assert not (home / name).exists()
    assert [entry.name for entry in (home / ".genesis").iterdir()] == ["locks"]
    assert external.is_dir() and foreign.read_text() == "preserved"
    calls = (home / "calls").read_text()
    for name in ("genesis", "data", ".qdrant"):
        assert f"LOCKS_HELD rm -rf {home / name}" in calls
    for root in (home / ".config/systemd/user", home / "runtime/systemd/user"):
        assert (root / "unrelated.slice").read_text() == "preserved"
        assert not (root / "genesis-cbm-query-clients.slice").is_symlink()
    if settings == "stale":
        assert "Preserved configured cache" in result.stdout


def test_orphan_scope_refuses_actual_script_before_mutation(owned_install):
    home, scripts, env = owned_install
    env["ORPHAN"] = "1"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh"), "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "nonquiescent" in result.stderr
    assert (home / "genesis").is_dir() and (home / ".genesis").is_dir()
    assert "disable" not in (home / "calls").read_text()


def test_dry_run_does_not_enter_guard(owned_install):
    home, scripts, env = owned_install
    artifacts = []
    for root in (home / ".config/systemd/user", home / "runtime/systemd/user"):
        (root / "default.target.wants").mkdir()
        for unit in ("genesis-cbm-query.service", "genesis-cbm-query-clients.slice"):
            path = root / unit
            path.write_text("preserved")
            link = root / "default.target.wants" / unit
            link.symlink_to(path)
            artifacts.extend((path, link))
    identities = [(path.lstat().st_ino, path.read_bytes()) for path in artifacts]
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh"), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert (home / "genesis").is_dir()
    assert "disable genesis-cbm" not in (home / "calls").read_text()
    assert [(path.lstat().st_ino, path.read_bytes()) for path in artifacts] == identities
    assert "LOCKS_HELD rm" not in (home / "calls").read_text()


def test_marker_without_real_fds_refuses_actual_script(owned_install):
    home, scripts, env = owned_install
    result = subprocess.run(
        [
            "/bin/bash",
            str(scripts / "uninstall.sh"),
            "--non-interactive",
            "--managed-uninstall-fds",
            "6",
            "7",
            "8",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "refused" in result.stderr
    assert (home / "genesis").is_dir()


def test_actual_host_delegation_propagates_container_failure(owned_install, tmp_path):
    home, scripts, env = owned_install
    source = (scripts / "uninstall.sh").read_text()
    start = source.index('            incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c')
    end = source.index("            REMOVED+=", start)
    incus = home / "tools/incus"
    incus.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CALLS"\nexit 7\n')
    incus.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            "CONTAINER_NAME=genesis; CONTAINER_USER=ubuntu;\n"
            + source[start:end]
            + "\necho WRONG_SUCCESS",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 7 and "WRONG_SUCCESS" not in result.stdout
    calls = (home / "calls").read_text()
    assert 'codebase_managed.py" uninstall' in calls
    assert "rm -" not in calls


@pytest.mark.parametrize("failure", ["STOP_FAIL", "MANAGER_FAIL"])
def test_manager_or_loaded_stop_failure_aborts_actual_cleanup(owned_install, failure):
    home, scripts, env = owned_install
    env[failure] = "1"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh"), "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0 and "refused" in result.stderr
    assert (home / "genesis").is_dir() and (home / ".genesis").is_dir()
    assert "LOCKS_HELD rm" not in (home / "calls").read_text()


def test_inactive_scope_without_service_pid_allows_actual_cleanup(owned_install):
    home, scripts, env = owned_install
    env.update(ORPHAN="1", ORPHAN_STATE="inactive")
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh"), "--non-interactive"],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (home / "genesis").exists()


def test_actual_confirmation_cancel_preserves_roots_and_reports_retirement(owned_install):
    home, scripts, env = owned_install
    result = subprocess.run(
        ["/bin/bash", str(scripts / "uninstall.sh")],
        input="NO\n",
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0 and "Aborted" in result.stdout
    assert "cancelling removal leaves it disabled" in result.stdout
    assert (home / "genesis").is_dir() and (home / ".genesis").is_dir()
    calls = (home / "calls").read_text()
    assert "UnitFileState" in calls and "MainPID" in calls
    assert "disable genesis-cbm-query.service" not in calls  # strictly absent unit needs no mutation
    assert "LOCKS_HELD rm" not in calls


def test_host_delegation_executes_the_same_actual_transaction(owned_install):
    home, scripts, env = owned_install
    source = (scripts / "uninstall.sh").read_text()
    start = source.index('            incus exec "$CONTAINER_NAME" -- su - "$CONTAINER_USER" -c')
    end = source.index("            REMOVED+=", start)
    incus = home / "tools/incus"
    incus.write_text("""#!/usr/bin/python3
import subprocess,sys
assert sys.argv[1:8]==["exec","genesis","--","su","-","ubuntu","-c"]
sys.exit(subprocess.run(["/bin/bash","-c",sys.argv[8]]).returncode)
""")
    incus.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            "CONTAINER_NAME=genesis; CONTAINER_USER=ubuntu;\n" + source[start:end],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Genesis removal complete (direct)" in result.stdout
    assert not (home / "genesis").exists()
    assert [entry.name for entry in (home / ".genesis").iterdir()] == ["locks"]
    assert "LOCKS_HELD rm -rf" in (home / "calls").read_text()


@pytest.mark.parametrize("mode", ["guardian-only", "full"])
def test_actual_host_genesis_phase_excludes_guardian_and_full(owned_install, mode):
    home, scripts, env = owned_install
    source = (scripts / "uninstall.sh").read_text()
    phase = source[source.index("# ── Phase 5:") : source.index("# ── Phase 6:")]
    result = subprocess.run(
        [
            "/bin/bash",
            "-e",
            "-c",
            f'MODE={mode}; HAS_GENESIS=true; IN_CONTAINER=false; info() {{ echo "$*"; }};\n'
            + phase,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / "genesis").is_dir()
    assert not (home / "calls").exists()


def test_default_host_refusal_preserves_guardian_installation(owned_install):
    home, scripts, env = owned_install
    guardian = home / ".local/share/genesis-guardian"
    guardian.mkdir(parents=True)
    (guardian / "code").write_text("preserved")
    state = home / ".local/state/genesis-guardian/state"
    state.mkdir(parents=True)
    source = (scripts / "uninstall.sh").read_text()
    begin = source.index("if [ -f /run/host/container-manager ]")
    end = source.index("    IN_CONTAINER=true", begin)
    host = home / "host-uninstall.sh"
    host.write_text(source[:begin] + "if false; then\n" + source[end:])
    incus = home / "tools/incus"
    incus.write_text("""#!/usr/bin/python3
import os,sys
from pathlib import Path
with Path(os.environ["CALLS"]).open("a") as out:out.write("INCUS "+" ".join(sys.argv[1:])+"\\n")
if any("codebase_managed.py" in arg for arg in sys.argv):sys.exit(7)
if "test" in sys.argv and "-f" in sys.argv:sys.exit(1)
""")
    incus.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(host), "--non-interactive"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 7, result.stdout + result.stderr
    assert (guardian / "code").read_text() == "preserved"
    assert state.is_dir()
    calls = (home / "calls").read_text()
    assert "codebase_managed.py" in calls
    assert "device remove" not in calls and "LOCKS_HELD rm" not in calls


@pytest.mark.parametrize("kind", ["both", "system", "user", "dangling"])
def test_actual_cleanup_removes_both_qdrant_locations(owned_install, kind):
    home, scripts, env = owned_install
    system = home / "system-bin/qdrant"
    user = home / ".local/bin/qdrant"
    for path in (system, user):
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind == "dangling":
            path.symlink_to(home / "missing-binary")
        elif kind == "both" or (kind == "system" and path == system) or (kind == "user" and path == user):
            path.write_text("fixture binary")
    script = scripts / "uninstall.sh"
    script.write_text(script.read_text().replace("/usr/local/bin/qdrant", str(system)))
    sudo = home / "tools/sudo"
    sudo.write_text('#!/bin/sh\nexec "$@"\n')
    sudo.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(script), "--genesis-only", "--non-interactive"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not system.exists() and not system.is_symlink()
    assert not user.exists() and not user.is_symlink()


def test_failed_runtime_inventory_does_not_delete_install_roots(owned_install):
    home, scripts, env = owned_install
    finder = home / "tools/find"
    finder.write_text("#!/bin/sh\nexit 7\n")
    finder.chmod(0o755)
    result = subprocess.run(["/bin/bash", str(scripts / "uninstall.sh"), "--genesis-only", "--non-interactive"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and "could not enumerate runtime state" in result.stderr
    assert all((home / name).is_dir() for name in ("genesis", ".genesis", "data", ".qdrant"))
    assert list(home.glob(".genesis-uninstall.*")), "failed inventory is retained as evidence"


def test_system_qdrant_failure_still_removes_user_binary(owned_install):
    home, scripts, env = owned_install
    system = home / "system-bin/qdrant"
    user = home / ".local/bin/qdrant"
    for path in (system, user):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("owned binary")
    script = scripts / "uninstall.sh"
    script.write_text(script.read_text().replace("/usr/local/bin/qdrant", str(system)))
    (home / "tools/sudo").write_text("#!/bin/sh\nexit 1\n")
    remover = home / "tools/rm"
    remover.write_text(remover.read_text().replace(
        'os.execv("/usr/bin/rm",["rm",*sys.argv[1:]])',
        f'if {str(system)!r} in sys.argv:sys.exit(1)\nos.execv("/usr/bin/rm",["rm",*sys.argv[1:]])',
    ))
    result = subprocess.run(["/bin/bash", str(script), "--genesis-only", "--non-interactive"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0 and "Could not remove" in result.stdout
    assert system.read_text() == "owned binary" and not user.exists()


@pytest.mark.parametrize("target_exists", [False, True])
def test_cleanup_unlinks_known_units_without_removing_foreign_target(owned_install, target_exists):
    home, scripts, env = owned_install
    target = home / "external/unit"
    target.parent.mkdir()
    if target_exists:
        target.write_text("foreign unit")
    links = [home / ".config/systemd/user" / name for name in ("genesis-old.service", "genesis-old.timer", "qdrant.service")]
    for link in links:
        link.symlink_to(target)
    result = subprocess.run(["/bin/bash", str(scripts / "uninstall.sh"), "--genesis-only", "--non-interactive"], env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not link.is_symlink() for link in links)
    assert target.read_text() == "foreign unit" if target_exists else not target.exists()


def test_retained_diagnostics_do_not_require_file_digest(guard, monkeypatch, tmp_path, capsys):
    monkeypatch.delattr(guard.report_retained_state.__globals__["hashlib"], "file_digest")
    monkeypatch.setitem(guard.report_retained_state.__globals__, "read_settings", lambda *a, **kw: {key: str(tmp_path / key) for key in ("binary", "cache", "runtime")})
    guard.report_retained_state()
    assert capsys.readouterr().out.count("Preserved configured") == 3
