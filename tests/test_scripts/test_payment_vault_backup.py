"""The card vault in backup.sh §8 and restore.sh §8 (scripts/lib/payment_vault_backup.sh).

The vault's three files travel as ONE archive, creds/payment_vault.tar.gpg.
Producer to consumer with the real scripts: a real vault (vault_store, one Stripe
public test card) goes through the real backup.sh on the `local` and a
filesystem-backed `smb` backend, then through the real restore.sh, both from the
Tier-1 clone and from the off-site snapshot alone. The lib's states and its
unpack side are driven directly. Sandboxed like test_backup_dated_snapshots
(HOME, GENESIS_DIR, GNUPGHOME in tmp; systemctl and curl stubbed).
"""

import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import textwrap
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from genesis.payments import vault_store as vs
from tests.test_scripts import test_backup_dated_snapshots as dated

pytest.importorskip("pyrage")

backup_env = dated.backup_env  # the shared sandbox fixture

_LIB = dated._BACKUP.parent / "lib" / "payment_vault_backup.sh"
_GATE = dated._BACKUP.parent / "lib" / "backup_status_gate.py"
PASSPHRASE = "quartz lantern ember noodle 41"
VISA = "4242" * 4  # Stripe's public test Visa


def _future() -> str:
    """An expiry two years out, read when the card is made, not at import."""
    return f"12/{(datetime.now(UTC).year + 2) % 100:02d}"


NAMES = ("identity.age", "recipient.txt", "cards.db")
ARCHIVE = "payment_vault.tar.gpg"
TMPNAME = f".{ARCHIVE}.tmp"
_ASIDE_RE = re.compile(r"payment_vault\.pre-restore-\d{8}T\d{6}Z\.\d+")


@pytest.fixture(scope="module")
def _sealed(tmp_path_factory):
    """One real init (scrypt costs seconds), copied fresh into each test."""
    path = tmp_path_factory.mktemp("sealed") / "payment_vault"
    vs.init_vault(PASSPHRASE, path)
    return path


def _place_vault(sealed: Path, dest: Path) -> Path:
    shutil.copytree(sealed, dest)
    os.chmod(dest, 0o700)
    vs.add_card(
        vs.parse_card(VISA, _future(), "123"),
        label="test",
        merchant=None,
        single_use=True,
        path=dest,
    )
    return dest


def _gpg_decrypt(src: Path, dst: Path, home: Path) -> None:
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(home),
            "--passphrase",
            "testpass",
            "-o",
            str(dst),
            "-d",
            str(src),
        ],
        check=True,
        capture_output=True,
    )


def _gpg_encrypt(src: Path, dst: Path, home: Path) -> None:
    home.mkdir(mode=0o700, exist_ok=True)
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(home),
            "--passphrase",
            "testpass",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "-o",
            str(dst),
            str(src),
        ],
        check=True,
        capture_output=True,
    )


def _card_number(vault: Path) -> str:
    """Unseal the identity with the vault passphrase and decrypt the one card."""
    from pyrage import decrypt, x25519
    from pyrage import passphrase as age_passphrase

    sealed = (vault / "identity.age").read_bytes()
    ident = x25519.Identity.from_str(age_passphrase.decrypt(sealed, PASSPHRASE).decode())
    with sqlite3.connect(f"file:{vault / 'cards.db'}?mode=ro", uri=True) as conn:
        (blob,) = conn.execute("SELECT blob FROM cards").fetchone()
    return json.loads(decrypt(blob, [ident]))["number"]


def _no_card_number_under(*roots: Path) -> list[str]:
    needle = VISA.encode()
    hits = []
    for root in roots:
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink() and needle in p.read_bytes():
                hits.append(str(p))
    return hits


def _tars_under(root: Path) -> list[str]:
    """Every plaintext tar under root (ustar magic at offset 257), whatever its name."""
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            with open(p, "rb") as f:
                head = f.read(262)
            if head[257:262] == b"ustar":
                hits.append(str(p))
    return hits


