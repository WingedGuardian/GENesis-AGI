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
    subprocess.run(
        ["sqlite3", str(gd / "data" / "genesis.db"), "CREATE TABLE t(x); INSERT INTO t VALUES(1);"],
        check=True,
        capture_output=True,
    )
    # A2a inputs: auto-memory (flat), a config overlay, and a secrets file — so the
    # off-site snapshot can include them alongside data/qdrant/transcripts.
    cc_id = str(gd).replace("/", "-")
    mem_dir = home / ".claude" / "projects" / cc_id / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "note.md").write_text("remembered\n")
    # A dotfile (e.g. a transient consolidate lock) — §4 stages it via `find`, so the
    # off-site upload must mirror it too (a shell glob would silently skip leading-dot names).
    (mem_dir / ".consolidate-lock").write_text("12345\n")
    (gd / "config").mkdir(parents=True)
    (gd / "config" / "sample.local.yaml").write_text("key: val\n")
    (gd / "secrets.env").write_text("FOO=bar\n")  # sourced by backup.sh; innocuous
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
        "#!/usr/bin/env bash\n"
        'prev=""\n'
        'for a in "$@"; do\n'
        f'  [ "$prev" = "-c" ] && printf "%s\\n" "$a" >> "{smb_log}"\n'
        '  prev="$a"\n'
        "done\n"
        "exit 0\n",
    )
    # curl stub: SF3 existence probe answers 404 (collections genuinely absent
    # → benign skip; a bare connection failure now FAILS the backup);
    # everything else fails (no Telegram asserted here).
    _make_stub(
        bind / "curl",
        "#!/usr/bin/env bash\n"
        'for a in "$@"; do [ "$a" = "-w" ] && { printf "404"; exit 0; }; done\n'
        "exit 1\n",
    )
    return {"home": home, "gd": gd, "bind": bind, "smb_log": smb_log, "tmp": tmp_path}


