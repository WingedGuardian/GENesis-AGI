"""The encrypted card vault on disk.

Layout of ``~/.genesis/payment_vault/`` (directory 0700, every file 0600):

- ``recipient.txt``: the vault's age X25519 public key. Adding a card needs
  only this.
- ``identity.key``: the matching secret key, as age's own text form
  (``AGE-SECRET-KEY-1...``), in a 0600 file with no passphrase (owner ruling
  a4a95392: what releases a card is the per-payment Telegram tap, and the
  cards are merchant-locked virtual cards). Its presence marks the vault
  initialised, so ``init`` writes it last and never over an existing one.
- ``identity.age``: the same key sealed under a passphrase, as vaults made
  before that ruling hold it. Such a vault is refused until ``vault migrate``
  (``migrate_vault``) unseals it once into ``identity.key``.
- ``cards.db`` (``user_version`` 1): one row per card. The number, expiry and
  security code exist only inside ``blob``, age-encrypted to
  ``recipient.txt``; the row keeps a label, brand, last four digits and the
  merchant scope in the clear, for ``list``. The blob also carries the card's
  ``card_ref``, merchant and single-use flag; whatever decrypts a card must
  compare them with the row it read and refuse a mismatch (a blob copied to
  another row, or a row re-scoped in the clear). Retiring a card clears its blob
  with ``secure_delete`` on, so the ciphertext does not stay in a free page
  of the file. That is best effort: SQLite's rollback journal and the
  filesystem may keep old ciphertext, so a retired virtual card is also
  closed with its issuer. Its ``vault_meta`` table records the sha256 of the
  key file in use (``identity.key``, or ``identity.age`` before migration) and
  the text of ``recipient.txt``, so a damaged or replaced key file is refused
  by a stdlib check, before any card is sealed to a key that cannot open it.

Only virtual cards go in: every card is scoped to one merchant, or marked
single-use, or both. Decrypting a card for a release the owner approved is in
``vault_release_store``.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import stat
import time
import unicodedata
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from genesis.env import genesis_home
from genesis.payments.vault_keyfile import (
    _RECIPIENT_TEXT,
    _bech32_ok,
    key_text_ok,
    sealed_shape_ok,
)
from genesis.security.card_shapes import card_brand, contains_card_number

IDENTITY = "identity.key"
SEALED_IDENTITY = "identity.age"  # a vault made before the passphrase was dropped
RECIPIENT = "recipient.txt"
CARDS = "cards.db"
SCHEMA_VERSION = 1
BLOB_VERSION = 1
MAX_LABEL_LENGTH = 100

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    card_ref    TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    brand       TEXT NOT NULL,
    last4       TEXT NOT NULL,
    merchant    TEXT,
    single_use  INTEGER NOT NULL CHECK (single_use IN (0, 1)),
    status      TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    created_at  TEXT NOT NULL,
    retired_at  TEXT,
    blob        BLOB,
    CHECK (merchant IS NOT NULL OR single_use = 1),
    CHECK ((status = 'active') = (blob IS NOT NULL))
)
"""
# The key pair: the key file's sha256 and the recipient text, written with the
# schema (and moved to identity.key by a migration). The stdlib checks backup
# runs cannot open a key, so without this a same-length corruption (or a
# replaced recipient.txt) would pass, and add would seal cards no key opens.
_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
)
"""
_IDENTITY_DIGEST = "identity_sha256"
_RECIPIENT_ROW = "recipient"

_EXPIRY = re.compile(r"(0[1-9]|1[0-2])\s*/\s*([0-9]{2}|[0-9]{4})")
_HOST = re.compile(
    r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)
CARD_REF = re.compile(r"card_[0-9a-f]{12}")


class VaultError(Exception):
    """A vault operation was refused. The message never carries a card value."""


def vault_dir() -> Path:
    return genesis_home() / "payment_vault"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _check_root(path: Path) -> bool:
    """True when the vault directory exists; refuse anything else at its path."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise VaultError(f"{path} cannot be checked ({exc.strerror})") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise VaultError(f"{path} is not a directory you own; move it aside")
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise VaultError(f"{path} is not readable and writable by you; `chmod 700` it")
    return True


def _ensure_dir(path: Path) -> None:
    if not _check_root(path):
        try:
            path.mkdir(mode=0o700, parents=True)
        except FileExistsError:
            pass  # created meanwhile; checked below
        except OSError as exc:
            raise VaultError(f"{path} cannot be created ({exc.strerror})") from None
        _check_root(path)
    try:
        if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise VaultError(f"{path} cannot be made private ({exc.strerror})") from None


