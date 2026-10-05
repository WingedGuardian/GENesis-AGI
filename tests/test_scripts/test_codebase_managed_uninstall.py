"""Guarded cleanup holds real locks and executes the actual script in owned homes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests.test_scripts.test_codebase_managed_config import ROOT, managed

guard = managed


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
    for name in ("sleep", "sudo", "ss"):
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
    for name in ("code_intel_cbm_worker.py", "code_intel_cbm_admission.py"):
        shutil.copy2(ROOT / "scripts/lib" / name, scripts / "lib" / name)
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
def test_actual_direct_cleanup_does_not_depend_on_settings(owned_install, settings):
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
    assert result.returncode == 0, result.stdout + result.stderr
    for name in ("genesis", ".genesis", "data", ".qdrant"):
        assert not (home / name).exists()
    assert external.is_dir() and foreign.read_text() == "preserved"
    calls = (home / "calls").read_text()
    for name in ("genesis", ".genesis", "data", ".qdrant"):
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
    assert "disable genesis-cbm-query.service" in (home / "calls").read_text()
    assert "LOCKS_HELD rm" not in (home / "calls").read_text()


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
    assert not (home / "genesis").exists() and not (home / ".genesis").exists()
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
