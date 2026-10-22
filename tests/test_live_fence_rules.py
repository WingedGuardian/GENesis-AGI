"""The live fence's decision rules, and its two run-level guarantees.

Companion to tests/test_live_fence.py. Every refused probe is harmless
unfenced; the guarded deploy-script stand-in is a stub under tmp_path that
only sources the real guard library.
"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from tests import _live_fence as fence


@pytest.fixture(autouse=True)
def _every_stub(monkeypatch):
    """Every deny-listed name gets a stub, installed here or not (as on CI)."""
    parts = os.environ["PATH"].split(os.pathsep)
    parts = [fence._S.all_stub_dir if p == fence._S.shim_dir else p for p in parts]
    monkeypatch.setenv("PATH", os.pathsep.join(parts))


@pytest.mark.parametrize("search", [".", "", "missing:."])
def test_relative_search_paths_use_the_child_cwd(tmp_path, monkeypatch, search):
    child = tmp_path / "child"
    child.mkdir()
    tool = child / "sudo"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())
    env = {"HOME": str(tmp_path), "PATH": search}
    assert fence.judge("sudo", ["sudo"], env, str(child)) is not None
    monkeypatch.setattr(fence._S, "allowed_roots", (str(child),))
    assert fence.judge("sudo", ["sudo"], env, str(child)) is None


def test_relative_tool_alias_uses_the_child_cwd(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    tool = child / "sudo"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    (child / "runner").symlink_to(tool.name)
    monkeypatch.setattr(fence._S, "allowed_roots", ())
    env = {"HOME": str(tmp_path), "PATH": os.defpath}
    assert fence.judge("./runner", ["./runner"], env, str(child)) is not None


def test_non_utf8_argv_keeps_the_uncatchable_refusal(tmp_path, monkeypatch):
    tool = tmp_path / "sudo"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())
    caught = False
    with fence.expect_violation() as violation:
        try:
            subprocess.run([str(tool), bytes([255])], check=False)
        except Exception:
            caught = True
    assert not caught
    assert any("audit" in line and "\\udcff" in line for line in violation.reasons)


def test_retired_pytest_keeps_inherited_shims_armed(tmp_path):
    base = tmp_path / "fence"
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    tool = fallback / "sudo"
    tool.write_text("#!/bin/sh\nprintf owned-fallback\n")
    tool.chmod(0o755)
    fence._write_shims(base / "bin", str(base / "hits.log"), ["sudo"])
    env = {"PATH": f"{base / 'bin'}:{fallback}:{os.defpath}", "HOME": str(tmp_path)}
    first = subprocess.run(["/bin/sh", "-c", "sudo"], env=env, capture_output=True)
    fence.uninstall_files(type("Config", (), {"_genesis_live_fence_dir": str(base)})())
    last = subprocess.run(["/bin/sh", "-c", "sudo"], env=env, capture_output=True)
    assert first.returncode != 0
    assert last.returncode == first.returncode
    assert b"owned-fallback" not in last.stdout


@pytest.mark.parametrize("name", ["claude", "claude.exe", "claude-code"])
@pytest.mark.parametrize("action,status", [("-p", 3), ("--help", 127), ("--version", 127)])
def test_claude_shim_spellings_share_refusal_and_ci_answers(tmp_path, name, action, status):
    log = tmp_path / "hits.log"
    fence._write_shims(tmp_path / "bin", str(log), [name])
    result = subprocess.run([str(tmp_path / "bin" / name), action], capture_output=True)
    assert result.returncode == status
    assert ("\tanswered\t" in log.read_text()) is (status == 127)


def test_real_claude_removes_all_installed_spellings_but_keeps_other_tools(tmp_path, monkeypatch):
    monkeypatch.setattr(fence._S, "base", str(tmp_path))
    monkeypatch.setattr(fence._S, "stubbed", frozenset({"claude", "claude.exe", "claude-code", "sudo"}))
    directory = Path(fence._stub_dir_without(frozenset({"claude"})))
    assert {path.name for path in directory.iterdir()} == {"sudo"}
    with fence.expect_violation() as violation:
        subprocess.run([str(directory / "sudo")], capture_output=True)
    assert any("sudo: found the fence's stub" in reason for reason in violation.reasons)


@pytest.mark.parametrize("optin", [False, True])
@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("current_stub", [False, True])
def test_live_cli_collection_requires_original_availability(tmp_path, monkeypatch, optin, available, nested, current_stub):
    original = tmp_path / "original"
    current = tmp_path / "current"
    original.mkdir()
    current.mkdir()
    for directory, present in [(original, available), (current, current_stub)]:
        if present:
            executable = directory / "claude"
            executable.write_text("#!/bin/sh\nexit 99\n")
            executable.chmod(0o755)
    monkeypatch.setattr(fence._S, "orig_path", str(original))
    monkeypatch.setenv("PATH", str(current))
    monkeypatch.setenv("GENESIS_LIVE_E2E", "1" if optin else "0")
    monkeypatch.setenv("CLAUDECODE", "1" if nested else "0")
    root = Path(__file__).parent
    for relative, functions in [
        ("test_cc/test_integration.py", ["test_real_cc_invocation", "test_real_cc_with_system_prompt"]),
        ("test_session_awareness/test_arbiter.py", ["test_live_arbiter_e2e"]),
    ]:
        module = runpy.run_path(str(root / relative))
        for name in functions:
            skipped = any(mark.args[0] for mark in module[name].pytestmark if mark.name == "skipif")
            assert skipped is (not optin or not available or (nested and relative.startswith("test_cc/")))


@pytest.mark.parametrize("home", ["absolute", "missing", "relative"])
def test_install_without_a_passwd_entry(tmp_path, home):
    code = """
