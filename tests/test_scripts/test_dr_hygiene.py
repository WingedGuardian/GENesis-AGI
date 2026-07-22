"""DR hygiene tests (audit SF6/SF7 + notes BK-N1/N2/N4/N5/N6) for the
backup/restore pair — the smaller, lower-severity correctness fixes that sit on
top of the DR-integrity core.

* **SF6** restore off-site pull of qdrant/transcripts warns on a failed get
  (→ non-zero restore) instead of the old silent `… | while | done || true`.
* **SF7** restore sets `umask 077` before any plaintext is written, so a
  decrypted secrets.env / transcript / memory file is never world-readable.
* **BK-N1** the backup-FAILED Telegram alert fires from the EXIT trap, so an
  early abort still alerts.
* **N2** the plaintext SQL dump temp is trap-cleaned (not left in ~/tmp on a
  mid-section death).
* **N4** an unattended restore (no TTY, no --force) fails loudly instead of
  declining every confirm and exiting 0.
* **N5** a restore with no restorable payloads fails instead of reporting
  success:true.
* **N6** a hard sqlite3 integrity-check error doesn't abort the script before
  its warn.

Sandboxed: HOME/GENESIS_DIR in tmp; real sqlite3/gpg/git/flock. Several checks
are extraction-style (assert on the shipped script text) where behavior is
awkward to trigger live.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_BACKUP = _SCRIPTS / "backup.sh"
_RESTORE = _SCRIPTS / "restore.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


# ── extraction-style (assert on the shipped script) ──────────────────


def test_backup_alert_fires_from_exit_trap():
    """BK-N1: the 🚨 backup-failed alert is invoked from the EXIT trap
    (_alert_backup_failed in _on_exit), and the old inline copy is gone so it
    fires exactly once — including on an early abort."""
    text = _BACKUP.read_text()
    assert "_alert_backup_failed()" in text
    assert "_on_exit()" in text and "trap _on_exit EXIT" in text
    # _on_exit runs the alert before writing status.
    on_exit = text.split("_on_exit()", 1)[1].split("trap _on_exit", 1)[0]
    assert on_exit.index("_alert_backup_failed") < on_exit.index("_write_status")
    # No second, inline '🚨 *Backup failed*' emission outside the alert function.
    assert text.count("🚨 *Backup failed*") == 1
    # Robustness (review fix): the trap must not abort mid-way under set -e —
    # the alert is guarded against an undefined _send_telegram (early abort) and
    # every step is `|| true` so _write_status + cleanup always run.
    alert = text.split("_alert_backup_failed()", 1)[1].split("_on_exit()", 1)[0]
    assert "declare -F _send_telegram" in alert
    assert on_exit.count("|| true") >= 3  # alert / write_status / backend_cleanup


def test_backup_sql_tmp_trap_cleaned():
    """N2: the plaintext SQL dump temp is removed in the EXIT trap, guarded for
    the not-yet-assigned case."""
    text = _BACKUP.read_text()
    on_exit = text.split("_on_exit()", 1)[1].split("trap _on_exit", 1)[0]
    assert 'rm -f "${_SQL_TMP:-}"' in on_exit
    assert '_SQL_TMP=""' in text  # initialized before the trap can fire


def test_restore_offsite_pull_warns_on_failure():
    """SF6: the qdrant/transcripts pull uses process-substitution + warn (not the
    silent `list | while | done || true`)."""
    text = _RESTORE.read_text()
    seg = text.split("for sub in qdrant transcripts", 1)[1].split("_pull_from_offsite", 1)[0]
    assert 'warn "off-site: failed to pull $sub/$fname' in seg
    assert "done < <(backend_list" in seg  # process substitution, runs in THIS shell
    assert "| while read -r fname; do" not in seg  # the old subshell form is gone


def test_restore_sets_umask_before_writes():
    """SF7: umask 077 is set before the first section writes any plaintext."""
    text = _RESTORE.read_text()
    assert "\numask 077" in text
    assert text.index("umask 077") < text.index("# ── 1. SQLite")


def test_restore_confirm_eof_dies():
    """N4: confirm() dies on read-EOF (no TTY) rather than treating it as a
    silent decline."""
    text = _RESTORE.read_text()
    conf = text.split("confirm() {", 1)[1].split("}", 1)[0]
    assert "if ! read -r" in conf and "die " in conf


def test_restore_integrity_check_guarded():
    """N6: the integrity_check command-substitution is `|| true`-guarded so a
    hard sqlite3 error can't abort before the warn."""
    text = _RESTORE.read_text()
    assert 'PRAGMA integrity_check;" 2>&1 | head -1) || true' in text


