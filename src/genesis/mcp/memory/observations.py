"""Observation tools: write, query, resolve."""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from genesis.db.crud import observations
from genesis.memory.provenance import (
    ORIGIN_EXTERNAL_UNTRUSTED,
    ORIGIN_FIRST_PARTY,
    session_origin_from_env,
)

from ..memory import mcp

logger = logging.getLogger(__name__)

query = observations.query
create = observations.create
resolve = observations.resolve

# WS-3 write-side type authorization: observation types an EXTERNAL-origin
# session may NEVER write via this tool. DENYLIST, not allowlist — external
# sessions (inbox judge, campaign, steward) legitimately write many digest
# types (user_signal, finding, generalizable_lesson, ...), and an allowlist
# would break those; only the privileged-consumer types are denied.
# ``user_model_delta``: consumed by UserModelEvolver.process_pending_deltas
# (auto-accept on confidence). Its legit writers are SERVER-SIDE
# (cc/reflection_bridge/_output.py, perception/writer.py — no session env),
# never this tool, so denying it costs no first-party function while closing
# the injection → user-model-poisoning write path. (``task_detected`` stays
# writable: the dispatcher's consumption gate already bars untrusted rows —
# deny at the privileged CONSUMER when one exists; deny the WRITE only for
# types whose consumer trusts content on origin, like the delta accept path
# trusts the reflection pipeline.)
_EXTERNAL_SESSION_DENIED_TYPES: frozenset[str] = frozenset({"user_model_delta"})

# WS-3 write-side field constraint for external-origin sessions. The metadata
# columns (source/type/category/priority) render VERBATIM and OUTSIDE the
# <external-content> wrapper at every surfacing consumer (the wrap only covers
# `content`), so an injected judge session could otherwise move its payload
# from content into e.g. source= and evade the boundary. These are STRUCTURAL
# identifier fields, never prose — enforce a strict charset + length so
# injection text (spaces, punctuation, newlines) cannot ride in them. Verified
# against the entire live observations table: 0 legitimate values violate this.
_SAFE_METADATA_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_ALLOWED_PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high", "critical"})


def _observation_type_permitted(observation_type: str) -> bool:
    """False iff the CURRENT session's origin may not write *observation_type*.

    Only external-untrusted sessions are restricted; owner/first_party and
    un-stamped (foreground/server) sessions are unrestricted. Reads the session
    origin live from ``GENESIS_SESSION_ORIGIN`` — the same env the origin
    stamp below uses.
    """
    if session_origin_from_env() == ORIGIN_EXTERNAL_UNTRUSTED:
        return observation_type not in _EXTERNAL_SESSION_DENIED_TYPES
    return True


def _external_metadata_violation(
    source: str, observation_type: str, priority: str, category: str | None
) -> str | None:
    """For an external-origin session, the first metadata field that fails the
    structural-identifier constraint, else None. Non-external sessions: None.

    Guards the columns that render outside the content wrapper. Returns a short
    field label (not the offending value) for a safe refusal message.
    """
    if session_origin_from_env() != ORIGIN_EXTERNAL_UNTRUSTED:
        return None
    for label, value in (("source", source), ("type", observation_type), ("category", category)):
        # fullmatch, not match: `$` also matches just before a single trailing
        # newline, which would let a line break ride into a rendered field.
        if value is not None and not _SAFE_METADATA_RE.fullmatch(value):
            return label
    if priority not in _ALLOWED_PRIORITIES:
        return "priority"
    return None


def _memory_mod():
    import genesis.mcp.memory_mcp as memory_mod

    return memory_mod


