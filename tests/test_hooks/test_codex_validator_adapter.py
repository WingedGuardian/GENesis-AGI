"""Adapter contract: caller authority, target identity and observable failures."""

import asyncio
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts/hooks"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(HOOKS))
    spec = importlib.util.spec_from_file_location("validator_adapter_fixture", HOOKS / "codex_validator_adapter.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HOME", str(tmp_path))
    return module


def payload(command="cd /tmp || exit\nprintf fixture", name="Bash", **extra):
    return {"hook_event_name": "PreToolUse", "tool_name": name,
            "tool_input": {"command": command}, "cwd": "/tmp",
            "session_id": "fixture-session", "tool_use_id": "fixture-call", **extra}


@pytest.mark.parametrize("command", ["pwd", "cd relative && pwd", "cd /tmp; pwd",
    "cd $HOME && pwd", "cd /tmp\n&& pwd", "cd /tmp &&", "cd /tmp/$(pwd) && pwd",
    "cd -- /tmp && pwd", "cd /tmp || pwd", "cd /tmp &&\npwd"])
def test_shell_requires_literal_absolute_anchor(adapter, command):
    with pytest.raises(ValueError):
        adapter.prepare(payload(command))


@pytest.mark.parametrize("body", ["printf fixture", "true & wait; pwd -P",
    "{ true; } & wait; pwd -P", "false || true & wait; pwd -P"])
def test_legacy_and_list_cannot_supply_foreground_anchor(adapter, tmp_path, body):
    with pytest.raises(ValueError, match="literal"):
        adapter.initial_directory(f"cd {tmp_path} && {body}")


@pytest.mark.parametrize("tail", [" || exit 0\npwd", " || exit;\npwd", " || exit &\npwd",
    " || exit # comment\npwd", " || exit > /dev/null\npwd", " || exit\\\n\npwd", " || exit\r\npwd",
    " || exit\n", " || exit\n   "])
def test_foreground_prefix_requires_exact_exit_newline_and_body(adapter, tmp_path, tail):
    with pytest.raises(ValueError):
        adapter.initial_directory(f"cd {tmp_path}" + tail)


@pytest.mark.parametrize("label,body,changes_directory", [('ordinary-semicolon', 'true; pwd -P', False),
 ('ordinary-and', 'true && pwd -P', False),
 ('ordinary-or', 'false || pwd -P', False),
 ('leading-and-list-background', 'true & wait; pwd -P', False),
 ('long-and-list-background', 'true && true & wait; pwd -P', False),
 ('or-list-background', 'false || true & wait; pwd -P', False),
 ('background-after-newline-continuation', 'true \\\n& wait; pwd -P', False),
 ('background-brace-body', '{ true; } & wait; pwd -P', False),
 ('background-subshell-body', '(true) & wait; pwd -P', False),
 ('background-if-body', 'if true; then true; fi & wait; pwd -P', False),
 ('background-while-body', 'while false; do true; done & wait; pwd -P', False),
 ('background-for-body', 'for x in 1; do true; done & wait; pwd -P', False),
 ('background-pipeline-body', 'true | true & wait; pwd -P', False),
 ('foreground-pipeline-body', 'true | true; pwd -P', False),
 ('body-subshell-background', '(true & wait); pwd -P', False),
 ('body-brace-background', '{ true & wait; }; pwd -P', False),
 ('body-if-background', 'if true; then true & wait; fi; pwd -P', False),
 ('body-for-background', 'for x in 1; do true & wait; done; pwd -P', False),
 ('later-independent-background', 'true; true & wait; pwd -P', False),
 ('newline-terminates-anchor-list', 'true\ntrue & wait; pwd -P', False),
 ('single-quoted-ampersand', "printf '%s' '&' >/dev/null; pwd -P", False),
 ('double-quoted-ampersand', 'printf "%s" "&" >/dev/null; pwd -P', False),
 ('escaped-ampersand', 'printf "%s" \\& >/dev/null; pwd -P', False),
 ('redirect-ampersand', 'true &>/dev/null; pwd -P', False),
 ('redirect-append-ampersand', 'true &>>/dev/null; pwd -P', False),
 ('redirect-fd-ampersand', 'true 2>&1; pwd -P', False),
 ('redirect-input-fd-ampersand', 'true 0<&0; pwd -P', False),
 ('comment-ampersand', 'true # & ignored\npwd -P', False),
 ('command-substitution-background', 'x=$(true & wait); pwd -P', False),
 ('process-substitution-background', 'cat <(true & wait); pwd -P', False),
 ('later-foreground-cd', 'true; cd @OTHER@; pwd -P', True),
 ('later-brace-cd', '{ cd @OTHER@; }; pwd -P', True),
 ('later-subshell-cd', '(cd @OTHER@); pwd -P', False)])
