"""GPG must receive the whole configured passphrase, never a truncated prefix."""

import shutil
import subprocess
from pathlib import Path

import pytest

from genesis.guardian import cred_integrity as ci
from genesis.guardian import credential_bridge as bridge


@pytest.mark.parametrize("password", ["prefix\nsuffix", "prefix\0suffix", "prefix\n"])
def test_decryption_rejects_terminators_before_spawning(tmp_path, monkeypatch, password):
    calls = []

    def invoked(*args, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(args[0], 0, b"synthetic payload", b"")

    monkeypatch.setattr(ci.subprocess, "run", invoked)
    with pytest.raises(ci._DecryptError, match="single line"):
        ci._gpg_decrypt(tmp_path / "payload.gpg", password)
    assert calls == []


@pytest.mark.parametrize(
    "password", ["spaces stay ", "quote\"and'literal", "unicode-λ", "carriage\rreturn"]
)
def test_decryption_preserves_valid_passphrase_bytes(tmp_path, monkeypatch, password):
    if not shutil.which("gpg"):
        pytest.skip("gpg not available")
    home = tmp_path / "gpg"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    source, encrypted = tmp_path / "source", tmp_path / "payload.gpg"
    source.write_bytes(b"synthetic payload")
    actual = subprocess.run
    calls = []

    def invoked(*args, **kwargs):
        calls.append(kwargs["input"])
        return actual(*args, **kwargs)

    try:
        actual(
            ["gpg", "--no-options", "--batch", "--yes", "--no-symkey-cache",
             "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--symmetric",
             "--output", str(encrypted), str(source)],
            input=password.encode("utf-8"), capture_output=True, check=True,
        )
        monkeypatch.setattr(ci.subprocess, "run", invoked)
        assert ci._gpg_decrypt(encrypted, password) == source.read_bytes()
        assert calls == [password.encode("utf-8")]
    finally:
        actual(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
               capture_output=True, check=False)


@pytest.mark.parametrize(
    "password", [" key ", "\tkey\t", "key\r", "  ", "quote\"and'literal", "λ-key", "key\n", "\nkey"]
)
def test_environment_resolution_preserves_original_value(tmp_path, monkeypatch, password):
    monkeypatch.setenv("GENESIS_BACKUP_PASSPHRASE", password)
    assert ci.resolve_passphrase(tmp_path) == password


@pytest.mark.parametrize(
    "line,expected",
    [
        ('GENESIS_BACKUP_PASSPHRASE=" key "', " key "),
        ("export GENESIS_BACKUP_PASSPHRASE=' key\t'", " key\t"),
        ('GENESIS_BACKUP_PASSPHRASE="key\r"', "key\r"),
        ('GENESIS_BACKUP_PASSPHRASE="  "', "  "),
        ("GENESIS_BACKUP_PASSPHRASE=literal\"quote'key", "literal\"quote'key"),
        ('GENESIS_BACKUP_PASSPHRASE="λ-key" # comment', "λ-key"),
        ("GENESIS_BACKUP_PASSPHRASE= key # comment", " key"),
    ],
)
def test_actual_escrow_writer_and_both_readers_preserve_key(tmp_path, monkeypatch, line, expected):
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    secrets = tmp_path / "genesis/secrets.env"
    secrets.parent.mkdir()
    secrets.write_bytes((line + "\n").encode())
    loader = Path(__file__).parents[2] / "scripts/lib/load_secrets.sh"
    loaded = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; load_secrets_file "$2"; printf %s "$GENESIS_BACKUP_PASSPHRASE"',
            "passphrase-parity",
            str(loader),
            str(secrets),
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "LC_ALL": "C.UTF-8"},
        check=True,
        capture_output=True,
    )
    assert loaded.stdout == expected.encode()
    assert ci.resolve_passphrase(tmp_path) == expected
    shared = tmp_path / ".genesis/shared"
    escrow = bridge.propagate_backup_passphrase(shared_dir=shared, secrets_path=secrets)
    assert escrow.read_bytes() == ("GENESIS_BACKUP_PASSPHRASE=" + expected + "\n").encode()
    secrets.unlink()
    assert ci.resolve_passphrase(tmp_path) == expected
    assert bridge.load_backup_passphrase(str(tmp_path / ".genesis")) == {
        "GENESIS_BACKUP_PASSPHRASE": expected
    }


def test_corrupt_nul_file_does_not_replace_existing_escrow(tmp_path):
    secrets, shared = tmp_path / "secrets.env", tmp_path / "shared"
    secrets.write_bytes(b"GENESIS_BACKUP_PASSPHRASE=prior-key\n")
    escrow = bridge.propagate_backup_passphrase(shared_dir=shared, secrets_path=secrets)
    original = escrow.read_bytes()
    secrets.write_bytes(b"GENESIS_BACKUP_PASSPHRASE=prefix\0suffix\n")
    assert bridge.propagate_backup_passphrase(shared_dir=shared, secrets_path=secrets) is None
    assert escrow.read_bytes() == original


@pytest.mark.parametrize(
    "raw", [b"GENESIS_BACKUP_PASSPHRASE=key\0\n", b"GENESIS_BACKUP_PASSPHRASE=\xff\n"]
)
def test_corrupt_escrow_remains_unavailable(tmp_path, monkeypatch, raw):
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    escrow = tmp_path / ".genesis/shared/guardian/backup_passphrase.env"
    escrow.parent.mkdir(parents=True)
    escrow.write_bytes(raw)
    assert ci.resolve_passphrase(tmp_path) is None
    assert bridge.load_backup_passphrase(str(tmp_path / ".genesis")) == {}
