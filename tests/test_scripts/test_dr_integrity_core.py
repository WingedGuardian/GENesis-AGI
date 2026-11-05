"""DR integrity core (audit SF3/SF4/SF5) tests for backup.sh / restore.sh.

Three properties of the disaster-recovery pair are guarded here:

* **Mutual exclusion (SF5).** backup.sh and restore.sh share one whole-run
  flock (`~/.genesis/locks/backup-restore.lock`). A timer-fired backup SKIPS
  (exit 0, and — critically — without touching ``backup_status.json``, which
  would page a false CRITICAL) when a restore holds the lock; a restore WAITS
  bounded and dies naming the holder. Backend ops are timeout-bounded so a
  hung mount can't hold the DR lock forever.
* **Round-trip verify (SF4).** The freshly-encrypted SQL dump must DECRYPT
  with the passphrase a DR box would use (escrow preferred — env-encrypt +
  escrow-decrypt is the drift detector). A failed round-trip fails the backup.
* **Freshness gate (SF3).** Only payloads regenerated THIS run enter the
  off-site dated snapshot; a leftover .gpg from a failed prior section is
  excluded (and kept locally). A collection that EXISTS but fails to snapshot
  fails the backup; a genuinely absent collection (HTTP 404) stays benign.

Fully sandboxed: ``HOME``/``GENESIS_DIR`` in tmp; real sqlite3/gpg/git/flock;
``curl`` stubbed per-scenario; the off-site tier uses the REAL `local` backend
against a tmp dir so upload assertions are actual files.
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_BACKUP = _SCRIPTS / "backup.sh"
_RESTORE = _SCRIPTS / "restore.sh"
_BACKENDS = _SCRIPTS / "lib" / "backup_backends.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


# curl stub bodies. Every stub logs Telegram sends to TGLOG and handles the
# Qdrant surface per scenario; anything unrecognized fails (offline).
_CURL_HEALTHY = """#!/usr/bin/env bash
# Healthy Qdrant: probe→200, snapshot POST→name, download→bytes, DELETE→ok.
args=("$@"); url=""; out=""; method="GET"; has_w=0; i=0
while [ $i -lt ${#args[@]} ]; do
  a="${args[$i]}"
  case "$a" in
    -X) i=$((i+1)); method="${args[$i]}" ;;
    -o) i=$((i+1)); out="${args[$i]}" ;;
    -w) i=$((i+1)); has_w=1 ;;
    --max-time|-H|-d|-F) i=$((i+1)) ;;
    http*) url="$a" ;;
  esac
  i=$((i+1))
