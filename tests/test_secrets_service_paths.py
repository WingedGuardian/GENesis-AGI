"""Service preparation keeps the out-of-checkout credentials file discoverable."""

from __future__ import annotations

import importlib.util
import os
import shutil
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


def test_fresh_install_defaults_to_the_config_location(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRETS_PATH", raising=False)
    assert secrets_location.resolve(environ={}, home=tmp_path, repo_root=tmp_path / "repo") == (
        tmp_path / ".config" / "genesis" / "secrets.env"
    )
    result = subprocess.run(
        ["bash", "-c", '. "$1"; genesis_secrets_path "$2"', "_",
         str(REPO / "scripts/lib/secrets_path.sh"), str(tmp_path / "repo")],
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert Path(result.stdout.strip()) == tmp_path / ".config/genesis/secrets.env"


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


def test_units_load_the_new_path_after_the_legacy_path():
    for name in ("genesis-server", "agent-zero"):
        text = (REPO / f"scripts/systemd/{name}.service.template").read_text()
        lines = [line for line in text.splitlines() if line.startswith("EnvironmentFile=")]
        assert lines == [
            'EnvironmentFile=-__REPO_DIR__/secrets.env',
            'EnvironmentFile=-%h/.config/genesis/secrets.env',
        ]


def test_environment_file_lines_are_accepted_by_systemd(tmp_path):
    analyzer = shutil.which("systemd-analyze")
    if analyzer is None:
        pytest.skip("systemd-analyze unavailable")
    for name in ("genesis-server", "agent-zero"):
        source = (REPO / f"scripts/systemd/{name}.service.template").read_text()
        lines = [line.replace("__REPO_DIR__", str(tmp_path / "repo space"))
                 for line in source.splitlines() if line.startswith("EnvironmentFile=")]
        unit = tmp_path / f"{name}-probe.service"
        unit.write_text("[Service]\nType=oneshot\nExecStart=/bin/true\n" + "\n".join(lines) + "\n")
        result = subprocess.run([analyzer, "verify", str(unit)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        # systemd can ignore a bad EnvironmentFile while still exiting zero.
        assert "EnvironmentFile=" not in result.stderr, result.stderr
    invalid = tmp_path / "invalid-probe.service"
    invalid.write_text("[Service]\nExecStart=/bin/true\nEnvironmentFile=relative.env\n")
    control = subprocess.run([analyzer, "verify", str(invalid)], capture_output=True, text=True)
    assert "EnvironmentFile=" in control.stderr, "analyzer did not detect invalid control"


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
