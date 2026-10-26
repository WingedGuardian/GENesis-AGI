"""Separate stateful publishing proof; run without the legacy replay plugin."""

import asyncio
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module
from tests.test_hooks import codex_adapter_replay as bridge
from tests.test_hooks import test_push_create_override as original

CASES = (
    "test_republish_bare_push_allows",
    "test_republish_explicit_push_allows",
    "test_safe_flags_still_allow",
    "test_republish_hit_records_branch",
    "test_republish_offline_allow_when_ls_remote_fails",
    "test_integration_record_stays_under_isolated_home",
)
COMMANDS = ("git push", "git push origin feat/x", "git push -uv -o ci.skip origin feat/x")


def arm_environment(home):
    home.mkdir()
    return {
        "PATH": os.environ["PATH"], "HOME": str(home), "GENESIS_HOME": str(home / ".genesis"),
        "GENESIS_DB_PATH": str(home / "absent.db"), "TMPDIR": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        "_TEST_CANONICAL_PUBLIC_REPO": "",
    }


@pytest.fixture
def publishing_fixture(monkeypatch, tmp_path, request):
    assert not request.config.pluginmanager.hasplugin("tests.test_hooks.codex_adapter_replay")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.syspath_prepend(str(bridge.ROOT / "scripts/hooks"))
    monkeypatch.setattr(bridge, "COPIED", {})
    config = SimpleNamespace(_codex_replay_state={})
    runtime = bridge.existing_replay_runtime.__wrapped__(
        SimpleNamespace(mktemp=lambda _: tmp_path), SimpleNamespace(config=config)
    )
    clone = Path(original.published_feat.__wrapped__(tmp_path))
    adapter = private_module("republish_adapter", bridge.ROOT / "scripts/hooks/codex_validator_adapter.py")
    allowlist = private_module("republish_allowlist", bridge.ROOT / "scripts/hooks/push_allowlist.py")
    return runtime, clone, adapter, allowlist


def invoke_replay(monkeypatch, tmp_path, fixture, name, *, scope=None, guard=None):
    runtime, clone, _, _ = fixture
    sidecar = tmp_path / "sidecar"
    (sidecar / "temp").mkdir(parents=True)
    env = arm_environment(tmp_path / "direct-home")
    request = SimpleNamespace(
        config=SimpleNamespace(_codex_replay_state={"sidecar": sidecar, "ambient_tmpdir": None}),
        node=SimpleNamespace(originalname=name, nodeid="fixture::" + name),
        module=scope or original,
    )
    monkeypatch.setattr(bridge, "ROWS", [])
    with pytest.MonkeyPatch.context() as patch:
        case = bridge.replay_case(request, patch, tmp_path,
                                  SimpleNamespace(getbasetemp=lambda: tmp_path), runtime)
        next(case)
        try:
            return subprocess.run(
                [sys.executable, str(guard or original._GUARD)], cwd=clone, env=env,
                input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(clone),
                                  "tool_input": {"command": "git push"}}),
                capture_output=True, text=True, timeout=15,
            )
        finally:
            with pytest.raises(StopIteration):
                next(case)


@pytest.mark.parametrize("name", CASES)
def test_six_frozen_publishing_allowances_remain_direct(monkeypatch, tmp_path, publishing_fixture, name):
    result = invoke_replay(monkeypatch, tmp_path, publishing_fixture, name)
    assert original._decision(result) == "allow"
    row = bridge.ROWS[-1]
    assert row["kind"] == "direct" and "publishing allowance" in row["reason"]
    assert row["publishing_test_sha256"] == bridge.sha(Path(original.__file__))
    assert row["original_guard_sha256"] == bridge.sha(original._GUARD)
    assert "adapter_assertions_passed" not in row
    assert (tmp_path / "direct-home/.genesis/pushed_branches.json").is_file()


@pytest.mark.parametrize("outside", ["module", "file", "case", "guard"])
def test_publishing_exception_does_not_expand(monkeypatch, tmp_path, publishing_fixture, outside):
    runtime, _, _, _ = publishing_fixture
    scope = SimpleNamespace(__name__=original.__name__, __file__=original.__file__)
    name, guard = CASES[0], original._GUARD
    if outside == "module":
        scope.__name__ = "tests.test_hooks.another_module"
    if outside == "file":
        scope.__file__ = str(tmp_path / "another_module.py")
    if outside == "case":
        name = "test_first_push_new_branch_asks"
    if outside == "guard":
        guard = runtime / "scripts/hooks/git_push_guard.py"
    result = invoke_replay(monkeypatch, tmp_path, publishing_fixture, name, scope=scope, guard=guard)
    assert original._decision(result) == "ask"
    assert bridge.ROWS[-1]["kind"] == "replay"
    assert not bridge.ROWS[-1]["actual_allow"]


@pytest.mark.parametrize("changed", ["test", "guard"])
def test_publishing_exception_requires_reviewed_source(monkeypatch, tmp_path, publishing_fixture, changed):
    target = Path(original.__file__) if changed == "test" else original._GUARD
    sha = bridge.sha
    monkeypatch.setattr(bridge, "sha", lambda path: "changed" if Path(path) == target else sha(path))
    with pytest.raises(AssertionError, match="publishing allowance source changed"):
        invoke_replay(monkeypatch, tmp_path, publishing_fixture, CASES[0])
    assert not (tmp_path / "direct-home/.genesis/pushed_branches.json").exists()


