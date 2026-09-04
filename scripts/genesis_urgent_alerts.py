#!/usr/bin/env python3
"""UserPromptSubmit hook: per-prompt session-state tags.

Runs before each user message is processed. Emits one-line session-state
tags to stdout (the harness prepends them to the prompt as context) and
maintains session-scoped state files:
1. Absolute timestamp / session clock (temporal awareness)
2. Charter drift tag ([Charter: … | open: N]; chartered sessions only)
3. Deploy nudge — only when main has MOVED under this session (its MCP
   subprocesses snapshot code at spawn and never reload, and there is no
   auto-restart, so the session is told what landed); once per deploy
4. Rolling buffer of the last few user messages (for session bookmarks)
5. /shelve|/unshelve soft hint

This hook does NOT inject Telegram-style OUTREACH alerts into the prompt —
those flooded every prompt as a duplicate channel and were removed
2026-04-09 (delivered via the outreach pipeline instead). The tags above are
lightweight session STATE, each fail-open, not alerts. Filename retained to
avoid churn in .claude/settings.json — TODO: rename to
genesis_session_state.py in a follow-up cleanup.

Reads hook input from stdin as JSON:
  {"session_id": "...", "prompt": "...", ...}

Reads session start timestamp from ~/.genesis/session_start (written by
the SessionStart hook). Falls back to a 10-minute lookback if file missing.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import pathname2url

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import session_path  # noqa: E402

# Load secrets.env so USER_TIMEZONE and other env vars are available
# before any genesis module imports (which may read os.environ at import time).
_SECRETS_PATH = Path(__file__).resolve().parent.parent / "secrets.env"
if _SECRETS_PATH.is_file():
    try:
        from dotenv import load_dotenv
        load_dotenv(str(_SECRETS_PATH), override=False)
    except ImportError:
        pass

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_SESSION_START_FILE = Path.home() / ".genesis" / "session_start"
_GENESIS_DIR = Path.home() / ".genesis"
_FALLBACK_LOOKBACK_MINUTES = 10
_MAX_BUFFER_LINES = 5
_MAX_MSG_LENGTH = 200

_SHELVE_PATTERN = re.compile(r"/(?:shelve|unshelve)\b", re.IGNORECASE)

# The deploy nudge speaks ONCE PER DEPLOY, not on a clock: it stamps the sha it
# announced and stays silent until HEAD moves again. A deploy is an EVENT, so the
# periodic re-nudge the old update_history-keyed banner used would repeat the
# same line for the life of the session now that detection actually fires.
_DEPLOY_STAMP = "deploy_notified"
# `git rev-parse HEAD` measured at 3.86 ms on the reference install. The bound is
# BUDGETED, not nominal: this hook's own timeout in .claude/settings.json is 10s
# and it must also cover two SQLite opens, so two git calls may not be allowed to
# consume it between them. 1.5s is still ~390x the measured cost, and the only
# thing that makes git slower is a wedged/read-only filesystem.
_GIT_TIMEOUT_S = 1.5
# Keep the notice to one line; the rest collapse into "+N more".
_PR_CAP = 5
_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")


def _is_sha(value: str | None) -> bool:
    """True iff ``value`` is a bare hex commit ref.

    THE chokepoint — every sha is validated through here, and validation happens
    where a value ENTERS (a git argv, the emitted line), never only where it is
    printed. That ordering is the point: a spawn commit read from the file plane
    reaches ``git log`` as part of the ``<spawn>..<head>`` operand, and git
    accepts leading-dash operands as OPTIONS. MEASURED with git 2.43.0: a value
    of ``--output=<path>`` makes ``git log`` create/truncate ``<path>..HEAD`` and
    exit 0 with empty stdout — an arbitrary-write primitive that then reads as a
    silent no-op. The file plane is written only from ``git rev-parse`` output
    today, so this is defense in depth; but it is defense the module previously
    only CLAIMED to have, because the guard sat downstream of the git call.
    """
    return bool(value) and _SHA_RE.fullmatch(value) is not None


def _get_session_start() -> str:
    """Get session start ISO timestamp. Falls back to 10 min ago."""
    if _SESSION_START_FILE.exists():
        try:
            return _SESSION_START_FILE.read_text().strip()
        except Exception:
            pass
    return (datetime.now(UTC) - timedelta(minutes=_FALLBACK_LOOKBACK_MINUTES)).isoformat()


def _format_day_time(iso: str) -> str:
    """Format ISO timestamp as 'Mon 14:32' in user's timezone."""
    try:
        from genesis.util.tz import fmt as _tz_fmt

        return _tz_fmt(iso, "%a %H:%M")
    except ImportError:
        # Fallback if genesis not importable
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%a %H:%M")
        except (ValueError, TypeError):
            return "unknown"
    except (ValueError, TypeError):
        return "unknown"