def _tree(root: Path) -> dict:
    """Every entry under root: relative path → bytes (files), link target, or a dir mark."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_dir():
            out[rel] = ("dir", stat.S_IMODE(p.stat().st_mode))
        else:
            out[rel] = ("file", p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    return out


def _status(env) -> dict:
    return json.loads((env["home"] / ".genesis" / "backup_status.json").read_text())


# ── the lib, driven directly ───────────────────────────────────────────


def _lib(tmp_path, vault: Path, out: Path, *, encrypt="gpg") -> subprocess.CompletedProcess:
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700, exist_ok=True)
    big = tmp_path / "bigtmp"
    big.mkdir(exist_ok=True)
    enc = {
        "gpg": "printf %s testpass | gpg --batch --yes --no-symkey-cache --passphrase-fd 0 "
        '--symmetric --cipher-algo AES256 -o "$2" "$1" 2>/dev/null',
        "fail": "return 1",
    }[encrypt]
    script = textwrap.dedent(f"""\
        set -euo pipefail
        source "{_LIB}"
        encrypt_file() {{ {enc}; }}
        out="$(backup_payment_vault "$1" "$2" "$3")" || out="failed aborted"
        printf '%s\\n' "$out"
    """)
    env = {**os.environ, "GNUPGHOME": str(gnupg), "HOME": str(tmp_path)}
    return subprocess.run(
        ["bash", "-c", script, "lib", str(vault), str(out), str(big)],
        env=env,
        capture_output=True,
        text=True,
    )


def _archive(out: Path) -> bytes | None:
    p = out / ARCHIVE
    return p.read_bytes() if p.is_file() else None


def _open_archive(src: Path, tmp_path: Path, name: str = "back") -> Path:
    """Decrypt a payment_vault.tar.gpg and unpack it with Python (independently of
    the lib's unpack); the member list must be exactly the three regular files."""
    tarf = tmp_path / f"{name}.tar"
    _gpg_decrypt(src, tarf, tmp_path / "gnupg")
    back = tmp_path / name
    back.mkdir()
    with tarfile.open(tarf) as tf:
        members = tf.getmembers()
        assert sorted(m.name for m in members) == sorted(NAMES)
        assert all(m.isreg() for m in members)
        tf.extractall(back, filter="data")
    return back


def test_names_match_the_vault_store():
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; printf "%s\\n" "${{PAYMENT_VAULT_FILES[@]}}"; '
            'printf "%s\\n" "$PAYMENT_VAULT_ARCHIVE"',
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    *files, archive = proc.stdout.split()
    assert tuple(files) == (vs.IDENTITY, vs.RECIPIENT, vs.CARDS) == NAMES
    assert archive == ARCHIVE


def test_ok_writes_one_archive_that_unpacks_to_a_working_vault(_sealed, tmp_path):
    vault = _place_vault(_sealed, tmp_path / "vault")
    out = tmp_path / "creds"
    proc = _lib(tmp_path, vault, out)
    assert proc.stdout.strip() == "ok", proc.stdout + proc.stderr
    # Exactly one file: no per-file layout, no staging name left behind.
    assert sorted(p.name for p in out.iterdir()) == [ARCHIVE]
    assert not (out / "payment_vault").exists()
    back = _open_archive(out / ARCHIVE, tmp_path)
    assert (back / "identity.age").read_bytes() == (vault / "identity.age").read_bytes()
    assert (back / "recipient.txt").read_bytes() == (vault / "recipient.txt").read_bytes()
    assert vs.list_cards(back) == vs.list_cards(vault)
    assert _card_number(back) == VISA
    # Neither the archive nor its plaintext tar holds the number (the blob is age-encrypted).
    assert _no_card_number_under(out, back, tmp_path / "back.tar") == []
    assert list((tmp_path / "bigtmp").iterdir()) == []  # the staging dir is removed


def test_absent_and_missing(tmp_path):
    out = tmp_path / "creds"
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "absent"
    assert not out.exists()
    out.mkdir()
    (out / ARCHIVE).write_bytes(b"earlier")
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "missing"
    assert (out / ARCHIVE).read_bytes() == b"earlier"


@pytest.mark.parametrize("cards", ["empty file", "empty table"])
@pytest.mark.parametrize("with_recipient", [False, True])
def test_a_directory_with_no_key_and_no_cards_is_no_vault(tmp_path, with_recipient, cards):
    """An init killed before identity.age landed (vault_store places recipient.txt
    first) is not initialised: there is no key to keep, and init can rerun."""
    vault = tmp_path / "vault"
    vault.mkdir()
    if cards == "empty file":
        sqlite3.connect(vault / "cards.db").close()  # creates a 0-byte file
    else:
        with sqlite3.connect(vault / "cards.db") as conn:
            conn.execute(vs._SCHEMA)
            conn.execute(f"PRAGMA user_version = {vs.SCHEMA_VERSION}")
    if with_recipient:
        (vault / "recipient.txt").write_text("age1" + "q" * 58 + "\n")
    out = tmp_path / "creds"
    assert _lib(tmp_path, vault, out).stdout.split()[0] == "absent"
    out.mkdir()
    (out / ARCHIVE).write_bytes(b"earlier")
    assert _lib(tmp_path, vault, out).stdout.split()[0] == "missing"
    assert (out / ARCHIVE).read_bytes() == b"earlier"


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("rows", "cards remain in a vault with no key"),
        ("unreadable", "cards.db cannot be read in a vault with no key"),
        ("link", "cards.db is not a regular file in a vault with no key"),
    ],
)
def test_cards_without_a_key_fail_never_absent(_sealed, tmp_path, damage, reason):
    """A vault whose identity.age is gone but whose cards.db holds cards is a loss
    to report, not an empty vault (vault_store refuses to re-init it too)."""
    vault = _place_vault(_sealed, tmp_path / "vault")
    out = tmp_path / "creds"
    assert _lib(tmp_path, vault, out).stdout.strip() == "ok"
    earlier = _archive(out)
    (vault / "identity.age").unlink()
    if damage == "unreadable":
        (vault / "cards.db").write_bytes(b"not sqlite" * 100)
    elif damage == "link":
        real = tmp_path / "elsewhere.db"
        (vault / "cards.db").rename(real)
        (vault / "cards.db").symlink_to(real)
    proc = _lib(tmp_path, vault, out)
    state, _, why = proc.stdout.strip().partition(" ")
    assert state == "failed" and reason in why, proc.stdout + proc.stderr
    assert _archive(out) == earlier  # the last good archive is kept
    # With no earlier archive it is still a failure, never "absent".
    (out / ARCHIVE).unlink()
    proc = _lib(tmp_path, vault, out)
    state, _, why = proc.stdout.strip().partition(" ")
    assert state == "failed" and reason in why, proc.stdout + proc.stderr
    assert list(out.iterdir()) == []


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("rm recipient.txt", "the vault is incomplete (recipient.txt"),
        ("rm cards.db", "the vault is incomplete (cards.db"),
        ("link cards.db", "the vault is incomplete (cards.db is missing or not a regular file)"),
        ("corrupt cards.db", "integrity check"),
        ("garble identity.age", "identity.age is not an age file"),
        ("garble recipient.txt", "recipient.txt does not hold an age public key"),
        ("version0 cards.db", "not a vault database of format version 1"),
        ("version2 cards.db", "not a vault database of format version 1"),
        ("encrypt fails", "encrypting the vault failed"),
    ],
)
def test_failed_keeps_the_earlier_archive_byte_identical(_sealed, tmp_path, damage, reason):
    vault = _place_vault(_sealed, tmp_path / "vault")
    out = tmp_path / "creds"
    assert _lib(tmp_path, vault, out).stdout.strip() == "ok"
    earlier = _archive(out)
    encrypt = "gpg"
    verb, name = damage.split(" ", 1)
    target = vault / name
    if verb == "rm":
        target.unlink()
    elif verb == "link":
        real = tmp_path / "elsewhere.db"
        target.rename(real)
        target.symlink_to(real)
    elif verb == "corrupt":
        # A damaged page, header intact: sqlite opens it and integrity_check fails.
        data = bytearray(target.read_bytes())
        page = 4096
        data[page : page * 2] = os.urandom(page)
        target.write_bytes(bytes(data))
    elif verb == "garble":
        target.write_text("not a key\n")
    elif verb.startswith("version"):
        with sqlite3.connect(target) as conn:
            conn.execute(f"PRAGMA user_version = {verb[-1]}")
    else:
        encrypt = "fail"
    proc = _lib(tmp_path, vault, out, encrypt=encrypt)
    state, _, why = proc.stdout.strip().partition(" ")
    assert state == "failed" and reason in why, proc.stdout + proc.stderr
    assert _archive(out) == earlier
    assert sorted(p.name for p in out.iterdir()) == [ARCHIVE]  # no staging name left
    assert list((tmp_path / "bigtmp").iterdir()) == []


def test_a_symlinked_vault_directory_is_refused(_sealed, tmp_path):
    real = _place_vault(_sealed, tmp_path / "real")
    (tmp_path / "vault").symlink_to(real)
    proc = _lib(tmp_path, tmp_path / "vault", tmp_path / "creds")
    assert proc.stdout.startswith("failed the vault path is not a directory"), proc.stdout
    assert not (tmp_path / "creds").exists()


@pytest.mark.parametrize("where", ["creds", "its parent"])
def test_a_symlinked_creds_directory_is_refused(_sealed, tmp_path, where):
    """A symlink at creds (or the clone above it) would send the archive outside the
    backups repo while the run reported ok."""
    vault = _place_vault(_sealed, tmp_path / "vault")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if where == "creds":
        (tmp_path / "creds").symlink_to(elsewhere)
        out = tmp_path / "creds"
    else:
        (elsewhere / "creds").mkdir()
        (tmp_path / "clone").symlink_to(elsewhere)
        out = tmp_path / "clone" / "creds"
    before = _tree(elsewhere)
    proc = _lib(tmp_path, vault, out)
    assert proc.stdout.startswith("failed the backup directory is a symlink"), proc.stdout
    assert _tree(elsewhere) == before


def test_a_staging_leftover_of_a_killed_run_is_removed(_sealed, tmp_path):
    out = tmp_path / "creds"
    out.mkdir()
    (out / TMPNAME).write_bytes(b"half")
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "absent"
    assert not (out / TMPNAME).exists()
    # And on a successful run, the leftover never survives next to the new archive.
    (out / TMPNAME).write_bytes(b"half")
    vault = _place_vault(_sealed, tmp_path / "vault")
    assert _lib(tmp_path, vault, out).stdout.strip() == "ok"
    assert sorted(p.name for p in out.iterdir()) == [ARCHIVE]


def test_a_killed_backups_plaintext_stage_is_removed_by_the_next_run(tmp_path):
    """SIGKILL skips the subshell's EXIT trap; the next run removes the stage it left
    under the temp root (class audit on #3211)."""
    big = tmp_path / "bigtmp"
    stale = big / "payment_vault.Ab12Cd"
    stale.mkdir(parents=True)
    (stale / "cards.db").write_bytes(b"plaintext leftover")
    keep = big / "payment_vault.notours-longer"
    keep.mkdir()
    out = tmp_path / "creds"
    out.mkdir()
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "absent"
    assert not stale.exists()
    assert keep.exists()  # only the six-character mktemp shape is ours