def test_foreground_opener_matches_bash_scope(adapter, tmp_path, label, body, changes_directory):
    anchor = tmp_path / "anchor"
    other = tmp_path / "other"
    anchor.mkdir()
    other.mkdir()
    body = body.replace("@OTHER@", shlex.quote(str(other)))
    command = f"cd {shlex.quote(str(anchor))} || exit\n{body}"
    assert adapter.initial_directory(command) == anchor
    actual = subprocess.run(["bash", "--noprofile", "--norc", "-c", command], cwd=tmp_path,
                            env={"PATH": os.defpath, "HOME": str(tmp_path)}, capture_output=True, text=True, timeout=5)
    assert actual.returncode == 0 and not actual.stderr, (label, actual)
    assert actual.stdout.strip().splitlines()[-1] == str(other if changes_directory else anchor)


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_shell_preserves_command_and_native_ids_but_replaces_cwd(adapter, tmp_path, quote):
    command = f"cd {quote}{tmp_path}{quote} || exit\nprintf '$HOME; untouched'"
    calls = adapter.prepare(payload(command, cwd="/not-execution-cwd"))
    assert len(calls) == 16
    for argv, data, budget in calls:
        assert data["tool_input"]["command"] == command
        assert data["cwd"] == str(tmp_path)
        assert data["session_id"] == "fixture-session"
        assert data["tool_use_id"] == "fixture-call"
        assert budget in {5, 8}
        assert "main_checkout_guard.py" not in " ".join(argv)
    push = [c for c in calls if "git_push_guard.py" in " ".join(c[0])]
    assert len(push) == 1 and push[0][2] == 5
    role = [c for c in calls if "validator_role_guard.py" in " ".join(c[0])]
    assert len(role) == 1 and role[0][2] == 8


def patch(*paths):
    return "*** Begin Patch\n" + "\n".join(f"*** Add File: {p}\n+fixture" for p in paths) + "\n*** End Patch"


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("root", ["tmp/validator", ".genesis/output/prv-fixture", ".genesis/output/validator-runs"])
def test_patch_checks_actual_absolute_target_and_never_bash_snapshots(adapter, tmp_path, root, relative):
    workspace = tmp_path / root
    workspace.mkdir(parents=True)
    target = workspace / "receipt.txt"
    name = "receipt.txt" if relative else str(target)
    calls = adapter.prepare(payload(patch(name), "apply_patch", cwd=str(workspace)))
    assert calls
    for _argv, data, _ in calls:
        assert data["tool_name"] == "Write"
        assert data["tool_input"] == {"file_path": str(target)}
    assert any("main_checkout_guard.py" in " ".join(c[0]) for c in calls)