import importlib.util, json, os, pathlib, sys, types
spec = importlib.util.spec_from_file_location('isolated_fence', sys.argv[1])
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)
def missing(uid):
    raise KeyError(uid)
f.pwd.getpwuid = missing
hooks = []
f.sys.addaudithook = hooks.append
config = types.SimpleNamespace(
    option=types.SimpleNamespace(basetemp=sys.argv[2]),
    addinivalue_line=lambda *args: None,
)
f.install(config)
print(json.dumps({'armed': f._S.installed, 'home': f._S.real_home,
                  'shim_exists': pathlib.Path(f._S.shim_dir).is_dir(),
                  'hook_count': len(hooks),
                  'roots': os.environ.get('GENESIS_TEST_FENCE_LIVE_ROOTS'),
                  'log': os.environ.get('GENESIS_TEST_FENCE_LOG')}))
"""
    env = {"PATH": os.defpath, "TMPDIR": str(tmp_path)}
    if home != "missing":
        env["HOME"] = str(tmp_path / "home") if home == "absolute" else "relative-home"
    result = subprocess.run(
        [sys.executable, "-c", code, fence.__file__, str(tmp_path)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    outcome = json.loads(result.stdout)
    repo_root = os.path.realpath(Path(fence.__file__).resolve().parent.parent)
    assert outcome == {
        "armed": True,
        "home": env.get("HOME") if home == "absolute" else None,
        "shim_exists": True,
        "hook_count": 1,
        "roots": repo_root,
        "log": outcome["log"],
    }
    assert outcome["log"].endswith("hits.log")


# ── names: other names for claude, missing paths, mappings, named opt-outs ──


def test_claude_under_another_name_is_still_claude(tmp_path, monkeypatch):
    target = tmp_path / "claude.exe"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    link = tmp_path / "cc"
    link.symlink_to(target)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    assert fence.judge(str(link), [str(link), "-p", "x"], None) is not None
    assert fence.judge(str(target), [str(target), "-p", "x"], None) is not None


def test_a_bare_path_alias_to_claude_is_still_claude(tmp_path, monkeypatch):
    target = tmp_path / "claude.exe"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    (tmp_path / "cc").symlink_to(target)
    monkeypatch.setattr(fence._S, "allowed_roots", ())
    env = {"PATH": str(tmp_path)}
    assert fence.judge("cc", ["cc", "-p", "x"], env) is not None


@pytest.mark.real_tool("gh")
@pytest.mark.user_manager
def test_both_opt_outs_on_one_test_keep_one_stub_dir_in_front():
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert os.path.isdir(first) and first.startswith(fence._S.base), first
    assert not os.path.exists(os.path.join(first, "gh"))
    assert not os.path.exists(os.path.join(first, "systemd-run"))


def test_a_missing_absolute_path_is_left_to_fail_as_on_ci():
    assert fence.judge("/nonexistent/bin/claude", ["/nonexistent/bin/claude", "-p"], None) is None


def test_an_environ_mapping_is_read(tmp_path, monkeypatch):
    tool = tmp_path / "sudo"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", (str(tmp_path),))
    env = MappingProxyType({"PATH": str(tmp_path)})
    assert fence.judge("sudo", ["sudo"], env) is None  # resolved inside an allowed root


@pytest.mark.real_tool("gh")
def test_real_tool_lets_only_the_named_tools_run():
    assert fence._S.real_tool_names == frozenset({"gh"})
    stubs = {"PATH": fence._S.all_stub_dir}
    assert fence.judge("claude", ["claude", "-p", "x"], stubs) is not None
    assert fence.judge("sudo", ["sudo", "-n", "true"], stubs) is not None


# ── run-level guarantees, in a separate pytest process ──────────────────────

_SUBRUN_CONFTEST = """
pytest_plugins = ["tests._live_fence"]


