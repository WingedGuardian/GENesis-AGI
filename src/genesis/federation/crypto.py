"""Federation crypto primitives (v1) — PyNaCl only.

The relay is untrusted store-and-forward: it sees ciphertext and opaque mailbox
ids, never plaintext or identity. All confidentiality + integrity is end-to-end
between the two paired installs, here:

- **Identity / integrity** — Ed25519 (``nacl.signing``). Every envelope is
  DETACHED-signed by the sender and verified by the receiver against the peer's
  PINNED verify key. Signing is **domain-separated** with LENGTH-PREFIXED framing
  (``DOMAIN | len(context) | context | message``) so a signature minted in one
  context (e.g. ``pairing``) can never be replayed as another (``msg``), and no
  byte inside ``context`` or ``message`` can shift the boundary between them.
- **Confidentiality** — X25519 ``Box`` (``nacl.public``). Distinct from the
  signing key — NEVER reuse an Ed25519 key for encryption (key-confusion).
- **At-rest sealing** — ``SecretBox`` for the peer write-cap (in the DB), keyed
  from a secret DERIVED from the identity seed. The identity keyfile ITSELF is not
  sealed (it can't be — the seal key comes from it); it is protected by ``0600``
  filesystem permissions. See ``keystore``.
- **Transcript integrity** — a hash chain: ``payload_hash = H(prev_hash ||
  canonical(payload))`` over a FROZEN canonical byte encoding.

v1 uses static ``Box`` keys (no forward secrecy) — acceptable for a low-volume,
human-gated channel whose transcript value is integrity, not eternal secrecy.
The envelope reserves a version + ephemeral-key slot so a ratchet can drop in
later without a wire break. See the plan doc for the FS rationale.
"""

from __future__ import annotations

import hashlib
import json

import nacl.encoding
import nacl.exceptions
import nacl.hash
import nacl.utils
from nacl.public import Box, PrivateKey, PublicKey
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey

# Domain-separation prefix. Bump the version suffix on any wire-format change.
DOMAIN = b"genesis-federation-v1"

# Frozen canonical encoding — DO NOT change without a version bump; signature and
# hash-chain verification across installs depend on byte-for-byte agreement.
# allow_nan=False: NaN/Infinity would serialize to non-standard JSON tokens a
# strict peer parser rejects, breaking cross-install byte-identity — fail loud.
_JSON_KW = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
    "allow_nan": False,
}


def canonical_payload(payload: dict) -> bytes:
    """Deterministic byte encoding of a message payload (sorted keys, compact,
    UTF-8). The single source of truth for what gets signed and hashed. Any
    non-JSON-serializable value raises — callers pass plain dict/str/int/None."""
    return json.dumps(payload, **_JSON_KW).encode("utf-8")


def _signed_bytes(context: str, message: bytes) -> bytes:
    """The domain-separated byte string that is actually signed/verified.

    LENGTH-PREFIXED framing (``DOMAIN | len(context):2 | context | message``): the
    2-byte context length makes the context/message boundary unambiguous no matter
    what bytes either contains, so a NUL or separator inside ``context`` (or a raw
    byte in ``message``) can never shift the boundary and collapse two different
    ``(context, message)`` pairs onto identical signed bytes."""
    ctx = context.encode("utf-8")
    if len(ctx) > 0xFFFF:
        raise ValueError("federation signing context too long (>65535 bytes)")
    return DOMAIN + len(ctx).to_bytes(2, "big") + ctx + message


def sign(signing_key: SigningKey, *, context: str, message: bytes) -> bytes:
    """Detached Ed25519 signature over the domain-separated ``(context,
    message)``. Returns the 64-byte signature."""
    return signing_key.sign(_signed_bytes(context, message)).signature


def verify(verify_key: VerifyKey, *, context: str, message: bytes, signature: bytes) -> bool:
    """Verify a detached signature. Returns False on ANY failure — bad sig, wrong
    key, wrong context, tampered message, OR a malformed signature (wrong length,
    empty, non-bytes). Never raises for a rejected signature, so a receive loop
    can call it directly on peer-supplied bytes without a DoS foot-gun. (nacl
    raises ValueError/TypeError for a malformed signature; both subclass
    ``CryptoError`` alongside ``BadSignatureError`` — verified against the
    installed PyNaCl.)"""
    try:
        verify_key.verify(_signed_bytes(context, message), signature)
        return True
    except nacl.exceptions.CryptoError:
        return False
    except TypeError:
        # a non-bytes signature can surface as a builtin TypeError depending on
        # the code path — a malformed signature is a rejection, never a raise.
        return False


