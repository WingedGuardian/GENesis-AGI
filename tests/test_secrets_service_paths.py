"""Service preparation keeps the out-of-checkout credentials file discoverable."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from genesis import env, secrets_location
from genesis.runtime.init import secrets as startup

REPO = Path(__file__).resolve().parents[1]


def _guard(monkeypatch):
    hooks = REPO / "scripts" / "hooks"
    monkeypatch.syspath_prepend(str(hooks))
    spec = importlib.util.spec_from_file_location("_service_secrets_guard", hooks / "secrets_target.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fresh_install_keeps_the_checkout_default_until_migration(tmp_path, monkeypatch):
    # Nothing creates the home config before the migration moves the file, so
    # no install ever holds two files the services would have to choose between.
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    assert secrets_location.resolve(environ={}, home=tmp_path, repo_root=tmp_path / "repo") == (
        tmp_path / "repo" / "secrets.env"
    )
    result = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_",
         str(REPO / "scripts/lib/secrets_path.sh"), str(tmp_path / "repo")],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert Path(result.stdout.strip()) == tmp_path / "repo" / "secrets.env"


def test_guard_identifies_an_alias_of_the_new_location(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    secret = tmp_path / ".config/genesis/secrets.env"
    secret.parent.mkdir(parents=True)
    secret.write_text("SYNTHETIC=value\n")
    alias = tmp_path / "ordinary.txt"
    alias.symlink_to(secret)
    assert _guard(monkeypatch).touches_secrets(paths=[str(alias)])


def test_guard_protects_a_missing_custom_destination(tmp_path, monkeypatch):
    destination = tmp_path / "vault" / "credentials.conf"
    monkeypatch.setenv("SECRETS_PATH", str(destination))
    assert _guard(monkeypatch).touches_secrets(paths=[str(destination)])


def test_missing_secrets_queues_a_critical_startup_alert(tmp_path, monkeypatch):
    monkeypatch.setattr(env, "secrets_path", lambda: tmp_path / "absent.env")
    root = tmp_path / "queue"
    monkeypatch.setattr(env, "alert_queue_root", lambda: root)
    startup.load(SimpleNamespace())
    from genesis.guardian.alert.queue import list_queued

    entries = list_queued(root)
    assert len(entries) == 1
    assert entries[0][1]["severity"] == "critical"


def test_dashboard_can_create_at_the_new_location(tmp_path, monkeypatch):
    from genesis.dashboard.routes import secrets as route

    destination = tmp_path / ".config/genesis/secrets.env"
    monkeypatch.setattr(route, "secrets_path", lambda: destination)
    route._update_secrets_file({"SYNTHETIC_KEY": "value"})
    assert "SYNTHETIC_KEY=value" in destination.read_text()
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700


def test_units_load_only_the_checkout_file_until_migration():
    # Loading two files lets a key cleared in one survive from the other; the
    # units stay on main's single file until the migration renders them anew.
    for name in ("genesis-server", "agent-zero"):
        text = (REPO / f"scripts/systemd/{name}.service.template").read_text()
        lines = [line for line in text.splitlines() if line.startswith("EnvironmentFile=")]
        assert lines == ["EnvironmentFile=__REPO_DIR__/secrets.env"]


def test_guardian_checks_the_new_file_before_the_legacy_file(tmp_path, monkeypatch):
    from genesis.guardian import cred_integrity as ci

    monkeypatch.delenv("SECRETS_PATH", raising=False)
    new = tmp_path / ".config/genesis/secrets.env"
    old = tmp_path / "genesis/secrets.env"
    new.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    new.write_text("SYNTHETIC=value\n")
    old.write_bytes(b"\x00")
    assert ci.validate_file(ci.DEFAULT_TARGETS[0], tmp_path, None).status == "ok"


def test_guardian_passphrase_follows_the_new_file(tmp_path, monkeypatch):
    from genesis.guardian import cred_integrity as ci

    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    new = tmp_path / ".config/genesis/secrets.env"
    new.parent.mkdir(parents=True)
    new.write_text("GENESIS_BACKUP_PASSPHRASE=synthetic\n")
    assert ci.resolve_passphrase(tmp_path) == "synthetic"


@pytest.mark.parametrize("command", [
    "ls *", 'sqlite3 example.db "select * from records"', 'echo "*"',
    "cat README.md", "cat <<'EOF'\n*\nEOF",
])
def test_repo_commands_stay_quiet_after_relocation(tmp_path, monkeypatch, command):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("synthetic\n")
    secret = tmp_path / ".config/genesis/secrets.env"
    secret.parent.mkdir(parents=True)
    secret.write_text("SYNTHETIC=value\n")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    guard = _guard(monkeypatch)
    assert not guard.touches_secrets(command=command)
    assert guard.touches_secrets(command=f"cat {secret}")


def test_guard_protects_a_bare_custom_destination(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "credentials.conf"))
    monkeypatch.chdir(tmp_path)
    assert _guard(monkeypatch).touches_secrets(command="cp example credentials.conf")


def test_a_broken_new_candidate_does_not_disarm_the_legacy_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    new = tmp_path / ".config/genesis/secrets.env"
    old = tmp_path / "genesis/secrets.env"
    new.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    new.symlink_to(new)
    old.write_text("SYNTHETIC=value\n")
    alias = tmp_path / "ordinary.txt"
    alias.symlink_to(old)
    assert _guard(monkeypatch).touches_secrets(paths=[str(alias)])


def test_installer_creates_the_secrets_destination_parent(tmp_path):
    source = (REPO / "scripts/install.sh").read_text()
    block = source.split("# secrets.env\n", 1)[1].split("# Identity seed files", 1)[0]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "secrets.env.example").write_text("SYNTHETIC=value\n")
    destination = tmp_path / ".config/genesis/secrets.env"
    result = subprocess.run(
        ["bash", "-c", "set -e\n" + block],
        env={"PATH": os.environ["PATH"], "REPO_DIR": str(repo), "SECRETS_FILE": str(destination)},
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert destination.read_text() == "SYNTHETIC=value\n"
    assert destination.stat().st_mode & 0o777 == 0o600
    assert destination.parent.stat().st_mode & 0o777 == 0o700


def _home_with_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    secret = tmp_path / ".config/genesis/secrets.env"
    secret.parent.mkdir(parents=True)
    secret.write_text("SYNTHETIC=value\n")
    return secret.parent


@pytest.mark.parametrize("command", [
    "grep -r KEY ~/.config/genesis",
    "cp -r ~/.config/genesis/ /tmp/x",
    "tar czf a.tgz ~/.config/genesis",
    "rsync -a $HOME/.config/genesis backup/",
    "grep -r KEY ~/.config/genesis/.",
    'grep -r KEY "$HOME/.config/genesis"',
    'tar cz "${HOME}/.config/genesis"',
    "cp -r \"$HOME\"/.config/genesis/ /tmp/x",
])
def test_a_directory_read_of_the_credentials_folder_is_gated(tmp_path, monkeypatch, command):
    # The folder exists only to hold the file, so reading it recursively reads
    # the credentials without ever naming them.
    _home_with_secret(tmp_path, monkeypatch)
    assert _guard(monkeypatch).touches_secrets(command=command)


def test_a_grep_tool_path_at_the_credentials_folder_is_gated(tmp_path, monkeypatch):
    folder = _home_with_secret(tmp_path, monkeypatch)
    alias = tmp_path / "linked"
    alias.symlink_to(folder)
    guard = _guard(monkeypatch)
    assert guard.touches_secrets(paths=[str(folder)])
    assert guard.touches_secrets(paths=[str(alias)])


@pytest.mark.parametrize("command", [
    "ls ~/.config",
    "grep -r KEY ~/.config/other",
    "grep -r foo ~/genesis/src",
    "cat ~/.config/genesis-notes.txt",
])
def test_neighbours_of_the_credentials_folder_stay_quiet(tmp_path, monkeypatch, command):
    _home_with_secret(tmp_path, monkeypatch)
    (tmp_path / ".config/other").mkdir()
    (tmp_path / "genesis/src").mkdir(parents=True)
    assert not _guard(monkeypatch).touches_secrets(command=command)


@pytest.mark.parametrize("command", [
    "cat $HOME/.config/genesis-voice/settings.json",
    'ls "$HOME/.config/genesis-notes"',
    "echo $HOME/.config",
])
def test_variable_spellings_near_the_folder_stay_quiet(tmp_path, monkeypatch, command):
    _home_with_secret(tmp_path, monkeypatch)
    assert not _guard(monkeypatch).touches_secrets(command=command)


def test_resolved_targets_follow_a_changed_home(tmp_path, monkeypatch):
    # The guard resolves its targets once per environment (a glob walk calls it
    # per hit); a different HOME must never be served the previous answer.
    guard = _guard(monkeypatch)
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for name in ("first", "second"):
        home = tmp_path / name
        (home / ".config/genesis").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        assert guard.touches_secrets(paths=[str(home / ".config/genesis")])
        assert not guard.touches_secrets(paths=[str(tmp_path / "first" / "x")])


def test_the_guard_fallback_matches_the_resolver(tmp_path, monkeypatch):
    # The guard loads its locations from secrets_location.py; when that cannot
    # load it falls back to the same list written out, never to a crash.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    for project in (None, str(tmp_path / "wt")):
        if project:
            monkeypatch.setenv("CLAUDE_PROJECT_DIR", project)
        else:
            monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        loaded = _guard(monkeypatch)
        assert loaded._locations() is not None
        broken = _guard(monkeypatch)
        monkeypatch.setattr(broken, "_RESOLVER", tmp_path / "missing.py")
        broken._resolver_module.clear()
        assert broken._locations() is None
        assert broken._candidate_roots() == loaded._candidate_roots()
        assert broken._dedicated_dir() == loaded._dedicated_dir()


@pytest.mark.parametrize(
    "stale_source",
    [
        # The resolver as it was before the home-config location existed.
        "def resolve(*, environ, repo_root):\n    return repo_root / 'secrets.env'\n",
        # A later resolver whose helpers changed signature.
        "def home_config_location(home, extra):\n    return None\n"
        "def legacy_locations():\n    return []\n",
        # Helpers that answer with the wrong type.
        "def home_config_location(home):\n    return 'not-a-path'\n"
        "def legacy_locations(home, repo_root):\n    return []\n",
    ],
)
def test_a_skewed_resolver_never_crashes_the_guard(tmp_path, monkeypatch, stale_source):
    # A hook file copied ahead of its PR can sit next to an older resolver. The
    # guard must still gate, because a crash lets the command run.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    stale = tmp_path / "stale_secrets_location.py"
    stale.write_text(stale_source)
    guard = _guard(monkeypatch)
    monkeypatch.setattr(guard, "_RESOLVER", stale)
    guard._resolver_module.clear()
    assert guard._locations() is not None
    assert guard.touches_secrets(command="cat ~/.config/genesis/secrets.env")
    assert guard.touches_secrets(command="cat ~/genesis/secrets.env")
    assert guard.touches_secrets(command="grep -r KEY ~/.config/genesis")
    assert not guard.touches_secrets(command="echo hello")


def test_the_xdg_spelling_of_the_folder_is_gated(tmp_path, monkeypatch):
    _home_with_secret(tmp_path, monkeypatch)
    guard = _guard(monkeypatch)
    assert guard.touches_secrets(command='tar cz "${XDG_CONFIG_HOME:-$HOME/.config}/genesis"')
    assert not guard.touches_secrets(command='ls "${XDG_CONFIG_HOME:-$HOME/.config}/genesis-voice"')
    # The checkout itself, spelled through a variable, is not the folder.
    assert not guard.touches_secrets(command='cd "${HOME}/genesis" && ls')
    assert not guard.touches_secrets(command='cd "$HOME/genesis"')


@pytest.mark.parametrize(
    "command",
    [
        "grep -r KEY genesis",
        "tar -Cgenesis -cf - .",
        "cp -r genesis /tmp/x",
    ],
)
def test_a_bare_folder_operand_from_its_parent_is_gated(tmp_path, monkeypatch, command):
    folder = _home_with_secret(tmp_path, monkeypatch)
    monkeypatch.chdir(folder.parent)
    assert _guard(monkeypatch).touches_secrets(command=command)


@pytest.mark.parametrize("command", ["grep -rl KEY", "tar cf - .", "echo hello"])
def test_any_command_from_inside_the_folder_is_gated(tmp_path, monkeypatch, command):
    folder = _home_with_secret(tmp_path, monkeypatch)
    monkeypatch.chdir(folder)
    assert _guard(monkeypatch).touches_secrets(command=command)


def test_an_option_attached_folder_path_is_gated(tmp_path, monkeypatch):
    folder = _home_with_secret(tmp_path, monkeypatch)
    guard = _guard(monkeypatch)
    assert guard.touches_secrets(command=f"tar -C{folder} -cf - .")
    assert guard.touches_secrets(command=f"tar --directory={folder} -cf - .")
    assert guard.touches_secrets(command="tar --directory=~/.config/genesis -cf - .")


@pytest.mark.parametrize(
    "command",
    ["python -m genesis db query x", "cd genesis && ls", "git commit -m genesis", "rm -rf build", "ls -la"],
)
def test_ordinary_genesis_words_stay_quiet_from_the_checkout(tmp_path, monkeypatch, command):
    _home_with_secret(tmp_path, monkeypatch)
    checkout = tmp_path / "genesis"
    (checkout / "genesis").mkdir(parents=True)
    monkeypatch.chdir(checkout)
    assert not _guard(monkeypatch).touches_secrets(command=command)
