"""The vault's key files checked by shape, stdlib only (backup's check runs
without the payments extra): an age recipient, an age X25519 secret key line,
and a passphrase-sealed key (``identity.age``, before migration)."""

from __future__ import annotations

import re

# An age X25519 recipient: "age1" plus 58 Bech32 characters (32 key bytes and
# a 6-character checksum). The checksum is verified below in the stdlib, so a
# corrupted key is caught even where pyrage is not installed.
_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"  # pragma: allowlist secret (public BIP-173 alphabet, not a secret)
_RECIPIENT_TEXT = re.compile(rf"age1[{_BECH32}]{{58}}")


def _bech32_ok(text: str) -> bool:
    """BIP-173 Bech32 checksum (age uses Bech32, not Bech32m); ``text`` is
    lowercase, its human-readable part ending at the last ``1``."""
    sep = text.rfind("1")
    hrp, data = text[:sep], [_BECH32.index(c) for c in text[sep + 1 :]]
    chk = 1
    for value in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i, gen in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= gen if (top >> i) & 1 else 0
    return chk == 1


# What `age_passphrase.encrypt` writes first: the header and a scrypt stanza.
_SEALED_HEADER = b"age-encryption.org/v1\n-> scrypt "
# An age X25519 identity as `str(x25519.Identity)` writes it: the uppercase
# Bech32 of "age-secret-key-" (32 key bytes and a checksum), one line.
_KEY_TEXT = re.compile(rf"AGE-SECRET-KEY-1[{_BECH32.upper()}]{{58}}")


def key_text_ok(data: bytes) -> bool:
    """Whether ``data`` is one whole age X25519 secret key line, checksum included."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return False
    text = text[:-1] if text.endswith("\n") else text
    return bool(_KEY_TEXT.fullmatch(text)) and _bech32_ok(text.lower())


def sealed_shape_ok(data: bytes) -> bool:
    """A whole passphrase-sealed X25519 identity: the age header, an scrypt stanza,
    a MAC line, then exactly 106 payload bytes (16 nonce + 74 key + 16 tag;
    MEASURED on pyrage 1.4.0). A file cut anywhere fails this."""
    _head, sep, rest = data.partition(b"\n--- ")
    mac, newline, payload = rest.partition(b"\n")
    return bool(data.startswith(_SEALED_HEADER) and sep and len(mac) == 43 and newline) and (
        len(payload) == 106
    )
