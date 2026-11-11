"""Shared reference-store domain operations.

Neutral home for reference-store logic used by BOTH the MCP tool layer
(``genesis.mcp.memory.knowledge``) and the dashboard route layer
(``genesis.dashboard.routes.references``). Keeping it here avoids the
presentation layer importing the MCP tool module (wrong direction) and
gives a single source of truth for:

- the reference ``project_type`` + valid ``kind`` set,
- parsing a stored reference ``body`` back into its description / value
  (the inverse of the ``_format_reference_body`` formatters), and
- the delete sequence (SQLite row + FTS + Qdrant point across both
  collections).

Body shape (produced identically by the ``reference_store`` MCP tool and
the extraction-job formatter)::

    [reference.{kind}] {identifier}

    {description}

    Value: {value}
    Tags: a, b            # optional
    Captured: via=... ...  # optional

``parse_reference_body`` is the security boundary for the dashboard: the
list/search/detail views must render ONLY the parsed ``description`` (never
the raw body), and the reveal endpoint returns ONLY the parsed ``value``.
It fails CLOSED — if the canonical ``\\n\\nValue: `` marker is absent the
value can't be located, so both fields come back empty rather than risk
leaking an unparsed secret into a non-reveal payload.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import unquote

from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN as _CREDENTIAL_DOMAIN
from genesis.db.crud.knowledge import DETAIL_PLACEHOLDER as _CRUD_DETAIL_PLACEHOLDER
from genesis.db.crud.knowledge import SECRET_PLACEHOLDER as _CRUD_PLACEHOLDER
from genesis.db.crud.knowledge import (
    get_reference_pair,
    get_secret_row,
    holds_secret,  # noqa: F401 - one rule, shared with the db layer
)

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

# Single source of truth — the MCP tool module imports these back.
REFERENCE_PROJECT = "reference"


class ReferenceStoreUnavailable(RuntimeError):
    """The vector store was unavailable so the reference delete was DEFERRED and
    must be retried — distinct from "no such reference" (which returns ``False``).
    Callers should surface a retryable failure, not a 404/absent result."""
REFERENCE_KINDS = frozenset({
    "credentials",
    "url",
    "network",
    "persona_pointer",
    "account",
    "fact",
})

# The marker separating the description block from the value, as emitted by
# both formatters (a blank line followed by the ``Value: `` line).
_VALUE_MARKER = "\n\nValue: "
_TRAILER_PREFIXES = ("Tags: ", "Captured: ")


def parse_reference_body(body: str | None) -> dict[str, str]:
    """Split a stored reference body into its description and value.

    Returns ``{"description": str, "value": str}``. Fails CLOSED: if the
    canonical ``\\n\\nValue: `` marker is missing (a body shape this code
    didn't write), both fields are empty and a warning is logged — the
    caller must never fall back to emitting the raw body.

    Handles multi-line descriptions and multi-line values; the trailing
    ``Tags:`` / ``Captured:`` lines are stripped from the value.
    """
    if not body:
        return {"description": "", "value": ""}

    idx = body.find(_VALUE_MARKER)
    if idx == -1:
        logger.warning(
            "parse_reference_body: no canonical value marker found — "
            "failing closed (body len=%d)", len(body),
        )
        return {"description": "", "value": ""}

    # Everything before the marker is "[header]\n\n{description}".
    # Drop the header line (first line), keep the rest as description.
    pre = body[:idx]
    description = pre.split("\n", 1)[-1].strip() if "\n" in pre else ""

    # Everything after the marker is "{value}" plus an optional trailer of
    # Tags:/Captured: lines at the very end.
    after = body[idx + len(_VALUE_MARKER):]
    value_lines = after.split("\n")
    while value_lines and value_lines[-1].startswith(_TRAILER_PREFIXES):
        value_lines.pop()
    value = "\n".join(value_lines).strip()

    return {"description": description, "value": value}



# ─── Credential values and descriptions live outside the indexed body (#2426) ─
#
# A credential's VALUE is the one part of a reference no recall surface needs:
# lookup goes by identifier, and leaving the value out of the indexed text does
# not cost identifier recall on either the vector or the keyword path. Its
# free-text DESCRIPTION is held out WITH it: a description can repeat the value
# in any spelling or position, and no parser scrubs free text of it reliably
# (owner ruling, every review round found another way through). Every store
# that indexes a body (knowledge_units + knowledge_fts, memory_fts, the Qdrant
# payload and its embedding) therefore gets only the header (the identifier),
# DETAIL_PLACEHOLDER, the SECRET_PLACEHOLDER value line and the Tags/Captured
# trailer. The value and the description go to ONE ``reference_secrets`` row
# (``value``, ``detail``), a table no recall path reads. The other indexed
# fields (identifier, tags, authority, provenance) are not scrubbed: a write
# whose field carries the value is refused (``refuse_value_in_fields``).

#: Domains whose value is held out. Identifier kinds (url, network, account,
#: fact) are not: their value is the thing a lookup searches for.
SECRET_DOMAINS = frozenset({_CREDENTIAL_DOMAIN})
# The placeholders live with the db layer's fail-closed check (crud/knowledge.py).
SECRET_PLACEHOLDER = _CRUD_PLACEHOLDER
DETAIL_PLACEHOLDER = _CRUD_DETAIL_PLACEHOLDER

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_TOKEN = 4
_TOKEN_EDGE = ".,;:!?()[]{}\"'"  # noqa: S105 - punctuation, not a secret


def _secret_token(token: str) -> bool:
    """A whitespace token that reads as a secret rather than a word: at least
    four characters, letters or digits mixed with a digit or a symbol. Plain
    words ("password", "Router") and email addresses never qualify."""
    if len(token) < _MIN_TOKEN or _EMAIL.match(token):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_symbol = any(not c.isalnum() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return (has_digit or has_symbol) and (has_alpha or (has_digit and has_symbol))


def _normalise(text: str) -> str:
    """``text`` as the refusal check compares it: NFKC (a fullwidth or other
    compatibility variant reads as its plain form), every format character
    (Unicode category Cf: zero-width spaces and joiners, the soft hyphen)
    removed, then casefolded."""
    folded = unicodedata.normalize("NFKC", text)
    return "".join(c for c in folded if unicodedata.category(c) != "Cf").casefold()


def _decoded(text: str) -> set[str]:
    """``text`` and its percent-decoded forms, up to three rounds."""
    forms = {text}
    for _ in range(3):
        text = unquote(text)
        forms.add(text)
    return forms


#: Where a structured value (a URL, a DSN, a key=value line, a header, XML,
#: a pipe- or backslash-separated record) splits into its parts, applied to the
#: NFKC form so fullwidth punctuation splits too. One generic fragmentation, no
#: per-format parser.
_FRAGMENT_SPLIT = re.compile(r"[\s:/@?&=#;,|\\<>\"'`(){}\[\]]+")
#: A host name: dotted labels ending in an alphabetic top-level label.
_HOSTNAME = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")
_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]{0,31})://")


def _value_probes(value: str, *, login_exempt: bool = False) -> list[tuple[str, bool]]:
    """(probe, whole-word only) for the refusal check: the whole value, and
    every secret-shaped fragment of it, normalised. The value is percent-decoded
    (up to three rounds), NFKC-normalised and split on whitespace and
    ``: / @ ? & = # ; , | \\ < > " ' ` ( ) { } [ ]``, so
    the password, token or key inside a connection URL, a DSN or a
    ``key=value`` line is a fragment of its own; a fragment that is a host name,
    a URL scheme, or all digits (a port) is not probed. A secret that reads as
    a plain word (all letters, or under four characters) is not secret-shaped,
    so it is matched only as the whole value, and only as a whole word: an
    ordinary word inside another is no match. That is a stated limit.

    "login / secret" is the shape the capture writers format: with
    ``login_exempt`` (the identifier only, which names the account) the exact
    login half is not probed, unless it is part of the secret (equal to, or
    inside, the secret or one of its fragments) or is key-shaped
    (``login_is_key_shaped``). A fragment of the secret half is always probed,
    even when it spells the login. Every other field is probed with the
    fragments of both halves."""
    whole = value.strip()
    probes: dict[str, bool] = {}
    for form in _decoded(whole) if whole else ():
        probes.setdefault(_normalise(form), form.isalpha() or len(form) < _MIN_TOKEN)
    login, sep, secret = whole.partition(" / ")
    exempt = login.strip().strip(_TOKEN_EDGE) if login_exempt and sep and secret.strip() else None
    secret_fragments: set[str] = set()
    if exempt:
        secret_fragments = {f for f, _ in _fragments(secret.strip())} | {
            _normalise(form) for form in _decoded(secret.strip())
        }
        folded_login = _normalise(exempt)
        if login_is_key_shaped(exempt) or any(folded_login in f for f in secret_fragments):
            exempt = None
    for fragment, _folded in _fragments(whole):
        if fragment == exempt:
            continue
        if _secret_token(fragment):
            probes.setdefault(_normalise(fragment), False)
    probes.pop("", None)
    return list(probes.items())


def _fragments(value: str) -> list[tuple[str, str]]:
    """(fragment, normalised fragment) for every part of ``value`` the refusal
    check splits out (``_value_probes``), less host names, URL schemes and
    all-digit parts (ports)."""
    out: list[tuple[str, str]] = []
    for form in _decoded(value):
        schemes = {m.group(1).casefold() for m in _SCHEME.finditer(form)}
        for fragment in _FRAGMENT_SPLIT.split(unicodedata.normalize("NFKC", form)):
            fragment = fragment.strip(_TOKEN_EDGE)
            folded = fragment.casefold()
            if (
                not fragment or fragment.isdigit()
                or folded in schemes or _HOSTNAME.match(folded)
            ):
                continue
            out.append((fragment, _normalise(fragment)))
    return out


#: How long a secret-shaped login must be to read as a key rather than an
#: account name: the floor of the labelled-token capture (16 characters).
_KEY_SHAPED_LOGIN = 16


def login_is_key_shaped(login: str) -> bool:
    """True when a login half reads as a key or token, never as an account
    name: secret-shaped (``_secret_token``), at least sixteen characters, and
    carrying a digit ("ghp_" plus forty characters, an "sk-" key). Shorter
    secret-shaped logins ("admin7", "ForumUser42") are ordinary account names,
    which is why ``_secret_token`` alone is not the test."""
    login = login.strip().strip(_TOKEN_EDGE)
    return (
        len(login) >= _KEY_SHAPED_LOGIN
        and _secret_token(login)
        and any(c.isdigit() for c in login)
    )


def refuse_value_in_fields(
    value: str, fields: dict[str, str | None], *, message: str | None = None,
    login_exempt: bool = False,
) -> None:
    """Fail closed: raise ValueError when the value, or any secret-shaped token
    of it (in their normalised and percent-decoded forms), occurs in one of the
    indexed ``fields``. Nothing is scrubbed. The message names the field, never
    the value; ``message`` replaces it (the identifier refusal). The body's own
    placeholder lines are no leak and are left out of the check."""
    patterns = _probe_patterns(value, login_exempt=login_exempt)
    for name, text in fields.items():
        if _carries(patterns, text):
            raise ValueError(
                message or (
                    f"refusing a credential reference whose {name} carries its "
                    "value; keep the value out of that field (#2426)"
                )
            )


def _probe_patterns(value: str, *, login_exempt: bool = False) -> list[re.Pattern[str]]:
    return [
        re.compile(
            (r"(?<![^\W_])" + re.escape(probe) + r"(?![^\W_])") if as_word else re.escape(probe),
        )
        for probe, as_word in _value_probes(value, login_exempt=login_exempt)
    ]


def _carries(patterns: list[re.Pattern[str]], text: str | None) -> bool:
    """Whether ``text`` (normalised, and percent-decoded) carries a probe. The
    body placeholders are no leak and are left out."""
    if not text:
        return False
    held_out = text.replace(SECRET_PLACEHOLDER, "").replace(DETAIL_PLACEHOLDER, "")
    haystacks = {_normalise(form) for form in _decoded(held_out)}
    return any(pattern.search(h) for pattern in patterns for h in haystacks)


def drop_value_tags(value: str, tags_json: str | None, body: str) -> tuple[str | None, str]:
    """(tags_json, body) with every caller tag that carries the value removed
    WHOLE, from the tags list and from the body's ``Tags:`` trailer line; the
    other tags stay. A tag is a label, not prose: dropping the whole element
    leaves no part of it behind, so this is not a scrub. Tags are dropped
    rather than refused because a shape heuristic will flag an ordinary tag
    now and then (a database name, a region), and the cost of that should be
    one tag, never the whole capture. Unparseable tags are left as they are,
    for the strict check to refuse."""
    patterns = _probe_patterns(value)
    if tags_json:
        try:
            tags = json.loads(tags_json)
        except ValueError:
            tags = None
        if isinstance(tags, list):
            kept = [t for t in tags if not (isinstance(t, str) and _carries(patterns, t))]
            if len(kept) != len(tags):
                tags_json = json.dumps(kept)
    idx = body.find(_VALUE_MARKER)
    if idx != -1:
        trailer = _trailer(body[idx + len(_VALUE_MARKER):])
        rebuilt = []
        for line in trailer:
            if line.startswith("Tags: "):
                kept_tags = [t for t in line[len("Tags: "):].split(", ") if not _carries(patterns, t)]
                if kept_tags:
                    rebuilt.append("Tags: " + ", ".join(kept_tags))
            else:
                rebuilt.append(line)
        if rebuilt != trailer:
            after = body[idx + len(_VALUE_MARKER):]
            value_line = after[: len(after) - len("\n".join(trailer))].rstrip("\n")
            body = _held_body(body[:idx], rebuilt, value_line)
    return tags_json, body


class HeldCredential(NamedTuple):
    """What ``split_secret`` takes out of a credential body."""

    #: The indexed body: header, DETAIL_PLACEHOLDER, the value placeholder line, trailer.
    body: str
    #: The value, or None when the body carried the value placeholder (a re-ingest).
    value: str | None
    #: The description, or None when the body carried DETAIL_PLACEHOLDER.
    detail: str | None


def split_secret(body: str) -> HeldCredential:
    """Take the value and the free-text description out of a credential body.

    The body is ``[reference.credentials] {identifier}``, a blank line, the
    description, a blank line, ``Value: {value}`` and the Tags/Captured trailer,
    the layout every credential writer formats (the reference_store and
    knowledge_ingest MCP tools, the extraction job, migrate_reference_data).
    The returned body keeps the header line and the trailer, with
    DETAIL_PLACEHOLDER where the description stood and the canonical value
    placeholder line. A body that already carries a placeholder (a re-ingest of
    a held body) returns None for that part, so the stored one stays. Raises
    ValueError, before anything is written, when the value cannot be located or
    a payment card number is in the value or the description.
    """
    from genesis.security.card_shapes import contains_card_number

    idx = body.find(_VALUE_MARKER)
    value = parse_reference_body(body)["value"] if idx != -1 else ""
    if not value:
        raise ValueError(
            "a credential reference must carry its value on a 'Value:' line; "
            "refusing to index one whose value cannot be held out"
        )
    if body.count(_VALUE_MARKER) != 1:
        # A description carrying the marker would move its tail into the secret
        # (both this split and parse_reference_body cut at the FIRST marker).
        raise ValueError(
            "a credential reference must carry exactly one 'Value:' line; "
            "refusing a body whose description contains one too"
        )
    head = body[:idx]
    header, _, description = head.partition("\n")
    description = description.strip()
    if contains_card_number(value) or contains_card_number(description):
        raise ValueError(
            "a credential reference carries a payment card number; cards are "
            "never stored as references"
        )
    held = _held_body(
        header + "\n\n" + DETAIL_PLACEHOLDER, _trailer(body[idx + len(_VALUE_MARKER):]),
    )
    return HeldCredential(
        held,
        None if value == SECRET_PLACEHOLDER else value,
        None if description == DETAIL_PLACEHOLDER else description,
    )


def refuse_trailer_shaped_value(value: str) -> None:
    """A value line starting like a body trailer (``Tags: ``, ``Captured: ``)
    would be read back as trailer: the value truncated and its tail indexed.
    Writers refuse such a value before formatting the body (#2426)."""
    if any(line.startswith(_TRAILER_PREFIXES) for line in value.split("\n")[1:]):
        raise ValueError(
            "a credential value must not contain a line starting 'Tags: ' or "
            "'Captured: '; refusing the write (#2426)"
        )


def _trailer(after_marker: str) -> list[str]:
    """The trailing Tags:/Captured: lines of the text after the value marker."""
    lines = after_marker.split("\n")
    trailer: list[str] = []
    while lines and lines[-1].startswith(_TRAILER_PREFIXES):
        trailer.insert(0, lines.pop())
    prefixes = [next(p for p in _TRAILER_PREFIXES if line.startswith(p)) for line in trailer]
    if len(prefixes) != len(set(prefixes)):
        # Writers emit each trailer line once; a repeated one means part of a
        # multi-line value was read as trailer.
        raise ValueError(
            "a credential body repeats a 'Tags:' or 'Captured:' line; refusing "
            "a value that cannot be split safely (#2426)"
        )
    return trailer


def _held_body(head: str, trailer: list[str], value: str = SECRET_PLACEHOLDER) -> str:
    """The canonical body: the head, ONE value line, then the trailer."""
    body = head + _VALUE_MARKER + value
    return body + "\n" + "\n".join(trailer) if trailer else body


def _put_detail_back(head: str, detail: str | None) -> str:
    """``head`` (header and description block) with a held-out description in
    place of DETAIL_PLACEHOLDER; unchanged when it carries no placeholder (a
    row written before the description was held out) or nothing is stored."""
    header, sep, rest = head.partition("\n\n")
    if detail is None or rest.strip() != DETAIL_PLACEHOLDER:
        return head
    return header + sep + detail


async def _held_pair(
    db: aiosqlite.Connection, row: dict,
) -> tuple[str, str | None, str | None]:
    """(body, value, detail) of a held-out credential row, as ONE writer's set.

    The secret row the snapshot's ``secret_ref`` names is read first: secret
    rows are never updated, so its value and detail are the very ones written
    beside the snapshot's body. When the snapshot names no secret any more (the
    unit was repointed since it was read, retiring the secret it named) or
    carries no ``secret_ref`` at all, the unit's CURRENT body, value and detail
    are read by one statement (``get_reference_pair``); the current secret is
    never put beside the snapshot's stale body. The value is None when no secret
    is stored for the unit (a partial restore, a hand edit), or the unit is gone.
    """
    body = row.get("body") or ""
    if row.get("secret_ref"):
        secret = await get_secret_row(db, row["secret_ref"])
        if secret is not None:
            return body, secret[0], secret[1]
    pair = await get_reference_pair(db, row["id"])
    if pair is None:
        return body, None, None
    return pair[0] or "", pair[1], pair[2]


async def resolve_value(db: aiosqlite.Connection, row: dict) -> str | None:
    """The value of a reference row, or None when a held-out value is missing.

    A held-out credential's value comes from ``reference_secrets`` through
    ``_held_pair``, so it is the value of the same write as the body it pairs
    with; every other row (identifier kinds, and credentials written before
    #2426 that still carry their value inline, with no secret_ref) is parsed
    from the body as before, an empty value included. None means a held-out
    credential names no stored secret, which only a partial restore or a hand
    edit can produce: logged as an error, never guessed at.
    """
    return (await reveal_reference(db, row))[0]


async def reveal_reference(db: aiosqlite.Connection, row: dict) -> tuple[str | None, str]:
    """(value, description) of a reference row: for a held-out credential both
    come from one ``reference_secrets`` row (``_held_pair``); every other row
    parses both from its body. The value is None when a held-out value is
    missing (logged as an error)."""
    parsed = parse_reference_body(row.get("body"))
    if parsed["value"] != SECRET_PLACEHOLDER or not holds_secret(
        row.get("project_type"), row.get("domain"),
    ):
        return parsed["value"], parsed["description"]
    body, secret, detail = await _held_pair(db, row)
    current = parse_reference_body(body)
    if secret is None:
        if current["value"] and current["value"] != SECRET_PLACEHOLDER:
            # The unit's current body carries its value inline (a row written
            # before #2426): that body's own value, never a guess.
            return current["value"], current["description"]
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return None, current["description"]
    description = current["description"]
    if detail is not None and description == DETAIL_PLACEHOLDER:
        description = detail
    return secret, description


async def compose_body(db: aiosqlite.Connection, row: dict) -> tuple[str, bool]:
    """(the row's body with its held-out description and value put back,
    whether the value was missing).

    For ``reference_lookup``, the one reader that returns a whole body. The
    placeholder is recognised by the value it parses to (the rule
    ``resolve_value`` and ``split_secret`` use), not by one exact spelling of
    its line, and the value goes back on a canonical value line. Body, value and
    description are one write's set (``_held_pair``): when the unit was
    repointed after ``row`` was read, the CURRENT body comes back with them.
    """
    body = row.get("body") or ""
    idx = body.find(_VALUE_MARKER)
    if (
        idx == -1
        or not holds_secret(row.get("project_type"), row.get("domain"))
        or parse_reference_body(body)["value"] != SECRET_PLACEHOLDER
    ):
        return body, False
    body, value, detail = await _held_pair(db, row)
    idx = body.find(_VALUE_MARKER)
    if idx == -1 or parse_reference_body(body)["value"] != SECRET_PLACEHOLDER:
        # The current body is not a held-out one (no marker, or a value inline):
        # it is returned as stored, its own pair.
        return body, False
    if value is None:
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return body, True
    head = _put_detail_back(body[:idx], detail)
    return _held_body(head, _trailer(body[idx + len(_VALUE_MARKER):]), value), False


async def delete_reference_entry(
    db: aiosqlite.Connection,
    store: Any | None,
    unit_id: str,
) -> bool:
    """Delete a reference entry: SQLite row + FTS row + Qdrant point.

    Mirrors the original ``reference_delete`` MCP sequence so both the tool
    and the dashboard route share one delete path. ``store`` is a
    ``MemoryStore`` (its ``.delete()`` tries both Qdrant collections and
    cascades metadata/links/pending); pass ``None`` for a SQLite-only
    fallback when no store is available.

    Refuses to delete non-reference rows (guards ``project_type``) so this
    can't be used as a generic knowledge-unit delete. Returns ``True`` if a
    row was deleted, ``False`` if no row existed with that id.
    """
    from genesis.db.crud import knowledge as knowledge_crud

    row = await knowledge_crud.get(db, unit_id)
    if row is None:
        return False
    if row.get("project_type") != REFERENCE_PROJECT:
        raise ValueError(
            f"delete_reference_entry: unit {unit_id} is not a reference entry "
            f"(project_type={row.get('project_type')!r})"
        )

    qdrant_id = row.get("qdrant_id")
    # store.store dedups by exact content, so two non-credential units can share
    # one point: never delete a point another unit still names. For a credential
    # the check is reliable: its writes never reuse a point (dedup=False in
    # ingest_knowledge_unit), so no other unit can name it. Two concurrent
    # deletes of the last owners of a SHARED (non-credential, or pre-change)
    # point can still both skip the delete.
    shared = bool(qdrant_id) and await knowledge_crud.qdrant_point_named_elsewhere(
        db, qdrant_id, exclude_id=unit_id,
    )
    if qdrant_id and store is not None and not shared:
        try:
            res = await store.delete(qdrant_id)
        except ReferenceStoreUnavailable:
            raise
        except Exception as exc:
            logger.error(
                "delete_reference_entry: Qdrant cleanup failed for unit %s "
                "(qdrant_id=%s) — deferring, leaving unit for retry",
                unit_id, qdrant_id, exc_info=True,
            )
            raise ReferenceStoreUnavailable(
                f"reference delete failed for unit {unit_id} (qdrant_id={qdrant_id})"
            ) from exc
        if res.get("deferred"):
            # Vector store unavailable → store.delete kept the point + metadata
            # intact. Do NOT drop the knowledge_units row, or we would orphan it
            # from its record. Signal a RETRYABLE failure (not "not found") so the
            # caller can report it correctly and retry the whole delete later.
            raise ReferenceStoreUnavailable(
                f"reference delete deferred for unit {unit_id} "
                f"(qdrant_id={qdrant_id}): vector store unavailable"
            )

    deleted = await knowledge_crud.delete(db, unit_id)
    logger.info("Reference entry %s deleted: %s", unit_id, deleted)
    return deleted
