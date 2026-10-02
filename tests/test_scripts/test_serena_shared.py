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
        (path / ".git").mkdir()
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


def test_configure_rejects_linked_worktree(tmp_path):
    branch = checkout(tmp_path / "branch", linked=True)
    with pytest.raises(ValueError, match="canonical main"):
        shared.configure(branch, True)


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
