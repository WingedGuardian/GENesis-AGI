"""All named patch targets are admitted before native chunk processing."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_hooks.test_codex_validator_guard import HOOKS, ROOT, invoke, payload
from tests.test_hooks.test_codex_validator_guard import guard as guard


@pytest.fixture
def policy(guard):
    from codex_validator_patch import check_patch, patch_targets

    return check_patch, patch_targets


def patch(name, operation="update", destination=None, endings="\n"):
    headers = {"add": "Add File", "delete": "Delete File", "update": "Update File"}
    body = f"*** {headers[operation]}: {name}\n"
    if destination:
        body += f"*** Move to: {destination}\n"
    if operation == "add":
        body += "+beta\n"
    elif operation == "update":
        body += "@@\n-alpha\n+beta\n"
    return ("*** Begin Patch\n" + body + "*** End Patch\n").replace("\n", endings)


@pytest.mark.parametrize("operation", ["add", "delete", "update", "move"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("endings", ["\n", "\r\n"])
def test_ordinary_sources_and_destinations(policy, tmp_path, operation, absolute, endings):
    source = tmp_path / "résumé with spaces.txt"
    if operation != "add":
        source.write_text("alpha\n")
    name = str(source) if absolute else source.name
    destination = tmp_path / "new-parent/result.txt"
    target = str(destination) if absolute else "new-parent/result.txt"
    text = patch(name, "update" if operation == "move" else operation,
                 target if operation == "move" else None, endings)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    policy[0](text, tmp_path)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    assert not destination.exists(), "policy created a native target"


@pytest.mark.parametrize("name", ["../outside", "/outside", ".", "folder/../outside",
    "AGENTS.md", "nested/AGENTS.md", ".codex/config.toml", "nested/.agents/skill.md",
    ".git/config", "file:///outside", "literal\\name", "prefix:name", " leading",
    "trailing ", "nul\x00name", "name\tpart", "name\rpart"])
def test_unsupported_or_protected_targets_are_refused(policy, tmp_path, name):
    with pytest.raises(ValueError):
        policy[0](patch(name, "add"), tmp_path)


@pytest.mark.parametrize("target_kind", ["leaf-link", "ancestor-link", "dangling-link",
    "hardlink", "directory", "fifo", "file-ancestor"])
@pytest.mark.parametrize("target_role", ["source", "destination"])
def test_every_existing_component_is_checked(policy, tmp_path, target_kind, target_role):
    ws = tmp_path / "workspace"
    ws.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("alpha\n")
    target = ws / "target"
    if target_kind == "ancestor-link":
        target.symlink_to(tmp_path, target_is_directory=True)
        target = target / "outside"
    elif target_kind in {"leaf-link", "dangling-link"}:
        target.symlink_to(outside if target_kind == "leaf-link" else tmp_path / "missing")
    elif target_kind == "hardlink":
        os.link(outside, target)
    elif target_kind == "directory":
        target.mkdir()
    elif target_kind == "fifo":
        os.mkfifo(target)
    else:
        target.write_text("alpha\n")
        target = target / "child"
    text = patch(str(target), "update") if target_role == "source" else patch(
        "ordinary.txt", "update", str(target))
    with pytest.raises(ValueError):
        policy[0](text, ws)
    assert outside.read_text() == "alpha\n"


@pytest.mark.parametrize("text", ["", "*** Begin Patch\n*** End Patch", "<<'EOF'\n"
    + patch("safe", "add") + "EOF", patch("safe", "add") + patch("other", "add"),
    patch("safe", "add").replace("*** Add File", "*** Environment ID"),
    patch("safe", "add").replace("+beta", "*** Unknown: hidden"),
    "*** Begin Patch\n+literal\n*** End Patch"])
def test_unknown_envelopes_and_directives_never_allow(policy, tmp_path, text):
    with pytest.raises(ValueError):
        policy[0](text, tmp_path)


def test_collector_checks_all_headers_and_does_not_split_unicode_filename_bytes(policy, tmp_path):
    text = patch("safe", "update").replace("@@", "@@\n *** Update File: /outside")
    with pytest.raises(ValueError):
        policy[0](text, tmp_path)
    for indent in ("  ", "\u00a0"):
        text = patch("ordinary", "add").replace("*** Add File", indent + "*** Add File")
        assert policy[1](text) == ["ordinary"]
    assert policy[1](patch("literal\u0085filename", "add")) == ["literal\u0085filename"]
    policy[0](patch("literal\u0085filename", "add"), tmp_path)
    text = patch("ordinary", "add").replace("+beta", "+*** Update File: /outside")
    assert policy[1](text) == ["ordinary"]


def test_bounded_targets_and_untrusted_cwd(policy, tmp_path):
    text = "*** Begin Patch\n" + "*** Delete File: safe\n" * 4097 + "*** End Patch"
    with pytest.raises(ValueError, match="too many"):
        policy[0](text, tmp_path)
    data = payload("apply_patch", {"command": patch("artifact.txt", "add")})
    data["cwd"] = "/outside"
    result = invoke(tmp_path, json.dumps(data).encode())
    assert result.returncode == 0 and result.stdout == b""
    assert not (tmp_path / "artifact.txt").exists()
    data["tool_input"]["command"] = patch(str(tmp_path.parent / "outside"), "add")
    result = invoke(tmp_path, json.dumps(data).encode())
    assert result.returncode == 2 and result.stdout == b""
    assert b"outside" not in result.stderr


@pytest.mark.skipif(os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
                    reason="native Codex integration is explicitly opt-in")
@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("endings", ["\n", "\r\n"])
@pytest.mark.parametrize("operation,kind", [
    (operation, kind)
    for operation in ("add", "delete", "update", "move")
    for kind in ("relative", "absolute", "outside", "leaf-link", "ancestor-link",
                 "hardlink", "protected")
] + [("move", kind) for kind in ("move-outside", "move-link", "move-hardlink", "move-protected")])
def test_native_patch_ownership_controls_actual_sources_and_destinations(
    tmp_path, client, endings, operation, kind,
):
    from tests.test_hooks.native_codex import (
        configure,
        fixture_provider,
        run_native,
        trust_fixture_hook,
    )

    binary = shutil.which("codex")
    assert binary, "native opt-in requires Codex"
    ws = tmp_path / "workspace"
    source = ws / ("AGENTS.md" if kind == "protected" else "source.txt")
    if kind == "outside":
        source = tmp_path / "outside.txt"
    elif kind == "ancestor-link":
        source = ws / "alias" / "outside.txt"
    name = "source.txt" if kind == "relative" else str(source)
    destination = ws / "destination.txt"
    if kind == "move-outside":
        destination = tmp_path / "outside.txt"
    elif kind == "move-protected":
        destination = ws / "nested/AGENTS.md"
    text = patch(name, "update" if operation == "move" else operation,
                 str(destination) if operation == "move" else None, endings)
    action = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
              "namespace": "functions", "name": "exec",
              "input": "text(await tools.apply_patch(" + json.dumps(text) + "));"}
    evidence = {"client": client, "endings": endings, "operation": operation, "kind": kind}
    try:
        with fixture_provider(action) as (url, requests):
            env = configure(tmp_path, url, "allow")
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().replace("timeout = 5", "timeout = 15"))
            outside = tmp_path / "outside.txt"
            outside.write_text("alpha\n")
            if kind == "leaf-link":
                source.symlink_to(outside)
            elif kind == "ancestor-link":
                (ws / "alias").symlink_to(tmp_path, target_is_directory=True)
            elif kind == "hardlink":
                os.link(outside, source)
            elif operation != "add":
                source.write_text("alpha\n")
            if kind == "move-link":
                destination.symlink_to(outside)
            elif kind == "move-hardlink":
                os.link(outside, destination)
            before = source.read_bytes() if source.exists() else None
            destination_before = destination.read_bytes() if destination.exists() else None
            (tmp_path / "hook.py").write_text(
                "import json,pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n"
                f"with pathlib.Path({str(tmp_path / 'hooks.jsonl')!r}).open('a') as out:"
                "out.write(json.dumps(json.loads(raw))+'\\n')\n"
                f"command={['/bin/bash', str(HOOKS / 'codex-validator-guard'), '--runtime-root', str(ROOT), '--workspace-root', str(ws)]!r}\n"
                "result=subprocess.run(command,input=raw,capture_output=True,timeout=12)\n"
                f"pathlib.Path({str(tmp_path / 'guard-result.json')!r}).write_text(json.dumps("
                "{'returncode':result.returncode,'stdout':result.stdout.decode(),'stderr':result.stderr.decode()}))\n"
                "sys.stderr.buffer.write(result.stderr)\nsys.exit(result.returncode)\n"
            )
            evidence["trust"] = trust_fixture_hook(binary, tmp_path, env)
            events, stderr = run_native(binary, client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr, requests=requests)
        hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
        assert len(hooks) == 1 and hooks[0]["tool_input"]["command"] == text
        result = json.loads((tmp_path / "guard-result.json").read_text())
        admitted = kind in {"relative", "absolute"}
        assert result["returncode"] == (0 if admitted else 2)
        assert result["stdout"] == ""
        if not admitted:
            assert source.exists() == (before is not None)
            assert not source.exists() or source.read_bytes() == before
            assert destination.exists() == (destination_before is not None)
            assert not destination.exists() or destination.read_bytes() == destination_before
            assert outside.read_text() == "alpha\n"
        elif operation == "delete":
            assert not source.exists()
        elif operation == "move":
            assert not source.exists() and destination.read_text() == "beta\n"
        else:
            assert source.read_text() == "beta\n"
    finally:
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
