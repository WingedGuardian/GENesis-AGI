"""CRUD operations for knowledge_units table + knowledge_fts index."""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

from genesis.db.crud._fts import fetch_fts

# #2426: a credential reference's value is held out of knowledge_units.body
# (memory/reference_ops.split_secret, run by ingest_knowledge_unit). These are
# the shared contract; reference_ops imports them from here so the db layer
# never imports memory/.
CREDENTIAL_DOMAIN = "reference.credentials"
SECRET_PLACEHOLDER = "[held in the reference secret store; read it with reference_lookup]"  # noqa: S105 - a marker, not a secret


_VALUE_MARKER = "\n\nValue: "
_TRAILER_PREFIXES = ("Tags: ", "Captured: ")


def holds_secret(project_type: str | None, domain: str | None) -> bool:
    """True for a reference whose value is held out of the indexed body."""
    return (
        (project_type or "").strip().lower() == "reference"
        and (domain or "").strip().lower() == CREDENTIAL_DOMAIN
    )


def _canonical_key(project_type: str, domain: str) -> tuple[str, str]:
    """A credential row's project and domain in their one stored spelling, so
    every reader that compares them exactly (the lookup, delete, the audit log)
    sees the row the case-insensitive ``holds_secret`` rule saw."""
    if holds_secret(project_type, domain):
        return "reference", CREDENTIAL_DOMAIN
    return project_type, domain


class CredentialKeyTaken(ValueError):  # noqa: N818 - names the state, not an error kind
    """An insert-only credential write found its key holding a different value.

    Nothing was written. The caller picks another key and tries again
    (for auto-capture, which a later change wires to it: a rotated password
    would add an entry, never replace one)."""


class CredentialKeyChanged(ValueError):  # noqa: N818 - names the state, not an error kind
    """A credential write found its key no longer in the state it observed
    (``KeyObservation``): another writer, in this process or another, changed
    the key in between. Nothing was written; the caller re-reads and retries."""


@dataclass(frozen=True)
class KeyObservation:
    """What a credential write read of its key before it stored its vector:
    the row's id, the secret and the point it named, and that secret's value.
    ``unit_id`` None means the key was absent. ``upsert(expected=...)`` applies
    the write only while the key still matches this (compare-and-swap)."""

    unit_id: str | None
    secret_ref: str | None
    qdrant_id: str | None
    value: str | None

    @property
    def absent(self) -> bool:
        return self.unit_id is None


async def _credential_row(
    db: aiosqlite.Connection, project_type: str, domain: str, concept: str,
) -> tuple[str, str | None, str | None, str | None] | None:
    """(id, secret_ref, the value it names or None, qdrant_id) of the row
    holding the key."""
    rows = await db.execute_fetchall(
        "SELECT k.id, k.secret_ref, s.value, k.qdrant_id FROM knowledge_units k "
        "LEFT JOIN reference_secrets s ON s.id = k.secret_ref "
        "WHERE k.project_type = ? AND k.domain = ? AND k.concept = ?",
        (project_type, domain, concept),
    )
    return (rows[0][0], rows[0][1], rows[0][2], rows[0][3]) if rows else None


async def observe_credential_key(
    db: aiosqlite.Connection, *, project_type: str, domain: str, concept: str,
) -> KeyObservation | None:
    """The key's current state, for a credential write to compare-and-swap
    against; None for a row that is not a credential reference."""
    if not holds_secret(project_type, domain):
        return None
    project_type, domain = _canonical_key(project_type, domain)
    row = await _credential_row(db, project_type, domain, concept)
    if row is None:
        return KeyObservation(None, None, None, None)
    return KeyObservation(row[0], row[1], row[3], row[2])


def _still_observed(row: tuple | None, expected: KeyObservation) -> bool:
    if expected.absent:
        return row is None
    return row is not None and row[0] == expected.unit_id and row[1] == expected.secret_ref and (
        row[3] == expected.qdrant_id
    )


async def check_credential_write(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    concept: str,
    secret_value: str | None,
    insert_only: bool = False,
    observed: KeyObservation | None = None,
) -> str | None:
    """Raise ValueError when a credential row could not commit with its value;
    otherwise return the id of the row already holding this key, or None.

    ``ingest_knowledge_unit`` runs this BEFORE it stores the vector, and
    ``upsert`` repeats the same reads before its first write, so a refused
    credential leaves nothing behind in any store. The statement touches
    ``reference_secrets``, so a database whose schema init has not created the
    table fails here too. With ``insert_only``, a key already holding a
    different value raises ``CredentialKeyTaken``; a returned id then means the
    key already holds this very value. A no-op (None) for every other row.
    ``observed`` (the caller's own read of the key) is used instead of reading
    it again, so the check and the compare-and-swap agree on one read.
    """
    if not holds_secret(project_type, domain):
        return None
    project_type, domain = _canonical_key(project_type, domain)
    if observed is not None:
        existing = None if observed.absent else (
            observed.unit_id, observed.secret_ref, observed.value, observed.qdrant_id,
        )
    else:
        existing = await _credential_row(db, project_type, domain, concept)
    if secret_value is None and not (existing and existing[2] is not None):
        raise ValueError(
            "refusing a credential reference with no value to hold: its body "
            "carries the placeholder and no secret is stored for it (#2426)"
        )
    if insert_only and existing and secret_value is not None and existing[2] != secret_value:
        raise CredentialKeyTaken(
            "this credential key already holds a different value (#2426)"
        )
    return existing[0] if existing else None