def recipient_text(path: Path) -> str:
    """recipient.txt's key text: read capped and not followed, checked by shape (a VaultError)."""
    return _recipient_text(path)


def ensure_private_dir(path: Path) -> None:
    """Create (or check) a 0700 directory you own: the vault, or the service's socket dir."""
    _ensure_dir(path)


def _write_new(name: str, data: bytes, dir_fd: int) -> None:
    """Write ``data`` to a file that must not exist yet, created 0600."""
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _read_capped(fd: int, name: str, limit: int) -> bytes:
    """A regular file's bytes, refused past ``limit``: never loaded whole, never cut short.

    A prefix of a damaged file could pass the format checks, so a longer file
    is an error rather than a truncated read.
    """
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultError(f"{name} is not a regular file")
        data = f.read(limit + 1)
    if len(data) > limit:
        raise VaultError(f"{name} is larger than a vault file can be (damaged?); restore it")
    return data


def _read_regular(name: str, dir_fd: int, limit: int = 4096) -> bytes:
    """Read a small regular file without following a symlink or blocking on a FIFO."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    return _read_capped(fd, name, limit)


def _read_small(file: Path, limit: int = 4096) -> bytes:
    """As ``_read_regular``, by path. Only the last component is not followed,
    so this also reads through the locked directory's ``/proc/self/fd`` path."""
    fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    return _read_capped(fd, file.name, limit)


@contextmanager
def _dir_lock(path: Path, *, tries: int | None = None, pause_s: float = 0.1):
    """Hold an exclusive lock on the vault directory (one writer at a time).

    Yields the directory's file descriptor. Init, add and retire write through
    it, so everything they touch lands in the directory they locked even if
    another directory is moved onto the path meanwhile. With ``tries``, the
    lock is tried that many times ``pause_s`` apart and then refused, so a
    caller in a thread is never left waiting on it for good.
    """
    if tries is not None and tries < 1:
        raise ValueError("tries must be at least 1")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise VaultError(
            "the vault is not initialised; run `python -m genesis vault init`"
        ) from None
    except OSError as exc:
        raise VaultError(f"the vault directory cannot be opened ({exc.strerror})") from None
    try:
        if tries is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            for attempt in range(tries):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if attempt + 1 == tries:
                        raise VaultError(
                            "the vault is busy (another vault command holds it)"
                        ) from None
                    time.sleep(pause_s)
        yield fd
    finally:
        os.close(fd)


def _recipient_text(path: Path) -> str:
    """recipient.txt's key text, checked by shape only (no crypto import)."""
    try:
        text = _read_small(path / RECIPIENT).decode("utf-8").strip()
    except OSError as exc:
        raise VaultError(f"recipient.txt cannot be read ({exc.strerror})") from None
    except UnicodeDecodeError:
        text = ""
    if not (_RECIPIENT_TEXT.fullmatch(text) and _bech32_ok(text)):
        raise VaultError(
            "recipient.txt does not hold a valid age public key; restore the vault from backup"
        )
    return text


def _read_recipient(path: Path):
    """The vault's age recipient; a missing or damaged recipient.txt is a VaultError."""
    return _parse_recipient(_recipient_text(path))


def _parse_recipient(text: str):
    from pyrage import RecipientError, x25519

    try:
        return x25519.Recipient.from_str(text)
    except RecipientError:
        raise VaultError(
            "recipient.txt does not hold a valid age public key; restore the vault from backup"
        ) from None


def _read_key_file(path: Path, name: str) -> bytes:
    try:
        dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:  # replaced or removed since it was checked
        raise VaultError(f"the vault directory cannot be opened ({exc.strerror})") from None
    try:
        return _read_regular(name, dfd)
    except OSError as exc:
        raise VaultError(f"{name} cannot be read ({exc.strerror})") from None
    finally:
        os.close(dfd)


def _check_identity(path: Path) -> bytes:
    """identity.key must be one whole age secret key line, checked by shape and checksum.

    A file cut or damaged anywhere fails this (stdlib only, as backup's check
    runs it), before a card is sealed to a key whose secret half is gone.
    """
    data = _read_key_file(path, IDENTITY)
    if not key_text_ok(data):
        raise VaultError(
            "identity.key is not a whole age secret key (damaged?); restore the vault from backup"
        )
    return data


def _check_sealed(path: Path) -> bytes:
    """identity.age must be a whole passphrase-sealed age key, checked without the passphrase.

    A sealed X25519 identity is the age header, an scrypt stanza, a MAC line,
    then exactly 106 payload bytes (16 nonce + 74 key + 16 tag; MEASURED on
    pyrage 1.4.0). A file cut anywhere fails this, before its migration.
    """
    return _sealed_ok(_read_key_file(path, SEALED_IDENTITY))