done
case "$url" in *api.telegram.org*) printf '%s\\n' "$*" >> "__TGLOG__"; exit 0 ;; esac
[ "$has_w" = 1 ] && { printf '200'; exit 0; }
[ "$method" = "POST" ] && { printf '{"result":{"name":"s1"}}'; exit 0; }
[ "$method" = "DELETE" ] && exit 0
[ -n "$out" ] && { printf 'snapshot-bytes' > "$out"; exit 0; }
exit 1
"""

_CURL_ABSENT = """#!/usr/bin/env bash
# Collections genuinely absent: probe answers 404; everything else fails.
case "$*" in *api.telegram.org*) printf '%s\\n' "$*" >> "__TGLOG__"; exit 0 ;; esac
for a in "$@"; do [ "$a" = "-w" ] && { printf '404'; exit 0; }; done
exit 1
"""

_CURL_DOWN = """#!/usr/bin/env bash
# Qdrant down: every call fails with no output (probe reads 000).
case "$*" in *api.telegram.org*) printf '%s\\n' "$*" >> "__TGLOG__"; exit 0 ;; esac
exit 1
"""

_CURL_EXISTS_NOSNAP = """#!/usr/bin/env bash
# Collection EXISTS (probe 200) but snapshot creation fails — the audit's real
# "collections exist but weren't captured" case.
case "$*" in *api.telegram.org*) printf '%s\\n' "$*" >> "__TGLOG__"; exit 0 ;; esac
for a in "$@"; do [ "$a" = "-w" ] && { printf '200'; exit 0; }; done
exit 1
"""


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / ".gnupg").mkdir(mode=0o700)
    (home / "tmp").mkdir()
    (gd / ".venv" / "bin").mkdir(parents=True)
    (gd / ".venv" / "bin" / "python").symlink_to(Path(sys.executable))
    (gd / "src").symlink_to(_SCRIPTS.parent / "src", target_is_directory=True)
    subprocess.run(
        ["sqlite3", str(gd / "data" / "genesis.db"), "CREATE TABLE t(x); INSERT INTO t VALUES(1);"],
        check=True,
        capture_output=True,
    )

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
    clone = home / "backups" / "genesis-backups"
    _git("clone", "-q", str(bare), str(clone), cwd=tmp_path)

    offsite = tmp_path / "offsite"
    offsite.mkdir()
    bind = tmp_path / "bin"
    bind.mkdir()
    systemctl_calls = tmp_path / "systemctl_calls.log"
    _make_stub(
        bind / "systemctl",
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{systemctl_calls}"\nexit 0\n',
    )
    tg = tmp_path / "telegram_calls.log"
    return {
        "home": home,
        "gd": gd,
        "bind": bind,
        "tg": tg,
        "offsite": offsite,
        "clone": clone,
        "tmp": tmp_path,
        "systemctl_calls": systemctl_calls,
    }


def _env(sb, **extra):
    env = dict(os.environ)
    for k in (
        "GENESIS_BACKUP_NAS",
        "GENESIS_BACKUP_NAS_USER",
        "GENESIS_BACKUP_NAS_PASS",
        "GENESIS_BACKUP_TIER2_BACKEND",
        "GENESIS_BACKUP_LOCAL_PATH",
        "GENESIS_PASSPHRASE_ESCROW",
        "SECRETS_PATH",  # GENESIS_DIR names the secrets file, as on CI
    ):
        env.pop(k, None)
    env.update(
        HOME=str(sb["home"]),
        GENESIS_DIR=str(sb["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        GENESIS_BACKUP_TMPDIR=str(sb["home"] / "tmp"),
        QDRANT_URL="http://127.0.0.1:1",
        TELEGRAM_BOT_TOKEN="bot-x",
        TELEGRAM_FORUM_CHAT_ID="chat-y",
        GENESIS_BACKUP_TIER2_BACKEND="local",
        GENESIS_BACKUP_LOCAL_PATH=str(sb["offsite"]),
        PATH=f"{sb['bind']}:{os.environ['PATH']}",
    )
    env.update(extra)
    return env


def _install_curl(sb, body: str) -> None:
    _make_stub(sb["bind"] / "curl", body.replace("__TGLOG__", str(sb["tg"])))


def _run_backup(sb, **extra):
    proc = subprocess.run(
        ["bash", str(_BACKUP)],
        env=_env(sb, **extra),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    status_file = Path(extra.get("GENESIS_HOME", sb["home"] / ".genesis")) / "backup_status.json"
    status = json.loads(status_file.read_text()) if status_file.exists() else None
    return proc, status


def _offsite_files(sb):
    return sorted(
        str(p.relative_to(sb["offsite"])) for p in sb["offsite"].rglob("*") if p.is_file()
    )


# ── SF5: mutual exclusion ────────────────────────────────────────────


def test_backup_skips_when_lock_held(sandbox):
    """Lock held → backup exits 0, logs SKIPPED, and writes NO status file
    (success:false would page a false CRITICAL during a legitimate restore)."""
    _install_curl(sandbox, _CURL_ABSENT)
    lock_dir = sandbox["home"] / ".genesis" / "locks"
    lock_dir.mkdir(parents=True)
    lock_file = lock_dir / "backup-restore.lock"
    lock_file.write_text("99999 restore 2026-01-01T00:00:00\n")
    with open(lock_file, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc, status = _run_backup(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "SKIPPED" in proc.stdout and "99999 restore" in proc.stdout, proc.stdout
    assert status is None, f"skip must not write backup_status.json: {status}"


def test_update_backup_lock_contention_is_loud_failure(sandbox):
    """An update-triggered backup cannot silently skip behind the DR lock."""
    _install_curl(sandbox, _CURL_ABSENT)
    lock_dir = sandbox["home"] / ".genesis" / "locks"
    lock_dir.mkdir(parents=True)
    lock_file = lock_dir / "backup-restore.lock"
    lock_file.write_text("99999 restore 2026-01-01T00:00:00\n")
    with open(lock_file, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc, status = _run_backup(
            sandbox,
            GENESIS_BACKUP_TRIGGER="update",
            GENESIS_BACKUP_RUN_ID="update-run-1",
        )
    assert proc.returncode != 0
    assert status["run_id"] == "update-run-1"
    assert status["failure_stage"] == "lock_busy"
    assert status["db_integrity_status"] == "indeterminate"
    assert status["sqlite_backup_verified"] is False


def test_backup_status_proves_sqlite_artifact_for_this_run(sandbox):
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox, GENESIS_BACKUP_RUN_ID="proof-run")
    assert proc.returncode == 0, proc.stdout
    assert status["run_id"] == "proof-run"
    assert status["db_integrity_status"] == "healthy"
    assert status["sqlite_backup_verified"] is True
    assert status["failure_class"] == "none"


def test_missing_integrity_checker_is_indeterminate_not_raw_fallback(sandbox):
    _install_curl(sandbox, _CURL_ABSENT)
    (sandbox["gd"] / ".venv" / "bin" / "python").unlink()
    proc, status = _run_backup(sandbox, GENESIS_BACKUP_RUN_ID="no-checker")
    assert proc.returncode != 0
    assert status["db_integrity_status"] == "indeterminate"
    assert status["failure_class"] == "db_integrity"
    assert status["failure_stage"] == "integrity_checker_unavailable"


def test_backup_holder_line_written(sandbox):
    """A winning backup records itself in the lock file (holder forensics)."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    holder = (sandbox["home"] / ".genesis" / "locks" / "backup-restore.lock").read_text()
    assert " backup " in holder, holder


