"""DR-04 honest off-site signal tests for ``scripts/backup.sh``.

A backup that captures local data but fails to replicate off-site (NAS) must say
so. ``backup_status.json`` carries ``offsite_confirmed``; when a NAS IS configured
but the upload didn't fully succeed, a *distinct* Telegram alert fires — and the
local backup stays marked successful (only the off-site copy is missing, not the
data). Local-only installs (no NAS configured) are a valid choice, not a failure.

Fully sandboxed: ``HOME`` + ``GENESIS_DIR`` point at a tmp dir. Real
``sqlite3``/``gpg``/``git``; ``smbclient`` and ``curl`` (Telegram + Qdrant)
stubbed so the run is deterministic and offline.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

_BACKUP = Path(__file__).resolve().parents[2] / "scripts" / "backup.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def backup_env(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / ".gnupg").mkdir(mode=0o700)
    subprocess.run(
        ["sqlite3", str(gd / "data" / "genesis.db"), "CREATE TABLE t(x); INSERT INTO t VALUES(1);"],
        check=True,
        capture_output=True,
    )

    # Backup repo: bare remote (main) + a clone at $HOME/backups/genesis-backups
    # so backup.sh skips its clone and `git push` lands on the local remote.
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(bare), cwd=tmp_path)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    seed = tmp_path / "seed"
    _git("-c", "init.defaultBranch=main", "clone", "-q", str(bare), str(seed), cwd=tmp_path)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    _git("config", "user.email", "t@t.t", cwd=seed)
    _git("config", "user.name", "t", cwd=seed)
    (seed / "README").write_text("seed\n")
    _git("add", "README", cwd=seed)
    _git("commit", "-qm", "init", cwd=seed)
    _git("push", "-q", "origin", "main", cwd=seed)
    (home / "backups").mkdir(parents=True)
    _git("clone", "-q", str(bare), str(home / "backups" / "genesis-backups"), cwd=tmp_path)

    bind = tmp_path / "bin"
    bind.mkdir()
    tg = tmp_path / "telegram_calls.log"
    # curl stub: capture Telegram sends; answer the SF3 existence probe with
    # 404 (collections genuinely absent → benign skip — a bare connection
    # failure now correctly FAILS the backup); fail everything else.
    _make_stub(
        bind / "curl",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in\n'
        f'  *api.telegram.org*) printf "%s\\n" "$*" >> "{tg}"; exit 0 ;;\n'
        "esac; done\n"
        'for a in "$@"; do [ "$a" = "-w" ] && { printf "404"; exit 0; }; done\n'
        "exit 1\n",
    )
    return {"home": home, "gd": gd, "bind": bind, "tg": tg, "tmp": tmp_path}


def _run(
    backup_env,
    *,
    smb_rc: int = 0,
    nas: bool = True,
    remove_db: bool = False,
    fail_complete: bool = False,
):
    if remove_db:  # force _SUCCESS=false (no SQLite data) for the failure-alert path
        (backup_env["gd"] / "data" / "genesis.db").unlink(missing_ok=True)
    if fail_complete:  # payloads upload OK, only the COMPLETE marker fails
        _make_stub(
            backup_env["bind"] / "smbclient",
            '#!/usr/bin/env bash\ncase "$*" in *COMPLETE*) exit 1 ;; esac\nexit 0\n',
        )
    else:
        _make_stub(backup_env["bind"] / "smbclient", f"#!/usr/bin/env bash\nexit {smb_rc}\n")
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]),
        GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        QDRANT_URL="http://127.0.0.1:1",
        TELEGRAM_BOT_TOKEN="bot-x",
        TELEGRAM_FORUM_CHAT_ID="chat-y",
        PATH=f"{backup_env['bind']}:{os.environ['PATH']}",
    )
    if nas:
        env.update(
            GENESIS_BACKUP_NAS="//nas/share",
            GENESIS_BACKUP_NAS_USER="u",
            GENESIS_BACKUP_NAS_PASS="p",
        )
    else:
        for k in ("GENESIS_BACKUP_NAS", "GENESIS_BACKUP_NAS_USER", "GENESIS_BACKUP_NAS_PASS"):
            env.pop(k, None)
    proc = subprocess.run(
        ["bash", str(_BACKUP)], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    status = json.loads((backup_env["home"] / ".genesis" / "backup_status.json").read_text())
    tg = backup_env["tg"].read_text() if backup_env["tg"].exists() else ""
    return proc, status, tg


def test_offsite_confirmed_true_when_nas_ok(backup_env):
    """NAS upload succeeds → offsite_confirmed:true and no alert."""
    proc, status, tg = _run(backup_env, smb_rc=0, nas=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert status["success"] is True, status
    assert status["tier2_status"] == "ok", status
    assert status["offsite_confirmed"] is True, status
    # The curl stub only logs on a Telegram send → empty log == no alert.
    assert not tg.strip(), f"unexpected alert on a fully-successful backup:\n{tg}"


def test_offsite_failure_alerts_but_local_succeeds(backup_env):
    """NAS configured but the upload fails → offsite_confirmed:false, the LOCAL
    backup still succeeds, and a distinct off-site alert fires."""
    proc, status, tg = _run(backup_env, smb_rc=1, nas=True)  # NAS upload fails
    assert proc.returncode == 0, (
        f"{proc.stdout}\n{proc.stderr}"
    )  # off-site miss is not a hard failure
    assert status["success"] is True, status
    assert status["offsite_confirmed"] is False, status
    assert status["tier2_status"] != "ok", status
    assert tg.strip(), f"off-site replication failure did not alert:\n{tg}"
    assert "replication" in tg.lower() or "off-site" in tg.lower(), tg


def test_backup_failure_still_alerts(backup_env):
    """The _send_telegram refactor must not suppress the original backup-failed
    alert: no SQLite data → success:false → 🚨 alert fires."""
    proc, status, tg = _run(backup_env, smb_rc=0, nas=True, remove_db=True)
    assert status["success"] is False, status
    assert "backup failed" in tg.lower(), f"backup-failed alert did not fire:\n{tg}"


def test_offsite_alert_deduplicated(backup_env):
    """A persistent off-site outage alerts ONCE (on the transition), not every
    6h run — the second consecutive failure is deduped via the prior status."""
    _run(backup_env, smb_rc=1, nas=True)  # 1st failure → alert
    _, status2, tg = _run(backup_env, smb_rc=1, nas=True)  # 2nd failure → deduped
    assert status2["offsite_confirmed"] is False, status2
    n = tg.lower().count("off-site replication failed")
    assert n == 1, f"expected exactly 1 (deduped) off-site alert across two runs, got {n}:\n{tg}"


def test_local_only_not_configured_no_alert(backup_env):
    """No NAS configured → offsite_confirmed:false, but local-only is a valid
    choice (not a failure), so no alert."""
    proc, status, tg = _run(backup_env, nas=False)
    assert status["success"] is True, status
    assert status["tier2_status"] == "not_configured", status
    assert status["offsite_confirmed"] is False, status
    assert not tg.strip(), f"local-only must not alert:\n{tg}"


def test_status_enrichment_fields_nas_ok(backup_env):
    """The dashboard reads these fields — assert the real script writes them with
    the right JSON types on a successful two-tier run (guards the enriched
    ``_write_status`` heredoc: counts are ints, not the ``null`` default)."""
    proc, status, _ = _run(backup_env, smb_rc=0, nas=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert status["tier2_backend"] == "smb", status
    # snapshot_id is this run's UTC stamp (YYYYMMDDTHHMMSSZ).
    assert len(status["snapshot_id"]) == 16 and status["snapshot_id"].endswith("Z"), status
    # GFS block ran (tier2 ok) → counts are integers (0 here: the smbclient stub
    # lists nothing), NEVER null and NEVER the wc -l off-by-one 1.
    assert status["snapshot_count"] == 0 and isinstance(status["snapshot_count"], int), status
    assert status["pruned_count"] == 0 and isinstance(status["pruned_count"], int), status
    assert status["tier1_pushed"] is True, status


def test_status_enrichment_fields_local_only(backup_env):
    """No off-site backend → snapshot bookkeeping stays JSON ``null`` (honest
    'unknown', not 0), backend is 'none', but Tier-1 still pushed."""
    proc, status, _ = _run(backup_env, nas=False)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert status["tier2_backend"] == "none", status
    assert status["snapshot_id"] == "", status
    assert status["snapshot_count"] is None and status["pruned_count"] is None, status
    assert status["tier1_pushed"] is True, status


def test_complete_marker_failure_is_offsite_failure(backup_env):
    """If payloads upload but the COMPLETE marker fails, restore would skip the
    snapshot — so it must report partial + offsite_confirmed:false + alert, not ok."""
    proc, status, tg = _run(backup_env, fail_complete=True, nas=True)
    assert status["success"] is True, status  # local backup is still fine
    assert status["tier2_status"] == "partial", status
    assert status["offsite_confirmed"] is False, status
    assert "replication" in tg.lower() or "off-site" in tg.lower(), tg
