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
        # backup.sh's tar always writes the directory itself first; mirror that for
        # the common parent of the members (none when they share no parent).
        parents = [p.strip("/").split("/")[:-1] for p in [*members, *(symlinks or {})]]
        parents = [p for p in parents if ".." not in p]
        common = parents[0] if parents else []
        for p in parents[1:]:
            n = 0
            while n < min(len(common), len(p)) and common[n] == p[n]:
                n += 1
            common = common[:n]
        if common:
            d = tarfile.TarInfo("/".join(common))
            d.type = tarfile.DIRTYPE
            d.mode = 0o755
            tf.addfile(d)
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
    assert not list(sb["home"].rglob("*.restore.*")), "staging dir left behind"
    status = json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())
    assert status["extra_restored"] == 1 and status["success"] is True, status


@pytest.mark.parametrize(
    "members,symlinks,refused",
    [
        ({"../escape.txt": b"x", "work/ok.txt": b"ok"}, None, True),
        ({"work/../../escape.txt": b"x", "work/ok.txt": b"ok"}, None, True),
        ({"work/ok.txt": b"ok"}, {"work/link": "/etc/passwd"}, True),  # absolute link target
        ({"work/ok.txt": b"ok"}, {"work/link": "../../outside"}, True),  # link escaping the tree
        ({"work/ok.txt": b"ok"}, {"work/up": "../.ssh"}, True),  # stays in $HOME, leaves the tree
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
    for unsafe in ("link", "up"):
        assert not (sb["home"] / "work" / unsafe).is_symlink(), "an unsafe link was planted"
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
        root = tarfile.TarInfo("work/big")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        tf.addfile(root)
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


def _status(sb):
    return json.loads((sb["home"] / ".genesis" / "restore_status.json").read_text())


@pytest.mark.parametrize(
    "root,reason",
    [
        ("work/store", "leads outside"),  # a symlink in the destination's PARENT path
        ("work/deep/store", "leads outside"),  # ...and mkdir -p must not create dirs through it
        ("work", "is a symlink"),  # the destination itself is a symlink
    ],
)
def test_extra_restore_never_writes_through_a_symlink(restore_sandbox, root, reason):
    """A pre-existing symlink in the destination path must not redirect the restore
    outside $HOME (security review CRITICAL, reproduced before the fix), and a
    symlinked destination is never replaced. Other archives still restore."""
    sb = restore_sandbox
    outside = sb["tmp"] / "outside-target"
    outside.mkdir()
    (sb["home"] / "work").symlink_to(outside)
    _seed_extra_archive(sb, "work-44444444", {f"{root}/evil.txt": b"redirected"})
    _seed_extra_archive(sb, "safe-55555555", {"safe/ok.txt": b"ok"})
    proc = _restore_extra(sb)
    assert not list(outside.rglob("*")), "the restore wrote outside $HOME"
    assert (sb["home"] / "work").is_symlink(), "a symlinked destination was replaced"
    assert (sb["home"] / "safe" / "ok.txt").read_text() == "ok"
    assert reason in proc.stdout, proc.stdout[-1500:]
    assert any(reason in f for f in _status(sb)["failures"]), _status(sb)


def _seed_tree_archive(sb, name: str, entries: list):
    """Like _seed_extra_archive, with explicit (path, kind, mode, data) entries so a
    test controls directory members and their modes."""
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, kind, mode, data in entries:
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.mtime = 1_700_000_000
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tf.addfile(info)
            else:
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
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


def test_extra_restore_keeps_directory_entries_and_modes(restore_sandbox):
    """Codex P2 on PR #2853: empty directories come back, and directory modes are
    the archived ones (minus group/other write), not 0700 from the script's umask.
    The stdlib `data` filter alone drops directory modes."""
    sb = restore_sandbox
    _seed_tree_archive(
        sb,
        "work_store-abcd1234",
        [
            ("work/store", "dir", 0o755, b""),
            ("work/store/empty", "dir", 0o751, b""),
            ("work/store/shared", "dir", 0o750, b""),
            ("work/store/groupw", "dir", 0o775, b""),
            ("work/store/shared/f.txt", "file", 0o640, b"x"),
        ],
    )
    proc = _restore_extra(sb)
    assert proc.returncode == 0, proc.stdout[-1500:]
    store = sb["home"] / "work" / "store"

    def mode(p):
        return stat.S_IMODE(p.stat().st_mode)

    assert (store / "empty").is_dir()
    assert mode(store) == 0o755
    assert mode(store / "empty") == 0o751
    assert mode(store / "shared") == 0o750
    assert mode(store / "groupw") == 0o755, "group write must be stripped"
    assert (store / "shared" / "f.txt").read_bytes() == b"x"


def test_extra_restore_replaces_the_directory_and_keeps_the_old_one_aside(restore_sandbox):
    """With --force an existing directory is replaced AS A UNIT (a file the archive
    lacks does not survive in place) and the previous one is moved aside intact."""
    sb = restore_sandbox
    live = sb["home"] / "work" / "store"
    live.mkdir(parents=True)
    (live / "a.parquet").write_bytes(b"OLD")
    (live / "only-live.txt").write_text("live")
    _seed_extra_archive(sb, "work_store-abcd1234", {"work/store/a.parquet": b"NEW"})
    proc = _restore_extra(sb)
    assert proc.returncode == 0, proc.stdout[-1500:]
    assert (live / "a.parquet").read_bytes() == b"NEW"
    assert not (live / "only-live.txt").exists()
    aside = list((sb["home"] / "work").glob("store.pre-restore-*"))
    assert len(aside) == 1, aside
    assert (aside[0] / "a.parquet").read_bytes() == b"OLD"
    assert (aside[0] / "only-live.txt").read_text() == "live"
    assert "kept at" in proc.stdout


def test_extra_restore_without_force_leaves_an_existing_directory(restore_sandbox):
    sb = restore_sandbox
    live = sb["home"] / "work" / "store"
    live.mkdir(parents=True)
    (live / "a.parquet").write_bytes(b"LIVE")
    _seed_extra_archive(sb, "work_store-abcd1234", {"work/store/a.parquet": b"NEW"})
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"])],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert (live / "a.parquet").read_bytes() == b"LIVE"
    assert not list((sb["home"] / "work").glob("store.pre-restore-*"))
    # Not restoring a whole directory is a recorded failure, not just a log line.
    assert "was not replaced (re-run with --force" in proc.stdout, proc.stdout[-1500:]
    assert any("was not replaced" in f for f in _status(sb)["failures"]), _status(sb)


def test_extra_restore_treats_an_empty_existing_directory_as_absent(restore_sandbox):
    """Review S7: an empty directory (e.g. created by bootstrap) must not block a
    restore that runs without --force."""
    sb = restore_sandbox
    (sb["home"] / "work" / "store").mkdir(parents=True)
    _seed_extra_archive(sb, "work_store-abcd1234", {"work/store/a.parquet": b"NEW"})
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"])],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert (sb["home"] / "work" / "store" / "a.parquet").read_bytes() == b"NEW", proc.stdout[-1500:]
    assert not list((sb["home"] / "work").glob("store.pre-restore-*"))


def test_extra_unwritable_parent_is_recorded_and_the_restore_carries_on(restore_sandbox):
    """Review B1: a staging dir that cannot be created (read-only parent) refuses that
    archive with a recorded failure; it must not abort the rest of the DR restore."""
    sb = restore_sandbox
    ro = sb["home"] / "ro"
    ro.mkdir()
    _seed_extra_archive(sb, "ro_store-77777777", {"ro/store/f.txt": b"x"})
    _seed_secret_payload(sb)
    ro.chmod(0o555)
    try:
        proc = _restore_extra(sb)
    finally:
        ro.chmod(0o755)
    assert "cannot create a staging dir" in proc.stdout, proc.stdout[-1500:]
    status = _status(sb)
    assert any("cannot create a staging dir" in f for f in status["failures"]), status
    assert status["secrets_restored"] is True, "a later section did not run"
    assert (sb["home"] / "genesis" / "secrets.env").is_file()


def test_extra_archive_without_its_directory_never_replaces_the_parent(restore_sandbox):
    """Security review CRITICAL: an archive holding one file (or symlink) and no
    directory member used to name the member's PARENT as the directory to swap,
    moving unrelated siblings aside. It is refused instead."""
    import io
    import tarfile

    sb = restore_sandbox
    projects = sb["home"] / "projects"
    (projects / "other").mkdir(parents=True)
    (projects / "other" / "keep.txt").write_text("keep")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo("projects/myproject")
        info.type = tarfile.SYMTYPE
        info.linkname = "real_target"
        tf.addfile(info)
    (sb["backup"] / "extra").mkdir(exist_ok=True)
    plain = sb["tmp"] / "lone.tar"
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
            str(sb["backup"] / "extra" / "lone-88888888.tar.gpg"),
            str(plain),
        ],
        env={**sb["env"], "GNUPGHOME": str(sb["home"] / ".gnupg")},
        check=True,
        capture_output=True,
    )
    proc = _restore_extra(sb)
    assert (projects / "other" / "keep.txt").read_text() == "keep"
    assert not list(sb["home"].glob("projects.pre-restore-*")), "the parent was moved aside"
    assert "does not hold a single directory" in proc.stdout, proc.stdout[-1500:]