def test_the_staging_name_is_gitignored_and_the_archive_is_not(tmp_path):
    """The backups clone's .gitignore line keeps a killed run's staging file out of
    a commit, and never hides the archive itself."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
    (repo / ".gitignore").write_text("creds/**/.*.tmp\n")
    (repo / "creds").mkdir()

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "check-ignore", "-q", rel], cwd=repo).returncode == 0

    assert ignored(f"creds/{TMPNAME}")
    assert not ignored(f"creds/{ARCHIVE}")


def test_waits_for_a_held_vault_lock(_sealed, tmp_path):
    """The backup takes the directory lock `vault init`, `add` and `retire` hold,
    so it never reads a half-made vault."""
    vault = _place_vault(_sealed, tmp_path / "vault")
    flag = tmp_path / "locked"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""\
        import fcntl, os, time
        fd = os.open({str(vault)!r}, os.O_RDONLY | os.O_DIRECTORY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        open({str(flag)!r}, "w").close()
        time.sleep(4)
    """),
        ]
    )
    try:
        while not flag.exists():
            time.sleep(0.02)
        start = time.monotonic()
        proc = _lib(tmp_path, vault, tmp_path / "creds")
        waited = time.monotonic() - start
    finally:
        holder.wait()
    assert proc.stdout.strip() == "ok", proc.stdout + proc.stderr
    assert waited >= 3.5, f"did not wait for the lock held 4s ({waited:.2f}s)"


def test_the_callers_exit_trap_survives_and_errexit_does_not_abort_it(tmp_path):
    """backup.sh's EXIT trap sends the failure alert; the lib's own trap must not
    replace it, and a failure inside must come back as a state, not an abort."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""\
        set -euo pipefail
        trap 'echo CALLER-TRAP' EXIT
        source "{_LIB}"
        encrypt_file() {{ return 1; }}
        mkdir -p "$1/v"; : > "$1/v/identity.age"; : > "$1/v/recipient.txt"; : > "$1/v/cards.db"
        out="$(backup_payment_vault "$1/v" "$1/o" "$1")" || out="failed aborted"
        echo "STATE=$out"
    """),
            "t",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
    )
    lines = proc.stdout.splitlines()
    assert lines[0].startswith("STATE=failed identity.age is not an age file"), proc.stdout
    assert lines[-1] == "CALLER-TRAP", proc.stdout
    assert proc.returncode == 0


# ── payment_vault_check: parity with vault_store ───────────────────────


def _check(d: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; payment_vault_check "$1"', "x", str(d)],
        capture_output=True,
        text=True,
    )


def _store_columns() -> str:
    with sqlite3.connect(":memory:") as conn:
        conn.execute(vs._SCHEMA)
        return " ".join(r[1] for r in conn.execute("PRAGMA table_info(cards)"))


def test_the_shell_check_accepts_a_real_vault_and_matches_its_schema(_sealed, tmp_path):
    vault = _place_vault(_sealed, tmp_path / "vault")
    proc = _check(vault)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    consts = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; printf "%s\\n%s\\n" "$PAYMENT_VAULT_SCHEMA_VERSION" '
            '"$PAYMENT_VAULT_COLUMNS"',
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert consts == [str(vs.SCHEMA_VERSION), _store_columns()]


def _rebuild_cards(db: Path, schema: str) -> None:
    db.unlink()
    with sqlite3.connect(db) as conn:
        conn.execute(schema)
        conn.execute(f"PRAGMA user_version = {vs.SCHEMA_VERSION}")


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("user_version 2", "format version 1"),
        ("extra column", "does not have the vault card table"),
        ("missing column", "does not have the vault card table"),
        ("no cards table", "does not have the vault card table"),
        ("recipient short", "recipient.txt does not hold an age public key"),
        ("recipient long", "recipient.txt does not hold an age public key"),
        ("recipient bad charset", "recipient.txt does not hold an age public key"),
        ("no vault_meta", "does not record the vault keys"),
        ("identity flipped", "identity.age is not the key this vault was created with"),
        ("recipient swapped", "recipient.txt is not the public key this vault was created with"),
        # Codex round 2 on #3211: what only the vault's own acceptance catches.
        ("recipient extra line", "recipient.txt does not hold a valid age public key"),
        ("cards constraint dropped", "cards.db is not a vault card database"),
        ("extra trigger", "cards.db is not a vault card database"),
        ("extra index", "cards.db is not a vault card database"),
    ],
)
def test_the_shell_check_refuses_what_vault_store_would(_sealed, tmp_path, damage, reason):
    vault = _place_vault(_sealed, tmp_path / "vault")
    db = vault / "cards.db"
    key = (vault / "recipient.txt").read_text().strip()
    if damage == "user_version 2":
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version = 2")
    elif damage == "extra column":
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE cards ADD COLUMN extra TEXT")
    elif damage == "missing column":
        _rebuild_cards(db, vs._SCHEMA.replace("    retired_at  TEXT,\n", ""))
    elif damage == "no cards table":
        _rebuild_cards(db, "CREATE TABLE other (x)")
    elif damage == "recipient short":
        (vault / "recipient.txt").write_text(key[:-1] + "\n")
    elif damage == "recipient long":
        (vault / "recipient.txt").write_text(key + "q\n")
    elif damage == "no vault_meta":
        with sqlite3.connect(db) as conn:
            conn.execute("DROP TABLE vault_meta")
    elif damage == "identity flipped":
        data = bytearray((vault / "identity.age").read_bytes())
        data[-5] ^= 0x01  # same length, same shape
        (vault / "identity.age").write_bytes(bytes(data))
    elif damage == "recipient swapped":
        from pyrage import x25519

        (vault / "recipient.txt").write_text(str(x25519.Identity.generate().to_public()) + "\n")
    elif damage == "recipient extra line":
        (vault / "recipient.txt").write_text(key + "\nage1extra\n")
    elif damage == "cards constraint dropped":
        # Same columns in the same order, label's NOT NULL gone: the names-only
        # column check passed this. vault_meta (the key binding) is kept.
        loose = vs._SCHEMA.replace("label       TEXT NOT NULL", "label       TEXT")
        with sqlite3.connect(db) as conn:
            conn.execute("ALTER TABLE cards RENAME TO old_cards")
            conn.execute(loose)
            conn.execute("INSERT INTO cards SELECT * FROM old_cards")
            conn.execute("DROP TABLE old_cards")
    elif damage == "extra trigger":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TRIGGER t AFTER INSERT ON cards BEGIN SELECT 1; END")
    elif damage == "extra index":
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE INDEX i ON cards (label)")
    else:
        (vault / "recipient.txt").write_text(key[:-1] + "b\n")  # 'b' is not Bech32
    proc = _check(vault)
    assert proc.returncode == 1 and reason in proc.stdout, proc.stdout + proc.stderr


# ── payment_vault_unpack: restore's side of the archive ────────────────


#: Two real vaults, one per mark, made once per module by _real_sets (scrypt
#: costs seconds). A stand-in set would not pass payment_vault_check, which runs
#: the vault's own acceptance (vault_store.verify_set).
_REAL_SETS: dict[str, Path] = {}


@pytest.fixture(scope="module", autouse=True)
def _real_sets(tmp_path_factory):
    for mark in ("p", "q"):
        path = tmp_path_factory.mktemp(f"real-{mark}") / "payment_vault"
        vs.init_vault(PASSPHRASE, path)
        _REAL_SETS[mark] = path
    yield
    _REAL_SETS.clear()


def _synthetic_set(dest: Path, *, cards_ok: bool = True, mark: str = "q") -> None:
    """A copy of the real vault named by `mark`, so two sets can be told apart
    (_recipient_mark). With cards_ok False, cards.db is replaced by junk."""
    shutil.copytree(_REAL_SETS[mark], dest)
    os.chmod(dest, 0o700)
    if not cards_ok:
        (dest / "cards.db").write_bytes(b"not sqlite" * 100)


def _write_tar(path: Path, members: list[tuple[str, str, object]]) -> None:
    """members: (name, kind, payload); kind is file (bytes), symlink or hardlink
    (target), or dir. Every member claims mode 0644, to show the unpack's own
    modes win."""
    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as tf:
        for name, kind, payload in members:
            ti = tarfile.TarInfo(name)
            ti.mode = 0o644
            if kind == "file":
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
            elif kind == "symlink":
                ti.type, ti.linkname = tarfile.SYMTYPE, payload
                tf.addfile(ti)
            elif kind == "hardlink":
                ti.type, ti.linkname = tarfile.LNKTYPE, payload
                tf.addfile(ti)
            else:
                ti.type, ti.mode = tarfile.DIRTYPE, 0o755
                tf.addfile(ti)


