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
from typing import TYPE_CHECKING, Any

from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN as _CREDENTIAL_DOMAIN
from genesis.db.crud.knowledge import SECRET_PLACEHOLDER as _CRUD_PLACEHOLDER
from genesis.db.crud.knowledge import (
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


# ─── Credential values live outside the indexed body (#2426) ──────────────────
#
# A credential's VALUE is the one part of a reference no recall surface needs:
# lookup goes by identifier and description (MEASURED 2026-11-04 on a live
# install: with the value dropped, top-1 identifier recall IMPROVED on both the
# vector and the keyword path). Every store that indexes a body
# (knowledge_units + knowledge_fts, memory_fts, the Qdrant payload and its
# embedding) therefore gets the body with the value replaced by
# SECRET_PLACEHOLDER, and the value goes to ``reference_secrets``, a table no
# recall path reads. ``resolve_value`` is its only reader.

#: Domains whose value is held out. Identifier kinds (url, network, account,
#: fact) are not: their value is the thing a lookup searches for.
SECRET_DOMAINS = frozenset({_CREDENTIAL_DOMAIN})
# The placeholder lives with the db layer's fail-closed check (crud/knowledge.py).
SECRET_PLACEHOLDER = _CRUD_PLACEHOLDER
#: What a fragment of the value reads as wherever else the body repeated it.
REDACTED = "[redacted]"

# A labelled segment inside a credential value ("user: x") whose label names an
# IDENTIFIER stays searchable; every other segment counts as secret.
_IDENTIFIER_LABELS = frozenset({
    "user", "username", "login", "email", "account", "userprofile", "host", "url",
})
_SEGMENT_SPLIT = re.compile(r"[,;]\s*|\s+/\s+|\s+\|\s+")
_LABELLED = re.compile(r"^([A-Za-z _-]{1,20})[:=]\s*(.+)$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Shorter fragments are left alone: an unlabelled 5-character token is as often
# an ordinary word ("admin") as a secret, and scrubbing it would redact prose.
_MIN_LABELLED = 4
_MIN_UNLABELLED = 6


def _secret_token(token: str) -> bool:
    """A whitespace token that reads as a secret rather than a word: at least
    four characters, letters or digits mixed with a digit or a symbol. Plain
    words ("password", "Router") never qualify, so prose is not redacted."""
    if len(token) < _MIN_LABELLED or _EMAIL.match(token):
        return False
    has_digit = any(c.isdigit() for c in token)
    has_symbol = any(not c.isalnum() for c in token)
    has_alpha = any(c.isalpha() for c in token)
    return (has_digit or has_symbol) and (has_alpha or (has_digit and has_symbol))


def secret_fragments(value: str) -> list[str]:
    """The parts of ``value`` that must appear nowhere else in the body, longest
    first: the whole value, each segment that is not an identifier, and every
    secret-looking token inside them.

    MEASURED (a live install, 2026-11-04): over 4 in 10 credential bodies
    repeated a segment of their value in the description or tags, most often
    the one labelled ``password``; none repeated the whole value. Holding out only the
    ``Value:`` line would have left them indexed. Tokens count too, because a
    prose value ("log in as x with password y") is one segment whose repeat in
    the description is usually just the password.
    """
    return _fragments(value)[0]


def _fragments(value: str) -> tuple[list[str], frozenset[str]]:
    """(every fragment, longest first; the ones that are secret BY LABEL or by
    position and so are redacted even glued to other text)."""
    whole = value.strip()
    fragments = {whole} if whole else set()
    glued: set[str] = set()
    # "login / secret" (a common shape on a live install): by convention the
    # first part is the login, an identifier the description legitimately
    # names, and the second part is the secret, however short.
    pair = [part.strip() for part in whole.split(" / ")] if whole.count(" / ") == 1 else []
    login = None
    if len(pair) == 2 and not any(_LABELLED.match(p) for p in pair):
        login = pair[0]
        if len(pair[1]) >= 2:
            fragments.add(pair[1])
            glued.add(pair[1])
    held: list[tuple[str, bool]] = []  # (part, came from a secret label)
    for line in value.splitlines():
        for segment in _SEGMENT_SPLIT.split(line.strip()):
            segment = segment.strip()
            if not segment or segment == login:
                continue
            match = _LABELLED.match(segment)
            if match:
                label, rhs = match.group(1).strip().lower(), match.group(2).strip()
                if label in _IDENTIFIER_LABELS or _EMAIL.match(rhs) or len(rhs) < _MIN_LABELLED:
                    continue
                held.append((rhs, True))
            elif not _EMAIL.match(segment):
                held.append((segment, False))
    for part, labelled in held:
        # A labelled secret is held whatever its shape; a short unlabelled
        # segment ("admin") only through its secret-looking tokens.
        if labelled or len(part) >= _MIN_UNLABELLED or _secret_token(part):
            fragments.add(part)
        if labelled:
            # A labelled multi-word secret ("password: blue moon river") is
            # secret word by word: a description repeating "blue moon" repeats it.
            words = {w for w in part.split() if len(w) >= _MIN_LABELLED}
            fragments.update(words)
            glued.update(words | {part})
        for token in part.split():
            token = token.strip(".,;:!?()[]{}\"'")
            if token != login and _secret_token(token):
                fragments.add(token)
            # "password=x token=y" on one line: the second pair is a token of
            # the first's value, and its own value is just as secret.
            inner = _LABELLED.match(token)
            if inner and inner.group(1).strip().lower() not in _IDENTIFIER_LABELS:
                rhs = inner.group(2).strip()
                if len(rhs) >= _MIN_LABELLED and not _EMAIL.match(rhs):
                    fragments.add(rhs)
    fragments.discard("")
    return sorted(fragments, key=len, reverse=True), frozenset(glued - {""})


def scrub(text: str, fragments: list[str], glued: frozenset[str] = frozenset()) -> str:
    """``text`` with every fragment replaced by REDACTED, longest first.

    A fragment carrying a digit or a symbol, or one in ``glued`` (secret by its
    label, such as an all-letter password), is replaced wherever it occurs, even
    glued to other text ("prefixHunter2!xyz"). Any other purely alphabetic
    fragment is replaced only where it stands alone, because inside a longer word
    ("active" in "inactive") it is that word.
    """
    for fragment in fragments:
        if fragment.isalpha() and fragment not in glued:
            text = re.sub(
                r"(?<![A-Za-z0-9])" + re.escape(fragment) + r"(?![A-Za-z0-9])", REDACTED, text,
            )
        else:
            text = text.replace(fragment, REDACTED)
    return text


def _scrub_tag(tag: str, fragments: list[str]) -> str:
    # A tag is a label, not prose: any occurrence goes, even inside a longer
    # word (MEASURED: one live credential's tag carries its password that way),
    # since over-redacting a tag costs nothing a lookup needs.
    for fragment in fragments:
        tag = tag.replace(fragment, REDACTED)
    return tag


def _scrub_tags(tags_json: str | None, fragments: list[str]) -> str | None:
    if not tags_json:
        return tags_json
    try:
        tags = json.loads(tags_json)
    except ValueError:
        return _scrub_tag(tags_json, fragments)
    if not isinstance(tags, list):
        return _scrub_tag(tags_json, fragments)
    return json.dumps([_scrub_tag(t, fragments) if isinstance(t, str) else t for t in tags])


def split_secret(
    body: str, tags_json: str | None = None, concept: str | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    """(body with the value held out, the value or None, tags scrubbed, concept
    scrubbed).

    The concept is the unit's identifier, indexed in knowledge_fts beside the
    body; an extraction with no entities derives it from its own text, which can
    carry the password, so it is scrubbed with the same fragments.

    The value line becomes ``Value: SECRET_PLACEHOLDER``, and every fragment of
    the value is redacted from the rest of the body and from the tags. A body
    whose value already IS the placeholder (a re-ingest of a held-out body)
    returns None for the value: there is nothing new to hold, and the secret
    already stored stays. Raises ValueError, before anything is written, when the
    value cannot be located or carries a payment card number: a credential whose
    value cannot be found cannot be kept out of the index.
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
    if value == SECRET_PLACEHOLDER:
        return body, None, tags_json, concept
    if contains_card_number(value):
        raise ValueError(
            "a credential reference carries a payment card number; cards are "
            "never stored as references"
        )
    fragments, glued = _fragments(value)
    lines = body[idx + len(_VALUE_MARKER):].split("\n")
    trailer: list[str] = []
    while lines and lines[-1].startswith(_TRAILER_PREFIXES):
        trailer.insert(0, lines.pop())
    held = scrub(body[:idx], fragments, glued) + _VALUE_MARKER + SECRET_PLACEHOLDER
    if trailer:
        held += "\n" + "\n".join(
            _scrub_tag(line, fragments) if line.startswith("Tags: ") else scrub(line, fragments, glued)
            for line in trailer
        )
    return (
        held, value, _scrub_tags(tags_json, fragments),
        scrub(concept, fragments, glued) if concept is not None else None,
    )


async def resolve_value(db: aiosqlite.Connection, row: dict) -> str | None:
    """The value of a reference row, or None when it has none to give.

    A held-out credential's value comes from ``reference_secrets``; every other
    row (identifier kinds, and credentials written before #2426 that still
    carry their value inline) is parsed from the body as before. None for a
    held-out credential means its secret row is missing, which only a partial
    restore or a hand edit can produce: logged as an error, never guessed at.
    """
    value = parse_reference_body(row.get("body"))["value"]
    if value != SECRET_PLACEHOLDER or not holds_secret(row.get("project_type"), row.get("domain")):
        return value or None
    rows = await db.execute_fetchall(
        "SELECT value FROM reference_secrets WHERE unit_id = ?", (row["id"],),
    )
    if not rows:
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return None
    return rows[0][0]


async def compose_body(db: aiosqlite.Connection, row: dict) -> tuple[str, bool]:
    """(the row's body with a held-out value put back, whether it was missing).

    For ``reference_lookup``, the one reader that returns a whole body. The
    description stays as stored, so a fragment redacted there stays redacted.
    """
    body = row.get("body") or ""
    placeholder_line = _VALUE_MARKER + SECRET_PLACEHOLDER
    if not holds_secret(row.get("project_type"), row.get("domain")) or placeholder_line not in body:
        return body, False
    value = await resolve_value(db, row)
    if value is None:
        return body, True
    return body.replace(placeholder_line, _VALUE_MARKER + value, 1), False


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
    if qdrant_id and store is not None:
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
