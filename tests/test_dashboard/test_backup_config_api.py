"""Tests for the dashboard backup configuration + status routes.

The schedule is managed through the ``genesis-backup.timer`` systemd USER unit
(the source of truth), NOT crontab — the hardened ``genesis-server`` namespace
(``NoNewPrivileges=yes``) neutralises the setgid ``crontab`` binary, so the old
crontab read/write path was dead. ``systemctl --user`` reaches the session
manager over D-Bus and works under that sandbox.

The secrets writer, the systemd helpers, and the drop-in file write are mocked —
no real secrets.env / systemd unit / crontab is touched.
"""

from __future__ import annotations

import shutil
import subprocess
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


def _proc(stdout="", returncode=0, stderr=""):
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

    assert (
        _strip_url_creds("https://user:ghp_tok@github.com/u/r.git") == "https://github.com/u/r.git"
    )
    assert _strip_url_creds("https://github.com/u/r.git") == "https://github.com/u/r.git"
    assert _strip_url_creds("git@github.com:u/r.git") == "git@github.com:u/r.git"
    assert _strip_url_creds("") == ""
    assert _strip_url_creds(None) is None


# ── _interval_from_calendar (pure) ────────────────────────────────────
def test_interval_from_calendar_maps_presets():
    from genesis.dashboard.routes.backup import _interval_from_calendar

    assert _interval_from_calendar("{ OnCalendar=*-*-* 00/6:10:00 ; next_elapse=... }") == "6h"
    assert _interval_from_calendar("{ OnCalendar=*-*-* 00/3:10:00 ; next_elapse=(null) }") == "3h"
    assert _interval_from_calendar("{ OnCalendar=*-*-* 04:10:00 ; next_elapse=... }") == "daily"
    # Unrecognised OnCalendar → "custom"; absent → None.
    assert _interval_from_calendar("{ OnCalendar=*-*-* 09:00:00 ; next_elapse=... }") == "custom"
    assert _interval_from_calendar("") is None
    assert _interval_from_calendar(None) is None


# ── _timer_state (parses systemctl) ───────────────────────────────────
def test_timer_state_parses_enabled_active():
    from genesis.dashboard.routes.backup import _timer_state

    def fake_run(argv, **kw):
        # ["systemctl", "--user", <subcommand>, <unit>, ...] → subcommand at [2].
        assert argv[:2] == ["systemctl", "--user"]
        sub = argv[2]
        if sub == "is-enabled":
            return _proc("enabled\n")
        if sub == "is-active":
            return _proc("active\n")
        if sub == "show":
            return _proc(
                "NextElapseUSecRealtime=Fri 2026-07-10 18:10:00 EDT\n"
                "LastTriggerUSec=Fri 2026-07-10 12:10:00 EDT\n"
                "TimersCalendar={ OnCalendar=*-*-* 00/6:10:00 ; next_elapse=... }\n"
            )
        return _proc("", returncode=1)

    with patch(f"{_BK}.subprocess.run", side_effect=fake_run):
        st = _timer_state()
    assert st["mechanism"] == "systemd-timer"
    assert st["enabled"] is True and st["active"] is True
    assert st["interval"] == "6h"
    assert "18:10:00" in st["next_run"]
    assert "12:10:00" in st["last_trigger"]


def test_timer_state_disabled_inactive():
    from genesis.dashboard.routes.backup import _timer_state

    def fake_run(argv, **kw):
        sub = argv[2]
        if sub == "is-enabled":
            return _proc("disabled\n")
        if sub == "is-active":
            return _proc("inactive\n", returncode=3)
        if sub == "show":
            return _proc(
                "NextElapseUSecRealtime=\nLastTriggerUSec=\n"
                "TimersCalendar={ OnCalendar=*-*-* 00/6:10:00 ; next_elapse=(null) }\n"
            )
        return _proc("", returncode=1)

    with patch(f"{_BK}.subprocess.run", side_effect=fake_run):
        st = _timer_state()
    assert st["enabled"] is False and st["active"] is False
    assert st["next_run"] is None and st["last_trigger"] is None
    assert st["interval"] == "6h"


def test_timer_state_survives_systemctl_failure():
    from genesis.dashboard.routes.backup import _timer_state

    with patch(f"{_BK}.subprocess.run", side_effect=OSError("no systemctl")):
        st = _timer_state()
    assert st == {
        "mechanism": "systemd-timer",
        "enabled": False,
        "active": False,
        "next_run": None,
        "last_trigger": None,
        "interval": None,
    }