async def get_reference_pair(
    db: aiosqlite.Connection, unit_id: str,
) -> tuple[str, str | None] | None:
    """(body, the held-out value it names or None) of the unit ``unit_id``, read
    by ONE statement, so the two are always one writer's pair; None when no
    such unit exists. The value is None for a unit naming no secret (a row
    written before #2426, or any non-credential row)."""
    rows = await db.execute_fetchall(
        "SELECT k.body, s.value FROM knowledge_units k "
        "LEFT JOIN reference_secrets s ON s.id = k.secret_ref WHERE k.id = ?",
        (unit_id,),
    )
    return (rows[0][0], rows[0][1]) if rows else None


async def get_secret(db: aiosqlite.Connection, secret_ref: str | None) -> str | None:
    """The value of the reference_secrets row ``secret_ref``, or None."""
    if not secret_ref:
        return None
    rows = await db.execute_fetchall(
        "SELECT value FROM reference_secrets WHERE id = ?", (secret_ref,),
    )
    return rows[0][0] if rows else None


async def get_reference_secret(db: aiosqlite.Connection, unit_id: str) -> str | None:
    """The held-out value the unit ``unit_id`` names through its secret_ref, or
    None when it names none (a row written before #2426, or no such unit)."""
    rows = await db.execute_fetchall(
        "SELECT s.value FROM knowledge_units k JOIN reference_secrets s "
        "ON s.id = k.secret_ref WHERE k.id = ?",
        (unit_id,),
    )
    return rows[0][0] if rows else None


async def sweep_orphan_secrets(
    db: aiosqlite.Connection, older_than_s: float = 3600, *, now: datetime | None = None,
) -> int:
    """Delete the reference_secrets rows no unit names, older than the bound.

    An orphan is what an upsert leaves when it stops between its secret insert
    and its unit write (a failure or a cancellation): unreferenced, never
    mispaired. The age bound keeps a secret whose unit write is still on its way;
    if the sweep took one anyway, that unit write would be refused by the
    secret_ref trigger, so a caller sees a failure, never a unit without its
    secret. Does not commit. Returns the number of rows deleted.
    """
    cutoff = ((now or datetime.now(UTC)) - timedelta(seconds=older_than_s)).isoformat()
    cursor = await db.execute(
        "DELETE FROM reference_secrets WHERE created_at < ? AND NOT EXISTS "
        "(SELECT 1 FROM knowledge_units k WHERE k.secret_ref = reference_secrets.id)",
        (cutoff,),
    )
    return cursor.rowcount or 0


async def qdrant_point_named_elsewhere(
    db: aiosqlite.Connection, qdrant_id: str, *, exclude_id: str,
) -> bool:
    """True when a unit other than ``exclude_id`` still names ``qdrant_id``."""
    rows = await db.execute_fetchall(
        "SELECT 1 FROM knowledge_units WHERE qdrant_id = ? AND id != ? LIMIT 1",
        (qdrant_id, exclude_id),
    )
    return bool(rows)


def refuse_inline_secret(project_type: str, domain: str, body: str) -> None:
    """Fail closed on a credential reference whose body could carry its value.

    Every writer of knowledge_units passes here (insert and upsert), so a path
    that bypasses ingest_knowledge_unit (the orchestrator, an upload, a script)
    cannot put a credential value into the indexed body. Accepted only: exactly
    one ``Value:`` marker, followed by the placeholder exactly (the canonical
    line ``split_secret`` writes), then nothing but blank lines and the trailing
    Tags/Captured lines, so neither a second Value line nor text after the
    placeholder can carry the secret. The ``knowledge_units`` triggers
    (``credential_body_trigger_ddl``) enforce the same rule in SQL, as far as SQL
    can, for a writer that runs none of this code.
    """
    if not holds_secret(project_type, domain):
        return
    text = body or ""
    held = text.count(_VALUE_MARKER) == 1
    if held:
        lines = text.split(_VALUE_MARKER, 1)[1].split("\n")
        while lines and lines[-1].startswith(_TRAILER_PREFIXES):
            lines.pop()
        held = (
            bool(lines) and lines[0] == SECRET_PLACEHOLDER
            and not "".join(lines[1:]).strip()
        )
    if not held:
        raise ValueError(
            "refusing a credential reference whose body carries its value: write "
            "it through ingest_knowledge_unit, which holds the value out (#2426)"
        )