def pytest_configure(config):
    from tests import _live_fence

    _live_fence.install(config)
"""

_SUBRUN_TESTS = """
import os
import subprocess
import tempfile
import time


def test_swallowed():
    try:
        subprocess.run(["sudo", "-n", "true"])
    except BaseException:
        pass


def test_answered_is_not_failed():
    subprocess.run(["systemctl", "--user", "is-active", "fence-probe-nonexistent"])


def test_guarded_script_refusal_fails_at_teardown():
    # A deploy-script stand-in: source the real guard, name a listed root. It
    # refuses itself (exit 97) and its REFUSED line lands in the fence log;
    # this test passed, so teardown must fail it.
    lib = os.path.join(os.environ["REPO"], "scripts", "lib", "live_system_guard.sh")
    root = os.path.realpath(os.environ["REPO"])
    script = os.path.join(tempfile.mkdtemp(), "deploy.sh")
    with open(script, "w") as f:
        f.write(f'#!/bin/bash\\n. "{lib}"\\nrefuse_if_test_fenced_live "{root}" || exit 97\\n')
    out = subprocess.run(["bash", script], capture_output=True)
    assert out.returncode == 97


def test_detached():
    # A script file: the in-process check cannot see inside it, so the call
    # reaches the stub from a child, after this test has ended.
    late = tempfile.mkdtemp() + "/late.sh"
    with open(late, "w") as f:
        f.write("sleep 1\\nsudo -n true\\n")
    subprocess.Popen(["sh", late])


def test_after():
    time.sleep(3)
"""


def test_the_fence_fails_a_swallowed_refusal_and_a_late_hit(tmp_path):
    """A small suite under the fence in its own pytest process: a refusal the code
    swallowed still fails its test, an answered read does not, a deploy script's
    own REFUSED line fails its test at teardown, and a refusal from a detached
    child after its test ended fails the session."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo), "REPO": str(repo)}
    env.pop("PYTEST_CURRENT_TEST", None)
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rA", str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    text = out.stdout + out.stderr
    assert out.returncode == 1, text
    assert "ERROR test_sub.py::test_swallowed" in text, text
    bad = [ln for ln in text.splitlines() if ln.startswith(("FAILED", "ERROR"))]
    assert not any("test_answered_is_not_failed" in ln for ln in bad), text
    assert "ERROR test_sub.py::test_guarded_script_refusal_fails_at_teardown" in text, text
    assert "REFUSED (test fence):" in text, text
    assert "refusals outside their test" in text and "test_detached" in text, text
    assert "live-state reads answered by a stub" in text, text