def test_restore_lock_timeout_names_holder(sandbox):
    """Restore waits bounded, then dies naming the holder — and records the
    failure in restore_status.json (unlike backup's silent skip)."""
    lock_dir = sandbox["home"] / ".genesis" / "locks"
    lock_dir.mkdir(parents=True)
    lock_file = lock_dir / "backup-restore.lock"
    lock_file.write_text("4242 backup 2026-01-01T00:00:00\n")
    with open(lock_file, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        proc = subprocess.run(
            ["bash", str(_RESTORE), "--dry-run"],
            env=_env(sandbox, GENESIS_RESTORE_LOCK_WAIT="1"),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    assert proc.returncode != 0, proc.stdout
    assert "4242 backup" in proc.stdout and "re-run" in proc.stdout, proc.stdout
    status = json.loads((sandbox["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["success"] is False, status
    assert any("lock" in f for f in status["failures"]), status


def test_restore_uses_global_update_then_dr_lock_order():
    text = _RESTORE.read_text()
    update_lock = text.index('exec {_RESTORE_UPDATE_LOCK_FD}>"$_UPDATE_LOCK_FILE"')
    dr_lock = text.index("dr_lock_open", update_lock)
    assert update_lock < dr_lock


def test_backup_gc_autodetach_disabled(sandbox):
    """A detached auto-gc would inherit the held lock fd past exit — backup
    must pin gc.autoDetach false in the clone."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, _ = _run_backup(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    got = subprocess.run(
        ["git", "config", "gc.autoDetach"],
        cwd=str(sandbox["clone"]),
        capture_output=True,
        text=True,
    )
    assert got.stdout.strip() == "false", got.stdout


# ── SF4: round-trip verify ───────────────────────────────────────────


def test_roundtrip_env_passphrase_verified(sandbox):
    """No escrow → round-trip runs with the env passphrase and passes."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox)
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout
    assert status["success"] is True, status


def test_escrow_drift_degrades_offsite_not_critical(sandbox):
    """Escrow stale but env passphrase decrypts (rotation drift): the LOCAL
    backup still succeeds (not a CRITICAL backup-failure), the fresh SQL dump is
    WITHHELD from the off-site snapshot (a DR box couldn't decrypt it), and a
    distinct re-escrow alert fires instead of 'backup failed'."""
    _install_curl(sandbox, _CURL_HEALTHY)
    escrow = sandbox["home"] / ".genesis" / "shared" / "guardian"
    escrow.mkdir(parents=True)
    (escrow / "backup_passphrase.env").write_text("GENESIS_BACKUP_PASSPHRASE=stale-rotated-away\n")
    proc, status = _run_backup(sandbox)
    assert status["success"] is True, status  # env-decryptable → local OK
    assert status["offsite_confirmed"] is False, status
    files = _offsite_files(sandbox)
    assert not any(f.endswith("data/genesis.sql.gpg") for f in files), files
    assert not any(f.endswith("/COMPLETE") for f in files), (
        files
    )  # no COMPLETE around a withheld dump
    tg = sandbox["tg"].read_text() if sandbox["tg"].exists() else ""
    assert "escrow drift" in tg.lower(), tg
    assert "backup failed" not in tg.lower(), tg


def test_roundtrip_escrow_matching_passes(sandbox):
    """Escrow present and matching the env passphrase → RESTORABLE via the
    escrow-preferred branch; backup succeeds and the SQL ships off-site."""
    _install_curl(sandbox, _CURL_HEALTHY)
    escrow = sandbox["home"] / ".genesis" / "shared" / "guardian"
    escrow.mkdir(parents=True)
    (escrow / "backup_passphrase.env").write_text("GENESIS_BACKUP_PASSPHRASE=testpass\n")
    proc, status = _run_backup(sandbox)
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout
    assert status["success"] is True, status
    files = _offsite_files(sandbox)
    assert any(f.endswith("data/genesis.sql.gpg") for f in files), files


def test_corrupt_sql_fails_and_withheld(sandbox):
    """A dump that won't decrypt with its OWN env passphrase (corruption) fails
    the backup AND is withheld from the off-site snapshot."""
    _install_curl(sandbox, _CURL_HEALTHY)
    # Fail decryption while allowing real encryption. Cover both GPG spellings:
    # the authenticated reader uses --decrypt, while older readers use -d.
    gpg_stub = sandbox["bind"] / "gpg"
    real_gpg = subprocess.run(
        ["bash", "-lc", "command -v gpg"], capture_output=True, text=True
    ).stdout.strip()
    _make_stub(
        gpg_stub,
        f"#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in -d|--decrypt) echo "gpg: decryption failed: Bad session key" >&2; exit 2 ;; esac; done\n'
        f'exec {real_gpg} "$@"\n',
    )
    prior = sandbox["clone"] / "data" / "genesis.sql.gpg"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_bytes(b"last-known-good")
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert "round-trip" in status["failure_reason"], status
    assert prior.read_bytes() == b"last-known-good"
    files = _offsite_files(sandbox)
    assert not any(f.endswith("data/genesis.sql.gpg") for f in files), files


def test_corrupt_source_refuses_before_backup_state_changes(sandbox):
    """A known-corrupt source never replaces local last-good or reaches NAS."""
    _install_curl(sandbox, _CURL_HEALTHY)
    prior = sandbox["clone"] / "data" / "genesis.sql.gpg"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_bytes(b"last-known-good")
    db = sandbox["gd"] / "data" / "genesis.db"
    db.write_bytes(b"not a sqlite database")

    custom_state = sandbox["home"] / "custom-genesis-home"
    proc, status = _run_backup(sandbox, GENESIS_HOME=str(custom_state))

    assert proc.returncode != 0, proc.stdout
    assert status["success"] is False
    assert "source integrity check failed" in status["failure_reason"]
    assert prior.read_bytes() == b"last-known-good"
    assert _offsite_files(sandbox) == []
    assert (custom_state / "db_quarantine.json").exists()
    stopped = sandbox["systemctl_calls"].read_text()
    assert "stop genesis-server.service" in stopped
    assert "stop genesis-bridge.service" in stopped


def test_indeterminate_check_does_not_treat_stale_marker_as_current_corruption(sandbox):
    """A stale marker cannot turn an inode-race result into corruption proof."""
    db = sandbox["gd"] / "data" / "genesis.db"
    marker = sandbox["home"] / ".genesis" / "db_quarantine.json"
    marker.write_text(
        json.dumps(
            {
                "db_path": str(db.resolve()),
                "st_dev": db.stat().st_dev,
                "st_ino": db.stat().st_ino + 1,
            }
        )
    )
    python = sandbox["gd"] / ".venv" / "bin" / "python"
    python.unlink()
    _make_stub(
        python,
        f"#!/usr/bin/env bash\n"
        'case "$*" in *"-m genesis.db.integrity check"*) '
        'echo "UNHEALTHY: database identity changed repeatedly"; exit 2 ;; esac\n'
        f'exec "{sys.executable}" "$@"\n',
    )

    proc, status = _run_backup(sandbox)

    assert proc.returncode != 0
    assert status["db_integrity_status"] == "indeterminate"
    assert not sandbox["systemctl_calls"].exists()


def test_roundtrip_error_detail_sanitized_valid_json(sandbox):
    """A gpg error containing control chars / non-ASCII must not corrupt
    backup_status.json (the health consumer's read_text()+json.loads would
    otherwise crash or swallow the CRITICAL). The status file stays valid JSON
    and parseable — proven by _run_backup already json.loads-ing it."""
    _install_curl(sandbox, _CURL_HEALTHY)
    gpg_stub = sandbox["bind"] / "gpg"
    real_gpg = subprocess.run(
        ["bash", "-lc", "command -v gpg"], capture_output=True, text=True
    ).stdout.strip()
    # Decrypt fails with a tab + CR + non-ASCII (é, 0xE9) in the message.
    _make_stub(
        gpg_stub,
        f"#!/usr/bin/env bash\n"
        'for a in "$@"; do case "$a" in -d|--decrypt) printf "gpg:\\tbad\\rk\\xc3\\xa9y\\n" >&2; exit 2 ;; esac; done\n'
        f'exec {real_gpg} "$@"\n',
    )
    proc, status = _run_backup(sandbox)
    # status is not None ⇒ json.loads succeeded ⇒ valid JSON despite the gpg tab/CR/UTF-8.
    assert status is not None and status["success"] is False, status
    assert "\t" not in status["failure_reason"] and "\r" not in status["failure_reason"], repr(
        status["failure_reason"]
    )


# ── SF3: freshness gate ──────────────────────────────────────────────


def test_qdrant_unreachable_is_loud_non_db_failure(sandbox):
    """Unreachable Qdrant is rebuildable, but the incomplete backup is loud."""
    _install_curl(sandbox, _CURL_DOWN)
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert status["failure_class"] == "non_db"
    assert status["failure_stage"] == "qdrant_probe"
    assert "Qdrant unreachable" in status["failure_reason"], status
    assert "unreachable" in proc.stdout.lower(), proc.stdout


def test_qdrant_reachable_but_snapshot_fails(sandbox):
    """A REACHABLE collection (probe 200) that then fails to snapshot IS the
    audit's real failure — data is present but wasn't captured."""
    _install_curl(sandbox, _CURL_EXISTS_NOSNAP)
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert "Qdrant backup failed" in status["failure_reason"], status


def test_qdrant_absent_is_benign(sandbox):
    """A real 404 (collection genuinely absent — fresh install) stays a
    benign skip: success:true, no Qdrant failure recorded."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert status["success"] is True, status
    assert "Qdrant" not in status["failure_reason"], status


def test_fresh_qdrant_uploaded_offsite(sandbox):
    """Healthy Qdrant → both collections snapshot fresh and land in the
    off-site dated snapshot with a COMPLETE marker."""
    _install_curl(sandbox, _CURL_HEALTHY)
    proc, status = _run_backup(sandbox)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert status["success"] is True, status
    assert status["qdrant_collections"] == 2, status
    files = _offsite_files(sandbox)
    assert any(f.endswith("qdrant/episodic_memory.snapshot.gpg") for f in files), files
    assert any(f.endswith("qdrant/knowledge_base.snapshot.gpg") for f in files), files
    assert any(f.endswith("/COMPLETE") for f in files), files


def test_corruption_backup_update_gate_and_database_restore_e2e(sandbox):
    """Replay the operator path: last-good → corruption → abort → restore."""
    _install_curl(sandbox, _CURL_ABSENT)
    first, first_status = _run_backup(sandbox, GENESIS_BACKUP_RUN_ID="last-good")
    assert first.returncode == 0, first.stdout
    assert first_status["sqlite_backup_verified"] is True

    db = sandbox["gd"] / "data" / "genesis.db"
    db.write_bytes(b"not a sqlite database")
    failed, failed_status = _run_backup(sandbox, GENESIS_BACKUP_RUN_ID="corrupt-run")
    assert failed.returncode != 0
    assert failed_status["db_integrity_status"] == "corrupt"
    assert (sandbox["home"] / ".genesis" / "db_quarantine.json").exists()

    gate = subprocess.run(
        [
            "python3",
            str(_SCRIPTS / "lib" / "backup_status_gate.py"),
            str(sandbox["home"] / ".genesis" / "backup_status.json"),
            "corrupt-run",
        ],
        capture_output=True,
        text=True,
    )
    assert gate.returncode == 2
    assert gate.stdout.startswith("abort_db:")

    _make_stub(
        sandbox["bind"] / "systemctl",
        '#!/usr/bin/env bash\ncase "$*" in *"is-active"*) exit 1 ;; *) exit 0 ;; esac\n',
    )
    # The fixture closes its only SQLite connection before spawning backup; no
    # server starts against this private database. Use that offline boundary
    # rather than depend on host-wide /proc visibility for this recovery test.
    restored = subprocess.run(
        ["bash", str(_RESTORE), "--database-only"],
        env=_env(sandbox, GENESIS_RESTORE_HOLDER_SCAN="none"),
        capture_output=True,
        text=True,
        input="y\n",
    )
    assert restored.returncode == 0, f"{restored.stdout}\n{restored.stderr}"
    assert not (sandbox["home"] / ".genesis" / "db_quarantine.json").exists()
    value = subprocess.run(
        ["sqlite3", str(db), "SELECT x FROM t;"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert value == "1"


def test_stale_qdrant_gpg_excluded_from_offsite(sandbox):
    """A leftover .gpg from a prior run (this run's snapshot failed with the
    collection absent) is EXCLUDED from the new dated snapshot but KEPT
    locally (last-good copy)."""
    _install_curl(sandbox, _CURL_ABSENT)
    stale = sandbox["clone"] / "data" / "qdrant"
    stale.mkdir(parents=True)
    (stale / "episodic_memory.snapshot.gpg").write_bytes(b"old-bytes")
    proc, status = _run_backup(sandbox)
    assert "stale (not regenerated this run)" in proc.stdout, proc.stdout
    files = _offsite_files(sandbox)
    assert not any("episodic_memory" in f for f in files), files
    assert (stale / "episodic_memory.snapshot.gpg").read_bytes() == b"old-bytes"


def test_stale_sql_excluded_from_offsite(sandbox):
    """SQL variant of the freshness gate: no DB this run + a leftover
    genesis.sql.gpg → excluded from the off-site snapshot (and the backup
    already fails via no-SQLite-data)."""
    _install_curl(sandbox, _CURL_ABSENT)
    (sandbox["gd"] / "data" / "genesis.db").unlink()
    data_dir = sandbox["clone"] / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "genesis.sql.gpg").write_bytes(b"old-sql")
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert proc.returncode != 0
    assert "source database not found" in proc.stdout, proc.stdout
    assert (data_dir / "genesis.sql.gpg").read_bytes() == b"old-sql"
    files = _offsite_files(sandbox)
    assert not any(f.endswith("data/genesis.sql.gpg") for f in files), files


# ── SF5: backend timeouts ────────────────────────────────────────────


def _bash_lib(sb, script: str, **envx) -> subprocess.CompletedProcess:
    env = _env(sb, **envx)
    return subprocess.run(
        ["bash", "-c", f'set -uo pipefail; source "{_BACKENDS}"; backend_init; {script}'],
        env=env,
        capture_output=True,
        text=True,
    )


def test_local_put_bounded_by_xfer_timeout(sandbox):
    """A hung transfer (stub cp sleeps past the budget) returns 124, not ∞ —
    the caller's `|| _T2_OK=false` degrades to partial instead of wedging."""
    _make_stub(sandbox["bind"] / "cp", "#!/usr/bin/env bash\nsleep 5\n")
    src = sandbox["tmp"] / "payload"
    src.write_text("x")
    proc = _bash_lib(
        sandbox, f'backend_put "{src}" "dst/payload"; echo "rc=$?"', GENESIS_BACKUP_XFER_TIMEOUT="1"
    )
    assert "rc=124" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


def test_smb_ops_run_under_timeout(sandbox):
    """A hung smbclient is BOUNDED by the ctl tier (nonzero within ~1s — the
    exists pipeline surfaces awk's exit under pipefail, so assert the bound,
    not the literal 124), and put/get escalate to the xfer tier (dynamic-scope
    override observed by the sleep surviving 1s ctl but dying at 2s xfer)."""
    _make_stub(sandbox["bind"] / "smbclient", "#!/usr/bin/env bash\nsleep 10\n")
    proc = _bash_lib(
        sandbox,
        'start=$SECONDS; backend_exists "x"; echo "ctl=$? elapsed=$((SECONDS-start))"',
        GENESIS_BACKUP_TIER2_BACKEND="smb",
        GENESIS_BACKUP_NAS="//nas/share",
        GENESIS_BACKUP_CTL_TIMEOUT="1",
        GENESIS_BACKUP_XFER_TIMEOUT="2",
    )
    assert "ctl=0" not in proc.stdout, f"{proc.stdout}\n{proc.stderr}"
    ctl_elapsed = int(proc.stdout.split("elapsed=")[1].split()[0])
    assert ctl_elapsed <= 4, f"exists must be ctl-bounded (~1s), sleep is 10s: {proc.stdout}"
    src = sandbox["tmp"] / "p2"
    src.write_text("x")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -uo pipefail; source "{_BACKENDS}"; backend_init; '
            f'start=$SECONDS; backend_put "{src}" "d/p2"; rc=$?; '
            'echo "rc=$rc elapsed=$((SECONDS-start))"',
        ],
        env=_env(
            sandbox,
            GENESIS_BACKUP_TIER2_BACKEND="smb",
            GENESIS_BACKUP_NAS="//nas/share",
            GENESIS_BACKUP_CTL_TIMEOUT="1",
            GENESIS_BACKUP_XFER_TIMEOUT="2",
        ),
        capture_output=True,
        text=True,
    )
    assert "rc=124" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"
    elapsed = int(proc.stdout.split("elapsed=")[1].split()[0])
    assert elapsed >= 2, f"put must use the xfer tier, not ctl: {proc.stdout}"


def test_backend_available_local_bounded(sandbox):
    """backend_available's local arm runs under timeout (external test, not
    the unboundable [ -d ] builtin) and still answers correctly."""
    proc = _bash_lib(sandbox, "backend_available && echo yes || echo no")
    assert "yes" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"
    proc = _bash_lib(
        sandbox,
        "backend_available && echo yes || echo no",
        GENESIS_BACKUP_LOCAL_PATH=str(sandbox["tmp"] / "missing"),
    )
    assert "no" in proc.stdout, f"{proc.stdout}\n{proc.stderr}"


# ── regression: the restore escrow refactor keeps its contract ───────


def test_escrow_lib_contract(sandbox):
    """passphrase_escrow_lookup: candidate order, export-prefix tolerance,
    no quote-stripping, always-0 return under set -e."""
    lib = _SCRIPTS / "lib" / "passphrase_escrow.sh"
    escrow = sandbox["home"] / ".genesis" / "shared" / "guardian"
    escrow.mkdir(parents=True)
    (escrow / "backup_passphrase.env").write_text('export GENESIS_BACKUP_PASSPHRASE="quoted"pass\n')
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{lib}"; passphrase_escrow_lookup; '
            'printf "%s|%s" "$ESCROW_PASSPHRASE" "$ESCROW_SOURCE"',
        ],
        env=_env(sandbox),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    val, src = proc.stdout.split("|")
    assert val == '"quoted"pass', f"quotes must survive: {val!r}"
    assert src.endswith("backup_passphrase.env"), src
    # No escrow anywhere → empty result, still exit 0 under set -e.
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; source "{lib}"; passphrase_escrow_lookup; '
            'printf "[%s]" "$ESCROW_PASSPHRASE"; echo ok',
        ],
        env=_env(sandbox, HOME=str(sandbox["tmp"] / "empty-home")),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0 and "[]" in proc.stdout and "ok" in proc.stdout, (
        f"{proc.stdout}\n{proc.stderr}"
    )


# ── Round-trip: streamed decrypt into an in-memory DB, no plaintext file ──

_MEM_OK = {"GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT": "0"}  # independent of the host's free memory
_SENTINEL = "plaintext-sentinel-4b8e21"


def _seed_sentinel(sb) -> None:
    subprocess.run(
        ["sqlite3", str(sb["gd"] / "data" / "genesis.db"), f"INSERT INTO t VALUES('{_SENTINEL}');"],
        check=True,
        capture_output=True,
    )


def _seed_realistic(sb) -> None:
    """Give the sandbox DB the shapes a live Genesis DB has and a one-table DB
    does not: an FTS5 virtual table (its .dump uses writable_schema inserts,
    which sqlite3 -safe's DEFENSIVE flag refuses), an index, a view, and
    enough rows (~400 KB) that the dump outruns a pipe buffer."""
    subprocess.run(
        [
            "sqlite3",
            str(sb["gd"] / "data" / "genesis.db"),
            "CREATE VIRTUAL TABLE docs USING fts5(body);"
            "INSERT INTO docs VALUES ('alpha beta'), ('gamma delta');"
            "CREATE TABLE big(id INTEGER PRIMARY KEY, v TEXT);"
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 4000) "
            "INSERT INTO big SELECT i, printf('%0100d', i) FROM n;"
            "CREATE INDEX big_v ON big(v);"
            "CREATE VIEW big_view AS SELECT id FROM big WHERE id > 10;",
        ],
        check=True,
        capture_output=True,
    )


