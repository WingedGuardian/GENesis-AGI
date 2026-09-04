"""MCP tools for the session charter + ledger (session-manager PR-2a).

The foreground write path of the determinism contract: at agreement moments
("yes, do that", "add it to the plan") the session calls session_ledger_add
so the item becomes a durable row that every post-compaction window gets
re-injected — summaries cannot erase it. The PreCompact hook
(scripts/genesis_precompact.py) owns origin_prompt/origin_ts; these tools own
the LIVING fields only (mission, pointers, ledger rows) — origin is not
addressable from here by construction.

session_id is the CC transcript session id — visible to the session in the
per-turn ``[Clock: ... | Session: <sid[:8]>]`` tag; truncated ids resolve by
unique prefix.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from genesis.mcp.health import mcp

logger = logging.getLogger(__name__)

_SESSIONS_DIR = Path.home() / ".genesis" / "sessions"

# Ledger statuses that CLOSE a row. Everything else in the crud allow-list
# leaves it a live commitment — see _impl_session_ledger_update, which derives
# the promise-creating set as the complement of this one so a status added to
# db/crud/session_charters.VALID_LEDGER_STATUSES is gated by default. A test
# pins this against that allow-list.
_TERMINAL_LEDGER_STATUSES = frozenset({"done", "absorbed", "dropped"})


def _get_db():
    """Late-import DB from the health MCP module state."""
    import genesis.mcp.health_mcp as health_mcp_mod

    svc = health_mcp_mod._service
    if svc is None:
        return None
    return getattr(svc, "_db", None)


async def _refresh_mirror(db, session_id: str) -> None:
    """Regenerate the charter.md human mirror after a mutation. Best-effort:
    the DB is canonical, a failed mirror only goes stale until the next write."""
    try:
        from genesis.db.crud import session_charters as crud
        from genesis.session_charter import write_charter_md

        charter = await crud.get(db, session_id)
        if charter is None:
            return
        ledger = await crud.ledger_list(db, session_id)
        write_charter_md(_SESSIONS_DIR, session_id, charter, ledger)
    except Exception:
        logger.warning("charter.md refresh failed for %s", session_id, exc_info=True)


def _default_added_by() -> str:
    """Dispatched sessions write as 'ambient'; interactive foreground as
    'foreground' (same discriminator the PreCompact hook and follow-up tools
    use)."""
    return "ambient" if os.environ.get("GENESIS_CC_SESSION") == "1" else "foreground"


def _is_dispatched() -> bool:
    """True in a Genesis-dispatched session (cc/invoker.py stamps this on every
    ``claude -p`` it spawns — channel conversations included)."""
    return os.environ.get("GENESIS_CC_SESSION") == "1"


def _own_session_ids() -> set[str]:
    """Every id a dispatched session may be asked to call "itself".

    TWO ids, in two different namespaces, and the caller usually knows only the
    second:

    - ``CLAUDE_CODE_SESSION_ID`` — the CC transcript id. This is the charter key
      namespace (``session_charters.session_id`` matches
      ``cc_sessions.cc_session_id``, see db/crud/session_charters.py module
      docstring). Set by CC on every stdio-MCP spawn.
    - ``GENESIS_SESSION_ID`` — the Genesis ``cc_sessions.id`` row id, stamped by
      cc/invoker.py:403 from the dispatch-time session context and inherited by
      this MCP child (the same read direct_session_tools.py:109 relies on).

    The gap this closes (Codex P1 on PR #1617): a channel session is TOLD its
    "Session ID" is the Genesis row id — ``ConversationManager`` passes
    ``session["id"]`` to the prompt assembler (cc/conversation.py:259,280 →
    cc/system_prompt.py:97) — and it never sees the CC transcript id at all,
    because the per-turn ``[Clock | Session: x]`` tag is suppressed for
    dispatched sessions (scripts/genesis_urgent_alerts.py:433-435). So the id
    the model actually passes is the Genesis row id, and comparing only against
    ``CLAUDE_CODE_SESSION_ID`` let the real production shape straight through.
    MEASURED on this install's ``cc_sessions``: for the 10 channel rows
    ``id != cc_session_id`` in 10/10, and for the 183 background rows
    ``id == cc_session_id`` in 0/183 — the two namespaces never collide, so
    treating the Genesis row id as "self" cannot mis-flag a legitimate
    cross-session write.
    """
    ids: set[str] = set()
    for var in ("CLAUDE_CODE_SESSION_ID", "GENESIS_SESSION_ID"):
        value = (os.environ.get(var) or "").strip()
        if value:
            ids.add(value)
    return ids


# The advertised SHORT form of a session id: the per-turn `[Clock | Session: x]`
# tag prints the first 8 characters, and `_unresolved_short_id_error` names that
# tag when it asks for the full id — so 8 is the shortest prefix a caller is ever
# told exists. Below it, a "prefix" is a guess.
_ADVERTISED_PREFIX_CHARS = 8


def _resolves_to_own(sid: str) -> bool:
    """Is *sid* this session's own id — including the advertised short prefix?

    ONE predicate, shared by the write gate and the read path's guidance,
    because a disagreement between them is not a cosmetic inconsistency. With an
    exact comparison, a caller passing the 8-char prefix it was SHOWN slipped
    past this classification: the read path then said "update/add can create the
    charter" while the write tools rejected that same prefix as unresolved. Two
    calls, two incompatible answers, both from Genesis.

    Resolved against OUR OWN ids rather than through
    ``crud.resolve_session_id``, which cannot answer this question: it searches
    ``session_charters.session_id`` and ``cc_sessions.cc_session_id``, while a
    channel session is advertised its ``cc_sessions.id`` — a different namespace
    — and may have no charter row yet. The prefix therefore comes back
    unchanged, which is exactly how it reached the failing comparison. Matching
    against the at-most-two ids we hold needs no query and cannot be made
    ambiguous by another session's rows.

    Uniqueness is required, not assumed: if a prefix matched BOTH own ids they
    would have to differ later on, so the caller has still named us — but a
    prefix that matches neither is not ours, and one shorter than the advertised
    form is not a prefix we ever handed out.
    """
    own = _own_session_ids()
    if not own:
        return False
    if sid in own:
        return True
    if len(sid) < _ADVERTISED_PREFIX_CHARS:
        return False
    return any(o.startswith(sid) for o in own)


def _self_write_unreadable_error(sid: str) -> dict | None:
    """Refuse a charter/ledger write a DISPATCHED session makes to ITS OWN charter.

    The charter system is foreground-only: the reader
    (``scripts/genesis_session_context.py`` — the emission block sits in the
    ``not is_genesis_session`` branch) and the maintainer
    (``scripts/genesis_precompact.py``, which returns early on the same
    discriminator so ``origin_prompt`` is never filled) both skip dispatched
    sessions. A dispatched session writing to its OWN charter therefore
    produces a row that is inert BY CONSTRUCTION — stored, never re-injected.

    Measured 2026-09-02: a Telegram DM session (``claude -p`` via CCInvoker)
    wrote a ledger row and told the user it would "survive to Friday". It could
    not — the emission block returned 0 chars for that session on startup,
    resume AND compact.

    Deliberately NARROW. A dispatched session writing to a FOREGROUND session's
    charter is legitimate and readable — that is what ``added_by='ambient'``
    exists for — so only the self-write is refused.

    Residual known gap: a dispatched session writing to a DIFFERENT dispatched
    session's charter is equally inert and is NOT caught here. The obvious
    alternative — classify the TARGET via ``cc_sessions`` — would be WRONG, not
    merely expensive: the incident session's own row records
    ``session_type='foreground', source_tag='foreground', channel='telegram'``,
    so a DB-based gate would have waved the actual defect through, and another
    ambient charter has no ``cc_sessions`` row at all. There is no DB signal for
    "will this session's charter ever be read"; the writer's own env is the only
    sound one.

    Fails OPEN when NEITHER own-id is available (see ``_own_session_ids``), or
    when one is STALE. The MCP child's env
    is a SNAPSHOT taken when CC spawned it: CC sets ``CLAUDE_CODE_SESSION_ID``
    at stdio-MCP spawn and, on a conversation reset, updates only its OWN
    ``process.env`` — the child keeps the pre-reset id (MEASURED 2026-09-02: the
    health MCP held ``837dfb4b…`` while the live session was ``d3d02163…``).
    Unreachable for this gate, because staleness needs a reset and a
    ``GENESIS_CC_SESSION=1`` session is a single-shot ``claude -p`` that never
    resets — but a future dispatched shape that DID reset would silently disarm
    this. A truthfulness gate, not a security boundary.

    ``_impl_session_ledger_update`` applies this same predicate, but only to the
    PROMISE-CREATING mutations (a ``text`` replacement, or a status back to
    open/in_progress): those rewrite an existing row into a new live-looking
    commitment just as effectively as an insert (db/crud/session_charters.py
    ledger_update takes arbitrary text and any VALID_LEDGER_STATUS, Codex P2 on
    PR #1617). Closing a legacy inert row — done/absorbed/dropped, or an
    evidence write — stays allowed, so this never traps a session with rows it
    cannot clean up.

    One thing this deliberately does NOT do: a refused session could route
    around it by passing a fabricated 32-char id (``upsert_stub`` is a bare
    INSERT OR IGNORE), producing an orphan charter — pre-existing and equally
    reachable from a foreground session, but newly incentivised by this refusal,
    so it is named here rather than discovered.
    """
    if not _is_dispatched():
        return None
    if not _resolves_to_own(sid):
        return None
    return {
        "error": "Refusing the write: this is a dispatched/channel session "
        "writing to its OWN charter, and the charter/ledger is foreground-only. "
        "The row would be stored but NEVER re-injected into any future window, "
        "so recording it here would promise a persistence that does not exist. "
        "Durable alternatives, IF this session's profile allows them: "
        "`memory_store` for a fact/decision/plan — denied on every "
        "direct-session profile (cc/direct_session.py _UNIVERSAL_DISALLOW), "
        "reachable from a conversation channel; or `follow_up_create` for "
        "actionable work — a dispatched session's item is routed to the COLD "
        "`tabled` lane, tracked but never auto-dispatched. If both are denied, "
        "put it in your final output: that transcript IS this session's "
        "deliverable."
    }


def _missing_charter_suffix(sid: str) -> str:
    """Extra guidance appended to the read path's "no charter" error.

    Keyed on the SAME predicate as the write gate, not on "am I dispatched"
    (Codex P2 on PR #1617). A dispatched session routinely reads a FOREGROUND
    session's charter — the ambient/cross-session path this gate deliberately
    leaves open — and that charter is NOT permanently absent: the target's own
    next compaction creates it (scripts/genesis_precompact.py), as does any
    foreground write. Telling an ambient caller to abandon a valid target is a
    false claim of absence, so the "will never appear" wording is confined to
    the caller's OWN charter.

    Three states, mirroring the write gate: own charter (permanently absent),
    own id unknown (uncertain — say so), everything else (no suffix; the base
    message is already correct).
    """
    if not _is_dispatched():
        return ""
    own = _own_session_ids()
    if _resolves_to_own(sid):
        # The caller's own charter — by the SAME predicate the write gate uses,
        # short prefix included, so the two calls cannot contradict each other.
        # Both write routes are refused and the PreCompact maintainer returns
        # early on GENESIS_CC_SESSION=1, so nothing can ever create it.
        return (
            " NOTE: that is THIS dispatched/channel session's own id — neither"
            " route fires for it (compaction skips this session class and both"
            " writes are refused), so no charter will ever appear here."
            " Continuity for a dispatched session depends on which kind it is:"
            " an autonomous task session has its task_states row (created by"
            " the task dispatcher / task_submit, autonomy/dispatcher.py:188 and"
            " mcp/health/task_tools.py:200); a CHANNEL conversation has NO"
            " task_states row at all — its continuity is the conversation"
            " itself plus, profile permitting, memory_store / follow_up_create,"
            " and otherwise this session's final output."
        )
    if not own:
        # Fail-open: with no id of our own, we cannot tell whether sid is us.
        return (
            " NOTE: this is a dispatched/channel session and its own id is"
            " unknown here, so whether that charter can ever appear could NOT"
            " be determined — if it is this session's own id, it never will"
            " (compaction skips this session class and both writes are"
            " refused)."
        )
    return ""


def _unresolved_short_id_error(sid: str) -> dict | None:
    """Refuse WRITES under a truncated id that did not resolve.

    A stub created under a short prefix would be orphaned the moment the
    PreCompact hook writes the real full session id — mission/ledger rows
    would never re-inject (Codex P2, PR #1053). Reads fail soft (not-found);
    writes must fail loud here.
    """
    if len(sid) < 32:
        return {
            "error": f"Session id prefix '{sid}' did not resolve to a known "
            "session. Pass the full session id (the [Clock | Session: x] tag "
            "shows the first 8 chars; the full id is this conversation's "
            "session UUID)."
        }
    return None


# ---------------------------------------------------------------------------
# Implementation functions (testable without FastMCP)
# ---------------------------------------------------------------------------


async def _impl_session_charter(session_id: str) -> dict:
    """Read a session's charter: origin, mission, pointers, ledger + counts."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}
    if not session_id.strip():
        return {"error": "session_id is required"}
    try:
        from genesis.db.crud import session_charters as crud

        sid = await crud.resolve_session_id(db, session_id)
        charter = await crud.get(db, sid)
        if charter is None:
            return {
                "error": f"No charter for session '{session_id}'. A charter row "
                "appears at the session's first compaction, or on the first "
                "session_charter_update / session_ledger_add call."
                + _missing_charter_suffix(sid)
            }
        ledger = await crud.ledger_list(db, sid)
        counts = await crud.ledger_counts(db, sid)
        return {
            "session_id": sid,
            "origin_prompt": charter.get("origin_prompt"),
            "origin_ts": charter.get("origin_ts"),
            "mission": charter.get("mission"),
            "pointers": charter.get("pointers") or [],
            "compaction_count": charter.get("compaction_count", 0),
            "created_at": charter.get("created_at"),
            "ledger": [
                {
                    "id": item["id"],
                    "text": item["text"],
                    "status": item["status"],
                    "added_by": item["added_by"],
                    "evidence": item.get("evidence"),
                }
                for item in ledger
            ],
            "ledger_counts": counts,
        }
    except Exception as exc:
        logger.error("session_charter failed", exc_info=True)
        return {"error": f"Failed to read charter: {exc}"}


async def _impl_session_charter_update(
    session_id: str,
    *,
    mission: str | None = None,
    add_pointer: str | None = None,
    remove_pointer: str | None = None,
) -> dict:
    """Update the charter's LIVING fields. Origin is not addressable here."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}
    if not session_id.strip():
        return {"error": "session_id is required"}
    if mission is None and add_pointer is None and remove_pointer is None:
        return {"error": "Nothing to update: pass mission, add_pointer, or remove_pointer"}
    try:
        from genesis.db.crud import session_charters as crud

        sid = await crud.resolve_session_id(db, session_id)
        # SELF first, then the id FORM. Both refuse, so the order only decides
        # which reason the caller is given — and "pass the full id" implies the
        # write would then land, which for this session's own charter it never
        # will. Answering the id form first costs the caller a round trip to
        # fetch an id that is about to be refused anyway.
        if err := _self_write_unreadable_error(sid):
            return err
        if err := _unresolved_short_id_error(sid):
            return err
        # A stub row lets mission/pointers precede the first compaction; the
        # PreCompact hook fills origin later (WHERE origin_prompt IS NULL).
        await crud.upsert_stub(db, sid)
        updated: list[str] = []
        if mission is not None:
            await crud.set_mission(db, sid, mission)
            updated.append("mission")
        if add_pointer is not None or remove_pointer is not None:
            charter = await crud.get(db, sid)
            pointers: list[str] = charter.get("pointers") or []
            if remove_pointer is not None:
                pointers = [p for p in pointers if p != remove_pointer]
                updated.append("remove_pointer")
            if add_pointer is not None and add_pointer not in pointers:
                pointers.append(add_pointer)
                updated.append("add_pointer")
            await crud.set_pointers(db, sid, pointers)
        await _refresh_mirror(db, sid)
        charter = await crud.get(db, sid)
        return {
            "session_id": sid,
            "updated": updated,
            "mission": charter.get("mission"),
            "pointers": charter.get("pointers") or [],
        }
    except Exception as exc:
        logger.error("session_charter_update failed", exc_info=True)
        return {"error": f"Failed to update charter: {exc}"}


async def _impl_session_ledger_add(
    session_id: str,
    text: str,
    *,
    source_ref: str | None = None,
    added_by: str | None = None,
) -> dict:
    """Add an open ledger item (agreement/TODO) to a session's charter."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}
    if not session_id.strip():
        return {"error": "session_id is required"}
    try:
        from genesis.db.crud import session_charters as crud

        sid = await crud.resolve_session_id(db, session_id)
        # SELF first, then the id FORM. Both refuse, so the order only decides
        # which reason the caller is given — and "pass the full id" implies the
        # write would then land, which for this session's own charter it never
        # will. Answering the id form first costs the caller a round trip to
        # fetch an id that is about to be refused anyway.
        if err := _self_write_unreadable_error(sid):
            return err
        if err := _unresolved_short_id_error(sid):
            return err
        await crud.upsert_stub(db, sid)
        item_id = await crud.ledger_add(
            db,
            session_id=sid,
            text=text,
            source_ref=source_ref,
            added_by=added_by or _default_added_by(),
        )
        await _refresh_mirror(db, sid)
        counts = await crud.ledger_counts(db, sid)
        open_n = counts.get("open", 0) + counts.get("in_progress", 0)
        # The message must mirror the GATE's predicate exactly. The gate needs
        # BOTH dispatched AND own-id-known-and-equal; a message keyed on only
        # the first is FALSE on the fail-open path, where a dispatched
        # SELF-write gets through and would be told the row lands on "THAT
        # session" — which is this one. Three states, not two.
        #
        # The confident branch is keyed on the TRANSCRIPT id specifically, not
        # on _own_session_ids() being non-empty: sid lives in the transcript-id
        # namespace (it is a session_charters key), so knowing only the Genesis
        # row id still leaves "sid might be my own transcript id" open, and the
        # beneficiary claim would be unsound.
        _own_transcript = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
        _dispatched = _is_dispatched()
        if _dispatched and _own_transcript:
            # Own id known and != sid (equal was refused by the gate above), so
            # this is the supported cross-session write. Name the beneficiary:
            # the row re-injects into the TARGET's windows, never this session's.
            message = (
                f"Ledger item recorded on session {sid[:8]}'s charter — it will "
                "re-inject into THAT session's post-compaction windows, NOT "
                "this one (dispatched sessions get no charter injection). "
                "Close via session_ledger_update."
            )
        elif _dispatched:
            # Fail-open: own id unknown, so sid may be THIS session. Claim
            # nothing about persistence rather than claim it confidently wrong.
            message = (
                f"Ledger item recorded on session {sid[:8]}'s charter. This "
                "session's own id is unknown, so whether that charter is ever "
                "re-injected could NOT be verified — do not tell the user it "
                "persists. Close via session_ledger_update."
            )
        else:
            message = (
                "Ledger item recorded — it will re-inject into every "
                "post-compaction window until closed via session_ledger_update."
            )
        return {
            "id": item_id,
            "session_id": sid,
            "status": "open",
            "open_items": open_n,
            "message": message,
        }
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("session_ledger_add failed", exc_info=True)
        return {"error": f"Failed to add ledger item: {exc}"}


async def _impl_session_ledger_update(
    item_id: str,
    *,
    status: str | None = None,
    text: str | None = None,
    evidence: str | None = None,
) -> dict:
    """Update a ledger item: close it (done), mark absorbed/dropped, or edit.

    A dispatched session's PROMISE-CREATING edits to its OWN charter are refused
    on the same predicate as the insert path. ``crud.ledger_update`` accepts an
    arbitrary replacement ``text`` and any VALID_LEDGER_STATUS including a
    reopen, so on an install that already carries a legacy inert row — exactly
    the population this gate protects — rewrite-plus-reopen mints a live-looking
    promise just as effectively as an insert would. Closure (done / absorbed /
    dropped) and evidence writes stay open so legacy rows remain cleanable.
    """
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}
    if not item_id.strip():
        return {"error": "item_id is required"}
    if status is None and text is None and evidence is None:
        return {"error": "Nothing to update: pass status, text, or evidence"}
    try:
        from genesis.db.crud import session_charters as crud

        # Read BEFORE writing: the gate needs the row's owning session, and the
        # refusal must land before any mutation (same ordering the insert path
        # uses relative to upsert_stub).
        existing = await crud.get_ledger_item(db, item_id)
        if existing is None:
            return {"error": f"No ledger item with id '{item_id}'"}
        # Promise-creating = a text replacement, or a status that leaves the row
        # LIVE. Derived as the complement of the terminal set against the crud
        # allow-list rather than a positive literal, so a status added upstream
        # is gated by default instead of silently slipping past — and so an
        # INVALID status still falls through to crud.ledger_update's ValueError
        # rather than being answered with a confusing refusal.
        live_statuses = crud.VALID_LEDGER_STATUSES - _TERMINAL_LEDGER_STATUSES
        creates_a_promise = text is not None or status in live_statuses
        if creates_a_promise and (err := _self_write_unreadable_error(existing["session_id"])):
            err["error"] = (
                "Refusing this edit: rewriting the text of, or reopening, a "
                "row on this dispatched session's OWN charter creates a new "
                "live-looking promise on a charter nothing re-injects. "
                "Closing it (status=done/absorbed/dropped) or attaching "
                "evidence is still allowed. " + err["error"]
            )
            return err
        ok = await crud.ledger_update(db, item_id, status=status, text=text, evidence=evidence)
        if not ok:
            return {"error": f"No ledger item with id '{item_id}'"}
        item = await crud.get_ledger_item(db, item_id)
        await _refresh_mirror(db, item["session_id"])
        return {
            "id": item_id,
            "session_id": item["session_id"],
            "status": item["status"],
            "text": item["text"],
            "evidence": item.get("evidence"),
        }
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        logger.error("session_ledger_update failed", exc_info=True)
        return {"error": f"Failed to update ledger item: {exc}"}


# ---------------------------------------------------------------------------
# MCP tool decorators
# ---------------------------------------------------------------------------


@mcp.tool()
async def session_charter(session_id: str) -> dict:
    """Read a session's charter: immutable origin, living mission/pointers,
    and the full ledger with item ids and status counts.

    The charter is the session's durable identity outside the context window —
    what the session is FOR. Use it to reconnect with the origin after heavy
    compaction, or to fetch ledger item ids before session_ledger_update.

    FOREGROUND SESSIONS ONLY: a dispatched/channel session's own charter is never re-injected (the SessionStart reader, the PreCompact maintainer and the per-turn drift tag all skip GENESIS_CC_SESSION=1), so a self-write is refused; writing to a FOREGROUND session's charter is supported.

    Args:
        session_id: CC session id (shown in the per-turn [Clock | Session: x]
            tag). A truncated prefix resolves when unambiguous.
    """
    return await _impl_session_charter(session_id)


@mcp.tool()
async def session_charter_update(
    session_id: str,
    mission: str = "",
    add_pointer: str = "",
    remove_pointer: str = "",
) -> dict:
    """Set the session's living mission and/or edit its pointer list.

    Call when the session's working mission crystallizes or shifts (a pivot,
    an approved plan) so post-compaction windows inherit it. Pointers are
    paths/refs to the session's governing artifacts (spec docs, plan files).
    The immutable origin cannot be changed by this tool.

    FOREGROUND SESSIONS ONLY: a dispatched/channel session's own charter is never re-injected (the SessionStart reader, the PreCompact maintainer and the per-turn drift tag all skip GENESIS_CC_SESSION=1), so a self-write is refused; writing to a FOREGROUND session's charter is supported.

    Args:
        session_id: CC session id (per-turn [Clock | Session: x] tag; unique
            prefix ok).
        mission: 1-3 line living mission statement (omit to leave unchanged).
        add_pointer: a path/ref to append (deduped; capped at 12 pointers).
        remove_pointer: exact pointer string to remove.
    """
    return await _impl_session_charter_update(
        session_id,
        mission=mission or None,
        add_pointer=add_pointer or None,
        remove_pointer=remove_pointer or None,
    )


@mcp.tool()
async def session_ledger_add(
    session_id: str,
    text: str,
    source_ref: str = "",
    added_by: str = "",
) -> dict:
    """Record an agreement/TODO as a durable ledger row on the session charter.

    CALL AT AGREEMENT MOMENTS: when the user says "yes, do that", approves a
    plan item, or work is promised — the row re-injects into every
    post-compaction window until closed, so no summary can erase it. This is
    the first line of defense; ambient extraction is only the safety net.

    FOREGROUND SESSIONS ONLY: a dispatched/channel session's own charter is never re-injected (the SessionStart reader, the PreCompact maintainer and the per-turn drift tag all skip GENESIS_CC_SESSION=1), so a self-write is refused; writing to a FOREGROUND session's charter is supported.

    Args:
        session_id: CC session id (per-turn [Clock | Session: x] tag; unique
            prefix ok).
        text: the agreement/TODO, one line, concrete enough to act on later.
        source_ref: optional provenance (plan file path, PR number, quote).
        added_by: origin of the write — foreground | ambient | pulse
            (default: auto-detected).
    """
    return await _impl_session_ledger_add(
        session_id,
        text,
        source_ref=source_ref or None,
        added_by=added_by or None,
    )


@mcp.tool()
async def session_ledger_update(
    item_id: str,
    status: str = "",
    text: str = "",
    evidence: str = "",
) -> dict:
    """Update a ledger item: mark it done/absorbed/dropped, or refine its text.

    Statuses: open | in_progress | done | absorbed (shipped elsewhere — cite
    evidence, e.g. the PR) | dropped (consciously abandoned). Get item ids
    from session_charter or the SessionStart injection block.

    FOREGROUND SESSIONS ONLY: a dispatched/channel session's own charter is never re-injected (the SessionStart reader, the PreCompact maintainer and the per-turn drift tag all skip GENESIS_CC_SESSION=1), so a self-write is refused; writing to a FOREGROUND session's charter is supported. On a row of its own charter a dispatched session may still close it (done/absorbed/dropped) or attach evidence — only a text replacement or a reopen is refused.

    Args:
        item_id: the ledger row id.
        status: new status (omit to leave unchanged).
        text: replacement text (omit to leave unchanged).
        evidence: supporting ref for done/absorbed (PR link, commit, quote).
    """
    return await _impl_session_ledger_update(
        item_id,
        status=status or None,
        text=text or None,
        evidence=evidence or None,
    )