#: The SQL twin of ``refuse_inline_secret``, for the writers that run none of
#: this module (a process started before #2426, a script, raw SQL). It checks a
#: credential row's body for exactly one value marker, followed by the
#: placeholder, followed by the end of the body or a line break. What follows
#: that line break (a second line of the value) only the Python check sees.
#: UPDATE fires only on the columns that decide it, so the retrieval counters
#: still update rows written before this change, whose value is still inline.
_CREDENTIAL_ROW = (
    "lower(trim(NEW.project_type, ' ' || char(9) || char(10) || char(13))) = 'reference' "
    "AND lower(trim(NEW.domain, ' ' || char(9) || char(10) || char(13))) "
    "= 'reference.credentials'"
)


def credential_body_trigger_ddl() -> tuple[str, ...]:
    """The BEFORE INSERT and BEFORE UPDATE triggers that refuse a credential
    body without the canonical placeholder line (#2426)."""
    marker = "char(10) || char(10) || 'Value: '"
    held = f"{marker} || '{SECRET_PLACEHOLDER}'"
    body = "coalesce(NEW.body, '')"
    held_len = len(_VALUE_MARKER + SECRET_PLACEHOLDER)
    refused = (
        f"WHEN {_CREDENTIAL_ROW} AND NOT ("
        f"instr({body}, {held}) > 0 "
        f"AND length({body}) - length(replace({body}, {marker}, '')) = {len(_VALUE_MARKER)} "
        f"AND substr({body}, instr({body}, {held}) + {held_len}, 1) IN ('', char(10))"
        ")"
    )
    message = (
        "refusing a credential reference whose body carries its value: write it "
        "through ingest_knowledge_unit (#2426)"
    )
    return tuple(
        f"CREATE TRIGGER IF NOT EXISTS knowledge_units_credential_body_{name} "
        f"BEFORE {event} ON knowledge_units {refused} "
        f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
        for name, event in (
            ("insert", "INSERT"),
            ("update", "UPDATE OF body, project_type, domain"),
        )
    )


#: The message the secret_ref guard raises; ``upsert`` recognises it.
_REF_GUARD_REFUSED = "refusing a credential reference that names no stored secret (#2426)"


def secret_ref_trigger_ddl() -> tuple[str, ...]:
    """The triggers that keep a credential unit and its secret row one pair.

    BEFORE INSERT, and BEFORE UPDATE of the columns that decide it, refuse a
    credential row whose secret_ref is NULL or names no reference_secrets row:
    no unit is ever without its secret. (An UPDATE of other columns, such as a
    retrieval counter, still works on a row written before #2426, whose value is
    inline and whose secret_ref is NULL.) AFTER a repoint and AFTER a delete,
    the secret row the unit stopped naming goes, unless another unit still names
    it. Rows of reference_secrets are never updated, only inserted and deleted.
    """
    guard = (
        f"WHEN {_CREDENTIAL_ROW} AND (NEW.secret_ref IS NULL OR NOT EXISTS "
        "(SELECT 1 FROM reference_secrets s WHERE s.id = NEW.secret_ref)) "
        f"BEGIN SELECT RAISE(ABORT, '{_REF_GUARD_REFUSED}'); END"
    )
    retire = (
        "BEGIN DELETE FROM reference_secrets WHERE id = OLD.secret_ref AND NOT EXISTS "
        "(SELECT 1 FROM knowledge_units k WHERE k.secret_ref = OLD.secret_ref); END"
    )
    return (
        "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_insert "
        f"BEFORE INSERT ON knowledge_units {guard}",
        "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_update "
        f"BEFORE UPDATE OF secret_ref, body, project_type, domain ON knowledge_units {guard}",
        "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_repoint "
        "AFTER UPDATE OF secret_ref ON knowledge_units "
        f"WHEN OLD.secret_ref IS NOT NULL AND OLD.secret_ref IS NOT NEW.secret_ref {retire}",
        "CREATE TRIGGER IF NOT EXISTS knowledge_units_secret_ref_delete "
        f"AFTER DELETE ON knowledge_units WHEN OLD.secret_ref IS NOT NULL {retire}",
    )


