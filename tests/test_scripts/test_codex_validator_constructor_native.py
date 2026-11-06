"""Pinned composed-adapter controls with actual Git and only owned destinations."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)
from tests.test_hooks.test_codex_native import native_binaries as native_binaries
from tests.test_hooks.test_codex_validator_adapter_native import source_copy
from tests.test_scripts.codex_validator_git import fixture_git_environment

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="opt-in pinned constructor destination qualification",
)


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("form", ["init-template", "clone-depth", "clone-template"])
@pytest.mark.parametrize("mode", ["allow", "deny", "noop"])
def test_native_adapter_checks_actual_git_destinations(tmp_path, monkeypatch, native_binaries, client, form, mode):
    binaries, versions = native_binaries
    assert set(versions.values()) == {"codex-cli 0.166.0"}
    binary, app = binaries[client], binaries["app"]
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    definition = private_module(
        "constructor_native_definition", ROOT / "scripts/hooks/codex_validator_adapter.py"
    )
    runtime = source_copy(tmp_path)
    hashes = {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(runtime.rglob("*")) if p.is_file()}
    scratch = tmp_path / "tmp/validator"
    scratch.mkdir(parents=True)
    template = scratch / "empty-template"
    template.mkdir()
    git = shutil.which("git")
    assert git
    source = scratch / "source.git"
    subprocess.run([git, "init", "--bare", "--template", str(template), str(source)],
                   cwd=scratch, check=True, capture_output=True, env=fixture_git_environment())
    args = {
        "init-template": ["init", "--template", str(template)],
        "clone-depth": ["clone", "--depth", "1", str(source)],
        "clone-template": ["clone", "--template", str(template), str(source)],
    }[form]
    if mode == "allow":
        args.append(str(scratch / "external-repository"))
        target = scratch / "external-repository/.git/HEAD"
    else:
        target = runtime / (".git/HEAD" if form == "init-template" else "source/.git/HEAD")
    command = "cd " + shlex.quote(str(scratch)) + " || exit\n"
    command += shlex.join([git, "-C", str(runtime), *args])
    item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec",
            "input": "text(await tools.exec_command(" + json.dumps({"cmd": command}) + "));"}
    evidence = {"client": client, "form": form, "mode": mode, "runtime": str(runtime),
                "hashes": hashes, "command": command, "target": str(target),
                "versions": versions, "binary": binary,
                "binary_hashes": {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                  for name, path in binaries.items()},
                "binary_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(tmp_path, url, "allow")
            env.update(HOME=str(tmp_path), GENESIS_HOME=str(tmp_path / ".genesis"),
                       GENESIS_CC_SESSION="1", _TEST_HOOK_ASK_POLICY="")
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().replace("timeout = 5", "timeout = 15"))
            hook = "true" if mode == "noop" else definition.hook_command(runtime)
            (tmp_path / "hook.py").write_text(
                "import pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n"
                f"with pathlib.Path({str(tmp_path / 'hooks.jsonl')!r}).open('a') as out: out.write(raw.decode()+'\\n')\n"
                f"result=subprocess.run(['bash','-c',{hook!r}],input=raw)\n"
                "raise SystemExit(result.returncode)\n"
            )
            evidence["trust"] = trust_fixture_hook(app, tmp_path, env)
            evidence["events"], evidence["stderr"] = run_native(
                binary, client, tmp_path, env, evidence
            )
            evidence["requests"] = requests
    finally:
        (tmp_path / "native-constructor-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(hooks) == 1 and hooks[0]["tool_name"] == "Bash"
    assert hooks[0]["tool_input"]["command"] == command
    assert not (tmp_path / "codex/auth.json").exists()
    if mode == "deny":
        assert "cannot create a repository in the main checkout" in json.dumps(requests[-1])
    assert target.exists() is (mode != "deny")
