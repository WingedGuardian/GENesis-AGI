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
import os
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
_TAG_GRAMMAR_CHARS = str.maketrans({"[": "", "]": "", "|": "/", "\u00b7": "-"})
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
    # Strip the characters the TAG GRAMMAR owns, not only the bracket pair.
    # Brackets alone stop a forged LINE; the grammar is
    # `[Concurrent | <src> <model> | <id>] <topic> - <digest>`, so a peer value
    # containing "|" still forges an extra FIELD inside the surviving line --
    # including the id position, which a reader attributes to the tag itself.
    # Substituted rather than deleted so the text stays readable.
    flattened = _WHITESPACE_RUN.sub(" ", text.translate(_TAG_GRAMMAR_CHARS)).strip()
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
    # The ROUTED identity outranks the cache. scripts/gmodel launches a peer
    # window with os.execve(claude, ..., env) carrying GENESIS_ROSTER_MODEL,
    # exactly because CC's self-reported model would otherwise say "Claude"
    # (gmodel:110-112); genesis_session_context.py:194 already gives that var
    # precedence for this session's own header, but caches only _hook_model
    # (:288) -- so without this a peer session advertises itself to every OTHER
    # session as Claude, wrong for precisely the sessions whose model matters.
    # Read from env rather than a second cache because hooks inherit the
    # launcher's environment (session_observer_hook.py:25 reads a launcher-set
    # var the same way). Checked BEFORE the session-id guard: the var describes
    # THIS process, so it is valid even when the id is missing.
    roster = os.environ.get("GENESIS_ROSTER_MODEL", "").strip()
    if roster:
        return roster
    if not session_id:
        return None
    try:
        data = json.loads(MODEL_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get(session_id) or "") or None