def _install_decrypt_spy(sb) -> Path:
    """Wrap gpg. Around the real gpg's DECRYPT of the artifact, list the sandbox
    files holding the DB's plaintext before it starts and after all of its
    plaintext has gone into the pipe, and record only the files that appeared
    in between: those belong to the round-trip (the dump step's own temp, which
    a separate change removes, already exists before). Each is tagged with its
    first bytes, so a database file reads "SQLite format 3" and a text dump
    reads as SQL. The live DB files and this stub are skipped."""
    real = shutil.which("gpg")
    log = sb["tmp"] / "decrypt_spy.log"
    log.write_text("")
    before = sb["tmp"] / "decrypt_spy.before"
    db = sb["gd"] / "data" / "genesis.db"
    stub = sb["bind"] / "gpg"
    skip = " ".join(
        f'-e "{p}"' for p in (db, f"{db}-wal", f"{db}-shm", f"{db}-journal", stub, log, before)
    )
    scan = f'grep -rlF "{_SENTINEL}" "{sb["home"]}" "{sb["tmp"]}" 2>/dev/null | grep -vxF {skip} | sort'
    _make_stub(
        stub,
        "#!/usr/bin/env bash\n"
        'case " $* " in *" --decrypt "*)\n'
        f'  {scan} > "{before}"\n'
        f'  "{real}" "$@"; rc=$?; echo DECRYPTED >> "{log}"\n'
        f'  {scan} | comm -13 "{before}" - '
        '| while read -r f; do printf "%s %s\\n" "$(head -c 15 "$f")" "$f"; done'
        f' >> "{log}"\n'
        "  exit $rc ;;\n"
        "esac\n"
        f'exec "{real}" "$@"\n',
    )
    return log


