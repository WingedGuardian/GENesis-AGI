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

    Fails OPEN when the session id is unavailable OR STALE. The MCP child's env
    is a SNAPSHOT taken when CC spawned it: CC sets ``CLAUDE_CODE_SESSION_ID``
    at stdio-MCP spawn and, on a conversation reset, updates only its OWN
    ``process.env`` — the child keeps the pre-reset id (MEASURED 2026-09-02: the
    health MCP held ``837dfb4b…`` while the live session was ``d3d02163…``).
    Unreachable for this gate, because staleness needs a reset and a
    ``GENESIS_CC_SESSION=1`` session is a single-shot ``claude -p`` that never
    resets — but a future dispatched shape that DID reset would silently disarm
    this. A truthfulness gate, not a security boundary.

    Two things this deliberately does NOT do. ``_impl_session_ledger_update`` is
    ungated: it can only edit an existing row, never mint a new promise. And a
    refused session could route around this by passing a fabricated 32-char id
    (``upsert_stub`` is a bare INSERT OR IGNORE), producing an orphan charter —
    pre-existing and equally reachable from a foreground session, but newly
    incentivised by this refusal, so it is named here rather than discovered.
    """
    if os.environ.get("GENESIS_CC_SESSION") != "1":
        return None
    own = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
    if not own or own != sid:
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
                + (
                    " NOTE: for a dispatched/channel session neither route"
                    " fires — compaction skips it and both writes are refused"
                    " — so no charter will ever appear. task_states is the"
                    " continuity spine for that session class."
                    if os.environ.get("GENESIS_CC_SESSION") == "1" else ""
                )
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
        if err := _unresolved_short_id_error(sid):
            return err
        if err := _self_write_unreadable_error(sid):
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
        if err := _unresolved_short_id_error(sid):
            return err
        if err := _self_write_unreadable_error(sid):
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
        _own = (os.environ.get("CLAUDE_CODE_SESSION_ID") or "").strip()
        _dispatched = os.environ.get("GENESIS_CC_SESSION") == "1"
        if _dispatched and _own:
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
    """Update a ledger item: close it (done), mark absorbed/dropped, or edit."""
    db = _get_db()
    if db is None:
        return {"error": "Database not available"}
    if not item_id.strip():
        return {"error": "item_id is required"}
    if status is None and text is None and evidence is None:
        return {"error": "Nothing to update: pass status, text, or evidence"}
    try:
        from genesis.db.crud import session_charters as crud

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
