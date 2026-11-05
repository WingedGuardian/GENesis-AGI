"""Isolated profile admission; every runtime/store/process fixture is owned."""

import asyncio
import contextlib
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_scripts.codex_validator_git import fixture_git_environment

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("sibling", ["codex_external_mcp", "codex_validator_adapter"])
def test_launcher_sibling_import_failure_stops_before_startup(tmp_path, sibling):
    runtime = tmp_path / "runtime"
    scripts = runtime / "scripts"
    hooks = scripts / "hooks"
    hooks.mkdir(parents=True)
    entry = scripts / "codex_validator_launcher.py"
    entry.write_bytes((ROOT / "scripts/codex_validator_launcher.py").read_bytes())
    for name, target, healthy in [
        ("codex_external_mcp", scripts, "_SESSION_CONTEXT = ()\n"),
        ("codex_validator_adapter", hooks, "def hook_command(runtime): return 'owned'\n"),
    ]:
        (target / (name + ".py")).write_text(
            "raise RuntimeError('owned sibling import failure')\n" if name == sibling else healthy
        )
    home = tmp_path / "profile"
    result = subprocess.run(
        [sys.executable, str(entry), "prepare", "--home", str(home), "--model", "owned-unused"],
        cwd=runtime, capture_output=True, text=True,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}, timeout=15,
    )
    assert result.returncode == 1 and "owned sibling import failure" in result.stderr
    assert not home.exists()


@pytest.fixture
def launcher(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    return private_module("validator_launcher_test", ROOT / "scripts/codex_validator_launcher.py")


def test_generated_profile_is_role_scoped_and_project_untrusted(launcher, tmp_path):
    value = tomllib.loads(launcher.policy(tmp_path, "owner-chosen-model"))
    assert value["model"] == "owner-chosen-model"
    assert value["approval_policy"] == "never"
    assert value["sandbox_mode"] == "danger-full-access"
    assert value["projects"][str(tmp_path)]["trust_level"] == "untrusted"
    assert set(value["mcp_servers"]) == {"genesis_validator_health", "genesis_validator_memory"}
    for key, server in value["mcp_servers"].items():
        assert server["args"][1:] == ["validator", "--server", key.rsplit("_", 1)[1]]
        assert server["required"] is True
    assert value["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 15
    assert value["agents"]["enabled"] is False


def test_profile_requires_exact_bytes_and_trust_suffix(launcher, tmp_path):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    path = home / "config.toml"
    raw = launcher.policy(tmp_path, "owner-model").encode()
    launcher.write_new(path, raw)
    assert launcher.profile_bytes(home, tmp_path, trusted=False) == raw
    suffix = launcher.trust_suffix(str(path) + ":pre_tool_use:0:0", "sha256:" + "a" * 64)
    path.write_bytes(raw + suffix.encode())
    assert launcher.profile_bytes(home, tmp_path, trusted=True) == raw + suffix.encode()
    with path.open("a") as out:
        out.write('\n[mcp_servers.extra]\ncommand="false"\n')
    with pytest.raises(ValueError):
        launcher.profile_bytes(home, tmp_path, trusted=True)


@pytest.mark.parametrize("replacement", ["partial", "symlink", "public", "hardlink"])
def test_private_config_failure_states_refuse(launcher, tmp_path, replacement):
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    path = home / "config.toml"
    if replacement == "symlink":
        other = tmp_path / "other"
        other.write_text("partial")
        path.symlink_to(other)
    else:
        launcher.write_new(path, b"partial")
        if replacement == "public":
            path.chmod(0o644)
        elif replacement == "hardlink":
            os.link(path, home / "alias")
    with pytest.raises((ValueError, OSError)):
        launcher.profile_bytes(home, tmp_path, trusted=True)


@pytest.fixture
def qualified_tree(launcher, tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", str(runtime)], check=True, env=fixture_git_environment())
    for rel in launcher.EXTRA_SOURCES:
        path = runtime / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"qualified\n")
    hook = runtime / "scripts/hooks/probe.py"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_bytes(b"guard\n")
    path = runtime / ".claude/hooks/genesis-hook"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"launcher\n")
    path.chmod(0o755)
    subprocess.run(
        [
            "git",
            "-C",
            str(runtime),
            "add",
            *launcher.EXTRA_SOURCES,
            "scripts/hooks/probe.py",
            ".claude/hooks/genesis-hook",
        ],
        check=True, env=fixture_git_environment(),
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(runtime),
            "-c",
            "user.name=Owned fixture",
            "-c",
            "user.email=fixture@example.com",
            "commit",
            "-qm",
            "Owned qualification fixture",
        ],
        check=True, env=fixture_git_environment(),
    )
    head = subprocess.check_output(
        ["git", "-C", str(runtime), "rev-parse", "HEAD"], text=True, env=fixture_git_environment()
    ).strip()
    return runtime, head, hook


