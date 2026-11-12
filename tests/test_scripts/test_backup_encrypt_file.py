"""backup.sh's encrypt_file on a source that is still being written (#4391).

A live Claude Code session appends to its transcript while the backup runs.
gpg given a file name records the input size up front and fails when the file
grows mid-read, and a failed gpg removed the previous good copy at the
destination. These tests run the real function body with real gpg.
"""

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from genesis.guardian.cred_integrity import decrypt_backup_file

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
PASSPHRASE = "test-passphrase-4391"

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")


@pytest.fixture(autouse=True)
def _gnupg_home(tmp_path, monkeypatch):
    """A private gpg home for both the encrypting shell and the decrypting check."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))


def _function_body(name: str) -> str:
    text = (SCRIPTS / "backup.sh").read_text()
    body = text.split(name + "() {", 1)[1].split("\n}", 1)[0]
    return name + "() {" + body + "\n}\n"


def _run_encrypt(
    tmp_path: Path, src: Path, dst: Path, *, path_prefix: str = ""
) -> subprocess.CompletedProcess:
    big_tmp = tmp_path / "bigtmp"
    big_tmp.mkdir(exist_ok=True)
    source = 'source "$1"\n'
    source += "log() { printf '%s\\n' \"$*\"; }\n"
    source += _function_body("encrypt_file")
    source += '_BACKUP_PASSPHRASE="$2"\nencrypt_file "$3" "$4"\n'
    env = dict(
        os.environ,
        GENESIS_BIG_TMP=str(big_tmp),
        PATH=path_prefix + os.environ["PATH"],
    )
    return subprocess.run(
        [
            "bash",
            "-s",
            "--",
            str(SCRIPTS / "lib/passphrase_escrow.sh"),
            PASSPHRASE,
            str(src),
            str(dst),
        ],
        input=source,
        text=True,
        capture_output=True,
        env=env,
        timeout=120,
    )


def _decrypt(tmp_path: Path, enc: Path) -> bytes:
    out = tmp_path / "decrypted"
    decrypt_backup_file(enc, out, PASSPHRASE)
    return out.read_bytes()


def _leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if "partial." in p.name)


def test_a_source_growing_during_encryption_is_backed_up(tmp_path):
    """The case behind #4391: the source grows while gpg reads it."""
    src = tmp_path / "session.jsonl"
    line = b'{"type":"assistant","message":"' + b"x" * 4000 + b'"}\n'
    with src.open("wb") as fh:
        for _ in range(16_000):  # about 64 MB, so the read takes a while
            fh.write(line)
    start = src.read_bytes()
    stop = threading.Event()

    def append():
        with src.open("ab") as fh:
            while not stop.is_set():
                fh.write(line)
                fh.flush()
                time.sleep(0.001)

    writer = threading.Thread(target=append)
    writer.start()
    try:
        proc = _run_encrypt(tmp_path, src, tmp_path / "session.jsonl.gpg")
    finally:
        stop.set()
        writer.join()
    assert src.stat().st_size > len(start), "fixture: the source never grew"
    assert proc.returncode == 0, proc.stdout + proc.stderr
    plain = _decrypt(tmp_path, tmp_path / "session.jsonl.gpg")
    assert plain.startswith(start)
    assert _leftovers(tmp_path) == []


def _failing_gpg(tmp_path: Path) -> str:
    """A gpg that writes part of its output file, says why on stderr, and fails."""
    binary = tmp_path / "fakebin"
    binary.mkdir()
    stub = binary / "gpg"
    stub.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        "cat >/dev/null\n"
        'printf "half" > "$out"\n'
        'echo "gpg: simulated write failure: No space left on device" >&2\n'
        "exit 2\n"
    )
    stub.chmod(0o700)
    return str(binary) + ":"


def test_a_long_destination_name_still_encrypts(tmp_path):
    """The partial's name must not grow with the destination's: a destination
    name at the directory's own length limit still encrypts, where a partial
    named after it could not exist. Sized from the filesystem, which differs
    between test sandboxes."""
    name_max = os.pathconf(tmp_path, "PC_NAME_MAX")
    src = tmp_path / ("m" * (name_max - 4))
    src.write_text("memory\n")
    dst = tmp_path / ("m" * (name_max - 4) + ".gpg")
    proc = _run_encrypt(tmp_path, src, dst)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _decrypt(tmp_path, dst) == b"memory\n"
    assert _leftovers(tmp_path) == []


