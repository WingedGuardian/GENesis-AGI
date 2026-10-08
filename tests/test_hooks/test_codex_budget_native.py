"""Opt-in real Codex context controls with synthetic budgets and scratch sinks."""

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)
from tests.test_hooks.test_codex_native import native_binaries as budget_binaries  # noqa: F401

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="native integration is opt-in",
)
ROOT = Path(__file__).resolve().parents[2]


def budget_fixture(root, env, blocked, legacy):
    """Load the actual adapter, stub only budget evidence, record real branches."""
    real_git = shutil.which("git")
    assert real_git
    for directory, branch in ((root / "workspace", "session"), (root / "alternate", "execution")):
        subprocess.run([real_git, "init", "-q", "-b", branch, str(directory)], check=True)
    shim_dir = root / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\nimport os, pathlib, sys\n"
        f"real_git = {real_git!r}\n"
        "args = sys.argv[1:]\n"
        "if 'commit' not in args: os.execv(real_git, [real_git, *args])\n"
        "target = pathlib.Path.cwd()\n"
        "if args[0] == '-C': target = target / args[1]\n"
        "elif args[0].startswith('-C'): target = target / args[0][2:]\n"
        "(target / 'commit-receipt.txt').write_text(str(target.resolve()))\n"
    )
    shim.chmod(0o700)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    (root / "hook.py").write_text(
        "import contextlib, importlib.util, io, json, pathlib, sys\n"
        f"root = pathlib.Path({str(root)!r})\n"
        f"spec = importlib.util.spec_from_file_location('adapter', {str(ROOT / 'scripts/hooks/codex_review_stop.py')!r})\n"
        "adapter = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(adapter)\n"
        "data = json.load(sys.stdin)\n"
        "with (root / 'hooks.jsonl').open('a') as out: out.write(json.dumps(data)+'\\n')\n"
        "def budget(cwd, branch, **kwargs):\n"
        "    with (root / 'budget.jsonl').open('a') as out: out.write(json.dumps({'cwd':cwd,'branch':branch})+'\\n')\n"
        f"    return {{'status':'ok','commit_approval_required': branch == 'execution' and {blocked!r},'count':4}}\n"
        "adapter.commits._branch_review_budget = budget\n"
        # Source-removal control of this change's exact implicit-cwd assumption.
        + ("adapter._commit_cwd = lambda seg: data['cwd']\n" if legacy else "")
        + "adapter.sys.stdin = io.StringIO(json.dumps(data))\n"
        "with contextlib.redirect_stdout(io.StringIO()): code = adapter.main()\n"
        "sys.exit(code)\n"
    )


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("mode", ["direct", "code"])
@pytest.mark.parametrize("form,blocked,legacy,allowed,branch", [
    ("implicit", True, False, False, None),
    ("relative", True, False, False, None),
    ("session", True, False, True, "session"),
    ("absolute", True, False, False, "execution"),
    ("absolute", False, False, True, "execution"),
    ("glued", True, False, False, None),
    ("implicit", True, True, True, "session"),
])
def test_native_budget_uses_explicit_target(
    tmp_path, request, client, mode, form, blocked, legacy, allowed, branch,
):
    binaries, versions = request.getfixturevalue("budget_binaries")
    target = tmp_path / "alternate"
    options = {
        "implicit": "", "relative": "-C .", "session": "-C " + shlex.quote(str(tmp_path / "workspace")),
        "absolute": "-C " + shlex.quote(str(target)), "glued": "-C" + shlex.quote(str(target)),
    }
    command = f"git {options[form]} commit -m fixture"
    args = {"cmd": command, "workdir": str(target if form != "session" else tmp_path / "workspace")}
    item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": "exec_command", "arguments": json.dumps(args)}
    if mode == "code":
        item = {
            "type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec",
            "input": "text(await tools.exec_command(" + json.dumps(args) + "));",
        }
    evidence = {"versions": versions, "client": client, "mode": mode, "form": form, "legacy": legacy, "args": args}
    try:
        with fixture_provider(item) as (url, requests):
            evidence["requests"] = requests
            env = configure(tmp_path, url, "allow")
            budget_fixture(tmp_path, env, blocked, legacy)
            evidence["trust"] = trust_fixture_hook(binaries["app"], tmp_path, env)
            events, stderr = run_native(binaries[client], client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr)
    finally:
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(hooks) == 1 and hooks[0]["cwd"] == str(tmp_path / "workspace")
    assert hooks[0]["tool_input"] == {"command": command}
    records = tmp_path / "budget.jsonl"
    lookups = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    assert [row["branch"] for row in lookups] == ([branch] if branch else [])
    receipt = (tmp_path / "workspace" if form == "session" else target) / "commit-receipt.txt"
    assert receipt.exists() == allowed
    if allowed:
        assert receipt.read_text() == str(receipt.parent)
    assert not list((tmp_path / "codex" / ".tmp").glob("plugins-clone-*"))
