"""The live fence's decision rules, and its two run-level guarantees.

Companion to tests/test_live_fence.py. Every deploy script these tests run is a
stub under tmp_path that only exits; every refused probe is harmless unfenced.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
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


# ── a deploy script against live state: live HOME or the live checkout ─────


@pytest.fixture()
def live_install(tmp_path, monkeypatch):
    """A stand-in for the deployed checkout: its scripts/update.sh is a stub that
    only exits, and the fence treats its root as the live one."""
    root = tmp_path / "deployed"
    script = root / "scripts" / "update.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr(fence._S, "live_roots", frozenset({os.path.realpath(root)}))
    return script


def test_a_live_checkout_script_with_a_private_home_is_refused(live_install, tmp_path):
    """The script takes its root from where it lives: a private HOME does not keep
    a run of the deployed checkout's update.sh off the live tree."""
    home = tmp_path / "home"
    home.mkdir()
    with fence.expect_violation() as v:
        subprocess.run(["bash", str(live_install)], env={**os.environ, "HOME": str(home)})
    assert any("live install" in r for r in v.reasons), v.reasons


def test_a_root_override_to_a_fixture_is_allowed(live_install, tmp_path):
    other = tmp_path / "fixture-tree"
    other.mkdir()
    env = {"HOME": str(tmp_path), "GENESIS_UPDATE_ORIG_DIR": str(other), "PATH": os.defpath}
    assert fence.judge("bash", ["bash", str(live_install)], env) is None


@pytest.mark.parametrize(
    "script,variable",
    [
        ("update.sh", "GENESIS_UPDATE_ORIG_DIR"),
        ("deploy_code_only.sh", "GENESIS_DEPLOY_ROOT"),
        ("backup.sh", "GENESIS_DIR"),
        ("restore.sh", "GENESIS_DIR"),
    ],
)
def test_relative_root_overrides_use_the_child_cwd(live_install, tmp_path, script, variable):
    live_root = live_install.parent.parent
    fixture = tmp_path / "fixture" / "scripts" / script
    fixture.parent.mkdir(parents=True)
    fixture.write_text("#!/bin/sh\nexit 0\n")
    env = {"HOME": str(tmp_path / "home"), "PATH": os.defpath, variable: "."}
    argv = ["bash", str(fixture)]
    assert fence.judge("bash", argv, env, str(live_root)) is not None
    assert fence.judge("bash", argv, env, str(fixture.parent.parent)) is None


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


def test_replay_direct_with_a_private_home_is_refused(live_install, monkeypatch, tmp_path):
    """The review's shape: the #3138 test with HOME on tmp. `_apply_direct`'s
    fallback runs the deployed update.sh, whose own systemctl fallback is a pkill
    of the live server. Refused because the script is the live checkout's."""
    from flask import Flask

    from genesis.dashboard.routes import updates

    for name in ("_STATE_FILE", "_SUMMARY_FILE", "_ESCALATION_FILE", "_CONFLICT_FILE"):
        monkeypatch.setattr(updates, name, tmp_path / name)
    monkeypatch.setattr(updates, "_HOME", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updates, "_UPDATE_SCRIPT", live_install)
    monkeypatch.setattr(updates, "_systemd_run_scope_available", lambda env: False)
    with Flask(__name__).app_context(), fence.expect_violation() as v:
        updates._apply_direct(Path("/nonexistent/pid"))
    assert any("live install" in r for r in v.reasons), v.reasons


S = "/x/scripts/update.sh"


@pytest.mark.parametrize(
    "argv",
    [
        ["env", "bash", S],
        ["timeout", "60", "bash", "-o", "pipefail", S],
        ["bash", "-c", f"cd /a && bash {S}"],
        ["sudo", "bash", S],
        ["setsid", "bash", "+x", S],
        ["systemd-run", "--user", "--scope", "--", "bash", S],
        ["sh", "-c", f"source {S}"],
        ["bash", "--rcfile", "/dev/null", S],
    ],
    ids=[
        "env",
        "timeout-option",
        "dash-c",
        "sudo",
        "setsid-plus",
        "systemd-run",
        "source",
        "rcfile",
    ],
)
def test_every_spelling_of_a_real_home_deploy_run_is_refused(argv):
    env = {"HOME": fence._S.real_home, "PATH": os.defpath}
    assert fence.judge(argv[0], argv, env) is not None


def test_parse_only_relative_paths_and_os_system_are_judged_right():
    env = {"HOME": fence._S.real_home, "PATH": os.defpath}
    assert fence.judge("bash", ["bash", "-n", S], env) is None
    assert fence.judge("bash", ["bash", "update.sh"], env, "/x/scripts") is not None
    # os.system is judged as `sh -c <line>`
    assert fence.judge("sh", ["sh", "-c", f"cd /x && bash {S}"], None) is not None


