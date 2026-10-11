"""The encrypted card vault on disk.

Layout of ``~/.genesis/payment_vault/`` (directory 0700, every file 0600):

- ``recipient.txt``: the vault's age X25519 public key. Adding a card needs
  only this, so ``vault add`` never asks for the passphrase.
- ``identity.age``: the matching secret key, encrypted under the owner's
  passphrase (age scrypt). Its presence marks the vault initialised, so
  ``init`` writes it last and never over an existing one.
- ``cards.db`` (``user_version`` 1): one row per card. The number, expiry and
  security code exist only inside ``blob``, age-encrypted to
  ``recipient.txt``; the row keeps a label, brand, last four digits and the
  merchant scope in the clear, for ``list``. Retiring a card clears its blob
  with ``secure_delete`` on, so the ciphertext does not stay in a free page
  of the file. That is best effort: SQLite's rollback journal and the
  filesystem may keep old ciphertext, so a retired virtual card is also
  closed with its issuer.

Only virtual cards go in: every card is scoped to one merchant, or marked
single-use, or both. Decryption of cards is not here; the vault service that
unlocks the identity and fills a card comes later.
"""

from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import stat
import unicodedata
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from genesis.env import genesis_home
from genesis.security.card_shapes import card_brand, contains_card_number

IDENTITY = "identity.age"
RECIPIENT = "recipient.txt"
CARDS = "cards.db"
SCHEMA_VERSION = 1
BLOB_VERSION = 1

# zxcvbn's top score: "very unguessable" (guesses >= 10^10).
MIN_PASSPHRASE_SCORE = 4
# zxcvbn refuses input over 72 characters by default; longer passphrases are
# scored whole. Its cost grows faster than linearly: MEASURED on a repetitive
# phrase, 0.9 s at 256 characters and 127 s at 1024. 256 is past any
# diceware passphrase (20 words is about 140).
MAX_PASSPHRASE_LENGTH = 256
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

_EXPIRY = re.compile(r"(0[1-9]|1[0-2])\s*/\s*([0-9]{2}|[0-9]{4})")
_HOST = re.compile(
    r"(?=.{1,253}\Z)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+([a-z]{2,63}|xn--[a-z0-9-]{1,59})"
)
CARD_REF = re.compile(r"card_[0-9a-f]{12}")


class VaultError(Exception):
    """A vault operation was refused. The message never carries a card value."""


def vault_dir() -> Path:
    return genesis_home() / "payment_vault"


def passphrase_score(passphrase: str) -> int:
    from zxcvbn import zxcvbn

    if len(passphrase) > MAX_PASSPHRASE_LENGTH:
        raise VaultError(f"the passphrase is longer than {MAX_PASSPHRASE_LENGTH} characters")
    return int(zxcvbn(passphrase, max_length=max(72, len(passphrase)))["score"])


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


def _read_regular(name: str, dir_fd: int, limit: int | None = None) -> bytes:
    """Read a regular file without following a symlink or blocking on a FIFO."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VaultError(f"{name} is not a regular file")
        return f.read() if limit is None else f.read(limit)


@contextmanager
def _dir_lock(path: Path):
    """Hold an exclusive lock on the vault directory (one init at a time).

    Yields the directory's file descriptor. Init writes through it, so every
    file it publishes lands in the directory it locked even if another
    directory is moved onto the path meanwhile.
    """
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


_RECIPIENT_TEXT = re.compile(r"age1[0-9a-z]+")
# What `age_passphrase.encrypt` writes first: the header and a scrypt stanza.
_SEALED_HEADER = b"age-encryption.org/v1\n-> scrypt "


def _recipient_text(path: Path) -> str:
    """recipient.txt's key text, checked by shape only (no crypto import)."""
    try:
        text = (path / RECIPIENT).read_bytes().decode("utf-8").strip()
    except OSError as exc:
        raise VaultError(f"recipient.txt cannot be read ({exc.strerror})") from None
    except UnicodeDecodeError:
        text = ""
    if not _RECIPIENT_TEXT.fullmatch(text):
        raise VaultError(
            "recipient.txt does not hold a valid age public key; restore the vault from backup"
        )
    return text