def _set_members(plain: Path) -> list[tuple[str, str, object]]:
    return [(n, "file", (plain / n).read_bytes()) for n in NAMES]


def _unpack(tarf: Path, parent: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_LIB}"; payment_vault_unpack "$1" "$2"',
            "x",
            str(tarf),
            str(parent),
        ],
        capture_output=True,
        text=True,
    )


def test_unpack_makes_a_private_new_directory(tmp_path):
    plain = tmp_path / "plain"
    _synthetic_set(plain)
    tarf = tmp_path / "v.tar"
    _write_tar(tarf, _set_members(plain))
    parent = tmp_path / "parent"
    parent.mkdir()
    proc = _unpack(tarf, parent)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    dest = Path(proc.stdout.strip())
    assert dest.parent == parent and dest.name.startswith(".payment_vault.")
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert sorted(p.name for p in dest.iterdir()) == sorted(NAMES)
    for n in NAMES:
        assert stat.S_IMODE((dest / n).stat().st_mode) == 0o600, n
        assert (dest / n).read_bytes() == (plain / n).read_bytes()


@pytest.mark.parametrize(
    "case",
    ["symlink cards.db", "hardlink cards.db", "duplicate", "extra", "dotdot", "missing", "dir"],
)
def test_unpack_refuses_anything_but_the_three_files(tmp_path, case):
    plain = tmp_path / "plain"
    _synthetic_set(plain)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    good = _set_members(plain)
    ident, recip, cards = good
    members = {
        "symlink cards.db": [ident, recip, ("cards.db", "symlink", str(outside))],
        "hardlink cards.db": [ident, recip, ("cards.db", "hardlink", "identity.age")],
        "duplicate": [ident, recip, cards, cards],
        "extra": [*good, ("notes.txt", "file", b"x")],
        "dotdot": [ident, recip, ("../cards.db", "file", cards[2])],
        "missing": [ident, recip],
        "dir": [ident, recip, ("cards.db", "dir", None)],
    }[case]
    tarf = tmp_path / "v.tar"
    _write_tar(tarf, members)
    parent = tmp_path / "parent"
    parent.mkdir()
    (parent / "keep.txt").write_text("keep\n")
    before = _tree(tmp_path / "parent")
    proc = _unpack(tarf, parent)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "the archive does not hold exactly the three vault files" in proc.stdout
    assert _tree(parent) == before  # nothing left behind
    assert not (tmp_path / "cards.db").exists()  # the ../ member never landed
    assert outside.read_bytes() == b"outside"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_unpack_refuses_a_set_that_fails_its_check_and_leaves_nothing(tmp_path):
    plain = tmp_path / "plain"
    _synthetic_set(plain, cards_ok=False)
    tarf = tmp_path / "v.tar"
    _write_tar(tarf, _set_members(plain))
    parent = tmp_path / "parent"
    parent.mkdir()
    proc = _unpack(tarf, parent)
    assert proc.returncode == 1 and "integrity check" in proc.stdout, proc.stdout
    assert list(parent.iterdir()) == []


# ── restore.sh §8, synthetic sources ───────────────────────────────────


def _restore(
    fresh: Path,
    source: Path,
    bind: Path,
    *,
    offsite: Path | None = None,
    env_extra: dict | None = None,
    args: tuple = ("--force",),
):
    (fresh / "genesis" / "data").mkdir(parents=True, exist_ok=True)
    (fresh / ".genesis").mkdir(exist_ok=True)
    (fresh / "tmp").mkdir(exist_ok=True)
    (fresh / ".gnupg").mkdir(mode=0o700, exist_ok=True)
    env = {
        **{k: v for k, v in os.environ.items() if k not in dated._SESSION_BUS_VARS},
        "HOME": str(fresh),
        "GENESIS_DIR": str(fresh / "genesis"),
        "GENESIS_BACKUP_TMPDIR": str(fresh / "tmp"),
        "GENESIS_BACKUP_TIER2_BACKEND": "none",
        "QDRANT_URL": "http://127.0.0.1:1",
        "GENESIS_BACKUP_PASSPHRASE": "testpass",
        "GENESIS_RESTORE_HOLDER_SCAN": "none",
        "GNUPGHOME": str(fresh / ".gnupg"),
        "PATH": f"{bind}:{os.environ['PATH']}",
    }
    for k in (
        "GENESIS_HOME",
        "GENESIS_CREDS_STAGE",
        "GENESIS_CREDS_MIRROR",
        "GENESIS_BACKUP_NAS_HOST",
    ):
        env.pop(k, None)
    argv = ["--from", str(source)]
    if offsite is not None:
        # An off-site restore reads no --from source (an explicit local source
        # skips the pull): `source` becomes the ambient Tier-1 checkout, and the
        # selected snapshot is pulled into restore's private view.
        env.update(
            GENESIS_BACKUP_TIER2_BACKEND="local",
            GENESIS_BACKUP_LOCAL_PATH=str(offsite),
            GENESIS_BACKUP_NAS_HOST="srchost",
        )
        checkout = fresh / "backups" / "genesis-backups"
        if source.exists() and not checkout.exists():
            checkout.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, checkout, symlinks=True)
        argv = []
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(dated._RESTORE), *argv, *args],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _bind(tmp_path: Path) -> Path:
    bind = tmp_path / "bin"
    bind.mkdir(exist_ok=True)
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    return bind


def _seal_archive(tmp_path: Path, dest: Path, *, mark="q", cards_ok=True, members=None) -> None:
    """Write a payment_vault.tar.gpg at dest from a synthetic set (or explicit members)."""
    plain = tmp_path / f"plain-{mark}-{cards_ok}"
    if not plain.exists():
        _synthetic_set(plain, cards_ok=cards_ok, mark=mark)
    tarf = tmp_path / f"{dest.name}.{mark}.tar"
    _write_tar(tarf, members if members is not None else _set_members(plain))
    dest.parent.mkdir(parents=True, exist_ok=True)
    _gpg_encrypt(tarf, dest, tmp_path / "srcgnupg")
    tarf.unlink()


def _source_with_archive(tmp_path: Path, **kw) -> Path:
    src = tmp_path / "source"
    _seal_archive(tmp_path, src / "creds" / ARCHIVE, **kw)
    return src


def _staged(fresh: Path) -> Path:
    return fresh / ".genesis" / "restore-creds" / "payment_vault"


def _recipient_mark(d: Path) -> str:
    """Which real set `d` is a copy of, by its recipient key."""
    key = (d / "recipient.txt").read_text().strip()
    (mark,) = [m for m, p in _REAL_SETS.items() if (p / "recipient.txt").read_text().strip() == key]
    return mark


def _assert_no_plaintext_tar(fresh: Path) -> None:
    stage = fresh / ".genesis" / "restore-creds"
    assert not (stage / "payment_vault.tar").exists()
    if stage.exists():
        assert [p.name for p in stage.iterdir() if ".tar" in p.name] == []
    assert _tars_under(fresh) == []


def test_restore_stages_a_new_vault_and_moves_an_earlier_one_aside(tmp_path):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    _synthetic_set(_staged(fresh), mark="p")  # an earlier restore
    (_staged(fresh) / "sentinel").write_text("earlier\n")
    src = _source_with_archive(tmp_path, mark="q")
    r = _restore(fresh, src, bind)
    assert "Payment vault: staged at" in r.stdout, r.stdout[-3000:]
    assert _recipient_mark(_staged(fresh)) == "q"
    assert sorted(p.name for p in _staged(fresh).iterdir()) == sorted(NAMES)
    (aside,) = [p for p in _staged(fresh).parent.iterdir() if p.name.startswith("payment_vault.")]
    assert _ASIDE_RE.fullmatch(aside.name), aside.name
    assert _recipient_mark(aside) == "p" and (aside / "sentinel").read_text() == "earlier\n"
    assert "the earlier staged copy was moved aside" in r.stdout
    assert stat.S_IMODE(_staged(fresh).stat().st_mode) == 0o700
    for n in NAMES:
        assert stat.S_IMODE((_staged(fresh) / n).stat().st_mode) == 0o600
    _assert_no_plaintext_tar(fresh)
    assert not (fresh / ".genesis" / "payment_vault").exists(), "restore must never place it"


