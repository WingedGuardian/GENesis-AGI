"""The card vault in backup.sh §8 and restore.sh §8 (scripts/lib/payment_vault_backup.sh).

Producer to consumer with the real scripts: a real vault (vault_store, one Stripe
public test card) goes through the real backup.sh on the `local` and a
filesystem-backed `smb` backend, then through the real restore.sh, both from the
Tier-1 clone and from the off-site snapshot alone. The lib's states are driven
directly. Sandboxed like test_backup_dated_snapshots (HOME, GENESIS_DIR, GNUPGHOME
in tmp; systemctl and curl stubbed).
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
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
FUTURE = f"12/{(datetime.now(UTC).year + 2) % 100:02d}"
NAMES = ("identity.age", "recipient.txt", "cards.db")


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
        vs.parse_card(VISA, FUTURE, "123"), label="test", merchant=None, single_use=True, path=dest
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


def _status(env) -> dict:
    return json.loads((env["home"] / ".genesis" / "backup_status.json").read_text())


# ── the lib, driven directly ───────────────────────────────────────────


def _lib(tmp_path, vault: Path, out: Path, *, encrypt="gpg", pre="") -> subprocess.CompletedProcess:
    gnupg = tmp_path / "gnupg"
    gnupg.mkdir(mode=0o700, exist_ok=True)
    big = tmp_path / "bigtmp"
    big.mkdir(exist_ok=True)
    enc = {
        "gpg": "printf %s testpass | gpg --batch --yes --no-symkey-cache --passphrase-fd 0 "
        '--symmetric --cipher-algo AES256 -o "$2" "$1" 2>/dev/null',
        "fail-cards": '[ "$(basename "$1")" = cards.db ] && return 1; cp "$1" "$2"',
    }[encrypt]
    script = textwrap.dedent(f"""\
        set -euo pipefail
        source "{_LIB}"
        encrypt_file() {{ {enc}; }}
        {pre}
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


def _gpgs(out: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(out.glob("*.gpg"))} if out.is_dir() else {}


def test_file_list_matches_the_vault_store(tmp_path):
    proc = subprocess.run(
        ["bash", "-c", f'source "{_LIB}"; printf "%s\\n" "${{PAYMENT_VAULT_FILES[@]}}"'],
        capture_output=True,
        text=True,
        check=True,
    )
    assert tuple(proc.stdout.split()) == (vs.IDENTITY, vs.RECIPIENT, vs.CARDS) == NAMES


def test_ok_writes_an_encrypted_set_that_decrypts_to_a_working_vault(_sealed, tmp_path):
    vault = _place_vault(_sealed, tmp_path / "vault")
    out = tmp_path / "out"
    proc = _lib(tmp_path, vault, out)
    assert proc.stdout.strip() == "ok", proc.stdout + proc.stderr
    assert sorted(_gpgs(out)) == sorted(f"{n}.gpg" for n in NAMES)
    assert list(out.glob(".*")) == []  # no staging names left behind
    back = tmp_path / "back"
    back.mkdir()
    for n in NAMES:
        _gpg_decrypt(out / f"{n}.gpg", back / n, tmp_path / "gnupg")
    assert (back / "identity.age").read_bytes() == (vault / "identity.age").read_bytes()
    assert (back / "recipient.txt").read_bytes() == (vault / "recipient.txt").read_bytes()
    assert vs.list_cards(back) == vs.list_cards(vault)
    assert _card_number(back) == VISA
    # The backup copy itself never holds the number (the blob is age-encrypted).
    assert _no_card_number_under(out, back) == []
    assert list((tmp_path / "bigtmp").iterdir()) == []  # the staging dir is removed


def test_absent_and_missing(tmp_path):
    out = tmp_path / "out"
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "absent"
    out.mkdir()
    (out / "cards.db.gpg").write_bytes(b"earlier")
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "missing"
    assert (out / "cards.db.gpg").read_bytes() == b"earlier"


