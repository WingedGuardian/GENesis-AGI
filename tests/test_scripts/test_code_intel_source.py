"""Captured execution bytes must survive checkout drift without being relabelled."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_execution import scope as scope
from tests.test_scripts.test_index_marker_fresh_cleanup import _historical

ROOT = Path(__file__).resolve().parents[2]
SELF = "scripts/lib/code_intel_source.py"
PYTHON = (
    SELF,
    "scripts/lib/managed_units.py",
    "scripts/lib/code_intel_cbm_admission.py",
    "src/genesis/util/systemd.py",
    "scripts/lib/code_intel_cbm_worker.py",
    "scripts/lib/codebase_managed_unit.py",
    "scripts/lib/code_intel_execution.py",
    "scripts/lib/code_intel_scope.py",
    "scripts/lib/code_intel_queued_worker.py",
    "scripts/lib/code_intel_gitnexus.py",
    "scripts/lib/code_intel_owner.py",
    "scripts/lib/code_intel_owner_admission.py",
    "scripts/lib/index_marker_legacy.py",
    "scripts/lib/index_marker_recovery.py",
    "scripts/lib/index_marker_legs.py",
    "scripts/lib/index_marker_execution.py",
    "scripts/lib/index_marker_cli.py",
    "scripts/lib/index_marker.py",
    "scripts/codebase_managed.py",
)
SHELL = (
    "scripts/lib/code_intel_index.sh",
    "scripts/lib/gitnexus_version.sh",
    "scripts/lib/proc_pressure.sh",
    "scripts/lib/code_intel_scope.sh",
)
ALIASES = (
    "managed_units",
    "code_intel_cbm_admission",
    "_genesis_util_systemd",
    "code_intel_cbm_worker",
    "codebase_managed_unit",
    "_genesis_code_intel_execution",
    "_genesis_code_intel_scope",
    "_genesis_code_intel_queued_worker",
    "_genesis_code_intel_gitnexus",
    "_genesis_code_intel_owner",
    "_genesis_code_intel_owner_admission",
    "_genesis_index_marker_legacy",
    "_genesis_index_marker_recovery",
    "_genesis_index_marker_legs",
    "_genesis_index_marker_execution",
    "_genesis_index_marker_cli",
    "_genesis_index_marker",
    "_genesis_codebase_managed",
)


@pytest.fixture(autouse=True)
def isolated_modules():
    old = {name: sys.modules[name] for name in ALIASES if name in sys.modules}
    path = sys.path[:]
    for name in ALIASES:
        sys.modules.pop(name, None)
    yield
    for name in ALIASES:
        sys.modules.pop(name, None)
    sys.modules.update(old)
    sys.path[:] = path


def case(tmp_path, monkeypatch, profile="entry", object_format="sha1"):
    # Assert inside the test call, so an absent implementation is a real RED.
    assert (ROOT / SELF).is_file(), "captured-source implementation is absent"
    root = tmp_path / "checkout with 'quotes' 雪"
    root.mkdir()
    for name in (*PYTHON, *SHELL, "scripts/code_intel_runner.sh"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / name).read_bytes())
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    for args in (
        ("init", "-q", "--object-format=" + object_format),
        ("add", "."),
        (
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        ),
    ):
        subprocess.run(["git", "-C", str(root), *args], env=env, check=True, capture_output=True)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    raw = (root / SELF).read_bytes()
    spec = importlib.util.spec_from_file_location("_source_fixture", root / SELF)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    module.__dict__["_ENTRY_BYTES"] = raw
    exec(compile(raw, str(root / SELF), "exec", dont_inherit=True), module.__dict__)  # noqa: S102 - trusted fixture bytes
    return root, module, module.capture(root, profile, raw)


def test_captured_fresh_scope_and_cli_survive_deleted_checkout(
    tmp_path, monkeypatch, scope, capsys
):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    loaded = captured.assemble()
    queue = loaded["queue"]
    assert queue._cli is loaded["cli"]
    claimed, _, leaf, _, _ = _historical(
        queue, scope, loaded["proof"].__dict__, monkeypatch
    )
    # Delete the independently declared inventory, so omitting the scope from
    # capture cannot leave an on-disk fallback that makes this probe pass.
    for name in (*PYTHON, *SHELL, "scripts/code_intel_runner.sh"):
        (root / name).unlink()
    assert not captured.current_matches()
    (leaf / "memory.events").write_text("low 0\nhigh 0\nmax 1\noom 1\noom_kill 1\n")
    sibling = queue.claim_native(claimed["context"]["hash"], provider="gitnexus")
    assert sibling
    capsys.readouterr()
    assert queue.main(["execution-status", "--context", json.dumps(claimed["context"])]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["safety_kind"] == "resource" and status["evidence_pending"] is True
    assert status["typed_applied_state"] == "consumed" and status["typed_charged_attempts"] == 0
    assert status["cleanup_invalidated"] == 1
    assert queue.apply_native(claimed["context"]) == "consumed"
    assert queue.get_inflight(claimed["context"]["hash"])["claim_id"] == sibling["context"]["claim_id"]


@pytest.mark.parametrize("lock_state", [
    "held", "open", "missing", "foreign", "competing", "symlink",
    "named_nonregular", "descriptor_nonregular",
])
def test_captured_runner_entry_preserves_only_its_canonical_lock(
    tmp_path, monkeypatch, lock_state
):
    root, module, old = case(tmp_path, monkeypatch, "runner")
    # A harmless scratch entry reports the exact received inode. No provider,
    # manager or queue operation is reachable, even before the transport fix.
    entry = root / "scripts/lib/code_intel_index.sh"
    entry.write_text(
        "#!/bin/bash\nexec /usr/bin/python3 -I -c "
        + shlex.quote("import os,json;s=os.fstat(8);print(json.dumps([s.st_dev,s.st_ino]))")
        + "\n"
    )
    subprocess.run(
        ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c",
         "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-am", "scratch entry"],
        check=True, capture_output=True,
    )
    captured = module.capture(root, "runner", old.buffers[SELF])
    lock = tmp_path / "state/locks/code-intel-runner.lock"
    lock.parent.mkdir(parents=True)
    selected = lock
    if lock_state == "symlink":
        selected = tmp_path / "symlink-target"
        selected.touch()
        lock.symlink_to(selected)
    elif lock_state == "named_nonregular":
        lock.mkdir()
        selected = tmp_path / "foreign"
        selected.touch()
    else:
        lock.touch()
    held = selected.open("a")
    if lock_state == "competing":
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if lock_state == "foreign":
        selected = tmp_path / "foreign"
    elif lock_state == "descriptor_nonregular":
        selected = Path("/dev/null")
    setup = "exec 8>&-;" if lock_state == "missing" else "exec 8>" + shlex.quote(str(selected)) + ";"
    if lock_state == "held":
        setup += "flock -n 8 || exit 97;"
    try:
        with captured.sealed() as capsule:
            result = subprocess.run(
                ["/bin/bash", "--noprofile", "--norc", "-p", "-c",
                 setup + "exec " + shlex.join(captured.resume_command("shell", "entry"))],
                stdin=capsule, capture_output=True, text=True, timeout=30,
            )
        if lock_state in ("held", "open"):
            assert result.returncode == 0, result.stderr
            metadata = lock.stat()
            assert json.loads(result.stdout) == [metadata.st_dev, metadata.st_ino]
        else:
            assert result.returncode != 0 and not result.stdout
    finally:
        held.close()


def test_actual_source_capsule_survives_creator_exit_before_describe(tmp_path, monkeypatch):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    read_gate, write_gate = os.pipe()
    consumer = """import json,os,sys