def test_qualified_source_allows_only_runtime_untracked_directories(launcher, qualified_tree):
    runtime, head, _ = qualified_tree
    (runtime / "data").mkdir()
    (runtime / "data/runtime").write_bytes(b"owned")
    (runtime / "tmp").mkdir()
    (runtime / "tmp/runtime").write_bytes(b"owned")
    launcher.check_sources(runtime, head)


@pytest.mark.parametrize("flag", [None, "--assume-unchanged", "--skip-worktree"])
def test_actual_guard_mutation_refused_despite_index_flags(launcher, qualified_tree, flag):
    runtime, head, hook = qualified_tree
    if flag:
        subprocess.run(
            ["git", "-C", str(runtime), "update-index", flag, "scripts/hooks/probe.py"], check=True, env=fixture_git_environment()
        )
    hook.write_bytes(b"changed\n")
    with pytest.raises(ValueError):
        launcher.check_sources(runtime, head)


@pytest.mark.parametrize("change", ["missing", "extra", "mode", "symlink"])
def test_qualified_input_failure_states(launcher, qualified_tree, change):
    runtime, head, hook = qualified_tree
    if change == "missing":
        hook.unlink()
    elif change == "extra":
        (hook.parent / "unqualified.py").write_bytes(b"extra")
    elif change == "mode":
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    else:
        body = hook.read_bytes()
        hook.unlink()
        other = runtime / "other"
        other.write_bytes(body)
        hook.symlink_to(other)
    with pytest.raises(ValueError):
        launcher.check_sources(runtime, head)


def test_model_name_is_toml_escaped_data(launcher, tmp_path):
    chosen = 'owner"\n[mcp_servers.injected]'
    parsed = tomllib.loads(launcher.policy(tmp_path, chosen))
    assert parsed["model"] == chosen
    assert len(parsed["mcp_servers"]) == 2


def test_cli_has_no_pass_through_policy_override():
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/codex_validator_launcher.py"),
            "run",
            "--config",
            "hooks={}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


@pytest.mark.parametrize(
    "name,expected",
    [
        ("/owned/scripts/update.sh", True),
        ("/owned/scripts/deploy_code_only.sh", True),
        ("/owned/tmp/genesis-update-ABC123.sh", True),
        ("/owned/tmp/genesis-update-ABC123.sh.notes", False),
        ("/owned/tmp/not-genesis-update-ABC123.sh", False),
    ],
)
def test_updater_actual_self_copy_and_lookalikes(launcher, name, expected):
    child = subprocess.Popen(
        ["/bin/bash", "-c", "read -r token", name], stdin=subprocess.PIPE, start_new_session=True
    )
    try:
        found = subprocess.run(
            ["/usr/bin/pgrep", "-f", launcher.DEPLOY_PATTERN], capture_output=True, text=True
        )
        assert found.returncode in (0, 1)
        assert (str(child.pid) in found.stdout.split()) is expected
    finally:
        child.communicate(input=b"done\n", timeout=5)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_qualified_executable_requires_owner_execute(launcher, qualified_tree, flag):
    runtime, head, _ = qualified_tree
    path = runtime / ".claude/hooks/genesis-hook"
    subprocess.run(
        ["git", "-C", str(runtime), "update-index", flag, ".claude/hooks/genesis-hook"], check=True, env=fixture_git_environment()
    )
    path.chmod(0o655)  # Other execute bits must not substitute for this owner.
    assert not path.stat().st_mode & stat.S_IXUSR
    with pytest.raises(ValueError):
        launcher.check_sources(runtime, head)


