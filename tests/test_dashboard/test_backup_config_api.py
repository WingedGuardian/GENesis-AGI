"""Tests for the dashboard backup configuration routes.

Validation, the secrets-writer reuse, cron-wrapper invocation, credential
redaction on reads, and auth-gated NAS fields. The secrets writer and the
crontab subprocess are mocked — no real secrets.env / crontab is touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint

_BK = "genesis.dashboard.routes.backup"
_SEC = "genesis.dashboard.routes.secrets"


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    app.secret_key = "test-secret-key"
    return app.test_client()


def _ok_proc(stdout="installed: ...", returncode=0, stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture(autouse=True)
def _authed():
    """Default the privileged-write gate to authenticated; individual tests
    that need the unauthenticated path re-patch within their own block."""
    with patch(f"{_BK}.is_authenticated", return_value=True):
        yield


# ── _strip_url_creds ──────────────────────────────────────────────────
def test_strip_url_creds_removes_token():
    from genesis.dashboard.routes.backup import _strip_url_creds
    assert _strip_url_creds("https://user:ghp_tok@github.com/u/r.git") == \
        "https://github.com/u/r.git"
    # No creds → unchanged; scp-style git@ has no secret token → unchanged.
    assert _strip_url_creds("https://github.com/u/r.git") == \
        "https://github.com/u/r.git"
    assert _strip_url_creds("git@github.com:u/r.git") == "git@github.com:u/r.git"
    assert _strip_url_creds("") == ""
    assert _strip_url_creds(None) is None


# ── POST auth gate ────────────────────────────────────────────────────
def test_set_requires_auth(client):
    """The privileged write endpoint returns 401 when a dashboard password is
    set but the caller is unauthenticated — and never writes anything."""
    with (
        patch(f"{_BK}.is_authenticated", return_value=False),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}.subprocess.run") as run,
    ):
        resp = client.post("/api/genesis/backup/config",
                           json={"tier2_backend": "none"})
    assert resp.status_code == 401
    wr.assert_not_called()
    run.assert_not_called()


# ── POST validation ───────────────────────────────────────────────────
def test_set_rejects_bad_backend(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file") as wr,
    ):
        resp = client.post("/api/genesis/backup/config",
                           json={"tier2_backend": "ftp"})
    assert resp.status_code == 422
    assert any("tier2_backend" in e for e in resp.get_json()["details"])
    wr.assert_not_called()


def test_set_smb_requires_nas(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config",
                           json={"tier2_backend": "smb"})
    assert resp.status_code == 422
    assert any("NAS" in e for e in resp.get_json()["details"])


def test_set_rejects_bad_nas_and_cron(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config", json={
            "tier2_backend": "smb", "nas": "not-a-share",
            "schedule": "every 6 hours", "schedule_enabled": True,
        })
    details = resp.get_json()["details"]
    assert resp.status_code == 422
    assert any("nas must look like" in e for e in details)
    assert any("cron" in e for e in details)


def test_set_rejects_bad_repo(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config",
                           json={"repo": "ftp://nope"})
    assert resp.status_code == 422


# ── POST happy paths ──────────────────────────────────────────────────
def test_set_writes_env_and_installs_cron(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}.subprocess.run", return_value=_ok_proc()) as run,
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post("/api/genesis/backup/config", json={
            "repo": "https://github.com/u/backups.git",
            "tier2_backend": "local", "local_path": "/mnt/bk",
            "schedule": "0 */6 * * *", "schedule_enabled": True,
        })
    assert resp.status_code == 200
    written = wr.call_args.args[0]
    assert written["GENESIS_BACKUP_REPO"] == "https://github.com/u/backups.git"
    assert written["GENESIS_BACKUP_TIER2_BACKEND"] == "local"
    assert written["GENESIS_BACKUP_LOCAL_PATH"] == "/mnt/bk"
    # cron wrapper invoked with install; the schedule is passed via the
    # environment (never the argv), and the wrapper path is correct.
    cmd = run.call_args.args[0]
    assert "install" in cmd and "0 */6 * * *" not in cmd
    assert cmd[1].endswith("manage_backup_cron.sh")
    assert run.call_args.kwargs["env"]["GENESIS_BACKUP_CRON_SCHEDULE"] == "0 */6 * * *"


def test_set_schedule_disabled_removes_cron(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}.subprocess.run", return_value=_ok_proc("removed")) as run,
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post("/api/genesis/backup/config",
                           json={"schedule_enabled": False})
    assert resp.status_code == 200
    assert "remove" in run.call_args.args[0]


def test_set_secrets_only_written_when_provided(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}.subprocess.run", return_value=_ok_proc()),
        patch.dict("os.environ", {}, clear=False),
    ):
        client.post("/api/genesis/backup/config", json={
            "passphrase": "s3cret", "schedule_enabled": False,
        })
    written = wr.call_args.args[0]
    assert written.get("GENESIS_BACKUP_PASSPHRASE") == "s3cret"
    assert "GENESIS_BACKUP_NAS_PASS" not in written


def test_set_warns_on_passphrase_rotation(client):
    def keyval(k):
        return "old-pass" if k == "GENESIS_BACKUP_PASSPHRASE" else ""
    with (
        patch(f"{_SEC}._key_value", side_effect=keyval),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}.subprocess.run", return_value=_ok_proc()),
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post("/api/genesis/backup/config",
                           json={"passphrase": "new-pass", "schedule_enabled": False})
    warnings = resp.get_json()["warnings"]
    assert any("re-encrypt" in w for w in warnings)


def test_set_cron_failure_returns_500(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}.subprocess.run",
              return_value=_ok_proc(returncode=2, stderr="bad cron")),
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post("/api/genesis/backup/config",
                           json={"schedule": "0 */6 * * *", "schedule_enabled": True})
    assert resp.status_code == 500


# ── GET config ────────────────────────────────────────────────────────
def test_get_config_strips_creds_and_gates_nas(client):
    values = {
        "GENESIS_BACKUP_REPO": "https://u:tok@github.com/u/r.git",
        "GENESIS_BACKUP_TIER2_BACKEND": "smb",
        "GENESIS_BACKUP_NAS": "//host/share",
        "GENESIS_BACKUP_NAS_USER": "bk",
        "GENESIS_BACKUP_PASSPHRASE": "pp",
        "GENESIS_BACKUP_NAS_PASS": "np",
        "GENESIS_BACKUP_LOCAL_PATH": "",
    }
    with (
        patch(f"{_SEC}._key_value", side_effect=lambda k: values.get(k, "")),
        patch(f"{_BK}.subprocess.run",
              return_value=_ok_proc("0 */6 * * * /h/genesis/scripts/backup.sh >> x 2>&1")),
        patch(f"{_BK}.is_authenticated", return_value=False),
    ):
        resp = client.get("/api/genesis/backup/config")
    data = resp.get_json()
    # Token stripped; raw secrets never returned; auth-gated fields absent.
    assert data["repo"] == "https://github.com/u/r.git"
    assert "pp" not in str(data) and "np" not in str(data)
    assert data["passphrase_set"] is True and data["nas_pass_set"] is True
    # Infra paths/shares are auth-gated — absent for unauthenticated callers.
    assert "nas" not in data and "nas_user" not in data and "local_path" not in data
    assert data["schedule"] == "0 */6 * * *" and data["schedule_enabled"] is True


def test_get_config_returns_nas_when_authenticated(client):
    values = {
        "GENESIS_BACKUP_NAS": "//host/share", "GENESIS_BACKUP_NAS_USER": "bk",
        "GENESIS_BACKUP_LOCAL_PATH": "/mnt/bk",
    }
    with (
        patch(f"{_SEC}._key_value", side_effect=lambda k: values.get(k, "")),
        patch(f"{_BK}.subprocess.run", return_value=_ok_proc("")),
        patch(f"{_BK}.is_authenticated", return_value=True),
    ):
        resp = client.get("/api/genesis/backup/config")
    data = resp.get_json()
    assert data["nas"] == "//host/share" and data["nas_user"] == "bk"
    assert data["local_path"] == "/mnt/bk"
