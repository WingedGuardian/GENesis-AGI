"""Caller credentials for a shared MCP service, and the resolver that reads them.

A shared MCP service serves many callers from one process, so its own
environment says nothing about who is calling (see ``genesis.caller_context``).
Each request instead carries a credential, and :meth:`CallerResolver.resolve`
turns that credential into the :class:`~genesis.caller_context.CallerContext`
the service sets for the request with ``request_caller``.

The design is stateless (owner ruling): no token table, no minting, no
revocation. There are two kinds of credential, each a static secret in its own
file under :func:`genesis.env.mcp_credentials_dir`:

- **The forwarder credential** (``forwarder``). A per-session forwarder runs in
  the Claude Code session's own environment and forwards that session's
  identity fields with each request. The service accepts those fields only
  after this credential has authenticated, and validates every one of them.
  Two foreground sessions are told apart by the session and transcript ids
  they forward.
- **One external-client credential per profile** (``external-<profile>``). It
  resolves to that profile's fixed caller and nothing else: a request that
  presents it together with identity fields is refused, so an external client
  can never claim to be a foreground or a supervised session.

Trust statement: every session runs as the same uid and can read these files,
so they stop drift and accidental misuse, not a determined local process. That
is the same boundary the environment markers give today. Note what that means
for the forwarder secret: whoever holds it can claim any foreground identity, so
it is equivalent to foreground authority, and only the forwarder should read it.

The resolver never reads the process environment. A request with a missing,
unknown or malformed credential is :class:`Rejected`, and its reason is the same
whichever secret it was closest to.

Nothing in the runtime calls this module yet: the shared-service mode is the
reader.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re
import secrets
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from genesis.caller_context import CallerContext
from genesis.memory.provenance import ORIGIN_CLASSES

logger = logging.getLogger(__name__)

FORWARDER = "forwarder"
_EXTERNAL_PREFIX = "external-"

# The fixed caller each external-client profile resolves to. A closed set: a
# profile missing here has no credential file and resolves nothing.
# ``interactive`` is the owner-attended Codex role (owner ruling: an interactive
# credential is supervised and not dispatched). It is provisioned only when
# asked for by name, never by default.
_PROFILE_CALLERS = MappingProxyType(
    {
        "external": CallerContext(dispatched=True, supervised=False, profile="external"),
        "validator": CallerContext(dispatched=True, supervised=False, profile="validator"),
        "interactive": CallerContext(dispatched=False, supervised=True, profile="interactive"),
    }
)
DEFAULT_PROFILES = ("external",)

# secrets.token_urlsafe(32): 32 random bytes, 43 URL-safe characters.
_TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_TOKEN_FILE_MAX_BYTES = 128  # one token plus slack for a trailing newline

# Forwarded identity, by field. The two flags are required: a forwarder that
# leaves one out is refused rather than given a default, so a missing field can
# never become foreground authority. The id fields are optional; an id that is
# present is passed through unchanged once it validates. 256 characters is a
# resource bound, not a measured length: the ids are UUIDs or short hex today.
_REQUIRED_FLAGS = ("dispatched", "supervised")
_OPTIONAL_IDS = ("session_id", "transcript_id", "legacy_session_id")
_ORIGIN = "origin"
_FORWARDED_FIELDS = frozenset((*_REQUIRED_FLAGS, *_OPTIONAL_IDS, _ORIGIN))
# Ids are a closed character set (no whitespace, no path separator, no leading
# dot), so no consumer can turn one into a path outside its directory. The empty
# string is kept: the environment path keeps it raw and readers treat it as unset.
_ID_RE = re.compile(r"|[A-Za-z0-9_:-][A-Za-z0-9._:-]{0,255}")


@dataclass(frozen=True, slots=True)
class Rejected:
    """A request the resolver refuses. ``reason`` is one of the constants below."""

    reason: str


MISSING_CREDENTIAL = "missing_credential"
INVALID_CREDENTIAL = "invalid_credential"
INVALID_IDENTITY = "invalid_identity"
IDENTITY_NOT_PERMITTED = "identity_not_permitted"


class CredentialStoreError(Exception):
    """The credential directory or one of its files is unusable. Never carries a secret."""


def credential_file_name(profile: str | None) -> str:
    """``forwarder`` for ``None``, else ``external-<profile>`` for a known profile."""
    if profile is None:
        return FORWARDER
    if profile not in _PROFILE_CALLERS:
        raise ValueError(f"unknown external-client profile {profile!r}")
    return _EXTERNAL_PREFIX + profile


def _default_dir() -> Path:
    from genesis.env import mcp_credentials_dir

    return mcp_credentials_dir()


def _secure_dir(directory: Path, *, create: bool) -> None:
    """Require *directory* to be a real 0700 directory this uid owns.

    With *create*, make it if absent and tighten a looser mode on one we own.
    """
    try:
        if create:
            directory.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(FileExistsError):
                directory.mkdir(mode=0o700)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise CredentialStoreError(
            f"{directory} is not a usable directory ({exc.strerror})"
        ) from None
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise CredentialStoreError(f"{directory} is not owned by this user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            if not create:
                raise CredentialStoreError(f"{directory} is readable by other users")
            os.fchmod(fd, 0o700)  # through the held handle: a swapped-in symlink is never followed
            logger.warning("Tightened %s to mode 0700", directory)
    finally:
        os.close(fd)


def _read_token(path: Path, *, repair_mode: bool) -> str:
    """Read one credential file: a regular 0600 file this uid owns, holding one token.

    Never follows a symlink and never blocks on a FIFO. Everything is checked
    on the open handle, and the content is validated BEFORE any repair: with
    *repair_mode*, a valid credential that is ours, singly linked and group- or
    world-accessible is tightened to 0600 instead of refused. Nothing else is
    ever changed, and the content is never rewritten.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise CredentialStoreError(f"{path} cannot be opened ({exc.strerror})") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise CredentialStoreError(f"{path} is not a regular file")
        if info.st_uid != os.getuid():
            raise CredentialStoreError(f"{path} is not owned by this user")
        if info.st_size > _TOKEN_FILE_MAX_BYTES:
            raise CredentialStoreError(f"{path} is larger than a credential")
        raw = os.read(fd, _TOKEN_FILE_MAX_BYTES + 1)
        try:
            token = raw.decode("ascii").strip()
        except UnicodeDecodeError:
            raise CredentialStoreError(f"{path} does not hold a credential") from None
        if len(raw) > _TOKEN_FILE_MAX_BYTES or not _TOKEN_RE.fullmatch(token):
            raise CredentialStoreError(f"{path} does not hold a credential")
        if stat.S_IMODE(info.st_mode) & 0o077:
            # A second hard link means another name shares this inode, whose mode
            # this must not change on that name's behalf.
            if not repair_mode or info.st_nlink != 1:
                raise CredentialStoreError(f"{path} is readable by other users")
            os.fchmod(fd, 0o600)
            logger.warning("Tightened %s to mode 0600", path)
    finally:
        os.close(fd)
    return token


