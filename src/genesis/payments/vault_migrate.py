"""The one-time migration of a vault made with a passphrase (``vault migrate``).

A vault made before owner ruling a4a95392 holds its key sealed under a
passphrase as ``identity.age``. ``migrate_vault`` unseals it once into
``identity.key``, after copying the vault aside; ``vault_store.vault_status``
reports the states a killed migration can leave ("needed", "resume",
"cleanup") and every reader refuses all but the finished one.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

from genesis.payments.vault_store import (
    _IDENTITY_DIGEST,
    CARDS,
    IDENTITY,
    RECIPIENT,
    SEALED_IDENTITY,
    VaultError,
    _db,
    _dir_lock,
    _pin,
    _public_of,
    _read_regular,
    _read_small,
    _recipient_text,
    _sealed_ok,
    _write_new,
    key_text_ok,
    vault_dir,
    vault_status,
)

# What the retired `vault unlock` / `vault lock` answer: the per-payment tap is
# the gate, and card fill has its own off switch.
RETIRED = (
    "unlock and lock are retired: the vault has no passphrase and its key is loaded "
    "at server start; every card release still needs the owner's tap. To stop card "
    "payments, set card_fill.enabled to false or GENESIS_CARD_FILL_DISABLED=1"
)
_NEEDS_MIGRATE = (
    "the vault's key is still sealed under the old passphrase; run "
    "`python -m genesis vault migrate` once in your own terminal"
)
_RESUME_MIGRATE = (
    "a vault migration stopped part-way; run `python -m genesis vault migrate` again "
    "to finish it (no passphrase needed)"
)
# What vault_status says for a vault still to migrate, by its state.
MIGRATE_TEXT = {"needed": _NEEDS_MIGRATE, "resume": _RESUME_MIGRATE}
# A sealed key's unseal (age scrypt) is given at most this much passphrase.
MAX_PASSPHRASE_LENGTH = 256
# Where migrate_vault keeps the vault as it was, beside the vault directory.
PRE_MIGRATE_PREFIX = "payment_vault.pre-migrate-"


def _unseal(passphrase: str, sealed: bytes, path: Path) -> bytes:
    """The sealed key as identity.key's text, if it opens with ``passphrase`` and pairs
    with ``path``'s recipient.txt."""
    from pyrage import DecryptError
    from pyrage import passphrase as age_passphrase

    if len(passphrase) > MAX_PASSPHRASE_LENGTH:
        raise VaultError(f"the passphrase is longer than {MAX_PASSPHRASE_LENGTH} characters")
    try:
        key = age_passphrase.decrypt(_sealed_ok(sealed), passphrase).decode().strip()
    except DecryptError:
        raise VaultError("wrong passphrase (or identity.age is damaged)") from None
    except UnicodeDecodeError:
        raise VaultError("identity.age does not hold an age X25519 key") from None
    # Cards are sealed to recipient.txt: a key that is not its pair opens none of them.
    if _public_of(key, SEALED_IDENTITY) != _recipient_text(path):
        raise VaultError(
            "identity.age and recipient.txt do not match; restore the vault from backup"
        )
    data = key.encode() + b"\n"
    del key
    if not key_text_ok(data):
        raise VaultError("identity.age does not hold an age X25519 key")
    return data


def migration_state(path: Path | None = None) -> str | None:
    """ "needed" (the key is sealed: migrate asks the passphrase once), "resume"
    (a migration stopped part-way: no passphrase needed), "cleanup" (only the
    sealed copy left beside the recorded key is to remove), or None. A vault
    that status refuses for another reason raises that reason."""
    status = vault_status(path or vault_dir())
    if status["migrate"] in (None, "cleanup") and status["error"]:
        raise VaultError(status["error"])
    return status["migrate"]


