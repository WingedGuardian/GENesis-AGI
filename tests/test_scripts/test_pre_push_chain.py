"""Installed native hooks must preserve the opted-in original and every input."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

SOURCE = Path(__file__).resolve().parents[2] / "scripts/hooks"


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, HOME=str(tmp_path))

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, env=env, capture_output=True, check=True).stdout

    git("-c", "init.templateDir=", "init", "-q")
    hooks = repo / "scripts/hooks"
    hooks.mkdir(parents=True)
    for name in ["sync-hooks.sh", "pre-push", "pre_push_chain.py", "pre_push_gates.sh"]:
        if (SOURCE / name).exists():
            shutil.copy2(SOURCE / name, hooks / name)

    def sync(*args):
        return subprocess.run(["bash", str(hooks / "sync-hooks.sh"), *args], cwd=repo, env=env, capture_output=True)

    return repo, env, git, sync


@pytest.mark.parametrize("absolute", [False, True])
def test_sync_uses_effective_hook_path(world, absolute):
    repo, env, git, sync = world
    target = repo / "custom-hooks"
    git("config", "core.hooksPath", str(target) if absolute else "custom-hooks")
    result = sync()
    assert result.returncode == 0, result.stderr
    assert (target / "pre-push").is_file()
    assert not (repo / ".git/hooks/pre-push").exists()


def test_custom_hook_stays_untouched_without_opt_in(world):
    repo, env, git, sync = world
    target = repo / "custom-hooks"
    target.mkdir()
    git("config", "core.hooksPath", str(target))
    hook = target / "pre-push"
    content = b"#!/bin/sh\nprintf original\n"
    hook.write_bytes(content)
    hook.chmod(0o751)
    assert sync().returncode == 2
    assert hook.read_bytes() == content
    assert hook.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize("stdin", [b"", b"(delete) " + b"0" * 40 + b" refs/heads/topic " + b"1" * 40 + b"\n"])
def test_opt_in_preserves_bytes_mode_location_and_stdin(world, stdin):
    repo, env, git, sync = world
    target = repo / "custom-hooks"
    target.mkdir()
    git("config", "core.hooksPath", str(target))
    hook = target / "pre-push"
    content = b'#!/bin/sh\ncat "$(dirname "$0")/sidecar"\nprintf "%s\\n" "$@"\ncat\n'
    hook.write_bytes(content)
    hook.chmod(0o751)
    (target / "sidecar").write_bytes(b"adjacent\n")
    result = sync("--chain-pre-push")
    assert result.returncode == 0, result.stderr
    assert hook.read_bytes() != content
    original = target / ".genesis-pre-push-original"
    assert original.read_bytes() == content
    assert original.stat().st_mode & 0o777 == 0o751
    run = subprocess.run([str(hook), "fixture", "fixture-destination"], cwd=repo, env=env, input=stdin, capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout == b"adjacent\nfixture\nfixture-destination\n" + stdin
    wrapper = hook.read_bytes()
    assert sync("--chain-pre-push").returncode == 0
    assert hook.read_bytes() == wrapper
    assert original.read_bytes() == content


def _custom(world, body=b"cat\n"):
    repo, env, git, sync = world
    target = repo / "custom-hooks"
    target.mkdir()
    git("config", "core.hooksPath", str(target))
    hook = target / "pre-push"
    hook.write_bytes(b"#!/bin/sh\n" + body)
    hook.chmod(0o751)
    return target, hook


def test_custom_consumption_does_not_hide_native_gate_input(world):
    repo, env, git, sync = world
    target, hook = _custom(world)
    assert sync("--chain-pre-push").returncode == 0
    data = b"(delete) " + b"0" * 40 + b" refs/heads/main " + b"1" * 40 + b"\n"
    run = subprocess.run([str(hook), "origin", "fixture"], cwd=repo, env=env, input=data, capture_output=True)
    assert run.returncode == 1
    assert run.stdout.startswith(data)
    assert b"Direct push" in run.stdout
    other = repo.parent / "unenrolled"
    other.mkdir()
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(other)], env=env, check=True)
    run = subprocess.run([str(hook), "origin", "fixture"], cwd=other, env=env, input=data, capture_output=True)
    assert run.returncode == 0
    assert run.stdout == data


def test_custom_failure_is_preserved(world):
    repo, env, git, sync = world
    target, hook = _custom(world, b"printf declined\\n\nexit 7\n")
    assert sync("--chain-pre-push").returncode == 0
    run = subprocess.run([str(hook), "origin", "fixture"], cwd=repo, env=env, input=b"", capture_output=True)
    assert run.returncode == 7


@pytest.mark.parametrize("conflict", [None, "wrapper", "original"])
def test_restore_preserves_original_or_reports_conflict(world, conflict):
    repo, env, git, sync = world
    target, hook = _custom(world)
    content = hook.read_bytes()
    assert sync("--chain-pre-push").returncode == 0
    if conflict:
        changed = hook if conflict == "wrapper" else target / ".genesis-pre-push-original"
        changed.write_bytes(b"user changed this\n")
        assert sync("--restore-pre-push").returncode == 2
        assert changed.read_bytes() == b"user changed this\n"
    else:
        assert sync("--restore-pre-push").returncode == 0
        assert hook.read_bytes() == content
        assert hook.stat().st_mode & 0o777 == 0o751


def test_existing_helper_collision_is_not_overwritten(world):
    repo, env, git, sync = world
    target, hook = _custom(world)
    helper = target / "pre_push_chain.py"
    helper.write_bytes(b"user helper\n")
    original = hook.read_bytes()
    assert sync("--chain-pre-push").returncode == 2
    assert helper.read_bytes() == b"user helper\n"
    assert hook.read_bytes() == original


def test_partial_installation_keeps_original_and_can_retry(world, monkeypatch):
    repo, env, git, sync = world
    target, hook = _custom(world)
    original = hook.read_bytes()
    spec = importlib.util.spec_from_file_location("chain_fixture", SOURCE / "pre_push_chain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    atomic = module._atomic

    def fail(path, data, mode):
        if path.name == "pre_push_gates.sh":
            raise OSError("owned installation fixture")
        atomic(path, data, mode)

    monkeypatch.setattr(module, "_atomic", fail)
    with pytest.raises(OSError):
        module.install(repo / "scripts/hooks", repo, opt_in=True)
    assert hook.read_bytes() == original
    monkeypatch.setattr(module, "_atomic", atomic)
    module.install(repo / "scripts/hooks", repo, opt_in=True)
    assert hook.read_bytes() != original
    assert (target / ".genesis-pre-push-original").read_bytes() == original


def test_separate_gitdir_effective_installation(world):
    repo, env, git, sync = world
    separate = repo.parent / "metadata"
    git("init", "--separate-git-dir", str(separate))
    assert sync().returncode == 0
    hook = separate / "hooks/pre-push"
    assert hook.is_file()
    run = subprocess.run([str(hook), "fixture", "fixture"], cwd=repo, env=env, input=b"", capture_output=True)
    assert run.returncode == 0, run.stderr


def test_main_can_install_linked_relative_effective_path(world):
    repo, env, git, sync = world
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    tree = git("hash-object", "-t", "tree", os.devnull).strip().decode()
    commit = git("commit-tree", tree, "-m", "fixture").strip().decode()
    git("update-ref", "HEAD", commit)
    linked = repo.parent / "linked"
    git("worktree", "add", "--detach", str(linked))
    git("config", "core.hooksPath", "relative-hooks")
    result = sync("--hook-repo", str(linked))
    assert result.returncode == 0, result.stderr
    hook = linked / "relative-hooks/pre-push"
    assert hook.is_file()
    run = subprocess.run([str(hook), "fixture", "fixture"], cwd=linked, env=env, input=b"", capture_output=True)
    assert run.returncode == 0, run.stderr


def test_real_git_push_runs_installed_gate(world):
    repo, env, git, sync = world
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    tree = git("hash-object", "-t", "tree", os.devnull).strip().decode()
    commit = git("commit-tree", tree, "-m", "synthetic native hook fixture").strip().decode()
    git("update-ref", "HEAD", commit)
    remote = repo.parent / "remote.git"
    subprocess.run(["git", "-c", "init.templateDir=", "init", "--bare", "-q", str(remote)], env=env, check=True)
    git("remote", "add", "fixture", str(remote))
    git("remote", "add", "origin", str(remote))
    assert sync().returncode == 0
    git("push", "fixture", "HEAD:refs/heads/topic")
    rejected = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=repo, env=env, capture_output=True)
    assert rejected.returncode != 0
    assert b"Direct push" in rejected.stdout
    absent = subprocess.run(["git", "--git-dir", str(remote), "show-ref", "--verify", "refs/heads/main"], env=env, capture_output=True)
    assert absent.returncode != 0


def test_modified_installed_helper_is_preserved(world):
    repo, env, git, sync = world
    target, hook = _custom(world)
    assert sync("--chain-pre-push").returncode == 0
    helper = target / "pre_push_gates.sh"
    helper.write_bytes(b"#!/bin/sh\n# user change\n")
    assert sync().returncode == 2
    assert helper.read_bytes() == b"#!/bin/sh\n# user change\n"


def test_chain_does_not_introduce_shell_environment_side_effects(world):
    repo, env, git, sync = world
    target, hook = _custom(world)
    hook.write_bytes(b'#!/usr/bin/python3\nimport os\nprint(os.environ["SHELLOPTS"])\nassert "CHAIN_LEAK" not in os.environ\n')
    assert sync("--chain-pre-push").returncode == 0
    startup = repo / "shell-startup"
    startup.write_text("export CHAIN_LEAK=unexpected\n")
    env.update(SHELLOPTS="errexit:pipefail", BASH_ENV=str(startup))
    run = subprocess.run([str(hook), "fixture", "fixture"], cwd=repo, env=env, input=b"", capture_output=True)
    assert run.returncode == 0, run.stderr
    assert run.stdout == b"errexit:pipefail\n"


def test_interrupted_wrapper_update_can_retry(world, monkeypatch):
    repo, env, git, sync = world
    target, hook = _custom(world)
    assert sync("--chain-pre-push").returncode == 0
    source = repo / "scripts/hooks"
    wrapper = source / "pre-push"
    wrapper.write_bytes(wrapper.read_bytes() + b"\n# new wrapper revision\n")
    spec = importlib.util.spec_from_file_location("chain_update_fixture", source / "pre_push_chain.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    atomic = module._atomic

    def fail(path, data, mode):
        if path.name == "pre-push":
            raise OSError("owned update interruption")
        atomic(path, data, mode)

    monkeypatch.setattr(module, "_atomic", fail)
    with pytest.raises(OSError):
        module.install(source, repo, opt_in=False)
    monkeypatch.setattr(module, "_atomic", atomic)
    module.install(source, repo, opt_in=False)
    assert hook.read_bytes() == wrapper.read_bytes()


def test_exact_target_source_restore_converges_with_manifest(world):
    repo, env, git, sync = world
    assert sync().returncode == 0
    target = repo / ".git/hooks"
    for name in ("pre-push", "pre_push_gates.sh"):
        source = repo / "scripts/hooks" / name
        source.write_bytes(source.read_bytes() + b"\n# approved target revision\n")
        shutil.copy2(source, target / name)
    assert sync().returncode == 0
    run = subprocess.run([str(target / "pre-push"), "fixture", "fixture"], cwd=repo, env=env, input=b"", capture_output=True)
    assert run.returncode == 0, run.stderr


def test_exact_legacy_downgrade_can_reupgrade(world):
    repo, env, git, sync = world
    assert sync().returncode == 0
    target = repo / ".git/hooks"
    shutil.copy2(repo / "scripts/hooks/pre_push_gates.sh", target / "pre-push")
    for name in ("pre_push_chain.py", "pre_push_gates.sh"):
        (target / name).unlink()
    assert sync().returncode == 0
    run = subprocess.run([str(target / "pre-push"), "fixture", "fixture"], cwd=repo, env=env, input=b"", capture_output=True)
    assert run.returncode == 0, run.stderr


def test_unowned_chain_wrapper_is_not_preserved_as_an_original(world):
    repo, env, git, sync = world
    hook = repo / ".git/hooks/pre-push"
    hook.parent.mkdir(exist_ok=True)
    shutil.copy2(repo / "scripts/hooks/pre-push", hook)
    assert sync("--chain-pre-push").returncode == 2
    assert not (hook.parent / ".genesis-pre-push-original").exists()


def test_restore_does_not_delete_an_unowned_preservation_file(world):
    repo, env, git, sync = world
    assert sync().returncode == 0
    original = repo / ".git/hooks/.genesis-pre-push-original"
    original.write_bytes(b"user-owned file created after installation\n")
    assert sync("--restore-pre-push").returncode == 2
    assert original.read_bytes() == b"user-owned file created after installation\n"


def chain(world):
    repo, env, git, sync = world
    return private_module("r7_owned_chain", repo / "scripts/hooks/pre_push_chain.py")


def install_original(world, custom=True):
    repo, env, git, sync = world
    target = repo / ".git/hooks"
    target.mkdir(exist_ok=True)
    hook = target / "pre-push"
    content = b"#!/bin/sh\ncat\n" if custom else None
    if custom:
        hook.write_bytes(content)
        hook.chmod(0o751)
    assert sync("--chain-pre-push").returncode == 0
    return target, hook, content


@pytest.mark.parametrize(
    "damage",
    ["missing-helper", "empty-helpers", "bool-version", "unknown-phase", "missing-closure"],
)
def test_damaged_record_cannot_skip_helper_preservation(world, damage):
    repo, env, git, sync = world
    target, hook, original = install_original(world)
    record = target / ".genesis-pre-push.json"
    state = json.loads(record.read_text())
    altered = b"#!/bin/sh\nprintf unexpected-consumer\n"
    helper = target / "pre_push_gates.sh"
    if damage in ("missing-helper", "empty-helpers"):
        helper.write_bytes(altered)
    else:
        altered = helper.read_bytes()
    if damage == "missing-helper":
        state["helpers"].pop("pre_push_gates.sh")
    elif damage == "empty-helpers":
        state["helpers"] = {}
    elif damage == "bool-version":
        state["version"] = True
    elif damage == "unknown-phase":
        state["phase"] = "other"
    else:
        state.pop("closure", None)
    record.write_text(json.dumps(state))
    before = record.read_bytes()
    run = subprocess.run(
        [str(hook), "fixture", "fixture"], cwd=repo, env=env, input=b"", capture_output=True
    )
    assert run.returncode == 2 and b"unexpected-consumer" not in run.stdout
    assert sync().returncode == 2
    assert helper.read_bytes() == altered and record.read_bytes() == before


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize(
    "boundary",
    [
        ".genesis-pre-push.json",
        ".genesis-pre-push-original",
        "pre_push_chain.py",
        "pre_push_gates.sh",
    ],
)
def test_restore_resumes_after_each_cleanup_unlink(world, monkeypatch, custom, boundary):
    if not custom and boundary == ".genesis-pre-push-original":
        pytest.skip("no original backup exists")
    repo, env, git, sync = world
    target, hook, content = install_original(world, custom)
    module = chain(world)
    unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path.parent == target and path.name == boundary:
            raise OSError("owned cleanup interruption")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError):
            module.install(repo / "scripts/hooks", repo, opt_in=False, restore=True)
    assert (target / module.MANIFEST).is_file(), "ownership must survive incomplete cleanup"
    module.install(repo / "scripts/hooks", repo, opt_in=False, restore=True)
    assert not (target / module.MANIFEST).exists()
    assert not (target / module.ORIGINAL).exists()
    assert all(not (target / name).exists() for name in module.HELPERS)
    if custom:
        assert hook.read_bytes() == content and hook.stat().st_mode & 0o777 == 0o751
    else:
        assert not hook.exists()
    assert sync("--chain-pre-push").returncode == 0


def expand_source(repo):
    source = repo / "scripts/hooks"
    path = source / "pre_push_chain.py"
    text = path.read_text()
    anchor = 'HELPERS = ("pre_push_chain.py", "pre_push_gates.sh")'
    assert text.count(anchor) == 1
    path.write_text(text.replace(anchor, anchor[:-1] + ', "future_chain_helper.py")'))
    (source / "future_chain_helper.py").write_text("# inert future owned helper\n")
    sync = source / "sync-hooks.sh"
    text = sync.read_text()
    anchor = '    "pre_push_gates.sh"\n'
    assert text.count(anchor) == 1
    sync.write_text(text.replace(anchor, anchor + '    "future_chain_helper.py"\n'))
    return source


@pytest.mark.parametrize("collision", ["file", "dangling-link", "directory"])
def test_additive_helper_collision_precedes_all_sync_mutations(world, collision):
    repo, env, git, sync = world
    target, hook, original = install_original(world)
    source = expand_source(repo)
    # An unrelated ordinary helper must not be synced before this refusal.
    generic = source / "emit_bugfix_audit.py"
    generic.write_text("new generic helper\n")
    prior = target / "emit_bugfix_audit.py"
    prior.write_text("existing generic helper\n")
    path = target / "future_chain_helper.py"
    if collision == "file":
        path.write_text("user content\n")
    elif collision == "dangling-link":
        path.symlink_to(target / "missing")
    else:
        path.mkdir()
    before = (target / ".genesis-pre-push.json").read_bytes()
    assert sync().returncode == 2
    assert prior.read_text() == "existing generic helper\n"
    assert (target / ".genesis-pre-push.json").read_bytes() == before
    if collision == "file":
        assert path.read_text() == "user content\n"
    elif collision == "dangling-link":
        assert path.is_symlink()
    else:
        assert path.is_dir()


def test_additive_helper_installation_retains_prior_closure_on_retry(world, monkeypatch):
    repo, env, git, sync = world
    target, hook, original = install_original(world)
    source = expand_source(repo)
    module = chain(world)
    atomic = module._atomic

    def interrupt(path, data, mode):
        if path.name == "future_chain_helper.py":
            raise OSError("owned expansion interruption")
        return atomic(path, data, mode)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_atomic", interrupt)
        with pytest.raises(OSError):
            module.install(source, repo, opt_in=False)
    state = json.loads((target / module.MANIFEST).read_text())
    assert state["phase"] == "prepared"
    assert state["previous"]["closure"] == ["pre_push_chain.py", "pre_push_gates.sh"]
    assert state["closure"] == list(module.HELPERS)
    module.install(source, repo, opt_in=False)
    active = json.loads((target / module.MANIFEST).read_text())
    assert active["phase"] == "active" and active["closure"] == list(module.HELPERS)
    assert "previous" not in active
    assert (target / "future_chain_helper.py").read_bytes() == (
        source / "future_chain_helper.py"
    ).read_bytes()


@pytest.mark.parametrize("retry", ["new-opt-in", "without-opt-in", "different-target", "existing-retry"])
def test_prepared_retry_enrolls_the_explicit_shared_repository(world, monkeypatch, retry):
    repo, env, git, sync = world
    target = repo.parent / "shared-hooks"
    git("config", "core.hooksPath", str(target))
    other = repo.parent / "other"
    other.mkdir()
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(other)], env=env, check=True)
    subprocess.run(["git", "-C", str(other), "config", "core.hooksPath", str(target)], env=env, check=True)
    shutil.copytree(repo / "scripts", other / "scripts")
    module = chain(world)
    atomic = module._atomic

    def interrupt(path, data, mode):
        if path == target / "pre-push":
            raise OSError("owned shared installation interruption")
        atomic(path, data, mode)

    with monkeypatch.context() as patch:
        patch.setattr(module, "_atomic", interrupt)
        with pytest.raises(OSError):
            module.install(repo / "scripts/hooks", repo, opt_in=True)
    before = json.loads((target / module.MANIFEST).read_text())
    assert before["phase"] == "prepared" and before["previous"] is None
    recorded = (target / module.MANIFEST).read_bytes()
    if retry == "different-target":
        wrapper = other / "scripts/hooks/pre-push"
        wrapper.write_bytes(wrapper.read_bytes() + b"\n# different target\n")
    selected = repo if retry == "existing-retry" else other
    options = [] if retry in ("without-opt-in", "existing-retry") else ["--chain-pre-push"]
    result = subprocess.run(["bash", str(selected / "scripts/hooks/sync-hooks.sh"), *options],
                            cwd=selected, env=env, capture_output=True)
    if retry in ("without-opt-in", "different-target"):
        assert result.returncode == 2
        assert (target / module.MANIFEST).read_bytes() == recorded
        assert not (target / "pre-push").exists()
        return
    assert result.returncode == 0, result.stderr
    state = json.loads((target / module.MANIFEST).read_text())
    expected = {str(repo / ".git"): str(repo)}
    if retry == "new-opt-in":
        expected[str(other / ".git")] = str(other)
    assert state["repos"] == expected
    data = b"(delete) " + b"0" * 40 + b" refs/heads/main " + b"1" * 40 + b"\n"
    for root in (repo, selected):
        run = subprocess.run([str(target / "pre-push"), "origin", "fixture"], cwd=root, env=env,
                             input=data, capture_output=True)
        assert run.returncode == 1 and b"Direct push" in run.stdout


@pytest.mark.parametrize("entry", ["direct", "sync"])
@pytest.mark.parametrize("location", [None, "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"])
def test_installation_refuses_unrelated_repo_despite_inherited_location(world, entry, location):
    repo, env, git, sync = world
    other = repo.parent / "unrelated"
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(other)], env=env, check=True)
    source = repo / "scripts/hooks"
    command = (["/usr/bin/python3", str(source / "pre_push_chain.py"), "--install", "--source", str(source), "--repo", str(other)]
               if entry == "direct" else ["bash", str(source / "sync-hooks.sh"), "--hook-repo", str(other)])
    if location:
        env[location] = str(other if location == "GIT_WORK_TREE" else other / ".git")
    result = subprocess.run(command, cwd=repo, env=env, capture_output=True)
    assert result.returncode == 2, result.stderr
    for root in (repo, other):
        hooks = root / ".git/hooks"
        assert not hooks.exists() or not list(hooks.iterdir())


@pytest.mark.parametrize("entry", ["direct", "sync"])
@pytest.mark.parametrize("location", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_installation_clears_location_but_preserves_config(world, entry, location):
    repo, env, git, sync = world
    other = repo.parent / "unrelated"
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(other)], env=env, check=True)
    target = repo / "configured-hooks"
    env.update({location: str(other / ".git"), "GIT_WORK_TREE": str(other),
                "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": str(target),
                "GIT_CONFIG_KEY_1": "safe.directory", "GIT_CONFIG_VALUE_1": str(repo)})
    source = repo / "scripts/hooks"
    command = (["/usr/bin/python3", str(source / "pre_push_chain.py"), "--install", "--source", str(source), "--repo", str(repo)]
               if entry == "direct" else ["bash", str(source / "sync-hooks.sh")])
    result = subprocess.run(command, cwd=repo, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (target / ".genesis-pre-push.json").is_file()
    state = json.loads((target / ".genesis-pre-push.json").read_text())
    assert state["repos"] == {str(repo / ".git"): str(repo)}
    assert (target / "pre-push").is_file()
    assert not (other / ".git/hooks").exists()


def test_native_invocation_preserves_git_location_from_unrelated_cwd(world):
    repo, env, git, sync = world
    target = repo / "custom-hooks"
    target.mkdir()
    git("config", "core.hooksPath", str(target))
    hook = target / "pre-push"
    hook.write_text('#!/bin/sh\nprintf "%s\\n%s\\n" "$GIT_DIR" "$GIT_WORK_TREE"\ncat\n')
    hook.chmod(0o751)
    assert sync("--chain-pre-push").returncode == 0
    elsewhere = repo.parent / "elsewhere"
    elsewhere.mkdir()
    env.update(GIT_DIR=str(repo / ".git"), GIT_WORK_TREE=str(repo))
    data = b"(delete) " + b"0" * 40 + b" refs/heads/main " + b"1" * 40 + b"\n"
    result = subprocess.run([str(hook), "origin", "fixture"], cwd=elsewhere, env=env, input=data, capture_output=True)
    assert result.returncode == 1 and b"Direct push" in result.stdout
    assert result.stdout.startswith((str(repo / ".git") + "\n" + str(repo) + "\n").encode() + data)