def _is_newer(a: str | None, b: str | None) -> bool:
    """True iff timestamp ``a`` parses, ``b`` parses, and a > b.

    PARSED, not string-compared -- but NOT for the reason an earlier version of
    this docstring gave. It claimed that because ``.isoformat()`` omits the
    microseconds at exactly zero, and "+" sorts before ".", a lexical compare
    would misorder a whole-second stamp against a same-second one. Both facts
    are true and the CONSEQUENCE IS NOT: the whole-second stamp really is the
    earlier instant, so the two orderings agree. MEASURED over 200,000 random
    same-format pairs mixing zero and non-zero microseconds: 0 disagreements.
    The claim is corrected rather than deleted because a maintainer who fails to
    reproduce a stated bug reasonably concludes the guard is unnecessary.

    The real divergence is a HETEROGENEOUS OFFSET -- "…T00:30:00+00:00" against
    "…T01:00:00+01:00", where the second is the earlier instant but sorts later
    lexically -- and a naive stamp, where a string compare silently succeeds
    against an aware one. Parsing catches the first and raises TypeError on the
    second, which lands in the False branch below. Nothing here writes an offset
    other than UTC today; this is defence against a future writer that does.

    Returns False whenever either side is missing or unparseable. That is the
    conservative direction on purpose: "cannot compare" must leave the previous
    ordering (extracted summary first) intact rather than promote a mission of
    unknown age.
    """
    if not a or not b:
        return False
    try:
        from datetime import datetime  # lazy: never needed on the PostToolUse path

        # STRICT `>`: on an exact tie the summary keeps precedence. A tie
        # means the two writes are indistinguishable in time, and the tie-break
        # should not silently promote the mission.
        return datetime.fromisoformat(a) > datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return False


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
            # FIRST: the topic Genesis ALREADY extracts. memory/
            # extraction_job.py writes cc_sessions.topic via
            # crud.cc_sessions.update_topic_and_keywords.
            #
            # The justification is COVERAGE and QUALITY, deliberately NOT
            # freshness. An earlier draft of this comment claimed the extracted
            # topic was "~5 min old" -- that was a sample taken shortly after an
            # extraction run, and it does not hold at steady state: the job runs
            # on `memory_extraction_hours` (default 2), and re-measured an hour
            # later the same four sessions were 69-73 minutes stale, heading for
            # 120. What DOES hold: only 7 of 24 charters carry a mission at all,
            # and when present it is often the founding blob rather than current
            # work, so the derivation below degrades to a raw ledger row for most
            # sessions. A 2-hour-old sentence describing the session beats that,
            # and beats the NULL every peer sees today.
            #
            # Rejected: ordering by recency instead (mission when
            # session_charters.updated_at is newer). That column is bumped by
            # set_pointers and by the charter upsert, not only set_mission, so it
            # is a charter-row timestamp rather than a mission timestamp -- a
            # pointers edit would promote a stale founding mission over a good
            # extracted topic. Doing it properly needs a real mission_updated_at.
            # Cheaper too: the value is already computed.
            # CONTENT ONLY from this table -- its `model` reads "unknown" for
            # 705/897 rows and its status/last_activity_at are stale for live
            # rows, which is exactly why session_heartbeats exists.
            # Decide table-PRESENCE explicitly instead of inferring it from an
            # exception class. `sqlite3.Error` is the base of both "no such
            # table" (this source is absent -- fall through, correct) and
            # "database is locked" / "disk I/O error" / "malformed image" (the
            # read FAILED -- must return None so the upsert PRESERVES). Catching
            # the class to mean the former let a transient failure fall through
            # to a charter/ledger with nothing to report, return "", and CLEAR a
            # good stored topic. An absent table is the fresh-install case,
            # where falling through is right.
            extracted = ""
            extracted_at = None
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cc_sessions'"
            ).fetchone():
                # No local handler: a failure here propagates to the outer
                # `except` -> None -> PRESERVE, which is the contract.
                # topic_updated_at, NOT last_extracted_at. The watermark is
                # advanced by a DIFFERENT crud function on every extraction
                # pass, including passes that write no topic (measured: 219/899
                # live rows carry a watermark with no topic) -- using it as the
                # topic's age would commit, on this side of the comparison,
                # exactly the defect mission_updated_at exists to avoid.
                # Column-probed like the charter side: absent -> NULL -> "cannot
                # compare" -> summary first, the pre-migration behaviour.
                cc_cols = {r[1] for r in conn.execute("PRAGMA table_info(cc_sessions)")}
                has_ts = "topic_updated_at" in cc_cols
                topic_ts = "topic_updated_at" if has_ts else "NULL"
                # Order by WHEN THE TOPIC WAS WRITTEN, falling back to insertion
                # order. rowid alone answers "most recently INSERTED row", which
                # is a different question: a row created first can carry the
                # topic written last. That distinction has no live instances --
                # 0 of 747 sessions have more than one lifecycle row, and 0 have
                # more than one row carrying a topic -- but the column exists
                # precisely to date the topic, so using it costs nothing and is
                # right by construction rather than by the population happening
                # to be degenerate.
                #
                # DESC puts NULLs last in SQLite, so rows stamped by a real
                # write win over pre-migration rows; `rowid DESC` then breaks
                # ties and is the whole ordering before migration 0091, where
                # the column does not exist and this degrades to the old
                # behaviour exactly.
                order = "topic_updated_at DESC, rowid DESC" if has_ts else "rowid DESC"
                row = conn.execute(
                    f"SELECT topic, {topic_ts} FROM cc_sessions"  # noqa: S608 - literal, ours
                    " WHERE cc_session_id = ?"
                    " AND topic IS NOT NULL AND TRIM(topic) != ''"
                    f" ORDER BY {order} LIMIT 1",  # noqa: S608 - literal, ours
                    (session_id,),
                ).fetchone()
                if row:
                    extracted = sanitize_detail(row[0], limit)
                    extracted_at = row[1]

            # `mission_updated_at` arrives with migration 0091; a database that
            # has not run it yet must still work, so the column is selected only
            # when present. Absent -> None -> "cannot compare" -> summary first,
            # which is the behaviour before that migration.
            charter_cols = {r[1] for r in conn.execute("PRAGMA table_info(session_charters)")}
            row = conn.execute(
                "SELECT mission, mission_updated_at FROM session_charters WHERE session_id = ?"
                if "mission_updated_at" in charter_cols
                else "SELECT mission, NULL FROM session_charters WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            mission = sanitize_detail(row[0] if row else None, limit)
            mission_at = row[1] if row else None

            # RECENCY decides between the two, not a fixed preference. The
            # summary is written on a multi-hour cycle; the mission is set the
            # moment a session declares a pivot. Whichever is the more recent
            # STATEMENT of what the session is doing wins. When the comparison
            # is impossible -- no stamp (a pre-0091 row, whose mission age is
            # genuinely unknown), an unparseable stamp, or no extraction to
            # compare against -- `_is_newer` returns False and the summary keeps
            # precedence, which is both the safe direction and the prior
            # behaviour. `updated_at` is NOT usable for this: set_pointers and
            # the charter upsert bump it too, so a pointer edit would promote a
            # stale founding mission.
            if mission and _is_newer(mission_at, extracted_at):
                return mission
            if extracted:
                return extracted
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
            # `0 <=` is load-bearing. A BACKWARDS clock step (VM restore, NTP
            # correction) makes this difference NEGATIVE, and a negative number
            # is always < window -- so the unguarded form reads "inside the
            # window" and refuses every write until wall time catches up. A
            # correction larger than _STALE_THRESHOLD would make an actively
            # working session vanish from its peers entirely. Same direction the
            # repo already settled at genesis_urgent_alerts.py:350
            # ("future marker -> do not suppress; emit").
            #
            # `now` is read before this stat() too, so a sibling refreshing the
            # stamp in between yields a negative difference and this check falls
            # through. That staleness is DELIBERATELY tolerated here and is not
            # the defect fixed below: the cost is one open+flock, and the
            # under-lock check is authoritative. Do not "fix" it by re-reading
            # the clock -- that adds a syscall to the ~99% path whose whole job
            # is to stay cheap.
            if 0 <= now - stamp.stat().st_mtime < window:
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
                # RE-READ THE CLOCK, and read it HERE. The `now` captured before
                # the flock is stale by exactly the interval this branch exists
                # to judge. A sibling that won the lock wrote ITS OWN, later
                # timestamp, so a loser's pre-lock `now` sits MICROSECONDS
                # BEHIND it and ``now - last`` goes NEGATIVE -- whereupon the
                # backwards-clock guard below reads "not inside the window" and
                # the loser claims too, the flock excluding nothing in the one
                # case it exists for. MEASURED: a sibling winning by 50us
                # produced a second winner every time, and CI caught it as
                # "expected exactly one winner, got 2". A clock read taken AFTER
                # the lock is never EARLIER than a write that completed under it,
                # so the race yields a NON-NEGATIVE difference (refused) while a
                # genuine backwards step still yields a negative one (claimed) --
                # both properties, one clock read.
                #
                # Non-negative, NOT positive: time.time() is a double whose ULP
                # at epoch magnitude is ~2.4e-07s, so two reads closer together
                # than that return the SAME float and the difference is exactly
                # 0.0. `0 <=` is therefore load-bearing for the RACE as well as
                # for the backwards step below. Tightening it to `0 <` reopens
                # the tie SILENTLY -- that mutant passed every test in
                # tests/test_scripts/test_session_heartbeat_throttle.py until the
                # tie case was written for it.
                now = time.time()
                # Same backwards-clock guard as the mtime check above; this
                # call site needs it independently, since a stamp can carry a
                # future CLAIM TIME while its mtime is old.
                if 0 <= now - last < window:
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


# ---------------------------------------------------------------------------
# Roster identity helpers -- stdlib-only, called ONLY where a heartbeat write
# already happens (behind throttle_ok / per prompt), so their cost rides a
# path that is already paying for a sqlite connect.
# ---------------------------------------------------------------------------


def claude_ancestor_pid(
    start_pid: int | None = None, proc_root: str = "/proc"
) -> int | None:
    """The owning ``claude`` session pid for this hook process, or None.

    Hooks run as ``claude -> launcher shell -> python`` (the launcher execs, but
    CC's own hook command runs through a shell), so ``claude`` is an ANCESTOR,
    not necessarily the parent -- walk the ppid chain to the first
    ``comm == "claude"``. Deliberately duplicated from
    ``scripts/genesis_urgent_alerts.py::_claude_ancestor_pid`` rather than
    imported: that module has module-scope side effects, and this one must stay
    a dependency-free leaf. ``/proc/<pid>/{comm,stat}`` are world-readable (no
    ptrace gate). Bounded walk (never loops on a cycle); fail-open None on any
    read/parse failure. The stat parse splits after the LAST ``')'`` because
    comm is parenthesised and may itself contain spaces or ``')'``.
    """
    try:
        pid = start_pid if start_pid is not None else os.getppid()
        for _ in range(16):
            if not isinstance(pid, int) or pid <= 1:
                return None
            base = Path(proc_root) / str(pid)
            comm = (base / "comm").read_text().strip()
            if comm == "claude":
                return pid
            stat = (base / "stat").read_text()
            after = stat.rsplit(")", 1)[1].split()
            ppid = int(after[1])
            if ppid == pid:
                return None
            pid = ppid
        return None
    except Exception:
        return None


def proc_start_iso(pid: int, proc_root: str = "/proc") -> str | None:
    """Wall-clock start time of a pid as ISO-8601 UTC, or None.

    starttime is field 22 of ``/proc/<pid>/stat`` (in clock ticks after the
    comm field), anchored to btime from ``/proc/stat``. Same math as
    ``genesis.observability.cc_slots.read_proc_start_iso`` -- duplicated here
    because this module must not import genesis (stdlib-only hook leaf).
    Written as a PAIR with pid so a reader can reject a recycled pid.
    """
    try:
        stat = (Path(proc_root) / str(pid) / "stat").read_text()
        fields = stat.rsplit(")", 1)[1].split()
        ticks = int(fields[19])  # starttime = stat field 22; 20th after comm
        btime = None
        for line in (Path(proc_root) / "stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None
        hz = os.sysconf("SC_CLK_TCK")
        epoch = btime + ticks / hz
        from datetime import UTC, datetime

        return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")
    except Exception:
        return None


def resolve_git_branch(cwd: str) -> str | None:
    """The branch of ``cwd``'s repository, by pure file reads -- no subprocess.

    Walks UP from cwd (bounded) to the first ``.git`` entry; a ``.git`` FILE is
    a linked worktree's ``gitdir:`` indirection (resolved relative to the dir
    holding it); reads HEAD there. Returns the branch name (only the
    ``refs/heads/`` prefix stripped -- branch names contain ``/``), ``""`` for
    KNOWN-no-branch (detached HEAD, or not a repo at all), and None when
    resolution FAILED (unreadable, malformed) -- the table's three-valued
    contract depends on that distinction.
    """
    try:
        if not cwd:
            return None
        p = Path(cwd)
        if not p.is_dir():
            return None
        for _ in range(30):
            entry = p / ".git"
            if entry.is_dir():
                gitdir = entry
                break
            if entry.is_file():
                # BOUNDED read: both this file and HEAD below are inside a
                # directory the session cd'd into — a cloned third-party repo
                # is a normal workflow, so their content is attacker-
                # influenced, and an unbounded read_text() of a crafted
                # multi-GB file is an allocation this zero-swap box cannot
                # absorb (security review). 512B covers any legitimate
                # gitdir line with room to spare.
                with open(entry, "rb") as fh:
                    first = fh.read(512).decode("utf-8", "replace")
                first = first.splitlines()[0].strip() if first else ""
                if not first.startswith("gitdir:"):
                    return None
                target = first[len("gitdir:"):].strip()
                if not target or len(target) >= 480:
                    return None  # truncated-by-bound or empty: unknown
                gitdir = (entry.parent / target).resolve()
                break
            if p.parent == p:
                return ""  # walked to filesystem root: known not-a-repo
            p = p.parent
        else:
            # Depth bound exhausted before reaching a .git OR the root: we
            # gave up, we did not learn anything — per the three-valued
            # contract that is a failed resolution, not a known no-branch.
            return None
        with open(gitdir / "HEAD", "rb") as fh:
            head = fh.read(512).decode("utf-8", "replace")
        head = head.splitlines()[0].strip() if head else ""
        if head.startswith("ref: "):
            ref = head[len("ref: "):]
            if ref.startswith("refs/heads/"):
                branch = ref[len("refs/heads/"):]
                # A branch name this long is not a value we accept — the
                # store gets None (unknown), never a silent truncation
                # (select-don't-amputate; render caps at 40 anyway).
                return branch if 0 < len(branch) <= 200 else None
            return ""  # a ref outside heads (bisect etc.): no branch
        return ""  # detached HEAD
    except Exception:
        return None
