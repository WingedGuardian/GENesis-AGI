"""Routing and recovery contracts for the native shared Serena boundary."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/serena_shared.py"
spec = importlib.util.spec_from_file_location("serena_shared", SCRIPT)
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)


def checkout(path, linked=False):
    path.mkdir()
    if linked:
        (path / ".git").write_text("gitdir: ../main/.git/worktrees/branch\n")
    else:
        subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    return path.resolve()


def test_nearest_worktree_wins_over_parent_project(tmp_path):
    main = checkout(tmp_path / "main")
    branch = checkout(main / "branch", linked=True)
    child = branch / "src"
    child.mkdir()
    assert shared.project_root(child) == branch


def test_canonical_symlink_resolves_to_same_checkout(tmp_path):
    main = checkout(tmp_path / "main")
    alias = tmp_path / "alias"
    alias.symlink_to(main, target_is_directory=True)
    assert shared.project_root(alias) == main


@pytest.mark.parametrize("context", shared.PROFILES)
@pytest.mark.parametrize("which", ["main", "linked", "none"])
def test_launch_routes_only_exact_main(tmp_path, monkeypatch, context, which):
    main = checkout(tmp_path / "main")
    linked = checkout(main / "linked", linked=True)
    monkeypatch.setattr(shared, "read_settings", lambda _: {"main": str(main), "enabled": True})
    monkeypatch.setattr(shared, "binary", lambda name: "/bin/" + name)
    monkeypatch.setattr(shared.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))
    captured = []
    monkeypatch.setattr(shared.os, "execv", lambda executable, argv: captured.append(argv))
    project = {"main": main, "linked": linked, "none": None}[which]
    shared.launch(context, project)
    argv = captured[0]
    if which == "main":
        assert argv[0] == "/bin/terse"
        assert argv[-1] == f"http://127.0.0.1:{shared.PORTS[context]}/mcp"
    else:
        assert argv[:4] == ["/bin/serena", "start-mcp-server", "--context", context]
        assert argv[4:] == (["--project", str(linked)] if project else ["--project-from-cwd"])


def test_failed_shared_service_does_not_spawn_stdio_duplicate(tmp_path, monkeypatch):
    main = checkout(tmp_path / "main")
    monkeypatch.setattr(shared, "read_settings", lambda _: {"main": str(main), "enabled": True})
    monkeypatch.setattr(shared.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=3))
    with pytest.raises(ValueError, match="unavailable"):
        shared.launch("claude-code", main)


@pytest.mark.parametrize(
    "content",
    ["null", '{"enabled": 1, "main": "/repo"}', '{"enabled": true, "main": "relative"}', "{"],
)
def test_malformed_settings_refuse(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        shared.read_settings(path)


def test_missing_settings_keep_native_mode(tmp_path):
    assert shared.read_settings(tmp_path / "absent") is None


def test_failed_settings_publication_preserves_previous_and_cleans_temp(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("previous")

    def fail_replace(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="publication failed"):
        shared.write_settings(settings, tmp_path, True)
    assert settings.read_text() == "previous"
    assert list(tmp_path.iterdir()) == [settings]


def test_disable_works_without_provider_or_proxy(tmp_path, monkeypatch):
    main = checkout(tmp_path / "main")
    settings = tmp_path / "config/settings.json"
    monkeypatch.setattr(shared, "settings_path", lambda: settings)
    calls = []
    monkeypatch.setattr(shared, "systemctl", lambda *args: calls.append(args))
    shared.configure(main, False)
    assert json.loads(settings.read_text()) == {"enabled": False, "main": str(main)}
    assert calls == [
        ("disable", "--now", "genesis-serena-claude-code.service", "genesis-serena-codex.service")
    ]


def test_unit_preserves_literal_path_and_resource_ownership(tmp_path):
    project = tmp_path / 'work % $name "quoted"'
    text = shared.render_unit(project, "codex", tmp_path / "provider", "/bin/serena", "/bin")
    assert 'work %% $name \\"quoted\\"' in text
    assert "ExecStart=:" in text
    assert f"WorkingDirectory={str(project).replace('%', '%%')}\n" in text
    assert "MemoryMax=4G" in text and "MemorySwapMax=0" in text
    assert "KillMode=control-group" in text and "StartLimitBurst=3" in text


def test_provider_upgrade_requires_revalidation(tmp_path, monkeypatch):
    monkeypatch.setattr(shared.subprocess, "check_output", lambda *a, **k: "Serena 2.0.0\n")
    with pytest.raises(ValueError, match="revalidate"):
        shared.serve(tmp_path, "codex", "/bin/serena")


def test_custom_resources_remain_in_original_store(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "custom.yml").write_text("customized: true\n")
    destination = tmp_path / "snapshot/modes"
    shared.link_resource(destination, source)
    shared.link_resource(destination, source)
    assert (destination / "custom.yml").read_text() == "customized: true\n"
    (destination / "new.yml").write_text("live: true\n")
    assert (source / "new.yml").read_text() == "live: true\n"


def test_existing_resource_data_is_never_discarded(tmp_path):
    destination = tmp_path / "global"
    destination.mkdir()
    (destination / "important.md").write_text("preserve")
    with pytest.raises(ValueError, match="preserve/reconcile"):
        shared.link_resource(destination, tmp_path / "original")
    assert (destination / "important.md").read_text() == "preserve"


def test_readiness_accepts_only_provider_owned_loopback_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert shared.owns_listener(os.getpid(), port)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            assert not shared.owns_listener(child.pid, port)
        finally:
            child.terminate()
            child.wait()


def test_foreign_active_listener_refuses_before_config_mutation(tmp_path, monkeypatch):
    main = checkout(tmp_path / "main")
    settings = tmp_path / "settings.json"
    settings.write_text("original")
    monkeypatch.setattr(shared, "settings_path", lambda: settings)
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        monkeypatch.setattr(shared, "PORTS", {"claude-code": listener.getsockname()[1]})
        with pytest.raises(ValueError, match="foreign listener"):
            shared.configure(main, True)
    assert settings.read_text() == "original"


def test_service_startup_refuses_unsafe_exposed_capabilities(monkeypatch):
    monkeypatch.setenv("MAINPID", "123")
    monkeypatch.setattr(shared, "owns_listener", lambda *args: True)

    def unsafe(*args):
        raise subprocess.CalledProcessError(1, "native capability check")

    monkeypatch.setattr(shared, "check_capabilities", unsafe)
    with pytest.raises(subprocess.CalledProcessError):
        shared.ready(9165, "/bin/serena")


@pytest.mark.parametrize("path", ["line\nbreak", "line\rbreak", "nul\x00byte"])
def test_unit_line_injection_refuses(path):
    with pytest.raises(ValueError):
        shared.quote_unit(path)


@pytest.mark.parametrize("enabled", [True, False, "invalid", None])
def test_installer_preserves_provider_when_sharing_enabled_or_unknown(tmp_path, enabled):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "serena").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "uv").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    for executable in bindir.iterdir():
        executable.chmod(0o755)
    settings = tmp_path / "config/serena-shared.json"
    settings.parent.mkdir()
    if enabled is not None:
        settings.write_text(json.dumps({"enabled": enabled}))
    calls = tmp_path / "calls"
    env = dict(
        os.environ, GENESIS_HOME=str(tmp_path), PATH=f"{bindir}:{os.defpath}", CALLS=str(calls)
    )
    helper = SCRIPT.parent / "lib/serena_install.sh"
    subprocess.run(
        ["bash", "-c", 'source "$1"; _install_serena', "bash", str(helper)], env=env, check=True
    )
    assert calls.exists() == (enabled is False or enabled is None)
    if calls.exists():
        assert calls.read_text() == "tool upgrade serena-agent\n"


@pytest.mark.parametrize("custom,fail_publication", [(False, False), (True, False), (False, True)])
def test_legacy_project_registration_migrates_without_touching_custom_entry(
    tmp_path, custom, fail_publication
):
    root = checkout(tmp_path / "project")
    entry = {
        "command": "serena",
        "args": ["start-mcp-server", "--context", "claude-code", "--project", str(root)],
    }
    if custom:
        entry["env"] = {"CUSTOM": "preserve"}
    other = {"command": "/operator/other", "args": ["preserve"]}
    config = root / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"serena": entry, "other": other}}))
    config.chmod(0o640)
    user_config = {"mcpServers": {"serena": {"command": "/operator/serena"}}}
    (tmp_path / ".claude.json").write_text(json.dumps(user_config))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    cli = bindir / "claude"
    cli.write_text(
        '#!/bin/sh\nif [ "$2" = list ]; then echo "serena: existing"; else echo mutation >> "$CALLS"; fi\n'
    )
    cli.chmod(0o755)
    python = bindir / "python3"
    python.write_text("""#!/usr/bin/python3
