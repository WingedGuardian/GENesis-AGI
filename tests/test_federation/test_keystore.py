"""Federation keystore — lazy 0600 identity keyfile, idempotency, empty-state,
and the derived write-cap seal key."""

from __future__ import annotations

import os
import stat

import pytest

from genesis.federation import crypto, keystore


@pytest.fixture(autouse=True)
def isolate_home(monkeypatch, tmp_path):
    """Point ~/.genesis at a tmp dir so the test never touches the real keyfile."""
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    return tmp_path


def test_empty_state_writes_nothing_until_asked():
    # No keyfile exists on a fresh install that has never federated.
    assert not keystore.identity_key_path().exists()


def test_create_then_reload_is_stable():
    a = keystore.load_or_create_identity()
    assert keystore.identity_key_path().exists()
    b = keystore.load_or_create_identity()  # second call reloads, does not regenerate
    assert a.verify_key_bytes == b.verify_key_bytes
    assert a.enc_public_bytes == b.enc_public_bytes


def test_signing_and_encryption_keys_are_distinct_and_32_bytes():
    idn = keystore.load_or_create_identity()
    assert len(idn.verify_key_bytes) == 32
    assert len(idn.enc_public_bytes) == 32
    assert idn.verify_key_bytes != idn.enc_public_bytes  # never reuse one key for both


def test_keyfile_is_0600():
    keystore.load_or_create_identity()
    mode = stat.S_IMODE(keystore.identity_key_path().stat().st_mode)
    assert mode == 0o600, f"keyfile mode {oct(mode)} — secret must not be group/world readable"


def test_seal_key_is_deterministic_and_seals_write_caps():
    idn = keystore.load_or_create_identity()
    key = idn.seal_key()
    assert len(key) == 32
    assert key == keystore.load_or_create_identity().seal_key()  # stable across loads
    sealed = crypto.seal(key, b"peer-write-cap")
    assert crypto.unseal(idn.seal_key(), sealed) == b"peer-write-cap"


def test_generated_signing_key_actually_signs():
    idn = keystore.load_or_create_identity()
    sig = crypto.sign(idn.signing_key, context="msg", message=b"hi")
    assert crypto.verify(idn.signing_key.verify_key, context="msg", message=b"hi", signature=sig)


def test_load_retightens_loose_permissions():
    """A keyfile that a restore/copy left group/other-readable is tightened back
    to 0600 on load, before its secret is read."""
    keystore.load_or_create_identity()  # creates at 0600
    path = keystore.identity_key_path()
    os.chmod(path, 0o644)  # simulate a permissive restore/copy
    idn = keystore.load_or_create_identity()  # load must re-tighten
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(idn.verify_key_bytes) == 32  # still loads correctly


def test_exclusive_create_refuses_to_clobber_existing():
    """The exclusive create claims identity.key itself — a second creator can't
    overwrite the first identity (the concurrent-first-pairing race)."""
    keystore.load_or_create_identity()  # creates identity.key
    path = keystore.identity_key_path()
    original = path.read_bytes()
    # a second exclusive-create attempt must NOT clobber — returns False
    assert keystore._exclusive_create_0600(path, b"different-identity") is False
    assert path.read_bytes() == original  # original untouched
    # and no stray temp file was left behind
    assert not list(path.parent.glob("*.tmp.*"))


def test_first_create_fsyncs_directory_and_its_parent(monkeypatch):
    """Durability of the FIRST pairing: fsyncing the new `federation` dir makes
    the keyfile's directory entry durable, but on first pairing mkdir() also
    created the `federation` dir itself — a new entry in its PARENT — and only
    an fsync of the parent makes THAT durable. A keyfile that vanishes on
    power loss regenerates a different identity, orphaning peer relationships."""
    opened: dict[int, str] = {}
    fsynced: list[str] = []
    real_open, real_fsync = os.open, os.fsync

    def rec_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        opened[fd] = str(path)
        return fd

    def rec_fsync(fd):
        fsynced.append(opened.get(fd, f"<unrecorded fd {fd}>"))
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", rec_open)
    monkeypatch.setattr(os, "fsync", rec_fsync)
    keystore.load_or_create_identity()
    fed_dir = str(keystore.federation_dir())
    parent = str(keystore.federation_dir().parent)
    assert fed_dir in fsynced, f"federation dir not fsynced (fsynced: {fsynced})"
    assert parent in fsynced, f"parent dir not fsynced (fsynced: {fsynced})"


def test_identity_key_is_covered_by_backup_and_restore():
    """Disaster recovery: the identity keyfile must be named in backup.sh's
    Tier-1 creds spec and in restore.sh's placement guidance — MEASURED at zero
    federation references before this pin (an unpaired install skips it via the
    [ -f ] guard, so coverage costs nothing until first pairing)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    backup = (root / "scripts" / "backup.sh").read_text(encoding="utf-8")
    restore = (root / "scripts" / "restore.sh").read_text(encoding="utf-8")
    assert ".genesis/federation/identity.key:federation_identity.key" in backup
    # pin the actual placement HINT, not a mere mention (a comment would pass)
    assert "federation_identity.key → ~/.genesis/federation/identity.key" in restore


def test_fsync_covers_every_newly_created_directory_level(monkeypatch, tmp_path):
    """When mkdir creates SEVERAL levels (restore-before-bootstrap ordering, where
    ~/.genesis does not exist yet), each new level is an entry in its own parent —
    every one of those parents must be fsynced, not just the leaf's."""
    deep = tmp_path / "a" / "b" / "genesis_home"
    monkeypatch.setenv("GENESIS_HOME", str(deep))
    opened: dict[int, str] = {}
    fsynced: list[str] = []
    real_open, real_fsync = os.open, os.fsync

    def rec_open(path, flags, *a, **k):
        fd = real_open(path, flags, *a, **k)
        opened[fd] = str(path)
        return fd

    def rec_fsync(fd):
        fsynced.append(opened.get(fd, f"<unrecorded fd {fd}>"))
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", rec_open)
    monkeypatch.setattr(os, "fsync", rec_fsync)
    keystore.load_or_create_identity()
    # every level mkdir created (federation, genesis_home, b, a) is an entry in a
    # parent that must be durable — a/b/genesis_home/federation and its ancestors
    for expected in (deep / "federation", deep, deep.parent, deep.parent.parent):
        assert str(expected) in fsynced, f"{expected} not fsynced (fsynced: {fsynced})"
