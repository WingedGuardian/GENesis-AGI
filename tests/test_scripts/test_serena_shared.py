"""Routing and recovery contracts for the native shared Serena boundary."""

import fcntl
import importlib.util
import json
import os
import selectors
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


@pytest.fixture(autouse=True)
def private_user_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


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
    monkeypatch.setattr(shared, "active_project", lambda unit: main)
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
    with (shared.unit_directory() / ".genesis-serena.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


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


@pytest.mark.parametrize("home_kind", ["absolute", "empty", "unset", "tilde"])
@pytest.mark.parametrize("enabled", [True, False, "invalid", None])
def test_installer_preserves_provider_when_sharing_enabled_or_unknown(tmp_path, enabled, home_kind):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "serena").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "uv").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    (bindir / "systemctl").write_text(
        "#!/bin/sh\nprintf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n'\n"
    )
    for executable in bindir.iterdir():
        executable.chmod(0o755)
    home = tmp_path / (".genesis" if home_kind in ("empty", "unset") else "state")
    settings = home / "config/serena-shared.json"
    settings.parent.mkdir(parents=True)
    if enabled is not None:
        settings.write_text(json.dumps({"enabled": enabled}))
    calls = tmp_path / "calls"
    env = dict(os.environ, HOME=str(tmp_path), PATH=f"{bindir}:{os.defpath}", CALLS=str(calls))
    env.pop("GENESIS_HOME", None)
    if home_kind != "unset":
        env["GENESIS_HOME"] = {"absolute": str(home), "empty": "", "tilde": "~/state"}[home_kind]
    helper = SCRIPT.parent / "lib/serena_install.sh"
    subprocess.run(
        ["bash", "-c", 'source "$1"; _install_serena', "bash", str(helper)], env=env, check=True
    )
    assert calls.exists() == (enabled is False or enabled is None)
    if calls.exists():
        assert calls.read_text() == "tool upgrade serena-agent\n"


@pytest.mark.parametrize(
    "project_path", ["canonical", "symlink", "other", "moved", "deleted", "current"]
)
@pytest.mark.parametrize("custom,fail_publication", [(False, False), (True, False), (False, True)])
def test_legacy_project_registration_migrates_without_touching_custom_entry(
    tmp_path, custom, fail_publication, project_path
):
    root = checkout(tmp_path / "project")
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    stored_project = {"canonical": root, "symlink": alias, "other": tmp_path / "other"}[
        project_path if project_path in ("canonical", "symlink", "other") else "canonical"
    ]
    entry = {
        "command": "serena",
        "args": ["start-mcp-server", "--context", "claude-code", "--project", str(stored_project)],
    }
    if project_path in ("moved", "deleted", "current"):
        old = (
            root / ".claude/mcp/run-serena"
            if project_path == "current"
            else tmp_path / "old/.claude/mcp/run-serena"
        )
        if project_path == "moved":
            old.parent.mkdir(parents=True)
            old.write_text("operator checkout launcher")
        entry = {"command": str(old), "args": ["--context", "claude-code"]}
    if custom:
        entry["env"] = {"CUSTOM": "preserve"}
    other = {"command": "/operator/other", "args": ["preserve"]}
    config = root / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"serena": entry, "other": other}}))
    config.chmod(0o640)
    original = config.read_bytes()
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
        if custom or fail_publication or project_path == "other"
        else {"command": str(root / ".claude/mcp/run-serena"), "args": ["--context", "claude-code"]}
    )
    if custom or fail_publication or project_path in ("other", "current"):
        assert config.read_bytes() == original
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
    monkeypatch.setattr(shared, "validate_version", lambda *args: None)
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
    monkeypatch.setattr(shared, "validate_version", lambda *args: None)
    monkeypatch.setattr(shared, "snapshot_context", lambda *args: None)
    monkeypatch.setattr(shared, "systemctl", lambda *args: None)
    return settings