@pytest.mark.parametrize(
    ("extra", "mode"),
    [(_MEM_OK, "in memory"), ({"GENESIS_BACKUP_VERIFY_MEMORY": "off"}, "on disk")],
)
def test_roundtrip_never_writes_the_decrypted_dump(sandbox, extra, mode):
    """The round-trip decrypts the whole dump, but never to a file: in memory
    mode no plaintext file exists at all, and in disk mode the only one is the
    rebuilt database (never the text dump the old code decrypted to). The spy
    sees named files only; sqlite's unlinked sort temps are covered by
    temp_store=MEMORY / SQLITE_TMPDIR in the code."""
    _seed_sentinel(sandbox)
    _seed_realistic(sandbox)
    _install_curl(sandbox, _CURL_ABSENT)
    log = _install_decrypt_spy(sandbox)
    proc, status = _run_backup(sandbox, **extra)
    assert status["success"] is True, (status, proc.stdout)
    assert f"round-trip verify {mode}" in proc.stdout, proc.stdout
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout
    lines = log.read_text().splitlines()
    assert "DECRYPTED" in lines, lines
    found = [line for line in lines if line != "DECRYPTED"]
    if mode == "in memory":
        assert found == [], f"plaintext file during the round-trip: {found}"
    else:
        assert all(line.startswith("SQLite format 3") for line in found), found


