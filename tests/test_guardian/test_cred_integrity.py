"""Tests for the standalone credential-integrity validator + restore."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from genesis.guardian import cred_integrity as ci
from genesis.guardian.cred_integrity import (
    DEFAULT_TARGETS,
    RESTORABLE_STATUSES,
    allowed_restore,
    check_all,
    resolve_passphrase,
    restore_file,
    validate_bytes,
    validate_file,
)

_HAS_GPG = shutil.which("gpg") is not None
_PASS = "test-passphrase-1234"  # noqa: S105 — synthetic test value

# ── validate_bytes: per-kind × corruption modes ─────────────────────────────


@pytest.mark.parametrize(
    ("kind", "data", "min_keys", "required", "status"),
    [
        ("dotenv", b"A=1\nB=2\nC=3\nD=4\nE=5\n", 5, (), "ok"),
        ("dotenv", b"", 5, (), "empty"),
        ("dotenv", b"   \n\n", 5, (), "empty"),
        ("dotenv", b"A=1\x00\x00\x00", 5, (), "nul_bytes"),
        ("dotenv", b"A=1\nB=2\n", 5, (), "parse_error"),          # too few keys
        ("dotenv", b"A=1\nB=2\nC=3\nD=4\nE=5\n", 5, ("Z",), "missing_keys"),
        ("json", b'{"claudeAiOauth": {"x": 1}}', 0, ("claudeAiOauth",), "ok"),
        ("json", b'{"bad": ', 0, (), "parse_error"),
        ("json", b"[]", 0, (), "parse_error"),                     # not a dict
        ("json", b"{}", 0, (), "parse_error"),                     # empty dict
        ("json", b'{"other": 1}', 0, ("claudeAiOauth",), "missing_keys"),
        ("yaml", b"github.com:\n  user: x\n", 0, (), "ok"),
        ("yaml", b"::: not yaml : [", 0, (), "parse_error"),
        ("yaml", b"just a scalar", 0, (), "parse_error"),          # not a mapping
        ("ssh_key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n", 0, (), "ok"),
        ("ssh_key", b"ssh-ed25519 AAAA...\n", 0, (), "parse_error"),  # a public key
        ("ssh_key", b"garbage", 0, (), "parse_error"),
    ],
)
def test_validate_bytes(kind, data, min_keys, required, status):
    result = validate_bytes(kind, data, required, min_keys)
    assert result.status == status
    assert result.ok == (status == "ok")


def test_nul_bytes_beats_everything():
    # A NUL-zeroed file (the outage signature) is corruption regardless of kind.
    assert validate_bytes("json", b'{"claudeAiOauth":1}\x00', ("claudeAiOauth",)).status == "nul_bytes"


def test_yaml_unavailable_degrades_to_ok(monkeypatch):
    monkeypatch.setattr(ci, "_YAML_OK", False)
    # Non-empty, no NUL → cannot prove corrupt without a parser → ok (never a
    # false restore). Empty/NUL still caught by the pre-checks.
    assert validate_bytes("yaml", b"anything non-empty").status == "ok"
    assert validate_bytes("yaml", b"").status == "empty"


# ── validate_file: missing vs absent disambiguation ─────────────────────────


def test_missing_with_backup_is_corruption(tmp_path):
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    (backup / "secrets").mkdir(parents=True)
    (backup / "secrets" / "secrets.env.gpg").write_bytes(b"x")
    home.mkdir()
    t = DEFAULT_TARGETS[0]  # secrets_env
    r = validate_file(t, home, backup)
    assert r.status == "missing" and r.ok is False


def test_missing_without_backup_is_absent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    t = DEFAULT_TARGETS[0]
    r = validate_file(t, home, None)
    assert r.status == "absent" and r.ok is True


def test_check_all_healthy_install(tmp_path):
    home = tmp_path / "home"
    (home / "genesis").mkdir(parents=True)
    (home / "genesis" / "secrets.env").write_text(
        "A=1\nB=2\nC=3\nD=4\nE=5\nGENESIS_BACKUP_PASSPHRASE=p\n"
    )
    results = check_all((DEFAULT_TARGETS[0],), home, None)
    assert results["secrets_env"].ok is True


# ── restore_file (real gpg) ─────────────────────────────────────────────────


def _encrypt(src_bytes: bytes, dst: Path, passphrase: str) -> None:
    """Encrypt bytes to dst.gpg exactly as scripts/backup.sh does."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".plain")
    tmp.write_bytes(src_bytes)
    try:
        proc = subprocess.run(
            ["gpg", "--batch", "--yes", "--quiet", "--passphrase-fd", "0",
             "--symmetric", "--cipher-algo", "AES256", "-o", str(dst), str(tmp)],
            input=passphrase.encode(),
            capture_output=True,
        )
    finally:
        tmp.unlink()
    assert proc.returncode == 0, proc.stderr.decode()


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not available")
def test_restore_happy_path(tmp_path):
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    good = b"A=1\nB=2\nC=3\nD=4\nE=5\nGENESIS_BACKUP_PASSPHRASE=p\n"
    _encrypt(good, backup / "secrets" / "secrets.env.gpg", _PASS)
    # Corrupt original present (NUL-zeroed).
    target_path = home / "genesis" / "secrets.env"
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"\x00" * 64)

    t = DEFAULT_TARGETS[0]
    r = restore_file(t, home=home, backup_dir=backup, passphrase=_PASS)
    assert r.ok and r.action == "restored"
    assert target_path.read_bytes() == good
    assert oct(target_path.stat().st_mode)[-3:] == "600"
    assert r.aside_path and Path(r.aside_path).read_bytes() == b"\x00" * 64
    assert ".corrupt-" in r.aside_path


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not available")
def test_restore_wrong_passphrase_leaves_original(tmp_path):
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    _encrypt(b"A=1\nB=2\nC=3\nD=4\nE=5\n", backup / "secrets" / "secrets.env.gpg", _PASS)
    target_path = home / "genesis" / "secrets.env"
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"\x00" * 8)

    r = restore_file(DEFAULT_TARGETS[0], home=home, backup_dir=backup, passphrase="wrong")
    assert not r.ok and r.action == "decrypt_failed"
    assert target_path.read_bytes() == b"\x00" * 8  # untouched


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not available")
def test_restore_invalid_backup_never_moves_original(tmp_path):
    """The safety invariant: a backup that decrypts to garbage must NOT move the
    (corrupt but present) original aside — validate-before-move."""
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    _encrypt(b"\x00\x00\x00", backup / "secrets" / "secrets.env.gpg", _PASS)  # bad content
    target_path = home / "genesis" / "secrets.env"
    target_path.parent.mkdir(parents=True)
    target_path.write_bytes(b"A=1\n")  # a present original

    r = restore_file(DEFAULT_TARGETS[0], home=home, backup_dir=backup, passphrase=_PASS)
    assert not r.ok and r.action == "backup_invalid"
    assert target_path.read_bytes() == b"A=1\n"      # original untouched
    assert r.aside_path is None
    # No stray tmp/aside files left behind.
    leftovers = [p.name for p in target_path.parent.iterdir()]
    assert leftovers == ["secrets.env"]


