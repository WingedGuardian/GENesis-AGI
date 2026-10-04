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
    """N6: a hard staged integrity-check error dies explicitly before swap."""
    text = _RESTORE.read_text()
    assert 'PRAGMA integrity_check;" 2>&1)' in text
    assert "staged integrity_check could not complete" in text
    assert text.index('PRAGMA integrity_check;" 2>&1)') < text.index('mv "$_DB_STAGE" "$DB_FILE"')


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


# ── §4c opt-in extra directories (backup.sh §6f) ─────────────────────


def _seed_extra_archive(sb, name: str, members: dict, *, symlinks: dict | None = None):
    """Write backup/extra/<name>.tar.gpg holding ``members`` (path -> bytes) and
    optional ``symlinks`` (path -> target), member paths stored verbatim."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, data in members.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tf.addfile(info, io.BytesIO(data))
        for path, target in (symlinks or {}).items():
            info = tarfile.TarInfo(path)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tf.addfile(info)
    (sb["backup"] / "extra").mkdir(exist_ok=True)
    plain = sb["tmp"] / f"{name}.tar"
    plain.write_bytes(buf.getvalue())
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--passphrase",
            _TEST_PASSPHRASE,
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "-o",
            str(sb["backup"] / "extra" / f"{name}.tar.gpg"),
            str(plain),
        ],
        env={**sb["env"], "GNUPGHOME": str(sb["home"] / ".gnupg")},
        check=True,
        capture_output=True,
    )


def _restore_extra(sb, *extra_args):
    return subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"]), "--force", *extra_args],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def test_extra_archive_restores_under_home_and_is_a_payload(restore_sandbox):
    """An extra/ archive alone passes the N5 guard and lands under $HOME with no staging left."""
    sb = restore_sandbox
    _seed_extra_archive(
        sb,
        "work_store-abcd1234",
        {"work/store/a.parquet": b"PAR1", "work/store/sub/b.txt": b"keep\n"},
    )
    proc = _restore_extra(sb)
    assert proc.returncode == 0, proc.stdout
    assert (sb["home"] / "work" / "store" / "a.parquet").read_bytes() == b"PAR1"
    assert (sb["home"] / "work" / "store" / "sub" / "b.txt").read_text() == "keep\n"
    assert not list(sb["home"].glob(".genesis-restore-extra.*")), "staging dir left behind"
    status = json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["extra_restored"] == 1 and status["success"] is True, status


@pytest.mark.parametrize(
    "members,symlinks,refused",
    [
        ({"../escape.txt": b"x", "work/ok.txt": b"ok"}, None, True),
        ({"work/../../escape.txt": b"x", "work/ok.txt": b"ok"}, None, True),
        ({"work/ok.txt": b"ok"}, {"work/link": "/etc/passwd"}, True),  # absolute link target
        ({"work/ok.txt": b"ok"}, {"work/link": "../../outside"}, True),  # link escaping the tree
        ({"work/ok.txt": b"ok"}, {"work/rel": "ok.txt"}, False),  # in-tree link: restored
    ],
)
def test_extra_restore_refuses_unsafe_members_and_keeps_the_rest(
    restore_sandbox, members, symlinks, refused
):
    """Refusal is per member (stdlib tarfile `data` filter): an unsafe member is
    never written, a refusal is recorded, and every safe member still restores."""
    sb = restore_sandbox
    _seed_extra_archive(sb, "bad-00000000", members, symlinks=symlinks)
    _seed_extra_archive(sb, "good-11111111", {"good/f.txt": b"fine"})
    proc = _restore_extra(sb)
    assert (sb["home"] / "good" / "f.txt").read_text() == "fine"
    assert (sb["home"] / "work" / "ok.txt").read_text() == "ok"
    for outside in (
        sb["tmp"] / "escape.txt",
        sb["home"].parent / "escape.txt",
        sb["tmp"] / "outside",
    ):
        assert not outside.exists(), outside
    link = sb["home"] / "work" / "link"
    assert not link.is_symlink(), "an unsafe link must not be planted"
    status = json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())
    if refused:
        assert "extra archive refused" in proc.stdout, proc.stdout
        assert any("extra archive refused" in f for f in status["failures"]), status
    else:
        rel = sb["home"] / "work" / "rel"
        assert rel.is_symlink() and os.readlink(rel) == "ok.txt" and rel.read_text() == "ok"
        assert not any("extra archive" in f for f in status["failures"]), status


def test_extra_absolute_member_is_contained_under_home(restore_sandbox):
    """The data filter strips a leading '/', so an absolute member lands INSIDE
    the restore tree (under $HOME), never at the absolute path."""
    sb = restore_sandbox
    _seed_extra_archive(sb, "abs-33333333", {"/abs-escape-probe/f.txt": b"x"})
    _restore_extra(sb)
    assert not os.path.exists("/abs-escape-probe")
    assert (sb["home"] / "abs-escape-probe" / "f.txt").read_bytes() == b"x"


@pytest.mark.parametrize("link_first", [True, False])
def test_unsafe_member_in_a_long_listing_is_still_refused(restore_sandbox, link_first):
    """A listing far larger than a pipe buffer, with the unsafe member first or
    last: it is refused in both positions while the 3,000 safe files restore.
    (Kept from an earlier `tar -tv | grep -q` design, where an early-exiting grep
    under pipefail could SIGPIPE the writer and read as 'nothing unsafe'.)"""
    import io
    import tarfile

    sb = restore_sandbox
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        link = tarfile.TarInfo("work/big/aa-link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        if link_first:
            tf.addfile(link)
        for i in range(3000):
            info = tarfile.TarInfo(f"work/big/f{i:05d}.txt")
            tf.addfile(info, io.BytesIO(b""))
        if not link_first:
            tf.addfile(link)
    (sb["backup"] / "extra").mkdir(exist_ok=True)
    plain = sb["tmp"] / "big.tar"
    plain.write_bytes(buf.getvalue())
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--passphrase",
            _TEST_PASSPHRASE,
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "-o",
            str(sb["backup"] / "extra" / "big-22222222.tar.gpg"),
            str(plain),
        ],
        env={**sb["env"], "GNUPGHOME": str(sb["home"] / ".gnupg")},
        check=True,
        capture_output=True,
    )
    proc = _restore_extra(sb)
    assert "extra archive refused" in proc.stdout, proc.stdout[-2000:]
    assert not (sb["home"] / "work" / "big" / "aa-link").is_symlink()
    assert len(list((sb["home"] / "work" / "big").iterdir())) == 3000


def test_extra_restore_never_writes_through_a_symlink_leading_outside_home(restore_sandbox):
    """A pre-existing symlink in a destination's parent path must not redirect the
    write outside $HOME (security review CRITICAL, reproduced before the fix)."""
    sb = restore_sandbox
    outside = sb["tmp"] / "outside-target"
    outside.mkdir()
    (sb["home"] / "work").symlink_to(outside)
    _seed_extra_archive(sb, "work-44444444", {"work/evil.txt": b"redirected", "safe/ok.txt": b"ok"})
    proc = _restore_extra(sb)
    assert not (outside / "evil.txt").exists(), "write escaped $HOME through a symlink"
    assert (sb["home"] / "safe" / "ok.txt").read_text() == "ok"
    assert "leads outside" in proc.stdout, proc.stdout[-1500:]
    status = json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())
    assert any("leads outside" in f for f in status["failures"]), status


def test_extra_dry_run_writes_nothing(restore_sandbox):
    sb = restore_sandbox
    _seed_extra_archive(sb, "work_store-abcd1234", {"work/store/a.parquet": b"PAR1"})
    proc = _restore_extra(sb, "--dry-run")
    assert proc.returncode == 0, proc.stdout
    assert "would restore" in proc.stdout
    assert not (sb["home"] / "work").exists()
