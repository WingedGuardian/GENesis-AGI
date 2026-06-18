"""2a: restore.sh pulls the latest COMPLETE off-site (NAS) snapshot.

On a fresh DR box the SQLite dump / Qdrant / transcripts live ONLY on the NAS
(gitignored from Tier-1). restore.sh must pull the latest *complete* dated
snapshot (a half-uploaded snapshot from a crashed backup must be skipped) into
BACKUP_DIR before restoring.

Real E2E: a genuine GPG-encrypted dump is served by the smbclient stub on `get`,
so the full pull→decrypt→.read chain runs. Fully sandboxed (HOME + GENESIS_DIR →
tmp); systemctl + smbclient stubbed. The stub treats snapshots named in
``COMPLETE_STAMPS`` (env) as having a COMPLETE marker.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_RESTORE = Path(__file__).resolve().parents[2] / "scripts" / "restore.sh"
_OLD_STAMP = "20260615T180000Z"
_NEW_STAMP = "20260617T180000Z"  # the latest


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
    backup = tmp_path / "backup"
    backup.mkdir()
    bind = tmp_path / "bin"
    bind.mkdir()
    smb_log = tmp_path / "smb.log"

    sql = tmp_path / "dump.sql"
    sql.write_text("CREATE TABLE t(x);\nINSERT INTO t VALUES(99);\n")
    dump_gpg = tmp_path / "genesis.sql.gpg"
    subprocess.run(["gpg", "--batch", "--yes", "--homedir", str(home / ".gnupg"),
                    "--passphrase", "testpass", "--symmetric", "--cipher-algo",
                    "AES256", "-o", str(dump_gpg), str(sql)], check=True, capture_output=True)
    qfile = tmp_path / "qsnap.gpg"
    qfile.write_text("QDRANT-SNAPSHOT-BYTES")  # served verbatim; not restored here

    _make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")  # server inactive
    # smbclient stub. Dispatches on the -c command:
    #   get genesis.sql.gpg  → cp the real encrypted dump
    #   get <qfile>          → cp the qdrant file
    #   ls COMPLETE          → echo COMPLETE iff this stamp is in $COMPLETE_STAMPS
    #   <snap>/qdrant; ls    → list a qdrant file (so the get loop is exercised)
    #   <snap>/transcripts;ls→ empty
    #   <host>; ls           → the two dated snapshot dirs
    _make_stub(
        bind / "smbclient",
        '#!/usr/bin/env bash\n'
        'cmd=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-c" ] && cmd="$a"; prev="$a"; done\n'
        f'printf "%s\\n" "$cmd" >> "{smb_log}"\n'
        'case "$cmd" in\n'
        '  *"get genesis.sql.gpg"*)\n'
        '      dest=$(printf "%s" "$cmd" | sed -E \'s/.*get genesis.sql.gpg +"([^"]*)".*/\\1/\')\n'
        f'      mkdir -p "$(dirname "$dest")"; cp "{dump_gpg}" "$dest" ;;\n'
        '  *"get \\"qsnap.gpg\\""*)\n'
        '      dest=$(printf "%s" "$cmd" | sed -E \'s/.*get "qsnap.gpg" +"([^"]*)".*/\\1/\')\n'
        f'      mkdir -p "$(dirname "$dest")"; cp "{qfile}" "$dest" ;;\n'
        '  *"ls COMPLETE"*)\n'
        '      for s in ${COMPLETE_STAMPS:-}; do case "$cmd" in *"$s"*) echo "  COMPLETE  A  0  x"; break ;; esac; done ;;\n'
        '  *"/qdrant"*) [ "${SERVE_QDRANT:-0}" = 1 ] && echo "  qsnap.gpg  A  10  x" || : ;;\n'
        '  *"/transcripts"*) : ;;\n'
        f'  *ls*) printf "  {_OLD_STAMP}  D  0  x\\n  {_NEW_STAMP}  D  0  x\\n" ;;\n'
        'esac\n'
        'exit 0\n',
    )
    return {"home": home, "gd": gd, "backup": backup, "bind": bind,
            "smb_log": smb_log, "tmp": tmp_path}


def _run(nas_sandbox, *, nas=True, complete=(_OLD_STAMP, _NEW_STAMP), serve_qdrant=False):
    env = dict(os.environ)
    env.update(
        HOME=str(nas_sandbox["home"]), GENESIS_DIR=str(nas_sandbox["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass", QDRANT_URL="http://127.0.0.1:1",
        COMPLETE_STAMPS=" ".join(complete), SERVE_QDRANT="1" if serve_qdrant else "0",
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


def test_restore_pulls_latest_complete_snapshot_and_restores_db(nas_sandbox):
    """E2E: pull the latest COMPLETE snapshot's dump → decrypt → restore the DB."""
    proc = _run(nas_sandbox, complete=(_OLD_STAMP, _NEW_STAMP))
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    db = nas_sandbox["gd"] / "data" / "genesis.db"
    out = subprocess.run(["sqlite3", str(db), "SELECT x FROM t;"], capture_output=True, text=True)
    assert out.stdout.strip() == "99", f"DB not restored from the NAS dump: {out.stdout!r}"
    got = [ln for ln in _smb_cmds(nas_sandbox).splitlines() if "get genesis.sql.gpg" in ln]
    assert got and all(_NEW_STAMP in ln for ln in got), f"didn't pull from the latest snapshot:\n{got}"


def test_restore_pulls_qdrant_files_from_snapshot(nas_sandbox):
    """The qdrant pull loop fetches each *.gpg into BACKUP_DIR/data/qdrant.
    (Exit code isn't checked: staging a qdrant file makes restore.sh probe the
    test's dead Qdrant and warn — irrelevant to the pull itself.)"""
    _run(nas_sandbox, complete=(_NEW_STAMP,), serve_qdrant=True)
    staged = nas_sandbox["backup"] / "data" / "qdrant" / "qsnap.gpg"
    assert staged.exists() and staged.read_text() == "QDRANT-SNAPSHOT-BYTES", \
        "qdrant snapshot not pulled from the NAS"


def test_restore_skips_incomplete_latest_snapshot(nas_sandbox):
    """If the newest snapshot has no COMPLETE marker (crashed mid-upload), restore
    falls back to the previous COMPLETE one — never a half-uploaded snapshot."""
    proc = _run(nas_sandbox, complete=(_OLD_STAMP,))  # newest is INCOMPLETE
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    got = [ln for ln in _smb_cmds(nas_sandbox).splitlines() if "get genesis.sql.gpg" in ln]
    assert got and all(_OLD_STAMP in ln for ln in got), f"didn't fall back to the older complete snapshot:\n{got}"
    assert all(_NEW_STAMP not in ln for ln in got), f"pulled the incomplete latest snapshot:\n{got}"
    assert "incomplete" in proc.stdout.lower()


def test_no_complete_snapshot_skips_pull(nas_sandbox):
    """No COMPLETE snapshot anywhere → pull skipped, no crash."""
    proc = _run(nas_sandbox, complete=())  # none complete
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "no complete dated snapshot" in proc.stdout.lower()


def test_no_nas_configured_skips_pull(nas_sandbox):
    """Without a NAS target, no pull happens and there are no smbclient calls."""
    proc = _run(nas_sandbox, nas=False)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert _smb_cmds(nas_sandbox).strip() == "", "smbclient called without a NAS target"
    assert "no backup payload" in proc.stdout.lower()
