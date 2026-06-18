"""2a: backup.sh uploads to per-run DATED snapshot dirs on the NAS.

Instead of fixed filenames (overwritten every run), each backup uploads to
``Genesis/<host>/<UTC-stamp>/{data,qdrant,transcripts}/…`` — a consistent
point-in-time snapshot that restore.sh can later pick the latest of, and that
GFS retention can prune. Transcripts are uploaded too (previously local-only).

Sandboxed (HOME + GENESIS_DIR → tmp). Real sqlite3/gpg/git; the smbclient stub
LOGS every ``-c`` command so we can assert the upload paths.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

_BACKUP = Path(__file__).resolve().parents[2] / "scripts" / "backup.sh"
_STAMP_RE = re.compile(r"\d{8}T\d{6}Z")


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
    subprocess.run(["sqlite3", str(gd / "data" / "genesis.db"),
                    "CREATE TABLE t(x); INSERT INTO t VALUES(1);"],
                   check=True, capture_output=True)
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
    smb_log = tmp_path / "smb_commands.log"
    # smbclient stub: log the -c command (the arg after -c), succeed.
    _make_stub(
        bind / "smbclient",
        '#!/usr/bin/env bash\n'
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  [ "$prev" = "-c" ] && printf "%s\\n" "$a" >> "{smb_log}"\n'
        '  prev="$a"\n'
        'done\n'
        'exit 0\n',
    )
    # curl stub: Telegram captured (unused here), everything else fails (Qdrant skipped).
    _make_stub(bind / "curl", '#!/usr/bin/env bash\nexit 1\n')
    return {"home": home, "gd": gd, "bind": bind, "smb_log": smb_log, "tmp": tmp_path}


def _run(backup_env):
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]), GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass", QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_NAS="//nas/share", GENESIS_BACKUP_NAS_USER="u",
        GENESIS_BACKUP_NAS_PASS="p",
        PATH=f'{backup_env["bind"]}:{os.environ["PATH"]}',
    )
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_FORUM_CHAT_ID"):
        env.pop(k, None)
    proc = subprocess.run(["bash", str(_BACKUP)], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    cmds = backup_env["smb_log"].read_text() if backup_env["smb_log"].exists() else ""
    return proc, cmds


def test_sqlite_uploaded_under_dated_snapshot_dir(backup_env):
    """genesis.sql.gpg is put into Genesis/<host>/<stamp>/data, not a fixed path."""
    proc, cmds = _run(backup_env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    put_sql = [ln for ln in cmds.splitlines() if "put" in ln and "genesis.sql.gpg" in ln]
    assert put_sql, f"no SQL upload command logged:\n{cmds}"
    # The cd target for the SQL put must include a dated snapshot dir + /data.
    assert any(_STAMP_RE.search(ln) for ln in put_sql), \
        f"SQL upload not under a dated snapshot dir:\n{put_sql}"
    assert any("/data" in ln for ln in put_sql), f"SQL upload not under .../data:\n{put_sql}"


def test_snapshot_dir_is_created(backup_env):
    """The dated snapshot dir (and its data/qdrant/transcripts subdirs) is mkdir'd."""
    proc, cmds = _run(backup_env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    mkdir_lines = [ln for ln in cmds.splitlines() if "mkdir" in ln]
    assert mkdir_lines, f"no mkdir commands logged:\n{cmds}"
    blob = "\n".join(mkdir_lines)
    assert _STAMP_RE.search(blob), f"no dated snapshot dir created:\n{blob}"
    # All three payload subdirs under the snapshot.
    for sub in ("data", "qdrant", "transcripts"):
        assert sub in blob, f"snapshot subdir '{sub}' not created:\n{blob}"


def _run_local(backup_env, offsite_root: Path):
    """Run backup.sh through the `local` Tier-2 backend (no smbclient stub)."""
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]), GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass", QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_TIER2_BACKEND="local",
        GENESIS_BACKUP_LOCAL_PATH=str(offsite_root),
        PATH=f'{backup_env["bind"]}:{os.environ["PATH"]}',
    )
    for k in ("GENESIS_BACKUP_NAS", "GENESIS_BACKUP_NAS_USER", "GENESIS_BACKUP_NAS_PASS",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_FORUM_CHAT_ID"):
        env.pop(k, None)
    return subprocess.run(["bash", str(_BACKUP)], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_local_backend_writes_dated_snapshot_to_real_fs(backup_env, tmp_path):
    """End-to-end through the `local` backend (no stub): backup.sh writes a REAL
    dated snapshot tree to GENESIS_BACKUP_LOCAL_PATH with the encrypted SQL dump +
    a COMPLETE marker, and reports offsite_confirmed. Proves the Tier-2 abstraction
    is not smb-only — the `local` backend is the regression anchor for the whole
    backup path.
    """
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = _run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    # Exactly one host dir, one dated snapshot, COMPLETE-marked, with the SQL dump.
    host_dirs = list((offsite / "Genesis").iterdir())
    assert len(host_dirs) == 1, f"expected one host dir, got {[d.name for d in host_dirs]}"
    snaps = [d for d in host_dirs[0].iterdir() if _STAMP_RE.fullmatch(d.name)]
    assert len(snaps) == 1, f"expected one dated snapshot, got {[d.name for d in snaps]}"
    snap = snaps[0]
    assert (snap / "data" / "genesis.sql.gpg").is_file(), "SQL dump missing from local snapshot"
    assert (snap / "COMPLETE").is_file(), "COMPLETE marker not written for a full snapshot"

    status = json.loads((backup_env["home"] / ".genesis" / "backup_status.json").read_text())
    assert status["tier2_status"] == "ok", status
    assert status["offsite_confirmed"] is True, status
