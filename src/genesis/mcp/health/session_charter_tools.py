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

from genesis.mcp.health import mcp
from genesis.session_charter import SESSIONS_DIR as _canonical_sessions_dir

logger = logging.getLogger(__name__)


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


# An ALIAS of the canonical constant, not a second definition — kept as a
# module attribute because it is the seam this module's tests redirect, and a
# suite that cannot redirect it writes charter.md into the real home tree.
_SESSIONS_DIR = _canonical_sessions_dir


async def _refresh_mirror(db, session_id: str) -> None:
    """Regenerate the charter.md human mirror after a mutation.

    Delegates to the canonical implementation in ``genesis.session_charter``,
    which the ambient ledger extractor also calls. Two copies would be free to
    drift, and both callers must agree on what a refresh does. The directory is
    passed explicitly so this module's seam still governs where it lands.
    """
    from genesis.session_charter import refresh_mirror

    await refresh_mirror(db, session_id, _SESSIONS_DIR)


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


def _names_own_session(sid: str, raw_id: str | None = None) -> bool:
    """Does the caller's request name THIS session — BEFORE or AFTER resolution?

    ``crud.resolve_session_id`` (db/crud/session_charters.py:126-155) rewrites a
    short id to whichever single ``session_charters.session_id`` or
    ``cc_sessions.cc_session_id`` row it happens to LIKE-match. Our own
    ``GENESIS_SESSION_ID`` is a ``cc_sessions.id``, which is in NEITHER of those
    columns, so a prefix of our own id that collides with one unrelated row
    comes back as somebody ELSE's full transcript id. An identity check that
    sees only the resolved value then reads the caller's own id as a
    cross-session target and lets the write through (Codex P2, PR #1617).

    So classify the RAW input too, and let self-ness WIN when the two answers
    disagree — they can only disagree when resolution silently changed who the
    caller named, and in that state the honest answer is "this may be you",
    which on a truthfulness gate means refuse rather than promise. Rare (an
    8-hex-char collision against ~200 rows), but the cost of checking is a
    string comparison against at most two ids and no query at all.
    """
    if _resolves_to_own(sid):
        return True
    return raw_id is not None and _resolves_to_own(raw_id.strip())