@pytest.mark.parametrize(
    "flags,runs",
    [
        (["+n"], True),
        (["+xn"], True),
        (["-n", "+n"], True),
        (["+n", "-n"], False),
        (["-o", "noexec"], False),
        (["+o", "noexec"], True),
        (["-o", "noexec", "+o", "noexec"], True),
        (["+o", "noexec", "-o", "noexec"], False),
    ],
)
def test_script_noexec_state_obeys_option_order(flags, runs):
    env = {"HOME": fence._S.real_home, "PATH": os.defpath}
    assert bool(fence.judge("bash", ["bash", *flags, S], env)) is runs


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
import importlib.util, json, pathlib, sys, types
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
                  'hook_count': len(hooks)}))
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
    assert outcome == {
        "armed": True,
        "home": env.get("HOME") if home == "absolute" else None,
        "shim_exists": True,
        "hook_count": 1,
    }


def test_a_heredoc_that_only_prints_a_denied_name_runs():
    """`cat <<EOF` with `exec claude` in its body prints the line: the body is
    data, dropped before the parser would read it as a command."""
    script = 'cat <<W\nexec claude "\\$@"\nW\n'  # install.sh's wrapper heredoc
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True)
    assert out.stdout == 'exec claude "$@"\n'
    assert fence._drain() == []


@pytest.mark.parametrize(
    "script",
    [
        "bash <<W\nsudo -n true\nW\n",  # fed to a shell: the body runs
        "cat <<W | sh\nsudo -n true\nW\n",  # piped onward
        "cat <<W\n$(sudo -n true)\nW\n",  # unquoted: the substitution runs
        "cat <<'W'\nx\nW\nclaude -p x\n",  # after the heredoc ends
        "cat <<W\nx\nW\nexport PATH=/usr/bin; claude -p x\n",
    ],
    ids=["shell", "pipe", "substitution", "after-body", "after-body-export"],
)
def test_a_heredoc_body_that_runs_and_what_follows_are_still_refused(script):
    assert fence.judge("bash", ["bash", "-c", script], None) is not None


def test_without_a_heredoc_a_shell_string_is_refused_in_process():
    assert fence.judge("bash", ["bash", "-c", "claude -p x"], None) is not None
    assert fence.judge("sh", ["sh", "-c", "sudo -n true"], None) is not None


# ── where each deploy script acts ───────────────────────────────────────────


@pytest.fixture()
def live_tree(tmp_path, monkeypatch):
    """A stand-in deployed checkout holding stub copies of every deploy script."""
    root = tmp_path / "deployed"
    (root / "scripts").mkdir(parents=True)
    for name in fence.DEPLOY_SCRIPTS:
        script = root / "scripts" / name
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(0o755)
    monkeypatch.setattr(fence._S, "live_roots", frozenset({os.path.realpath(root)}))
    return root


def _private(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "PATH": os.defpath, **extra}


def test_backup_and_restore_act_on_genesis_dir_not_where_they_live(live_tree, tmp_path):
    """backup.sh/restore.sh act on ${GENESIS_DIR:-$HOME/genesis}: run from the
    live checkout against a fixture tree is allowed; pointed at the live
    checkout from anywhere, refused."""
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    other = tmp_path / "worktree" / "scripts"
    other.mkdir(parents=True)
    (other / "restore.sh").write_text("#!/bin/sh\nexit 0\n")
    for name in ("backup.sh", "restore.sh"):
        live_script = str(live_tree / "scripts" / name)
        assert (
            fence.judge("bash", ["bash", live_script], _private(tmp_path, GENESIS_DIR=str(fixture)))
            is None
        )
        assert (
            fence.judge("bash", ["bash", live_script], _private(tmp_path)) is None
        )  # $HOME/genesis
    env = _private(tmp_path, GENESIS_DIR=str(live_tree))
    assert fence.judge("bash", ["bash", str(other / "restore.sh")], env) is not None


def test_an_override_naming_the_live_checkout_is_refused(live_tree, tmp_path):
    other = tmp_path / "worktree" / "scripts"
    other.mkdir(parents=True)
    for name, var in fence._ROOT_OVERRIDES.items():
        (other / name).write_text("#!/bin/sh\nexit 0\n")
        env = _private(tmp_path, **{var: str(live_tree)})
        assert fence.judge("bash", ["bash", str(other / name)], env) is not None, name
        env = _private(tmp_path, **{var: str(tmp_path / "fixture")})
        assert fence.judge("bash", ["bash", str(other / name)], env) is None, name


def test_bootstrap_acts_on_the_checkout_it_lives_in(live_tree, tmp_path):
    script = str(live_tree / "scripts" / "bootstrap.sh")
    assert fence.judge("bash", ["bash", script], _private(tmp_path)) is not None


def test_a_deploy_command_the_shell_builds_is_judged_by_home(tmp_path):
    """`cd <dir>/scripts && ./update.sh`, `"$ROOT"/scripts/update.sh`: the file
    cannot be found from here, so the HOME rule alone decides."""
    real = {"HOME": fence._S.real_home, "PATH": os.defpath}
    for line in ("cd /x/scripts && ./update.sh", '"$ROOT"/scripts/backup.sh'):
        assert fence.judge("bash", ["bash", "-c", line], real) is not None, line
        assert fence.judge("bash", ["bash", "-c", line], _private(tmp_path)) is None, line