def _prepare_fts5(query: str) -> str | None:
    """Prepare a query string for FTS5 MATCH.

    Lowercases the query to neutralize accidental FTS5 boolean operators —
    uppercase OR/AND are interpreted as operators by FTS5, but lowercase
    or/and are plain search terms. FTS5 content matching is case-insensitive,
    so lowercasing doesn't affect result quality.

    Returns None if the query is empty after escaping (caller should return []).
    """
    cleaned = re.sub(r'[^\w\s]', " ", query.lower(), flags=re.UNICODE).strip()
    return cleaned or None


async def insert(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    source_doc: str,
    concept: str,
    body: str,
    id: str | None = None,
    source_platform: str | None = None,
    section_title: str | None = None,
    relationships: str | None = None,
    caveats: str | None = None,
    tags: str | None = None,
    confidence: float = 0.85,
    source_date: str | None = None,
    ingested_at: str | None = None,
    qdrant_id: str | None = None,
    embedding_model: str | None = None,
    source_pipeline: str | None = None,
    purpose: str | None = None,
    ingestion_source: str | None = None,
    origin_class: str | None = None,
    _commit: bool = True,
) -> str:
    """Insert a knowledge unit into both knowledge_units and knowledge_fts. Returns id.

    Pass _commit=False for batch operations where the caller manages the transaction.
    A credential reference is refused: it commits only beside its secret, which
    ``upsert`` (``secret_value``) writes.
    """
    refuse_inline_secret(project_type, domain, body)
    if holds_secret(project_type, domain):
        raise ValueError(
            "refusing a credential reference written without its secret: use "
            "upsert with secret_value (#2426)"
        )
    project_type, domain = _canonical_key(project_type, domain)
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    await db.execute(
        """INSERT INTO knowledge_units
           (id, project_type, domain, source_doc, source_platform, section_title,
            concept, body, relationships, caveats, tags, confidence,
            source_date, ingested_at, qdrant_id, embedding_model,
            source_pipeline, purpose, ingestion_source, origin_class)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (unit_id, project_type, domain, source_doc, source_platform, section_title,
         concept, body, relationships, caveats, tags, confidence,
         source_date, now_iso, qdrant_id, embedding_model,
         source_pipeline, purpose, ingestion_source, origin_class),
    )

    await db.execute(
        """INSERT INTO knowledge_fts
           (unit_id, concept, body, tags, domain, project_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (unit_id, concept, body, tags or "", domain, project_type),
    )

    if _commit:
        await db.commit()
    return unit_id


async def get(db: aiosqlite.Connection, unit_id: str) -> dict | None:
    """Get a knowledge unit by id."""
    async with db.execute(
        "SELECT * FROM knowledge_units WHERE id = ?", (unit_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row, strict=False))


async def get_by_qdrant_ids(
    db: aiosqlite.Connection, qdrant_ids: list[str]
) -> dict[str, list[dict]]:
    """Map Qdrant point IDs → the knowledge_units rows sharing each ID.

    Qdrant point IDs map to ``knowledge_units.qdrant_id`` (NOT ``.id``) — the
    same contract as ``increment_retrieved_batch``. A retriever result's
    ``memory_id`` is the Qdrant point ID, so hydrating a vector hit back to its
    knowledge_units row must key on ``qdrant_id``, not the primary key (keying
    on ``.id`` silently matches nothing — the two are independent UUIDs). Uses
    the ``idx_knowledge_units_qdrant_id`` index.

    Returns a LIST of rows per ``qdrant_id`` because a single Qdrant point can
    back multiple units: ``MemoryStore.store`` dedups identical content to one
    point, so two units with the same body but different
    ``(project_type, domain, concept)`` keys share a ``qdrant_id`` (the column
    is not UNIQUE). Collapsing to one row would silently drop the others and
    could resolve a vector hit to the wrong unit; callers decide how to handle
    a shared point (e.g. keep only rows matching a project/domain filter).
    """
    if not qdrant_ids:
        return {}
    placeholders = ",".join("?" for _ in qdrant_ids)
    async with db.execute(
        f"SELECT * FROM knowledge_units WHERE qdrant_id IN ({placeholders})",
        qdrant_ids,
    ) as cursor:
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
    result: dict[str, list[dict]] = {}
    for row in rows:
        record = dict(zip(columns, row, strict=False))
        result.setdefault(record["qdrant_id"], []).append(record)
    return result


async def find_by_unique_key(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    concept: str,
) -> dict | None:
    """Find a knowledge unit by its unique key (project_type, domain, concept).

    Used by the reference store to detect existing entries before upsert so
    that stale Qdrant points for replaced content can be cleaned up before
    writing the new version.
    """
    async with db.execute(
        "SELECT * FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    ) as cursor:
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row, strict=False))