def _fake_cgroup(sb, inactive_file: int) -> dict:
    """A cgroup v2 tree whose root cap leaves 1 MiB beyond memory.current."""
    root = sb["tmp"] / "cgfs"
    leaf = root / "a" / "b"
    leaf.mkdir(parents=True)
    (root / "memory.max").write_text(f"{1 << 30}\n")
    (root / "memory.current").write_text(f"{(1 << 30) - (1 << 20)}\n")
    (root / "memory.stat").write_text(f"anon 1\ninactive_file {inactive_file}\nactive_file 5\n")
    for d in (leaf.parent, leaf):
        (d / "memory.max").write_text("max\n")
        (d / "memory.current").write_text("1\n")
    proc_cg = sb["tmp"] / "proc-cgroup"
    proc_cg.write_text("0::/a/b\n")
    return {
        "GENESIS_BACKUP_CGROUP_FS": str(root),
        "GENESIS_BACKUP_PROC_CGROUP": str(proc_cg),
        # The sandbox DB is a few KB; this factor makes it need ~10 MiB.
        "GENESIS_BACKUP_VERIFY_RAM_FACTOR": "999",
        **_MEM_OK,
    }


@pytest.mark.parametrize(
    ("inactive_file", "want"),
    [(0, "memory short"), (512 << 20, "round-trip verify in memory")],
)
def test_roundtrip_cgroup_cap_counts_reclaimable_cache(sandbox, inactive_file, want):
    """A capped cgroup bounds the memory choice, and its cold page cache
    (inactive_file) counts as available: max - current alone reads near zero
    on any long-running box, which would refuse memory every time."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox, **_fake_cgroup(sandbox, inactive_file))
    assert status["success"] is True, status
    assert want in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("extra", "why"),
    [
        ({"GENESIS_BACKUP_VERIFY_MEMORY": "off"}, "GENESIS_BACKUP_VERIFY_MEMORY=off"),
        # The floor alone refuses memory: a tiny DB fits, but keeping all of
        # MemTotal free after the check is impossible.
        ({"GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT": "100"}, "memory short"),
    ],
)
def test_roundtrip_falls_back_to_disk(sandbox, extra, why):
    """Memory forced off or short: rebuild the DB on disk and still pass; the
    log says which and why."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox, **extra)
    assert status["success"] is True, status
    assert "round-trip verify on disk" in proc.stdout, proc.stdout
    assert why in proc.stdout, proc.stdout
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout


