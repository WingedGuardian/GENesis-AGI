"""Shared helpers for the cross-session heartbeat row.

STDLIB ONLY, deliberately. Two hooks import this, and one of them
(``session_observer_hook``) runs on EVERY tool call, where a heavy import is paid
per call because each invocation is a fresh process. Measured on this box, with
wide run-to-run spread (~100-220 ms across samples), importing
``genesis.db.crud`` costs of order 100 ms and ``genesis_session_context`` ~93 ms,
against ~0.2 ms for the JSON read below. Treat those as orders of magnitude, not
constants -- an earlier version of this file quoted three different figures for
the same import and every one of them was wrong. Keep it dependency-free.

What lives here and why it is shared rather than inlined: the ``[Concurrent]``
awareness line is assembled by ``proactive_memory_hook`` but the data it needs is
produced elsewhere (the model by a SessionStart hook, the topic by the charter
and ledger). Putting the resolution in one tested place is what stops the two
hooks drifting apart on the format.
"""

from __future__ import annotations

import fcntl
import json
import re
import sys
import time
from pathlib import Path

# `sqlite3` and `urllib.request` are imported LAZILY, inside the two functions
# that need them, and that is a correctness-of-budget decision rather than style.
# Each hook invocation is a FRESH PROCESS, so deferring work inside
# `_maybe_refresh_heartbeat` buys nothing if importing THIS module already paid
# for it. MEASURED marginal cost on top of what the observer hook already loads:
#     both at module scope : 58.0 ms  (urllib.request ~66ms cold, sqlite3 ~10ms)
#     both lazy            : 10.2 ms
# The observer's own stated budget is <50ms for the entire hook, so the eager
# form spent more than the whole budget before doing anything -- on every tool
# call in every session. `pathname2url` is needed only by `ro_uri`, which is
# needed only by `resolve_topic`, which the PostToolUse path never calls at all.

# Self-locate so the sibling import below resolves however this module is
# loaded. Both consumers happen to put this directory on the path before
# importing us, so relying on that WORKED in production -- and failed silently
# everywhere else: throttle_ok swallows ImportError and returns False, so a
# caller that had not done it would simply never refresh, with nothing to
# notice. Importing at module scope makes a genuinely broken import loud.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from hook_input import session_path  # noqa: E402

# Mirrors scripts/genesis_session_context.py, which WRITES this cache at
# SessionStart (CC omits `model` on resume/clear, so it is cached to survive
# that). This module only ever READS it.
#
# A deliberate read-only twin rather than a shared owner: importing that module
# costs ~93 ms for a ~0.2 ms read, and moving its writer here would pull a second
# hook-surface file into an unrelated PR. The failure mode of drift is BENIGN --
# a changed path yields no model, which the COALESCE upsert preserves rather than
# wipes, so the line degrades to today's behaviour instead of showing something
# wrong. tests/test_scripts/test_session_heartbeat_helpers.py asserts both
# constants still agree with that module, so drift fails CI rather than silently.
MODEL_CACHE_FILE = Path.home() / ".genesis" / "cc_session_model.json"

# The ledger statuses that mean "still being worked". Matches the predicate
# genesis_urgent_alerts.py already uses for its charter tag.
_LIVE_LEDGER_STATUSES = ("open", "in_progress")

_TOPIC_MAX = 200
_WHITESPACE_RUN = re.compile(r"\s+")


def ro_uri(db_path: Path) -> str:
    """A WAL-aware read-only SQLite URI for ``db_path``, percent-encoding the path.

    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` silently opens the WRONG
    (empty) database when the path contains a URI-special char: ``?`` or ``#``
    truncates the path there, SQLite reads a different file, and the query fails
    with "no such table". ``pathname2url`` percent-encodes it so the real DB opens;
    idempotent on an ordinary path.

    Twin of ``genesis_urgent_alerts._ro_uri``. Converging the two (and the ~15
    sites still using the raw f-string) is tracked separately -- it is not this
    change's job, and copying the CORRECT version is strictly better than adding
    another naive one.
    """
    from urllib.request import pathname2url  # lazy: ~66ms cold, never on the hot path

    return f"file:{pathname2url(str(db_path))}?mode=ro"