def encrypt(
    my_private: PrivateKey, peer_public: PublicKey, plaintext: bytes
) -> tuple[bytes, bytes]:
    """X25519 ``Box`` encrypt. Returns ``(nonce, ciphertext)`` with a fresh
    random nonce (never reuse a nonce with the same key pair)."""
    nonce = nacl.utils.random(Box.NONCE_SIZE)
    box = Box(my_private, peer_public)
    encrypted = box.encrypt(plaintext, nonce)
    # EncryptedMessage carries nonce+ciphertext; return them split for storage.
    return (encrypted.nonce, encrypted.ciphertext)


def decrypt(
    my_private: PrivateKey, peer_public: PublicKey, nonce: bytes, ciphertext: bytes
) -> bytes:
    """X25519 ``Box`` decrypt. Raises ``nacl.exceptions.CryptoError`` on any
    tamper/wrong-key. **Prefer :func:`verify_and_decrypt`** on the receive path —
    calling this directly skips the Ed25519 signature check that binds the
    ciphertext to its (context, chain-position) and to the sender's identity key,
    and silently returns plausible plaintext for an envelope that failed to
    verify."""
    box = Box(my_private, peer_public)
    return box.decrypt(ciphertext, nonce)


def verify_and_decrypt(
    verify_key: VerifyKey,
    my_private: PrivateKey,
    peer_public: PublicKey,
    *,
    context: str,
    signed_message: bytes,
    signature: bytes,
    nonce: bytes,
    ciphertext: bytes,
) -> bytes | None:
    """Verify THEN decrypt — the safe receive-path primitive that makes the
    verify-before-decrypt ordering impossible to get wrong. ``signed_message`` is
    the exact byte string the sender signed (the caller/envelope defines it, and
    it must bind ``ciphertext`` — e.g. include its hash). Returns the plaintext,
    or ``None`` if the signature does not verify (decrypt is NOT attempted).
    ``CryptoError`` still propagates from decrypt for a signature-valid but
    otherwise-corrupt ciphertext (a genuine anomaly worth surfacing)."""
    if not verify(verify_key, context=context, message=signed_message, signature=signature):
        return None
    return decrypt(my_private, peer_public, nonce, ciphertext)


def seal(key: bytes, plaintext: bytes) -> bytes:
    """SecretBox-seal at-rest data (write-caps, keyfile). ``key`` is 32 bytes.
    Returns nonce-prefixed ciphertext (SecretBox self-frames the nonce)."""
    return SecretBox(key).encrypt(plaintext)


def unseal(key: bytes, sealed: bytes) -> bytes:
    """Reverse :func:`seal`. Raises ``CryptoError`` on tamper/wrong key."""
    return SecretBox(key).decrypt(sealed)


def chain_hash(prev_hash: str | None, payload: dict) -> str:
    """Transcript chain link: ``sha256(prev_hash_bytes || canonical(payload))``
    as hex. ``prev_hash=None`` is the genesis link (empty prefix), so the first
    message in a direction has a well-defined hash. Deterministic across installs
    because :func:`canonical_payload` is frozen.

    ``prev_hash`` MUST be ``None`` or a 64-char lowercase sha256 hexdigest — a
    FIXED length, which is what makes the separator-free ``prev || payload``
    concatenation unambiguous (a variable-length prefix could let two different
    ``(prev, payload)`` pairs concatenate to the same bytes). An empty string is
    rejected rather than silently treated as genesis (that would be a caller bug
    conflating "no prior" with "the prior hash is empty")."""
    if prev_hash is not None and (
        len(prev_hash) != 64 or any(c not in "0123456789abcdef" for c in prev_hash)
    ):
        raise ValueError("prev_hash must be None or a 64-char lowercase sha256 hexdigest")
    h = hashlib.sha256()
    if prev_hash is not None:
        h.update(prev_hash.encode("ascii"))
    h.update(canonical_payload(payload))
    return h.hexdigest()


def fingerprint(verify_key_bytes: bytes) -> str:
    """Short human-comparable fingerprint of a peer identity key, for the
    out-of-band SAS check at pairing (the two humans read it aloud to defeat a
    MITM). Grouped hex of a truncated BLAKE2b digest — stable for a given key."""
    digest = nacl.hash.blake2b(verify_key_bytes, digest_size=10, encoder=nacl.encoding.RawEncoder)
    hex_str = digest.hex().upper()
    return "-".join(hex_str[i : i + 4] for i in range(0, len(hex_str), 4))