def _run(backup_env):
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]),
        GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_NAS="//nas/share",
        GENESIS_BACKUP_NAS_USER="u",
        GENESIS_BACKUP_NAS_PASS="p",
        PATH=f"{backup_env['bind']}:{os.environ['PATH']}",
    )
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_FORUM_CHAT_ID"):
        env.pop(k, None)
    proc = subprocess.run(
        ["bash", str(_BACKUP)], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    cmds = backup_env["smb_log"].read_text() if backup_env["smb_log"].exists() else ""
    return proc, cmds


def test_sqlite_uploaded_under_dated_snapshot_dir(backup_env):
    """genesis.sql.gpg is put into Genesis/<host>/<stamp>/data, not a fixed path."""
    proc, cmds = _run(backup_env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    put_sql = [ln for ln in cmds.splitlines() if "put" in ln and "genesis.sql.gpg" in ln]
    assert put_sql, f"no SQL upload command logged:\n{cmds}"
    # The cd target for the SQL put must include a dated snapshot dir + /data.
    assert any(_STAMP_RE.search(ln) for ln in put_sql), (
        f"SQL upload not under a dated snapshot dir:\n{put_sql}"
    )
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
        HOME=str(backup_env["home"]),
        GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_TIER2_BACKEND="local",
        GENESIS_BACKUP_LOCAL_PATH=str(offsite_root),
        PATH=f"{backup_env['bind']}:{os.environ['PATH']}",
    )
    for k in (
        "GENESIS_BACKUP_NAS",
        "GENESIS_BACKUP_NAS_USER",
        "GENESIS_BACKUP_NAS_PASS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_FORUM_CHAT_ID",
    ):
        env.pop(k, None)
    return subprocess.run(
        ["bash", str(_BACKUP)], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )


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


def test_local_backend_snapshot_includes_memory_config_secrets(backup_env, tmp_path):
    """A2a: the off-site snapshot must ALSO contain memory/, config_overrides/, and
    secrets/ (previously git-Tier-1 only), so the destination is a COMPLETE copy and a
    no-git fresh-box DR works. Real `local` backend, real fs."""
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = _run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    host_dirs = list((offsite / "Genesis").iterdir())
    assert len(host_dirs) == 1, f"expected one host dir, got {[d.name for d in host_dirs]}"
    snaps = [d for d in host_dirs[0].iterdir() if _STAMP_RE.fullmatch(d.name)]
    assert len(snaps) == 1, f"expected one dated snapshot, got {[d.name for d in snaps]}"
    snap = snaps[0]

    # memory: at least one flat .gpg under the snapshot's memory/.
    mem_gpgs = list((snap / "memory").glob("*.gpg")) if (snap / "memory").is_dir() else []
    assert mem_gpgs, f"no memory/*.gpg in the off-site snapshot: {[d.name for d in snap.iterdir()]}"
    # config_overrides: the overlay shipped as-is (plaintext, mirrors Tier-1).
    assert (snap / "config_overrides" / "sample.local.yaml").is_file(), (
        f"config overlay missing from off-site snapshot: {[d.name for d in snap.iterdir()]}"
    )
    # secrets: the encrypted secrets payload.
    assert (snap / "secrets" / "secrets.env.gpg").is_file(), (
        f"secrets payload missing from off-site snapshot: {[d.name for d in snap.iterdir()]}"
    )
    # COMPLETE still written (and only because all three landed too).
    assert (snap / "COMPLETE").is_file(), "COMPLETE marker not written"


def test_offsite_memory_includes_dotfiles(backup_env, tmp_path):
    """The off-site memory upload must include DOTFILES (e.g. .consolidate-lock.gpg) so the
    snapshot is a faithful mirror of Tier-1 (§4 stages dotfiles via `find`). A shell glob
    `memory/*.gpg` silently skips leading-dot names; the upload must enumerate like §4."""
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = _run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    host = next((offsite / "Genesis").iterdir())
    snaps = [d for d in host.iterdir() if _STAMP_RE.fullmatch(d.name)]
    assert len(snaps) == 1, [d.name for d in snaps]
    mem = snaps[0] / "memory"
    assert (mem / ".consolidate-lock.gpg").is_file(), (
        f"dotfile dropped from off-site memory mirror: {sorted(p.name for p in mem.iterdir())}"
    )
    assert (mem / "note.md.gpg").is_file()  # regular file still mirrored (no regression)


def test_large_temp_goes_to_dedicated_dir_not_inherited_tmpdir(backup_env, tmp_path):
    """The big SQLite .dump must be created in a dedicated dir (GENESIS_BACKUP_TMPDIR,
    default ~/tmp) — NOT the inherited TMPDIR, which in a CC session is the watchgod-
    policed ~/.genesis/cc-tmp 'oxygen' folder. Regression for the 2026-06-18 incident:
    a 269MB dump via bare `mktemp` filled cc-tmp and the watchgod killed CC sessions.
    The seeded `tmp_filesystem_limit` procedure already mandates ~/tmp for large temp."""
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    cctmp = tmp_path / "cc-tmp-sentinel"  # stand-in for the watchgod "oxygen" folder
    cctmp.mkdir()
    bigtmp = tmp_path / "dedicated-big-tmp"
    bigtmp.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]),
        GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_TIER2_BACKEND="local",
        GENESIS_BACKUP_LOCAL_PATH=str(offsite),
        TMPDIR=str(cctmp),  # inherited — MUST NOT be used for the big dump
        GENESIS_BACKUP_TMPDIR=str(bigtmp),  # the dedicated dir the dump MUST use
        PATH=f"{backup_env['bind']}:{os.environ['PATH']}",
    )
    for k in (
        "GENESIS_BACKUP_NAS",
        "GENESIS_BACKUP_NAS_USER",
        "GENESIS_BACKUP_NAS_PASS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_FORUM_CHAT_ID",
    ):
        env.pop(k, None)
    proc = subprocess.run(
        ["bash", str(_BACKUP)], env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    # The script announces where large temp goes; it must be the dedicated dir, not cc-tmp.
    assert f"big-temp dir: {bigtmp}" in proc.stdout, (
        f"backup did not route large temp to the dedicated dir (expected {bigtmp}):\n{proc.stdout}"
    )


# --------------------------------------------------------------------------- #
# GFS retention prune (D4) — off-site dated snapshots
# --------------------------------------------------------------------------- #


def _seed_offsite_snapshot(host_dir: Path, stamp: str, *, complete: bool) -> None:
    snap = host_dir / stamp
    (snap / "data").mkdir(parents=True)
    (snap / "data" / "genesis.sql.gpg").write_text("x")
    (snap / "transcripts").mkdir()
    (snap / "transcripts" / "t.jsonl.gpg").write_text("x")
    if complete:
        (snap / "COMPLETE").write_text("")


def test_gfs_prune_keeps_buckets_never_latest_or_incomplete_or_transcripts(backup_env, tmp_path):
    """backup.sh's GFS prune keeps daily/weekly/monthly + the run's latest COMPLETE, deletes
    the rest, NEVER touches an incomplete (in-flight) snapshot, and every retained snapshot
    keeps its transcripts/. Real `local` backend, real fs (the SMB path shares this dispatch;
    it is verified manually at deploy)."""
    from datetime import UTC, datetime, timedelta

    host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
    offsite = tmp_path / "offsite"
    host_dir = offsite / "Genesis" / host
    host_dir.mkdir(parents=True)

    now = datetime.now(UTC)
    # 200 daily COMPLETE snapshots (day-1 .. day-200), all older than the run's new one.
    seeded = []
    for d in range(1, 201):
        s = (now - timedelta(days=d)).strftime("%Y%m%dT%H%M%SZ")
        _seed_offsite_snapshot(host_dir, s, complete=True)
        seeded.append(s)
    # An INCOMPLETE (no COMPLETE) snapshot — GFS must never consider or delete it.
    incomplete = (now - timedelta(days=149, hours=3)).strftime("%Y%m%dT%H%M%SZ")
    _seed_offsite_snapshot(host_dir, incomplete, complete=False)

    proc = _run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"

    remaining = {d.name for d in host_dir.iterdir() if _STAMP_RE.fullmatch(d.name)}
    # the run wrote a new (newest) COMPLETE snapshot — it must survive the prune.
    new_stamps = remaining - set(seeded) - {incomplete}
    assert len(new_stamps) == 1, f"expected exactly one new snapshot, got {new_stamps}"
    assert (host_dir / next(iter(new_stamps)) / "COMPLETE").is_file()
    # GFS keeps at most daily7 + weekly4 + monthly6 of the COMPLETE snapshots (overlaps -> fewer).
    complete_remaining = {s for s in remaining if (host_dir / s / "COMPLETE").is_file()}
    assert len(complete_remaining) <= 7 + 4 + 6
    # a very old, non-boundary snapshot is pruned.
    assert (now - timedelta(days=199)).strftime("%Y%m%dT%H%M%SZ") not in remaining
    # the incomplete snapshot is NEVER pruned (no COMPLETE -> not eligible).
    assert incomplete in remaining, "GFS must not delete an incomplete/in-flight snapshot"
    # every retained snapshot keeps its transcripts/.
    for s in complete_remaining:
        assert (host_dir / s / "transcripts").is_dir(), f"{s} lost its transcripts/"