@pytest.mark.parametrize("kind", ["outside", "mixed", "relative-decoy", "symlink", "root-symlink", "hardlink", "parent", "move", "config"])
def test_patch_rejects_every_unsafe_target(adapter, tmp_path, kind):
    root = tmp_path / "tmp/validator"
    root.mkdir(parents=True)
    target = root / "receipt.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    cwd = root
    command = patch(str(target))
    if kind == "outside":
        command = patch(str(outside))
    elif kind == "mixed":
        command = patch(str(target), str(outside))
    elif kind == "relative-decoy":
        command, cwd = patch("receipt.txt"), tmp_path
    elif kind == "symlink":
        target.symlink_to(outside)
    elif kind == "root-symlink":
        other = tmp_path / "other"
        other.mkdir()
        root.rmdir()
        root.symlink_to(other, target_is_directory=True)
    elif kind == "hardlink":
        target.hardlink_to(outside)
    elif kind == "parent":
        command = patch(str(root / ".." / "outside.txt"))
    elif kind == "move":
        command = f"*** Begin Patch\n*** Update File: {target}\n*** Move to: {outside}\n@@\n-old\n+new\n*** End Patch"
    elif kind == "config":
        command = patch(str(root / ".codex/config.toml"))
    with pytest.raises(ValueError):
        adapter.prepare(payload(command, "apply_patch", cwd=str(cwd)))
    assert outside.read_text() == "untouched"


def test_mcp_only_gets_mcp_aware_guard_with_unchanged_arguments(adapter):
    data = payload(name="mcp__genesis_validator_memory__memory_store")
    data["tool_input"] = {"content": "synthetic", "nested": {"a": 1}}
    calls = adapter.prepare(data)
    assert len(calls) == 1
    assert "secrets_env_access_guard.py" in " ".join(calls[0][0])
    assert calls[0][1] == data


@pytest.mark.parametrize("bad", [None, [], {}, payload(name="Write"), payload(name="mcp__foreign__write"), payload(hook_event_name="PostToolUse")])
def test_unknown_native_shapes_are_refused(adapter, bad):
    with pytest.raises(ValueError):
        adapter.prepare(bad)


def script(tmp_path, body):
    path = tmp_path / "guard.py"
    path.write_text("from pathlib import Path; Path(" + repr(str(tmp_path / "called")) + ").touch()\n" + body)
    return [sys.executable, str(path)]


@pytest.mark.parametrize("body,allowed,note", [
    ("pass", True, ""),
    ('print(\'{"hookSpecificOutput":{"additionalContext":"fixture advisory"}}\')', True, "fixture advisory"),
    ('import sys; print("fixture warning",file=sys.stderr)', True, "fixture warning"),
    ('print(\'{"hookSpecificOutput":{"permissionDecision":"ask","permissionDecisionReason":"needs owner"}}\')', False, "owner"),
    ('print(\'{"hookSpecificOutput":{"permissionDecision":"deny","permissionDecisionReason":"fixture deny"}}\')', False, "fixture deny"),
    ('import sys; print("fixture deny",file=sys.stderr);sys.exit(2)', False, "fixture deny"),
    ('raise RuntimeError("synthetic secret must not echo")', False, "failed"),
    ('print("not json")', False, "failed"),
    ('print(\'{"unexpected":true}\')', False, "failed"),
    ('print(\'{"hookSpecificOutput":{"permissionDecision":"unknown"}}\')', False, "failed"),
    ('print(\'{"hookSpecificOutput":{"permissionDecision":null}}\')', False, "failed"),
    ('print(\'{"hookSpecificOutput":{"additionalContext":42}}\')', False, "failed"),
])
def test_child_decisions_crashes_and_advisories(adapter, tmp_path, body, allowed, note):
    invocation = (script(tmp_path, body), payload(), 1)
    if allowed:
        assert note in asyncio.run(adapter.run_guard(*invocation))
        assert (tmp_path / "called").exists()
    else:
        with pytest.raises(ValueError, match=note) as caught:
            asyncio.run(adapter.run_guard(*invocation))
        assert "synthetic secret" not in str(caught.value)