@pytest.mark.parametrize(
    "members,reason",
    [
        ({"work/a.txt": b"a", "other/b.txt": b"b"}, "does not hold a single directory"),
        ({".genesis/notes.txt": b"x"}, "which the core restore owns"),  # contains ~/.genesis/eval
        ({"genesis/data/x.txt": b"x"}, "which the core restore owns"),  # inside GENESIS_DIR
    ],
)
def test_extra_restore_refuses_an_archive_it_cannot_swap_safely(restore_sandbox, members, reason):
    """An archive with more than one top-level directory, or one that overlaps a path
    the core restore owns, is refused whole and recorded; nothing of it is written."""
    sb = restore_sandbox
    _seed_extra_archive(sb, "bad-66666666", members)
    proc = _restore_extra(sb)
    assert reason in proc.stdout, proc.stdout[-1500:]
    assert any(reason in f for f in _status(sb)["failures"]), _status(sb)
    for path in members:
        assert not (sb["home"] / path).exists(), path


def test_extra_dry_run_writes_nothing(restore_sandbox):
    sb = restore_sandbox
    _seed_extra_archive(sb, "work_store-abcd1234", {"work/store/a.parquet": b"PAR1"})
    proc = _restore_extra(sb, "--dry-run")
    assert proc.returncode == 0, proc.stdout
    assert "would restore" in proc.stdout
    assert not (sb["home"] / "work").exists()