@mcp.tool()
async def observation_write(
    content: str,
    source: str,
    type: str,
    priority: str = "medium",
    category: str | None = None,
    speculative: bool = False,
) -> str:
    """Write processed reflection/observation. Returns observation_id."""
    if not _observation_type_permitted(type):
        # Security refusal: an external-origin session tried to write a
        # privileged observation type (likely a prompt-injection attempt on
        # untrusted content). Refuse loudly but never raise — the judge
        # session must keep functioning.
        logger.warning(
            "observation_write REFUSED type=%r from external-origin session "
            "(source=%r) — privileged type denied for external writers",
            type,
            source,
        )
        return (
            f"refused: observation type {type!r} is not permitted from an external-origin session"
        )
    _bad_field = _external_metadata_violation(source, type, priority, category)
    if _bad_field is not None:
        # The metadata columns render outside the content wrapper — an external
        # session may not smuggle injection text through them.
        logger.warning(
            "observation_write REFUSED from external-origin session: %s field "
            "fails the structural-identifier constraint (source=%r type=%r)",
            _bad_field,
            source,
            type,
        )
        return (
            f"refused: {_bad_field} must be a short identifier "
            "([A-Za-z0-9_.:-], max 64) for an external-origin session"
        )
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    result = await observations.create(
        memory_mod._db,
        id=str(uuid.uuid4()),
        source=source,
        type=type,
        content=content,
        priority=priority,
        created_at=datetime.now(UTC).isoformat(),
        category=category,
        speculative=int(speculative),
        # WS-3: stamp the dispatching session's origin (mirrors memory_store /
        # procedure_store / knowledge writers), so an external-origin session
        # (e.g. the inbox judge over untrusted content) can no longer forge a
        # privileged-looking observation — a NULL origin used to read as
        # first-party "by omission" and slip past the user-model consumer gate.
        # Coalesce None → first_party (server/foreground writers); the gate
        # normalizes adversarially, so a raw None must never be forwarded.
        origin_class=session_origin_from_env() or ORIGIN_FIRST_PARTY,
        skip_if_duplicate=True,
    )
    return result or "duplicate_skipped"


@mcp.tool()
async def observation_query(
    type: str | None = None,
    priority: str | None = None,
    source: str | None = None,
    resolved: bool | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query observations by type/priority/source.

    External-origin rows are returned with their content wrapped in
    ``<external-content>`` markers — tool results land directly in the calling
    session's LLM context, so this mirrors ``memory_recall``'s wrap discipline
    (data, not instructions). ``origin_class`` stays visible on each row.
    """
    from genesis.memory.provenance import wrap_if_external

    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    rows = await observations.query(
        memory_mod._db,
        type=type,
        priority=priority,
        source=source,
        resolved=resolved,
        limit=limit,
    )
    for row in rows:
        content = row.get("content")
        if isinstance(content, str):
            row["content"] = wrap_if_external(content, row.get("origin_class"))
    return rows


@mcp.tool()
async def observation_resolve(
    observation_id: str,
    resolution_notes: str,
) -> bool:
    """Mark observation resolved with notes.

    WS-3: resolving is the SUPPRESSION dual of forgery — a resolved row drops
    off every ``resolved = 0`` surface (dashboard, morning report, ego/sentinel
    context, the dispatcher's task_detected pickup). An external-origin session
    may therefore only resolve rows that are THEMSELVES external_untrusted (it
    manages its own digests); it may not hide internal alerts/escalations/
    task_detected rows. Non-external sessions are unrestricted.
    """
    memory_mod = _memory_mod()
    memory_mod._require_init()
    assert memory_mod._db is not None
    if session_origin_from_env() == ORIGIN_EXTERNAL_UNTRUSTED:
        target = await observations.get_by_id(memory_mod._db, observation_id)
        if target is not None and target.get("origin_class") != ORIGIN_EXTERNAL_UNTRUSTED:
            logger.warning(
                "observation_resolve REFUSED: external-origin session may not "
                "resolve a non-external row (id=%r origin=%r) — suppression guard",
                observation_id,
                target.get("origin_class"),
            )
            return False
    return await observations.resolve(
        memory_mod._db,
        observation_id,
        resolved_at=datetime.now(UTC).isoformat(),
        resolution_notes=resolution_notes,
    )