@pytest.mark.parametrize("decision", [None, "allow"])
@pytest.mark.parametrize("fields", range(8))
def test_success_keeps_all_guard_explanation_fields(adapter, tmp_path, decision, fields):
    specific = {} if decision is None else {"permissionDecision": decision}
    if fields & 1:
        specific["permissionDecisionReason"] = "fixture successful reason"
    if fields & 2:
        specific["additionalContext"] = "fixture additional context"
    body = []
    if specific:
        body.append("print(" + repr(json.dumps({"hookSpecificOutput": specific})) + ")")
    if fields & 4:
        body.append("import sys; print('fixture stderr note', file=sys.stderr)")
    note = asyncio.run(adapter.run_guard(script(tmp_path, "\n".join(body)), payload(), 1))
    for bit, expected in ((1, "fixture successful reason"), (2, "fixture additional context"),
                          (4, "fixture stderr note")):
        if fields & bit:
            assert expected in note
    assert (tmp_path / "called").exists()


def test_child_environment_is_scrubbed_before_execution(adapter, tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    argv = script(tmp_path, 'import os; assert "GENESIS_CC_SESSION" not in os.environ; assert os.environ["CLAUDE_PROJECT_DIR"] == ' + repr(str(ROOT)))
    assert asyncio.run(adapter.run_guard(argv, payload(), 1)) == ""


def test_timeout_kills_owned_descendants_and_missing_executable_refuses(adapter, tmp_path):
    receipt = tmp_path / "late-receipt"
    body = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr("import time; from pathlib import Path; time.sleep(.7); Path(" + repr(str(receipt)) + ").touch()") + "]); time.sleep(20)"
    with pytest.raises(ValueError, match="timed out"):
        asyncio.run(adapter.run_guard(script(tmp_path, body), payload(), .15))
    # Wait inside an async test probe, never leave an unobserved background job.
    asyncio.run(asyncio.sleep(.8))
    assert not receipt.exists()
    with pytest.raises(ValueError, match="failed"):
        asyncio.run(adapter.run_guard([str(tmp_path / "missing")], payload(), 1))


def test_guards_execute_concurrently_and_whole_call_is_bounded(adapter, monkeypatch):
    started = []

    async def child(*args):
        started.append(args)
        while len(started) < 15:
            await asyncio.sleep(.001)
        return "advisory"

    monkeypatch.setattr(adapter, "run_guard", child)
    assert "advisory" in asyncio.run(adapter.evaluate(payload()))
    assert len(started) == 16
    monkeypatch.setattr(adapter, "OVERALL_BUDGET", .02)
    started.clear()
    async def hung(*args):
        await asyncio.sleep(20)
    monkeypatch.setattr(adapter, "run_guard", hung)
    with pytest.raises(ValueError, match="timed out"):
        asyncio.run(adapter.evaluate(payload()))


@pytest.mark.parametrize("body,expected", [("print('allow')", 0),
    ("print('deny');raise SystemExit(2)", 2), ("print('allow');raise SystemExit(1)", 2),
    ("raise RuntimeError()", 2), ("print('unknown')", 2), ("", 2),
    ("print('allow\\n{\"hookSpecificOutput\":{\"additionalContext\":\"fixture\"}}')", 0)])
def test_outer_requires_explicit_allow_and_signals_explained_denies(tmp_path, body, expected):
    wrapper = tmp_path / "codex-validator-adapter"
    shutil.copyfile(HOOKS / wrapper.name, wrapper)
    (tmp_path / "codex_validator_adapter.py").write_text(body)
    result = subprocess.run(["bash", "-c", 'bash "$1" 3>"$2"', "fixture", str(wrapper), str(tmp_path / "fd3")],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == expected
    assert (tmp_path / "fd3").read_text() == ("deny\n" if expected else "allow\n")
    if expected:
        assert result.stdout == ""
    elif "additionalContext" in body:
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "fixture"


def test_inline_guard_is_the_unchanged_configured_body(adapter):
    entries = json.loads((ROOT / ".claude/settings.json").read_text())["hooks"]["PreToolUse"]
    original = next(h["command"] for e in entries for h in e["hooks"] if "never boot the full" in h["command"])
    assert shlex.split(adapter.INLINE_BASH_SAFETY) == shlex.split(original)


def test_pipe_full_timeout_drains_and_reaps(adapter, tmp_path):
    argv = script(tmp_path, 'import os,time; os.write(1,b"x"*1048576); time.sleep(20)')
    async def probe():
        with pytest.raises(ValueError, match="timed out"):
            await asyncio.wait_for(adapter.run_guard(argv, payload(), .1), 2)
    asyncio.run(probe())


def test_patch_fanout_is_bounded_and_overall_cancellation_joins_children(adapter, tmp_path, monkeypatch):
    root = tmp_path / "tmp/validator"
    root.mkdir(parents=True)
    active = peak = 0
    async def child(*args):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        try:
            await asyncio.sleep(.01)
            return ""
        finally:
            active -= 1
    monkeypatch.setattr(adapter, "run_guard", child)
    asyncio.run(adapter.evaluate(payload(patch(*(str(root / f"f{i}") for i in range(30))), "apply_patch")))
    assert peak == 15 and active == 0


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"hook_event_name":"PreToolUse","hook_event_name":"PreToolUse"}',
    b'{"x":NaN}', b'{"x":1e999}', b"x" * (1024 * 1024 + 1)],
    ids=["malformed", "list", "duplicate", "nan", "infinite", "oversized"])
