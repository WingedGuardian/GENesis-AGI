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
import subprocess
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


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    (home / ".genesis").mkdir(parents=True)
    (home / ".gnupg").mkdir(mode=0o700)
    (home / "tmp").mkdir()
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
    tg = tmp_path / "telegram_calls.log"
    return {
        "home": home,
        "gd": gd,
        "bind": bind,
        "tg": tg,
        "offsite": offsite,
        "clone": clone,
        "tmp": tmp_path,
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
    status_file = sb["home"] / ".genesis" / "backup_status.json"
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
    assert "round-trip decrypt verified (passphrase source: env)" in proc.stdout, proc.stdout
    assert status["success"] is True, status


def test_roundtrip_escrow_drift_fails_backup(sandbox):
    """Escrowed passphrase differs from the env one (rotation drift) → the
    round-trip fails, the backup fails loudly, and the alert names it."""
    _install_curl(sandbox, _CURL_ABSENT)
    escrow = sandbox["home"] / ".genesis" / "shared" / "guardian"
    escrow.mkdir(parents=True)
    (escrow / "backup_passphrase.env").write_text("GENESIS_BACKUP_PASSPHRASE=stale-rotated-away\n")
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert "round-trip" in status["failure_reason"], status
    assert status["sqlite_lines"] == 0, status
    tg = sandbox["tg"].read_text() if sandbox["tg"].exists() else ""
    assert "backup failed" in tg.lower(), tg


def test_roundtrip_escrow_matching_passes(sandbox):
    """Escrow present and matching → verified against the escrow copy."""
    _install_curl(sandbox, _CURL_ABSENT)
    escrow = sandbox["home"] / ".genesis" / "shared" / "guardian"
    escrow.mkdir(parents=True)
    (escrow / "backup_passphrase.env").write_text("GENESIS_BACKUP_PASSPHRASE=testpass\n")
    proc, status = _run_backup(sandbox)
    assert "passphrase source: escrow" in proc.stdout, proc.stdout
    assert status["success"] is True, status


# ── SF3: freshness gate ──────────────────────────────────────────────


def test_qdrant_down_fails_backup(sandbox):
    """Server unreachable is a FAILURE (the audit hole: it used to read as
    'collection may not exist' and report success forever)."""
    _install_curl(sandbox, _CURL_DOWN)
    proc, status = _run_backup(sandbox)
    assert status["success"] is False, status
    assert "Qdrant backup failed" in status["failure_reason"], status
    assert "probe" in status["failure_reason"], status


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
    assert "genesis.sql.gpg is stale" in proc.stdout, proc.stdout
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