def _session_dir(session_id: str) -> Path | None:
    """The per-session state dir, or None when the id is not a safe path component.

    Every path in this module that interpolates ``session_id`` goes through here,
    so a traversal id (``../..``) cannot escape ``~/.genesis/sessions/`` — two of
    the call sites ``mkdir(parents=True)``, so an escape would CREATE directories.
    One chokepoint rather than four hand-copied checks (see hook_input).
    """
    return session_path(_GENESIS_DIR / "sessions", session_id)


def _emit_temporal_context(session_id: str, now: datetime) -> None:
    """Emit absolute timestamp context line."""
    session_start_iso = _get_session_start()
    started = _format_day_time(session_start_iso)

    # Read last prompt time from session-scoped state
    session_dir = _session_dir(session_id)
    last_prompt_file = session_dir / "last_prompt_time" if session_dir else None
    last_msg = ""
    if last_prompt_file is not None and last_prompt_file.exists():
        with contextlib.suppress(OSError):
            last_msg = _format_day_time(last_prompt_file.read_text().strip())

    try:
        from genesis.util.tz import fmt as _tz_fmt

        clock = _tz_fmt(now.isoformat())
    except ImportError:
        clock = now.strftime("%a %Y-%m-%d %H:%M UTC")
    parts = [f"Clock: {clock}", f"Session: {session_id[:8]}", f"Started: {started}"]
    if last_msg:
        parts.append(f"Last msg: {last_msg}")

    print(f"[{' | '.join(parts)}]")
    sys.stdout.flush()


def _buffer_message(session_id: str, prompt: str, now: datetime) -> None:
    """Append user message to session-scoped rolling buffer."""
    session_dir = _session_dir(session_id)
    if session_dir is None:
        return
    session_dir.mkdir(parents=True, exist_ok=True)

    messages_file = session_dir / "messages.jsonl"
    last_prompt_file = session_dir / "last_prompt_time"

    # Write current timestamp for next temporal context
    with contextlib.suppress(OSError):
        last_prompt_file.write_text(now.isoformat())

    # Append truncated message to rolling buffer
    entry = json.dumps({
        "text": prompt[:_MAX_MSG_LENGTH],
        "timestamp": now.isoformat(),
    })

    try:
        # Read existing lines, keep last N-1, append new
        existing: list[str] = []
        if messages_file.exists():
            existing = messages_file.read_text().strip().splitlines()
        existing = existing[-((_MAX_BUFFER_LINES) - 1):]
        existing.append(entry)
        messages_file.write_text("\n".join(existing) + "\n")
    except OSError:
        pass


def _check_shelve_hint(prompt: str) -> None:
    """Detect /shelve or /unshelve and emit a soft hint."""
    if _SHELVE_PATTERN.search(prompt):
        print(
            "The user may be asking to bookmark this session. "
            "If that's their intent, use the bookmark_shelve or "
            "bookmark_unshelve MCP tool."
        )
        sys.stdout.flush()


def _ro_uri(db_path: Path) -> str:
    """A WAL-aware read-only SQLite URI for ``db_path``, percent-encoding the path.

    ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)`` silently opens the WRONG
    (empty) database if the filesystem path contains a URI-special char: a ``?`` or
    ``#`` truncates the path there (verified — SQLite then reads a different file and
    the query hits "no such table"), so the read fails closed to ``None`` on an install
    whose repo path contains one. ``pathname2url`` percent-encodes the path (``?``→%3F,
    ``#``→%23, space→%20, ``%``→%25); SQLite decodes it back, so the real DB opens.
    Idempotent on an ordinary path (no special chars → unchanged).
    """
    return f"file:{pathname2url(str(db_path))}?mode=ro"


