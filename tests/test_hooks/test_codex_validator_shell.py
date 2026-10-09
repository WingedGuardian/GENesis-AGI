"""Real data files and subprocess protocol, never production stores/providers."""

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "00000000-0000-0000-0000-000000000001"
PROBE = {"version": 1, "operation": "protocol_probe"}


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "hooks"))
    loaded = []
    for name, path in (("validator_shell", ROOT / "scripts/hooks/codex_validator_shell.py"),
                       ("validator_request", ROOT / "scripts/codex_validator_request.py")):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


@pytest.fixture
def files(tmp_path):
    workspace = tmp_path / "work space"
    workspace.mkdir(mode=0o700)
    requests = workspace / "requests"
    requests.mkdir(mode=0o700)
    request = requests / (TOKEN + ".json")
    request.write_text(json.dumps(PROBE))
    request.chmod(0o600)
    runtime = tmp_path / "runtime's space"
    (runtime / ".venv/bin").mkdir(parents=True)
    (runtime / ".venv/bin/python").symlink_to(sys.executable)
    (runtime / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/codex_validator_request.py",
                    runtime / "scripts/codex_validator_request.py")
    command = shlex.join([str(runtime / ".venv/bin/python"), "-I",
                          str(runtime / "scripts/codex_validator_request.py"),
                          "--workspace-root", str(workspace), "--request", str(request)])
    return workspace, request, runtime, command


def test_exact_carrier_and_read_only_probe(modules, files):
    shell, runner = modules
    workspace, request, runtime, command = files
    original = request.read_bytes()
    shell.check_command(command, workspace, runtime)
    assert runner.execute(runner.read_request(workspace, str(request))) == {**PROBE, "ok": True}
    assert request.read_bytes() == original


@pytest.mark.parametrize("change", [
    lambda c: c + "\n", lambda c: c + " # comment", lambda c: " " + c,
    lambda c: "env " + c, lambda c: "timeout 3 " + c,
    lambda c: "X=1 " + c, lambda c: "bash -c " + shlex.quote(c),
    lambda c: "eval " + shlex.quote(c), lambda c: c + " >/dev/null",
    lambda c: c + " 2>&1", lambda c: c + " && true", lambda c: c + " ; true",
    lambda c: c + " | cat", lambda c: "(" + c + ")",
    lambda c: c + " &", lambda c: c + " $(true)", lambda c: c + " <(true)",
    lambda c: c.replace(" -I ", " -c "), lambda c: c.replace(" --request ", " --unknown "),
    lambda c: c.replace(" --workspace-root ", " --workspace-root= "),
])
def test_unsupported_carriers_refuse(modules, files, change):
    shell, _ = modules
    workspace, _, runtime, command = files
    with pytest.raises(ValueError):
        shell.check_command(change(command), workspace, runtime)


@pytest.mark.parametrize("supplied", [
    TOKEN + ".json", "requests/" + TOKEN + ".json",
    "/outside/requests/" + TOKEN + ".json", "../" + TOKEN + ".json",
    "/wrong/" + TOKEN.replace("-", "") + ".json",
    "/wrong/{" + TOKEN + "}.json", "/wrong/1234.json", "/wrong/" + TOKEN + ".json.extra",
])
def test_request_path_never_uses_cwd(modules, files, supplied, monkeypatch, tmp_path):
    shell, _ = modules
    workspace, _, _, _ = files
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        shell.check_request(workspace, supplied)


@pytest.mark.parametrize("fault", ["symlink", "hardlink", "directory", "fifo", "public", "oversize", "missing", "parent_symlink", "public_parent", "public_workspace"])
def test_private_file_constraints(modules, files, fault, tmp_path):
    shell, _ = modules
    workspace, request, _, _ = files
    if fault in {"symlink", "directory", "fifo", "missing"}:
        request.unlink()
    if fault == "symlink":
        other = tmp_path / "other.json"
        other.write_text(json.dumps(PROBE))
        other.chmod(0o600)
        request.symlink_to(other)
    elif fault == "hardlink":
        os.link(request, tmp_path / "alias.json")
    elif fault == "directory":
        request.mkdir(mode=0o700)
    elif fault == "fifo":
        os.mkfifo(request, 0o600)
    elif fault == "public":
        request.chmod(0o644)
    elif fault == "oversize":
        request.write_bytes(b" " * (shell.MAX_REQUEST_BYTES + 1))
    elif fault == "parent_symlink":
        moved = tmp_path / "moved"
        request.parent.rename(moved)
        (workspace / "requests").symlink_to(moved, target_is_directory=True)
    elif fault == "public_parent":
        request.parent.chmod(0o755)
    elif fault == "public_workspace":
        workspace.chmod(0o755)
    with pytest.raises((ValueError, OSError)):
        shell.check_request(workspace, str(request))