# ── live behavior ────────────────────────────────────────────────────


@pytest.fixture
def restore_sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis" / "data"
    gd.mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / "tmp").mkdir()
    (home / ".gnupg").mkdir(mode=0o700)
    backup = tmp_path / "backup"
    backup.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        GENESIS_DIR=str(home / "genesis"),
        GENESIS_BACKUP_TMPDIR=str(home / "tmp"),
        GENESIS_BACKUP_TIER2_BACKEND="none",
        QDRANT_URL="http://127.0.0.1:1",
    )
    return {"home": home, "gd": gd, "backup": backup, "env": env, "tmp": tmp_path}


_TEST_PASSPHRASE = "testpass"  # noqa: S105 — test fixture, not a real secret


def _seed_secret_payload(sb, passphrase=_TEST_PASSPHRASE):
    """Put one encrypted secrets.env payload in the backup so a restore has
    something to do (passes the N5 empty-guard)."""
    (sb["backup"] / "secrets").mkdir()
    plain = sb["tmp"] / "secrets.plain"
    plain.write_text("SECRET=value\n")
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--passphrase",
            passphrase,
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "-o",
            str(sb["backup"] / "secrets" / "secrets.env.gpg"),
            str(plain),
        ],
        env={**sb["env"], "GNUPGHOME": str(sb["home"] / ".gnupg")},
        check=True,
        capture_output=True,
    )


def test_n5_empty_backup_fails(restore_sandbox):
    """N5: a --force restore against a backup with zero payloads fails loudly
    (was: exit 0 'success' having restored nothing)."""
    sb = restore_sandbox
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"]), "--force"],
        env=sb["env"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, proc.stdout
    assert "no restorable payloads found" in proc.stdout, proc.stdout
    status = json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["success"] is False, status


def test_n5_legacy_plaintext_memory_counts(restore_sandbox):
    """Review fix: the N5 guard must count a legacy plaintext memory file (§4
    restores any file, not just .gpg) — else it false-fails a valid legacy
    backup. Presence of the payload → the guard passes (no 'nothing to restore'
    die); the run proceeds (and legitimately finds nothing NEW to do)."""
    sb = restore_sandbox
    (sb["backup"] / "memory").mkdir()
    (sb["backup"] / "memory" / "note.md").write_text("plaintext-legacy\n")  # no .gpg
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"]), "--force"],
        env=sb["env"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert "no restorable payloads found" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout


def test_n5_credential_mirror_counts(restore_sandbox):
    """Review fix: with an empty BACKUP_DIR but a host-side credential mirror
    holding secrets (§7 restores from it), the N5 guard must NOT die — that
    mirror-only recovery is a real DR path."""
    sb = restore_sandbox
    mirror = sb["home"] / ".genesis" / "shared" / "guardian" / "creds-mirror"
    (mirror / "secrets").mkdir(parents=True)
    (mirror / "secrets" / "secrets.env.gpg").write_bytes(b"encrypted-blob")
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"]), "--force"],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert "no restorable payloads found" not in proc.stdout, proc.stdout


def test_n4_no_tty_no_force_fails(restore_sandbox):
    """N4: no TTY + no --force → the first confirm dies instead of silently
    declining every section and exiting 0. (A payload is present so we reach a
    confirm rather than the N5 empty-guard.)"""
    sb = restore_sandbox
    _seed_secret_payload(sb)
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"])],  # no --force
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode != 0, proc.stdout
    assert "no TTY to confirm" in proc.stdout, proc.stdout


def test_n5_force_with_payload_succeeds(restore_sandbox):
    """Control: a --force restore that HAS a payload passes the N5 guard and
    restores it (0600 via the umask)."""
    sb = restore_sandbox
    _seed_secret_payload(sb)
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"]), "--force"],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": "testpass",
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
            "SECRETS_PATH": str(sb["home"] / "genesis" / "secrets.env"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    secrets_out = sb["home"] / "genesis" / "secrets.env"
    assert secrets_out.is_file(), proc.stdout
    # SF7: decrypted secrets are 0600 (no world/group bits), written under umask 077.
    mode = stat.S_IMODE(secrets_out.stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)