def sanitize_detail(text: str | None, limit: int) -> str:
    """Flatten untrusted text to ONE safe line for the [Concurrent] awareness tag.

    Everything rendered on that line is written by ANOTHER session's model -- a
    charter mission, a ledger item, a tool digest. "Genesis-authored" means
    LLM-authored, so a newline plus a forged ``[Concurrent | ...]`` line would
    otherwise land verbatim in this session's context. The injector already
    refuses to render ``user_summary`` for the same reason; this extends that care
    to the fields it DOES render.

    Measured when written: 0 of 6 live missions and 0 of 102 ledger texts
    contained a newline, so this is closing a structurally-open surface, not an
    observed exploit.
    """
    if not text:
        return ""
    flattened = _WHITESPACE_RUN.sub(" ", text.replace("[", "").replace("]", "")).strip()
    if limit <= 1:
        # `flattened[: limit - 1] + "…"` is longer than `limit` at 0 and 1
        # (at 0 the slice is [: -1], i.e. almost everything). Unreachable from
        # the three real call sites (200/90/80), guarded so it stays true.
        return "…"[:limit] if limit > 0 else ""
    if len(flattened) <= limit:
        return flattened
    # Mark the cut, as genesis_urgent_alerts._emit_charter_tag does — an
    # unmarked truncation reads as a complete sentence that simply stops.
    return flattened[: limit - 1] + "…"


def cached_model(session_id: str) -> str | None:
    """The model id a SessionStart hook cached for ``session_id``, or None.

    Returns None (never "") on any miss so callers can pass it straight to the
    upsert, where COALESCE preserves whatever is already stored.
    """
    if not session_id:
        return None
    try:
        data = json.loads(MODEL_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get(session_id) or "") or None


