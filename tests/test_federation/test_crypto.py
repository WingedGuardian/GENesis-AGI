"""Federation crypto primitives — sign/verify, encrypt/decrypt, seal/unseal,
canonical encoding, hash chain, fingerprint. The tests assert the SECURITY
properties (tamper/wrong-key/wrong-context rejection), not just happy paths."""

from __future__ import annotations

import nacl.exceptions
import nacl.utils
import pytest
from nacl.public import PrivateKey
from nacl.secret import SecretBox
from nacl.signing import SigningKey

from genesis.federation import crypto

# --- canonical encoding ---------------------------------------------------


def test_canonical_is_order_independent_and_content_sensitive():
    a = crypto.canonical_payload({"b": 2, "a": 1})
    b = crypto.canonical_payload({"a": 1, "b": 2})
    assert a == b  # key order does not change the bytes
    assert crypto.canonical_payload({"a": 1}) != crypto.canonical_payload({"a": 2})
    # non-ascii survives (ensure_ascii=False, utf-8)
    assert crypto.canonical_payload({"t": "café"}) == b'{"t":"caf\xc3\xa9"}'


# --- Ed25519 sign / verify (+ domain separation) --------------------------


def test_sign_verify_roundtrip():
    sk = SigningKey.generate()
    sig = crypto.sign(sk, context="msg", message=b"hello")
    assert crypto.verify(sk.verify_key, context="msg", message=b"hello", signature=sig)


def test_verify_rejects_tampered_message():
    sk = SigningKey.generate()
    sig = crypto.sign(sk, context="msg", message=b"hello")
    assert not crypto.verify(sk.verify_key, context="msg", message=b"hell0", signature=sig)


def test_verify_rejects_wrong_key():
    sk, other = SigningKey.generate(), SigningKey.generate()
    sig = crypto.sign(sk, context="msg", message=b"hello")
    assert not crypto.verify(other.verify_key, context="msg", message=b"hello", signature=sig)


def test_domain_separation_context_mismatch_fails():
    """A signature minted under one context must NOT verify under another —
    prevents replaying a pairing signature as a message signature."""
    sk = SigningKey.generate()
    sig = crypto.sign(sk, context="pairing", message=b"hello")
    assert crypto.verify(sk.verify_key, context="pairing", message=b"hello", signature=sig)
    assert not crypto.verify(sk.verify_key, context="msg", message=b"hello", signature=sig)


# --- X25519 Box encrypt / decrypt -----------------------------------------


def test_encrypt_decrypt_roundtrip():
    a, b = PrivateKey.generate(), PrivateKey.generate()
    nonce, ct = crypto.encrypt(a, b.public_key, b"secret payload")
    assert ct != b"secret payload"
    assert crypto.decrypt(b, a.public_key, nonce, ct) == b"secret payload"


def test_encrypt_uses_fresh_nonce_each_call():
    a, b = PrivateKey.generate(), PrivateKey.generate()
    n1, _ = crypto.encrypt(a, b.public_key, b"x")
    n2, _ = crypto.encrypt(a, b.public_key, b"x")
    assert n1 != n2  # nonce reuse would be catastrophic


def test_decrypt_wrong_key_raises():
    a, b, evil = PrivateKey.generate(), PrivateKey.generate(), PrivateKey.generate()
    nonce, ct = crypto.encrypt(a, b.public_key, b"secret")
    with pytest.raises(nacl.exceptions.CryptoError):
        crypto.decrypt(evil, a.public_key, nonce, ct)


def test_decrypt_tampered_ciphertext_raises():
    a, b = PrivateKey.generate(), PrivateKey.generate()
    nonce, ct = crypto.encrypt(a, b.public_key, b"secret")
    tampered = bytes([ct[0] ^ 0x01]) + ct[1:]
    with pytest.raises(nacl.exceptions.CryptoError):
        crypto.decrypt(b, a.public_key, nonce, tampered)


# --- SecretBox seal / unseal ----------------------------------------------


def test_seal_unseal_roundtrip_and_wrong_key():
    key = nacl.utils.random(SecretBox.KEY_SIZE)
    sealed = crypto.seal(key, b"write-cap-token")
    assert sealed != b"write-cap-token"
    assert crypto.unseal(key, sealed) == b"write-cap-token"
    with pytest.raises(nacl.exceptions.CryptoError):
        crypto.unseal(nacl.utils.random(SecretBox.KEY_SIZE), sealed)