def _copy_aside(path: Path, pinned: Path) -> Path:
    """The vault as it is, copied into a new 0700 sibling directory (its key still
    sealed): the way back if a migration goes wrong. cards.db goes through SQLite's
    online backup, so the copy is never torn."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    aside = path.parent / f"{PRE_MIGRATE_PREFIX}{stamp}"
    try:
        aside.mkdir(mode=0o700)
    except FileExistsError:
        raise VaultError(f"{aside} already exists; wait a second and run migrate again") from None
    except OSError as exc:
        raise VaultError(f"the pre-migration copy cannot be made ({exc.strerror})") from None
    try:
        dfd = os.open(aside, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name in (SEALED_IDENTITY, RECIPIENT):
                _write_new(name, _read_small(pinned / name), dfd)
            target = f"/proc/self/fd/{dfd}/{CARDS}"
            with _db(pinned) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
            os.chmod(CARDS, 0o600, dir_fd=dfd)
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except (OSError, sqlite3.Error) as exc:
        raise VaultError(
            f"the pre-migration copy failed ({type(exc).__name__}); the vault was not changed"
        ) from None
    return aside


def pre_migrate_copies(path: Path | None = None) -> list[Path]:
    """The copies migrate_vault kept beside the vault. After the migration the
    unsealed key opens the cards in them too, so a card retired since stays
    readable in one until the owner deletes it."""
    path = path or vault_dir()
    try:
        return sorted(p for p in path.parent.glob(f"{PRE_MIGRATE_PREFIX}*") if p.is_dir())
    except OSError:
        return []


def _rebind(pinned: Path, old: bytes, new: bytes) -> None:
    """Record ``new`` as the vault's key file in cards.db, if it still records ``old``."""
    with _db(pinned, write=True) as conn, conn:
        cur = conn.execute(
            "UPDATE vault_meta SET value = ? WHERE key = ? AND value = ?",
            (hashlib.sha256(new).hexdigest(), _IDENTITY_DIGEST, hashlib.sha256(old).hexdigest()),
        )
        if cur.rowcount != 1:
            raise VaultError("cards.db changed during the migration; nothing more was done")


def migrate_vault(passphrase: str | None, path: Path | None = None) -> Path | None:
    """Unseal a vault's passphrase-sealed key into identity.key, once.

    "needed": the passphrase opens identity.age; the vault is first copied
    aside (``_copy_aside``, whose path is returned), then identity.key is
    written and linked (never over an existing one), cards.db's digest row is
    moved from the sealed key to it, and identity.age is removed. "resume"
    (killed after the link): identity.key must pair with recipient.txt; the row
    is moved and identity.age removed, with no passphrase. A vault with nothing
    to migrate is refused. Every step runs under the vault's directory lock,
    through the locked directory, so a kill leaves only states ``vault_status``
    reports.
    """
    path = path or vault_dir()
    with _dir_lock(path) as dfd:
        _pin(path, dfd)
        # A run killed between writing the key and removing its temporary name
        # leaves the plain key there: never kept, whatever the state.
        with suppress(FileNotFoundError):
            os.unlink(f".{IDENTITY}.tmp", dir_fd=dfd)
        state = migration_state(path)
        pinned = _pin(path, dfd)
        if state is None:
            raise VaultError("the vault has no sealed key to migrate")
        if state == "cleanup":
            # Killed after the row moved: identity.key is the recorded key, so
            # the sealed copy left beside it is only removed.
            os.unlink(SEALED_IDENTITY, dir_fd=dfd)
            os.fsync(dfd)
            return None
        try:
            sealed = _sealed_ok(_read_regular(SEALED_IDENTITY, dfd))
        except OSError as exc:
            raise VaultError(f"identity.age cannot be read ({exc.strerror})") from None
        aside = None
        if state == "needed":
            if not passphrase:
                raise VaultError("the passphrase is needed to unseal the key")
            key = _unseal(passphrase, sealed, pinned)
            aside = _copy_aside(path, pinned)
            with suppress(FileNotFoundError):  # a killed run's leftover
                os.unlink(f".{IDENTITY}.tmp", dir_fd=dfd)
            try:
                _write_new(f".{IDENTITY}.tmp", key, dfd)
                os.link(f".{IDENTITY}.tmp", IDENTITY, src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileExistsError:
                raise VaultError(
                    "an identity.key appeared during the migration; it was left as it is"
                ) from None
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(f".{IDENTITY}.tmp", dir_fd=dfd)
            os.fsync(dfd)
        else:
            try:
                key = _read_regular(IDENTITY, dfd)
            except OSError as exc:
                raise VaultError(f"identity.key cannot be read ({exc.strerror})") from None
            if not key_text_ok(key):
                raise VaultError("identity.key is not a whole age secret key; restore the vault")
            if _public_of(key.decode("ascii").strip(), IDENTITY) != _recipient_text(pinned):
                raise VaultError(
                    "identity.key and recipient.txt do not match; restore the vault from backup"
                )
        _rebind(pinned, sealed, key)
        del key
        os.unlink(SEALED_IDENTITY, dir_fd=dfd)
        os.fsync(dfd)
        return aside