def resolve_topic(db_path: Path, session_id: str, *, limit: int = _TOPIC_MAX) -> str | None:
    """What this session is WORKING ON: charter mission, else newest live ledger item.

    Both sources are Genesis-authored, which is the point -- the injector must not
    surface another session's raw user text, so ``origin_prompt`` is deliberately
    NOT a fallback here even though it is always populated and the charter tag
    elsewhere does use it.

    THE RETURN CONTRACT IS THREE-VALUED, and the distinction is load-bearing now
    that the column is COALESCE-preserved:
      * a string  -- the topic.
      * ``""``    -- READ SUCCESSFULLY, and there is genuinely nothing to report
                     (no mission, no live ledger item). "" is not NULL, so the
                     upsert OVERWRITES with it and the stale topic is CLEARED.
      * ``None``  -- could NOT read (missing db, missing tables, locked, no
                     session id). The upsert then PRESERVES whatever is stored.
    Collapsing the middle case into None is a real bug, not a nicety: a session
    that finishes its last ledger item would keep advertising it to every peer
    for the rest of its life, and the new liveness refresh would keep stamping
    that line as freshly confirmed.

    Read-only and bounded: this runs on the UserPromptSubmit path.
    """
    if not session_id:
        return None  # cannot even attempt -> preserve
    try:
        import sqlite3  # lazy: ~10ms, and never needed on the PostToolUse path

        if not db_path.exists():
            return None
        conn = sqlite3.connect(ro_uri(db_path), uri=True, timeout=0.5)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            row = conn.execute(
                "SELECT mission FROM session_charters WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            mission = sanitize_detail(row[0] if row else None, limit)
            if mission:
                return mission
            # Statuses written out literally rather than interpolated from the
            # constant: an f-string here trips ruff S608 (it cannot tell the
            # placeholders are safe), and genesis_urgent_alerts.py already spells
            # the same predicate this way. _LIVE_LEDGER_STATUSES documents it and
            # the test below pins the two in step.
            item = conn.execute(
                "SELECT text FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open','in_progress')"
                # in_progress first: that is the item actually being worked, not
                # merely queued. Then newest.
                " ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END,"
                " created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            # NOT `or None`: a successful read with nothing to report must return
            # "" so the stale topic is cleared. See the three-valued contract above.
            return sanitize_detail(item[0] if item else None, limit)
        finally:
            conn.close()
    except Exception:
        # Could not read -> None -> the upsert PRESERVES the stored topic. Never
        # worth a failed hook, and never worth clearing a good value either.
        return None


# ---------------------------------------------------------------------------
# Liveness refresh (PostToolUse)
# ---------------------------------------------------------------------------
#
# WHY a second write path exists at all. `get_active_sync` hides any row older
# than _STALE_THRESHOLD (10 min), and the only writer is UserPromptSubmit -- so a
# session working heads-down for twenty minutes DISAPPEARS from every peer's
# awareness while it is at its busiest. Raising the threshold fails the other
# way: ended sessions would linger as though live. A cheap activity write is what
# resolves that honestly.
#
# It refreshes `updated_at` ONLY. `topic` is near-static (it changes when the
# mission does) and `model` never changes within a session, so recomputing them
# per tool call would buy nothing; the COALESCE conflict clause is what makes a
# field-free upsert a pure liveness touch rather than a wipe.

SESSIONS_DIR = Path.home() / ".genesis" / "sessions"
THROTTLE_WINDOW_S = 60.0


def throttle_ok(
    session_id: str,
    *,
    window_s: float | None = None,
    sessions_dir: Path | None = None,
) -> bool:
    """True at most once per ``window_s`` per session. Cheap on the false path.

    PostToolUse fires on EVERY tool call, so the common case must cost one
    ``stat()`` and nothing else -- no database, no non-stdlib import. Everything
    expensive belongs behind this.

    Concurrency has two levels and both are needed. ACROSS sessions there is no
    shared state at all: the stamp path is keyed by session id. WITHIN one
    session, Claude Code issues tool calls in PARALLEL, so two hook processes can
    both see a stale stamp in the same instant; the non-blocking flock plus a
    re-check UNDER the lock is what makes exactly one of them win. Without the
    re-check, both would pass the initial stat and both would write.

    Fails CLOSED (returns False) on any error: a missed liveness refresh costs a
    peer's awareness line, which is strictly better than a hook that raises.
    """
    if not session_id:
        return False
    window = THROTTLE_WINDOW_S if window_s is None else window_s
    base = SESSIONS_DIR if sessions_dir is None else sessions_dir
    try:
        # session_path (imported at module scope) carries the shared
        # path-component validation: session_id arrives from hook stdin and must
        # never escape the sessions dir. Re-deriving that check here is exactly
        # what its docstring tells callers not to do.
        stamp = session_path(base, session_id, "heartbeat.stamp")
        if stamp is None:
            return False
        now = time.time()
        try:
            if now - stamp.stat().st_mtime < window:
                return False  # the ONLY cost on ~99% of tool calls
        except OSError:
            pass  # no stamp yet -> fall through and try to claim
        stamp.parent.mkdir(parents=True, exist_ok=True)
        # "a+" so the file is created if absent AND readable. The claim time is
        # the file's CONTENT, not its mtime: opening for append CREATES the file
        # with mtime=now, so an mtime re-check under the lock would read a
        # just-created stamp as fresh and the FIRST call for a session could
        # never claim its window (measured before this was wired). Content
        # cannot be faked by creation -- an empty file parses to 0.0 and claims.
        with open(stamp, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False  # a sibling hook process is claiming this window
            try:
                fh.seek(0)
                try:
                    last = float(fh.read().strip() or 0.0)
                except ValueError:
                    last = 0.0  # corrupt stamp -> treat as never claimed
                if now - last < window:
                    return False  # a sibling claimed it between our stat and our lock
                fh.seek(0)
                fh.truncate()
                fh.write(repr(now))
                fh.flush()
                return True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:
        return False