def test_malformed_or_oversized_native_input_is_denied_without_echo(tmp_path, raw):
    result = subprocess.run(["bash", str(HOOKS / "codex-validator-adapter")], input=raw,
                            capture_output=True, timeout=5,
                            env={"PATH": os.defpath, "HOME": str(tmp_path)})
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"BLOCKED:" in result.stderr


def test_outer_missing_evaluator_is_an_explained_deny(tmp_path):
    wrapper = tmp_path / "codex-validator-adapter"
    shutil.copyfile(HOOKS / wrapper.name, wrapper)
    result = subprocess.run(["bash", str(wrapper)], input=b"{}", capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b""
    assert b"did not explicitly allow" in result.stderr


@pytest.mark.parametrize("dependency,fault", [(None, None)] + [
    (name, fault) for name in ("codex_validator_guard", "codex_validator_patch")
    for fault in ("missing", "crash")
])
def test_launcher_denies_each_module_scope_import_failure(tmp_path, dependency, fault):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy

    runtime = source_copy(tmp_path)
    if dependency:
        target = runtime / "scripts/hooks" / (dependency + ".py")
        if fault == "missing":
            target.unlink()
        else:
            target.write_text("raise RuntimeError('fixture-import-failure')\n")
    data = payload(name="mcp__genesis_validator_memory__memory_store")
    data["tool_input"] = {"content": "synthetic"}
    signal_file = tmp_path / "fd3"
    result = subprocess.run(
        ["bash", "-c", 'bash "$1" 3>"$2"', "fixture",
         str(runtime / "scripts/hooks/codex-validator-adapter"), str(signal_file)],
        input=json.dumps(data), capture_output=True, text=True, timeout=10,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "GENESIS_HOME": str(tmp_path / ".genesis"),
             "GENESIS_DB_PATH": str(tmp_path / "absent.db")},
    )
    assert result.returncode == (2 if dependency else 0)
    assert signal_file.read_text() == ("deny\n" if dependency else "allow\n")
    assert not result.stdout
    if dependency:
        assert "did not explicitly allow" in result.stderr
        assert ("fixture-import-failure" if fault == "crash" else dependency) in result.stderr