def test_restore_moves_an_earlier_staged_symlink_aside_without_following_it(tmp_path):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    target = tmp_path / "target"
    _synthetic_set(target, mark="p")
    before = _tree(target)
    _staged(fresh).parent.mkdir(parents=True)
    _staged(fresh).symlink_to(target)
    r = _restore(fresh, _source_with_archive(tmp_path), bind)
    assert "Payment vault: staged at" in r.stdout, r.stdout[-3000:]
    assert not _staged(fresh).is_symlink() and _recipient_mark(_staged(fresh)) == "q"
    (aside,) = [p for p in _staged(fresh).parent.iterdir() if _ASIDE_RE.fullmatch(p.name)]
    assert aside.is_symlink() and os.readlink(aside) == str(target)
    assert _tree(target) == before


@pytest.mark.parametrize(
    "case, says",
    [
        ("symlink member", "the archive failed its check (the archive does not hold exactly"),
        ("bad set", "the archive failed its check (cards.db failed its integrity check)"),
        ("not gpg", f"decrypting {ARCHIVE} failed"),
    ],
)
def test_a_refused_archive_is_not_staged_and_the_earlier_one_stays(tmp_path, case, says):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    _synthetic_set(_staged(fresh), mark="p")
    before = _tree(_staged(fresh).parent)
    src = tmp_path / "source"
    dest = src / "creds" / ARCHIVE
    if case == "symlink member":
        plain = tmp_path / "plain-q-True"
        _synthetic_set(plain)
        ident, recip, _ = _set_members(plain)
        _seal_archive(
            tmp_path, dest, members=[ident, recip, ("cards.db", "symlink", "/etc/passwd")]
        )
    elif case == "bad set":
        _seal_archive(tmp_path, dest, cards_ok=False)
    else:
        dest.parent.mkdir(parents=True)
        dest.write_bytes(os.urandom(512))
    r = _restore(fresh, src, bind)
    assert says in r.stdout, r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    assert _tree(_staged(fresh).parent) == before  # nothing moved aside, nothing added
    _assert_no_plaintext_tar(fresh)
    assert r.returncode != 0
    # The copy still at the staging path is not this backup's, and restore says so.
    assert "holds a vault from an EARLIER restore, not from this backup" in r.stdout


def test_a_symlinked_archive_is_named_and_not_staged(tmp_path):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    real = tmp_path / "elsewhere" / ARCHIVE
    _seal_archive(tmp_path, real)
    src = tmp_path / "source"
    (src / "creds").mkdir(parents=True)
    (src / "creds" / ARCHIVE).symlink_to(real)
    (tmp_path / "x").write_text("x\n")
    _gpg_encrypt(tmp_path / "x", src / "creds" / "gh_hosts.yml.gpg", tmp_path / "srcgnupg")
    r = _restore(fresh, src, bind)
    assert f"not staged. {ARCHIVE} in the backup source is a symlink" in r.stdout, r.stdout[-3000:]
    assert not _staged(fresh).exists()
    _assert_no_plaintext_tar(fresh)


def test_a_restore_killed_before_placing_the_set_leaves_no_decrypted_copy(tmp_path):
    """SIGTERM between unpacking and renaming the set into place: the EXIT trap
    removes the unpacked set (Codex round 3 on #3211)."""
    bind = _bind(tmp_path)
    dated._make_stub(
        bind / "mv",
        "#!/usr/bin/env bash\n"
        'case "$*" in *.payment_vault.*) kill -TERM "$PPID"; exit 1 ;; esac\n'
        'exec /bin/mv "$@"\n',
    )
    fresh = tmp_path / "fresh"
    r = _restore(fresh, _source_with_archive(tmp_path), bind)
    assert r.returncode != 0, r.stdout[-3000:]
    stage = fresh / ".genesis" / "restore-creds"
    left = (
        sorted(p.name for p in stage.iterdir() if "payment_vault" in p.name)
        if stage.exists()
        else []
    )
    assert left == [], left
    _assert_no_plaintext_tar(fresh)


def test_a_restore_killed_while_the_set_is_checked_leaves_no_decrypted_copy(tmp_path):
    """SIGTERM while payment_vault_unpack checks the set, inside its subshell, before
    restore has its name: the EXIT trap still removes it (architect, round 3). The
    stub signals every process from itself up to the top restore process (its $(...)
    subshells carry the same command line), as a stop of the whole
    process group (systemd, Ctrl-C) would, so the subshell cannot clean up first."""
    bind = _bind(tmp_path)
    real = shutil.which("sqlite3")
    assert real, "sqlite3 is needed"
    dated._make_stub(
        bind / "sqlite3",
        "#!/usr/bin/env bash\n"
        'case "$*" in *.payment_vault.*/cards.db*)\n'
        '  p=$PPID; chain=""\n'
        '  while [ "$p" -gt 1 ]; do\n'
        '    chain="$chain $p"\n'
        "    pp=$(awk '{print $4}' /proc/$p/stat)\n"
        "    if tr '\\0' ' ' < /proc/$p/cmdline | grep -q 'restore.sh' &&\n"
        "       ! tr '\\0' ' ' < /proc/$pp/cmdline | grep -q 'restore.sh'; then\n"
        "      kill -TERM $chain; exit 1\n"
        "    fi\n"
        "    p=$pp\n"
        "  done ;;\n"
        "esac\n"
        f'exec {real} "$@"\n',
    )
    fresh = tmp_path / "fresh"
    r = _restore(fresh, _source_with_archive(tmp_path), bind)
    assert r.returncode != 0, r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    stage = fresh / ".genesis" / "restore-creds"
    left = (
        sorted(p.name for p in stage.iterdir() if "payment_vault" in p.name)
        if stage.exists()
        else []
    )
    assert left == [], left
    _assert_no_plaintext_tar(fresh)


def test_an_earlier_staged_symlink_into_the_live_vault_is_moved_aside(tmp_path):
    """A staged copy that is a symlink into the live vault is moved aside, not
    reported as an overlap and not followed (class audit on #3211)."""
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    live = fresh / ".genesis" / "payment_vault"
    _synthetic_set(live, mark="p")
    before = _tree(live)
    _staged(fresh).parent.mkdir(parents=True)
    _staged(fresh).symlink_to(live)
    r = _restore(fresh, _source_with_archive(tmp_path), bind)
    assert "Payment vault: staged at" in r.stdout, r.stdout[-3000:]
    assert "overlaps the live vault" not in r.stdout
    assert not _staged(fresh).is_symlink() and _recipient_mark(_staged(fresh)) == "q"
    assert _tree(live) == before


def test_a_stage_on_the_shells_vault_is_refused_when_secrets_move_genesis_home(
    tmp_path, monkeypatch
):
    """restore reads GENESIS_HOME after secrets.env; the vault CLI uses the shell's.
    Staging onto either one is refused (class audit on #3211)."""
    # restore loads secrets.env only when this is unset; the suite's conftest sets it.
    monkeypatch.delenv("GENESIS_MERGE_OVERRIDE_DIR", raising=False)
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    shell_home = fresh / "shell-home"
    (shell_home / "payment_vault").mkdir(parents=True)
    (fresh / "genesis").mkdir(parents=True)
    (fresh / "genesis" / "secrets.env").write_text(f"GENESIS_HOME={fresh / 'secret-home'}\n")
    r = _restore(
        fresh,
        _source_with_archive(tmp_path),
        bind,
        env_extra={"GENESIS_HOME": str(shell_home), "GENESIS_CREDS_STAGE": str(shell_home)},
    )
    # The refusal names the vault that overlaps (the shell's), not the secrets one.
    assert f"overlaps the live vault {shell_home / 'payment_vault'} " in r.stdout, r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    # Nothing moved aside or placed over the shell's vault (restore keeps its own locks/ dir there).
    assert sorted(p.name for p in shell_home.iterdir() if "payment_vault" in p.name) == [
        "payment_vault"
    ]


