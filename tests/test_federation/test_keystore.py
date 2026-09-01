"""Federation keystore — lazy 0600 identity keyfile, idempotency, empty-state,
and the derived write-cap seal key."""

from __future__ import annotations

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
