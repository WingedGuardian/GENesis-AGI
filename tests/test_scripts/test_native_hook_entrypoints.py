"""Run only the shipped hook-install stage, never the full system installer."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("entry", ["install", "bootstrap"])
@pytest.mark.parametrize("profile", ["fresh", "nonexecutable_sync", "missing_sync", "custom", "no_hooks_dir", "separate_gitdir"])
def test_install_stage_uses_complete_owned_chain(tmp_path, entry, profile):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1"}
    repo = tmp_path / "repo"
    args = ["git", "init", "-q"]
    if profile == "separate_gitdir":
        args += ["--separate-git-dir", str(tmp_path / "gitdir")]
    subprocess.run([*args, str(repo)], env=env, check=True)
    source = repo / "scripts/hooks"
    shutil.copytree(ROOT / "scripts/hooks", source)
    common = tmp_path / "gitdir" if profile == "separate_gitdir" else repo / ".git"
    hooks = common / "hooks"
    if profile == "no_hooks_dir":
        shutil.rmtree(hooks)
    if profile == "nonexecutable_sync":
        (source / "sync-hooks.sh").chmod(0o644)
    if profile == "missing_sync":
        (source / "sync-hooks.sh").unlink()
    custom = b"#!/bin/sh\n# owned custom hook\nexit 0\n"
    if profile == "custom":
        (hooks / "pre-push").write_bytes(custom)
        (hooks / "pre-push").chmod(0o755)
    text = (ROOT / f"scripts/{entry}.sh").read_text()
    variable = "REPO_DIR" if entry == "install" else "GENESIS_ROOT"
    end = "#  Step 2 — Directories" if entry == "install" else "# --- MCP Server Registration"
    stage = text[text.index(f'HOOKS_SRC="${variable}/scripts/hooks"'):text.index(end)]
    env[variable] = str(repo)
    result = subprocess.run(["bash", "-c", stage], cwd=repo, env=env, capture_output=True, text=True)
    manifest = hooks / ".genesis-pre-push.json"
    if profile in ("custom", "missing_sync"):
        assert "WARNING" in result.stdout + result.stderr
        assert "hooks synced" not in result.stdout
        assert not manifest.exists()
        if profile == "custom":
            assert (hooks / "pre-push").read_bytes() == custom
            assert (hooks / "pre-merge-commit").exists()
        else:
            assert not (hooks / "pre-push").exists()
        return
    assert result.returncode == 0, result.stderr
    assert manifest.exists(), result.stdout + result.stderr
    for name in ("pre-push", "pre_push_chain.py", "pre_push_gates.sh", "pre-commit", "pre-merge-commit"):
        assert (hooks / name).read_bytes() == (source / name).read_bytes()
    repeat = subprocess.run(["bash", "-c", stage], cwd=repo, env=env, capture_output=True, text=True)
    assert repeat.returncode == 0, repeat.stderr


@pytest.mark.parametrize("script", ["spike_upgrade_test.sh", "spike_caveat_fixes_test.sh"])
def test_manual_sync_fixture_includes_native_closure(tmp_path, script):
    home = tmp_path / "home"
    home.mkdir()
    hooks = ROOT / "scripts/hooks"
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "WORKTREE_ROOT": str(ROOT), "TEST_ROOT": str(tmp_path),
           "SRC_POST_COMMIT": str(hooks / "post-commit"),
           "SRC_AUDIT_HELPER": str(hooks / "emit_bugfix_audit.py"),
           "SRC_ADMISSION_SHIM": str(hooks / "db_admission_check.py")}
    text = (ROOT / "scripts" / script).read_text()
    if script == "spike_upgrade_test.sh":
        stage = text[text.index("_make_fake_genesis_repo() {"):text.index("# =========================================================================")]
        stage += '\n_make_fake_genesis_repo "$TEST_ROOT/repo"\n'
        repo = tmp_path / "repo"
    else:
        stage = text[text.index('REPO_A="$TEST_ROOT/scenario-a"'):text.index("# Directly invoke")]
        repo = tmp_path / "scenario-a"
    subprocess.run(["bash", "-c", stage], env=env, check=True, capture_output=True)
    result = subprocess.run(["bash", str(repo / "scripts/hooks/sync-hooks.sh"), "--quiet"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (repo / ".git/hooks/.genesis-pre-push.json").exists()
    assert (repo / ".git/hooks/post-commit").exists()