def test_restore_says_when_a_staged_vault_is_from_an_earlier_restore(tmp_path):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    _synthetic_set(_staged(fresh))
    src = tmp_path / "source"
    (src / "creds").mkdir(parents=True)
    (tmp_path / "x").write_text("x\n")
    _gpg_encrypt(tmp_path / "x", src / "creds" / "gh_hosts.yml.gpg", tmp_path / "srcgnupg")
    r = _restore(fresh, src, bind)
    assert "holds a vault from an EARLIER restore, not from this backup" in r.stdout, r.stdout[
        -3000:
    ]


def test_restore_dry_run_touches_nothing_in_the_stage(tmp_path):
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    _synthetic_set(_staged(fresh), mark="p")
    before = _tree(_staged(fresh).parent)
    r = _restore(fresh, _source_with_archive(tmp_path), bind, args=("--dry-run",))
    assert "would stage the card vault" in r.stdout, r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    assert _tree(_staged(fresh).parent) == before


@pytest.mark.parametrize(
    "alias", ["equal", "stage inside live", "live inside stage", "via symlink"]
)
def test_a_stage_that_aliases_the_live_vault_is_refused(tmp_path, alias):
    """GENESIS_CREDS_STAGE pointed at (or around) GENESIS_HOME must never stage the
    vault onto the live one, symlink or not."""
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    extra: dict = {}
    if alias == "equal":
        live = fresh / ".genesis" / "payment_vault"
        extra["GENESIS_CREDS_STAGE"] = str(fresh / ".genesis")
    elif alias == "stage inside live":
        live = fresh / ".genesis" / "payment_vault"
        extra["GENESIS_CREDS_STAGE"] = str(live)
    elif alias == "live inside stage":
        gh = fresh / "gh" / "payment_vault" / "home"
        live = gh / "payment_vault"
        extra.update(GENESIS_HOME=str(gh), GENESIS_CREDS_STAGE=str(fresh / "gh"))
    else:
        live = fresh / ".genesis" / "payment_vault"
        (fresh).mkdir(parents=True)
        (fresh / "alias").symlink_to(fresh / ".genesis", target_is_directory=True)
        extra["GENESIS_CREDS_STAGE"] = str(fresh / "alias")
    _synthetic_set(live, mark="p")
    (live / "sentinel").write_text("live\n")
    live.chmod(0o700)
    before = _tree(live)
    r = _restore(fresh, _source_with_archive(tmp_path), bind, env_extra=extra)
    assert "Payment vault: not staged. Its staging path" in r.stdout, r.stdout[-3000:]
    assert "overlaps the live vault" in r.stdout
    assert "Payment vault: staged at" not in r.stdout
    assert _tree(live) == before
    # Nothing was unpacked or moved aside next to it either.
    assert [p.name for p in live.parent.iterdir() if "payment_vault" in p.name] == ["payment_vault"]
    assert _tars_under(fresh) == []


def test_a_vault_from_a_fallback_copy_is_staged_with_a_warning(tmp_path):
    """No creds in the Tier-1 source: §8 falls back to the host mirror, which may
    hold a vault since retired, so the staging says where it came from."""
    bind = _bind(tmp_path)
    fresh = tmp_path / "fresh"
    mirror = tmp_path / "mirror"
    _seal_archive(tmp_path, mirror / "creds" / ARCHIVE)
    src = tmp_path / "source"
    src.mkdir()
    r = _restore(fresh, src, bind, env_extra={"GENESIS_CREDS_MIRROR": str(mirror)})
    assert "using host-side mirror" in r.stdout, r.stdout[-3000:]
    assert "Payment vault: staged at" in r.stdout
    assert f"it came from the fallback copy {mirror / 'creds'}" in r.stdout
    assert "may hold a vault since retired" in r.stdout
    assert _recipient_mark(_staged(fresh)) == "q"
    _assert_no_plaintext_tar(fresh)


# ── the provenance gate: an off-site restore stages only its own snapshot's vault ──


def _snapshot(tmp_path: Path) -> Path:
    snap = tmp_path / "offsite" / "Genesis" / "srchost" / "20261011T000000Z"
    (snap / "creds").mkdir(parents=True)
    (snap / "COMPLETE").write_text("genesis-snapshot 1\n")
    (tmp_path / "gh").write_text("x\n")
    _gpg_encrypt(tmp_path / "gh", snap / "creds" / "gh_hosts.yml.gpg", tmp_path / "srcgnupg")
    return snap


def _assert_not_staged_from_checkout(r, fresh: Path) -> None:
    assert "off-site: pulling latest snapshot" in r.stdout, r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    assert not _staged(fresh).exists()
    _assert_no_plaintext_tar(fresh)


def test_an_older_archive_in_the_checkout_is_not_staged_when_the_snapshot_has_none(tmp_path):
    """An off-site restore reads the selected snapshot's private view, never the checkout."""
    bind = _bind(tmp_path)
    _snapshot(tmp_path)  # the selected snapshot holds creds but no vault archive
    src = _source_with_archive(tmp_path, mark="p")  # an older checkout's archive
    fresh = tmp_path / "fresh"
    r = _restore(fresh, src, bind, offsite=tmp_path / "offsite")
    _assert_not_staged_from_checkout(r, fresh)


def test_a_failed_pull_of_the_archive_is_not_staged(tmp_path):
    bind = _bind(tmp_path)
    snap = _snapshot(tmp_path)
    (snap / "creds" / ARCHIVE).mkdir()  # listed, but the get (a cp) fails
    src = _source_with_archive(tmp_path, mark="p")
    fresh = tmp_path / "fresh"
    r = _restore(fresh, src, bind, offsite=tmp_path / "offsite")
    assert f"failed to pull creds/{ARCHIVE}" in r.stdout, r.stdout[-3000:]
    _assert_not_staged_from_checkout(r, fresh)
    assert r.returncode != 0


@pytest.mark.parametrize(
    "checkout_archive", [True, False], ids=["checkout-has-one", "checkout-has-none"]
)
def test_an_offsite_dry_run_does_not_predict_from_the_checkout(tmp_path, checkout_archive):
    """A dry run pulls no snapshot, so the checkout's archive (or its absence) must
    not be reported as what a real off-site restore would stage."""
    bind = _bind(tmp_path)
    snap = _snapshot(tmp_path)
    _seal_archive(tmp_path, snap / "creds" / ARCHIVE, mark="q")
    if checkout_archive:
        src = _source_with_archive(tmp_path, mark="p")
    else:
        src = tmp_path / "source"
        (src / "creds").mkdir(parents=True)
        (tmp_path / "gh2").write_text("x\n")
        _gpg_encrypt(tmp_path / "gh2", src / "creds" / "gh_hosts.yml.gpg", tmp_path / "srcgnupg")
    fresh = tmp_path / "fresh"
    r = _restore(fresh, src, bind, offsite=tmp_path / "offsite", args=("--dry-run",))
    assert "off-site: (dry-run) would resume or select a snapshot" in r.stdout, r.stdout[-3000:]
    assert "would stage the card vault only if the selected off-site snapshot holds" in r.stdout
    assert "would stage the card vault from" not in r.stdout
    assert not _staged(fresh).exists()
    _assert_no_plaintext_tar(fresh)


def test_an_offsite_dry_run_still_refuses_a_stage_that_aliases_the_live_vault(tmp_path):
    """The overlap refusal does not depend on the snapshot, so the dry run says what
    the real run would: not staged."""
    bind = _bind(tmp_path)
    snap = _snapshot(tmp_path)
    _seal_archive(tmp_path, snap / "creds" / ARCHIVE, mark="q")
    fresh = tmp_path / "fresh"
    live = fresh / ".genesis" / "payment_vault"
    _synthetic_set(live, mark="p")
    before = _tree(live)
    r = _restore(
        fresh,
        _source_with_archive(tmp_path, mark="p"),
        bind,
        offsite=tmp_path / "offsite",
        env_extra={"GENESIS_CREDS_STAGE": str(fresh / ".genesis")},
        args=("--dry-run",),
    )
    assert "overlaps the live vault" in r.stdout, r.stdout[-3000:]
    assert "would stage the card vault" not in r.stdout
    assert _tree(live) == before