# ── _set_timer_schedule (writes reset drop-in) ────────────────────────
def test_set_timer_schedule_writes_reset_dropin(tmp_path):
    from genesis.dashboard.routes import backup as bk

    dropin = tmp_path / "genesis-backup.timer.d" / "schedule.conf"
    with (
        patch.object(bk, "_TIMER_DROPIN", dropin),
        patch(f"{_BK}.subprocess.run", return_value=_proc("")) as run,
    ):
        assert bk._set_timer_schedule("12h") is True
    content = dropin.read_text()
    # The empty OnCalendar= line MUST precede the real one — it resets systemd's
    # additive base so the template's 6h schedule does not also fire.
    assert content == "[Timer]\nOnCalendar=\nOnCalendar=00/12:10\n"
    # daemon-reload issued after the write.
    assert any(
        c.args[0][:4] == ["systemctl", "--user", "daemon-reload"] or c.args[0][3] == "daemon-reload"
        for c in run.call_args_list
    )


def test_set_timer_schedule_rejects_unknown_interval(tmp_path):
    from genesis.dashboard.routes import backup as bk

    dropin = tmp_path / "schedule.conf"
    with (
        patch.object(bk, "_TIMER_DROPIN", dropin),
        patch(f"{_BK}.subprocess.run") as run,
    ):
        assert bk._set_timer_schedule("7h") is False
    assert not dropin.exists()
    run.assert_not_called()


# ── _set_timer_enabled ────────────────────────────────────────────────
def test_set_timer_enabled_true_enables_now():
    from genesis.dashboard.routes import backup as bk

    with patch(f"{_BK}.subprocess.run", return_value=_proc("")) as run:
        assert bk._set_timer_enabled(True) is True
    argv = run.call_args.args[0]
    assert argv[:2] == ["systemctl", "--user"]
    assert "enable" in argv and "--now" in argv and bk._TIMER_UNIT in argv


def test_set_timer_enabled_false_disables_now():
    from genesis.dashboard.routes import backup as bk

    with patch(f"{_BK}.subprocess.run", return_value=_proc("")) as run:
        assert bk._set_timer_enabled(False) is True
    argv = run.call_args.args[0]
    assert "disable" in argv and "--now" in argv


# ── M5: the two calendar maps stay in lockstep ────────────────────────
@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_calendar_maps_are_consistent():
    """Every short OnCalendar we WRITE must normalise (per systemd) to exactly
    the long form we READ back — guards the two hand-maintained maps drifting."""
    from genesis.dashboard.routes.backup import _CALENDAR_TO_INTERVAL, _INTERVAL_TO_CALENDAR

    # Both maps describe the same 4 presets.
    assert set(_INTERVAL_TO_CALENDAR) == set(_CALENDAR_TO_INTERVAL.values())
    for key, short in _INTERVAL_TO_CALENDAR.items():
        out = subprocess.run(
            ["systemd-analyze", "calendar", short], capture_output=True, text=True, timeout=10
        )
        norm = next(
            (
                ln.split(":", 1)[1].strip()
                for ln in out.stdout.splitlines()
                if "Normalized form" in ln
            ),
            None,
        )
        assert norm is not None, f"no normalized form for {short}"
        assert _CALENDAR_TO_INTERVAL.get(norm) == key, (
            f"{short} normalises to {norm!r}; reverse-map disagrees"
        )


# ── POST auth gate ────────────────────────────────────────────────────
def test_set_requires_auth(client):
    with (
        patch(f"{_BK}.is_authenticated", return_value=False),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}._set_timer_enabled") as en,
        patch(f"{_BK}._set_timer_schedule") as sch,
    ):
        resp = client.post("/api/genesis/backup/config", json={"tier2_backend": "none"})
    assert resp.status_code == 401
    wr.assert_not_called()
    en.assert_not_called()
    sch.assert_not_called()