@pytest.mark.parametrize("field", ["runtime", "model", "home"])
@pytest.mark.parametrize("suffix", ["ascii", "α", "🙂"])
def test_valid_unicode_scalar_values_survive_toml(launcher, tmp_path, field, suffix):
    runtime = tmp_path / ("runtime" + (suffix if field == "runtime" else ""))
    model = "owner-model" + (suffix if field == "model" else "")
    home = tmp_path / ("private" + (suffix if field == "home" else ""))
    key = str(home / "config.toml") + ":pre_tool_use:0:0"
    value = tomllib.loads(
        launcher.policy(runtime, model) + launcher.trust_suffix(key, "sha256:" + "a" * 64)
    )
    assert value["model"] == model
    assert value["projects"][str(runtime)]["trust_level"] == "untrusted"
    assert value["hooks"]["state"][key]["trusted_hash"] == "sha256:" + "a" * 64


async def test_cleanup_uncertainty_refuses_readiness(launcher, monkeypatch):
    from types import SimpleNamespace

    from genesis.util import proc_kill

    async def wait_error():
        raise TimeoutError("owned unretired fake child")

    process = SimpleNamespace(pid=43210, returncode=None, wait=wait_error)
    signals = []
    monkeypatch.setattr(proc_kill.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(proc_kill, "DEFAULT_REAP_TIMEOUT_S", 0.01)
    with pytest.raises(proc_kill.ProcessCleanupUncertain):
        await launcher.finish_child(process)
    assert signals and all(pid == process.pid for pid, _ in signals)


async def test_completed_cleanup_control(launcher, monkeypatch):
    from types import SimpleNamespace

    from genesis.util import proc_kill

    async def wait_done():
        return 0

    def absent(pid, sig):
        assert pid == 43210
        raise ProcessLookupError

    monkeypatch.setattr(proc_kill.os, "killpg", absent)
    process = SimpleNamespace(pid=43210, returncode=0, wait=wait_done)
    await launcher.finish_child(process)


@pytest.mark.parametrize("cancels", [0, 1, 2])
@pytest.mark.parametrize("uncertain", [False, True])
async def test_cleanup_preserves_cancellation_after_retirement(
    launcher, monkeypatch, cancels, uncertain
):
    from genesis.util.proc_kill import ProcessCleanupUncertain

    entered, release = asyncio.Event(), asyncio.Event()
    retired = []

    async def owned_cleanup(process):
        entered.set()
        await release.wait()
        retired.append(process)
        if uncertain:
            raise ProcessCleanupUncertain("owned uncertainty")

    monkeypatch.setattr(launcher, "stop_child", owned_cleanup)
    task = asyncio.create_task(launcher.finish_child("owned-child"))
    await entered.wait()
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    if uncertain:
        with pytest.raises(ProcessCleanupUncertain):
            await task
    elif cancels:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
    assert retired == ["owned-child"]


@pytest.mark.parametrize("cancels", [1, 2])
async def test_creation_before_handle_cancellation_retires_child(
    launcher, monkeypatch, tmp_path, cancels
):
    from types import SimpleNamespace

    entered, release = asyncio.Event(), asyncio.Event()
    retired = []
    process = SimpleNamespace(pid=43210)

    async def owned_creation(*args, **kwargs):
        entered.set()
        await release.wait()
        return process

    async def owned_cleanup(child):
        retired.append(child)

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", owned_creation)
    monkeypatch.setattr(launcher, "finish_child", owned_cleanup)
    task = asyncio.create_task(launcher.spawn(["owned-fake-command"], tmp_path, {}))
    await entered.wait()
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired == [process]


@pytest.mark.parametrize(
    "fault",
    [
        "control",
        "hook_key",
        "hook_hash",
        "hook_trust",
        "hook_disabled",
        "hook_async",
        "hook_command",
        "hook_event",
        "hook_errors",
        "hook_extra",
        "hook_timeout",
        "model",
        "provider",
        "approval",
        "sandbox",
        "project_trust",
        "extra_server",
        "server_command",
        "extra_event",
        "delegation",
        "plugin",
        "apps_enabled",
        "apps_unavailable",
        "extra_status_server",
        "missing_tool",
        "tools_error",
        "status_cursor",
        "duplicate_server",
        "protocol_error",
        "eof",
        "changed_profile",
    ],
)
async def test_effective_native_admission_refuses_drift(launcher, monkeypatch, tmp_path, fault):
    from types import SimpleNamespace

    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    path = home / "config.toml"
    fingerprint = "sha256:" + "a" * 64
    key = str(path) + ":pre_tool_use:0:0"
    raw = (
        launcher.policy(tmp_path, "owned-model") + launcher.trust_suffix(key, fingerprint)
    ).encode()
    launcher.write_new(path, raw)
    effective = tomllib.loads(raw.decode())
    # The fixed launcher CLI override is reflected by native config/read.
    effective["features"]["apps"] = False
    hook = {
        "key": key,
        "eventName": "preToolUse",
        "handlerType": "command",
        "command": effective["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
        "matcher": ".*",
        "async": False,
        "enabled": True,
        "timeoutSec": 15,
        "sourcePath": str(path),
        "trustStatus": "trusted",
        "currentHash": fingerprint,
    }
    listing = {"data": [{"errors": [], "hooks": [hook]}]}
    status = {
        "data": [
            {
                "name": "genesis_validator_" + name,
                "tools": {t: {} for t in launcher.profile_tools(name, "validator")},
                "toolsError": None,
            }
            for name in ("health", "memory")
        ],
        "nextCursor": None,
    }
    hook_faults = {
        "hook_key": ("key", "other"),
        "hook_hash": ("currentHash", "invalid"),
        "hook_trust": ("trustStatus", "untrusted"),
        "hook_disabled": ("enabled", False),
        "hook_async": ("async", True),
        "hook_command": ("command", "false"),
        "hook_event": ("eventName", "postToolUse"),
        "hook_timeout": ("timeoutSec", 99),
    }
    if fault in hook_faults:
        field, value = hook_faults[fault]
        hook[field] = value
    elif fault == "hook_errors":
        listing["data"][0]["errors"] = ["owned error"]
    elif fault == "hook_extra":
        listing["data"][0]["hooks"].append(dict(hook))
    elif fault in ("model", "provider", "approval", "sandbox"):
        field = {
            "model": "model",
            "provider": "model_provider",
            "approval": "approval_policy",
            "sandbox": "sandbox_mode",
        }[fault]
        effective[field] = "changed"
    elif fault == "project_trust":
        effective["projects"][str(tmp_path)]["trust_level"] = "trusted"
    elif fault == "extra_server":
        effective["mcp_servers"]["extra"] = {}
    elif fault == "server_command":
        effective["mcp_servers"]["genesis_validator_health"]["command"] = "false"
    elif fault == "extra_event":
        effective["hooks"]["PostToolUse"] = ["owned extra"]
    elif fault == "delegation":
        effective["agents"]["enabled"] = True
    elif fault == "plugin":
        effective["features"]["plugins"] = True
    elif fault == "apps_enabled":
        effective["features"]["apps"] = True
    elif fault == "apps_unavailable":
        effective["features"].pop("apps")
    elif fault == "extra_status_server":
        status["data"].append({"name": "codex_apps", "tools": {}, "toolsError": None})
    elif fault == "missing_tool":
        status["data"][0]["tools"].pop(next(iter(status["data"][0]["tools"])))
    elif fault == "tools_error":
        status["data"][0]["toolsError"] = "owned discovery error"
    elif fault == "status_cursor":
        status["nextCursor"] = "more"
    elif fault == "duplicate_server":
        status["data"].append(dict(status["data"][0]))
    frames = [
        json.dumps({"id": i, "result": value}).encode() + b"\n"
        for i, value in enumerate(({}, listing, {"config": effective}, status), 1)
    ]
    if fault == "protocol_error":
        frames[1] = b'{"id":2,"error":{"message":"owned error"}}\n'
    if fault == "eof":
        frames = []
    retired = []
    writes = []
    invocations = []

    async def readline():
        if fault == "changed_profile":
            path.write_bytes(raw + b"\n")
        return frames.pop(0) if frames else b""

    async def drain():
        pass

    process = SimpleNamespace(
        stdin=SimpleNamespace(write=writes.append, drain=drain),
        stdout=SimpleNamespace(readline=readline),
    )

    async def spawn(*args):
        invocations.append(args[0])
        return process

    async def cleanup(child):
        retired.append(child)

    monkeypatch.setattr(launcher, "spawn", spawn)
    monkeypatch.setattr(launcher, "finish_child", cleanup)
    if fault == "control":
        assert await launcher.inspect("owned-client", tmp_path, home, {}, trusted=True) == hook
        assert invocations[0][invocations[0].index("--disable") + 1] == "apps"
    else:
        with pytest.raises(ValueError):
            await launcher.inspect("owned-client", tmp_path, home, {}, trusted=True)
    assert retired == [process]
    assert writes


@pytest.mark.parametrize("cancels", [0, 1, 2])
async def test_real_owned_creation_handoff_and_retirement(launcher, monkeypatch, tmp_path, cancels):
    created, release = asyncio.Event(), asyncio.Event()
    children = []
    original = asyncio.create_subprocess_exec

    async def delayed_creation(*args, **kwargs):
        process = await original(*args, **kwargs)
        children.append(process)
        created.set()
        await release.wait()
        return process

    monkeypatch.setattr(launcher.asyncio, "create_subprocess_exec", delayed_creation)
    task = asyncio.create_task(
        launcher.spawn(
            [sys.executable, "-c", "import time; time.sleep(300)"],
            tmp_path,
            {"PATH": os.defpath},
        )
    )
    try:
        await created.wait()
        for _ in range(cancels):
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancels:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is children[0]
            await launcher.finish_child(children[0])
        assert len(children) == 1 and children[0].returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(children[0].pid, 0)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for process in children:
            if process.returncode is None:
                await launcher.finish_child(process)


@pytest.fixture
def branch_launch(launcher, qualified_tree, monkeypatch, tmp_path):
    """A real owned linked worktree; Codex inspection/exec are captured, never run."""
    runtime, base, _ = qualified_tree
    branch = tmp_path / "branch"
    subprocess.run(["git", "-C", str(runtime), "worktree", "add", "--detach", str(branch), base],
                   check=True, capture_output=True, env=fixture_git_environment())
    entry_sources = ("scripts/codex-validator", "scripts/codex_validator_launcher.py")
    for rel in entry_sources:
        (branch / rel).write_bytes(b"branch candidate\n")
    subprocess.run(["git", "-C", str(branch), "add", *entry_sources], check=True, env=fixture_git_environment())
    subprocess.run(["git", "-C", str(branch), "-c", "user.name=Owned fixture",
                    "-c", "user.email=fixture@example.com", "commit", "-qm", "Branch entry"], check=True, env=fixture_git_environment())
    head = launcher.git(branch, "rev-parse", "HEAD").decode().strip()
    home, inspections, executions = tmp_path / "profile", [], []
    monkeypatch.setattr(launcher, "RUNTIME", branch)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "/usr/bin/true")
    monkeypatch.setattr(launcher, "check_binary", lambda _: None)
    monkeypatch.setattr(launcher, "check_no_deploy", lambda: None, raising=False)
    monkeypatch.setattr(launcher.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(launcher.sys.stdout, "isatty", lambda: True)

    async def inspect(binary, selected, private, env, *, trusted):
        assert selected == runtime and private == home
        assert env["GENESIS_REPO_ROOT"] == str(runtime)
        assert "GENESIS_HOOK_DEV_LOCAL" not in env
        policy = tomllib.loads(launcher.profile_bytes(home, runtime, trusted=trusted).decode())
        assert policy["projects"] == {str(runtime): {"trust_level": "untrusted"}}
        assert all(server["cwd"] == str(runtime) for server in policy["mcp_servers"].values())
        assert str(branch) not in policy["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        inspections.append(trusted)
        return {"key": str(home / "config.toml") + ":pre_tool_use:0:0", "currentHash": "sha256:" + "a" * 64}

    class CapturedExec(BaseException):
        pass

    def execve(binary, argv, env):
        executions.append((binary, argv, env))
        raise CapturedExec

    monkeypatch.setattr(launcher, "inspect", inspect)
    monkeypatch.setattr(launcher.os, "execve", execve)

    def invoke(operation, *, flag=True, qualified=True, device_auth=False):
        argv = ["owned-launcher", operation, "--home", str(home), "--model", "owned-model"]
        if device_auth:
            argv.append("--device-auth")
        if flag:
            argv.append("--owner-branch-test")
        if qualified:
            argv.extend(["--qualified-commit", qualified if isinstance(qualified, str) else head])
        monkeypatch.setattr(launcher.sys, "argv", argv)
        return launcher.main()

    def prepared():
        home.mkdir(mode=0o700)
        raw = launcher.policy(runtime, "owned-model")
        raw += launcher.trust_suffix(str(home / "config.toml") + ":pre_tool_use:0:0", "sha256:" + "a" * 64)
        launcher.write_new(home / "config.toml", raw.encode())

    return launcher, runtime, branch, head, home, inspections, executions, invoke, prepared, CapturedExec


@pytest.mark.parametrize("owner_branch", [False, True])
def test_device_auth_uses_validated_dedicated_login(branch_launch, monkeypatch, owner_branch):
    module, runtime, _, _, home, inspections, executions, invoke, prepared, captured = branch_launch
    if not owner_branch:
        monkeypatch.setattr(module, "RUNTIME", runtime)
    head = module.git(module.RUNTIME, "rev-parse", "HEAD").decode().strip()
    prepared()
    with pytest.raises(captured):
        invoke("login", flag=owner_branch, qualified=head, device_auth=True)
    binary, argv, env = executions[0]
    assert argv == [binary, "--no-daemon", "login", "--device-auth"]
    assert env["CODEX_HOME"] == str(home)
    assert not inspections and not (home / "auth.json").exists()


@pytest.mark.parametrize("operation", ["prepare", "check", "run"])
def test_device_auth_refuses_other_operations_before_state_changes(branch_launch, operation, capsys):
    _, _, _, _, home, inspections, executions, invoke, _, _ = branch_launch
    with pytest.raises(SystemExit) as stopped:
        invoke(operation, device_auth=True)
    assert stopped.value.code == 2
    assert "--device-auth is only valid with login" in capsys.readouterr().err
    assert not home.exists() and not inspections and not executions


@pytest.mark.parametrize("operation", ["prepare", "login", "check", "run"])
def test_owner_branch_mode_uses_only_main_execution_roots(branch_launch, monkeypatch, operation):
    module, runtime, branch, head, home, inspections, executions, invoke, prepared, captured = branch_launch
    monkeypatch.setenv("GENESIS_HOOK_DEV_LOCAL", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/owned-invalid-root")
    if operation != "prepare":
        prepared()
    if operation in {"login", "run"}:
        with pytest.raises(captured):
            invoke(operation)
        _, argv, env = executions[0]
        assert not any(k.startswith("GIT_") for k in env)
        if operation == "run":
            assert argv[argv.index("-C") + 1] == str(runtime)
            assert "--strict-config" in argv
            assert argv[argv.index("--disable") + 1] == "apps"
        else:
            assert argv == [executions[0][0], "--no-daemon", "login"]
    else:
        assert invoke(operation) == 0
    assert inspections == {"prepare": [False, True], "check": [True], "run": [True], "login": []}[operation]
    assert not (home / "auth.json").exists()
    # The branch's two new entry files differ from main, while all guards agree.
    assert (branch / "scripts/codex-validator").read_bytes() != (runtime / "scripts/codex-validator").read_bytes()


@pytest.mark.parametrize("operation", ["prepare", "login", "check", "run"])
@pytest.mark.parametrize("fault", ["no_flag", "no_commit", "flag_in_main", "deploy", "dirty_main",
                                    "dirty_branch", "main_guard", "branch_guard", "main_extra_hook",
                                    "branch_extra_hook", "missing_main_dependency", "main_source_mode"])
def test_owner_branch_refusals_precede_profile_writes_or_inspection(branch_launch, monkeypatch, operation, fault):
    module, runtime, branch, head, home, inspections, executions, invoke, _, _ = branch_launch
    if fault == "flag_in_main":
        monkeypatch.setattr(module, "RUNTIME", runtime)
    elif fault == "deploy":
        def active():
            raise ValueError("owned active deploy")
        monkeypatch.setattr(module, "check_no_deploy", active)
    elif fault.startswith("dirty_"):
        root = runtime if fault == "dirty_main" else branch
        (root / "scripts/codex-validator").write_bytes(b"uncommitted\n")
    elif fault in {"main_guard", "branch_guard"}:
        root = runtime if fault == "main_guard" else branch
        subprocess.run(["git", "-C", str(root), "update-index", "--assume-unchanged", "scripts/hooks/probe.py"], check=True, env=fixture_git_environment())
        (root / "scripts/hooks/probe.py").write_bytes(b"hidden guard change\n")
    elif fault.endswith("extra_hook"):
        root = runtime if fault == "main_extra_hook" else branch
        (root / "scripts/hooks/unqualified.py").write_bytes(b"extra\n")
    elif fault == "missing_main_dependency":
        (runtime / "src/genesis/mcp/external_profiles.py").unlink()
    elif fault == "main_source_mode":
        (runtime / ".claude/hooks/genesis-hook").chmod(0o644)
    assert invoke(operation, flag=fault != "no_flag", qualified=fault != "no_commit") == 2
    assert not home.exists() and not inspections and not executions


def test_branch_rechecks_sources_after_inspection_before_trust_append(branch_launch, monkeypatch):
    module, runtime, branch, head, home, inspections, executions, invoke, _, _ = branch_launch
    original = module.inspect

    async def changed(*args, **kwargs):
        result = await original(*args, **kwargs)
        (runtime / "scripts/hooks/probe.py").write_bytes(b"changed during inspection\n")
        return result

    monkeypatch.setattr(module, "inspect", changed)
    assert invoke("prepare") == 2
    assert inspections == [False] and not executions
    assert "state" not in tomllib.loads((home / "config.toml").read_text())["hooks"]


def test_branch_runtime_directories_remain_permitted(branch_launch):
    _, runtime, branch, _, _, _, _, invoke, prepared, _ = branch_launch
    for root in (runtime, branch):
        (root / "data").mkdir()
        (root / "data/owned-runtime").write_bytes(b"untracked runtime\n")
    prepared()
    assert invoke("check") == 0


@pytest.mark.parametrize("operation", ["prepare", "login", "check", "run"])
def test_normal_main_admission_remains_available(branch_launch, monkeypatch, operation):
    module, runtime, _, _, _, _, executions, invoke, prepared, captured = branch_launch
    monkeypatch.setattr(module, "RUNTIME", runtime)
    head = module.git(runtime, "rev-parse", "HEAD").decode().strip()
    if operation != "prepare":
        prepared()
    if operation in {"login", "run"}:
        with pytest.raises(captured):
            invoke(operation, flag=False, qualified=head)
        assert len(executions) == 1
        binary, argv, _ = executions[0]
        if operation == "login":
            assert argv == [binary, "--no-daemon", "login"]
        else:
            assert "--strict-config" in argv
            assert argv[argv.index("--disable") + 1] == "apps"
    else:
        assert invoke(operation, flag=False, qualified=head) == 0


@pytest.mark.parametrize("operation", ["prepare", "login", "check", "run"])
def test_branch_inherited_session_refuses_before_state_change(branch_launch, monkeypatch, operation):
    _, _, _, _, home, inspections, executions, invoke, _, _ = branch_launch
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert invoke(operation) == 2
    assert not home.exists() and not inspections and not executions


@pytest.mark.parametrize("signum", ["SIGTERM", "SIGHUP"])
@pytest.mark.parametrize("repeated", [False, True])
@pytest.mark.parametrize("phase", ["inspection", "retirement"])
async def test_signal_callbacks_preserve_retirement_and_restore_handlers(
    launcher, monkeypatch, signum, repeated, phase
):
    """Exercise the real callback/cancellation seam without signaling pytest."""
    entered, retiring, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    retired, installed = [], {}
    previous = {sig: launcher.signal.getsignal(sig)
                for sig in (launcher.signal.SIGTERM, launcher.signal.SIGHUP)}
    monkeypatch.setattr(launcher.signal, "signal", lambda sig, handler: installed.__setitem__(sig, handler))

    async def cleanup(child):
        retiring.set()
        await release.wait()
        retired.append(child)

    async def inspection(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await launcher.finish_child("owned-child")

    monkeypatch.setattr(launcher, "stop_child", cleanup)
    monkeypatch.setattr(launcher, "_inspect", inspection)
    task = asyncio.create_task(launcher.inspect("unused", Path("/owned"), Path("/owned/private"), {}, trusted=False))
    await entered.wait()
    if phase == "retirement":
        task.cancel()
        await retiring.wait()
    callback = installed[getattr(launcher.signal, signum)]
    callback(None, None)
    await retiring.wait()
    if repeated:
        callback(None, None)
        await asyncio.sleep(0)
    assert not task.done() and not retired
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert retired == ["owned-child"] and installed == previous


@pytest.mark.parametrize(("reported", "accepted", "warning"), [
    ("codex-cli 0.163.0", True, True),
    ("codex-cli 0.164.0", True, True),
    ("codex-cli 0.165.0", True, True),
    ("codex-cli 0.166.0", True, False),
    ("codex-cli 0.166.0\n", True, False),
    ("codex-cli 1.0.0", True, True),
    ("codex-cli 0.166.0-alpha.1", True, True),
    ("codex-cli 0.166.0+build.1", True, True),
    ("codex-cli 0.166.0-alpha.1+build.1", True, True),
    ("codex-cli 0.165", False, False),
    ("codex-cli 0.165.0\nextra output", False, False),
    ("other-cli 0.165.0", False, False),
    ("unrecognized version", False, False),
    ("", False, False),
])
def test_binary_preflight_warns_on_unqualified_versions(launcher, monkeypatch, capsys, reported, accepted, warning):
    from types import SimpleNamespace

    def version(argv, **kwargs):
        assert argv == ["/owned/codex", "--version"]
        assert kwargs["check"] is True and kwargs["capture_output"] is True
        return SimpleNamespace(stdout=reported)

    monkeypatch.setattr(launcher.subprocess, "run", version)
    if accepted:
        launcher.check_binary("/owned/codex")
    else:
        with pytest.raises(ValueError, match="Unrecognized Codex version"):
            launcher.check_binary("/owned/codex")
    captured = capsys.readouterr()
    assert not captured.out
    if warning:
        assert reported.strip() in captured.err
        assert "codex-cli 0.166.0" in captured.err
        assert "unqualified" in captured.err
    else:
        assert not captured.err


def test_binary_preflight_refuses_failed_version_command(launcher, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(launcher.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.check_binary("/owned/codex")


@pytest.mark.parametrize("operation", ["init-objects", "add-index"])
def test_git_fixture_setup_ignores_inherited_locations(tmp_path, monkeypatch, operation):
    owned = tmp_path / "owned"
    foreign = tmp_path / "foreign"
    owned.mkdir()
    foreign.mkdir()
    if operation == "init-objects":
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", str(foreign))
        subprocess.run(["git", "init", "--bare", str(owned)], check=True,
                       capture_output=True, env=fixture_git_environment())
        assert (owned / "HEAD").is_file()
        assert not list(foreign.iterdir())
    else:
        for directory in (owned, foreign):
            subprocess.run(["git", "init", "-q", str(directory)], check=True,
                           env=fixture_git_environment())
            (directory / "note").write_text("owned fixture\n")
        monkeypatch.setenv("GIT_DIR", str(foreign / ".git"))
        subprocess.run(["git", "-C", str(owned), "add", "note"], check=True,
                       env=fixture_git_environment())
        assert (owned / ".git/index").is_file()
        assert not (foreign / ".git/index").exists()


@pytest.mark.parametrize("git_key", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_environment_scrubs_git_before_any_branch_root_lookup(
    launcher, qualified_tree, monkeypatch, tmp_path, git_key
):
    runtime, head, _ = qualified_tree
    branch = tmp_path / "environment-branch"
    subprocess.run(
        ["git", "-C", str(runtime), "worktree", "add", "--detach", str(branch), head],
        check=True, capture_output=True, env=fixture_git_environment(),
    )
    # Load both real source files from the linked tree, even in a CI main checkout.
    for relative in ("scripts/codex_validator_launcher.py", "scripts/codex_external_mcp.py"):
        (branch / relative).write_bytes((ROOT / relative).read_bytes())
    monkeypatch.syspath_prepend(str(branch / "scripts"))
    monkeypatch.delitem(sys.modules, "codex_external_mcp", raising=False)
    module = private_module("owned_branch_environment", branch / "scripts/codex_validator_launcher.py")
    helper = sys.modules["codex_external_mcp"]
    assert Path(helper.__file__) == branch / "scripts/codex_external_mcp.py"
    assert (branch / ".git").is_file()
    for key in helper._SESSION_CONTEXT:
        monkeypatch.setenv(key, "owned-inherited-context")
    monkeypatch.setenv(git_key, str(tmp_path / "absent-git-directory"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "absent-index"))
    monkeypatch.setenv("GENESIS_HOOK_DEV_LOCAL", "1")
    home = tmp_path / "uncreated-private-home"
    env = module.environment(runtime, home)
    assert not any(key.startswith("GIT_") for key in env)
    assert not set(helper._SESSION_CONTEXT).intersection(env)
    assert "GENESIS_HOOK_DEV_LOCAL" not in env
    assert env["GENESIS_REPO_ROOT"] == str(runtime)
    assert env["CODEX_HOME"] == str(home)
    assert env["PATH"] == str(runtime / ".venv/bin") + ":/usr/bin:/bin"
    assert not home.exists()