def _emit_charter_tag(session_id: str) -> None:
    """One-line drift tag: [Charter: <mission|origin snippet> | open: N].

    Read-only stdlib sqlite3, mode=ro URI (WAL-aware — never immutable=1,
    which misses un-checkpointed writes), 500ms connect / 300ms busy budget:
    this runs on EVERY prompt and must never cost the user anything.
    Omitted entirely when the session has no charter row yet (pre-first-
    compaction — the origin is still in context), the DB/table is missing
    (un-migrated install), or the DB is locked. open counts open+in_progress
    ledger rows; "open: 0" IS shown for a chartered session — a clear ledger
    is signal.
    """
    try:
        root = os.environ.get("GENESIS_REPO_ROOT", "")
        db = (Path(root) if root else Path.home() / "genesis") / "data" / "genesis.db"
        if not db.exists():
            return
        conn = sqlite3.connect(_ro_uri(db), uri=True, timeout=0.5)
        try:
            conn.execute("PRAGMA busy_timeout=300")
            row = conn.execute(
                "SELECT mission, origin_prompt FROM session_charters"
                " WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            (open_n,) = conn.execute(
                "SELECT COUNT(*) FROM session_ledger WHERE session_id = ?"
                " AND status IN ('open','in_progress')",
                (session_id,),
            ).fetchone()
        finally:
            conn.close()
        mission, origin = row
        label = (mission or "").strip()
        if label:
            label = label[:80] + ("…" if len(label) > 80 else "")
        else:
            first_line = next(
                (ln for ln in (origin or "").strip().splitlines() if ln.strip()), ""
            )
            if not first_line:
                return
            snippet = first_line[:60] + ("…" if len(first_line) > 60 else "")
            label = f'origin: "{snippet}"'
        print(f"[Charter: {label} | open: {open_n}]")
        sys.stdout.flush()
    except Exception:
        return  # fail-open: a tag miss must never surface as an error


def _claude_ancestor_pid() -> int | None:
    """The owning interactive ``claude`` session pid for this hook, or None.

    This hook runs as ``claude → genesis-hook launcher → python``, so the
    session's ``claude`` process is an ANCESTOR (not necessarily the direct
    parent). Walk the ppid chain (bounded, never loops on a cycle) to the first
    ``comm == "claude"``. ``/proc/<pid>/{comm,stat}`` are world-readable — no
    ptrace gate. Fail-open None on any read/parse failure (a nudge miss must
    never cost the user)."""
    pid = os.getpid()
    for _ in range(16):
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return None
        if comm == "claude":
            return pid
        try:
            # After the last ')' (comm may contain spaces/')'): state=field3=idx0,
            # ppid=field4=idx1.
            ppid = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if ppid <= 1 or ppid == pid:
            return None
        pid = ppid
    return None


