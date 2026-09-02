"""Federation local identity keystore (v1).

Holds this install's federation identity: an Ed25519 signing key (identity /
message signatures) and a DISTINCT X25519 private key (``Box`` encryption). They
live in a single ``0600`` keyfile under ``~/.genesis/federation/identity.key``
(honoring ``GENESIS_HOME`` so tests isolate to a tmp dir) — filesystem
permissions are the at-rest protection for the keyfile itself.

The keyfile is created LAZILY, on first pairing — never at boot — so a fresh
install that never federates writes nothing (empty-state clean).

Peer write-caps stored in the DB are additionally ``SecretBox``-sealed with a key
DERIVED from the identity seed (:meth:`Identity.seal_key`), so a DB leak alone
never exposes a usable cap. Deriving from the keyfile (rather than a separate
``secrets.env`` entry) keeps v1 self-contained: an attacker who already has the
``0600`` keyfile has the identity anyway, so no security is lost, and there is no
extra bootstrap secret to provision.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import nacl.encoding
import nacl.hash
from nacl.public import PrivateKey
from nacl.signing import SigningKey

from genesis.env import genesis_home

_KEYFILE_VERSION = 1
_SEAL_KEY_CONTEXT = b"genesis-federation-seal-v1"


def federation_dir() -> Path:
    """``~/.genesis/federation`` (GENESIS_HOME-aware). Not created here."""
    return genesis_home() / "federation"


def identity_key_path() -> Path:
    """Path to the ``0600`` identity keyfile."""
    return federation_dir() / "identity.key"


@dataclass(frozen=True)
class Identity:
    """This install's federation identity keypair (in memory)."""

    signing_key: SigningKey  # Ed25519 — identity + message signatures
    enc_private: PrivateKey  # X25519 — Box encryption (distinct key)

    @property
    def verify_key_bytes(self) -> bytes:
        """32-byte Ed25519 public verify key — what a peer pins."""
        return self.signing_key.verify_key.encode()

    @property
    def enc_public_bytes(self) -> bytes:
        """32-byte X25519 public key — what a peer encrypts to."""
        return self.enc_private.public_key.encode()

    def seal_key(self) -> bytes:
        """32-byte SecretBox key for at-rest sealing of DB write-caps, derived
        from the Ed25519 seed via a domain-separated BLAKE2b. Deterministic for
        a given identity; rotates only when the identity does."""
        seed = self.signing_key.encode()  # the 32-byte Ed25519 seed (the secret)
        return nacl.hash.blake2b(
            _SEAL_KEY_CONTEXT,  # domain-separation label as the hashed data
            digest_size=32,
            key=seed,  # the SECRET is the keyed-MAC key (idiomatic PRF derivation)
            encoder=nacl.encoding.RawEncoder,
        )


def _serialize(identity: Identity) -> bytes:
    return json.dumps(
        {
            "version": _KEYFILE_VERSION,
            "ed25519_seed": base64.b64encode(identity.signing_key.encode()).decode(),
            "x25519_private": base64.b64encode(identity.enc_private.encode()).decode(),
        }
    ).encode("utf-8")


def _deserialize(raw: bytes) -> Identity:
    data = json.loads(raw)
    if data.get("version") != _KEYFILE_VERSION:
        raise ValueError(f"unsupported federation keyfile version: {data.get('version')!r}")
    return Identity(
        signing_key=SigningKey(base64.b64decode(data["ed25519_seed"])),
        enc_private=PrivateKey(base64.b64decode(data["x25519_private"])),
    )


def _exclusive_create_0600(path: Path, payload: bytes) -> bool:
    """Create ``path`` with ``payload`` at mode 0600, EXCLUSIVELY on the FINAL
    path. Returns True if THIS call created it, False if it already existed (a
    concurrent creator won). Crash-safe AND race-safe: the content is fully
    written + fsync'd into a private 0600 temp, then **hard-linked** into place —
    ``os.link`` fails with ``FileExistsError`` if the destination exists, so two
    concurrent first-pairings can't both "win" and clobber each other's identity
    (which ``os.replace`` would, silently orphaning the loser's keys)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)  # mkdir mode is ignored if the dir already exists
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(str(tmp), str(path))  # atomic; raises FileExistsError if it exists
        except FileExistsError:
            return False
        # fsync the DIRECTORY so the new directory entry is durable — fsyncing the
        # file alone doesn't guarantee the link/rename survives a crash or power
        # loss, and a keyfile that vanishes on reboot regenerates a DIFFERENT
        # identity, orphaning peer relationships + caps sealed under the old key.
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return True
    finally:
        if tmp.exists():
            tmp.unlink()


def _load_existing_identity(path: Path) -> Identity:
    """Read an EXISTING identity keyfile, enforcing its at-rest protection before
    exposing the secret: reject a non-regular file (a symlink/fifo swap), and if a
    restore/copy left group/other permission bits set, TIGHTEN it back to 0600
    (self-heal) rather than reading a world/group-readable private seed."""
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"federation identity keyfile is not a regular file: {path}")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o600)
    return _deserialize(path.read_bytes())


def load_or_create_identity() -> Identity:
    """Return this install's federation identity, generating + persisting it on
    first call. Idempotent and race-safe: a concurrent creator that loses the
    exclusive-link race re-reads the winner's keyfile; an existing keyfile has its
    0600 protection re-validated before its secret is read."""
    path = identity_key_path()
    if path.exists():
        return _load_existing_identity(path)
    identity = Identity(signing_key=SigningKey.generate(), enc_private=PrivateKey.generate())
    if _exclusive_create_0600(path, _serialize(identity)):
        return identity
    # lost the create race — another process created it first; use theirs.
    return _load_existing_identity(path)