def _create_once(path: Path) -> bool:
    """Write a fresh token to *path* unless a file is already there. True if created.

    The token goes to a private temporary file (``mkstemp`` creates it 0600) and
    is hard-linked into place, so the credential never exists empty or
    half-written, and a link never replaces an existing file: of two concurrent
    callers the first link wins and the second keeps that file.
    """
    try:
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(secrets.token_urlsafe(_TOKEN_BYTES))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                return False
        finally:
            os.unlink(tmp)
        dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise CredentialStoreError(
            f"{path.parent} cannot hold a credential ({exc.strerror})"
        ) from None
    return True


def ensure_caller_credentials(
    profiles: tuple[str, ...] = DEFAULT_PROFILES,
    directory: Path | None = None,
) -> dict[str, str]:
    """Make sure the forwarder credential and each named profile's credential exist.

    Idempotent. An existing credential is never overwritten; one with a loose
    mode is tightened, and one that is not a valid credential file raises
    :class:`CredentialStoreError` rather than being replaced. Returns
    ``{file name: "created" | "present"}``. No secret is returned or logged.

    It finishes by loading the whole directory exactly as the resolver will, so
    a success here means :meth:`CallerResolver.load` succeeds too: a bad file
    the caller did not name, or two files holding one secret, raise here.
    """
    names = [credential_file_name(None), *(credential_file_name(p) for p in profiles)]
    directory = directory or _default_dir()
    _secure_dir(directory, create=True)
    result: dict[str, str] = {}
    for name in dict.fromkeys(names):
        path = directory / name
        created = _create_once(path)
        _read_token(path, repair_mode=True)
        result[name] = "created" if created else "present"
    CallerResolver.load(directory)
    return result


def _presented_bytes(credential: object) -> bytes | None:
    if isinstance(credential, bytes):
        return credential
    if isinstance(credential, str) and credential.isascii():
        return credential.encode("ascii")
    return None


def _validated_identity(fields: object) -> CallerContext | None:
    """The forwarded identity as a CallerContext, or ``None`` if any field is wrong."""
    if not isinstance(fields, Mapping):
        return None
    fields = dict(fields)  # one read: a custom Mapping cannot answer differently twice
    if not set(fields) <= _FORWARDED_FIELDS:
        return None
    if any(type(fields.get(flag)) is not bool for flag in _REQUIRED_FLAGS):
        return None
    for key in _OPTIONAL_IDS:
        value = fields.get(key)
        if value is not None and not (type(value) is str and _ID_RE.fullmatch(value)):
            return None
    origin = fields.get(_ORIGIN)
    if origin is not None and (
        type(origin) is not str or (origin and origin not in ORIGIN_CLASSES)
    ):
        return None
    return CallerContext(
        dispatched=fields["dispatched"],
        supervised=fields["supervised"],
        session_id=fields.get("session_id"),
        origin=origin,
        transcript_id=fields.get("transcript_id"),
        legacy_session_id=fields.get("legacy_session_id"),
    )


