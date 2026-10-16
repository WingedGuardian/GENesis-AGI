"""Exercise the actual shell encryption/decryption entry points before GPG."""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


@pytest.mark.parametrize(
    "password", ["", "spaces stay ", 'quote"literal', "unicode-λ", "carriage\rreturn"]
)
def test_shell_validation_preserves_supported_strings(password):
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; backup_passphrase_valid "$2"',
            "test",
            str(SCRIPTS / "lib/passphrase_escrow.sh"),
            password,
        ],
        capture_output=True,
        timeout=5,
    )
    assert proc.returncode == 0
    assert proc.stdout == proc.stderr == b""


@pytest.mark.parametrize(
    "script,function",
    [
        ("backup.sh", "encrypt_file"),
        ("backup.sh", "_roundtrip_ok"),
        ("restore.sh", "decrypt_file"),
    ],
)
@pytest.mark.parametrize("password", ["prefix\nsuffix", "prefix\n", "\nprefix"])
def test_shell_entrypoints_reject_truncated_keys_before_gpg(tmp_path, script, function, password):
    text = (SCRIPTS / script).read_text()
    body = text.split(function + "() {", 1)[1].split("\n}", 1)[0]
    invoked = tmp_path / "gpg-invoked"
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "gpg"
    stub.write_text('#!/bin/sh\ntouch "$GPG_INVOKED"\nexit 1\n')
    stub.chmod(0o700)
    env = dict(
        os.environ,
        PATH=str(binary) + ":" + os.environ["PATH"],
        GPG_INVOKED=str(invoked),
        GENESIS_BIG_TMP=str(tmp_path),
    )
    source = 'source "$1"\n' + function + "() {" + body + "\n}\n"
    source += '_BACKUP_PASSPHRASE="$2"\n' + function + ' "$2" "$3"\n'
    proc = subprocess.run(
        [
            "bash",
            "-s",
            "--",
            str(SCRIPTS / "lib/passphrase_escrow.sh"),
            password,
            str(tmp_path / "payload.gpg"),
        ],
        input=source,
        text=True,
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert proc.returncode == 1, proc.stderr
    assert not invoked.exists()
    assert not (tmp_path / "payload.gpg").exists()
    assert password not in proc.stdout + proc.stderr


@pytest.mark.parametrize(
    "raw,valid",
    [
        (b"GENESIS_BACKUP_PASSPHRASE=key\n", True),
        (b"GENESIS_BACKUP_PASSPHRASE=key\r\n", True),
        (b"GENESIS_BACKUP_PASSPHRASE=key\0\n", False),
        (b"\0GENESIS_BACKUP_PASSPHRASE=key\n", False),
    ],
)
def test_shell_file_guard_checks_raw_nul_before_parsing(tmp_path, raw, valid):
    path = tmp_path / "key.env"
    path.write_bytes(raw)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; backup_passphrase_file_valid "$2"',
            "test",
            str(SCRIPTS / "lib/passphrase_escrow.sh"),
            str(path),
        ],
        capture_output=True,
        timeout=5,
    )
    assert (proc.returncode == 0) is valid
    assert proc.stdout == proc.stderr == b""


def test_shell_escrow_lookup_never_normalizes_nul_to_a_key(tmp_path):
    escrow = tmp_path / "invalid.env"
    escrow.write_bytes(b"GENESIS_BACKUP_PASSPHRASE=prefix\0\n")
    env = dict(os.environ, HOME=str(tmp_path), GENESIS_PASSPHRASE_ESCROW=str(escrow))
    proc = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; passphrase_escrow_lookup; printf "%s" "$ESCROW_PASSPHRASE"',
            "test",
            str(SCRIPTS / "lib/passphrase_escrow.sh"),
        ],
        capture_output=True,
        env=env,
        timeout=5,
    )
    assert proc.returncode == 0 and proc.stdout == proc.stderr == b""