def _sealed_ok(data: bytes) -> bytes:
    """``data``, if it has the shape of a whole sealed key (``_check_sealed``)."""
    if not sealed_shape_ok(data):
        raise VaultError(
            "identity.age is not a whole passphrase-sealed age key (damaged?); "
            "restore the vault from backup"
        )
    return data


def _bound_rows(path: Path) -> tuple[str, str]:
    with _db(path) as conn:
        rows = dict(conn.execute("SELECT key, value FROM vault_meta").fetchall())
    digest, recipient = rows.get(_IDENTITY_DIGEST), rows.get(_RECIPIENT_ROW)
    if not isinstance(digest, str) or not isinstance(recipient, str):
        raise VaultError(
            "cards.db records no digest of the vault's keys; restore the vault from backup"
        )
    return digest, recipient


def _check_bound_keys(path: Path, data: bytes, name: str = IDENTITY) -> None:
    """The key file ``name`` and recipient.txt must be the pair cards.db records."""
    digest, recipient = _bound_rows(path)
    # A digest of a uniformly random key (or of a sealed one) and a public key:
    # nothing to time, so plain compares are enough.
    if digest != hashlib.sha256(data).hexdigest():
        raise VaultError(
            f"{name} is not the key this vault was created with (damaged or replaced); "
            "restore the vault from backup"
        )
    if recipient != _recipient_text(path):
        raise VaultError(
            "recipient.txt is not the public key this vault was created with (replaced?); "
            "restore the vault from backup"
        )


def verify_set(path: Path) -> None:
    """Everything the vault requires of its three files, by a stdlib check.

    The key file's shape (``identity.key``; or ``identity.age``, the sealed form
    of a set backed up before migration, which the vault takes back and asks to
    migrate again), the exact schema (``_shape``: version, DDL, no other
    objects), the whole recipient file with its checksum, and the key pair
    cards.db records. Backup and restore run this (``payment_vault_check`` in
    scripts/lib/payment_vault_backup.sh), so a set the vault would refuse is
    never published over the last good archive. Raises VaultError on the first
    defect.
    """
    if os.path.lexists(path / IDENTITY):
        _check_bound_keys(path, _check_identity(path))
    else:
        _check_bound_keys(path, _check_sealed(path), SEALED_IDENTITY)


def _translate(exc: sqlite3.Error) -> VaultError:
    if isinstance(exc, sqlite3.OperationalError):  # locked, disk I/O, shape: maybe transient
        return VaultError(f"cards.db could not be read or written ({exc})")
    if type(exc) is sqlite3.DatabaseError:  # "not a database", "malformed": the file itself
        return VaultError("cards.db is unreadable (not a database, or damaged)")
    return VaultError(f"cards.db refused the operation ({type(exc).__name__})")


def _normalised(sql: str | None) -> str:
    text = re.sub(r"\s+", " ", sql or "").strip().replace("IF NOT EXISTS ", "")
    return re.sub(r"\s*([(),])\s*", r"\1", text)


