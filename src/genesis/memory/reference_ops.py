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
from typing import TYPE_CHECKING, Any, NamedTuple

from genesis.db.crud.knowledge import CREDENTIAL_DOMAIN as _CREDENTIAL_DOMAIN
from genesis.db.crud.knowledge import DETAIL_PLACEHOLDER as _CRUD_DETAIL_PLACEHOLDER
from genesis.db.crud.knowledge import SECRET_PLACEHOLDER as _CRUD_PLACEHOLDER
from genesis.db.crud.knowledge import (
    get_reference_pair,
    get_secret_row,
    holds_secret,  # noqa: F401 - one rule, shared with the db layer
)

# The masking itself (``ValueMask``, ``value_masks``, the caps, ``HELD_MASK``)
# lives in ``reference_masks``; its public names are re-exported here.
from genesis.memory.reference_masks import (
    HELD_MASK,
    MAX_TAG_CHARS,  # noqa: F401 - re-exported
    MAX_TAGS,
    ValueMask,
    _as_mask,
    _mask_tag,
    _serialised_clean,
    cap_masked,  # noqa: F401 - re-exported
    cap_tag,
    mask_value_spans,  # noqa: F401 - re-exported
    value_masks,  # noqa: F401 - re-exported
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
# (``value``, ``detail``), a table no recall path reads. In the other indexed
# fields (identifier, tags, authority, provenance) every span of the value is
# MASKED with ``HELD_MASK`` (``mask_value_spans``) and the write goes on: no
# capture is refused or degraded to keep a secret out of the index (owner
# ruling, #2426), since the original is already held in reference_secrets.

#: Domains whose value is held out. Identifier kinds (url, network, account,
#: fact) are not: their value is the thing a lookup searches for.
SECRET_DOMAINS = frozenset({_CREDENTIAL_DOMAIN})
# The placeholders live with the db layer's fail-closed check (crud/knowledge.py).
SECRET_PLACEHOLDER = _CRUD_PLACEHOLDER
DETAIL_PLACEHOLDER = _CRUD_DETAIL_PLACEHOLDER


def _trailer_tag(tag: str) -> str:
    """A tag as the body's one ``Tags:`` line can carry it: a line break in
    it would start a line of its own (a second ``Value:`` line, say)."""
    return " ".join(tag.splitlines())


def mask_value_tags(
    value: str | ValueMask, tags_json: str | None, body: str,
) -> tuple[str | None, str]:
    """(tags_json, body) for a credential row: at most ``MAX_TAGS`` tags, each
    masked whole and then capped (``cap_tag``), with every span of the value masked strictly
    (strings, numbers, lists and dicts at any nesting), in the tags list and in
    the body's ``Tags:`` trailer line, which is also checked whole (a value
    holding ", " spans tags no piece of which matches alone). A tag that was
    nothing but the value is dropped. A ``Captured:`` line is dropped: the
    caller's provenance is held in the secret row's meta. Tags that are not a
    JSON list are decoded and masked as one value."""
    mask = _as_mask(value)
    if tags_json:
        try:
            tags = json.loads(tags_json)
        except ValueError:
            tags = tags_json
        if not isinstance(tags, list):
            tags = [tags]
        kept = [cap_tag(t) for t in (_mask_tag(mask, t) for t in tags[:MAX_TAGS]) if t is not None]
        tags_json = _serialised_clean(mask, kept)
    idx = body.find(_VALUE_MARKER)
    if idx != -1:
        after = body[idx + len(_VALUE_MARKER):]
        trailer = _trailer(after)
        rebuilt = []
        for line in trailer:
            if not line.startswith("Tags: "):
                continue
            pieces = line[len("Tags: "):].split(", ")[:MAX_TAGS]
            joined = ", ".join(t for t in (_mask_tag(mask, t) for t in pieces) if t is not None)
            if mask.carries(joined):
                joined = mask.mask(joined)
            # Capped only once masked, so a cut never leaves part of a secret.
            joined = ", ".join(cap_tag(t) for t in joined.split(", "))
            if mask.carries("Tags: " + joined):
                # The line as stored, prefix and all, is checked once more.
                line = mask.mask("Tags: " + joined)
                joined = line[len("Tags: "):] if line.startswith("Tags: ") else HELD_MASK
            if joined.replace(HELD_MASK, "").strip(", "):
                rebuilt.append("Tags: " + joined)
        if rebuilt != trailer:
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


def split_secret(
    body: str,
    *,
    header: str | None = None,
    tags: list[str] | None = None,
    value: str | None = None,
    detail: str | None = None,
) -> HeldCredential:
    """Take the value and the free-text description out of a credential body.

    The body is ``[reference.credentials] {identifier}``, a blank line, the
    description, a blank line, ``Value: {value}`` and the Tags/Captured trailer,
    the layout every credential writer formats (the reference_store and
    knowledge_ingest MCP tools, the extraction job, migrate_reference_data).
    ``header`` is the header line expected for the key (default: the body's own
    first line); a first line that is not it is part of the description. The
    returned body is that header, DETAIL_PLACEHOLDER where the description
    stood, the canonical value placeholder line and the trailer. A body that
    already carries a placeholder (a re-ingest of a held body) returns None for
    that part, so the stored one stays. ``value`` and ``detail``, when a writer
    passes them, are the value and description as it knows them (a multi-line
    value is held out whole, whatever its lines look like).

    A body whose split point is ambiguous (no ``Value:`` line, the marker twice,
    a value line shaped like a trailer line, a repeated trailer line, or a
    parsed value that is not the writer's) is never refused: MORE is held out.
    Everything after the header goes to the secret (the value: the text after
    the first marker, or the whole remainder when there is none), the
    description before the first marker stays the detail (None when there is
    no marker to separate it, so a stored one is kept), and the trailer is
    rebuilt only from the structured ``tags``, so no part of the ambiguous text
    is indexed. Raises ValueError, before anything is written, only when a
    payment card number is in the value or the description (#3172).
    """
    from genesis.security.card_shapes import contains_card_number

    first = body.partition("\n")[0]
    header = first if header is None else header
    rest = body[len(first):] if first == header else "\n" + body
    idx = rest.find(_VALUE_MARKER)
    clean = idx != -1 and rest.count(_VALUE_MARKER) == 1
    if clean:
        after = rest[idx + len(_VALUE_MARKER):]
        if value is not None and value.strip() and after.startswith(value):
            # The writer's value says exactly where the value line ends: what
            # follows it is the trailer, whatever the value's lines look like.
            tail = after[len(value):]
            trailer = tail.strip("\n").split("\n") if tail.strip() else []
            clean = tail[:1] in ("", "\n") and all(
                line.startswith(_TRAILER_PREFIXES) for line in trailer
            ) and _split_trailer("\n".join(trailer))[1]
            held = value
        else:
            trailer, clean = _split_trailer(after)
            held = parse_reference_body(rest)["value"]
            value_lines = held.split("\n")[1:]
            clean = clean and not any(
                line.startswith(_TRAILER_PREFIXES) for line in value_lines
            ) and (value is None or held == value.strip())
    if clean:
        description: str | None = rest[:idx].strip()
    else:
        # Hold out more: the remainder is the value, nothing of it indexed.
        held = (rest[idx + len(_VALUE_MARKER):] if idx != -1 else rest).strip()
        description = rest[:idx].strip() if idx != -1 else None
        trailer = ["Tags: " + ", ".join(_trailer_tag(t) for t in tags)] if tags else []
    if value is not None:
        # The writer's value, exactly as given (a whitespace-only one is empty).
        held = value if value.strip() else ""
    if detail is not None:
        description = detail.strip()
    if contains_card_number(held) or contains_card_number(description or ""):
        raise ValueError(
            "a credential reference carries a payment card number; cards are "
            "never stored as references"
        )
    return HeldCredential(
        _held_body(header + "\n\n" + DETAIL_PLACEHOLDER, trailer),
        # An empty value is an empty value, held like any other; only the
        # placeholder means "keep the stored one" (#2426 PR A).
        None if held == SECRET_PLACEHOLDER else held,
        None if description == DETAIL_PLACEHOLDER else description,
    )


def _split_trailer(after_marker: str) -> tuple[list[str], bool]:
    """(the trailing Tags:/Captured: lines of the text after the value marker,
    whether they are unambiguous). Writers emit each trailer line once; a
    repeated one means part of a multi-line value was read as trailer."""
    lines = after_marker.split("\n")
    trailer: list[str] = []
    while lines and lines[-1].startswith(_TRAILER_PREFIXES):
        trailer.insert(0, lines.pop())
    prefixes = [next(p for p in _TRAILER_PREFIXES if line.startswith(p)) for line in trailer]
    return trailer, len(prefixes) == len(set(prefixes))


def _trailer(after_marker: str) -> list[str]:
    """The trailing Tags:/Captured: lines of the text after the value marker."""
    return _split_trailer(after_marker)[0]


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


def _credential(row: dict) -> bool:
    return holds_secret(row.get("project_type"), row.get("domain"))


def _held_row(row: dict, parsed_value: str) -> bool:
    """Whether the row's value (and description) live in reference_secrets: a
    credential whose body carries the placeholder, or any other reference row
    written with a held-out value (``ingest_knowledge_unit(held_value=...)``),
    which names it by ``secret_ref``; its body's value line is masked."""
    if _credential(row):
        return parsed_value == SECRET_PLACEHOLDER
    return bool(row.get("secret_ref"))


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
    if not _held_row(row, parsed["value"]):
        return parsed["value"], parsed["description"]
    body, secret, detail = await _held_pair(db, row)
    current = parse_reference_body(body)
    if secret is None:
        if _credential(row) and current["value"] and current["value"] != SECRET_PLACEHOLDER:
            # The unit's current body carries its value inline (a row written
            # before #2426): that body's own value, never a guess.
            return current["value"], current["description"]
        logger.error("reference %s holds its value out, but no secret is stored for it", row["id"])
        return None, current["description"]
    description = current["description"]
    if detail is not None and description == DETAIL_PLACEHOLDER:
        description = detail
    return secret, description


async def reveal_meta(db: aiosqlite.Connection, row: dict) -> dict | None:
    """The caller metadata held with a credential's value (#2426): the
    authority and provenance it was written with, purpose, ingestion source,
    relationships, caveats, and the identifier and tags as given. Read from the
    secret row the row names; None when none is held."""
    from genesis.db.crud.knowledge import IDENTITY_META, get_secret_meta

    text = await get_secret_meta(db, row.get("secret_ref"))
    try:
        meta = json.loads(text) if text else None
    except ValueError:
        return None
    if isinstance(meta, dict):
        # What the write recorded of the entry's identity is not the caller's.
        meta.pop(IDENTITY_META, None)
    return meta


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
    if idx == -1 or not _held_row(row, parse_reference_body(body)["value"]):
        return body, False
    body, value, detail = await _held_pair(db, row)
    idx = body.find(_VALUE_MARKER)
    if idx == -1 or (
        _credential(row) and parse_reference_body(body)["value"] != SECRET_PLACEHOLDER
    ):
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