# ── names: PATH set on the command line, other names for claude ─────────────


def test_a_path_set_on_the_command_line_is_honored(tmp_path, monkeypatch):
    """`env PATH=<dir> systemctl` runs the systemctl in <dir>, not the stub."""
    real_bin = tmp_path / "realbin"
    real_bin.mkdir()
    fake = real_bin / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    for argv in (
        ["env", f"PATH={real_bin}", "systemctl", "--user", "stop", "x"],
        ["sh", "-c", f"PATH={real_bin} systemctl --user stop x"],
    ):
        assert fence.judge(argv[0], argv, None) is not None, argv


def test_claude_under_another_name_is_still_claude(tmp_path, monkeypatch):
    target = tmp_path / "claude.exe"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    link = tmp_path / "cc"
    link.symlink_to(target)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    assert fence.judge(str(link), [str(link), "-p", "x"], None) is not None
    assert fence.judge(str(target), [str(target), "-p", "x"], None) is not None


@pytest.mark.real_tool("gh")
@pytest.mark.user_manager
def test_both_opt_outs_on_one_test_keep_one_stub_dir_in_front():
    first = os.environ["PATH"].split(os.pathsep)[0]
    assert os.path.isdir(first) and first.startswith(fence._S.base), first
    assert not os.path.exists(os.path.join(first, "gh"))
    assert not os.path.exists(os.path.join(first, "systemd-run"))


# ── deny names: wrappers, missing paths, mappings, named opt-outs ───────────


def test_a_wrapped_real_binary_is_refused(tmp_path, monkeypatch):
    """`env /path/to/claude -p` is refused like a direct call: every command a
    wrapper runs gets the deny check."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    assert fence.judge("env", ["env", str(fake), "-p", "x"], None) is not None


def test_a_missing_absolute_path_is_left_to_fail_as_on_ci():
    assert fence.judge("/nonexistent/bin/claude", ["/nonexistent/bin/claude", "-p"], None) is None


def test_an_environ_mapping_is_read(tmp_path):
    stub = tmp_path / "scripts" / "update.sh"
    env = MappingProxyType({"HOME": str(tmp_path), "PATH": os.defpath})
    assert fence.judge("bash", ["bash", str(stub)], env) is None


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
    swallowed still fails its test, an answered read does not, and a refusal from
    a detached child after its test ended fails the session."""
    (tmp_path / "conftest.py").write_text(_SUBRUN_CONFTEST)
    (tmp_path / "test_sub.py").write_text(_SUBRUN_TESTS)
    repo = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo)}
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
    assert "refusals outside their test" in text and "test_detached" in text, text
    assert "live-state reads answered by a stub" in text, text


# ── round-2 classes: wrappers alone, PATH anywhere on the line, heredoc edges ─


@pytest.mark.parametrize(
    "line",
    [
        "sudo -v",  # a refused wrapper with nothing after it
        "sudo",
        "echo $((1<<A))\nsudo -n true\nA",  # arithmetic, not a heredoc
        "# cat <<A\nsudo -n true\nA",  # a comment
        "echo '<<A'\nsudo -n true\nA",  # a quoted string
        "x=$(bash <<A\nsudo -n true\nA\n)",  # interpreter inside $(...)
        "<<A sh\nsudo -n true\nA",  # interpreter after the operator
        "bash <<< 'sudo -n true'",  # a here-string fed to a shell
    ],
    ids=[
        "wrapper-alone",
        "bare-wrapper",
        "arith",
        "comment",
        "quoted",
        "subst",
        "after-op",
        "herestring",
    ],
)
def test_shell_string_edges_are_still_judged(line):
    assert fence.judge("bash", ["bash", "-c", line], None) is not None


def test_a_here_string_fed_to_data_is_data():
    assert fence.judge("bash", ["bash", "-c", "grep -c x <<< 'claude -p x'"], None) is None


def test_a_path_set_anywhere_on_the_line_is_honored(tmp_path, monkeypatch):
    real_bin = tmp_path / "realbin"
    real_bin.mkdir()
    fake = real_bin / "systemctl"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(fence._S, "allowed_roots", ())  # treat tmp as an install dir
    for argv in (
        ["env", f"PATH={real_bin}", "bash", "-c", "systemctl --user stop x"],
        ["bash", "-c", f"PATH={real_bin} sh -c 'systemctl --user stop x'"],
        ["bash", "-c", f"export PATH={real_bin}; systemctl --user stop x"],
    ):
        assert fence.judge(argv[0], argv, None) is not None, argv


def test_env_dash_i_resolves_against_the_default_path():
    if shutil.which("systemctl", path=os.defpath) is None:
        pytest.skip("no systemctl on the default PATH")
    assert fence.judge("env", ["env", "-i", "systemctl", "--user", "stop", "x"], None) is not None


def test_a_shell_option_value_is_not_the_script():
    env = {"HOME": fence._S.real_home, "PATH": os.defpath}
    assert fence.judge("bash", ["bash", "-O", "extglob", S], env) is not None