def git_state(clone):
    return tuple(subprocess.check_output(["git", "-C", str(clone), *args], text=True)
                 for args in (("config", "--local", "--list"), ("show-ref",)))


def assert_adapter_republish(raw, refused, note):
    specific = json.loads(raw["stdout"]).get("hookSpecificOutput", {}) if raw["stdout"].strip() else {}
    reason = specific.get("permissionDecisionReason", "")
    assert (raw["code"] == 0 and specific.get("permissionDecision") == "ask"
            and "another step in this command" in reason and "git config or remotes" in reason
            and refused and reason in note), "Expected real guard ask/refusal cause"


@pytest.mark.parametrize("offline", [False, True], ids=["fresh-online", "recorded-offline"])
@pytest.mark.parametrize("command", COMMANDS, ids=["bare", "explicit", "safe-flags"])
@pytest.mark.parametrize("fault", [None, "empty", "deny"], ids=["real", "empty-control", "deny-control"])
def test_republish_matrix_owns_both_initial_states(monkeypatch, tmp_path, publishing_fixture,
                                                 offline, command, fault):
    runtime, clone, adapter, allowlist = publishing_fixture
    remote = tmp_path / "remote.git"
    url = str(remote)
    direct_env = arm_environment(tmp_path / "direct-home")
    adapted_env = arm_environment(tmp_path / "adapted-home")
    initial = None
    if offline:
        seed_env = arm_environment(tmp_path / "fixture-record")
        with monkeypatch.context() as patch:
            patch.setattr(os, "environ", seed_env)
            assert allowlist._state_path() == Path(seed_env["GENESIS_HOME"]) / "pushed_branches.json"
            allowlist.record({url}, "feat/x")
            assert allowlist.is_recorded({url}, "feat/x")
        initial = (Path(seed_env["GENESIS_HOME"]) / "pushed_branches.json").read_bytes()
        remote.rename(tmp_path / "unavailable.git")
    for env in (direct_env, adapted_env):
        cache = Path(env["GENESIS_HOME"]) / "pushed_branches.json"
        if initial is not None:
            cache.parent.mkdir()
            cache.write_bytes(initial)
        assert cache.exists() == offline
    before = git_state(clone)
    remote_before = git_state(remote) if remote.exists() else None
    active = runtime
    guard_relative = "scripts/hooks/git_push_guard.py"
    expected_guard = bridge.sha(runtime / guard_relative)
    if fault:
        active = tmp_path / "mutant-runtime"
        shutil.copytree(runtime, active, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
        target = active / guard_relative
        assert not target.is_symlink()
        target.write_text("pass\n" if fault == "empty" else
                          'import sys; print("fixture generic deny",file=sys.stderr);sys.exit(2)\n')
        assert bridge.sha(target) != expected_guard
    payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": str(clone),
               "tool_input": {"command": command}}
    direct = subprocess.run(
        [str(runtime / ".claude/hooks/genesis-hook"), "hooks/git_push_guard.py"],
        input=json.dumps(payload), cwd=clone, env=direct_env, capture_output=True, text=True, timeout=15,
    )
    assert direct.returncode == 0 and original._decision(direct) == "allow"
    raw = {}
    reducer = adapter.child_result

    def observe(code, stdout, stderr):
        raw.update(code=code, stdout=stdout.decode(), stderr=stderr.decode())
        return reducer(code, stdout, stderr)

    payload["tool_input"]["command"] = "cd " + shlex.quote(str(clone)) + " || exit\n" + command
    refused, note = False, ""
    with monkeypatch.context() as patch:
        patch.setattr(os, "environ", adapted_env)
        patch.setattr(adapter, "RUNTIME_ROOT", active)
        patch.setattr(adapter, "child_result", observe)
        call = next(c for c in adapter.prepare(payload) if c[0][-1] == "hooks/git_push_guard.py")
        try:
            note = asyncio.run(adapter.run_guard(*call))
        except adapter.Refused as exc:
            refused, note = True, str(exc)
    if fault:
        with pytest.raises(AssertionError, match="Expected real guard ask/refusal cause"):
            assert_adapter_republish(raw, refused, note)
    else:
        assert_adapter_republish(raw, refused, note)
    for env in (direct_env, adapted_env):
        cache = Path(env["GENESIS_HOME"]) / "pushed_branches.json"
        expected = env is direct_env or not fault or offline
        assert cache.exists() == expected
        if expected:
            with monkeypatch.context() as patch:
                patch.setattr(os, "environ", env)
                assert allowlist.is_recorded({url}, "feat/x")
                assert allowlist._state_path() == cache
            if offline:
                assert cache.read_bytes() == initial
    assert git_state(clone) == before and remote.exists() == (not offline)
    if remote.exists():
        assert git_state(remote) == remote_before
    assert bridge.sha(runtime / guard_relative) == expected_guard
    (tmp_path / "republish-evidence.json").write_text(json.dumps({
        "offline": offline, "command": command, "fault": fault,
        "guard_sha256": expected_guard, "executed_guard_sha256": bridge.sha(active / guard_relative),
        "adapter_sha256": hashlib.sha256((bridge.ROOT / "scripts/hooks/codex_validator_adapter.py").read_bytes()).hexdigest(),
        "direct_genesis_home": direct_env["GENESIS_HOME"], "adapted_genesis_home": adapted_env["GENESIS_HOME"],
        "fixture_initial_sha256": hashlib.sha256(initial).hexdigest() if initial else None,
        "raw": raw, "refused": refused, "note": note,
        "kind": "instrument-control" if fault else "independent-state-pair",
    }, indent=2))