def test_a_symlinked_creds_checkout_is_left_alone_and_the_snapshot_vault_staged(tmp_path):
    bind = _bind(tmp_path)
    snap = _snapshot(tmp_path)
    _seal_archive(tmp_path, snap / "creds" / ARCHIVE, mark="q")
    target = tmp_path / "target"
    _seal_archive(tmp_path, target / ARCHIVE, mark="p")  # an archive the link leads to
    before = _tree(target)
    src = tmp_path / "source"
    (src / "memory").mkdir(parents=True)
    # Another payload, so the run reaches §8 (a symlinked creds does not count as one).
    (src / "memory" / "note.md").write_text("remembered\n")
    (src / "creds").symlink_to(target)
    fresh = tmp_path / "fresh"
    r = _restore(fresh, src, bind, offsite=tmp_path / "offsite")
    # The pull writes only restore's private view, so the link is never followed.
    assert "off-site: pulling latest snapshot" in r.stdout, r.stdout[-3000:]
    assert _tree(target) == before
    assert "Payment vault: staged at" in r.stdout, r.stdout[-3000:]
    assert _recipient_mark(_staged(fresh)) == "q"  # the snapshot's, not the link target's


# ── backup.sh and restore.sh, end to end ───────────────────────────────


def _staged_matches(vault: Path, fresh: Path, proc) -> None:
    staged = _staged(fresh)
    assert sorted(p.name for p in staged.iterdir()) == sorted(NAMES), proc.stdout[-3000:]
    for n in ("identity.age", "recipient.txt"):
        assert (staged / n).read_bytes() == (vault / n).read_bytes()
    assert vs.list_cards(staged) == vs.list_cards(vault)
    assert _card_number(staged) == VISA
    assert "Payment vault: staged at" in proc.stdout, proc.stdout[-3000:]
    assert "mv -T" in proc.stdout
    assert f"git rm creds/{ARCHIVE}" in proc.stdout
    assert "every backup reports payment_vault: missing and fails" in proc.stdout
    assert "fallback copy" not in proc.stdout
    assert not (fresh / ".genesis" / "payment_vault").exists(), "restore must never place it"
    _assert_no_plaintext_tar(fresh)