# --- hash chain -----------------------------------------------------------


def test_chain_hash_deterministic_and_links_prev():
    p = {"seq": 1, "body": "hi"}
    h_a, h_b = "a" * 64, "b" * 64  # valid 64-hex prev hashes
    assert crypto.chain_hash(None, p) == crypto.chain_hash(None, p)  # deterministic
    # different prev → different link even for the same payload
    assert crypto.chain_hash(h_a, p) != crypto.chain_hash(h_b, p)
    # genesis (None) differs from a real prev
    assert crypto.chain_hash(None, p) != crypto.chain_hash(h_a, p)
    # payload change changes the hash
    assert crypto.chain_hash(h_a, p) != crypto.chain_hash(h_a, {"seq": 1, "body": "bye"})


# --- fingerprint (SAS) ----------------------------------------------------


def test_fingerprint_stable_and_key_specific():
    k1 = SigningKey.generate().verify_key.encode()
    k2 = SigningKey.generate().verify_key.encode()
    assert crypto.fingerprint(k1) == crypto.fingerprint(k1)  # stable
    assert crypto.fingerprint(k1) != crypto.fingerprint(k2)  # binds the key
    assert "-" in crypto.fingerprint(k1)  # human-groupable


# --- review fixes ---------------------------------------------------------


def test_verify_returns_false_for_malformed_signature():
    """A malformed signature (wrong length / empty / non-bytes) must be a
    rejection, not a raise — else a peer trivially DoSes the receive loop."""
    vk = SigningKey.generate().verify_key
    for bad in (b"short", b"x" * 63, b"x" * 65, b"", "notbytes", None):
        assert crypto.verify(vk, context="msg", message=b"hi", signature=bad) is False


def test_domain_separation_survives_boundary_shift():
    """Length-prefixed framing: a signature over (context='a', message=b'bc')
    must NOT verify as (context='ab', message=b'c') — the boundary can't shift."""
    sk = SigningKey.generate()
    sig = crypto.sign(sk, context="a", message=b"bc")
    assert crypto.verify(sk.verify_key, context="a", message=b"bc", signature=sig)
    assert not crypto.verify(sk.verify_key, context="ab", message=b"c", signature=sig)
    # a NUL inside context cannot collapse onto a different pairing either
    sig2 = crypto.sign(sk, context="msg", message=b"\x00evil")
    assert not crypto.verify(sk.verify_key, context="msg\x00evil", message=b"", signature=sig2)


def test_chain_hash_rejects_bad_prev_hash():
    p = {"seq": 1}
    valid = "a" * 64
    assert crypto.chain_hash(valid, p)  # 64-hex ok
    for bad in ("", "abc", "A" * 64, "g" * 64, "a" * 63):
        with pytest.raises(ValueError):
            crypto.chain_hash(bad, p)


def test_canonical_rejects_non_finite_floats():
    # NaN/Infinity would emit non-standard JSON a strict peer parser rejects
    with pytest.raises(ValueError):
        crypto.canonical_payload({"x": float("nan")})
    with pytest.raises(ValueError):
        crypto.canonical_payload({"x": float("inf")})


def test_verify_and_decrypt_happy_and_bad_sig():
    """verify_and_decrypt returns plaintext on a good sig and None (WITHOUT
    decrypting) on a bad sig — the verify-before-decrypt ordering, enforced."""
    sender_sk = SigningKey.generate()
    a, b = PrivateKey.generate(), PrivateKey.generate()  # a=receiver, b=sender enc
    plaintext = b"hello peer"
    nonce, ct = crypto.encrypt(b, a.public_key, plaintext)
    signed = b"envelope-bytes-that-bind-the-ciphertext"
    sig = crypto.sign(sender_sk, context="msg", message=signed)

    got = crypto.verify_and_decrypt(
        sender_sk.verify_key,
        a,
        b.public_key,
        context="msg",
        signed_message=signed,
        signature=sig,
        nonce=nonce,
        ciphertext=ct,
    )
    assert got == plaintext
    # a bad signature → None, and decrypt is never reached
    bad = crypto.verify_and_decrypt(
        sender_sk.verify_key,
        a,
        b.public_key,
        context="msg",
        signed_message=b"tampered",
        signature=sig,
        nonce=nonce,
        ciphertext=ct,
    )
    assert bad is None
