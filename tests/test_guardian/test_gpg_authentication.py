"""Real OpenPGP controls for the shared backup authentication boundary."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.guardian import cred_integrity as ci


@pytest.fixture
def packets(tmp_path, monkeypatch):
    if not shutil.which("gpg"):
        pytest.skip("gpg not available")
    home = tmp_path / "g"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(home))
    plain = tmp_path / "source"
    plain.write_bytes(b"private backup\n")
    encrypted = tmp_path / "encrypted.gpg"
    literal = tmp_path / "literal.gpg"
    for target, options in ((encrypted, ["--symmetric"]), (literal, ["--store"])):
        subprocess.run(
            ["gpg", "--no-options", "--batch", "--yes", "--pinentry-mode", "loopback",
             "--passphrase-fd", "0", "--output", str(target), *options, str(plain)],
            input=b"synthetic password", capture_output=True, check=True,
        )
    try:
        yield plain, encrypted, literal
    finally:
        subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                       capture_output=True, check=False)


def test_encrypted_positive_and_wrong_password_negative(packets):
    plain, encrypted, _ = packets
    assert ci._gpg_decrypt(encrypted, "synthetic password") == plain.read_bytes()
    with pytest.raises(ci._DecryptError):
        ci._gpg_decrypt(encrypted, "different password")


def test_literal_packet_is_not_an_encrypted_backup(packets):
    _, _, literal = packets
    with pytest.raises(ci._DecryptError):
        ci._gpg_decrypt(literal, "different password")


@pytest.mark.parametrize("interface", ["bytes", "file", "stream"])
@pytest.mark.parametrize("failure", ["literal", "wrong-password", "tampered", "truncated", "concatenated"])
def test_rejects_bad_backups_without_publishing(packets, tmp_path, interface, failure):
    _, encrypted, literal = packets
    source = encrypted
    password = "synthetic password"
    if failure == "literal":
        source = literal
    elif failure == "wrong-password":
        password = "different password"
    else:
        data = encrypted.read_bytes()
        if failure == "tampered":
            data = data[:-1] + bytes([data[-1] ^ 1])
        elif failure == "truncated":
            data = data[:-8]
        else:
            data += literal.read_bytes()
        source = tmp_path / "invalid.gpg"
        source.write_bytes(data)
    target = tmp_path / "destination"
    target.write_bytes(b"previous backup must survive")
    with pytest.raises(ci._DecryptError):
        if interface == "bytes":
            ci._gpg_decrypt(source, password)
        elif interface == "file":
            ci.decrypt_backup_file(source, target, password)
        else:
            with ci.decrypt_backup_stream(source, password, tmp_path) as stream:
                stream.read(1)  # Early evidence still needs the complete integrity trailer.
    assert target.read_bytes() == b"previous backup must survive"
    assert not list(tmp_path.glob(".gpg-restore-*"))


def test_file_and_stream_positive_preserve_exact_bytes(packets, tmp_path):
    plain, encrypted, _ = packets
    target = tmp_path / "destination"
    ci.decrypt_backup_file(encrypted, target, b"synthetic password")
    assert target.read_bytes() == plain.read_bytes()
    assert target.stat().st_mode & 0o777 == 0o600
    with ci.decrypt_backup_stream(encrypted, "synthetic password", tmp_path) as stream:
        recovered = stream.read()
    assert recovered == plain.read_bytes()


def test_system_python_cli_has_no_package_dependency(packets, tmp_path):
    plain, encrypted, literal = packets
    target = tmp_path / "destination"
    script = Path(ci.__file__).resolve()
    for source, expected_exit in ((encrypted, 0), (literal, 1)):
        result = subprocess.run(
            [sys.executable, "-I", str(script), "decrypt-backup", str(source), str(target)],
            input=b"synthetic password", capture_output=True,
        )
        assert result.returncode == expected_exit, result.stderr
        assert target.read_bytes() == plain.read_bytes()


@pytest.mark.parametrize("mode", ["aead", "no-mdc"])
def test_integrity_modes_and_unsafe_gpg_config(packets, tmp_path, mode):
    plain, _, _ = packets
    source = tmp_path / "mode.gpg"
    options = (["--force-aead", "--rfc4880bis"] if mode == "aead" else ["--rfc2440"])
    if mode == "aead" and b"--force-aead" not in subprocess.check_output(["gpg", "--dump-options"]):
        pytest.skip("gpg lacks AEAD creation")
    subprocess.run(
        ["gpg", "--no-options", "--batch", "--yes", "--pinentry-mode", "loopback",
         "--passphrase-fd", "0", *options, "--symmetric", "--cipher-algo", "AES256",
         "--output", str(source), str(plain)],
        input=b"synthetic password", capture_output=True, check=True,
    )
    # Configuration cannot weaken normal recovery into accepting an unprotected message.
    (tmp_path / "g/gpg.conf").write_text("ignore-mdc-error\n")
    if mode == "aead":
        assert ci._gpg_decrypt(source, "synthetic password") == plain.read_bytes()
    else:
        with pytest.raises(ci._DecryptError):
            ci._gpg_decrypt(source, "synthetic password")


@pytest.mark.parametrize("mode", ["signed", "asymmetric", "mixed"])
def test_public_key_success_cannot_authenticate_the_supplied_password(packets, tmp_path, mode):
    plain, _, _ = packets
    base = ["gpg", "--no-options", "--batch", "--yes", "--pinentry-mode", "loopback",
            "--passphrase-fd", "0"]
    subprocess.run(base + ["--quick-generate-key", "Synthetic Backup Test", "ed25519", "sign", "0"],
                   input=b"", capture_output=True, check=True)
    listing = subprocess.check_output(["gpg", "--no-options", "--with-colons", "--list-secret-keys"],
                                      stderr=subprocess.DEVNULL)
    key = next(line.split(b":")[9].decode() for line in listing.split(b"\n") if line.startswith(b"fpr:"))
    subprocess.run(base + ["--quick-add-key", key, "cv25519", "encr", "0"],
                   input=b"", capture_output=True, check=True)
    options = ["--sign", "--local-user", key] if mode == "signed" else [
        "--encrypt", "--recipient", key, "--trust-model", "always"]
    if mode == "mixed":
        options += ["--symmetric"]
    source = tmp_path / "public-key.gpg"
    subprocess.run(base + options + ["--output", str(source), str(plain)],
                   input=b"synthetic password", capture_output=True, check=True)
    # Oracle: native GPG accepts these bytes even with this wrong supplied password.
    native = subprocess.run(base + ["--decrypt", str(source)], input=b"different password",
                            capture_output=True, check=True)
    assert native.stdout == plain.read_bytes()
    with pytest.raises(ci._DecryptError):
        ci._gpg_decrypt(source, "different password")