def test_a_partial_removed_mid_run_never_replaces_the_copy(tmp_path):
    """Another run's sweep may delete this run's partial while gpg writes it. The
    stamp step must not recreate it empty and move that over the good copy."""
    binary = tmp_path / "fakebin"
    binary.mkdir()
    stub = binary / "gpg"
    stub.write_text(
        "#!/bin/sh\n"
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        "cat >/dev/null\n"
        'printf "ciphertext" > "$out"\n'
        'rm -f "$out"\n'  # a concurrent sweep removed it
        "exit 0\n"
    )
    stub.chmod(0o700)
    src = tmp_path / "session.jsonl"
    src.write_text("content\n")
    dst = tmp_path / "session.jsonl.gpg"
    dst.write_bytes(b"previous good copy")
    proc = _run_encrypt(tmp_path, src, dst, path_prefix=str(binary) + ":")
    assert dst.read_bytes() == b"previous good copy", "an empty partial replaced the copy"
    assert proc.returncode != 0
    assert _leftovers(tmp_path) == []


def test_a_failed_encryption_keeps_the_previous_copy_and_names_the_cause(tmp_path):
    src = tmp_path / "session.jsonl"
    src.write_text("new content\n")
    dst = tmp_path / "session.jsonl.gpg"
    dst.write_bytes(b"previous good copy")
    proc = _run_encrypt(tmp_path, src, dst, path_prefix=_failing_gpg(tmp_path))
    assert dst.read_bytes() == b"previous good copy", "the failed run destroyed the old copy"
    assert "simulated write failure: No space left on device" in proc.stderr
    assert proc.stdout == "", "stdout is a caller's result channel (backup_payment_vault)"
    assert _leftovers(tmp_path) == []
    assert proc.returncode != 0
    assert PASSPHRASE not in proc.stdout + proc.stderr
    assert list((tmp_path / "bigtmp").iterdir()) == []


def test_an_unreadable_source_fails_and_keeps_the_previous_copy(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-000 file")
    src = tmp_path / "session.jsonl"
    src.write_text("content\n")
    src.chmod(0)
    dst = tmp_path / "session.jsonl.gpg"
    dst.write_bytes(b"previous good copy")
    try:
        proc = _run_encrypt(tmp_path, src, dst)
    finally:
        src.chmod(0o600)
    assert proc.returncode != 0
    assert dst.read_bytes() == b"previous good copy"
    assert _leftovers(tmp_path) == []


def test_success_leaves_no_partial_and_the_freshness_skip_still_works(tmp_path):
    """The copy is stamped just after the source's pre-read mtime.

    The callers skip a file whose copy is newer than its source. An unchanged
    source must keep being skipped, and a later append must be re-encrypted.
    """
    src = tmp_path / "session.jsonl"
    src.write_text("line one\n")
    an_hour_ago = time.time_ns() - 3600 * 10**9
    os.utime(src, ns=(an_hour_ago, an_hour_ago))
    dst = tmp_path / "session.jsonl.gpg"
    proc = _run_encrypt(tmp_path, src, dst)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _leftovers(tmp_path) == []
    assert _decrypt(tmp_path, dst) == b"line one\n"
    assert list((tmp_path / "bigtmp").iterdir()) == []

    assert dst.stat().st_mtime_ns == an_hour_ago + 1
    assert _newer(dst, src), "an unchanged source would be re-encrypted on every run"
    with src.open("a") as fh:
        fh.write("line two\n")
    assert not _newer(dst, src), "an append after the read would never be backed up"


def _newer(a: Path, b: Path) -> bool:
    return subprocess.run(["bash", "-c", '[ "$1" -nt "$2" ]', "_", str(a), str(b)]).returncode == 0


def test_a_source_written_moments_ago_is_encrypted_again_next_run(tmp_path):
    """An append within the same timestamp tick does not move the mtime, so a
    source written moments ago gets a copy stamped at exactly its mtime, which
    the callers' -nt skip does not trust. A source mtime ahead of the clock
    (skew) takes the same branch. The future mtime keeps this independent of
    how long the encryption takes on a loaded runner."""
    src = tmp_path / "session.jsonl"
    src.write_text("just written\n")
    future = time.time_ns() + 30 * 10**9
    os.utime(src, ns=(future, future))
    dst = tmp_path / "session.jsonl.gpg"
    proc = _run_encrypt(tmp_path, src, dst)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert dst.stat().st_mtime_ns == future
    assert not _newer(dst, src)


def test_a_directory_at_the_destination_is_a_failure(tmp_path):
    src = tmp_path / "session.jsonl"
    src.write_text("content\n")
    dst = tmp_path / "session.jsonl.gpg"
    dst.mkdir()
    proc = _run_encrypt(tmp_path, src, dst)
    assert proc.returncode != 0
    assert list(dst.iterdir()) == [], (
        "the copy was moved into the directory and reported as written"
    )
    assert _leftovers(tmp_path) == []