@pytest.mark.parametrize("failure", ["snapshot", "startup", "publication"])
def test_failed_checkout_transition_never_routes_old_clients_to_new_tree(
    tmp_path, monkeypatch, configured_paths, failure
):
    old = checkout(tmp_path / "old")
    new = checkout(tmp_path / "new")
    shared.write_settings(configured_paths, old, True)
    original_write = shared.write_settings
    unit_calls = []

    def write_settings(path, project, enabled):
        if failure == "publication" and enabled:
            raise OSError("publication failed")
        original_write(path, project, enabled)

    def systemctl(*args):
        unit_calls.append(args)
        if failure == "startup" and args[0] == "enable":
            raise subprocess.CalledProcessError(1, "enable services")

    if failure == "snapshot":

        def fail_snapshot(*args):
            raise OSError("snapshot failed")

        monkeypatch.setattr(shared, "install_units", fail_snapshot)
    monkeypatch.setattr(shared, "write_settings", write_settings)
    monkeypatch.setattr(shared, "systemctl", systemctl)
    with pytest.raises((OSError, subprocess.CalledProcessError)):
        shared.configure(new, True)
    assert shared.read_settings(configured_paths) == {"main": str(new), "enabled": False}
    assert unit_calls[-1] == (
        "disable",
        "--now",
        "genesis-serena-claude-code.service",
        "genesis-serena-codex.service",
    )
    captured = []
    monkeypatch.setattr(shared.os, "execv", lambda executable, argv: captured.append(argv))
    shared.launch("claude-code", old)
    assert captured[0][0] == "/bin/serena"
    assert captured[0][-2:] == ["--project", str(old)]


@pytest.mark.parametrize("suffix", [" ", "\t", "\\"])
def test_unsupported_checkout_path_ending_rejected_before_mutation(
    tmp_path, configured_paths, suffix
):
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


@pytest.mark.parametrize("override", [None, "", "~/state", "absolute"])
def test_settings_home_matches_canonical_env_semantics(tmp_path, monkeypatch, override):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GENESIS_HOME", raising=False)
    if override is not None:
        monkeypatch.setenv(
            "GENESIS_HOME", str(tmp_path / "state") if override == "absolute" else override
        )
    expected = tmp_path / ("state" if override else ".genesis") / "config/serena-shared.json"
    assert shared.settings_path() == expected


@pytest.mark.parametrize("existing_config", [False, True])
def test_missing_project_entry_registered_despite_user_scope_server(tmp_path, existing_config):
    root = checkout(tmp_path / "project")
    config = root / ".mcp.json"
    unrelated = {"mcpServers": {"other": {"command": "/operator/other"}}}
    if existing_config:
        config.write_text(json.dumps(unrelated))
    user = {"mcpServers": {"serena": {"command": "/operator/serena"}}}
    user_file = tmp_path / ".claude.json"
    user_file.write_text(json.dumps(user))
    helper = SCRIPT.parent / "lib/mcp_register.sh"
    subprocess.run(
        ["bash", "-e", "-c", 'source "$1"; _register_serena "$2"', "bash", str(helper), str(root)],
        env=dict(os.environ, HOME=str(tmp_path), PATH=os.defpath),
        check=True,
    )
    result = json.loads(config.read_text())
    assert result["mcpServers"].pop("serena") == {
        "command": str(root / ".claude/mcp/run-serena"),
        "args": ["--context", "claude-code"],
    }
    assert result == (unrelated if existing_config else {"mcpServers": {}})
    assert json.loads(user_file.read_text()) == user


@pytest.mark.parametrize("path", ["direct", "host"])
def test_uninstall_stops_and_disables_both_shared_services(tmp_path, path):
    source = (SCRIPT.parent / "uninstall.sh").read_text()
    if path == "direct":
        helper = source[
            source.index("safe_disable_service() {") : source.index("# Run a command inside")
        ]
        start = source.index("        PRESSURE_UNIT=genesis-disk-hygiene-pressure")
        commands = source[start : source.index("        # Persistent= timers", start)]
    else:
        helper = 'container_exec() { bash -c "$1"; }\n'
        start = source.index(
            '            container_exec "', source.index("# Stop all services (timers first")
        )
        commands = source[start : source.index('            ok "Stopped Genesis services"', start)]
    calls = tmp_path / "calls"
    executable = tmp_path / "systemctl"
    executable.write_text('#!/bin/sh\nprintf "%s\n" "$*" >> "$CALLS"\n')
    executable.chmod(0o755)
    subprocess.run(
        ["bash", "-e", "-c", "DRY_RUN=false; ok() { :; }; skip() { :; };\n" + helper + commands],
        env=dict(os.environ, CALLS=str(calls), PATH=f"{tmp_path}:{os.defpath}"),
        check=True,
    )
    recorded = [line.split() for line in calls.read_text().splitlines()]
    for context in shared.PROFILES:
        unit = shared.unit_name(context)
        for operation in ("stop", "disable"):
            assert any(args[:2] == ["--user", operation] and unit in args[2:] for args in recorded)