def test_restore_no_backup(tmp_path):
    r = restore_file(DEFAULT_TARGETS[0], home=tmp_path / "h",
                     backup_dir=tmp_path / "b", passphrase=_PASS)
    assert not r.ok and r.action == "skipped_no_backup"


@pytest.mark.skipif(not _HAS_GPG, reason="gpg not available")
def test_restore_ssh_key_sets_dir_perms(tmp_path):
    home = tmp_path / "home"
    backup = tmp_path / "backup"
    key = b"-----BEGIN OPENSSH PRIVATE KEY-----\nabcdef\n-----END OPENSSH PRIVATE KEY-----\n"
    _encrypt(key, backup / "creds" / "ssh" / "id_ed25519.gpg", _PASS)
    t = next(t for t in DEFAULT_TARGETS if t.name == "ssh_id_ed25519")
    r = restore_file(t, home=home, backup_dir=backup, passphrase=_PASS)
    assert r.ok and r.action == "restored"
    assert oct((home / ".ssh").stat().st_mode)[-3:] == "700"
    assert oct((home / ".ssh" / "id_ed25519").stat().st_mode)[-3:] == "600"


# ── resolve_passphrase chain ────────────────────────────────────────────────


def test_resolve_passphrase_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("GENESIS_BACKUP_PASSPHRASE", "from-env")
    assert resolve_passphrase(tmp_path) == "from-env"