def test_extra_read_only_root_restores_and_the_restore_carries_on(restore_sandbox):
    """Audit B1: a directory archived read-only (0555) used to fail the final rename,
    and its staging copy then could not be removed, ending the whole restore under
    set -e with decrypted data left behind. It now restores with its mode, later
    sections run, and no staging is left."""
    sb = restore_sandbox
    _seed_tree_archive(
        sb,
        "work_ro-99999999",
        [
            ("work/ro", "dir", 0o555, b""),
            ("work/ro/sub", "dir", 0o555, b""),
            ("work/ro/sub/f.txt", "file", 0o444, b"x"),
        ],
    )
    _seed_secret_payload(sb)
    try:
        proc = _restore_extra(sb)
        ro = sb["home"] / "work" / "ro"
        assert (ro / "sub" / "f.txt").read_bytes() == b"x", proc.stdout[-1500:]
        assert stat.S_IMODE(ro.stat().st_mode) == 0o555
        assert stat.S_IMODE((ro / "sub").stat().st_mode) == 0o555
        status = _status(sb)
        assert status["secrets_restored"] is True, status
        assert status["extra_restored"] == 1, status
        assert not list(sb["home"].rglob("*.restore.*"))
    finally:
        for p in sb["home"].rglob("*"):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)


def test_extra_empty_destination_is_kept_when_the_restore_fails(restore_sandbox):
    """Codex round 3: an existing empty destination is not removed before the
    replacement is ready. Extraction fails here (a file `x`, then a directory under
    it), with the parent writable, and the empty destination must still be there."""
    sb = restore_sandbox
    store = sb["home"] / "work" / "store"
    store.mkdir(parents=True)
    _seed_tree_archive(
        sb,
        "work_store-abcd1234",
        [
            ("work/store", "dir", 0o755, b""),
            ("work/store/x", "file", 0o644, b""),
            ("work/store/x/y", "dir", 0o755, b""),
        ],
    )
    proc = subprocess.run(
        ["bash", str(_RESTORE), "--from", str(sb["backup"])],
        env={
            **sb["env"],
            "GENESIS_BACKUP_PASSPHRASE": _TEST_PASSPHRASE,
            "GNUPGHOME": str(sb["home"] / ".gnupg"),
        },
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert "could not be restored" in proc.stdout, proc.stdout[-1500:]
    assert store.is_dir(), "the empty destination was removed by a failed restore"
    assert not list(sb["home"].rglob("*.restore.*"))