# ── POST validation ───────────────────────────────────────────────────
def test_set_rejects_bad_backend(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config", json={"tier2_backend": "ftp"})
    assert resp.status_code == 422
    assert any("tier2_backend" in e for e in resp.get_json()["details"])


def test_set_smb_requires_nas(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config", json={"tier2_backend": "smb"})
    assert resp.status_code == 422
    assert any("NAS" in e for e in resp.get_json()["details"])


def test_set_rejects_bad_interval(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_BK}._set_timer_enabled") as en,
    ):
        resp = client.post(
            "/api/genesis/backup/config", json={"schedule_enabled": True, "schedule_interval": "7h"}
        )
    assert resp.status_code == 422
    assert any("schedule_interval" in e for e in resp.get_json()["details"])
    en.assert_not_called()


def test_set_rejects_bad_repo(client):
    with patch(f"{_SEC}._key_value", return_value=""):
        resp = client.post("/api/genesis/backup/config", json={"repo": "ftp://nope"})
    assert resp.status_code == 422


# ── POST happy paths ──────────────────────────────────────────────────
def test_set_writes_env_and_enables_timer(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}._set_timer_schedule", return_value=True) as sch,
        patch(f"{_BK}._set_timer_enabled", return_value=True) as en,
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post(
            "/api/genesis/backup/config",
            json={
                "repo": "https://github.com/u/backups.git",
                "tier2_backend": "local",
                "local_path": "/mnt/bk",
                "schedule_interval": "12h",
                "schedule_enabled": True,
            },
        )
    assert resp.status_code == 200
    written = wr.call_args.args[0]
    assert written["GENESIS_BACKUP_REPO"] == "https://github.com/u/backups.git"
    assert written["GENESIS_BACKUP_TIER2_BACKEND"] == "local"
    sch.assert_called_once_with("12h")
    en.assert_called_once_with(True)
    assert resp.get_json()["schedule"] == "enabled"


def test_set_schedule_disabled_skips_interval_and_disables(client):
    """M2: turning the schedule OFF must NOT validate the interval (a host on a
    'custom' schedule must still be able to disable) — only disable the timer."""
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}._set_timer_schedule") as sch,
        patch(f"{_BK}._set_timer_enabled", return_value=True) as en,
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post(
            "/api/genesis/backup/config",
            json={"schedule_enabled": False, "schedule_interval": "7h"},
        )  # bad interval ignored
    assert resp.status_code == 200
    sch.assert_not_called()
    en.assert_called_once_with(False)
    assert resp.get_json()["schedule"] == "disabled"


def test_set_secrets_only_written_when_provided(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file") as wr,
        patch(f"{_BK}._set_timer_enabled", return_value=True),
        patch.dict("os.environ", {}, clear=False),
    ):
        client.post(
            "/api/genesis/backup/config", json={"passphrase": "s3cret", "schedule_enabled": False}
        )
    written = wr.call_args.args[0]
    assert written.get("GENESIS_BACKUP_PASSPHRASE") == "s3cret"
    assert "GENESIS_BACKUP_NAS_PASS" not in written


def test_set_warns_on_passphrase_rotation(client):
    def keyval(k):
        return "old-pass" if k == "GENESIS_BACKUP_PASSPHRASE" else ""

    with (
        patch(f"{_SEC}._key_value", side_effect=keyval),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}._set_timer_enabled", return_value=True),
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post(
            "/api/genesis/backup/config", json={"passphrase": "new-pass", "schedule_enabled": False}
        )
    assert any("re-encrypt" in w for w in resp.get_json()["warnings"])


def test_set_timer_enable_failure_returns_500(client):
    with (
        patch(f"{_SEC}._key_value", return_value=""),
        patch(f"{_SEC}._update_secrets_file"),
        patch(f"{_BK}._set_timer_schedule", return_value=True),
        patch(f"{_BK}._set_timer_enabled", return_value=False),
        patch.dict("os.environ", {}, clear=False),
    ):
        resp = client.post(
            "/api/genesis/backup/config", json={"schedule_interval": "6h", "schedule_enabled": True}
        )
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
        patch(
            f"{_BK}._timer_state",
            return_value={
                "mechanism": "systemd-timer",
                "enabled": True,
                "active": True,
                "next_run": None,
                "last_trigger": None,
                "interval": "6h",
            },
        ),
        patch(f"{_BK}.is_authenticated", return_value=False),
    ):
        resp = client.get("/api/genesis/backup/config")
    data = resp.get_json()
    assert data["repo"] == "https://github.com/u/r.git"
    assert "pp" not in str(data) and "np" not in str(data)
    assert data["passphrase_set"] is True and data["nas_pass_set"] is True
    # Infra paths/shares are auth-gated — absent for unauthenticated callers.
    assert "nas" not in data and "nas_user" not in data and "local_path" not in data
    assert data["schedule_interval"] == "6h" and data["schedule_enabled"] is True


