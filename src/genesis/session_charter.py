"""Session-charter markdown mirror — the canonical renderer.

The charter's canonical store is the DB (``session_charters`` +
``session_ledger``, migration 0058); ``~/.genesis/sessions/<sid>/charter.md``
is the human-readable mirror regenerated after every write. This module is
the runtime-side renderer used by the ledger MCP tools and the backfill
script.

NOTE: ``scripts/genesis_precompact.py`` carries an intentionally duplicated
``_charter_md`` — the PreCompact hook is deliberately stdlib-only (fail-open
under a 5s budget) and must not import the genesis package, and runtime code
must not import from ``scripts/``. A parity test
(tests/test_scripts/test_precompact_charter.py) pins both renderers to
byte-identical output, so drift fails CI immediately.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The canonical mirror root. Defined here, with the renderer that writes it, so
# callers do not each keep their own copy of the path.
SESSIONS_DIR = Path.home() / ".genesis" / "sessions"

# A session id is used as a PATH SEGMENT, so it must not be able to leave the
# sessions tree. Charset follows the in-repo precedent
# (subsystem_traps_hook.py) rather than a strict UUID: measured ids on live
# installs are not all hex-UUIDs (a `wt-` prefixed form exists), and this still
# rejects `/`, `\`, `..`, NUL and the empty string.
#
# `\A…\Z` and 255, character-for-character identical to
# scripts/hooks/hook_input.py:_SESSION_ID_RE — this is the src/ chokepoint for
# the same rule, and callers in src/ import it from here rather than keeping a
# fourth copy. Both halves of that sentence were wrong before and were caught
# by review: `^…$` accepts a TRAILING NEWLINE in Python (`'abc\n'` matched),
# which would have created a session directory whose name ends in a newline,
# and the 128 cap rejected ids the upstream guard admits. A regex copied "to
# mirror" a sibling must be diffed against it, not eyeballed.
_SAFE_SESSION_ID = re.compile(r"\A[A-Za-z0-9_-]{1,255}\Z")

_STATUS_MARKS = {
    "open": " ",
    "in_progress": "~",
    "done": "x",
    "absorbed": "a",
    "dropped": "d",
}


def charter_md(charter: dict, ledger: list[dict] | None = None) -> str:
    """Render charter.md from a charter row + optional ledger rows.

    Stub rows (origin not yet filled — an MCP write preceded the session's
    first compaction) render with an empty Origin section rather than the
    string "None".
    """
    lines = [
        f"# Session Charter — {charter.get('session_id', '')}",
        "",
        f"- **Born:** {charter.get('origin_ts') or 'unknown'}",
        f"- **Compactions:** {charter.get('compaction_count', 0)}",
        f"- **Charter created:** {charter.get('created_at', '')}",
        "",
        "## Origin (immutable)",
        "",
        str(charter.get("origin_prompt") or ""),
    ]
    mission = charter.get("mission")
    if mission:
        lines += ["", "## Mission", "", str(mission)]
    pointers = charter.get("pointers") or []
    if pointers:
        lines += ["", "## Pointers", ""]
        lines += [f"- {p}" for p in pointers]
    if ledger:
        lines += ["", "## Ledger", ""]
        for item in ledger:
            mark = _STATUS_MARKS.get(str(item.get("status", "open")), " ")
            lines.append(f"- [{mark}] {item.get('text', '')}")
    return "\n".join(lines) + "\n"


def session_dir(sessions_dir: Path, session_id: str) -> Path | None:
    """The session's mirror directory, or ``None`` if the id is not path-safe.

    Returns the PATH rather than a boolean deliberately: a bool leaves every
    caller to decide what to skip on rejection, which is an open set and the
    shape that let this class recur. Handing back the resolved directory (or
    nothing) makes the safe use the only convenient one.
    """
    if not _SAFE_SESSION_ID.match(session_id or ""):
        logger.warning(
            "refusing charter mirror path for unsafe session id %r", session_id
        )
        return None
    return sessions_dir / session_id


def write_charter_md(
    sessions_dir: Path,
    session_id: str,
    charter: dict,
    ledger: list[dict] | None = None,
) -> None:
    """Best-effort mirror write: the DB is canonical, a failed mirror only
    means charter.md goes stale until the next write regenerates it.

    The id is validated before it becomes a path segment. Without that,
    ``sessions_dir / "../.."`` escapes the sessions tree and this function
    happily ``mkdir(parents=True)``s and writes model-controlled content there
    — reproduced, not theorised.
    """
    target = session_dir(sessions_dir, session_id)
    if target is None:
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / "charter.md").write_text(charter_md(charter, ledger), encoding="utf-8")
    except OSError as exc:
        logger.warning("charter.md mirror write failed for %s: %s", session_id, exc)


async def refresh_mirror(
    db, session_id: str, sessions_dir: Path | None = None
) -> None:
    """Regenerate charter.md after a ledger/charter mutation.

    One definition, shared by the MCP tools and the ambient extractor. A second
    copy would be free to drift, and the two callers must agree — a promoted row
    that never reaches the mirror is invisible to the next window.

    *sessions_dir* is a PARAMETER rather than a hardcoded constant so a caller —
    a test above all — can point it somewhere safe. Reading the module constant
    directly makes the destination unredirectable, and a suite that cannot
    redirect it writes into the operator's real ``~/.genesis/sessions``.

    Best-effort by contract: the DB is canonical, so a failed mirror only means
    charter.md is stale until the next write. Note that `db` must carry a row
    factory — ``crud.get`` builds ``dict(row)`` — and that this `except` is
    exactly what hid that from us once already.
    """
    try:
        from genesis.db.crud import session_charters as crud

        charter = await crud.get(db, session_id)
        if charter is None:
            return
        ledger = await crud.ledger_list(db, session_id)
        write_charter_md(sessions_dir or SESSIONS_DIR, session_id, charter, ledger)
    except Exception:
        logger.warning("charter.md refresh failed for %s", session_id, exc_info=True)
