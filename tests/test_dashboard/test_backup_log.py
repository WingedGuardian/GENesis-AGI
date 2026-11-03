"""Backup diagnostics come from the latest service invocation, behind login."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard._blueprint import blueprint
from genesis.dashboard.routes import backup

_INVOCATION = "a" * 32


@pytest.fixture
def client(monkeypatch, tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(blueprint)
    monkeypatch.setattr(backup, "has_verified_credential", lambda: True)
    monkeypatch.setattr(
        backup, "_systemctl", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=_INVOCATION)
    )
    stale = tmp_path / "backup.log"
    stale.write_text("stale legacy backup\n")
    monkeypatch.setattr(backup, "_BACKUP_LOG", stale, raising=False)
    return app.test_client()


def test_log_uses_latest_invocation_and_preserves_early_long_diagnostics(client):
    lines = ["refused member '" + "x" * 700 + "': AbsoluteLinkError"]
    lines += [f"uploaded synthetic-file-{i}" for i in range(80)]
    lines += ["Backup complete (success=true)"]
    with (
        patch.object(
            backup, "_systemctl", return_value=SimpleNamespace(returncode=0, stdout=_INVOCATION)
        ) as ctl,
        patch.object(backup, "systemctl_env", return_value={"TEST_JOURNAL_ENV": "1"}),
        patch.object(
            backup.subprocess,
            "run",
            return_value=SimpleNamespace(returncode=0, stdout="\n".join(lines)),
        ) as run,
    ):
        response = client.get("/api/genesis/backup/log")
    assert response.status_code == 200
    assert response.json["lines"] == lines
    assert response.json["invocation_id"] == _INVOCATION
    assert response.json["source"] == "journal"
    ctl.assert_called_once_with("show", backup._SERVICE_UNIT, "--property=InvocationID", "--value")
    assert run.call_args.args[0] == [
        "journalctl",
        "--user",
        "--unit",
        backup._SERVICE_UNIT,
        f"_SYSTEMD_INVOCATION_ID={_INVOCATION}",
        "--no-pager",
        "--output=cat",
        "--all",
        "--quiet",
    ]
    assert run.call_args.kwargs["env"] == {"TEST_JOURNAL_ENV": "1"}
    assert run.call_args.kwargs["timeout"] > 0


def test_log_requires_verified_credentials_even_when_auth_is_disabled(client, monkeypatch):
    monkeypatch.setattr(backup, "has_verified_credential", lambda: False)
    monkeypatch.setattr(backup, "is_authenticated", lambda: True)
    with patch.object(backup, "_systemctl") as ctl, patch.object(backup.subprocess, "run") as run:
        response = client.get("/api/genesis/backup/log")
    assert response.status_code == 403
    assert response.json["lines"] == []
    ctl.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("proc", [None, SimpleNamespace(returncode=1, stdout="private-error")])
def test_log_service_probe_failure_is_explicit(client, proc):
    with (
        patch.object(backup, "_systemctl", return_value=proc),
        patch.object(backup.subprocess, "run") as run,
    ):
        response = client.get("/api/genesis/backup/log")
    assert response.status_code == 503
    assert response.json["lines"] == []
    assert response.json["error"]
    run.assert_not_called()


@pytest.mark.parametrize("invocation", ["", "not-an-invocation"])
def test_log_does_not_read_historical_runs_without_a_valid_invocation(client, invocation):
    with (
        patch.object(
            backup, "_systemctl", return_value=SimpleNamespace(returncode=0, stdout=invocation)
        ),
        patch.object(backup.subprocess, "run") as run,
    ):
        response = client.get("/api/genesis/backup/log")
    assert response.json["lines"] == []
    assert response.json["error"]
    run.assert_not_called()


@pytest.mark.parametrize(
    "error", [OSError("private-path"), subprocess.TimeoutExpired("journalctl", 5)]
)
def test_log_journal_unavailable_never_returns_the_stale_file(client, error):
    with patch.object(backup.subprocess, "run", side_effect=error):
        response = client.get("/api/genesis/backup/log")
    assert response.status_code == 503
    assert response.json["lines"] == []
    assert "private-path" not in response.get_data(as_text=True)


def test_log_failed_journal_read_does_not_return_partial_output_or_private_stderr(client):
    proc = SimpleNamespace(returncode=1, stdout="partial private output", stderr="private stderr")
    with patch.object(backup.subprocess, "run", return_value=proc):
        response = client.get("/api/genesis/backup/log")
    assert response.status_code == 503
    assert response.json["lines"] == []
    assert "private" not in response.get_data(as_text=True)


def test_log_empty_journal_is_explicit_instead_of_showing_old_logs(client):
    with patch.object(
        backup.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="")
    ):
        response = client.get("/api/genesis/backup/log")
    assert response.json["lines"] == []
    assert response.json["error"]