def _shape(conn: sqlite3.Connection) -> str:
    """'vault' (the vault's tables at SCHEMA_VERSION), 'empty' (no tables yet), or 'foreign'."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    tables = {name: sql for kind, name, sql in rows if kind == "table"}
    # Nothing but the tables and their primary keys' implicit indexes (sql
    # NULL; the sqlite_ prefix is reserved): a trigger, view or explicit index
    # would run with, or change, the vault's own statements.
    if any(
        kind != "table" and not (kind == "index" and sql is None and name.startswith("sqlite_"))
        for kind, name, sql in rows
    ):
        return "foreign"
    if set(tables) == {"cards", "vault_meta"} and version == SCHEMA_VERSION:
        # The tables must be the ones init writes, constraints and all: a
        # cards table of another shape is foreign, not a vault the code can trust.
        same = _normalised(tables["cards"]) == _normalised(_SCHEMA) and _normalised(
            tables["vault_meta"]
        ) == _normalised(_META_SCHEMA)
        return "vault" if same else "foreign"
    if not tables and version == 0:
        return "empty"
    return "foreign"


def _own_regular(db: Path) -> os.stat_result:
    """cards.db's lstat, if it is a regular file this user owns; checked before every connect.

    SQLite follows a symlink (its nofollow=1 URI flag does not refuse one here:
    MEASURED on 3.45.1) and a read-write open may roll back a journal, so a
    connect must only ever reach the vault's own file.
    """
    try:
        info = os.lstat(db)
    except FileNotFoundError:
        raise VaultError("cards.db is missing") from None
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise VaultError(
            "cards.db is not a regular file you own (a symlink or other); move it aside"
        )
    return info


def _same_file(db: Path, info: os.stat_result) -> None:
    """After a connect: cards.db is still the file checked before it (SQLite opens at connect)."""
    try:
        opened = os.stat(db)
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        raise VaultError("cards.db changed while it was being opened; try again")


@contextmanager
def _db(path: Path, *, write: bool = False):
    """An open, existing cards.db whose statements raise VaultError, never sqlite3 errors.

    Opened read-write but never created, and never given a schema: only init
    writes the schema, so a reader cannot turn a truncated or foreign file
    into an empty vault database. With ``write``, the shape check runs inside
    a ``BEGIN IMMEDIATE`` the caller's statements share, so nothing can change
    the schema or the key binding between the checks and the write.
    """
    db = path / CARDS
    info = _own_regular(db)
    conn = None
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=rw", uri=True)
        _same_file(db, info)
        conn.execute("PRAGMA secure_delete = ON")
        if write:
            conn.execute("BEGIN IMMEDIATE")
        shape = _shape(conn)
        if shape != "vault":
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if shape == "empty":
                why = "empty"
            elif version == SCHEMA_VERSION:
                why = "unexpected tables, indexes, views or triggers"
            else:
                why = f"format version {version}"
            raise VaultError(
                f"cards.db is not a vault card database ({why}); restore the vault from backup"
            )
        yield conn
    except sqlite3.Error as exc:
        raise _translate(exc) from None
    finally:
        if conn is not None:
            conn.close()


def _card_count(path: Path) -> int:
    """Cards in cards.db: 0 for an absent, 0-byte or never-written file."""
    db = path / CARDS
    try:
        if not os.path.lexists(db):
            return 0
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    info = _own_regular(db)
    if info.st_size == 0:
        return 0
    try:
        # mode=rw (never creates): a read-only connection cannot roll back the
        # journal a killed write left, which would lock init out for good.
        with closing(sqlite3.connect(f"file:{quote(str(db))}?mode=rw", uri=True)) as c:
            _same_file(db, info)
            shape = _shape(c)
    except sqlite3.Error as exc:
        raise _translate(exc) from None
    if shape == "empty":
        return 0
    if shape == "foreign":
        raise VaultError("cards.db is not an empty vault card database; move it aside first")
    with _db(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]


def is_initialised(path: Path | None = None) -> bool:
    """A key file (``identity.key``, or a sealed ``identity.age`` still to migrate) and recipient.txt."""
    path = path or vault_dir()
    try:
        keyed = (path / IDENTITY).is_file() or (path / SEALED_IDENTITY).is_file()
        return keyed and (path / RECIPIENT).is_file()
    except OSError as exc:
        raise VaultError(f"the vault cannot be checked ({exc.strerror})") from None


def _has_key_entry(path: Path) -> bool:
    return os.path.lexists(path / IDENTITY) or os.path.lexists(path / SEALED_IDENTITY)


def _incomplete() -> VaultError:
    return VaultError(
        "the vault is incomplete (a key file is there but recipient.txt is missing or not "
        "a file); restore it from backup or move the vault directory aside"
    )


def check_can_init(path: Path | None = None) -> None:
    """Refuse init before anything is asked for or written.

    Refuses when ``identity.key`` or ``identity.age`` exists in any form (file,
    directory, symlink), and when ``cards.db`` already holds cards: a new key
    would leave them sealed to the old one and unreadable.
    """
    path = path or vault_dir()
    if not _check_root(path):
        return
    if _has_key_entry(path):
        if is_initialised(path):
            raise VaultError("the vault is already initialised")
        raise _incomplete()
    count = _card_count(path)
    if count:
        raise VaultError(
            f"cards.db holds {count} card(s) sealed to an earlier key; move the vault directory aside first"
        )


def init_vault(path: Path | None = None) -> str:
    """Create the vault; return the recipient (public key).

    Refuses as ``check_can_init`` does. A run killed midway leaves no
    ``identity.key`` and no cards, so the vault still reads as uninitialised
    and a rerun starts over. Concurrent inits are serialised by a directory
    lock, every file is written through the locked directory's descriptor, and
    the key is linked into place, so one can never replace another's.
    """
    from pyrage import x25519

    path = path or vault_dir()
    check_can_init(path)
    identity = x25519.Identity.generate()
    recipient = str(identity.to_public())
    key = str(identity).encode() + b"\n"
    del identity
    _ensure_dir(path)
    try:
        with _dir_lock(path) as dfd:
            here, held = os.lstat(path), os.fstat(dfd)
            if (here.st_dev, here.st_ino) != (held.st_dev, held.st_ino):
                raise VaultError(f"another directory was moved onto {path}; it was left as it is")
            check_can_init(path)  # another init may have finished while we waited
            _publish(dfd, recipient.encode() + b"\n", key)
            here = os.lstat(path)
            if (here.st_dev, here.st_ino) != (held.st_dev, held.st_ino):
                raise VaultError(
                    f"another directory was moved onto {path} while init ran; it was left "
                    "as it is, and this run's vault is in the directory that was moved away"
                )
    except OSError as exc:
        raise VaultError(f"the vault could not be written ({exc.strerror or exc})") from None
    return recipient


def _create_cards_db(dfd: int, identity_digest: str, recipient_text: str) -> None:
    """Create cards.db (0600, schema at SCHEMA_VERSION) in ``dfd``, or accept an empty one.

    Either way it records the key pair this init is about to publish: the
    key file's sha256 and the recipient text.
    """
    cfd = os.open(CARDS, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dfd)
    conn = None
    try:
        info = os.fstat(cfd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise VaultError("cards.db is not a regular file you own; move it aside")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(cfd, 0o600)
        # Through the locked directory's descriptor (Linux /proc), so the schema
        # lands in this directory even if the vault path is swapped. SQLite
        # opens the file at connect (MEASURED on 3.45.1) and follows a symlink,
        # so check that cards.db is still the file checked above once it is
        # open (cfd stays open, so that inode cannot be reused). This catches a
        # swap before the open; one swapped in and back again within that
        # window is not seen, which for an accidental same-user operation is
        # accepted.
        conn = sqlite3.connect(f"/proc/self/fd/{dfd}/{CARDS}", isolation_level=None)
        opened = os.stat(CARDS, dir_fd=dfd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise VaultError("cards.db changed while init was opening it; run init again")
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise _translate(exc) from None
    except BaseException:
        if conn is not None:
            conn.close()
        raise
    finally:
        os.close(cfd)
    try:
        shape = _shape(conn)
        if shape == "foreign":
            raise VaultError("cards.db is not an empty vault card database; move it aside")
        # Tables, digest and version land together or not at all.
        conn.execute("BEGIN IMMEDIATE")
        if shape == "vault":
            if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]:
                conn.execute("ROLLBACK")
                raise VaultError("cards.db holds cards sealed to an earlier key; move it aside")
        else:
            conn.execute(_SCHEMA)
            conn.execute(_META_SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.executemany(
            "INSERT OR REPLACE INTO vault_meta (key, value) VALUES (?, ?)",
            [(_IDENTITY_DIGEST, identity_digest), (_RECIPIENT_ROW, recipient_text)],
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise _translate(exc) from None
    finally:
        conn.close()


def _publish(dfd: int, recipient: bytes, key: bytes) -> None:
    """Write cards.db, recipient.txt, then identity.key; temp names never outlive it."""
    try:
        _publish_staged(dfd, recipient, key)
    finally:
        for name in (RECIPIENT, IDENTITY):
            with suppress(FileNotFoundError):
                os.unlink(f".{name}.tmp", dir_fd=dfd)


def _publish_staged(dfd: int, recipient: bytes, key: bytes) -> None:
    staged = {RECIPIENT: recipient, IDENTITY: key}
    for name in staged:  # leftovers of a killed run (the secret key, or the public one)
        with suppress(FileNotFoundError):
            os.unlink(f".{name}.tmp", dir_fd=dfd)
    for name, data in staged.items():
        _write_new(f".{name}.tmp", data, dfd)
    # Before the identity: an initialised vault always has one, recording its digest.
    _create_cards_db(dfd, hashlib.sha256(key).hexdigest(), recipient.decode().strip())
    for name in (IDENTITY, SEALED_IDENTITY):  # through dfd, so nothing is overwritten
        with suppress(FileNotFoundError):
            os.stat(name, dir_fd=dfd, follow_symlinks=False)
            raise VaultError(
                f"an {name} appeared while this init ran; this init stopped without "
                "publishing its key, and the copy that appeared is refused as not this vault's "
                "key (move it aside and run init again, or restore the vault from backup)"
            )
    os.replace(f".{RECIPIENT}.tmp", RECIPIENT, src_dir_fd=dfd, dst_dir_fd=dfd)
    try:
        # identity.key last, and linked rather than renamed: link fails if
        # the target exists, so an identity is never overwritten.
        os.link(f".{IDENTITY}.tmp", IDENTITY, src_dir_fd=dfd, dst_dir_fd=dfd)
    except FileExistsError:
        # Someone else's identity landed (a hand copy). This run's recipient
        # must not stay beside it; theirs cannot be told apart from a
        # leftover, so the vault is left incomplete (refused until restored),
        # never paired with the wrong key.
        with suppress(OSError, VaultError):
            if _read_regular(RECIPIENT, dfd) == recipient:
                os.unlink(RECIPIENT, dir_fd=dfd)
        with suppress(FileNotFoundError):
            os.unlink(f".{IDENTITY}.tmp", dir_fd=dfd)
        raise VaultError(
            "another identity.key appeared while this init ran; it was not replaced. "
            "If `vault status` says incomplete, restore the vault from backup"
        ) from None
    os.unlink(f".{IDENTITY}.tmp", dir_fd=dfd)
    os.fsync(dfd)


def parse_card(number: str, expiry: str, cvc: str) -> dict:
    """Validate and normalise card entry. Errors never echo the input."""
    digits = re.sub(r"[ \-  ]", "", number)
    brand = card_brand(digits) if digits.isascii() and digits.isdigit() else None
    if brand is None:
        raise VaultError("that is not a valid card number (checksum, brand or length)")
    m = _EXPIRY.fullmatch(expiry.strip())
    if not m:
        raise VaultError("the expiry must be MM/YY or MM/YYYY")
    month, year = int(m.group(1)), int(m.group(2))
    year += 2000 if year < 100 else 0
    now = datetime.now(UTC)
    if (year, month) < (now.year, now.month):
        raise VaultError("that card has expired")
    code = cvc.strip()
    if not (code.isascii() and code.isdigit() and len(code) == (4 if brand == "amex" else 3)):
        raise VaultError("the security code must be 3 digits (4 for Amex)")
    return {"number": digits, "exp_month": month, "exp_year": year, "cvc": code, "brand": brand}


def normalise_merchant(merchant: str) -> str:
    host = merchant.strip().lower().rstrip(".")
    if not _HOST.fullmatch(host):
        raise VaultError("the merchant scope must be a bare host name, like shop.example.com")
    return host


def check_entry(label: str, merchant: str | None, single_use: bool) -> str | None:
    """Check a card's clear fields before any secret is asked for; return the
    normalised merchant. Virtual cards only: a merchant scope, single-use, or both."""
    if merchant is None and not single_use:
        raise VaultError("a card needs a merchant scope (--merchant) or --single-use")
    if (
        not label.strip()
        or len(label) > MAX_LABEL_LENGTH
        or any(unicodedata.category(ch).startswith("C") for ch in label)
    ):
        raise VaultError(f"the label must be 1-{MAX_LABEL_LENGTH} printable characters")
    # Both are stored in the clear and printed by `list`.
    if contains_card_number(label) or (merchant and contains_card_number(merchant)):
        raise VaultError("the label or merchant looks like it holds a card number")
    return normalise_merchant(merchant) if merchant is not None else None


def check_ready(path: Path | None = None) -> None:
    """Refuse to use a vault that is not initialised, not intact, or not private.

    Everything ``vault status`` reports as an error refuses here (a vault still
    to migrate included), before any card detail is asked for.
    """
    path = path or vault_dir()
    status = vault_status(path)
    if status["error"]:
        raise VaultError(status["error"])
    if not status["initialised"]:
        raise VaultError("the vault is not initialised; run `python -m genesis vault init`")
    _read_recipient(path)  # the full key parse, beyond status's shape check
    loose = [name for name, info in status["files"].items() if not info["private"]]
    if loose:
        raise VaultError(f"not private: {', '.join(loose)}; fix the modes (see `vault status`)")


def _require_cards_db(path: Path) -> None:
    """Init writes cards.db before the key file, so an initialised vault without one lost it."""
    if not os.path.lexists(path / CARDS):
        raise VaultError(
            "cards.db is missing from an initialised vault (lost, or left out of a restore); "
            "restore the vault from backup rather than adding to an empty one"
        )


def add_card(
    card: dict,
    *,
    label: str,
    merchant: str | None,
    single_use: bool,
    name: str = "",
    postal_code: str = "",
    path: Path | None = None,
) -> str:
    """Encrypt ``card`` (from ``parse_card``) to the vault's public key; return its ref."""
    from pyrage import encrypt

    path = path or vault_dir()
    merchant = check_entry(label, merchant, single_use)
    with _dir_lock(path) as dfd:
        _pin(path, dfd)  # the checks below read the locked directory, not a later one
        check_ready(path)
        pinned = _pin(path, dfd)
        recipient_text = _recipient_text(pinned)  # read once: the key sealed to is the key checked
        recipient = _parse_recipient(recipient_text)
        try:
            digest = hashlib.sha256(_read_small(pinned / IDENTITY)).hexdigest()
        except OSError as exc:
            raise VaultError(f"identity.key cannot be read ({exc.strerror})") from None
        ref = "card_" + secrets.token_hex(6)
        # The row's identity and scope ride inside the authenticated blob, so a
        # blob copied to another row, or a row re-scoped in the clear, is caught
        # by whoever decrypts it.
        secret = {"v": BLOB_VERSION, "card_ref": ref, "merchant": merchant}
        secret["single_use"] = single_use
        secret.update({k: card[k] for k in ("number", "exp_month", "exp_year", "cvc")})
        secret.update(name=name, postal_code=postal_code)
        try:
            blob = encrypt(json.dumps(secret).encode(), [recipient])
        except Exception as exc:  # noqa: BLE001 - never a raw library error, never a value
            raise VaultError(f"the card could not be encrypted ({type(exc).__name__})") from None
        _insert_card(pinned, ref, card, label, merchant, single_use, blob, (digest, recipient_text))
    return ref