@pytest.mark.parametrize("with_recipient", [False, True])
def test_a_directory_with_no_key_is_no_vault(tmp_path, with_recipient):
    """An init killed before identity.age landed (vault_store places recipient.txt
    first) is not initialised: there is no key to keep, and init can rerun."""
    vault = tmp_path / "vault"
    vault.mkdir()
    sqlite3.connect(vault / "cards.db").close()
    if with_recipient:
        (vault / "recipient.txt").write_text("age1" + "q" * 58 + "\n")
    out = tmp_path / "out"
    assert _lib(tmp_path, vault, out).stdout.split()[0] == "absent"
    out.mkdir()
    (out / "identity.age.gpg").write_bytes(b"earlier")
    assert _lib(tmp_path, vault, out).stdout.split()[0] == "missing"


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("rm recipient.txt", "recipient.txt is missing"),
        ("rm cards.db", "cards.db is missing"),
        ("link cards.db", "cards.db is missing or not a regular file"),
        ("corrupt cards.db", "integrity check"),
        ("garble identity.age", "identity.age is not an age file"),
        ("garble recipient.txt", "recipient.txt does not hold an age public key"),
        ("zero cards.db", "format version unset"),  # PRAGMA user_version = 0
        ("encrypt fails", "encrypting cards.db failed"),
    ],
)
def test_failed_keeps_the_earlier_set_untouched(_sealed, tmp_path, damage, reason):
    vault = _place_vault(_sealed, tmp_path / "vault")
    out = tmp_path / "out"
    assert _lib(tmp_path, vault, out).stdout.strip() == "ok"
    earlier = _gpgs(out)
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
    elif verb == "zero":
        with sqlite3.connect(target) as conn:
            conn.execute("PRAGMA user_version = 0")
    else:
        encrypt = "fail-cards"
    proc = _lib(tmp_path, vault, out, encrypt=encrypt)
    state, _, why = proc.stdout.strip().partition(" ")
    assert state == "failed" and reason in why, proc.stdout + proc.stderr
    assert _gpgs(out) == earlier
    assert list(out.glob(".*")) == []
    assert list((tmp_path / "bigtmp").iterdir()) == []


def test_a_symlinked_vault_directory_is_refused(_sealed, tmp_path):
    real = _place_vault(_sealed, tmp_path / "real")
    (tmp_path / "vault").symlink_to(real)
    proc = _lib(tmp_path, tmp_path / "vault", tmp_path / "out")
    assert proc.stdout.startswith("failed the vault path is not a directory"), proc.stdout


def test_staging_leftovers_of_a_killed_run_are_removed(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / ".cards.db.gpg.tmp").write_bytes(b"half")
    assert _lib(tmp_path, tmp_path / "none", out).stdout.split()[0] == "absent"
    assert not (out / ".cards.db.gpg.tmp").exists()


def test_waits_for_a_running_vault_init(_sealed, tmp_path):
    """The backup takes the directory lock `vault init` holds, so it never reads
    a half-made vault."""
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
        proc = _lib(tmp_path, vault, tmp_path / "out")
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


# ── backup.sh and restore.sh, end to end ───────────────────────────────