def test_get_config_returns_nas_when_authenticated(client):
    values = {
        "GENESIS_BACKUP_NAS": "//host/share",
        "GENESIS_BACKUP_NAS_USER": "bk",
        "GENESIS_BACKUP_LOCAL_PATH": "/mnt/bk",
    }
    with (
        patch(f"{_SEC}._key_value", side_effect=lambda k: values.get(k, "")),
        patch(
            f"{_BK}._timer_state",
            return_value={
                "mechanism": "systemd-timer",
                "enabled": False,
                "active": False,
                "next_run": None,
                "last_trigger": None,
                "interval": None,
            },
        ),
        patch(f"{_BK}.is_authenticated", return_value=True),
    ):
        resp = client.get("/api/genesis/backup/config")
    data = resp.get_json()
    assert data["nas"] == "//host/share" and data["nas_user"] == "bk"
    assert data["local_path"] == "/mnt/bk"


# ── GET status: destinations + allowlist ──────────────────────────────
def _status_env(tmp_path, status: dict, authed=True):
    """Patch the status route's filesystem deps to a controlled fixture."""
    from genesis.dashboard.routes import backup as bk

    sf = tmp_path / "backup_status.json"
    import json

    sf.write_text(json.dumps(status))
    return (
        patch.object(bk, "_STATUS_FILE", sf),
        patch.object(bk, "_BACKUP_SCRIPT", tmp_path / "backup.sh"),
        patch.object(bk, "_BACKUP_DIR", tmp_path / "nope"),  # not a dir → repo None
        patch(
            f"{_BK}._timer_state",
            return_value={
                "mechanism": "systemd-timer",
                "enabled": True,
                "active": True,
                "next_run": "x",
                "last_trigger": "y",
                "interval": "6h",
            },
        ),
        patch(f"{_BK}.is_authenticated", return_value=authed),
    )


def test_status_builds_destinations_and_schedule(client, tmp_path):
    status = {
        "timestamp": "2026-07-10T16:05:26Z",
        "success": True,
        "duration_s": 325,
        "sqlite_lines": 599261,
        "qdrant_collections": 2,
        "transcript_files": 859,
        "memory_files": 409,
        "secrets_encrypted": True,
        "failure_reason": "",
        "tier2_status": "ok",
        "offsite_confirmed": True,
        "tier2_backend": "smb",
        "snapshot_id": "20260710T160526Z",
        "snapshot_count": 17,
        "pruned_count": 1,
        "tier1_pushed": True,
    }
    patches = _status_env(tmp_path, status, authed=True)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch(
            f"{_SEC}._key_value",
            side_effect=lambda k: "//host/share" if k == "GENESIS_BACKUP_NAS" else "",
        ),
    ):
        resp = client.get("/api/genesis/backup/status")
    data = resp.get_json()
    assert "cron_schedule" not in data
    assert data["schedule"]["interval"] == "6h"
    d = data["destinations"]
    assert d["tier2"]["backend"] == "smb" and d["tier2"]["confirmed"] is True
    assert d["tier2"]["snapshot_count"] == 17
    # Authenticated → NAS target visible.
    assert d["tier2"]["target"] == "//host/share"
    assert d["tier1"]["pushed"] is True


def test_status_hides_nas_target_when_unauthenticated(client, tmp_path):
    status = {
        "timestamp": "t",
        "success": True,
        "tier2_status": "ok",
        "offsite_confirmed": True,
        "tier2_backend": "smb",
        "snapshot_id": "s",
        "snapshot_count": 3,
        "tier1_pushed": True,
    }
    patches = _status_env(tmp_path, status, authed=False)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch(
            f"{_SEC}._key_value",
            side_effect=lambda k: "//secret-host/share" if k == "GENESIS_BACKUP_NAS" else "",
        ),
    ):
        resp = client.get("/api/genesis/backup/status")
    data = resp.get_json()
    assert "target" not in data["destinations"]["tier2"]
    # The NAS host must never appear anywhere in an unauthenticated response.
    assert "secret-host" not in str(data)


def test_status_allowlists_last_backup_fields(client, tmp_path):
    """M6: a future/unknown field in the status file must NOT auto-leak through
    the unauthenticated /status route — last_backup is projected to a known set."""
    status = {
        "timestamp": "t",
        "success": True,
        "duration_s": 1,
        "tier2_status": "ok",
        "offsite_confirmed": True,
        "_leaked_nas_path": "//private-nas/secret-share",
    }
    patches = _status_env(tmp_path, status, authed=False)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch(f"{_SEC}._key_value", return_value=""),
    ):
        resp = client.get("/api/genesis/backup/status")
    data = resp.get_json()
    assert "_leaked_nas_path" not in data["last_backup"]
    assert "private-nas" not in str(data)