def _pin(path: Path, dfd: int) -> Path:
    """The locked directory, if it is still the one at ``path``, as a path through its fd.

    Everything read or written after the checks goes through this path, so a
    vault directory moved onto ``path`` meanwhile is never mixed with the one
    that was checked: the key and the database always come from one directory.
    """
    try:
        live = os.lstat(path)
    except OSError as exc:
        raise VaultError(f"the vault directory cannot be checked ({exc.strerror})") from None
    held = os.fstat(dfd)
    if (live.st_dev, live.st_ino) != (held.st_dev, held.st_ino):
        raise VaultError("the vault directory was replaced during the operation; try again")
    return Path(f"/proc/self/fd/{dfd}")


def _insert_card(path, ref, card, label, merchant, single_use, blob, keys) -> None:
    """Insert the row, if cards.db still records ``keys`` (identity digest, recipient)."""
    with _db(path, write=True) as conn, conn:
        # Checked in the transaction that writes, against the key pair read
        # after check_ready: a cards.db swapped in or rewritten since is
        # refused here, rather than taking a card into a vault status refuses.
        rows = dict(conn.execute("SELECT key, value FROM vault_meta").fetchall())
        if (rows.get(_IDENTITY_DIGEST), rows.get(_RECIPIENT_ROW)) != keys:
            raise VaultError(
                "cards.db changed during the add (it does not record the key pair the "
                "card was sealed to); nothing was saved. Run `vault status`"
            )
        conn.execute(
            "INSERT INTO cards (card_ref, label, brand, last4, merchant, single_use, status,"
            " created_at, blob) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)",
            (
                ref,
                label,
                card["brand"],
                card["number"][-4:],
                merchant,
                int(single_use),
                _now(),
                blob,
            ),
        )