def _tracked(repo: Path, path: str = "creds") -> list[str]:
    return subprocess.run(
        ["git", "ls-files", path], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.split()


def test_round_trip_through_the_clone_and_through_the_offsite_snapshot(
    _sealed, backup_env, tmp_path
):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = dated._run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "Payment vault: 1 archive (encrypted)" in proc.stdout
    status = _status(backup_env)
    assert status["payment_vault"] == "ok" and status["success"] is True, status
    repo = dated._repo(backup_env)
    tracked = _tracked(repo)
    assert f"creds/{ARCHIVE}" in tracked
    assert [t for t in tracked if t.startswith("creds/payment_vault/")] == []
    assert not (repo / "creds" / "payment_vault").exists()
    ignored = subprocess.run(["git", "check-ignore", "-q", f"creds/{TMPNAME}"], cwd=repo)
    assert ignored.returncode == 0, "the staging name must be gitignored"
    (snap,) = dated._snaps(offsite)
    assert (snap / "creds" / ARCHIVE).read_bytes() == (repo / "creds" / ARCHIVE).read_bytes()
    assert not (snap / "creds" / "payment_vault").exists()
    assert _no_card_number_under(repo, offsite, backup_env["home"] / "tmp") == []

    # 1. From the Tier-1 clone (no snapshot selected).
    via_clone = tmp_path / "fresh-clone"
    r = _restore(via_clone, repo, backup_env["bind"])
    _staged_matches(vault, via_clone, r)

    # 2. A no-git box: an empty source dir, everything pulled from the snapshot.
    via_offsite = tmp_path / "fresh-offsite"
    empty = tmp_path / "empty-source"
    empty.mkdir()
    r2 = _restore(via_offsite, empty, backup_env["bind"], offsite=offsite)
    assert "off-site: pulling latest snapshot" in r2.stdout, r2.stdout[-3000:]
    _staged_matches(vault, via_offsite, r2)  # only the snapshot held this archive


def test_no_vault_is_absent_and_changes_nothing(backup_env, tmp_path):
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = dated._run_local(backup_env, offsite)
    assert proc.returncode == 0, proc.stdout
    status = _status(backup_env)
    assert status["payment_vault"] == "absent" and status["success"] is True, status
    creds = dated._repo(backup_env) / "creds"
    assert not (creds / ARCHIVE).exists() and not (creds / "payment_vault").exists()


def test_a_relative_genesis_home_still_finds_the_vault(_sealed, backup_env, tmp_path):
    """§8 runs inside the backups clone, so GENESIS_HOME is made absolute first."""
    _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    env = dict(os.environ)
    env.update(
        HOME=str(backup_env["home"]),
        GENESIS_HOME=".genesis",
        GENESIS_DIR=str(backup_env["gd"]),
        GENESIS_BACKUP_PASSPHRASE="testpass",
        QDRANT_URL="http://127.0.0.1:1",
        GENESIS_BACKUP_TIER2_BACKEND="local",
        GENESIS_BACKUP_LOCAL_PATH=str(offsite),
        PATH=f"{backup_env['bind']}:{os.environ['PATH']}",
    )
    for k in (
        "GENESIS_BACKUP_NAS",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_FORUM_CHAT_ID",
        "GENESIS_BACKUP_EXTRA_DIRS",
        *dated._SESSION_BUS_VARS,
    ):
        env.pop(k, None)
    proc = subprocess.run(
        ["bash", str(dated._BACKUP)],
        env=env,
        cwd=backup_env["home"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    status = _status(backup_env)
    assert status["payment_vault"] == "ok", (status, proc.stdout[-3000:])
    assert (dated._repo(backup_env) / "creds" / ARCHIVE).is_file()


def test_a_vault_backup_failure_fails_the_run_and_update_continues_degraded(
    _sealed, backup_env, tmp_path
):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    assert dated._run_local(backup_env, offsite).returncode == 0
    repo = dated._repo(backup_env)
    earlier = _archive(repo / "creds")
    (vault / "recipient.txt").unlink()
    dated._next_second()
    dated._run_local(backup_env, offsite)
    status = _status(backup_env)
    assert status["success"] is False and status["payment_vault"] == "failed", status
    assert status["failure_class"] == "non_db" and status["failure_stage"] == "payment_vault"
    assert "payment vault backup failed" in status["failure_reason"]
    assert status["tier2_status"] == "ok"  # the rest of the backup still landed
    assert _archive(repo / "creds") == earlier
    gate = subprocess.run(
        [
            "python3",
            str(_GATE),
            str(backup_env["home"] / ".genesis" / "backup_status.json"),
            status["run_id"],
        ],
        capture_output=True,
        text=True,
    )
    assert gate.stdout.strip() == "continue_degraded:backup:payment_vault", gate.stdout


def test_a_vault_that_vanished_fails_the_run_and_keeps_the_last_archive(
    _sealed, backup_env, tmp_path
):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    assert dated._run_local(backup_env, offsite).returncode == 0
    repo = dated._repo(backup_env)
    earlier = _archive(repo / "creds")
    shutil.rmtree(vault)
    dated._next_second()
    dated._run_local(backup_env, offsite)
    status = _status(backup_env)
    assert status["payment_vault"] == "missing" and status["success"] is False, status
    assert "payment vault backup missing" in status["failure_reason"]
    assert _tracked(repo, f"creds/{ARCHIVE}") == [f"creds/{ARCHIVE}"]
    assert _archive(repo / "creds") == earlier
    # The newest snapshot still carries the last archive, so a restore finds it.
    newest = max(dated._snaps(offsite), key=lambda p: p.name)
    assert (newest / "creds" / ARCHIVE).read_bytes() == earlier


def test_no_passphrase_is_skipped_not_a_vault_failure(_sealed, backup_env, tmp_path):
    _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    dated._run_local(backup_env, offsite, {"GENESIS_BACKUP_PASSPHRASE": ""})
    status = _status(backup_env)
    assert status["payment_vault"] == "skipped", status
    assert "payment vault" not in status["failure_reason"]
    assert not (dated._repo(backup_env) / "creds" / ARCHIVE).exists()


@pytest.mark.parametrize("genesis_home", [None, "relocated"])
def test_an_extra_directory_can_never_take_the_vault(tmp_path, genesis_home):
    """The vault is a core path, so GENESIS_BACKUP_EXTRA_DIRS cannot tar a live
    cards.db (which could tear) for restore to place back as a unit."""
    home = tmp_path / "home"
    gh = home / ".genesis" if genesis_home is None else tmp_path / genesis_home
    env = {
        **os.environ,
        "HOME": str(home),
        "GENESIS_DIR": str(home / "genesis"),
        "TRANSCRIPT_DIR": str(home / "t"),
        "_SCRIPT_DIR": str(dated._BACKUP.parent),
    }
    env.pop("GENESIS_HOME", None)
    if genesis_home is not None:
        env["GENESIS_HOME"] = str(gh)
    core = dated._BACKUP.parent / "lib" / "backup_core_paths.sh"
    for probe in (gh / "payment_vault", gh / "payment_vault" / "sub", gh):
        proc = subprocess.run(
            ["bash", "-c", f'source "{core}"; backup_core_overlap "$1"', "x", str(probe)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (probe, proc.stdout, proc.stderr)


def test_a_relative_genesis_home_still_guards_the_vault_after_a_cd(tmp_path):
    """backup.sh sources the core-path lib and then `cd`s into the backups clone:
    a relative GENESIS_HOME must resolve where it was set, not after the cd, or
    the live vault passes the guard (Codex round 3 on #3211)."""
    home = tmp_path / "home"
    (tmp_path / "rel" / "payment_vault").mkdir(parents=True)
    elsewhere = tmp_path / "backups-clone"
    elsewhere.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "GENESIS_DIR": str(home / "genesis"),
        "TRANSCRIPT_DIR": str(home / "t"),
        "_SCRIPT_DIR": str(dated._BACKUP.parent),
        "GENESIS_HOME": "rel",
    }
    core = dated._BACKUP.parent / "lib" / "backup_core_paths.sh"
    probe = tmp_path / "rel" / "payment_vault"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{core}"; cd "$2"; backup_core_overlap "$1"',
            "x",
            str(probe),
            str(elsewhere),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)


def test_a_genesis_home_set_after_sourcing_is_guarded_too(tmp_path):
    """backup.sh loads secrets.env after sourcing the lib; a GENESIS_HOME set there
    (the one genesis-server uses) must still be a core path (architect, round 3)."""
    home = tmp_path / "home"
    later = tmp_path / "server-home"
    env = {
        **os.environ,
        "HOME": str(home),
        "GENESIS_DIR": str(home / "genesis"),
        "TRANSCRIPT_DIR": str(home / "t"),
        "_SCRIPT_DIR": str(dated._BACKUP.parent),
    }
    env.pop("GENESIS_HOME", None)
    core = dated._BACKUP.parent / "lib" / "backup_core_paths.sh"
    for probe in (later / "payment_vault", home / ".genesis" / "payment_vault"):
        proc = subprocess.run(
            [
                "bash",
                "-c",
                f'source "{core}"; export GENESIS_HOME="$2"; backup_core_overlap "$1"',
                "x",
                str(probe),
                str(later),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (probe, proc.stdout, proc.stderr)


def _smb_filesystem_stub(bind: Path, root: Path, log: Path) -> None:
    """A filesystem model of smbclient -c: mkdir is not recursive (and a failed one
    lets the batch go on, which _smb_mkdir relies on); a cd into a missing directory
    stops the batch with an error (an assumption: no real server here)."""
    dated._make_stub(
        bind / "smbclient",
        textwrap.dedent(f"""\
        #!{sys.executable}
        import shlex, shutil, sys
        from pathlib import Path
        root = Path({str(root)!r})
        cmd = sys.argv[sys.argv.index("-c") + 1]
        with open({str(log)!r}, "a") as f:
            f.write(cmd + "\\n")
        # -D DIR starts the batch in DIR (main's atomic rename uses it).
        cwd = root / sys.argv[sys.argv.index("-D") + 1] if "-D" in sys.argv else root
        for clause in cmd.split(";"):
            args = shlex.split(clause)
            if not args:
                continue
            op, *vals = args
            if op == "cd":
                cwd = root / vals[0]
                if not cwd.is_dir():
                    print("NT_STATUS_OBJECT_PATH_NOT_FOUND")
                    sys.exit(1)
            elif op == "mkdir":
                try:
                    (cwd / vals[0]).mkdir()
                except OSError:
                    pass
            elif op == "put":
                shutil.copyfile(vals[0], cwd / vals[1])
            elif op == "get":
                shutil.copyfile(cwd / vals[0], vals[1])
            elif op == "ls":
                entries = [cwd / vals[0]] if vals else sorted(cwd.iterdir())
                for e in entries:
                    if e.exists():
                        print(f"  {{e.name}} {{'D' if e.is_dir() else 'A'}} 0 Synthetic")
            elif op == "deltree":
                shutil.rmtree(cwd / vals[0], ignore_errors=True)
            elif op == "rename":  # smbclient rename SRC DST [-f]
                shutil.move(str(cwd / vals[0]), str(cwd / vals[1]))
        sys.exit(0)
    """),
    )


def test_smb_offsite_puts_the_one_archive_flat_in_creds(_sealed, backup_env, tmp_path):
    _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    nas = tmp_path / "nas"
    nas.mkdir()
    log = tmp_path / "smb.log"
    _smb_filesystem_stub(backup_env["bind"], nas, log)
    proc, _ = dated._run(backup_env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    status = _status(backup_env)
    assert status["tier2_status"] == "ok" and status["payment_vault"] == "ok", status
    (host,) = (nas / "Genesis").iterdir()
    (snap,) = [d for d in host.iterdir() if dated._STAMP_RE.fullmatch(d.name)]
    assert (snap / "COMPLETE").is_file()
    repo = dated._repo(backup_env)
    assert (snap / "creds" / ARCHIVE).read_bytes() == (repo / "creds" / ARCHIVE).read_bytes()
    assert not (snap / "creds" / "payment_vault").exists()
    cmds = log.read_text().splitlines()
    # The put targets the flat creds/ level (checked on disk above); the backend
    # chooses how it reaches that directory.
    assert any(f'put "creds/{ARCHIVE}" "{ARCHIVE}"' in c for c in cmds), cmds
    assert not [c for c in cmds if "mkdir" in c and "creds/payment_vault" in c], cmds


def test_an_unresolved_src_path_refuses_instead_of_importing_from_cwd(_sealed, tmp_path):
    """An empty sys.path entry is the current directory: with src/ unresolved, a
    stray genesis/ there must not stand in for vault_store."""
    vault = _place_vault(_sealed, tmp_path / "vault")
    with sqlite3.connect(vault / "cards.db") as conn:
        conn.execute("CREATE INDEX i ON cards (label)")  # vault_store refuses this set
    stub = tmp_path / "cwd" / "genesis" / "payments"
    stub.mkdir(parents=True)
    for d in (stub.parent, stub):
        (d / "__init__.py").write_text("")
    (stub / "vault_store.py").write_text(
        "class VaultError(Exception):\n    pass\n\ndef verify_set(path):\n    return None\n"
    )
    proc = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; _PV_SRC=""; payment_vault_check "$1"', "x", str(vault)],
        cwd=tmp_path / "cwd",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "cannot find vault_store" in proc.stdout