@pytest.mark.parametrize("locked", [False, True])
@pytest.mark.parametrize("first_enable", [False, True])
@pytest.mark.parametrize("second_enable", [False, True])
@pytest.mark.parametrize("first_fails", [False, True])
def test_configuration_process_lock_covers_all_transitions_and_releases_on_error(
    tmp_path, first_enable, second_enable, first_fails, locked
):
    code = """import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('shared', sys.argv[1])
shared=importlib.util.module_from_spec(spec);spec.loader.exec_module(shared)
native_lock=shared.fcntl.flock
def lock(fd, operation):
    print('locking', flush=True)
    if sys.argv[5] == 'True': native_lock(fd, operation)
shared.fcntl.flock=lock
def action(project, enabled):
    print(str(project), flush=True)
    sys.stdin.buffer.read(1)
    if sys.argv[4] == 'True': raise OSError('owned failure')
shared.configure_locked=action
shared.configure(Path(sys.argv[2]), sys.argv[3] == 'True')
"""
    clients = []
    with selectors.DefaultSelector() as ready:
        try:
            for number, enable, fails in [
                (1, first_enable, first_fails),
                (2, second_enable, False),
            ]:
                client = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        code,
                        str(SCRIPT),
                        str(number),
                        str(enable),
                        str(fails),
                        str(locked),
                    ],
                    env=dict(os.environ, GENESIS_HOME=str(tmp_path / str(number))),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=0,
                )
                clients.append(client)
                ready.register(client.stdout, selectors.EVENT_READ)
                assert ready.select(5)
                assert client.stdout.readline().strip() == b"locking"
                if number == 1:
                    assert ready.select(5)
                    assert client.stdout.readline().strip() == b"1"
                    ready.unregister(client.stdout)
                elif locked:
                    assert client.wait(timeout=5) == 1
                    assert b"configuration busy" in client.stderr.read()
                else:
                    assert ready.select(5), "no-op control did not enter the overlapping transition"
                    assert client.stdout.readline().strip() == b"2"
            clients[0].stdin.write(b"x")
            clients[0].stdin.flush()
            assert clients[0].wait(timeout=5) == int(first_fails)
            if not locked:
                clients[1].stdin.write(b"x")
                clients[1].stdin.flush()
                assert clients[1].wait(timeout=5) == 0
        finally:
            for client in clients:
                if client.poll() is None:
                    client.kill()
                client.communicate(timeout=5)