def list_cards(path: Path | None = None) -> list[dict]:
    """Every card's clear columns (never the blob, never the expiry)."""
    path = path or vault_dir()
    if not _check_root(path):
        return []
    if is_initialised(path):
        _require_cards_db(path)
    elif _card_count(path) == 0:  # a run killed before its identity landed
        return []
    with _db(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT card_ref, label, brand, last4, merchant, single_use, status, created_at,"
            " retired_at FROM cards ORDER BY created_at, card_ref"
        ).fetchall()
    return [dict(r) for r in rows]


def check_active_card(card_ref: str, path: Path | None = None) -> None:
    """Refuse a malformed or unknown ref, without echoing what was typed."""
    path = path or vault_dir()
    if not CARD_REF.fullmatch(card_ref):
        raise VaultError(
            "that is not a card ref (card_ followed by 12 hex digits; see `vault list`)"
        )
    if not any(c["card_ref"] == card_ref and c["status"] == "active" for c in list_cards(path)):
        raise VaultError(f"no active card {card_ref}")


def _public_of(key_text: str, name: str) -> str:
    """The recipient text of an age X25519 secret key."""
    from pyrage import IdentityError, x25519

    try:
        return str(x25519.Identity.from_str(key_text).to_public())
    except (IdentityError, ValueError):
        raise VaultError(f"{name} does not hold an age X25519 key") from None