def _read_recipient(path: Path):
    """The vault's age recipient; a missing or damaged recipient.txt is a VaultError."""
    from pyrage import RecipientError, x25519

    try:
        return x25519.Recipient.from_str(_recipient_text(path))
    except RecipientError:
        raise VaultError(
            "recipient.txt does not hold a valid age public key; restore the vault from backup"
        ) from None


def _check_identity(path: Path) -> None:
    """identity.age must be a whole passphrase-sealed age key, checked without the passphrase.

    A sealed X25519 identity is the age header, an scrypt stanza, a MAC line,
    then exactly 106 payload bytes (16 nonce + 74 key + 16 tag; MEASURED on
    pyrage 1.4.0). A file cut anywhere fails this, before a card is sealed to
    a key whose secret half is gone.
    """
    dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        data = _read_regular(IDENTITY, dfd, 4096)
    except OSError as exc:
        raise VaultError(f"identity.age cannot be read ({exc.strerror})") from None
    finally:
        os.close(dfd)
    _head, sep, rest = data.partition(b"\n--- ")
    mac, newline, payload = rest.partition(b"\n")
    if (
        not (data.startswith(_SEALED_HEADER) and sep and len(mac) == 43 and newline)
        or len(payload) != 106
    ):
        raise VaultError(
            "identity.age is not a whole passphrase-sealed age key (damaged?); "
            "restore the vault from backup"
        )


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
    """'vault' (the cards table at SCHEMA_VERSION), 'empty' (no tables yet), or 'foreign'."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    tables = {name for name, _sql in rows}
    if tables == {"cards"} and version == SCHEMA_VERSION:
        # The table must be the one init writes, constraints and all: a cards
        # table of another shape is foreign, not a vault the code can trust.
        return "vault" if _normalised(rows[0][1]) == _normalised(_SCHEMA) else "foreign"
    if not tables and version == 0:
        return "empty"
    return "foreign"


@contextmanager
def _db(path: Path):
    """An open, existing cards.db whose statements raise VaultError, never sqlite3 errors.

    Opened read-write but never created, and never given a schema: only init
    writes the schema, so a reader cannot turn a truncated or foreign file
    into an empty vault database.
    """
    db = path / CARDS
    try:
        info = os.lstat(db)
    except FileNotFoundError:
        raise VaultError("cards.db is missing") from None
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    if not stat.S_ISREG(info.st_mode):
        raise VaultError("cards.db is not a regular file (a symlink or other); refusing to open it")
    conn = None
    try:
        conn = sqlite3.connect(f"file:{quote(str(db))}?mode=rw", uri=True)
        conn.execute("PRAGMA secure_delete = ON")
        shape = _shape(conn)
        if shape != "vault":
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            raise VaultError(
                "cards.db is not a vault card database "
                f"({'empty' if shape == 'empty' else f'format version {version}'}); "
                "restore the vault from backup"
            )
        yield conn
    except sqlite3.Error as exc:
        raise _translate(exc) from None
    finally:
        if conn is not None:
            conn.close()


def _card_count(path: Path) -> int:
    """Cards in cards.db: 0 for an absent, 0-byte or never-written file."""
    try:
        if not os.path.lexists(path / CARDS) or os.lstat(path / CARDS).st_size == 0:
            return 0
    except OSError as exc:
        raise VaultError(f"cards.db cannot be checked ({exc.strerror})") from None
    try:
        with closing(sqlite3.connect(f"file:{quote(str(path / CARDS))}?mode=ro", uri=True)) as c:
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
    path = path or vault_dir()
    try:
        return (path / IDENTITY).is_file() and (path / RECIPIENT).is_file()
    except OSError as exc:
        raise VaultError(f"the vault cannot be checked ({exc.strerror})") from None


def _incomplete() -> VaultError:
    return VaultError(
        "the vault is incomplete (identity.age is there but recipient.txt is missing or not "
        "a file); restore it from backup or move the vault directory aside"
    )


def check_can_init(path: Path | None = None) -> None:
    """Refuse init before anything is asked for or written.

    Refuses when ``identity.age`` exists in any form (file, directory,
    symlink), and when ``cards.db`` already holds cards: a new key would
    leave them sealed to the old one and unreadable.
    """
    path = path or vault_dir()
    if not _check_root(path):
        return
    if os.path.lexists(path / IDENTITY):
        if is_initialised(path):
            raise VaultError("the vault is already initialised")
        raise _incomplete()
    count = _card_count(path)
    if count:
        raise VaultError(
            f"cards.db holds {count} card(s) sealed to an earlier key; move the vault directory aside first"
        )


def init_vault(passphrase: str, path: Path | None = None) -> str:
    """Create the vault; return the recipient (public key).

    Refuses as ``check_can_init`` does, and a passphrase below
    MIN_PASSPHRASE_SCORE. The key is sealed (scrypt, slow) before the lock is
    taken, so nothing slow runs between the check under the lock and the
    writes. A run killed midway leaves no ``identity.age`` and no cards, so
    the vault still reads as uninitialised and a rerun starts over.
    Concurrent inits are serialised by a directory lock, every file is written
    through the locked directory's descriptor, and the identity is linked into
    place, so one can never replace another's.
    """
    from pyrage import passphrase as age_passphrase
    from pyrage import x25519

    path = path or vault_dir()
    check_can_init(path)
    if passphrase_score(passphrase) < MIN_PASSPHRASE_SCORE:
        raise VaultError("the passphrase is too guessable (needs zxcvbn score 4)")
    identity = x25519.Identity.generate()
    recipient = str(identity.to_public())
    sealed = age_passphrase.encrypt(str(identity).encode(), passphrase)
    del identity
    _ensure_dir(path)
    try:
        with _dir_lock(path) as dfd:
            here, held = os.lstat(path), os.fstat(dfd)
            if (here.st_dev, here.st_ino) != (held.st_dev, held.st_ino):
                raise VaultError(f"another directory was moved onto {path}; it was left as it is")
            check_can_init(path)  # another init may have finished while we waited
            _publish(dfd, recipient.encode() + b"\n", sealed)
            here = os.lstat(path)
            if (here.st_dev, here.st_ino) != (held.st_dev, held.st_ino):
                raise VaultError(
                    f"another directory was moved onto {path} while init ran; it was left "
                    "as it is, and this run's vault is in the directory that was moved away"
                )
    except OSError as exc:
        raise VaultError(f"the vault could not be written ({exc.strerror or exc})") from None
    return recipient


def _create_cards_db(dfd: int) -> None:
    """Create cards.db (0600, schema at SCHEMA_VERSION) in ``dfd``, or accept an empty one."""
    cfd = os.open(CARDS, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dfd)
    try:
        info = os.fstat(cfd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise VaultError("cards.db is not a regular file you own; move it aside")
        if stat.S_IMODE(info.st_mode) != 0o600:
            os.fchmod(cfd, 0o600)
    finally:
        os.close(cfd)
    # Through the locked directory's descriptor (Linux /proc), so the schema
    # lands in the file just checked even if the vault path is swapped.
    conn = sqlite3.connect(f"/proc/self/fd/{dfd}/{CARDS}", isolation_level=None)
    try:
        shape = _shape(conn)
        if shape == "vault":
            if conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]:
                raise VaultError("cards.db holds cards sealed to an earlier key; move it aside")
            return
        if shape == "foreign":
            raise VaultError("cards.db is not an empty vault card database; move it aside")
        conn.execute("BEGIN IMMEDIATE")  # table and version land together or not at all
        conn.execute(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise _translate(exc) from None
    finally:
        conn.close()


def _publish(dfd: int, recipient: bytes, sealed: bytes) -> None:
    """Write cards.db, recipient.txt, then identity.age, all in ``dfd``."""
    staged = {RECIPIENT: recipient, IDENTITY: sealed}
    for name in staged:  # leftovers of a killed run (the sealed key, or the public one)
        with suppress(FileNotFoundError):
            os.unlink(f".{name}.tmp", dir_fd=dfd)
    for name, data in staged.items():
        _write_new(f".{name}.tmp", data, dfd)
    _create_cards_db(dfd)  # before the identity: an initialised vault always has one
    with suppress(FileNotFoundError):  # checked here, through dfd, so nothing is overwritten
        os.stat(IDENTITY, dir_fd=dfd, follow_symlinks=False)
        raise VaultError("an identity.age appeared while this init ran; nothing was changed")
    os.replace(f".{RECIPIENT}.tmp", RECIPIENT, src_dir_fd=dfd, dst_dir_fd=dfd)
    try:
        # identity.age last, and linked rather than renamed: link fails if
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
            "another identity.age appeared while this init ran; it was not replaced. "
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

    Everything ``vault status`` reports as an error refuses here, before any
    card detail or passphrase is asked for.
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
    """Init writes cards.db before identity.age, so an initialised vault without one lost it."""
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
    check_ready(path)
    merchant = check_entry(label, merchant, single_use)
    recipient = _read_recipient(path)
    secret = {"v": BLOB_VERSION}
    secret.update({k: card[k] for k in ("number", "exp_month", "exp_year", "cvc")})
    secret.update(name=name, postal_code=postal_code)
    blob = encrypt(json.dumps(secret).encode(), [recipient])
    ref = "card_" + secrets.token_hex(6)
    with _db(path) as conn, conn:
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
    return ref


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


def verify_passphrase(passphrase: str, path: Path | None = None) -> None:
    from pyrage import DecryptError
    from pyrage import passphrase as age_passphrase

    path = path or vault_dir()
    if not is_initialised(path):
        raise VaultError("the vault is not initialised")
    from pyrage import IdentityError, x25519

    try:
        key = age_passphrase.decrypt((path / IDENTITY).read_bytes(), passphrase).decode()
    except DecryptError:
        raise VaultError("wrong passphrase (or identity.age is damaged)") from None
    except OSError as exc:
        raise VaultError(f"identity.age cannot be read ({exc.strerror})") from None
    try:
        public = str(x25519.Identity.from_str(key).to_public())
    except (IdentityError, ValueError):
        raise VaultError("identity.age does not hold an age X25519 key") from None
    finally:
        del key
    # Cards are sealed to recipient.txt: a key that is not its pair opens none of them.
    if public != _recipient_text(path):
        raise VaultError(
            "identity.age and recipient.txt do not match; restore the vault from backup"
        )


def retire_card(card_ref: str, passphrase: str, path: Path | None = None) -> None:
    """Mark a card retired and clear its encrypted blob. Needs the passphrase."""
    path = path or vault_dir()
    check_ready(path)
    check_active_card(card_ref, path)
    verify_passphrase(passphrase, path)
    with _db(path) as conn, conn:
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
    file is unreadable or the wrong kind, the vault is incomplete, identity.age
    or recipient.txt is damaged, cards.db is missing, damaged or foreign, or
    cards remain without an identity. ``cards`` is None when they cannot be
    counted. Nothing here writes to the vault.
    """
    path = path or vault_dir()
    files = {}
    counts: dict | None = {"active": 0, "retired": 0}
    error = None
    initialised = False
    try:
        if _check_root(path):
            for name in ("", IDENTITY, RECIPIENT, CARDS):
                try:
                    mode = stat.S_IMODE(os.lstat(path / name).st_mode)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise VaultError(f"{name} cannot be checked ({exc.strerror})") from None
                files[name or "."] = {"mode": oct(mode), "private": not mode & 0o077}
            initialised = is_initialised(path)
            if os.path.lexists(path / IDENTITY) and not initialised:
                raise _incomplete()
            if initialised:
                _check_identity(path)
                if importlib.util.find_spec("pyrage") is not None:
                    _read_recipient(path)  # the checksum too, as add will
                else:
                    _recipient_text(path)  # status runs without the payments extra
            for card in list_cards(path):
                if card["status"] not in counts:
                    raise VaultError("cards.db holds a card with an unknown status; restore it")
                counts[card["status"]] += 1
            held = counts["active"] + counts["retired"]
            if held and not initialised:
                raise VaultError(
                    f"cards.db holds {held} card(s) but identity.age is missing; "
                    "restore the vault from backup"
                )
    except VaultError as exc:
        error, counts = str(exc), None
    return {
        "path": str(path),
        "initialised": initialised,
        "cards": counts,
        "files": files,
        "error": error,
    }