def test_resolve_passphrase_from_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "secrets.env").write_text(
        "A=1\nGENESIS_BACKUP_PASSPHRASE=from-secrets\n"
    )
    assert resolve_passphrase(tmp_path) == "from-secrets"


def test_resolve_passphrase_corrupt_secrets_falls_to_escrow(tmp_path, monkeypatch):
    """The circular case: secrets.env is what's corrupt, so its passphrase is
    unusable — the escrow must win."""
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "secrets.env").write_bytes(b"\x00" * 32)  # zeroed
    esc = tmp_path / ".genesis" / "shared" / "guardian"
    esc.mkdir(parents=True)
    (esc / "backup_passphrase.env").write_text("GENESIS_BACKUP_PASSPHRASE=from-escrow\n")
    assert resolve_passphrase(tmp_path) == "from-escrow"


def test_resolve_passphrase_none(tmp_path, monkeypatch):
    monkeypatch.delenv("GENESIS_BACKUP_PASSPHRASE", raising=False)
    assert resolve_passphrase(tmp_path) is None


# ── allowed_restore rate-cap ────────────────────────────────────────────────


def test_allowed_restore():
    now = datetime.now(UTC)
    recent = [(now - timedelta(hours=1)).isoformat(), (now - timedelta(hours=2)).isoformat()]
    old = [(now - timedelta(days=2)).isoformat()]
    assert allowed_restore(old, now, 3) is True
    assert allowed_restore(recent, now, 3) is True
    assert allowed_restore(recent, now, 2) is False
    assert allowed_restore(recent + old, now, 2) is False  # old ones don't count
    assert allowed_restore([], now, 0) is False


def test_restorable_statuses_exclude_unreadable():
    assert "unreadable" not in RESTORABLE_STATUSES
    assert "absent" not in RESTORABLE_STATUSES
    assert {"missing", "empty", "nul_bytes", "parse_error"} <= RESTORABLE_STATUSES


# ── Standalone-ness: pipe mode must match in-process AND import no genesis.* ──


def test_pipe_mode_parity_and_no_genesis_imports(tmp_path):
    """The guardian runs this module by piping its SOURCE into a bare python3.
    This proves (a) it runs with zero package context — failing the build if any
    `genesis.*` import is added — and (b) the pipe verdict matches check_all()."""
    home = tmp_path / "home"
    (home / "genesis").mkdir(parents=True)
    (home / "genesis" / "secrets.env").write_text("A=1\nB=2\nC=3\nD=4\nE=5\n")
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("not json{")  # corrupt

    module_src = Path(ci.__file__).read_text()
    # Run in an isolated dir with NO access to the genesis package on sys.path.
    proc = subprocess.run(
        [sys.executable, "-", "check", "--json", "--home", str(home)],
        input=module_src,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home)},
    )
    assert proc.returncode == 0, proc.stderr
    piped = json.loads(proc.stdout)["results"]
    assert piped["secrets_env"]["status"] == "ok"
    assert piped["claude_credentials"]["status"] == "parse_error"

    in_proc = check_all(None, home, None)
    assert piped["secrets_env"]["status"] == in_proc["secrets_env"].status
    assert piped["claude_credentials"]["status"] == in_proc["claude_credentials"].status


def test_default_targets_match_backup_paths():
    """Guard: every target's backup_rel matches scripts/backup.sh §8 naming."""
    by_name = {t.name: t for t in DEFAULT_TARGETS}
    assert by_name["secrets_env"].backup_rel == "secrets/secrets.env.gpg"
    assert by_name["claude_credentials"].backup_rel == "creds/claude_credentials.json.gpg"
    assert by_name["gh_hosts"].backup_rel == "creds/gh_hosts.yml.gpg"
    assert by_name["ssh_guardian_key"].backup_rel == "creds/ssh/genesis_guardian_ed25519.gpg"