def _current_head(root: Path) -> str | None:
    """The main tree's CURRENT HEAD sha, or None if unknowable.

    A subprocess ``git rev-parse``, deliberately, rather than reading
    ``.git/HEAD`` and the ref file: a ref may live loose, packed in
    ``packed-refs``, or both (all three states occur), so a loose-only reader
    works until the next ``git gc`` and then fails SILENTLY — the worst failure
    mode for a fail-open hook. Measured at 3.86 ms on the reference install,
    against a hook that already opens SQLite twice.

    ``root`` is the MAIN tree. That is the same tree a spawn identity is
    captured from (``mcp_spawn_identity`` reads ``env.repo_root()`` so a
    worktree session still reports main's HEAD), so both sides of the
    comparison are on one line. Fail-open None on any failure.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    # Validate BEFORE returning: this value becomes a git argv operand in
    # _deploy_span, not just message text. See _is_sha.
    return sha if _is_sha(sha) else None


def _deploy_span(root: Path, spawn: str, head: str) -> tuple[int, int, list[str]] | None:
    """``(landed, only_ours, pr_numbers)`` for ``spawn...head``, or None on failure.

    ONE ``git log``, reached only after ``differs_from_head`` has already said
    the shas differ — so the cost lands on the transition, never on the steady
    state.

    THE SPLIT IS THE POINT. ``differs_from_head`` compares two shas and so knows
    only that they are not equal; the DIRECTION lives in the history, and this is
    where the history is read. A symmetric ``spawn...head`` with ``--left-right``
    answers it in that same one call: ``>`` marks a commit only the checkout has
    (what landed under us), ``<`` marks one only this process has (what the
    checkout does not contain).

    Returning the two counts separately, rather than a single number, is what
    makes the three states distinguishable by the caller instead of guessed:

        only_ours == 0, landed > 0   main moved forward — an ordinary deploy
        only_ours > 0,  landed > 0   the histories DIVERGED (a rebase/force-move)
        only_ours > 0,  landed == 0  the checkout moved BACK past this process

    The predecessor asked ``spawn..head`` (two dots) and read a count of zero as
    "diverged". That is wrong in both directions and it is the defect Codex named
    (P2, PR #1651): git defines ``A..B`` as commits reachable from B and not from
    A, so a genuine divergence is NON-zero and was reported as an ordinary
    "main moved N commits", while zero actually means the checkout moved BACK —
    the one case where restarting loses code rather than gaining it.

    PR numbers come from the trailing ``(#N)`` that squash merges append —
    MEASURED 40/40 on recent main commits. Anchored to end-of-line so a subject
    that cites another PR mid-sentence (``(supersedes #1446) (#1577)``) yields
    only the real merge number. Newest first, as ``git log`` orders them.

    Both refs are re-validated here rather than trusted from the caller: they
    become a single ``<spawn>..<head>`` argv operand, and git reads a
    leading-dash operand as an OPTION (see ``_is_sha``).
    """
    if not _is_sha(spawn) or not _is_sha(head):
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [
                "git",
                "-C",
                str(root),
                "log",
                "--oneline",
                "--left-right",
                f"{spawn}...{head}",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    # Split on "\n" ONLY, not str.splitlines(): the latter also breaks on
    # U+2028/U+2029/\x0b/\x85, which a commit subject may legitimately contain —
    # MEASURED, one such subject becomes two "commits", inflating the count and
    # losing that line's PR number. The count is the one author-influenceable
    # value in an LLM-visible line, so it gets the exact separator git used.
    lines = [ln for ln in proc.stdout.split("\n") if ln.strip()]
    # `--left-right` prefixes every line with `<` or `>` and nothing else, so the
    # side is read off column 0. A line with neither marker is not classified as
    # either side: it would mean git changed its output shape, and inventing a
    # side for it is how a wrong direction gets asserted with confidence.
    landed = [ln for ln in lines if ln.startswith(">")]
    only_ours = [ln for ln in lines if ln.startswith("<")]
    prs = [m.group(1) for ln in landed if (m := re.search(r"\(#(\d+)\)$", ln))]
    return len(landed), len(only_ours), prs


def _deploy_message(
    spawn: str, head: str, landed: int, only_ours: int, prs: list[str]
) -> str | None:
    """The notice line, or None when there is nothing honest to say.

    Pure (no IO) so the wording and every suppression rule are unit-testable.
    Suppresses on a non-sha either side (the line becomes LLM-visible prompt
    context, so nothing but a real sha is ever interpolated) and when neither
    side has a unique commit (nothing observed, so nothing to say).

    THE LEAD CLAUSE IS CHOSEN BY THE COUNTS, NOT ASSUMED. Three states reach
    here (see ``_deploy_span``) and they are not the same news:

    * main moved forward — the ordinary deploy, and the only one that may say so;
    * the histories diverged — this process holds commits the checkout does not,
      so a restart REPLACES that code rather than catching up to it;
    * the checkout moved back — a restart loses code outright.

    The remedy tail is identical in all three (a restart is still what changes
    what is loaded), which is exactly why the lead has to carry the difference.

    Carries only shas, counts, and PR numbers: no commit subjects, no paths,
    no branch names. That is a privacy floor and it also removes tag-forgery as
    a concern outright — there is no attacker-controlled text in the line.
    """
    if not _is_sha(spawn) or not _is_sha(head):
        return None
    if landed <= 0 and only_ours <= 0:
        return None
    shown = prs[:_PR_CAP]
    pr_clause = ""
    if shown:
        listed = " ".join(f"#{n}" for n in shown)
        if len(prs) > len(shown):
            listed += f" +{len(prs) - len(shown)} more"
        pr_clause = f" — PRs {listed}"
    def _n(k: int) -> str:
        return f"{k} commit" if k == 1 else f"{k} commits"

    # The LABEL moves with the state too. "Deploy" is a claim in its own right —
    # a rollback announced under it reads as new code arriving, which is the
    # opposite of what happened.
    if only_ours == 0:
        label = "Deploy"
        lead = f"main moved {_n(landed)} under this session"
    elif landed == 0:
        label = "Rollback"
        lead = (
            f"the checkout moved BACK past this session — it is missing "
            f"{_n(only_ours)} this session's code has"
        )
    else:
        label = "Diverged"
        lead = (
            f"the checkout DIVERGED from this session — {_n(landed)} landed "
            f"that this session lacks, and {only_ours} it has are not in the "
            f"checkout"
        )
    # WHAT IS ALREADY LIVE, and what each stale thing actually needs — both
    # narrowed to what is true (Codex P2 x2, PR #1651).
    #
    # "hooks/policy are already live" over-claimed. A hook's COMMAND is re-read
    # per invocation, so a change to hook CODE is live — but Claude Code
    # snapshots hook CONFIGURATION at session start, so a commit that adds or
    # removes a hook, or changes its matcher or timeout in .claude/settings.json,
    # is NOT live in this session. Saying "hooks are live" tells a session it
    # need not act on exactly the change it must act on.
    #
    # And "a session restart" does not restart genesis-server. Restarting the
    # session respawns its MCP children; genesis-server is an independent
    # systemd user unit and needs `systemctl --user restart genesis-server`
    # (CLAUDE.md, Process Management). Naming one action for two different
    # things left the server running old code with the notice reading as though
    # it had been handled.
    return (
        f"[⚠ {label}: {lead} "
        f"({spawn[:8]} → {head[:8]}){pr_clause} — hook CODE is live; hook CONFIG "
        f"changes are not (snapshotted at session start). MCP needs a session "
        f"restart; genesis-server needs "
        f"`systemctl --user restart genesis-server`]"
    )


def _deploy_stamped(session_id: str) -> str | None:
    """The sha last announced to this session, or None if never/unreadable.

    None means "not yet told", which lets the notice through — fail-open toward
    surfacing a deploy, consistent with every sibling emitter here.

    ``ValueError`` is caught alongside ``OSError`` and is NOT redundant:
    ``read_text`` raises ``UnicodeDecodeError`` on non-UTF-8 bytes, and that is
    a ``ValueError``, not an ``OSError`` (verified). Catching only ``OSError``
    let a torn/garbled stamp escape to the caller's blanket handler, which
    returns BEFORE re-stamping — so one bad write silenced that session's nudge
    permanently. Fail-open here re-reads as "not yet told" and the next emit
    overwrites the bad file.
    """
    sdir = _session_dir(session_id)
    if sdir is None:
        return None
    try:
        return (sdir / _DEPLOY_STAMP).read_text().strip() or None
    except (OSError, ValueError):
        return None


def _record_deploy_notice(session_id: str, head: str) -> bool:
    """Stamp the announced sha. Returns True iff it persisted.

    A persistently unwritable ``~/.genesis/sessions/`` would otherwise let the
    notice print on EVERY prompt, since an unreadable stamp reads as "not yet
    told". Returning False lets the caller SUPPRESS instead of spam, so a broken
    filesystem degrades to silence rather than a per-prompt banner. Written
    BEFORE the line is printed, for the same reason.

    Written ATOMICALLY (tempfile in the same dir + ``os.replace``, mirroring
    ``mcp_spawn_store.persist_spawn_commit``): a bare ``write_text`` can leave a
    truncated or garbled stamp if the process dies mid-write, and a reader
    cannot tell that from a real sha.
    """
    sdir = _session_dir(session_id)
    if sdir is None:
        return False
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(sdir), prefix=f".{_DEPLOY_STAMP}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(head)
            os.replace(tmp, sdir / _DEPLOY_STAMP)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return True
    except OSError:
        return False


def _emit_deploy_nudge(session_id: str) -> None:
    """One line when main has moved under this session, once per deploy.

    Each FastMCP subprocess snapshots its code at spawn and NEVER reloads, and
    genesis-server holds imported code until restart — while CC hooks re-exec
    per invocation from the main tree and so are already live. A deploy landing
    mid-session therefore leaves the session's recall (and its security
    read-exclusions) on old code with no auto-restart, so the remedy is a nudge
    naming what landed.

    Detection is OBSERVED HEAD DRIFT, not the ``update_history`` record. MEASURED
    on a live install (2026-09-03): 46 HEAD movements in 30 days produced 5 rows
    (10.9%), because the sanctioned code-only deploy path writes none — so a
    record-keyed verdict was silent for 89% of deploys, and structurally silent
    for EVERY session spawned after the last ``scripts/update.sh`` run. Observing
    HEAD needs no producer and cannot be bypassed. See ``commit_identity`` for
    why the BLOCKING guard deliberately keeps the record-keyed verdict.

    Identify this session's slot by matching the walked ``claude`` pid against
    the ``mcp-spawn`` file plane (world-readable; pid- AND start-time-validated
    by ``read_spawn_identity`` against a recycled pid). Wrapped fail-open so a
    miss never surfaces as an error and a session at HEAD stays silent.
    """
    try:
        from genesis.env import repo_root
        from genesis.observability.cc_slots import read_proc_start_iso
        from genesis.observability.commit_identity import differs_from_head
        from genesis.observability.mcp_spawn_store import (
            enumerate_spawn_slots,
            read_spawn_identity,
        )

        pid = _claude_ancestor_pid()
        if pid is None:
            return
        # Find THIS session's slot by matching its claude pid in the spawn plane
        # (env GENESIS_SLOT would also work, but a pid match can't be fooled by an
        # inherited/stale slot value and mirrors the dashboard badge's join).
        slot = next((s for s, rpid, _sat in enumerate_spawn_slots() if rpid == pid), None)
        if slot is None:
            return
        ident = read_spawn_identity(slot, pid, read_proc_start_iso(pid))
        if ident is None:
            return
        spawn = ident[0]
        # The spawn commit comes off the file plane, and it becomes half of a
        # `<spawn>..<head>` git argv operand below — validate at the boundary it
        # ENTERS, not where it is printed. See _is_sha.
        if not _is_sha(spawn):
            return
        # repo_root() — the SAME resolver the spawn commit was captured with
        # (mcp_spawn_identity), so both sides of the comparison are provably on
        # one tree. It resolves to the installed package's own location, which is
        # the main tree on every install; `Path.home() / "genesis"` would have
        # been a guess that happens to be right only where the repo is at that
        # path, and the launcher does not export GENESIS_REPO_ROOT.
        repo = repo_root()
        head = _current_head(repo)
        if head is None or not differs_from_head(spawn, head):
            return
        # Already announced THIS deploy to THIS session — silent until HEAD moves
        # again. Checked before the `git log`, so the steady state costs one
        # rev-parse and a small file read.
        if _deploy_stamped(session_id) == head:
            return
        span = _deploy_span(repo, spawn, head)
        if span is None:
            # A FAILED read is not an observation. `_deploy_span` returns None
            # only when it could not look — git timed out, exited nonzero, or a
            # ref would not resolve — and it signals the conclusive
            # "nothing to say" case as a count of ZERO instead. Stamping on None
            # recorded a failure as a completed announcement, and because the
            # stamp check runs BEFORE the git call, that session then exited
            # early on every later prompt: one transient timeout silenced the
            # deploy nudge for the whole life of the session (Codex P2,
            # PR #1651). Returning here re-pays two subprocesses on the next
            # prompt, which is the cost of not knowing — and it is the cheap
            # error against a nudge that never fires again.
            return
        msg = _deploy_message(spawn, head, *span)
        # Stamp on any CONCLUSIVE observation of `head`, including the ones we
        # deliberately stay silent about (neither side holds a unique commit).
        # Those states are genuinely answered, so re-paying two git subprocesses
        # on every later prompt for them is waste, not caution.
        persisted = _record_deploy_notice(session_id, head)
        # Print only if the stamp landed: an unwritable session dir reads back as
        # "not yet told", so printing anyway would repeat the banner every prompt.
        if not msg or not persisted:
            return
        print(msg)
        sys.stdout.flush()
    except Exception:
        return  # fail-open: a nudge miss must never surface as an error


def main() -> None:
    # Skip if Genesis context is disabled
    if not _FLAG.exists():
        return

    # Skip for Genesis-dispatched sessions (they have their own alert path)
    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    prompt = hook_input.get("prompt", "")
    now = datetime.now(UTC)

    # 1. Temporal context (always, even if no session_id)
    if session_id:
        _emit_temporal_context(session_id, now)
        # 1b. Charter drift tag (chartered sessions only; fail-open)
        _emit_charter_tag(session_id)

    # 2. Buffer user message for bookmarks. Deliberately BEFORE the deploy nudge:
    # the nudge is the only step here that spawns subprocesses, so it is the only
    # one that can burn the hook's whole timeout (a wedged/read-only filesystem).
    # This hook's timeout is not fatal to the turn, but a kill part-way through
    # would silently drop the bookmark buffer AND last_prompt_time — which would
    # then also corrupt the NEXT prompt's "Last msg" tag. Cheap state first.
    if session_id and prompt:
        _buffer_message(session_id, prompt, now)

    # 3. Deploy nudge (only when main has moved under this session; once per
    # deploy; fail-open). Last of the session-state writes, for the reason above.
    if session_id:
        _emit_deploy_nudge(session_id)

    # 4. Shelve/unshelve hint
    if prompt:
        _check_shelve_hint(prompt)


if __name__ == "__main__":
    main()