def retire_card(card_ref: str, path: Path | None = None) -> None:
    """Mark a card retired and clear its encrypted blob."""
    path = path or vault_dir()
    with _dir_lock(path) as dfd:
        _pin(path, dfd)  # the checks below read the locked directory, not a later one
        check_ready(path)
        check_active_card(card_ref, path)
        _retire_row(_pin(path, dfd), card_ref)


def _retire_row(path: Path, card_ref: str) -> None:
    with _db(path, write=True) as conn, conn:
        cur = conn.execute(
            "UPDATE cards SET status = 'retired', retired_at = ?, blob = NULL"
            " WHERE card_ref = ? AND status = 'active'",
            (_now(), card_ref),
        )
        if cur.rowcount != 1:
            raise VaultError(f"no active card {card_ref}")


def vault_status(path: Path | None = None) -> dict:
    """Whether the vault exists and is intact, its card counts, and its file modes.

    ``error`` is set when the vault cannot be used as it is: the root or a
    file is unreadable or the wrong kind, the vault is incomplete, the key file
    or recipient.txt is damaged or is not the pair cards.db records, cards.db
    is missing, damaged or foreign, cards remain without an identity, or the
    key is still sealed (``migrate`` is then "needed", or "resume" for a
    migration that stopped part-way; the cards are still counted). ``cards`` is
    None when they cannot be counted. Nothing here writes to the vault.
    """
    path = path or vault_dir()
    files = {}
    counts: dict | None = {"active": 0, "retired": 0}
    error = None
    migrate = None
    initialised = False
    try:
        if _check_root(path):
            for name in ("", IDENTITY, SEALED_IDENTITY, f".{IDENTITY}.tmp", RECIPIENT, CARDS):
                try:
                    mode = stat.S_IMODE(os.lstat(path / name).st_mode)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise VaultError(f"{name} cannot be checked ({exc.strerror})") from None
                files[name or "."] = {"mode": oct(mode), "private": not mode & 0o077}
            initialised = is_initialised(path)
            if _has_key_entry(path) and not initialised:
                raise _incomplete()
            key = sealed = None
            if initialised:
                if os.path.lexists(path / IDENTITY):
                    key = _check_identity(path)
                else:
                    sealed = _check_sealed(path)
                if importlib.util.find_spec("pyrage") is not None:
                    _read_recipient(path)  # the checksum too, as add will
                else:
                    _recipient_text(path)  # status runs without the payments extra
            for card in list_cards(path):
                if card["status"] not in counts:
                    raise VaultError("cards.db holds a card with an unknown status; restore it")
                counts[card["status"]] += 1
            # After list_cards, so cards.db's own errors come first.
            if key is not None:
                migrate = _key_state(path, key)
                # The digest proves the file is the recorded one; with the
                # payments extra, also that it is recipient.txt's pair.
                pairs = importlib.util.find_spec("pyrage") is None or _public_of(
                    key.decode("ascii").strip(), IDENTITY
                ) == _recipient_text(path)
                if migrate in (None, "cleanup") and not pairs:
                    raise VaultError(
                        "identity.key and recipient.txt do not match; restore the vault from backup"
                    )
            elif sealed is not None:
                _check_bound_keys(path, sealed, SEALED_IDENTITY)
                migrate = "needed"
            held = counts["active"] + counts["retired"]
            if held and not initialised:
                raise VaultError(
                    f"cards.db holds {held} card(s) but identity.key is missing; "
                    "restore the vault from backup"
                )
            if migrate in ("needed", "resume"):
                from genesis.payments.vault_migrate import MIGRATE_TEXT

                error = MIGRATE_TEXT[migrate]
    except VaultError as exc:
        error, counts, migrate = str(exc), None, None
    return {
        "path": str(path),
        "initialised": initialised,
        "cards": counts,
        "files": files,
        "error": error,
        "migrate": migrate,
    }


def _key_state(path: Path, key: bytes) -> str | None:
    """None when identity.key is the key cards.db records; "cleanup" when it is
    and a sealed copy is still beside it (a migration stopped after its row
    moved); "resume" when a migration stopped after placing the key (cards.db
    still records the sealed key, which is still there). Anything else is a
    VaultError."""
    digest, _recipient = _bound_rows(path)
    if digest == hashlib.sha256(key).hexdigest():
        _check_bound_keys(path, key)  # the recipient row too
        return "cleanup" if os.path.lexists(path / SEALED_IDENTITY) else None
    if os.path.lexists(path / SEALED_IDENTITY):  # resume only with a key that pairs
        with suppress(VaultError):
            _check_bound_keys(path, _check_sealed(path), SEALED_IDENTITY)
            if importlib.util.find_spec("pyrage") is None or _public_of(
                key.decode("ascii").strip(), IDENTITY
            ) == _recipient_text(path):
                return "resume"
    _check_bound_keys(path, key)  # raises: the key file is not the recorded one
    return None