async def upsert(
    db: aiosqlite.Connection,
    *,
    project_type: str,
    domain: str,
    source_doc: str,
    concept: str,
    body: str,
    id: str | None = None,
    source_platform: str | None = None,
    section_title: str | None = None,
    relationships: str | None = None,
    caveats: str | None = None,
    tags: str | None = None,
    confidence: float = 0.85,
    source_date: str | None = None,
    ingested_at: str | None = None,
    qdrant_id: str | None = None,
    embedding_model: str | None = None,
    source_pipeline: str | None = None,
    purpose: str | None = None,
    ingestion_source: str | None = None,
    origin_class: str | None = None,
    secret_value: str | None = None,
    insert_only: bool = False,
    expected: KeyObservation | None = None,
    _commit: bool = True,
) -> tuple[str, bool]:
    """Insert or update a knowledge unit keyed on (project_type, domain, concept).

    ``secret_value`` is a credential reference's value, held out of ``body``
    (#2426, ``reference_ops.split_secret``). It goes into a NEW
    ``reference_secrets`` row under a fresh random id, and then ONE statement
    writes the unit row naming that id in ``secret_ref``. The body and the
    secret it names are therefore always one writer's pair, however this
    shared connection interleaves other coroutines' statements: the connection
    serializes statements, not transactions, and one statement is the unit of
    pairing. If the unit write fails or is cancelled, the new secret row is an
    unreferenced orphan (``sweep_orphan_secrets`` removes it later); nothing is
    restored, and nothing is mispaired. The triggers of
    ``secret_ref_trigger_ddl`` refuse a credential row that names no secret and
    retire the secret a unit stops naming. None keeps the row's current secret
    (a re-ingest of a held-out body); passing one for a row that is not a
    credential reference is a ValueError.

    ``expected`` (a credential write's own earlier read of its key, which
    ``ingest_knowledge_unit`` takes before it stores the vector): the write is
    applied only while the key still matches it, checked inside the ONE
    statement that writes the unit, so a same-key writer in another process
    that committed in between makes this call raise ``CredentialKeyChanged``
    with nothing written (its fresh secret row retired). An absent observation
    inserts only if the key is still absent; a present one updates only the row
    still naming the observed secret and point. Ignored with ``insert_only``,
    which never changes an existing row anyway.

    ``insert_only`` (for auto-capture; no writer passes it yet): a credential key that already exists is
    never changed. If it holds the same value the call is a no-op that returns
    ``(existing_id, False)``; if it holds a different one, ``CredentialKeyTaken``
    is raised and nothing is written, so the caller tries another key.

    Returns (unit_id, inserted) where ``inserted=True`` means a new row was
    created and ``inserted=False`` means an existing row was updated (or, with
    ``insert_only``, left as it was).

    On conflict (same project_type + domain + concept):
      - The existing row's ``id`` is preserved (kept stable for callers that
        track it, e.g. Qdrant ``qdrant_id`` references).
      - ``retrieved_count`` is preserved (don't reset access history).
      - Everything else is updated, including a fresh ``ingested_at``.

    The FTS5 shadow row is replaced: old row deleted, new row inserted under
    the stable unit_id, in the same implicit transaction as the unit write and
    committed by the single ``db.commit()`` at the end.

    **Caller invariant**: on re-upsert, callers should pass the existing
    row's ``id`` (via ``find_by_unique_key``) rather than a fresh UUID.
    If a different ``id`` is passed for a conflicting key, the
    ``inserted`` flag will incorrectly return True because the actual_id
    (from the pre-existing row) won't match the caller-supplied id.
    ``ingest_knowledge_unit`` enforces this correctly.
    """
    refuse_inline_secret(project_type, domain, body)
    credential = holds_secret(project_type, domain)
    if secret_value is not None and not credential:
        raise ValueError("secret_value is only held for a credential reference (#2426)")
    project_type, domain = _canonical_key(project_type, domain)
    unit_id = id or str(uuid.uuid4())
    now_iso = ingested_at or datetime.now(UTC).isoformat()

    secret_ref: str | None = None
    if credential:
        # Every check runs before the first write. The read touches
        # reference_secrets, so a database whose schema init has not created it
        # fails here, with nothing written.
        existing = await _credential_row(db, project_type, domain, concept)
        if expected is not None and not insert_only and not _still_observed(existing, expected):
            raise CredentialKeyChanged(
                "this credential key changed since the write read it (#2426)"
            )
        if secret_value is None:
            if not (existing and existing[2] is not None):
                raise ValueError(
                    "refusing a credential reference with no value to hold: its "
                    "body carries the placeholder and no secret is stored for it (#2426)"
                )
            # The row keeps the secret it names. The INSERT half of the
            # statement below carries it too, because SQLite runs the BEFORE
            # INSERT trigger even when the conflict turns the insert into an
            # update: if a delete took the row (and its secret) since this
            # read, the trigger refuses the write instead of re-creating a
            # placeholder body with no value.
            secret_ref = existing[1]
        elif insert_only and existing:
            if existing[2] != secret_value:
                raise CredentialKeyTaken(
                    "this credential key already holds a different value (#2426)"
                )
            return existing[0], False
        else:
            secret_ref = secrets.token_hex(16)
            await db.execute(
                "INSERT INTO reference_secrets (id, value, created_at) VALUES (?, ?, ?)",
                (secret_ref, secret_value, datetime.now(UTC).isoformat()),
            )

    cas = credential and expected is not None and not insert_only
    if insert_only or (cas and expected.absent):
        on_conflict = "DO NOTHING"
    else:
        # A new secret repoints the row (the repoint trigger retires the old
        # secret); without one the row keeps the secret it names.
        repoint = (
            ",\n                   secret_ref      = excluded.secret_ref"
            if secret_value is not None
            else ""
        )
        on_conflict = f"""DO UPDATE SET
                   source_doc      = excluded.source_doc,
                   source_platform = excluded.source_platform,
                   section_title   = excluded.section_title,
                   body            = excluded.body,
                   relationships   = excluded.relationships,
                   caveats         = excluded.caveats,
                   tags            = excluded.tags,
                   confidence      = excluded.confidence,
                   source_date     = excluded.source_date,
                   ingested_at     = excluded.ingested_at,
                   qdrant_id       = excluded.qdrant_id,
                   embedding_model = excluded.embedding_model,
                   source_pipeline = excluded.source_pipeline,
                   purpose         = excluded.purpose,
                   ingestion_source = excluded.ingestion_source,
                   origin_class    = excluded.origin_class{repoint}"""
    # Compare-and-swap (``expected`` present): the row is written only while it
    # still exists and names the observed secret and point. INSERT ... SELECT
    # with an EXISTS guard, so a row deleted since the read is not re-created,
    # and a WHERE on the update, so a row repointed since is not overwritten.
    cas_present = cas and not expected.absent
    if cas_present:
        on_conflict += (
            "\n               WHERE knowledge_units.id = ? AND knowledge_units.secret_ref IS ?"
            " AND knowledge_units.qdrant_id IS ?"
        )
    source = (
        "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? "
        "WHERE EXISTS (SELECT 1 FROM knowledge_units WHERE project_type = ? "
        "AND domain = ? AND concept = ?)"
        if cas_present
        else "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    try:
        cursor = await db.execute(
            f"""INSERT INTO knowledge_units
               (id, project_type, domain, source_doc, source_platform, section_title,
                concept, body, relationships, caveats, tags, confidence,
                source_date, ingested_at, qdrant_id, embedding_model,
                source_pipeline, purpose, ingestion_source, origin_class, secret_ref)
               {source}
               ON CONFLICT(project_type, domain, concept) {on_conflict}""",  # noqa: S608 - fixed fragments, values bound
            (
                unit_id,
                project_type,
                domain,
                source_doc,
                source_platform,
                section_title,
                concept,
                body,
                relationships,
                caveats,
                tags,
                confidence,
                source_date,
                now_iso,
                qdrant_id,
                embedding_model,
                source_pipeline,
                purpose,
                ingestion_source,
                origin_class,
                secret_ref,
                *(
                    (project_type, domain, concept,
                     expected.unit_id, expected.secret_ref, expected.qdrant_id)
                    if cas_present else ()
                ),
            ),
        )
    except aiosqlite.IntegrityError as exc:
        if _REF_GUARD_REFUSED not in str(exc):
            raise
        raise ValueError(
            "refusing a credential reference with no value to hold: the secret "
            "it names was deleted or repointed while this write ran (#2426)"
        ) from exc

    if cas and not cursor.rowcount:
        # The key changed between the caller's read and this statement, which
        # therefore wrote nothing. A secret row this call inserted is named by
        # no unit (its id is ours alone): retire it, then say so.
        if secret_value is not None and secret_ref is not None:
            await db.execute(
                "DELETE FROM reference_secrets WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM knowledge_units WHERE secret_ref = ?)",
                (secret_ref, secret_ref),
            )
        if _commit:
            await db.commit()
        raise CredentialKeyChanged("this credential key changed while the write ran (#2426)")

    if insert_only and not cursor.rowcount:
        # Another writer inserted the key between the check and this
        # statement, which therefore changed nothing. Our secret row is named
        # by no unit and by no one else (its id is ours alone): retire it now.
        if secret_ref is not None and secret_value is not None:
            await db.execute(
                "DELETE FROM reference_secrets WHERE id = ? AND NOT EXISTS "
                "(SELECT 1 FROM knowledge_units WHERE secret_ref = ?)",
                (secret_ref, secret_ref),
            )
        winner = await _credential_row(db, project_type, domain, concept)
        if _commit:
            await db.commit()
        if credential and (winner is None or winner[2] != secret_value):
            raise CredentialKeyTaken("this credential key already holds a different value (#2426)")
        if winner is not None:
            return winner[0], False

    # Find the row that actually lives in the table — either the one we
    # just inserted (same unit_id) or a pre-existing row with the same
    # unique key (different id).  This matters because callers passing
    # id=None on an UPSERT conflict would otherwise get back a brand-new
    # UUID that points at nothing.
    rows = await db.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE project_type = ? AND domain = ? AND concept = ?",
        (project_type, domain, concept),
    )
    row = rows[0] if rows else None
    actual_id = row[0] if row else unit_id
    inserted = actual_id == unit_id

    # Replace FTS5 shadow row so full-text search stays consistent with body.
    # FTS5 has no ON CONFLICT so we delete + re-insert under the actual id.
    await db.execute("DELETE FROM knowledge_fts WHERE unit_id = ?", (actual_id,))
    await db.execute(
        """INSERT INTO knowledge_fts
           (unit_id, concept, body, tags, domain, project_type)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (actual_id, concept, body, tags or "", domain, project_type),
    )

    if _commit:
        await db.commit()
    return actual_id, inserted


async def search_fts(
    db: aiosqlite.Connection,
    query: str,
    *,
    project: str | None = None,
    exclude_project: str | None = None,
    domain: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Full-text search on knowledge content. Returns matching rows with rank.

    JOINs back to knowledge_units to include source_pipeline (authority boosting
    in the recall merge step) plus ingested_at/confidence (reference browser
    provenance badge).
    """
    escaped = _prepare_fts5(query)
    if not escaped:
        return []
    sql = (
        "SELECT f.unit_id, f.concept, f.body, f.tags, f.domain, f.project_type,"
        " f.rank, k.source_pipeline, k.ingested_at, k.confidence,"
        " k.origin_class"
        " FROM knowledge_fts f"
        " LEFT JOIN knowledge_units k ON k.id = f.unit_id"
        " WHERE knowledge_fts MATCH ?"
    )
    params: list = [escaped]
    if project:
        sql += " AND f.project_type = ?"
        params.append(project)
    if exclude_project:
        # NULL-SAFE, and that is not a detail: in SQL `project_type != 'x'` is
        # NULL — therefore NOT TRUE — for a row whose project_type is NULL, so
        # the bare comparison silently drops every untyped row from the result.
        # The exclusion must remove one partition, not every row that failed to
        # declare one.
        sql += " AND (f.project_type IS NULL OR f.project_type != ?)"
        params.append(exclude_project)
    if domain:
        sql += " AND f.domain = ?"
        params.append(domain)
    sql += " ORDER BY f.rank LIMIT ?"
    params.append(limit)
    # AND-first, OR-fallback on zero rows: FTS5's implicit-AND otherwise starves
    # multi-word queries (every token must match). knowledge_fts has no boolean
    # mode, so the fallback always applies here.
    rows = await fetch_fts(db, sql, params)
    return [
        {
            "unit_id": r[0], "concept": r[1], "body": r[2],
            "tags": r[3], "domain": r[4], "project_type": r[5], "rank": r[6],
            "source_pipeline": r[7], "ingested_at": r[8], "confidence": r[9],
            # WS-3 stored provenance (0054-stamped at ingest).
            "origin_class": r[10],
        }
        for r in rows
    ]


async def stats(
    db: aiosqlite.Connection,
    *,
    project: str | None = None,
    exclude_project: str | None = None,
) -> dict:
    """Aggregate stats for knowledge units.

    ``exclude_project`` is NULL-safe on purpose: a bare ``project_type != ?``
    is NULL, and so not TRUE, for a row that declares no project_type, which
    would drop every untyped row from the totals.
    """
    # COMPOSED, not exclusive. An `elif` here would silently drop the
    # exclusion whenever both filters are supplied, so `stats` would disagree
    # with `search_fts` — which ANDs them — about what the same pair of
    # arguments means. No caller passes both today; the inconsistency is the
    # defect, because the next one will not know which function it got.
    clauses: list[str] = []
    values: list[str] = []
    if project:
        clauses.append("project_type = ?")
        values.append(project)
    if exclude_project:
        clauses.append("(project_type IS NULL OR project_type != ?)")
        values.append(exclude_project)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params: tuple = tuple(values)

    rows = await db.execute_fetchall(
        f"SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at) FROM knowledge_units {where}",
        params,
    )
    row = rows[0] if rows else None
    total, oldest, newest = row

    # Domain breakdown
    domain_rows = await db.execute_fetchall(
        f"SELECT domain, COUNT(*) FROM knowledge_units {where} GROUP BY domain",
        params,
    )
    domains = {r[0]: r[1] for r in domain_rows}

    # Tier breakdown (curated vs recon vs other)
    tier_rows = await db.execute_fetchall(
        f"""SELECT
                CASE
                    WHEN source_pipeline = 'curated' THEN 'curated'
                    WHEN source_pipeline = 'recon' THEN 'recon'
                    WHEN source_pipeline IS NULL THEN 'unknown'
                    ELSE source_pipeline
                END AS tier,
                COUNT(*)
            FROM knowledge_units {where}
            GROUP BY tier""",
        params,
    )
    by_tier = {r[0]: r[1] for r in tier_rows}

    return {
        "total": total,
        "oldest_ingested": oldest,
        "newest_ingested": newest,
        "by_domain": domains,
        "by_tier": by_tier,
    }


async def list_by_domain(
    db: aiosqlite.Connection,
    *,
    project_type: str,
) -> dict[str, list[dict]]:
    """Return all units for a project_type grouped by domain.

    Returns ``{domain: [{id, concept, body, ingested_at, tags,
    source_pipeline, confidence}, ...]}`` sorted by domain then ingested_at
    desc. Used by the dashboard reference browser — ``source_pipeline`` and
    ``confidence`` drive the provenance badge (manual/verified vs
    auto-captured/unverified).
    """
    rows = await db.execute_fetchall(
        "SELECT id, domain, concept, body, ingested_at, tags, "
        "source_pipeline, confidence "
        "FROM knowledge_units WHERE project_type = ? "
        "ORDER BY domain, ingested_at DESC",
        (project_type,),
    )
    result: dict[str, list[dict]] = {}
    for uid, domain, concept, body, ingested_at, tags, source_pipeline, confidence in rows:
        result.setdefault(domain, []).append({
            "id": uid,
            "domain": domain,  # self-describing rows (consumers derive kind from this)
            "concept": concept,
            "body": body,
            "ingested_at": ingested_at,
            "tags": tags,
            "source_pipeline": source_pipeline,
            "confidence": confidence,
        })
    return result


async def select_fenced_intake_rows(db: aiosqlite.Connection) -> list[dict]:
    """Return intake-provenance units whose body still carries a ```json fence.

    Selection for ``scripts/cleanup_fenced_knowledge_units.py`` (2026-07-03
    context-layer audit): before the atomizer learned to unwrap fences,
    surplus-intake output was stored verbatim as fenced envelopes. Scoped to
    intake provenance so legitimate documents that merely contain inline JSON
    fences are never selected. Ordered oldest-first so a resumed cleanup run
    processes rows in a stable order.
    """
    rows = await db.execute_fetchall(
        "SELECT id, project_type, domain, source_doc, source_pipeline, "
        "body, confidence, ingested_at, qdrant_id "
        "FROM knowledge_units "
        "WHERE source_doc LIKE 'intake:%' AND body LIKE '%```json%' "
        "ORDER BY ingested_at",
    )
    keys = ("id", "project_type", "domain", "source_doc", "source_pipeline",
            "body", "confidence", "ingested_at", "qdrant_id")
    return [dict(zip(keys, row, strict=True)) for row in rows]


async def increment_retrieved_batch(
    db: aiosqlite.Connection,
    qdrant_ids: list[str],
) -> int:
    """Increment retrieved_count for knowledge units matched by Qdrant point IDs.

    Qdrant point IDs map to knowledge_units.qdrant_id (not .id) because
    ingest_knowledge_unit stores the memory_metadata ID as qdrant_id.
    Returns count of rows updated.
    """
    if not qdrant_ids:
        return 0
    placeholders = ",".join("?" for _ in qdrant_ids)
    cursor = await db.execute(
        f"UPDATE knowledge_units SET retrieved_count = retrieved_count + 1 "
        f"WHERE qdrant_id IN ({placeholders})",
        qdrant_ids,
    )
    await db.commit()
    return cursor.rowcount


async def delete(db: aiosqlite.Connection, unit_id: str) -> bool:
    """Delete a knowledge unit from knowledge_units and knowledge_fts.

    A credential's secret row goes with it through the delete trigger
    (``secret_ref_trigger_ddl``), whoever deletes the unit; the orphan sweep
    then clears any secret an interrupted upsert left behind."""
    cursor = await db.execute(
        "DELETE FROM knowledge_units WHERE id = ?", (unit_id,)
    )
    await db.execute(
        "DELETE FROM knowledge_fts WHERE unit_id = ?", (unit_id,)
    )
    await sweep_orphan_secrets(db)
    await db.commit()
    return cursor.rowcount > 0