@pytest.mark.parametrize("context", shared.PROFILES)
def test_stale_configuration_root_refuses_service_for_another_checkout(
    tmp_path, monkeypatch, context
):
    first = checkout(tmp_path / "first")
    second = checkout(tmp_path / "second")
    state = tmp_path / "state-first"
    shared.write_settings(state / "config/serena-shared.json", first, True)
    shared.write_settings(tmp_path / "state-second/config/serena-shared.json", second, True)
    monkeypatch.setenv("GENESIS_HOME", str(state))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=second)
    monkeypatch.setattr(
        shared.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(shared.subprocess, "check_output", lambda *args, **kwargs: str(child.pid))
    try:
        with pytest.raises(ValueError, match="different checkout"):
            shared.launch(context, first)
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_unsupported_provider_refuses_before_configuration_changes(tmp_path, monkeypatch):
    main = checkout(tmp_path / "main")
    settings = tmp_path / "config/serena-shared.json"
    shared.write_settings(settings, main, True)
    original = settings.read_bytes()
    monkeypatch.setattr(shared, "PORTS", dict.fromkeys(shared.PROFILES, 0))
    monkeypatch.setattr(shared, "settings_path", lambda: settings)
    monkeypatch.setattr(shared, "binary", lambda name: "/bin/" + name)
    native_output = shared.subprocess.check_output

    def output(command, **kwargs):
        return (
            "Serena 2.0.0\n"
            if command == ["/bin/serena", "--version"]
            else native_output(command, **kwargs)
        )

    monkeypatch.setattr(shared.subprocess, "check_output", output)
    with pytest.raises(ValueError, match="revalidate"):
        shared.configure(main, True)
    assert settings.read_bytes() == original
    assert not list(shared.unit_directory().glob("*.service"))


def test_installer_skips_active_configuration_without_waiting_or_mutating_provider(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    scripts = {
        "serena": "#!/bin/sh\nexit 0\n",
        "uv": '#!/bin/sh\necho upgrade >> "$CALLS"\n',
        "flock": '#!/bin/sh\necho locking\nexec /usr/bin/flock "$@"\n',
    }
    for name, content in scripts.items():
        executable = bindir / name
        executable.write_text(content)
        executable.chmod(0o755)
    env = dict(
        os.environ,
        GENESIS_HOME=str(tmp_path / "state"),
        PATH=f"{bindir}:{os.defpath}",
        CALLS=str(calls),
    )
    code = """import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('shared',sys.argv[1])
shared=importlib.util.module_from_spec(spec);spec.loader.exec_module(shared)
def action(project,enabled):
    print('entered',flush=True);sys.stdin.buffer.read(1)
    shared.write_settings(shared.settings_path(),project,True)
shared.configure_locked=action
shared.configure(Path('/main'),True)
"""
    clients = []
    with selectors.DefaultSelector() as ready:
        try:
            first = subprocess.Popen(
                [sys.executable, "-c", code, str(SCRIPT)],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            clients.append(first)
            ready.register(first.stdout, selectors.EVENT_READ)
            assert ready.select(5) and first.stdout.readline().strip() == b"entered"
            ready.unregister(first.stdout)
            second = subprocess.Popen(
                [
                    "bash",
                    "-c",
                    'source "$1"; _install_serena',
                    "bash",
                    str(SCRIPT.parent / "lib/serena_install.sh"),
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            clients.append(second)
            ready.register(second.stdout, selectors.EVENT_READ)
            assert ready.select(5) and second.stdout.readline().strip() == b"locking"
            stdout, stderr = second.communicate(timeout=5)
            assert second.returncode == 0 and b"clients or configuration active" in stdout, stderr
            assert not calls.exists()
            first.stdin.write(b"x")
            first.stdin.flush()
            assert first.wait(timeout=5) == 0
            assert not calls.exists()
        finally:
            for client in clients:
                if client.poll() is None:
                    client.kill()
                client.communicate(timeout=5)


@pytest.mark.parametrize("context", shared.PROFILES)
@pytest.mark.parametrize(
    "state,upgrade",
    [
        ("LoadState=not-found\nActiveState=inactive\nUnitFileState=\n", True),
        ("LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n", True),
        ("LoadState=loaded\nActiveState=failed\nUnitFileState=disabled\n", True),
        ("LoadState=masked\nActiveState=inactive\nUnitFileState=masked\n", True),
        ("LoadState=loaded\nActiveState=active\nUnitFileState=disabled\n", False),
        ("LoadState=loaded\nActiveState=activating\nUnitFileState=disabled\n", False),
        ("LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled\n", False),
        ("LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled-runtime\n", False),
        ("LoadState=masked\nActiveState=inactive\nUnitFileState=masked-runtime\n", False),
        ("LoadState=loaded\nActiveState=inactive\nUnitFileState=static\n", False),
        ("unreadable", False),
        ("malformed", False),
    ],
)
def test_global_provider_upgrade_requires_both_services_stopped_and_disabled(
    tmp_path, context, state, upgrade
):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    scripts = {
        "serena": "#!/bin/sh\nexit 0\n",
        "uv": '#!/bin/sh\necho upgrade >> "$CALLS"\n',
        "systemctl": (
            '#!/bin/sh\nif [ "$3" = "$SELECTED" ]; then\n'
            '  [ "$STATE" != unreadable ] || exit 1\n  printf "%s" "$STATE"\n'
            "else\n  printf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n'\nfi\n"
        ),
    }
    for name, content in scripts.items():
        executable = bindir / name
        executable.write_text(content)
        executable.chmod(0o755)
    env = dict(
        os.environ,
        GENESIS_HOME=str(tmp_path / "other-config-root"),
        PATH=f"{bindir}:{os.defpath}",
        CALLS=str(calls),
        SELECTED=shared.unit_name(context),
        STATE=state,
    )
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; _install_serena',
            "bash",
            str(SCRIPT.parent / "lib/serena_install.sh"),
        ],
        env=env,
        check=True,
    )
    assert calls.exists() is upgrade


@pytest.mark.parametrize("context", shared.PROFILES)
def test_configure_then_installer_in_another_root_preserves_global_provider(
    tmp_path, configured_paths, context
):
    shared.configure(checkout(tmp_path / "main"), True)
    assert shared.read_settings(configured_paths)["enabled"] is True
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls"
    scripts = {
        "serena": "#!/bin/sh\nexit 0\n",
        "uv": '#!/bin/sh\necho upgrade >> "$CALLS"\n',
        "systemctl": (
            '#!/bin/sh\nif [ "$3" = "$SELECTED" ]; then\n'
            "printf 'LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled\\n'\n"
            "else\nprintf 'LoadState=not-found\\nActiveState=inactive\\nUnitFileState=\\n'\nfi\n"
        ),
    }
    for name, content in scripts.items():
        executable = bindir / name
        executable.write_text(content)
        executable.chmod(0o755)
    subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; _install_serena',
            "bash",
            str(SCRIPT.parent / "lib/serena_install.sh"),
        ],
        env=dict(
            os.environ,
            GENESIS_HOME=str(tmp_path / "root-b"),
            PATH=f"{bindir}:{os.defpath}",
            CALLS=str(calls),
            SELECTED=shared.unit_name(context),
        ),
        check=True,
    )
    assert not calls.exists()
    assert shared.read_settings(configured_paths)["enabled"] is True


@pytest.mark.parametrize("context", shared.PROFILES)
@pytest.mark.parametrize("use_shared", [False, True])
def test_launcher_binds_checkout_through_proxy_exec_and_releases_native_mode(
    tmp_path, context, use_shared
):
    code = """import importlib.util, sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('shared',sys.argv[1])
shared=importlib.util.module_from_spec(spec);spec.loader.exec_module(shared)
def command(context,project):
    print('validated',flush=True);sys.stdin.buffer.read(1)
    return [sys.executable,'-c',"import sys;print('proxy',flush=True);sys.stdin.buffer.read(1)"],sys.argv[3]=='True'
shared.launch_command=command
shared.launch(sys.argv[2],Path('/main'))
"""
    client = subprocess.Popen(
        [sys.executable, "-c", code, str(SCRIPT), context, str(use_shared)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    lock_path = shared.unit_directory() / ".genesis-serena.lock"
    try:
        with selectors.DefaultSelector() as ready:
            ready.register(client.stdout, selectors.EVENT_READ)
            assert ready.select(5) and client.stdout.readline().strip() == b"validated"
            with lock_path.open("a") as lock:
                # Even before exec, a replacement cannot pass the checked identity.
                with pytest.raises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                client.stdin.write(b"x")
                client.stdin.flush()
                assert ready.select(5) and client.stdout.readline().strip() == b"proxy"
                if use_shared:
                    with pytest.raises(BlockingIOError):
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                client.stdin.write(b"x")
                client.stdin.flush()
                assert client.wait(timeout=5) == 0
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if client.poll() is None:
            client.kill()
        client.communicate(timeout=5)


@pytest.mark.parametrize("use_shared", [False, True])
def test_failed_exec_releases_configuration_lock(tmp_path, monkeypatch, use_shared):
    monkeypatch.setattr(shared, "launch_command", lambda *args: (["/missing"], use_shared))

    def fail_exec(*args):
        raise OSError("execution failed")

    monkeypatch.setattr(shared.os, "execv", fail_exec)
    with pytest.raises(OSError, match="execution failed"):
        shared.launch("codex", tmp_path)
    with (shared.unit_directory() / ".genesis-serena.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("resource", ["modes", "prompt_templates", "memories/global"])
def test_absent_source_resource_is_created_before_native_directory_access(tmp_path, resource):
    source = tmp_path / "source" / resource
    destination = tmp_path / "snapshot" / resource
    shared.link_resource(destination, source)
    assert destination.is_symlink() and source.is_dir()
    source.rmdir()
    shared.link_resource(destination, source)
    assert source.is_dir(), "existing dangling link must also be repaired"
    # This is the pinned provider's global-memory initialization operation.
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "first.md").write_text("shared resource")
    assert (source / "first.md").read_text() == "shared resource"


@pytest.mark.parametrize("value", [None, "", " \t ", "custom", " \tcustom \n"])
def test_resource_snapshot_matches_native_provider_home_normalization(
    tmp_path, monkeypatch, configured_paths, value
):
    source = tmp_path / ("custom" if value and value.strip() else ".serena")
    source.mkdir()
    (source / "serena_config.yml").write_text("native: true\n")
    if value is None:
        monkeypatch.delenv("SERENA_HOME", raising=False)
    else:
        monkeypatch.setenv("SERENA_HOME", value.replace("custom", str(source)))
    shared.configure(checkout(tmp_path / "main"), True)
    for context in shared.PROFILES:
        home = configured_paths.parent.parent / "serena-shared" / context
        assert (home / "serena_config.yml").read_text() == "native: true\n"
        for resource in ("modes", "prompt_templates", "memories/global"):
            assert (home / resource).resolve() == source / resource
            assert (home / resource).is_dir()