def test_roundtrip_memory_failure_retries_on_disk(sandbox):
    """An authenticated artifact whose in-memory rebuild fails (memory charged
    elsewhere since the start-time check) is re-checked on disk; it never
    becomes a CORRUPT verdict by itself."""
    _seed_realistic(sandbox)
    _install_curl(sandbox, _CURL_ABSENT)
    real = shutil.which("sqlite3")
    # Like a real out-of-memory death: stop reading part-way, so the decrypt
    # hits a broken pipe and exits non-zero too.
    _make_stub(
        sandbox["bind"] / "sqlite3",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do [ "$a" = ":memory:" ] && { head -c 65536 > /dev/null; exit 7; }; done\n'
        f'exec "{real}" "$@"\n',
    )
    proc, status = _run_backup(sandbox, **_MEM_OK)
    assert "round-trip failed in memory (sqlite3 exit 7); retrying on disk" in proc.stdout, (
        proc.stdout
    )
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout
    assert status["success"] is True, status


def _dump_stub(sb, body: str) -> None:
    """`sqlite3 <db> .dump` prints body and exits 0; other calls run sqlite3."""
    real = shutil.which("sqlite3")
    _make_stub(
        sb["bind"] / "sqlite3",
        "#!/usr/bin/env bash\n"
        f'if [ "${{2:-}}" = ".dump" ]; then printf "%b" "{body}"; exit 0; fi\n'
        f'exec "{real}" "$@"\n',
    )


@pytest.mark.parametrize("extra", [_MEM_OK, {"GENESIS_BACKUP_VERIFY_MEMORY": "off"}])
def test_roundtrip_rejects_a_dump_that_never_commits(sandbox, extra):
    """A dump cut before COMMIT still encrypts to an authentic artifact. Checked
    in the same session that imported it, its open transaction would look
    complete; the check must fail it, in memory and on disk."""
    _install_curl(sandbox, _CURL_ABSENT)
    _dump_stub(sandbox, "BEGIN TRANSACTION;\\nCREATE TABLE t(x);\\nINSERT INTO t VALUES(1);\\n")
    proc, status = _run_backup(sandbox, **extra)
    assert status["failure_stage"] == "sqlite_roundtrip", status
    assert "round-trip decrypt verified" not in proc.stdout, proc.stdout
    assert not (sandbox["clone"] / "data" / "genesis.sql.gpg").exists()


def test_roundtrip_dump_cannot_run_shell_commands(sandbox):
    """The SQL runs before GPG has authenticated the whole stream, so it runs
    in sqlite3 safe mode: a dot-command in the dump is refused and the check
    fails rather than executing it."""
    _install_curl(sandbox, _CURL_ABSENT)
    marker = sandbox["tmp"] / "PWNED"
    _dump_stub(
        sandbox,
        f"BEGIN TRANSACTION;\\nCREATE TABLE t(x);\\n.shell touch {marker}\\nCOMMIT;\\n",
    )
    proc, status = _run_backup(sandbox, **_MEM_OK)
    assert not marker.exists()
    assert status["failure_stage"] == "sqlite_roundtrip", status


@pytest.mark.parametrize(
    "knob", ["GENESIS_BACKUP_VERIFY_RAM_FACTOR", "GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT"]
)
@pytest.mark.parametrize("value", ["3x", "08", "99999999999999999999"])
def test_roundtrip_bad_ram_knob_does_not_abort(sandbox, knob, value):
    """A non-integer, a leading-zero value (read as decimal, never as a bad
    octal that kills the run under set -e) or an overlong knob never aborts."""
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox, **{**_MEM_OK, knob: value})
    assert status["success"] is True, (status, proc.stderr)
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout


@pytest.mark.parametrize(
    ("extra", "mode"),
    [(_MEM_OK, "in memory"), ({"GENESIS_BACKUP_VERIFY_MEMORY": "off"}, "on disk")],
)
def test_roundtrip_passes_a_realistic_schema(sandbox, extra, mode):
    """FTS5, an index, a view and a dump larger than a pipe buffer all replay
    and pass, in memory and on disk (sqlite3 -safe alone refuses the FTS5
    part, which would fail every live backup)."""
    _seed_realistic(sandbox)
    _install_curl(sandbox, _CURL_ABSENT)
    proc, status = _run_backup(sandbox, **extra)
    assert f"round-trip verify {mode}" in proc.stdout, proc.stdout
    assert "round-trip decrypt verified" in proc.stdout, proc.stdout
    assert status["success"] is True, status