if os.read(int(sys.argv[1]),1)!=b'1': raise SystemExit(2)
os.close(int(sys.argv[1]))
command=json.loads(sys.argv[2]);os.execv(command[0],command)
"""
    creator_program = """import importlib.util,json,os,pathlib,subprocess,sys
root=pathlib.Path(sys.argv[1]);path=root/'scripts/lib/code_intel_source.py'
raw=path.read_bytes();spec=importlib.util.spec_from_file_location('_source_creator',path)
module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
module.__dict__['_ENTRY_BYTES']=raw
exec(compile(raw,str(path),'exec',dont_inherit=True),module.__dict__)
captured=module.capture(root,'runner',raw);gate=int(sys.argv[2])
with captured.sealed() as fd:
    subprocess.Popen([sys.executable,'-I','-S','-c',sys.argv[3],str(gate),
                      json.dumps(captured.resume_command())],stdin=fd,pass_fds=(gate,))
os._exit(0)
"""
    try:
        with subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", creator_program, str(root), str(read_gate), consumer],
            pass_fds=(read_gate,), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as creator:
            os.close(read_gate)
            read_gate = None
            # The consumer cannot dispatch until its actual capsule creator
            # has exited; source validity is independent of creator liveness.
            assert creator.wait(timeout=30) == 0
            os.write(write_gate, b"1")
            stdout, stderr = creator.communicate(timeout=30)
        observed = json.loads(stdout)
        assert observed == dict(commit=captured.commit, files=captured.files, automatic_authority=False), stderr
    finally:
        if read_gate is not None:
            os.close(read_gate)
        os.close(write_gate)


@pytest.mark.parametrize("profile", ["entry", "runner"])
@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_exact_closed_inventory_commit_and_immutable_buffers(
    tmp_path, monkeypatch, profile, object_format
):
    root, module, captured = case(tmp_path, monkeypatch, profile, object_format)
    expected = {*PYTHON, *SHELL} | (
        {"scripts/code_intel_runner.sh"} if profile == "runner" else set()
    )
    assert set(captured.buffers) == expected
    assert captured.files == {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in expected
    }
    assert (
        captured.commit
        == subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    )
    with pytest.raises(TypeError):
        captured.buffers[SELF] = b"changed"
    assert captured.current_matches()
    with pytest.raises(ValueError, match="profile"):
        module.capture(root, "arbitrary", captured.buffers[SELF])


@pytest.mark.parametrize("name", [*PYTHON, *SHELL, "scripts/code_intel_runner.sh"])
def test_every_captured_path_drift_is_detected_without_relabelling(tmp_path, monkeypatch, name):
    root, module, captured = case(tmp_path, monkeypatch, "runner")
    before = dict(captured.files)
    (root / name).write_text("raise RuntimeError('new unbound bytes')\n")
    assert not captured.current_matches()
    assert captured.files == before
    if name == SELF:
        # The already captured entry remains A, correctly bound to commit A.
        # Disk freshness is separate; it must not relabel or discard A.
        assert module.capture(root, "runner", captured.buffers[SELF]).files == before
    else:
        with pytest.raises(ValueError, match="commit|source"):
            module.capture(root, "runner", captured.buffers[SELF])
    loaded = captured.assemble()
    assert loaded["worker"].BUILD == loaded["managed"].BUILD
    assert loaded["worker"].load_managed() is loaded["managed"].__dict__
    assert loaded["proof"]._ADMISSION is loaded["admission"].__dict__
    assert loaded["queue"]._proof is loaded["proof"]
    assert loaded["queue"]._cli is loaded["cli"]


    assert (
        loaded["queued"].QueuedRun.__init__.__globals__["_CAPTURED_CLEANUP"]
        is loaded["scope"].cleanup_observation
    )
    assert root / "scripts/codebase_managed.py" == loaded["managed"].SCRIPT
    assert loaded["unit"].systemctl_env() == loaded["systemd"].systemctl_env()


@pytest.mark.parametrize("alias", ALIASES)
def test_foreign_cached_module_refuses_without_replacing_it(tmp_path, monkeypatch, alias):
    _, _, captured = case(tmp_path, monkeypatch)
    foreign = object()
    monkeypatch.setitem(sys.modules, alias, foreign)
    with pytest.raises(ValueError, match="already loaded"):
        captured.assemble()
    assert sys.modules[alias] is foreign
    assert all(name not in sys.modules for name in ALIASES if name != alias)


def test_sealed_roundtrip_keeps_old_bytes_after_files_disappear(tmp_path, monkeypatch):
    root, module, captured = case(tmp_path, monkeypatch, "runner")
    with captured.sealed() as fd:
        assert fcntl.fcntl(fd, fcntl.F_GET_SEALS) & module.SEALS == module.SEALS
        os.lseek(fd, os.fstat(fd).st_size, os.SEEK_SET)
        for name in captured.buffers:
            (root / name).unlink()
        restored = module.restore(fd, captured.buffers[SELF], captured.transport_sha256)
        assert restored.files == captured.files and restored.commit == captured.commit
        assert (
            restored.assemble()["worker"].load_managed()["SCRIPT"]
            == root / "scripts/codebase_managed.py"
        )
        for action in (
            lambda: os.pwrite(fd, b"x", 0),
            lambda: os.ftruncate(fd, 0),
            lambda: os.ftruncate(fd, os.fstat(fd).st_size + 1),
        ):
            with pytest.raises(OSError):
                action()


@pytest.mark.parametrize(
    "fault",
    ["unsealed", "version", "unsupported-version", "missing", "extra", "digest", "entry", "duplicate"],
)
def test_capsule_rejects_incomplete_or_wrong_transport(tmp_path, monkeypatch, fault):
    _, module, captured = case(tmp_path, monkeypatch)
    with captured.sealed() as good:
        raw = os.pread(good, os.fstat(good).st_size, 0)
    document = json.loads(raw)
    if fault == "version":
        document["version"] = 2
    elif fault == "unsupported-version":
        document["version"] = 3
    elif fault == "missing":
        document["buffers"].pop(SHELL[0])
    elif fault == "extra":
        document["buffers"]["arbitrary.py"] = ""
    elif fault == "digest":
        document["files"][SHELL[0]] = "0" * 64
    payload = json.dumps(document).encode()
    if fault == "duplicate":
        payload = b'{"version":9,' + payload[1:]
    fd = os.memfd_create("fixture", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, payload)
        if fault != "unsealed":
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, module.SEALS)
        with pytest.raises(ValueError):
            module.restore(
                fd,
                b"different entry" if fault == "entry" else captured.buffers[SELF],
                hashlib.sha256(payload).hexdigest(),
            )
    finally:
        os.close(fd)


def test_settings_capture_is_exact_and_validation_is_preserved(tmp_path, monkeypatch):
    root, _, captured = case(tmp_path, monkeypatch)
    managed = captured.assemble()["managed"]
    settings = root / "settings.json"
    raw = b'{ "version":2,"build":"wrong" }\n'
    settings.write_bytes(raw)
    seen = []
    assert managed.read_document(settings, capture=seen.append) == json.loads(raw)
    assert seen == [raw]
    with pytest.raises(ValueError):
        managed.read_settings(settings, capture=seen.append)
    assert seen == [raw, raw]  # Parsed bytes arrive before settings-schema validation.
    settings.write_bytes(b"x" * 65537)
    seen.clear()
    with pytest.raises(ValueError, match="64 KiB"):
        managed.read_document(settings, capture=seen.append)
    assert seen == []


def test_real_cli_bootstrap_captures_then_describes_without_queue_or_provider(
    tmp_path, monkeypatch
):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(root / SELF), "describe", "runner"],
        capture_output=True,
        text=True,
        check=True,
    )
    value = json.loads(result.stdout)
    assert value["files"] == captured.files and value["commit"] == captured.commit
    assert value["automatic_authority"] is False
    assert not (tmp_path / "state").exists()


def test_actual_isolated_handoff_uses_capsule_after_checkout_files_disappear(tmp_path, monkeypatch):
    root, module, captured = case(tmp_path, monkeypatch, "runner")
    with captured.sealed() as fd:
        for name in captured.buffers:
            (root / name).unlink()
        result = subprocess.run(
            captured.resume_command(), stdin=fd, capture_output=True, text=True, check=True
        )
    value = json.loads(result.stdout)
    assert value == dict(commit=captured.commit, files=captured.files, automatic_authority=False)
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("drift", ["removed", "changed"])
def test_captured_queue_cli_uses_captured_bytes_after_disk_drift(
    tmp_path, monkeypatch, capsys, drift
):
    root, _, captured = case(tmp_path, monkeypatch)
    cli = root / "scripts/lib/index_marker_cli.py"
    if drift == "removed":
        cli.unlink()
    else:
        cli.write_text("raise RuntimeError('unbound CLI bytes executed')\n")
    loaded = captured.assemble()
    queue = loaded["queue"]
    assert queue._cli is loaded["cli"]
    repo = str(root / "literal '$() 雪\n")
    assert queue.main(["hash", "--repo", repo]) == 0
    assert capsys.readouterr().out.strip() == queue.marker_hash(repo)
    assert queue.main(["import-status"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "absent"
    assert not (tmp_path / "state").exists()


def test_handoff_rejects_wrong_entry_before_compiling_it(tmp_path, monkeypatch):
    _, module, captured = case(tmp_path, monkeypatch)
    with captured.sealed() as good:
        value = json.loads(os.pread(good, os.fstat(good).st_size, 0))
    raw = b"print('unbound-entry-executed')\nraise SystemExit(0)\n"
    value["buffers"][SELF] = base64.b64encode(raw).decode("ascii")
    value["files"][SELF] = hashlib.sha256(raw).hexdigest()
    fd = os.memfd_create("wrong-entry", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        os.write(fd, json.dumps(value).encode())
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, module.SEALS)
        result = subprocess.run(captured.resume_command(), stdin=fd, capture_output=True, text=True)
        assert "unbound-entry-executed" not in result.stdout
        assert result.returncode != 0
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["file", "scripts-directory"])
def test_captured_managed_origin_does_not_follow_later_symlinks(tmp_path, monkeypatch, kind):
    root, _, captured = case(tmp_path, monkeypatch)
    expected = root / "scripts/codebase_managed.py"
    if kind == "file":
        foreign = tmp_path / "foreign.py"
        foreign.write_text("raise RuntimeError('foreign code must not execute')\n")
        expected.unlink()
        expected.symlink_to(foreign)
    else:
        moved = tmp_path / "moved-scripts"
        (root / "scripts").rename(moved)
        (root / "scripts").symlink_to(moved, target_is_directory=True)
    loaded = captured.assemble()["managed"]
    assert expected == loaded.SCRIPT
    assert loaded.__file__ == str(expected)
    assert loaded.runtime_config.__code__.co_filename == str(expected)
    # Isolate source-origin policy from unrelated immutable artifact checks.
    monkeypatch.setattr(loaded, "read_settings", lambda path, **kwargs: {"main": str(root)})
    monkeypatch.setattr(loaded, "validate_provider_paths", lambda config: None)
    assert loaded.runtime_config(root / "settings")["main"] == str(root)
    foreign_main = tmp_path / "foreign-main"
    (foreign_main / ".git").mkdir(parents=True)
    monkeypatch.setattr(loaded, "read_settings", lambda path, **kwargs: {"main": str(foreign_main)})
    with pytest.raises(ValueError, match="primary checkout"):
        loaded.runtime_config(root / "settings")


@pytest.mark.parametrize("role", ["entry", "runner"])
@pytest.mark.parametrize(
    "helper,symbol",
    [
        ("gitnexus_version.sh", "genesis_gitnexus_installed_is_pinned"),
        ("proc_pressure.sh", "pressure_loadavg1"),
        ("code_intel_scope.sh", "_run_with_watchdog"),
    ],
)
def test_shell_prelude_sources_only_captured_helpers_and_original_origin(
    tmp_path, monkeypatch, role, helper, symbol
):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    (root / "scripts/lib" / helper).write_text("exit 93\n")
    program = captured.shell_prelude(role) + "\n_ci_source " + shlex.quote(helper) + "\n"
    program += (
        "declare -F " + shlex.quote(symbol) + ' >/dev/null || exit 94\nprintf "%s" "$_CI_ORIGIN"\n'
    )
    with captured.sealed() as fd:
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-p", "-c", program],
            stdin=fd,
            capture_output=True,
            text=True,
            check=True,
        )
    name = "scripts/code_intel_runner.sh" if role == "runner" else "scripts/lib/code_intel_index.sh"
    assert result.stdout == str(root / name)


def test_captured_shell_entry_keeps_logical_origin_after_all_sources_disappear(
    tmp_path, monkeypatch
):
    root, _, captured = case(tmp_path, monkeypatch, "runner")
    repo = tmp_path / "linked-repository"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: unused linked fixture\n")
    for name in captured.buffers:
        (root / name).unlink()
    with captured.sealed() as fd:
        result = subprocess.run(
            captured.shell_command("entry", [str(repo), "both", "fast"]),
            stdin=fd,
            env={**os.environ, "CODE_INTEL_INDEX_DISABLE": "0"},
            capture_output=True,
            text=True,
            check=True,
        )
    assert "linked git worktree" in result.stdout
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("startup", [False, True])
@pytest.mark.parametrize("function", [False, True])
@pytest.mark.parametrize("noexec", [False, True])
def test_captured_runner_stays_fenced_and_uses_held_queue_after_deletion(
    tmp_path, monkeypatch, startup, function, noexec
):
    _, _, captured = case(tmp_path, monkeypatch, "runner")
    marker = tmp_path / "startup-executed"
    startup_file = tmp_path / "startup.sh"
    startup_file.write_text("printf bad > " + shlex.quote(str(marker)) + "\n")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
        and not key.startswith("BASH_FUNC_")
    }
    if startup:
        env["BASH_ENV"] = str(startup_file)
    if function:
        env["BASH_FUNC_flock%%"] = "() { printf bad > " + shlex.quote(str(marker)) + "; return 0; }"
    if noexec:
        env["SHELLOPTS"] = "noexec"
    with captured.sealed() as fd:
        for name in captured.buffers:
            (captured.root / name).unlink()
        result = subprocess.run(
            captured.resume_command("shell", "runner"),
            stdin=fd,
            env=env,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "state/code-intelligence-runner.log").read_text()
    assert "automatic queue protocol incomplete" in log
    assert not marker.exists()
    assert {
        str(path.relative_to(tmp_path / "state")) for path in (tmp_path / "state").rglob("*")
    } == {"locks", "locks/code-intel-runner.lock", "code-intelligence-runner.log"}


def test_captured_queue_role_observes_without_initializing_after_deletion(tmp_path, monkeypatch):
    _, _, captured = case(tmp_path, monkeypatch, "runner")
    with captured.sealed() as fd:
        for name in captured.buffers:
            (captured.root / name).unlink()
        result = subprocess.run(
            captured.resume_command("queue", "automatic-ready"),
            stdin=fd,
            capture_output=True,
            text=True,
        )
    assert result.returncode == 10, result.stderr
    assert json.loads(result.stdout) == {"state": "absent", "receipts_available": False}
    assert not (tmp_path / "state").exists()
