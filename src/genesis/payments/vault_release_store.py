"""Opening a card for a release, and retiring a single-use card after it.

The vault service calls these in a thread, with the identity it loaded from
``identity.key``, for a release the owner approved (``payments/release.py``).
They reuse ``vault_store``'s locked, checked access to cards.db; the clear
columns and the sealed blob are the ones ``vault_store.add_card`` writes.
"""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path

from genesis.payments import vault_store as vs

_BLOB_TYPES = {
    "v": int,
    "card_ref": str,
    "merchant": (str, type(None)),
    "single_use": bool,
    "number": str,
    "exp_month": int,
    "exp_year": int,
    "cvc": str,
    "name": str,
    "postal_code": str,
}
_DAMAGED_BLOB = (
    "the card's sealed record is damaged or not this row's; restore the vault from backup"
)


def open_card(card_ref: str, identity, path: Path | None = None) -> dict:
    """Decrypt one active card with ``identity`` (the key the vault service holds).

    Returns the blob's fields plus the row's label and last four. The blob must
    be version ``vault_store.BLOB_VERSION``, hold exactly the keys ``add_card`` seals, and
    name this row's card_ref, merchant and single-use flag (a blob copied to
    another row, or a row re-scoped in the clear, is refused); its expiry month
    must be 1-12, and the row's clear last four must be its number's last four
    (the label is not sealed and is not checked). Every error is a
    fixed-text VaultError: never pyrage's or json's message, never a value.
    """
    from pyrage import decrypt

    path = path or vs.vault_dir()
    if not isinstance(card_ref, str) or not vs.CARD_REF.fullmatch(card_ref):
        raise vs.VaultError("that is not a card ref")
    vs.check_ready(path)
    with vs._db(path) as conn:
        row = conn.execute(
            "SELECT label, last4, merchant, single_use, blob FROM cards"
            " WHERE card_ref = ? AND status = 'active'",
            (card_ref,),
        ).fetchone()
    if row is None:
        raise vs.VaultError(f"no active card {card_ref}")
    label, last4, merchant, single_use, blob = row
    if not isinstance(blob, bytes):
        raise vs.VaultError(_DAMAGED_BLOB)
    # Raised outside the handler: `from None` hides the cause but would still
    # keep it as __context__, and a JSONDecodeError holds the whole plaintext.
    # BaseException: a pyo3 panic is not an Exception, and this runs in a
    # worker thread, where no KeyboardInterrupt is delivered.
    secret = None
    with suppress(BaseException):  # DecryptError, a panic, bad JSON: one fixed text
        secret = json.loads(decrypt(blob, [identity]))
    if secret is None:
        raise vs.VaultError("the card could not be decrypted with the held key") from None
    if (
        not isinstance(secret, dict)
        or set(secret) != set(_BLOB_TYPES)
        or not all(isinstance(secret[k], t) for k, t in _BLOB_TYPES.items())
        or isinstance(secret["v"], bool)
        or isinstance(secret["exp_month"], bool)
        or isinstance(secret["exp_year"], bool)
        # Value checks only after the type checks above: a TypeError here
        # would carry the plaintext in its __context__.
        or not 1 <= secret["exp_month"] <= 12
        or secret["v"] != vs.BLOB_VERSION
        or secret["card_ref"] != card_ref
        or secret["merchant"] != merchant
        or secret["single_use"] is not bool(single_use)
        # The clear last four are what the approval shows: they must be the sealed number's.
        or secret["number"][-4:] != last4
    ):
        del secret
        raise vs.VaultError(_DAMAGED_BLOB) from None
    secret.update(label=label, last4=last4)
    return secret


def retire_released(card_ref: str, path: Path | None = None) -> None:
    """Retire a single-use card after its release; nothing to ask (the release was granted by the owner's tap).

    The directory lock is tried for about five seconds and then refused, so the
    thread running this always ends.
    """
    path = path or vs.vault_dir()
    with vs._dir_lock(path, tries=50) as dfd:
        vs._retire_row(vs._pin(path, dfd), card_ref)


def load_identity(path: Path | None = None):
    """The vault's age X25519 identity, from identity.key, for the vault service.

    Refuses (VaultError) whatever ``check_ready`` refuses (a vault still to
    migrate included), a key file that is not the one cards.db records, and a
    key whose public half is not recipient.txt. The bytes checked are the
    bytes parsed: the file is read once.
    """
    path = path or vs.vault_dir()
    vs.check_ready(path)
    data = vs._check_identity(path)
    vs._check_bound_keys(path, data)
    text = data.decode("ascii").strip()
    del data
    from pyrage import IdentityError, x25519

    try:
        identity = x25519.Identity.from_str(text)
    except (IdentityError, ValueError):
        raise vs.VaultError("identity.key does not hold an age X25519 key") from None
    finally:
        del text
    if str(identity.to_public()) != vs._recipient_text(path):
        raise vs.VaultError(
            "identity.key and recipient.txt do not match; restore the vault from backup"
        )
    return identity