def _restore(env_dir: Path, source: Path, bind: Path, *, offsite: Path | None = None):
    fresh = env_dir
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
    env.pop("GENESIS_HOME", None)
    if offsite is not None:
        env.update(GENESIS_BACKUP_TIER2_BACKEND="local", GENESIS_BACKUP_LOCAL_PATH=str(offsite))
    return subprocess.run(
        ["bash", str(dated._RESTORE), "--from", str(source), "--force"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _staged_matches(vault: Path, fresh: Path, proc) -> None:
    staged = fresh / ".genesis" / "restore-creds" / "payment_vault"
    assert sorted(p.name for p in staged.iterdir()) == sorted(NAMES), proc.stdout[-3000:]
    for n in ("identity.age", "recipient.txt"):
        assert (staged / n).read_bytes() == (vault / n).read_bytes()
    assert vs.list_cards(staged) == vs.list_cards(vault)
    assert _card_number(staged) == VISA
    assert "Payment vault: staged at" in proc.stdout, proc.stdout[-3000:]
    assert "mv -T" in proc.stdout
    assert "every backup reports payment_vault: missing and fails" in proc.stdout
    assert not (fresh / ".genesis" / "payment_vault").exists(), "restore must never place it"


def test_round_trip_through_the_clone_and_through_the_offsite_snapshot(
    _sealed, backup_env, tmp_path
):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = dated._run_local(backup_env, offsite)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    status = _status(backup_env)
    assert status["payment_vault"] == "ok" and status["success"] is True, status
    repo = dated._repo(backup_env)
    tracked = subprocess.run(
        ["git", "ls-files", "creds/payment_vault"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert sorted(tracked) == sorted(f"creds/payment_vault/{n}.gpg" for n in NAMES)
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", "creds/payment_vault/.cards.db.gpg.tmp"], cwd=repo
    )
    assert ignored.returncode == 0, "staging names must be gitignored"
    (snap,) = dated._snaps(offsite)
    assert sorted(p.name for p in (snap / "creds" / "payment_vault").iterdir()) == sorted(
        f"{n}.gpg" for n in NAMES
    )
    assert _no_card_number_under(repo, offsite, backup_env["home"] / "tmp") == []

    # 1. From the Tier-1 clone.
    via_clone = tmp_path / "fresh-clone"
    r = _restore(via_clone, repo, backup_env["bind"])
    _staged_matches(vault, via_clone, r)

    # 2. A no-git box: an empty source dir, everything pulled from the snapshot.
    via_offsite = tmp_path / "fresh-offsite"
    empty = tmp_path / "empty-source"
    empty.mkdir()
    r2 = _restore(via_offsite, empty, backup_env["bind"], offsite=offsite)
    assert "pulled creds/payment_vault/cards.db.gpg" in r2.stdout, r2.stdout[-3000:]
    _staged_matches(vault, via_offsite, r2)


def test_no_vault_is_absent_and_changes_nothing(backup_env, tmp_path):
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    proc = dated._run_local(backup_env, offsite)
    assert proc.returncode == 0, proc.stdout
    status = _status(backup_env)
    assert status["payment_vault"] == "absent" and status["success"] is True, status
    assert not (dated._repo(backup_env) / "creds" / "payment_vault").exists()


def test_a_vault_backup_failure_fails_the_run_and_update_continues_degraded(
    _sealed, backup_env, tmp_path
):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    assert dated._run_local(backup_env, offsite).returncode == 0
    repo = dated._repo(backup_env)
    earlier = _gpgs(repo / "creds" / "payment_vault")
    (vault / "recipient.txt").unlink()
    dated._next_second()
    dated._run_local(backup_env, offsite)
    status = _status(backup_env)
    assert status["success"] is False and status["payment_vault"] == "failed", status
    assert status["failure_class"] == "non_db" and status["failure_stage"] == "payment_vault"
    assert "payment vault backup failed" in status["failure_reason"]
    assert status["tier2_status"] == "ok"  # the rest of the backup still landed
    assert _gpgs(repo / "creds" / "payment_vault") == earlier
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


def test_a_vault_that_vanished_fails_the_run_and_keeps_the_last_set(_sealed, backup_env, tmp_path):
    vault = _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    assert dated._run_local(backup_env, offsite).returncode == 0
    shutil.rmtree(vault)
    dated._next_second()
    dated._run_local(backup_env, offsite)
    status = _status(backup_env)
    assert status["payment_vault"] == "missing" and status["success"] is False, status
    assert "payment vault backup missing" in status["failure_reason"]
    tracked = subprocess.run(
        ["git", "ls-files", "creds/payment_vault"],
        cwd=dated._repo(backup_env),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert len(tracked) == 3
    # The newest snapshot still carries the last set, so a restore finds it.
    newest = max(dated._snaps(offsite), key=lambda p: p.name)
    assert len(list((newest / "creds" / "payment_vault").glob("*.gpg"))) == 3


def test_no_passphrase_is_skipped_not_a_vault_failure(_sealed, backup_env, tmp_path):
    _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    dated._run_local(backup_env, offsite, {"GENESIS_BACKUP_PASSPHRASE": ""})
    status = _status(backup_env)
    assert status["payment_vault"] == "skipped", status
    assert "payment vault" not in status["failure_reason"]


def _smb_filesystem_stub(bind: Path, root: Path, log: Path) -> None:
    """A filesystem model of smbclient -c: mkdir is not recursive (and a failed one
    lets the batch go on, which _smb_mkdir relies on); a cd into a missing directory
    stops the batch with an error (an assumption: no real server here, and the
    code under test makes the directory first either way)."""
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
        cwd = root
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
        sys.exit(0)
    """),
    )


def test_smb_offsite_creates_the_vault_directory_before_putting_into_it(
    _sealed, backup_env, tmp_path
):
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
    assert sorted(p.name for p in (snap / "creds" / "payment_vault").iterdir()) == sorted(
        f"{n}.gpg" for n in NAMES
    )
    cmds = log.read_text().splitlines()
    mk = next(i for i, c in enumerate(cmds) if c.rstrip("; ").endswith('/creds/payment_vault"'))
    put = next(i for i, c in enumerate(cmds) if 'creds/payment_vault"; put' in c)
    assert mk < put, cmds


def _synthetic_set(dest: Path, *, cards_ok: bool = True) -> None:
    """A set that passes payment_vault_check without a scrypt init."""
    dest.mkdir(parents=True)
    (dest / "identity.age").write_bytes(b"age-encryption.org/v1\n-> scrypt x\n")
    (dest / "recipient.txt").write_text("age1" + "q" * 58 + "\n")
    if cards_ok:
        with sqlite3.connect(dest / "cards.db") as conn:
            conn.execute("PRAGMA user_version = 1")
            conn.execute("CREATE TABLE cards (card_ref TEXT)")
    else:
        (dest / "cards.db").write_bytes(b"not sqlite" * 100)


def _encrypt_into(src_dir: Path, out_dir: Path, gnupg: Path, names=NAMES) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for n in names:
        subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--homedir",
                str(gnupg),
                "--passphrase",
                "testpass",
                "--symmetric",
                "--cipher-algo",
                "AES256",
                "-o",
                str(out_dir / f"{n}.gpg"),
                str(src_dir / n),
            ],
            check=True,
            capture_output=True,
        )


def _source_with(tmp_path, *, cards_ok=True, names=NAMES) -> Path:
    gnupg = tmp_path / "srcgnupg"
    gnupg.mkdir(mode=0o700)
    plain = tmp_path / "plain"
    _synthetic_set(plain, cards_ok=cards_ok)
    src = tmp_path / "source"
    _encrypt_into(plain, src / "creds" / "payment_vault", gnupg, names)
    return src


def test_restore_warns_when_the_staged_set_fails_its_check(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    src = _source_with(tmp_path, cards_ok=False)
    r = _restore(tmp_path / "fresh", src, bind)
    assert (
        "Payment vault: the staged set failed its check (cards.db failed its integrity check)"
        in r.stdout
    ), r.stdout[-3000:]
    assert "Payment vault: staged at" not in r.stdout
    assert r.returncode != 0


def test_restore_never_mixes_a_stale_staged_file_into_a_new_set(tmp_path):
    """A staged cards.db from an earlier restore must not complete a set whose own
    cards.db is not in this backup."""
    bind = tmp_path / "bin"
    bind.mkdir()
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    fresh = tmp_path / "fresh"
    _synthetic_set(fresh / ".genesis" / "restore-creds" / "payment_vault")  # an earlier restore
    src = _source_with(tmp_path, names=("identity.age", "recipient.txt"))
    r = _restore(fresh, src, bind)
    assert not (fresh / ".genesis" / "restore-creds" / "payment_vault" / "cards.db").exists()
    assert "the staged set failed its check (cards.db is missing" in r.stdout, r.stdout[-3000:]
    assert r.returncode != 0


def test_restore_says_when_a_staged_set_is_from_an_earlier_restore(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    fresh = tmp_path / "fresh"
    _synthetic_set(fresh / ".genesis" / "restore-creds" / "payment_vault")
    src = tmp_path / "source"
    (src / "creds").mkdir(parents=True)
    gnupg = tmp_path / "g"
    gnupg.mkdir(mode=0o700)
    (tmp_path / "x").write_text("x\n")
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(gnupg),
            "--passphrase",
            "testpass",
            "--symmetric",
            "-o",
            str(src / "creds" / "gh_hosts.yml.gpg"),
            str(tmp_path / "x"),
        ],
        check=True,
        capture_output=True,
    )
    r = _restore(fresh, src, bind)
    assert "holds a vault set from an EARLIER restore" in r.stdout, r.stdout[-3000:]


def test_restore_dry_run_touches_no_staged_vault(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    fresh = tmp_path / "fresh"
    earlier = fresh / ".genesis" / "restore-creds" / "payment_vault"
    _synthetic_set(earlier)
    src = _source_with(tmp_path)
    env_bind = bind
    (fresh / "genesis" / "data").mkdir(parents=True)
    env = {
        **{k: v for k, v in os.environ.items() if k not in dated._SESSION_BUS_VARS},
        "HOME": str(fresh),
        "GENESIS_DIR": str(fresh / "genesis"),
        "GENESIS_BACKUP_TMPDIR": str(fresh),
        "GENESIS_BACKUP_TIER2_BACKEND": "none",
        "GENESIS_BACKUP_PASSPHRASE": "testpass",
        "GENESIS_RESTORE_HOLDER_SCAN": "none",
        "GNUPGHOME": str(tmp_path / "srcgnupg"),
        "PATH": f"{env_bind}:{os.environ['PATH']}",
    }
    env.pop("GENESIS_HOME", None)
    r = subprocess.run(
        ["bash", str(dated._RESTORE), "--from", str(src), "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert sorted(p.name for p in earlier.iterdir()) == sorted(NAMES), r.stdout[-2000:]
    assert "Payment vault: staged at" not in r.stdout


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


def test_a_symlinked_backup_directory_is_refused(_sealed, tmp_path):
    """A symlink at creds/payment_vault (or creds) in the backups clone would send
    the set outside the repo while the run reported ok."""
    vault = _place_vault(_sealed, tmp_path / "vault")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "out").symlink_to(elsewhere)
    proc = _lib(tmp_path, vault, tmp_path / "out")
    assert proc.stdout.startswith("failed the backup directory is a symlink"), proc.stdout
    assert list(elsewhere.iterdir()) == []
    creds = tmp_path / "creds"
    creds.symlink_to(elsewhere)
    proc = _lib(tmp_path, vault, creds / "payment_vault")
    assert proc.stdout.startswith("failed the backup directory is a symlink"), proc.stdout
    assert list(elsewhere.iterdir()) == []


def test_smb_refuses_names_that_would_break_out_of_the_command_string(
    _sealed, backup_env, tmp_path
):
    """Names in the backups clone reach `smbclient -c` inside quotes; one holding a
    quote or ';' is refused (the snapshot is partial), never sent."""
    _place_vault(_sealed, backup_env["home"] / ".genesis" / "payment_vault")
    nas = tmp_path / "nas"
    nas.mkdir()
    log = tmp_path / "smb.log"
    _smb_filesystem_stub(backup_env["bind"], nas, log)
    bad = dated._repo(backup_env) / "creds" / 'x"; mkdir "pwned'
    bad.mkdir(parents=True)
    (bad / "a.gpg").write_bytes(b"x")
    (dated._repo(backup_env) / "creds" / "semi;colon.gpg").write_bytes(b"x")
    proc, _ = dated._run(backup_env)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    cmds = log.read_text()
    assert "pwned" not in cmds and "semi;colon" not in cmds, cmds
    assert not list(nas.rglob("pwned"))
    status = _status(backup_env)
    assert status["tier2_status"] == "partial", status  # the refused puts count as failed
    assert status["payment_vault"] == "ok"


def test_restore_does_not_pull_into_a_symlinked_creds_directory(tmp_path):
    bind = tmp_path / "bin"
    bind.mkdir()
    dated._make_stub(bind / "systemctl", "#!/usr/bin/env bash\nexit 3\n")
    gnupg = tmp_path / "srcgnupg"
    gnupg.mkdir(mode=0o700)
    plain = tmp_path / "plain"
    _synthetic_set(plain)
    offsite = tmp_path / "offsite"
    snap = offsite / "Genesis" / "srchost" / "20261011T000000Z"
    _encrypt_into(plain, snap / "creds" / "payment_vault", gnupg)
    (snap / "COMPLETE").write_text("genesis-snapshot 1\n")
    src = tmp_path / "source"
    (src / "creds").mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    (src / "creds" / "payment_vault").symlink_to(target)
    fresh = tmp_path / "fresh"
    r = _restore(fresh, src, bind, offsite=offsite)
    assert "creds/payment_vault in the backup checkout is a symlink" in r.stdout, r.stdout[-3000:]
    assert list(target.iterdir()) == []
    assert r.returncode != 0