def test_roundtrip_rejects_a_dump_cut_inside_a_view(sandbox):
    """A dump cut inside a final CREATE VIEW, with no terminator, would turn
    the check's BEGIN into a column alias and pass inside the dump's open
    transaction; the check closes the statement first, so it fails."""
    _install_curl(sandbox, _CURL_ABSENT)
    _dump_stub(
        sandbox, "BEGIN TRANSACTION;\\nCREATE TABLE t(x);\\nCREATE VIEW v AS SELECT x FROM t"
    )
    proc, status = _run_backup(sandbox, **_MEM_OK)
    assert status["failure_stage"] == "sqlite_roundtrip", status
    assert "round-trip decrypt verified" not in proc.stdout, proc.stdout


@pytest.mark.parametrize("filler_rows", [0, 6000])
def test_roundtrip_failure_detail_never_quotes_database_content(sandbox, filler_rows):
    """sqlite3's error messages quote the failing SQL line, which is database
    content; the failure reason (status file, alerts) must not carry it. With
    ~500 KB after the bad line, sqlite3 stops reading part-way and the decrypt
    hits a broken pipe: the reason must still name the rebuild, never a failed
    authentication."""
    _install_curl(sandbox, _CURL_ABSENT)
    filler = "INSERT INTO t VALUES('" + "x" * 80 + "');\\n"
    _dump_stub(
        sandbox,
        f"BEGIN TRANSACTION;\\nCREATE TABLE t(x);\\nINSERT INTO t VALUES('{_SENTINEL}') garbage;\\n"
        + filler * filler_rows
        + "COMMIT;\\n",
    )
    proc, status = _run_backup(sandbox, **_MEM_OK)
    assert "authentication" not in status["failure_reason"], status
    assert status["failure_stage"] == "sqlite_roundtrip", status
    assert _SENTINEL not in status["failure_reason"], status
    assert "database rebuild or checks failed" in status["failure_reason"], status


_V1_UNLIMITED = 9223372036854771712  # what an uncapped v1 memory cgroup reports


def _fake_cgroup_v1(sb, *, cap_on: str, inactive_file: int, hybrid: bool) -> dict:
    """A cgroup v1 memory controller with the process in /ct/svc. The 1 GiB
    cap sits on the leaf ("leaf") or on its parent ("parent"), with 1 MiB free
    at that level; the leaf itself uses only 10 MiB. A hybrid host's proc file
    also carries a "0::" line."""
    gib, mib = 1 << 30, 1 << 20
    root = sb["tmp"] / "cgfs1"
    levels = {
        root / "memory": (_V1_UNLIMITED, 5, 0),
        root / "memory" / "ct": (gib, gib - mib, inactive_file)
        if cap_on == "parent"
        else (_V1_UNLIMITED, 20 * mib, 0),
        root / "memory" / "ct" / "svc": (gib, gib - mib, inactive_file)
        if cap_on == "leaf"
        else (_V1_UNLIMITED, 10 * mib, 0),
    }
    for d, (limit, usage, inactive) in levels.items():
        d.mkdir(parents=True, exist_ok=True)
        (d / "memory.limit_in_bytes").write_text(f"{limit}\n")
        (d / "memory.usage_in_bytes").write_text(f"{usage}\n")
        (d / "memory.stat").write_text(f"cache 1\ntotal_inactive_file {inactive}\n")
    proc_cg = sb["tmp"] / "proc-cgroup-v1"
    proc_cg.write_text(
        "12:cpuset:/ct/svc\n7:memory:/ct/svc\n1:name=systemd:/ct/svc\n"
        + ("0::/ct/svc\n" if hybrid else "")
    )
    return {
        "GENESIS_BACKUP_CGROUP_FS": str(root),
        "GENESIS_BACKUP_PROC_CGROUP": str(proc_cg),
        "GENESIS_BACKUP_VERIFY_RAM_FACTOR": "999",
        **_MEM_OK,
    }


@pytest.mark.parametrize("hybrid", [False, True])
@pytest.mark.parametrize(
    ("cap_on", "inactive_file", "want"),
    [
        ("leaf", 0, "memory short"),
        ("leaf", 512 << 20, "round-trip verify in memory"),
        # A cap on an ancestor counts everything under it, not the leaf's usage.
        ("parent", 0, "memory short"),
        ("none", 0, "round-trip verify in memory"),
    ],
)
def test_roundtrip_cgroup_v1_cap_is_read(sandbox, cap_on, inactive_file, want, hybrid):
    """On a cgroup v1 or hybrid host the memory controller's caps bound the
    memory choice, level by level up the tree. A hybrid host also has a "0::"
    line, which must not hide the v1 cap."""
    _install_curl(sandbox, _CURL_ABSENT)
    extra = _fake_cgroup_v1(sandbox, cap_on=cap_on, inactive_file=inactive_file, hybrid=hybrid)
    proc, status = _run_backup(sandbox, **extra)
    assert status["success"] is True, status
    assert want in proc.stdout, proc.stdout


def test_roundtrip_memory_budget_counts_the_wal(sandbox):
    """Committed data still in the WAL is in the dump and the rebuilt DB but
    not in the main file's size, so the budget adds the -wal file: a DB whose
    main file fits the cap but whose WAL does not is checked on disk."""
    import sqlite3 as sqlite

    db = sandbox["gd"] / "data" / "genesis.db"
    con = sqlite.connect(db)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
        con.execute("CREATE TABLE w(v)")
        con.executemany("INSERT INTO w VALUES (?)", [("y" * 1000,)] * 3000)
        con.commit()
        assert (db.parent / "genesis.db-wal").stat().st_size > (2 << 20)
        assert db.stat().st_size < (1 << 20)
        _install_curl(sandbox, _CURL_ABSENT)
        # Headroom 1 MiB + 1 MiB of cold cache: the main file x2 fits, the WAL x2 does not.
        extra = _fake_cgroup(sandbox, 1 << 20)
        extra["GENESIS_BACKUP_VERIFY_RAM_FACTOR"] = "2"
        proc, status = _run_backup(sandbox, **extra)
    finally:
        con.close()
    assert "memory short" in proc.stdout, proc.stdout
    assert status["success"] is True, status