# ── /trigger uses the service (non-blocking) ──────────────────────────
def test_trigger_starts_service_no_block(client, tmp_path):
    from genesis.dashboard.routes import backup as bk

    script = tmp_path / "backup.sh"
    script.write_text("#!/bin/bash\n")
    with (
        patch.object(bk, "_BACKUP_SCRIPT", script),
        patch(f"{_BK}.subprocess.run", return_value=_proc("")) as run,
    ):
        resp = client.post("/api/genesis/backup/trigger")
    assert resp.status_code == 200
    argv = run.call_args.args[0]
    # Type=oneshot: a plain `start` blocks ~325s until the backup finishes —
    # --no-block returns immediately so the request thread never hangs.
    assert "start" in argv and "--no-block" in argv
    assert bk._SERVICE_UNIT in argv


def test_trigger_missing_script_404(client, tmp_path):
    from genesis.dashboard.routes import backup as bk

    with patch.object(bk, "_BACKUP_SCRIPT", tmp_path / "absent.sh"):
        resp = client.post("/api/genesis/backup/trigger")
    assert resp.status_code == 404


# ── Tier-2 backend resolution (backward-compat) ───────────────────────
def test_resolved_backend_backward_compat():
    """Mirrors backup_backends.sh:_backend_resolve — explicit selector wins;
    NAS-only (no selector) → smb; nothing → none."""
    from genesis.dashboard.routes import backup as bk

    with patch(
        f"{_SEC}._key_value",
        side_effect=lambda k: "local" if k == "GENESIS_BACKUP_TIER2_BACKEND" else "",
    ):
        assert bk._resolved_backend() == "local"
    with patch(
        f"{_SEC}._key_value",
        side_effect=lambda k: "//host/share" if k == "GENESIS_BACKUP_NAS" else "",
    ):
        assert bk._resolved_backend() == "smb"
    with patch(f"{_SEC}._key_value", return_value=""):
        assert bk._resolved_backend() == "none"


def test_get_config_backend_backward_compat(client):
    """A NAS-only install (no explicit selector) must report backend 'smb', not
    'none' — otherwise the form misleads and a save could write TIER2_BACKEND=none
    and silently disable off-site backups."""
    values = {"GENESIS_BACKUP_NAS": "//host/share"}
    with (
        patch(f"{_SEC}._key_value", side_effect=lambda k: values.get(k, "")),
        patch(
            f"{_BK}._timer_state",
            return_value={
                "mechanism": "systemd-timer",
                "enabled": True,
                "active": True,
                "next_run": None,
                "last_trigger": None,
                "interval": "6h",
            },
        ),
        patch(f"{_BK}.is_authenticated", return_value=True),
    ):
        resp = client.get("/api/genesis/backup/config")
    assert resp.get_json()["tier2_backend"] == "smb"


def test_status_destinations_backend_backward_compat(client, tmp_path):
    """When the status file predates tier2_backend, destinations still resolves
    the backend via backward-compat (NAS set → smb), not 'none'."""
    status = {
        "timestamp": "t",
        "success": True,
        "tier2_status": "ok",
        "offsite_confirmed": True,
    }  # no tier2_backend field (old script)
    patches = _status_env(tmp_path, status, authed=False)
    with (
        patches[0],
        patches[1],
        patches[2],
        patches[3],
        patches[4],
        patch(
            f"{_SEC}._key_value",
            side_effect=lambda k: "//host/share" if k == "GENESIS_BACKUP_NAS" else "",
        ),
    ):
        resp = client.get("/api/genesis/backup/status")
    assert resp.get_json()["destinations"]["tier2"]["backend"] == "smb"


# ── Secrets-UI masking (unchanged secrets.py guard) ───────────────────
def test_sensitive_re_masks_backup_nas_pass():
    from genesis.dashboard.routes.secrets import _SENSITIVE_RE

    assert _SENSITIVE_RE.search("GENESIS_BACKUP_NAS_PASS")
    assert _SENSITIVE_RE.search("GENESIS_BACKUP_PASSPHRASE")
    assert not _SENSITIVE_RE.search("GENESIS_BACKUP_NAS_SHARE")
    assert not _SENSITIVE_RE.search("GENESIS_BACKUP_NAS_USER")
    assert not _SENSITIVE_RE.search("GENESIS_BACKUP_LOCAL_PATH")
