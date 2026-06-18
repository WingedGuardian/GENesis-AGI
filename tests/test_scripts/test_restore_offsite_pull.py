"""restore.sh pulls the latest COMPLETE off-site snapshot via the pluggable backend.

On a fresh DR box the SQLite dump / Qdrant / transcripts live ONLY off-site
(gitignored from Tier-1). restore.sh must pull the latest *complete* dated snapshot
(a half-uploaded snapshot from a crashed backup must be skipped) before restoring.

These exercise the REAL end-to-end pull→decrypt→restore through the `local` backend
against a real off-site filesystem (no smbclient stub): a genuine GPG-encrypted dump
is staged under Genesis/<host>/<stamp>/data and restore.sh selects + decrypts +
`.read`s it. Fully sandboxed (HOME + GENESIS_DIR → tmp); systemctl stubbed so the
restore's server-quiesce is a no-op. The `local` backend is the backend-agnostic
proof that the off-site selection logic (latest-complete / incomplete-skip / host
override / sole-host auto-detect) is not smb-specific.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"
_OLD = "20260615T180000Z"
_NEW = "20260617T180000Z"  # the latest


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / ".gnupg").mkdir(mode=0o700)
    backup = tmp_path / "backup"   # empty → the off-site pull is what stages payloads
    backup.mkdir()
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    bind = tmp_path / "bin"
    bind.mkdir()
    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")  # server inactive

    # A genuine GPG-encrypted SQL dump (restored content: SELECT x -> 99).
    sql = tmp_path / "dump.sql"
    sql.write_text("CREATE TABLE t(x);\nINSERT INTO t VALUES(99);\n")
    dump_gpg = tmp_path / "genesis.sql.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(home / ".gnupg"),
                    "--passphrase", "testpass", "--symmetric", "--cipher-algo", "AES256",
                    "-o", str(dump_gpg), str(sql)], check=True, capture_output=True)

    def _enc(text: str, name: str) -> Path:
        plain = tmp_path / f"{name}.plain"
        plain.write_text(text)
        out = tmp_path / name
        subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(home / ".gnupg"),
                        "--passphrase", "testpass", "--symmetric", "--cipher-algo", "AES256",
                        "-o", str(out), str(plain)], check=True, capture_output=True)
        return out

    # A2a extra payloads: memory + secrets encrypted, config plaintext.
    mem_gpg = _enc("remembered\n", "note.md.gpg")
    sec_gpg = _enc("SECRET=xyz\n", "secrets.env.gpg")

    return {"home": home, "gd": gd, "backup": backup, "offsite": offsite,
            "bind": bind, "dump_gpg": dump_gpg, "mem_gpg": mem_gpg, "sec_gpg": sec_gpg}


def _snapshot(sandbox, host: str, stamp: str, *, complete: bool = True,
              with_extras: bool = False) -> None:
    """Create an off-site dated snapshot on the real filesystem."""
    snap = sandbox["offsite"] / "Genesis" / host / stamp
    (snap / "data").mkdir(parents=True)
    (snap / "data" / "genesis.sql.gpg").write_bytes(sandbox["dump_gpg"].read_bytes())
    if with_extras:
        # A2a: memory/ (flat .gpg), config_overrides/ (plaintext yaml), secrets/ (encrypted).
        (snap / "memory").mkdir()
        (snap / "memory" / "note.md.gpg").write_bytes(sandbox["mem_gpg"].read_bytes())
        (snap / "config_overrides").mkdir()
        (snap / "config_overrides" / "sample.local.yaml").write_text("key: val\n")
        (snap / "secrets").mkdir()
        (snap / "secrets" / "secrets.env.gpg").write_bytes(sandbox["sec_gpg"].read_bytes())
    if complete:
        (snap / "COMPLETE").write_text("")


def _run(sandbox, *, backend="local", host_override=None):
    env = dict(os.environ)
    env.update(
        HOME=str(sandbox["home"]), GENESIS_DIR=str(sandbox["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass", QDRANT_URL="http://127.0.0.1:1",
        PATH=f'{sandbox["bind"]}:{os.environ["PATH"]}',
    )
    if backend == "local":
        env["GENESIS_BACKUP_TIER2_BACKEND"] = "local"
        env["GENESIS_BACKUP_LOCAL_PATH"] = str(sandbox["offsite"])
    else:
        env["GENESIS_BACKUP_TIER2_BACKEND"] = backend
    if host_override is not None:
        env["GENESIS_BACKUP_NAS_HOST"] = host_override
    return subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sandbox["backup"]), "--force"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _db_value(sandbox) -> str:
    out = subprocess.run(["sqlite3", str(sandbox["gd"] / "data" / "genesis.db"),
                          "SELECT x FROM t;"], capture_output=True, text=True)
    return out.stdout.strip()


def test_pulls_latest_complete_and_restores_db(sandbox):
    """Two COMPLETE snapshots → pull the newest → decrypt → restore the DB."""
    _snapshot(sandbox, "sourcebox", _OLD)
    _snapshot(sandbox, "sourcebox", _NEW)
    proc = _run(sandbox, host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _db_value(sandbox) == "99", "DB not restored from the latest off-site snapshot"
    assert _NEW in proc.stdout and "pulling latest snapshot" in proc.stdout, proc.stdout


def test_skips_incomplete_latest_snapshot(sandbox):
    """Newest snapshot has no COMPLETE marker (crashed mid-upload) → fall back to
    the previous COMPLETE one; never a half-uploaded snapshot."""
    _snapshot(sandbox, "sourcebox", _OLD, complete=True)
    _snapshot(sandbox, "sourcebox", _NEW, complete=False)  # incomplete latest
    proc = _run(sandbox, host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _db_value(sandbox) == "99"
    assert f"pulling latest snapshot {_OLD}" in proc.stdout, (
        f"did not fall back to the older COMPLETE snapshot:\n{proc.stdout}")


def test_no_complete_snapshot_skips_pull(sandbox):
    """No COMPLETE snapshot anywhere → pull skipped, no crash."""
    _snapshot(sandbox, "sourcebox", _NEW, complete=False)
    proc = _run(sandbox, host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "no COMPLETE dated snapshot" in proc.stdout, proc.stdout


def test_explicit_host_override(sandbox):
    """Fresh box whose hostname differs: GENESIS_BACKUP_NAS_HOST points the pull at
    the SOURCE host's snapshots."""
    _snapshot(sandbox, "sourcebox", _NEW)
    proc = _run(sandbox, host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _db_value(sandbox) == "99"


def test_autodetects_sole_host(sandbox):
    """Hostname has no snapshots but exactly one host dir exists → auto-detect it,
    so fresh-box DR works without setting the host name."""
    _snapshot(sandbox, "onlyhost", _NEW)
    proc = _run(sandbox, host_override="ghost")  # 'ghost' has no snapshots
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "using the only host: onlyhost" in proc.stdout, proc.stdout
    assert _db_value(sandbox) == "99"


def test_autodetect_ignores_stray_file_under_genesis(sandbox):
    """A stray FILE under Genesis/ must NOT be mistaken for a host dir — the
    sole-host auto-detect must still find the one real host. (Regression: the
    generic backend_list returned files+dirs, corrupting the host count; the fix
    uses a dir-only listing.)"""
    _snapshot(sandbox, "onlyhost", _NEW)
    (sandbox["offsite"] / "Genesis" / "stray.txt").write_text("junk")
    proc = _run(sandbox, host_override="ghost")  # 'ghost' has no snapshots
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "using the only host: onlyhost" in proc.stdout, (
        f"stray file under Genesis/ broke sole-host auto-detect:\n{proc.stdout}")
    assert _db_value(sandbox) == "99"


def test_no_backend_configured_skips_pull(sandbox):
    """backend=none → no off-site pull, no payload, no crash."""
    _snapshot(sandbox, "sourcebox", _NEW)
    proc = _run(sandbox, backend="none", host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # nothing pulled → restore finds no SQL payload
    assert "no backup payload" in proc.stdout.lower() or _db_value(sandbox) != "99"


def test_pulls_and_rehydrates_memory_config_secrets(sandbox):
    """A2a: on a no-git fresh box, restore must pull memory/config/secrets from the
    off-site snapshot (not just data/qdrant/transcripts) and rehydrate them — so DR
    works without the Tier-1 git clone."""
    _snapshot(sandbox, "sourcebox", _NEW, with_extras=True)
    proc = _run(sandbox, host_override="sourcebox")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    cc_id = str(sandbox["gd"]).replace("/", "-")
    mem_file = sandbox["home"] / ".claude" / "projects" / cc_id / "memory" / "note.md"
    assert mem_file.is_file() and mem_file.read_text() == "remembered\n", \
        f"memory not rehydrated from off-site:\n{proc.stdout}"
    cfg_file = sandbox["gd"] / "config" / "sample.local.yaml"
    assert cfg_file.is_file() and cfg_file.read_text() == "key: val\n", \
        f"config overlay not rehydrated from off-site:\n{proc.stdout}"
    sec_file = sandbox["gd"] / "secrets.env"
    assert sec_file.is_file() and sec_file.read_text() == "SECRET=xyz\n", \
        f"secrets not rehydrated from off-site:\n{proc.stdout}"
    assert oct(sec_file.stat().st_mode)[-3:] == "600", "secrets not chmod 0600"