@pytest.mark.parametrize("body", [
    b"{", b"null", b"[]", b'"private marker"',
    b'{"version":1,"version":1,"operation":"protocol_probe"}',
    b'{"version":NaN,"operation":"protocol_probe"}',
    b'{"version":1e999,"operation":"protocol_probe"}',
    b'{"version":true,"operation":"protocol_probe"}',
    b'{"version":1.0,"operation":"protocol_probe"}',
    b'{"version":2,"operation":"protocol_probe"}',
    b'{"version":1,"operation":"unknown private marker"}',
    b'{"version":1,"operation":"protocol_probe","command":"private marker"}',
])
def test_runner_refusals_are_static(files, body, tmp_path):
    workspace, request, _, _ = files
    request.write_bytes(body)
    result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/codex_validator_request.py"),
                             "--workspace-root", str(workspace), "--request", str(request)],
                            cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"Validator request refused\n"
    assert request.read_bytes() == body


def test_real_subprocess_probe_from_other_cwd(files, tmp_path):
    workspace, request, _, _ = files
    result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/codex_validator_request.py"),
                             "--workspace-root", str(workspace), "--request", str(request)],
                            cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {**PROBE, "ok": True}
    assert result.stderr == b""
    assert request.exists()


def test_malformed_invocation_does_not_echo_arguments(tmp_path):
    result = subprocess.run([sys.executable, "-I", str(ROOT / "scripts/codex_validator_request.py"),
                             "--private-marker", "private marker"], cwd=tmp_path,
                            capture_output=True, timeout=10)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"Validator request refused\n"


@pytest.mark.parametrize("change", ["replace", "grow"])
def test_runner_validates_actual_opened_file(modules, files, monkeypatch, change):
    _, runner = modules
    workspace, request, _, _ = files
    original = runner.private_open

    def changed_open(path, flags):
        if change == "replace":
            path.rename(path.with_suffix(".previous"))
            path.write_text(json.dumps(PROBE))
            path.chmod(0o600)
        else:
            path.write_bytes(b" " * (runner.MAX_REQUEST_BYTES + 1))
        return original(path, flags)

    monkeypatch.setattr(runner, "private_open", changed_open)
    with pytest.raises(ValueError):
        runner.read_request(workspace, str(request))


@pytest.mark.parametrize("root", ["workspace", "runtime"])
def test_control_character_roots_refuse(modules, files, root):
    shell, _ = modules
    workspace, _, runtime, command = files
    if root == "workspace":
        changed = Path(str(workspace) + chr(1))
        workspace.rename(changed)
        workspace = changed
    else:
        changed = Path(str(runtime) + chr(1))
        runtime.rename(changed)
        runtime = changed
    with pytest.raises(ValueError, match="roots contain unsupported characters"):
        shell.check_command(command, workspace, runtime)


@pytest.mark.parametrize("fault", ["missing_interpreter", "nonexecutable_interpreter", "missing_runner"])
def test_runtime_unavailable_refuses(modules, files, fault):
    shell, _ = modules
    workspace, _, runtime, command = files
    interpreter = runtime / ".venv/bin/python"
    if fault == "missing_runner":
        (runtime / "scripts/codex_validator_request.py").unlink()
    else:
        interpreter.unlink()
        if fault == "nonexecutable_interpreter":
            # Replace the private symlink; never chmod its shared interpreter.
            interpreter.write_text("synthetic fixture")
            interpreter.chmod(0o600)
    with pytest.raises(ValueError, match="runtime is unavailable"):
        shell.check_command(command, workspace, runtime)


def test_real_guard_wrapper_allows_exact_invocation(files, tmp_path):
    workspace, _, runtime, command = files
    for name in ("codex-validator-guard", "codex_validator_guard.py", "codex_validator_shell.py",
                 "codex_validator_patch.py", "shell_parse.py"):
        target = runtime / "scripts/hooks" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "scripts/hooks" / name, target)
    (runtime / "src").symlink_to(ROOT / "src", target_is_directory=True)
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
               "tool_input": {"command": command}, "cwd": str(tmp_path)}
    result = subprocess.run(["/bin/bash", str(runtime / "scripts/hooks/codex-validator-guard"),
                             "--runtime-root", str(runtime), "--workspace-root", str(workspace)],
                            input=json.dumps(payload).encode(), cwd=tmp_path, capture_output=True,
                            env={"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
                                 "HOME": str(tmp_path)}, timeout=10)
    assert result.returncode == 0
    assert result.stdout == b""
    assert result.stderr == b""