# Reuse existing guard fixtures/vectors, and exercise each through the adapter's
# real invocation path. Commands here are INPUT ONLY, never executed as shell.
@pytest.mark.parametrize("guard,command,note,denied", [
    ("destructive_command_guard", "rm -rf /", "targets must be at least 4 levels deep", True),
    ("repo_routing_guard", "git add firmware/probe.py", "wrong-repo", True),
    ("full_suite_guard", "pytest tests/", "full-suite", True),
    ("tmux_kill_server_guard", "tmux kill-server", "kill-server", False),
    ("pipe_status_guard", "false | true; echo $?", "PIPESTATUS", False),
    ("credential_surface_hook", "ssh fixture@192.0.2.10", "Network topology", False),
    ("git_discard_guard", "git clean -fd", "git clean", True),
    ("git_push_guard", 'sqlite3 fixture.db "INSERT INTO t VALUES (1)"', "sqlite3", True),
    ("secrets_env_access_guard", "cat ~/genesis/secrets.env", "secrets.env", True),
    ("protected_paths_guard", "rm -rf ~/genesis/data", "irreplaceable", True),
    ("worktree_cwd_guard", "git worktree remove /tmp/fixture-worktree", "worktree removal", True),
    ("capped_read_advisory", "gh pr list", "30", False),
    ("rm_trash_advisory", "rm ~/.genesis/output/fixture.txt", "trash", False),
    ("pr_close_advisory", "gh pr close 1", "closes", False),
    ("inline", "git reset --hard", "git reset --hard", True),
])
def test_existing_guard_vectors_through_real_adapter_path(adapter, tmp_path, monkeypatch, guard, command, note, denied):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy
    from tests.test_hooks.test_repo_routing_guard import _TOPOLOGY, _make_repo, _write

    runtime = source_copy(tmp_path)
    monkeypatch.setattr(adapter, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(runtime))
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    for relative in ("genesis/secrets.env", ".genesis/output/fixture.txt"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
    credential = private_module("adapter_credential_paths", runtime / "scripts/hooks/credential_surface_hook.py")
    topology = credential._NETWORK_TOPOLOGY
    assert topology.is_relative_to(tmp_path)
    topology.parent.mkdir(parents=True)
    topology.write_text("192.0.2.10 uses a synthetic topology fixture\n")
    repo = _make_repo(tmp_path / "repo", "https://github.com/WingedGuardian/GENesis-AGI.git")
    _write(repo, "firmware/probe.py")
    config = tmp_path / "topology.yaml"
    config.write_text(_TOPOLOGY)
    monkeypatch.setenv("REPO_TOPOLOGY_PATH", str(config))
    # Prevent even an accidental metadata lookup from reaching the network.
    bins = tmp_path / "bin"
    bins.mkdir()
    (bins / "gh").write_text("#!/bin/sh\nexit 1\n")
    (bins / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", str(bins) + os.pathsep + os.environ["PATH"])
    calls = adapter.prepare(payload(f"cd {repo} || exit\n{command}", cwd=str(repo)))
    invocation = calls[-1] if guard == "inline" else next(c for c in calls if f"/{guard}.py" in " ".join(c[0]))
    if denied:
        with pytest.raises(ValueError, match=note):
            asyncio.run(adapter.run_guard(*invocation))
    else:
        assert note.lower() in asyncio.run(adapter.run_guard(*invocation)).lower()
    # A harmless action must still allow through the SAME real guard/launcher.
    control = {**invocation[1], "tool_input": {"command": f"cd {repo} || exit\nprintf fixture"}}
    asyncio.run(adapter.run_guard(invocation[0], control, invocation[2]))


@pytest.mark.parametrize("body,allowed", [(None, False), ("if broken", False), ("exit 0", False),
    ("printf allow >&3", True), ("printf deny >&3; echo fixture-denial >&2; exit 2", False),
    ("echo bad-json; exit 1", False),
    ('printf allow >&3; printf \'%s\\n\' \'{"hookSpecificOutput":{"additionalContext":"fixture"}}\'', True)])
def test_generated_outer_definition_requires_signal_and_preserves_advice(adapter, tmp_path, body, allowed):
    root = tmp_path / "runtime"
    wrapper = root / "scripts/hooks/codex-validator-adapter"
    wrapper.parent.mkdir(parents=True)
    if body is not None:
        wrapper.write_text(body)
    result = subprocess.run(["bash", "-c", adapter.hook_command(root)], capture_output=True, text=True, timeout=5)
    assert result.returncode == (0 if allowed else 2)
    if allowed and "additionalContext" in body:
        assert json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"] == "fixture"
    elif not allowed:
        assert result.stderr


def test_anchor_is_the_actual_child_cwd_for_relative_secret_globs(adapter, tmp_path, monkeypatch):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy
    runtime = source_copy(tmp_path)
    monkeypatch.setattr(adapter, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    anchor = tmp_path / "genesis"
    anchor.mkdir()
    (anchor / "secrets.env").write_text("synthetic")
    data = payload(f"cd {anchor} || exit\ncat s*.env", cwd=str(runtime))
    invocation = next(c for c in adapter.prepare(data) if "secrets_env_access_guard.py" in " ".join(c[0]))
    with pytest.raises(ValueError, match="owner"):
        asyncio.run(adapter.run_guard(*invocation))


@pytest.mark.parametrize("name", ["d[ab]", "d{a,b}"])
def test_anchor_rejects_unquoted_expansion_and_allows_quoted_literals(adapter, tmp_path, name):
    literal = tmp_path / name
    for directory in (literal, tmp_path / "da", tmp_path / "db"):
        directory.mkdir()
    raw = f"cd {literal} || exit\npwd"
    actual = subprocess.run(["bash", "-c", raw], capture_output=True, text=True, timeout=5)
    assert actual.returncode != 0 or actual.stdout.strip() != str(literal)
    with pytest.raises(ValueError, match="literal"):
        adapter.initial_directory(raw)
    for quote in ("'", '"'):
        anchored = f"cd {quote}{literal}{quote} || exit\npwd"
        actual = subprocess.run(["bash", "-c", anchored], capture_output=True, text=True, timeout=5)
        assert actual.returncode == 0 and actual.stdout.strip() == str(literal)
        assert adapter.initial_directory(anchored) == literal


@pytest.mark.parametrize("quote", ["", "'", '"'])
@pytest.mark.parametrize("linked", [False, True])
def test_anchor_refuses_parent_components_but_retains_canonical_and_symlink_anchors(adapter, tmp_path, quote, linked):
    origin = tmp_path / "origin"
    origin.mkdir()
    physical = tmp_path / "other/child"
    physical.mkdir(parents=True)
    child = origin / "child"
    if linked:
        child.symlink_to(physical, target_is_directory=True)
    else:
        child.mkdir()
    name = str(child) + "/.."
    actual = subprocess.run(["bash", "-c", f"cd {quote}{name}{quote} && pwd -P"],
                            capture_output=True, text=True, check=True, timeout=5)
    assert actual.stdout.strip() == str(origin)
    if linked:
        assert Path(name).resolve() != origin
    with pytest.raises(ValueError, match="parent"):
        adapter.initial_directory(f"cd {quote}{name}{quote} || exit\nprintf fixture")
    for anchor, expected in ((origin, origin), (child, physical if linked else child)):
        assert adapter.initial_directory(f"cd {quote}{anchor}{quote} || exit\nprintf fixture") == expected


def test_missing_anchor_directory_is_refused(adapter, tmp_path):
    with pytest.raises(ValueError, match="existing"):
        adapter.initial_directory(f"cd {tmp_path / 'absent'} || exit\nprintf fixture")
    missing = shlex.quote(str(tmp_path / "absent"))
    for opener, ran in ((f"cd {missing} || exit\n", False), (f"cd {missing}; ", True)):
        actual = subprocess.run(["bash", "--noprofile", "--norc", "-c", opener + "printf body-ran"],
                                cwd=tmp_path, capture_output=True, text=True, timeout=5)
        assert (actual.stdout == "body-ran") == ran
        assert (actual.returncode == 0) == ran


@pytest.mark.parametrize("suppressed", [False, True])
def test_install_ask_suppression_is_honored_without_changing_other_clients(adapter, tmp_path, monkeypatch, suppressed):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy
    runtime = source_copy(tmp_path)
    monkeypatch.setattr(adapter, "RUNTIME_ROOT", runtime)
    monkeypatch.delenv("_TEST_HOOK_ASK_POLICY", raising=False)
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    config = tmp_path / ".genesis/config/genesis.yaml"
    config.parent.mkdir(parents=True)
    config.write_text("hooks:\n  asks:\n    secrets_env: " + ("false\n" if suppressed else "true\n"))
    anchor = tmp_path / "genesis"
    anchor.mkdir()
    (anchor / "secrets.env").write_text("synthetic")
    invocation = next(c for c in adapter.prepare(payload(f"cd {anchor} || exit\ncat secrets.env"))
                      if "secrets_env_access_guard.py" in " ".join(c[0]))
    if suppressed:
        assert "hooks.asks.secrets_env" in asyncio.run(adapter.run_guard(*invocation))
    else:
        with pytest.raises(ValueError, match="owner"):
            asyncio.run(adapter.run_guard(*invocation))


@pytest.mark.parametrize("receiver", ["cat", "git commit -F -", "gh pr comment 1 --body-file -"])
@pytest.mark.parametrize("shape", ["prose-only", "neutral-only", "real-tail"])
def test_compound_heredoc_cost_and_real_removal_control(adapter, tmp_path, monkeypatch, receiver, shape):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy

    runtime = source_copy(tmp_path)
    monkeypatch.setattr(adapter, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    # The tail arm has neutral data: refusal must come from the actual trailing
    # operation, never the separately pinned inline-prose limitation.
    body = "fix: refuse `xargs git worktree remove`" if shape == "prose-only" else "fixture neutral text"
    command = receiver + " <<'EOF'\n" + body + "\nEOF"
    if shape == "real-tail":
        command += "\ngit worktree remove /tmp/fixture-not-executed"
    # Both inputs are guard data only: no cat, commit, post or removal executes.
    baseline = subprocess.run(
        [str(runtime / ".claude/hooks/genesis-hook"), "hooks/worktree_cwd_guard.py"],
        input=json.dumps(payload(command, cwd=str(tmp_path))),
        capture_output=True, text=True, cwd=tmp_path, timeout=8,
    )
    assert baseline.returncode == (2 if shape == "real-tail" else 0)
    call = next(c for c in adapter.prepare(payload(f"cd {tmp_path} || exit\n{command}"))
                if "/worktree_cwd_guard.py" in " ".join(c[0]))
    if shape == "neutral-only":
        asyncio.run(adapter.run_guard(*call))
    else:
        with pytest.raises(ValueError, match="worktree") as caught:
            asyncio.run(adapter.run_guard(*call))
        if shape == "prose-only":
            assert "stdin or a list" in str(caught.value)


@pytest.mark.parametrize("receiver", ["cat", "git commit -F", "gh pr comment 1 --body-file"])
def test_body_file_avoids_inline_prose_refusal(adapter, tmp_path, monkeypatch, receiver):
    from tests.test_hooks.test_codex_validator_adapter_native import source_copy

    runtime = source_copy(tmp_path)
    monkeypatch.setattr(adapter, "RUNTIME_ROOT", runtime)
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / ".genesis"))
    scratch = tmp_path / "tmp/validator"
    scratch.mkdir(parents=True)
    body = scratch / "fixture-body.txt"
    body.write_text("fixture-body: xargs git worktree remove\n")
    data = payload(f"cd {scratch} || exit\n{receiver} {shlex.quote(str(body))}")
    call = next(c for c in adapter.prepare(data) if "/worktree_cwd_guard.py" in " ".join(c[0]))
    asyncio.run(adapter.run_guard(*call))
    if receiver == "cat":
        # The read workflow must pass the whole adapter, not only this guard.
        asyncio.run(adapter.evaluate(data))