import os, sys
sys.argv = sys.argv[1:]
if os.environ['FAIL_PUBLICATION'] == '1':
    def fail(*args): raise OSError('publication failed')
    os.replace = fail
exec(compile(sys.stdin.read(), '<registration helper>', 'exec'))
""")
    python.chmod(0o755)
    helper = SCRIPT.parent / "lib/mcp_register.sh"
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        PATH=f"{bindir}:{os.defpath}",
        CALLS=str(calls),
        FAIL_PUBLICATION=str(int(fail_publication)),
    )
    subprocess.run(
        ["bash", "-e", "-c", 'source "$1"; _register_serena "$2"', "bash", str(helper), str(root)],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    assert not calls.exists()
    actual = json.loads(config.read_text())["mcpServers"]
    assert json.loads((tmp_path / ".claude.json").read_text()) == user_config
    assert actual["other"] == other
    assert actual["serena"] == (
        entry
        if custom or fail_publication
        else {"command": str(root / ".claude/mcp/run-serena"), "args": ["--context", "claude-code"]}
    )
    assert config.stat().st_mode & 0o777 == 0o640
    assert list(root.glob(".mcp.json.*")) == []


def test_reconfigure_removes_deleted_native_settings_snapshot(tmp_path, monkeypatch):
    main = checkout(tmp_path / "main")
    source = tmp_path / "original"
    source.mkdir()
    native = source / "serena_config.yml"
    native.write_text("custom_setting: true\n")
    settings = tmp_path / "state/config/serena-shared.json"
    monkeypatch.setattr(shared, "settings_path", lambda: settings)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SERENA_HOME", str(source))
    monkeypatch.setattr(shared, "PORTS", dict.fromkeys(shared.PROFILES, 0))
    monkeypatch.setattr(shared, "binary", lambda name: "/bin/" + name)
    monkeypatch.setattr(shared, "snapshot_context", lambda *args: None)
    monkeypatch.setattr(shared, "systemctl", lambda *args: None)
    shared.configure(main, True)
    snapshots = [
        settings.parent.parent / "serena-shared" / profile / "serena_config.yml"
        for profile in shared.PROFILES
    ]
    assert all(p.read_text() == native.read_text() for p in snapshots)
    native.unlink()
    shared.configure(main, True)
    assert all(not p.exists() for p in snapshots)


@pytest.mark.parametrize("filename", ["bootstrap.sh", "install.sh"])
def test_both_install_paths_use_shared_upgrade_and_migration_helpers(filename):
    source = (SCRIPT.parent / filename).read_text()
    assert '. "$SCRIPT_DIR/lib/serena_install.sh"' in source
    assert "_install_serena" in source
    assert "_register_serena" in source
    assert "uv tool upgrade serena-agent" not in source


@pytest.fixture
def configured_paths(tmp_path, monkeypatch):
    settings = tmp_path / "state/config/serena-shared.json"
    monkeypatch.setattr(shared, "settings_path", lambda: settings)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SERENA_HOME", str(tmp_path / "provider"))
    monkeypatch.setattr(shared, "PORTS", dict.fromkeys(shared.PROFILES, 0))
    monkeypatch.setattr(shared, "binary", lambda name: "/bin/" + name)
    monkeypatch.setattr(shared, "snapshot_context", lambda *args: None)
    monkeypatch.setattr(shared, "systemctl", lambda *args: None)
    return settings


@pytest.mark.parametrize("failure", ["startup", "publication"])
def test_failed_checkout_transition_never_routes_old_clients_to_new_tree(
    tmp_path, monkeypatch, configured_paths, failure
):
    old = checkout(tmp_path / "old")
    new = checkout(tmp_path / "new")
    shared.write_settings(configured_paths, old, True)
    original_write = shared.write_settings

    def write_settings(path, project, enabled):
        if failure == "publication" and enabled:
            raise OSError("publication failed")
        original_write(path, project, enabled)

    def systemctl(*args):
        if failure == "startup" and args[0] == "enable":
            raise subprocess.CalledProcessError(1, "enable services")

    monkeypatch.setattr(shared, "write_settings", write_settings)
    monkeypatch.setattr(shared, "systemctl", systemctl)
    with pytest.raises((OSError, subprocess.CalledProcessError)):
        shared.configure(new, True)
    assert shared.read_settings(configured_paths) == {"main": str(new), "enabled": False}
    captured = []
    monkeypatch.setattr(shared.os, "execv", lambda executable, argv: captured.append(argv))
    shared.launch("claude-code", old)
    assert captured[0][0] == "/bin/serena"
    assert captured[0][-2:] == ["--project", str(old)]


@pytest.mark.parametrize("suffix", [" ", "\t"])
def test_trailing_checkout_whitespace_rejected_before_mutation(tmp_path, configured_paths, suffix):
    main = checkout(tmp_path / ("main" + suffix))
    with pytest.raises(ValueError, match="end in whitespace"):
        shared.configure(main, True)
    assert not configured_paths.exists()


def test_separate_git_directory_is_a_supported_main_checkout(tmp_path, configured_paths):
    main = checkout(tmp_path / "main")
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "init",
            "--quiet",
            "--separate-git-dir",
            str(tmp_path / "metadata"),
        ],
        check=True,
    )
    assert (main / ".git").is_file()
    shared.configure(main, True)
    assert shared.read_settings(configured_paths)["enabled"] is True


def test_actual_linked_worktree_cannot_be_shared_main(tmp_path, configured_paths):
    main = checkout(tmp_path / "main")
    subprocess.run(
        [
            "git",
            "-C",
            str(main),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--quiet", "--detach", str(linked)], check=True
    )
    with pytest.raises(ValueError, match="linked worktree"):
        shared.configure(linked, True)
    assert not configured_paths.exists()


def test_disable_sharing_does_not_require_readable_git_metadata(tmp_path, configured_paths):
    main = tmp_path / "missing-checkout"
    shared.configure(main, False)
    assert shared.read_settings(configured_paths)["enabled"] is False