def _self_write_unreadable_error(sid: str, raw_id: str | None = None) -> dict | None:
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
    PROMISE-CREATING mutations — those that leave the row LIVE and either
    replace its ``text`` or move its status back to open/in_progress. Those
    rewrite an existing row into a new live-looking commitment just as
    effectively as an insert (db/crud/session_charters.py ledger_update takes
    arbitrary text and any VALID_LEDGER_STATUS, Codex P2 on PR #1617). Anything
    that leaves the row TERMINAL — closing it done/absorbed/dropped, correcting
    the text of an already-closed row, or refining text and closing in one call
    — stays allowed, so this never traps a session with rows it cannot clean up
    or correct.

    One thing this deliberately does NOT do: a refused session could route
    around it by passing a fabricated 32-char id (``upsert_stub`` is a bare
    INSERT OR IGNORE), producing an orphan charter — pre-existing and equally
    reachable from a foreground session, but newly incentivised by this refusal,
    so it is named here rather than discovered.
    """
    if not _is_dispatched():
        return None
    if not _names_own_session(sid, raw_id):
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


def _missing_charter_suffix(sid: str, raw_id: str | None = None) -> str:
    """Extra guidance appended to the read path's "no charter" error.

    Keyed on the SAME predicate as the write gate, not on "am I dispatched"
    (Codex P2 on PR #1617). A dispatched session routinely reads a FOREGROUND
    session's charter — the ambient/cross-session path this gate deliberately
    leaves open — and that charter is NOT permanently absent: the target's own
    next compaction creates it (scripts/genesis_precompact.py), as does any
    foreground write. Telling an ambient caller to abandon a valid target is a
    false claim of absence, so the "will never appear" wording is confined to
    the caller's OWN charter.

    Three states, mirroring the write gate: own charter (nothing will ever
    re-inject it HERE), own id unknown (uncertain — say so), everything else
    (no suffix; the base message is already correct).

    What the own-charter branch may NOT say is that the charter can never
    APPEAR (Codex P2, PR #1617). The claim is caller-relative, and this PR's
    own premise is that a cross-session write is legitimate and supported —
    so any foreground session, and any OTHER dispatched session (the residual
    gap named in ``_self_write_unreadable_error``), can create this very row
    through ``session_charter_update`` / ``session_ledger_add``. The claim that
    IS sound, and the one the caller actually needs, is about re-injection into
    THIS session: the SessionStart reader, the PreCompact maintainer and the
    per-turn drift tag all skip ``GENESIS_CC_SESSION=1``, so no charter — this
    one or one somebody else creates later — ever reaches this session's
    windows.
    """
    if not _is_dispatched():
        return ""
    own = _own_session_ids()
    if _names_own_session(sid, raw_id):
        # The caller's own charter — by the SAME predicate the write gate uses,
        # short prefix and pre-resolution spelling included, so the two calls
        # cannot contradict each other.
        return (
            " NOTE: that is THIS dispatched/channel session's own id — nothing"
            " THIS session can do will create it (compaction skips this session"
            " class and both write routes are refused here), and even if another"
            " session creates it through the supported cross-session write path,"
            " it will never be re-injected into THIS session's windows."
            " Continuity for a dispatched session depends on which kind it is:"
            " an autonomous task session has its task_states row (created by"
            " the task dispatcher / task_submit, autonomy/dispatcher.py:188 and"
            " mcp/health/task_tools.py:200); a CHANNEL conversation has NO"
            " task_states row at all — its continuity is the conversation"
            " itself plus, profile permitting, memory_store / follow_up_create,"
            " and otherwise this session's final output."
        )
    if not own:
        # Fail-open: with no id of our own we cannot tell whether sid is us —
        # and in that state the write gate does NOT fire either, so this branch
        # must not repeat the own-charter branch's "both writes are refused".
        # Saying so was self-contradictory: the same missing id that makes the
        # target unknowable is what lets the write through (Codex P2, #1617).
        return (
            " NOTE: this is a dispatched/channel session and its own id is"
            " unknown here, so whether that is this session's OWN charter could"
            " NOT be determined. Writes are not refused in this state — they"
            " fail OPEN and would land — but if it IS this session's own id,"
            " nothing written there is ever re-injected into this session"
            " (compaction skips this session class)."
        )
    return ""


def _unresolved_short_id_error(sid: str) -> dict | None:
    """Refuse WRITES under an id that is not a whole session id.

    A stub created under a short prefix would be orphaned the moment the
    PreCompact hook writes the real full session id — mission/ledger rows
    would never re-inject (Codex P2, PR #1053). Reads fail soft (not-found);
    writes must fail loud here.

    The test is the id's SHAPE, not its length. `len(sid) < 32` let a mistyped
    UUID or a 32-character fragment through, and a charter stub then exists
    under a key no session will ever carry — the same defect Codex found on the
    follow-up provenance path (P2, PR #1622), sharing this one generator, so it
    is fixed here in the same move rather than left as the next round's finding.
    """
    from genesis.db.crud.session_charters import is_full_session_id

    if not is_full_session_id(sid):
        return {
            "error": f"Session id '{sid}' did not resolve to a complete session "
            "id. Pass the full session id (the [Clock | Session: x] tag shows "
            "the first 8 chars; the full id is this conversation's session "
            "UUID)."
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
                + _missing_charter_suffix(sid, session_id)
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
        if err := _self_write_unreadable_error(sid, session_id):
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
        if err := _self_write_unreadable_error(sid, session_id):
            return err
        if err := _unresolved_short_id_error(sid):
            return err
        # Internal provenance is not a caller-supplied input. Without this,
        # any caller of this tool could claim `ambient_ledger_extractor` and
        # make a forged row indistinguishable from a real extractor leak — the
        # exact thing the shadow report's leak invariant exists to detect.
        if added_by and added_by not in crud.CALLER_SETTABLE_ADDED_BY:
            return {
                "error": f"added_by must be one of "
                f"{sorted(crud.CALLER_SETTABLE_ADDED_BY)} — {added_by!r} is "
                "internal provenance and cannot be set by a caller"
            }
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
    promise just as effectively as an insert would.

    "Promise-creating" is judged on the RESULTING row, not on which fields the
    call names: an edit that leaves the row terminal (done / absorbed /
    dropped) creates no promise, so closure, evidence writes, correcting the
    text of an already-closed row, and text-plus-closure in one call all stay
    open — legacy rows stay both cleanable and correctable.
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
        # Promise-creating is a property of the row this edit LEAVES BEHIND, not
        # of the fields it touches (Codex P2, PR #1617). Judging `text is not
        # None` on its own refused two edits that create no promise at all: a
        # correction to the text of an already done/absorbed/dropped row, and a
        # text refinement applied atomically WITH status="done". Both leave a
        # terminal row, which the charter injection excludes and which nobody can
        # read as a live commitment — and refusing them contradicts the tool's
        # own promise that closure stays available.
        #
        # So: resolve the RESULTING status first (an omitted status leaves the
        # existing one), and only then ask whether the edit mints a live
        # promise. The live set is still derived as the complement of the
        # terminal set against the crud allow-list rather than a positive
        # literal, so a status added upstream is gated by default instead of
        # silently slipping past — and an INVALID status still falls through to
        # crud.ledger_update's ValueError rather than a confusing refusal.
        live_statuses = crud.VALID_LEDGER_STATUSES - _TERMINAL_LEDGER_STATUSES
        resulting_status = status if status is not None else existing["status"]
        leaves_the_row_live = resulting_status not in _TERMINAL_LEDGER_STATUSES
        creates_a_promise = leaves_the_row_live and (text is not None or status in live_statuses)
        if creates_a_promise and (err := _self_write_unreadable_error(existing["session_id"])):
            err["error"] = (
                "Refusing this edit: it would leave a LIVE row on this "
                "dispatched session's OWN charter with new text or a reopened "
                "status — a new live-looking promise on a charter nothing "
                "re-injects. Anything that leaves the row terminal is still "
                "allowed: close it (status=done/absorbed/dropped), attach "
                "evidence, correct the text of an already-closed row, or pass "
                "text together with status=done. " + err["error"]
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

    FOREGROUND SESSIONS ONLY: a dispatched/channel session's own charter is never re-injected (the SessionStart reader, the PreCompact maintainer and the per-turn drift tag all skip GENESIS_CC_SESSION=1), so a self-write is refused; writing to a FOREGROUND session's charter is supported. On a row of its own charter a dispatched session may still close it (done/absorbed/dropped), attach evidence, edit the text of an already-closed row, or pass text together with status=done — only an edit that leaves the row LIVE with new text or a reopened status is refused.

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
