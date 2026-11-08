"""Pinned native preflight with owned fake MCP servers, never live Genesis."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_scripts.codex_validator_git import fixture_git_environment

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="opt-in pinned native preflight qualification",
)


@pytest.mark.parametrize("owner_branch", [False, True])
async def test_actual_generated_policy_native_discovery_and_trust(tmp_path, monkeypatch, owner_branch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    launcher = private_module(
        "native_validator_launcher", ROOT / "scripts/codex_validator_launcher.py"
    )
    binary = os.environ["GENESIS_CODEX_CLI_BIN"]
    assert subprocess.check_output([binary, "--version"], text=True).strip() == launcher.VERSION
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for directory in launcher.HOOK_TREES:
        shutil.copytree(
            ROOT / directory, runtime / directory, ignore=shutil.ignore_patterns("__pycache__")
        )
    # A lone interpreter symlink loses pyvenv.cfg and cannot import FastMCP.
    (runtime / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    # Only this disposable server entry is substituted. It imports no Genesis
    # modules and exposes harmless named functions; no configured live MCP runs.
    tools = {
        name: sorted(launcher.profile_tools(name, "validator")) for name in ("health", "memory")
    }
    wrapper = runtime / "scripts/codex_external_mcp.py"
    wrapper.write_text(
        "import sys\nfrom fastmcp import FastMCP\n"
        f'tools={tools!r}\nmcp=FastMCP("owned-native-preflight")\n'
        'async def read() -> str:\n return "owned fixture"\n'
        "for name in tools[sys.argv[-1]]:mcp.tool(name=name)(read)\n"
        "mcp.run()\n"
    )
    qualified = None
    branch = None
    if owner_branch:
        for rel in launcher.EXTRA_SOURCES:
            target = runtime / rel
            if target != wrapper:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / rel, target)
        subprocess.run(["git", "init", "-q", str(runtime)], check=True, env=fixture_git_environment())
        subprocess.run(["git", "-C", str(runtime), "add", *launcher.EXTRA_SOURCES, *launcher.HOOK_TREES], check=True, env=fixture_git_environment())
        subprocess.run(["git", "-C", str(runtime), "-c", "user.name=Owned fixture",
                        "-c", "user.email=fixture@example.com", "commit", "-qm", "Owned main"], check=True, env=fixture_git_environment())
        branch = tmp_path / "launcher-branch"
        subprocess.run(["git", "-C", str(runtime), "worktree", "add", "--detach", str(branch)], check=True, capture_output=True, env=fixture_git_environment())
        with (branch / "scripts/codex-validator").open("a") as out:
            out.write("\n# Owned branch entry qualifier\n")
        subprocess.run(["git", "-C", str(branch), "add", "scripts/codex-validator"], check=True, env=fixture_git_environment())
        subprocess.run(["git", "-C", str(branch), "-c", "user.name=Owned fixture",
                        "-c", "user.email=fixture@example.com", "commit", "-qm", "Owned branch"], check=True, env=fixture_git_environment())
        qualified = launcher.git(branch, "rev-parse", "HEAD").decode().strip()
        monkeypatch.setattr(launcher, "RUNTIME", branch)
        assert launcher.execution_root(True) == runtime
        launcher.check_qualification(runtime, qualified)
    project = runtime / ".codex"
    project.mkdir()
    (project / "config.toml").write_text(
        'model="PROJECT_SENTINEL"\n[mcp_servers.project_sentinel]\ncommand="false"\n'
    )
    home = tmp_path / "private"
    home.mkdir(mode=0o700)
    user_home = tmp_path / "user"
    user_home.mkdir(mode=0o700)
    raw = launcher.policy(runtime, "owner-selected-unused-model").encode()
    launcher.write_new(home / "config.toml", raw)
    env = {
        "HOME": str(user_home),
        "CODEX_HOME": str(home),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "GENESIS_REPO_ROOT": str(runtime),
    }
    native_events = []
    decode = launcher.load_json

    def record(raw):
        value = decode(raw)
        native_events.append(value)
        (tmp_path / "native-responses.json").write_text(json.dumps(native_events, indent=2))
        return value

    monkeypatch.setattr(launcher, "load_json", record)
    before = await launcher.inspect(binary, runtime, home, env, trusted=False)
    assert before["trustStatus"] == "untrusted"
    suffix = launcher.trust_suffix(before["key"], before["currentHash"]).encode()
    with (home / "config.toml").open("ab") as out:
        out.write(suffix)
    after = await launcher.inspect(binary, runtime, home, env, trusted=True)
    assert after["trustStatus"] == "trusted"
    assert after["currentHash"] == before["currentHash"]
    assert (home / "config.toml").read_bytes() == raw + suffix
    assert not (home / "auth.json").exists()
    if owner_branch:
        launcher.check_qualification(runtime, qualified)
    source_inputs = [p for directory in launcher.HOOK_TREES
                     for p in (ROOT / directory).rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    source_inputs += [ROOT / rel for rel in launcher.EXTRA_SOURCES]
    (tmp_path / "launcher-native-evidence.json").write_text(json.dumps({
        "owner_branch": owner_branch, "runtime": str(runtime), "branch": str(branch) if branch else None,
        "qualification_commit": qualified, "fake_server_entry": str(wrapper),
        "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_inputs},
        "copied_hashes": {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for directory in launcher.HOOK_TREES for p in (runtime / directory).rglob("*")
                          if p.is_file() and "__pycache__" not in p.parts},
        "native_events": native_events, "auth_absent": not (home / "auth.json").exists(),
    }, indent=2))
    assert (
        hashlib.sha256((runtime / "scripts/hooks/codex_validator_adapter.py").read_bytes()).digest()
        == hashlib.sha256((ROOT / "scripts/hooks/codex_validator_adapter.py").read_bytes()).digest()
    )