class CallerResolver:
    """Resolves one request's credential and forwarded fields to its caller.

    Holds the configured secrets in memory and never prints them. Stateless per
    request: it reads no environment and keeps nothing between calls.
    """

    __slots__ = ("_entries",)

    def __init__(self, forwarder: str | None, external: Mapping[str, str]):
        pairs: list[tuple[str | None, object]] = (
            [(None, forwarder)] if forwarder is not None else []
        )
        for profile, token in external.items():
            credential_file_name(profile)
            pairs.append((profile, token))
        if any(not (isinstance(t, str) and _TOKEN_RE.fullmatch(t)) for _k, t in pairs):
            raise CredentialStoreError("a caller credential is not a valid token")
        entries = tuple((kind, token.encode("ascii")) for kind, token in pairs)
        if len({secret for _kind, secret in entries}) != len(entries):
            raise CredentialStoreError("two caller credentials hold the same secret")
        self._entries = entries

    @classmethod
    def load(cls, directory: Path | None = None) -> CallerResolver:
        """Read every credential present. Creates nothing; an absent one resolves nothing."""
        directory = directory or _default_dir()
        _secure_dir(directory, create=False)
        forwarder: str | None = None
        external: dict[str, str] = {}
        try:
            paths = sorted(directory.iterdir())
        except OSError as exc:
            raise CredentialStoreError(f"{directory} cannot be listed ({exc.strerror})") from None
        for path in paths:
            name = path.name
            if name == FORWARDER:
                forwarder = _read_token(path, repair_mode=False)
            elif name.startswith(_EXTERNAL_PREFIX):
                profile = name[len(_EXTERNAL_PREFIX) :]
                if profile not in _PROFILE_CALLERS:
                    logger.warning("Ignoring credential file %s: unknown profile", path)
                    continue
                external[profile] = _read_token(path, repair_mode=False)
        return cls(forwarder, external)

    def __reduce__(self):
        raise TypeError("CallerResolver holds secrets and cannot be pickled")

    def __repr__(self) -> str:
        kinds = ", ".join(FORWARDER if k is None else k for k, _s in self._entries)
        return f"CallerResolver({kinds})"

    def resolve(
        self, request_credential: object, forwarded_fields: object
    ) -> CallerContext | Rejected:
        """The caller for one request, or why it is refused.

        Every configured secret is compared, in constant time, whatever matches
        first. A forwarder match accepts the forwarded identity once it
        validates. An external-profile match resolves to that profile's fixed
        caller and refuses any forwarded identity field.
        """
        presented = _presented_bytes(request_credential)
        if presented == b"" or request_credential is None:
            return Rejected(MISSING_CREDENTIAL)
        # Every token is 43 characters, so length says nothing about which one is
        # near; the bound only keeps an oversized value out of the compare loop.
        if presented is None or len(presented) > _TOKEN_FILE_MAX_BYTES:
            return Rejected(INVALID_CREDENTIAL)
        matched = [kind for kind, secret in self._entries if hmac.compare_digest(presented, secret)]
        if len(matched) != 1:
            return Rejected(INVALID_CREDENTIAL)
        profile = matched[0]
        if profile is None:
            identity = _validated_identity(forwarded_fields)
            return identity if identity is not None else Rejected(INVALID_IDENTITY)
        if not (
            forwarded_fields is None
            or (isinstance(forwarded_fields, Mapping) and not forwarded_fields)
        ):
            return Rejected(IDENTITY_NOT_PERMITTED)
        return _PROFILE_CALLERS[profile]


def add_parser(subparsers) -> None:
    """``genesis mcp-credentials ensure [--profile P ...]``: provision, print no secret."""
    parser = subparsers.add_parser(
        "mcp-credentials", help="Provision the shared MCP service's caller credentials"
    )
    actions = parser.add_subparsers(dest="mcp_credentials_action", required=True)
    ensure = actions.add_parser(
        "ensure", help="Create any missing credential file (existing ones are kept)"
    )
    ensure.add_argument(
        "--profile",
        action="append",
        choices=sorted(_PROFILE_CALLERS),
        help=f"External-client profile to provision (repeatable; default: {', '.join(DEFAULT_PROFILES)})",
    )
    ensure.set_defaults(func=_cli_ensure)


def _cli_ensure(args) -> int:
    profiles = tuple(args.profile) if args.profile else DEFAULT_PROFILES
    directory = _default_dir()
    try:
        result = ensure_caller_credentials(profiles, directory)
    except CredentialStoreError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for name, status in result.items():
        print(f"{status}: {directory / name}")
    return 0
