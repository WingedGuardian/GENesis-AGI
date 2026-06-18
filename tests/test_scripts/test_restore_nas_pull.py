"""2a: restore.sh pulls the latest off-site (NAS) snapshot before restoring.

On a fresh DR box the SQLite dump / Qdrant / transcripts live ONLY on the NAS
(gitignored from Tier-1). restore.sh must pull the latest dated snapshot from
the NAS into BACKUP_DIR so the restore can proceed. This is the gap that made
off-site recovery impossible.

Real E2E: a genuine GPG-encrypted SQL dump is served by the smbclient stub on
`get`, so the full pull→decrypt→.read chain runs (real sqlite3 + gpg). Fully
sandboxed (HOME + GENESIS_DIR → tmp); systemctl + smbclient stubbed.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"
_OLD_STAMP = "20260615T180000Z"
_NEW_STAMP = "20260617T180000Z"  # the latest — restore must pick this one


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def nas_sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / ".gnupg").mkdir(mode=0o700)
    backup = tmp_path / "backup"            # fresh-box BACKUP_DIR: no data/ locally
    backup.mkdir()
    bind = tmp_path / "bin"
    bind.mkdir()
    smb_log = tmp_path / "smb.log"

    # A real GPG-encrypted SQL dump the NAS stub will serve on `get`.
    sql = tmp_path / "dump.sql"
    sql.write_text("CREATE TABLE t(x);\nINSERT INTO t VALUES(99);\n")
    dump_gpg = tmp_path / "genesis.sql.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(home / ".gnupg"),
                    "--passphrase", "testpass", "--symmetric", "--cipher-algo",
                    "AES256", "-o", str(dump_gpg), str(sql)], check=True, capture_output=True)

    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")  # server inactive
    # smbclient stub: serve the host-dir listing (two dated snapshots), copy the
    # encrypted dump on `get`, and report empty qdrant/transcripts listings.
    _make_stub(
        bind / "smbclient",
        '#!/usr/bin/env bash\n'
        'cmd=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-c" ] && cmd="$a"; prev="$a"; done\n'
        f'printf "%s\\n" "$cmd" >> "{smb_log}"\n'
        'case "$cmd" in\n'
        '  *"get genesis.sql.gpg"*)\n'
        '      dest=$(printf "%s" "$cmd" | sed -E \'s/.*get genesis.sql.gpg +"([^"]*)".*/\\1/\')\n'
        '      mkdir -p "$(dirname "$dest")"\n'
        f'      cp "{dump_gpg}" "$dest" ;;\n'
        '  *"/qdrant; ls"*|*"/transcripts; ls"*) : ;;\n'
        f'  *ls*) printf "  {_OLD_STAMP}  D  0  x\\n  {_NEW_STAMP}  D  0  x\\n" ;;\n'
        'esac\n'
        'exit 0\n',
    )
    return {"home": home, "gd": gd, "backup": backup, "bind": bind,
            "smb_log": smb_log, "tmp": tmp_path}


def _run(nas_sandbox, *, nas=True):
    env = dict(os.environ)
    env.update(
        HOME=str(nas_sandbox["home"]), GENESIS_DIR=str(nas_sandbox["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass", QDRANT_URL="http://127.0.0.1:1",
        PATH=f'{nas_sandbox["bind"]}:{os.environ["PATH"]}',
    )
    if nas:
        env.update(GENESIS_BACKUP_NAS="//nas/share",
                   GENESIS_BACKUP_NAS_USER="u", GENESIS_BACKUP_NAS_PASS="p")
    else:
        for k in ("GENESIS_BACKUP_NAS", "GENESIS_BACKUP_NAS_USER", "GENESIS_BACKUP_NAS_PASS"):
            env.pop(k, None)
    return subprocess.run(
        ["bash", str(_RESTORE), "--from", str(nas_sandbox["backup"]), "--force"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def _smb_cmds(nas_sandbox) -> str:
    return nas_sandbox["smb_log"].read_text() if nas_sandbox["smb_log"].exists() else ""


def test_restore_pulls_latest_snapshot_and_restores_db(nas_sandbox):
    """End-to-end: pull the latest NAS snapshot's dump → decrypt → restore the DB."""
    proc = _run(nas_sandbox, nas=True)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The restored DB holds the dump's content (proves pull→decrypt→.read worked).
    db = nas_sandbox["gd"] / "data" / "genesis.db"
    out = subprocess.run(["sqlite3", str(db), "SELECT x FROM t;"],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "99", f"DB not restored from the NAS dump: {out.stdout!r}"
    # And it pulled from the LATEST snapshot, not the older one.
    cmds = _smb_cmds(nas_sandbox)
    got = [ln for ln in cmds.splitlines() if "get genesis.sql.gpg" in ln]
    assert got, f"no SQL get issued:\n{cmds}"
    assert all(_NEW_STAMP in ln for ln in got), f"pulled a non-latest snapshot:\n{got}"
    assert all(_OLD_STAMP not in ln for ln in got), f"pulled the OLD snapshot:\n{got}"


def test_no_nas_configured_skips_pull(nas_sandbox):
    """Without a NAS target, no pull happens and the (absent) local payload just
    yields 'no backup payload' — no crash, no smbclient calls."""
    proc = _run(nas_sandbox, nas=False)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _smb_cmds(nas_sandbox).strip() == "", "smbclient called without a NAS target"
    assert "no backup payload" in proc.stdout.lower()
