#!/usr/bin/env python3
"""SessionStart hook: inject Genesis context into CC sessions.

This script runs at CC session start (via .claude/settings.json SessionStart hook).
Its stdout becomes context visible to Claude in the session.

Each section is printed and flushed immediately so that if the hook times out
(e.g. DB query hangs), identity files (instant disk reads) are already captured.

Also writes session start timestamp to ~/.genesis/session_start for use by the
UserPromptSubmit urgent-alert hook (interactive sessions only).

For interactive (foreground) sessions: injects everything — identity files,
cognitive state, procedures, temporal context, capabilities.

For bridge-dispatched sessions (GENESIS_CC_SESSION=1): skips identity files
and cognitive state (already provided via --system-prompt), but still injects
procedures, temporal context, resume signals, and capabilities.

Skips ALL injection when:
- ~/.genesis/cc_context_enabled flag file is absent (eject lever)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# The shared hook helpers live in scripts/hooks/; this script runs from scripts/
# (a different sys.path[0]), so add the hooks dir before importing them. Same
# idiom as the sibling UserPromptSubmit hook. Unguarded on purpose: a missing
# helper is a broken checkout and must be loud, not silently unbounded.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_output import HOOK_STDOUT_CAP, BoundedStdout, emit_cost, utf16_len  # noqa: E402

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
_SETUP_COMPLETE = Path.home() / ".genesis" / "setup-complete"
_IDENTITY_DIR = Path(__file__).resolve().parent.parent / "src" / "genesis" / "identity"
# Which identity files ride which part. Split across two hook entries because
# each entry gets its own harness budget (see the cap block below).
_IDENTITY_PARTS = {
    "identity-core": ["SOUL.md", "STEERING.md"],
    "identity-user": ["USER.md", "CONVERSATION.md"],
}
_SESSION_START_FILE = Path.home() / ".genesis" / "session_start"
_SESSION_CONFIG = Path.home() / ".genesis" / "session_config.json"
_SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "genesis" / "skills"

# ── The harness output cap, and the part budget ─────────────────────────
# Claude Code persists a hook's stdout above a size threshold: the WHOLE output
# goes to a file and the model receives a ~2 KB preview. Everything below the
# preview is then invisible — not truncated visibly, GONE, with no marker in
# context saying so.
#
# MEASURED 2026-08-30 on this install (CC 2.1.246; ~25 probe sessions via the
# env-gated seam in main(), method in docs/reference/cc-compatibility.md):
#   * threshold = EXACTLY 10,000 CHARACTERS of hook stdout (10,000 inline,
#     10,001 filed; 6,000 two-byte chars = 12,044 BYTES arrived inline, so the
#     unit is characters, not bytes);
#   * the budget is PER HOOK ENTRY — two SessionStart hooks emitting 9,000
#     chars each both arrived inline, in print AND interactive mode. That
#     per-entry budget is why this script runs as FOUR settings.json entries
#     (--part charter | identity-core | identity-user | knowledge);
#   * the threshold MOVES ACROSS VERSIONS: on CC 2.1.218 it sat near the high
#     20 Ks (filings were rare, 28-32 KB); the 2.1.246 update dropped it to
#     10 K and the filing rate tripled the SAME DAY. Whether it can also change
#     WITHOUT a version bump (a remote flag) is UNVERIFIED — the product does
#     use remote gates, but no evidence ties one to this threshold. Either way
#     the constant below is a measured value for one pinned version, and the
#     ALARMS — the per-prompt scream, the awareness watcher over the harness's
#     own filings — carry the guarantee, never this number. The watcher never
#     reads this constant, which is what makes it right even when this is wrong.
#
# The constant itself lives in scripts/hooks/hook_output.py — ONE home, shared
# with every other model-facing hook. A second copy is the drift that already
# broke a test in this branch when a cap moved in one file and not the other.
_HOOK_STDOUT_CAP = HOOK_STDOUT_CAP
_PART_BUDGET = 9_800

_PARTS = ("charter", "identity-core", "identity-user", "knowledge")


def _part_budget(part: str) -> int:
    """Budget for ``part``. ONE home, because two callers must agree.

    ``--part all`` is a manual/test path that emits every part through a single
    stream, so it gets the sum. `_begin_part` sized the writer this way while
    `_audit_line` printed the per-part constant as its denominator, so an `all`
    run reported a true numerator over a false denominator — e.g. `25000/9800`,
    on the line `main()`'s docstring calls the COMPLETION PROOF.
    """
    return _PART_BUDGET * len(_PARTS) if part == "all" else _PART_BUDGET


#: Cap on the block label quoted in a cut audit line, so its worst case is
#: BOUNDED rather than however long the longest block name happens to get.
_AUDIT_BLOCK_LABEL_MAX = 40


def _audit_line(
    part: str,
    intended: int,
    emitted: int,
    *,
    cut: tuple[str, int] | None = None,
    where: str = "",
) -> str:
    """The trailing `_[ctx <part>: …]_` self-audit line. ONE renderer.

    Exists so :data:`_AUDIT_LINE_RESERVE` can be MEASURED from it instead of
    guessed. The reserve was 120 while the cut variant is ~206 chars in
    production shape (36-char session id, real mirror path, a block label like
    `identity:CONVERSATION.md`) — and `room` at that moment is ALWAYS exactly
    the reserve, because `_cut_here` fills to the ceiling by construction. So
    the audit line was deterministically truncated on the one path where it
    carries information, losing its trailing `]_`. `main()`'s docstring calls
    this line "the COMPLETION PROOF"; it was landing mangled.
    """
    if cut is None:
        return f"\n_[ctx {part}: {emitted}/{_part_budget(part)} chars]_"
    block, dropped = cut
    return (
        f"\n_[ctx {part}: {intended} intended / {emitted} emitted "
        f"— CUT {dropped} chars at '{block[:_AUDIT_BLOCK_LABEL_MAX]}'{where}]_"
    )


# Room reserved for the trailing `_[ctx <part>: …]_` self-audit line — DERIVED
# from the renderer above at its realistic worst case, not a round number.
# Pinned by test_the_audit_reserve_fits_the_line_it_reserves_for, which rebuilds
# this same worst case, so the two cannot drift.
_AUDIT_LINE_RESERVE = (
    len(
        _audit_line(
            "identity-user",
            99_999,
            99_999,
            cut=("x" * _AUDIT_BLOCK_LABEL_MAX, 99_999),
            where=(
                f" — full text: {Path.home()}/.genesis/sessions/{'0' * 36}/context-identity-user.md"
            ),
        )
    )
    + 16  # slack for a longer home path than this box's
)

# Every part is ALSO written whole to ~/.genesis/sessions/<sid>/context-<part>.md.
# The harness keeps a filed payload on disk; a hard cut would not, so without
# this mirror the chokepoint would trade a recoverable failure for an
# unrecoverable one. With it, a cut is a pointer. Rides the existing per-session
# store (charter.md, last_prompt_time already live there) — no new store.
_MIRROR_STEM = "context"


def _mirror_path(session_id: str, part: str) -> Path | None:
    """Where this part's FULL intended text is mirrored, or None if unsafe.

    Uses the shared ``hook_input.session_path`` chokepoint for the id guard
    rather than re-deriving it (hooks hand-copied that check in three shapes
    and omitted it in four files before it was centralised).
    """
    try:
        from hook_input import session_path
    except Exception:
        return None
    d = session_path(Path.home() / ".genesis" / "sessions", session_id)
    return None if d is None else d / f"{_MIRROR_STEM}-{part}.md"


# The recovery instruction, first line of every part. It sits INSIDE the
# harness's 2,000-char preview by construction, which is the one place a
# filed part is still visible: MEASURED, sessions act on instructions in the
# preview head (they emitted the status header it asks for) while 0 of THIS
# PROJECT'S 175 transcripts ever followed the harness's own "Full output saved
# to:" line. (Population named on purpose — a nearby docstring cites 143 files
# and 195 wrappers from two other instruments, and an unlabelled number invites
# the reader to reconcile figures that were never the same denominator.)
def _miswire_alert(miswired: str) -> str:
    """The in-band mis-wire block. A function so the budget test measures the
    REAL text rather than an estimate that drifts away from it."""
    return (
        "## GENESIS ALERT: SessionStart hook MIS-WIRED\n\n"
        f"The session-context hook was invoked with {miswired}. Only the CHARTER "
        "part was emitted — identity (SOUL/USER/STEERING/CONVERSATION), essential "
        "knowledge, in-flight state and capabilities are MISSING from this window. "
        "Fix the four `--part` SessionStart entries in `.claude/settings.json` "
        "(charter, identity-core, identity-user, knowledge), then restart the "
        "session. Tell the user this happened."
    )


def _recovery_header(part: str, mirror: Path | None) -> str:
    """The in-band recovery pointer, kept SHORT on purpose.

    It is emitted on every part of every window, so its length is pure overhead
    four times over — and it competes for the same budget as the charter block,
    whose degrade floor (every open ledger id) cannot move. The full
    explanation lives in CLAUDE.md, which is loaded natively and therefore
    survives exactly the failure this line announces; here we only need to be
    identifiable, name the mirror, and say what to do first.
    """
    where = f" · mirror: {mirror}" if mirror else ""
    return (
        f"[genesis-ctx:{part}{where}] If this arrived as a preview or truncation "
        "notice, Read that path first — CLAUDE.md → Hook Output Persistence."
    )


_CONVERSATION_POINTER = (
    "## Conversation protocol — omitted for size\n\n"
    "USER.md left this hook's output no room for "
    "`src/genesis/identity/CONVERSATION.md` under the harness's per-hook stdout "
    "cap (over it, the WHOLE output is filed away behind a 2 KB preview). Read "
    "that file directly when conversation protocol matters."
)

#: The tail a truncated identity file appends (see the truncation branch). Sized
#: generously; only its LENGTH is used, as a reserve.
_TRUNCATION_NOTICE_TAIL = (
    "\n\n_[CONVERSATION.md truncated at 99999 chars — the full file exceeds this "
    "hook's stdout budget; read src/genesis/identity/CONVERSATION.md directly.]_"
)

#: Room a part must hold back for the degrade of the file STILL TO COME in it.
#: Emitted by the charter part while the setup floor is unmet. A module
#: constant, not an inline literal, so `test_charter_part_overhead_fits_the_budget`
#: can MEASURE it instead of hand-copying its length — that test guards the
#: charter part's un-cuttability with margin in the low hundreds, and a copied
#: number lets an ordinary prose edit here overrun the part with the test green.
_ONBOARDING_BLOCK = (
    "## FIRST-RUN ONBOARDING REQUIRED\n\n"
    "Genesis is **not fully functional yet** — the setup floor is "
    "unmet (it needs Claude Code logged in, at least one LLM/routing "
    "API key, and at least one embedding key). **Before doing "
    "anything else**, run the onboarding flow to finish configuring "
    "the system.\n\n"
    "The onboarding skill is at: "
    "`src/genesis/skills/onboarding/SKILL.md`\n\n"
    "Read the skill file and follow its steps. Do not skip this — the "
    "user needs a working system before Genesis can be useful.\n\n"
    "If the user's first message is unrelated to setup, acknowledge it "
    "but explain that you need to complete onboarding first."
)

#: Ceiling for the cap-measurement probe seam (GENESIS_CTX_PROBE_BYTES). Far
#: above any real probe — the threshold being measured is ~10k — and it exists
#: only so an env typo cannot allocate its way to an OOM inside a hook.
_PROBE_MAX_CHARS = 200_000

_OUT: BoundedStdout | None = None


def _writer() -> BoundedStdout:
    """The part's bounded stdout. Lazily built so direct callers still work."""
    global _OUT
    if _OUT is None:
        _OUT = BoundedStdout(_PART_BUDGET, label="all", reserve=_AUDIT_LINE_RESERVE)
    return _OUT


#: Set by _begin_part so the crash path in main() can still name the part and
#: reach the right mirror when _emit_body dies before returning them.
_CURRENT_PART = "charter"
_CURRENT_SID = ""


def _current_part(fallback: str) -> str:
    return _CURRENT_PART or fallback


def _current_session_id(fallback: str) -> str:
    return _CURRENT_SID or fallback


def _begin_part(part: str, mirror: Path | None, session_id: str = "") -> None:
    """Open a fresh bounded stream for ``part`` and emit its recovery header.

    The ``all`` mode is a MANUAL/test invocation that deliberately emits every
    part at once; it is never wired as a hook entry, so one entry's cap is the
    wrong bound for it and cutting there would truncate a legitimate full run.
    It is bounded at the sum of what the four real entries carry — still a
    bound, just the honest one — and its audit line reports the true size.
    """
    global _OUT, _CURRENT_PART, _CURRENT_SID
    _CURRENT_PART, _CURRENT_SID = part, session_id
    budget = _part_budget(part)
    _OUT = BoundedStdout(
        budget,
        label=part,
        reserve=_AUDIT_LINE_RESERVE,
        mirror_hint=str(mirror) if mirror else "",
    )
    _OUT.emit(_recovery_header(part, mirror), block="recovery-header")


def _emit(text: str, *, block: str = "") -> None:
    """Print a section, bounded by the harness cap, flushed immediately.

    Streams rather than buffering — the flush-per-section contract is what makes
    a timeout kill lose only the tail. The BUDGET is enforced here, at the one
    chokepoint, rather than by each block remembering to call ``_fits``: 2 of 12
    blocks in the knowledge part remembered, and the other ten are how ~30 KB of
    identity/charter went missing from 195 windows.
    """
    _writer().emit(text, block=block)


# `_fits` is GONE, deliberately, and test_no_budget_arithmetic_outside_the_writer
# keeps it gone. Every degrade decision now goes through
# `_writer().emit_or_degrade(...)`, where the caller says what the fallback LOOKS
# like and the writer settles every number.
#
# Why the helper had to go rather than be used more carefully: it invited its
# callers to compute a `reserve`, and each computed one was found wrong by
# review — the audit reserve counted twice, a pointer reserved in a part that
# cannot emit one, a `keep` derived from the budget constant instead of the
# room. The arithmetic was never hard; having it in six places was.


def _routed_session_notice(model: str | None) -> str | None:
    """Markdown NOTICE when this interactive session is routed to a roster peer.

    ``model`` is ``GENESIS_ROSTER_MODEL`` (set only by ``scripts/gmodel`` for a
    peer). CC's baked-in "You are powered by …" identity text still says Claude
    when the endpoint is a peer, so the self-reported ``[model]`` header would be
    wrong — this block surfaces the true model and steers the header. Returns
    ``None`` (no block) for a native/plain session.
    """
    if not model:
        return None
    return (
        f"## ⚠ Routed session — running on {model}\n\n"
        f"This CLI session is routed to **{model}** (a non-Anthropic roster peer), "
        "NOT native Claude. Claude Code's built-in identity text still says Claude "
        f"— ignore it; the model answering you is **{model}**. Begin your status "
        f"header with `[{model} / <effort>]` accordingly. Note: Genesis MCP tools "
        "may be unavailable or limited on non-Anthropic endpoints."
    )


def _model_display_name(model_id: str) -> str | None:
    """Map a Claude Code model identifier to its `Display Name Version` form.

    Returns None when the id is empty or unrecognized — the caller then falls
    back to injecting the raw id with a mapping instruction (robust to models
    newer than this table), so a stale table degrades gracefully rather than
    emitting a wrong header. Handles a bracketed context-window suffix
    (``claude-opus-4-8[1m]``) and a trailing date stamp
    (``claude-haiku-4-5-20251001``).
    """
    if not model_id:
        return None
    import re

    mid = model_id.strip().lower()
    mid = mid.split("[", 1)[0]  # drop "[1m]"-style context-window suffix
    mid = re.sub(r"-\d{8}$", "", mid)  # drop trailing -YYYYMMDD date stamp
    table = {
        "claude-fable-5": "Fable 5",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-7": "Opus 4.7",
        "claude-sonnet-5": "Sonnet 5",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-haiku-4-5": "Haiku 4.5",
    }
    return table.get(mid)


_MODEL_CACHE_FILE = Path.home() / ".genesis" / "cc_session_model.json"
# Bound the map so it self-evicts (no retention wiring needed). Genesis runs
# several concurrent cc-N slot sessions, so a single slot would be clobbered by
# whichever session started/compacted most recently — a per-session map keyed by
# id keeps each session's model available for its own later resume.
_MODEL_CACHE_MAX = 24


def _cache_session_model(session_id: str, model: str) -> None:
    """Persist `model` under `session_id` in a bounded, self-evicting map.

    Written whenever CC provides `model` (startup/compact) so that a later
    `claude --resume` — where CC OMITS `model` — can recover it. Insertion-order
    eviction keeps the map at `_MODEL_CACHE_MAX` most-recent sessions.

    Written via a PID-UNIQUE tmp + `os.replace`, which buys two different
    things. A reader never sees a partial file (rename is atomic). Concurrent
    WRITERS no longer destroy each other: with one shared `<name>.tmp` the
    interleaving installed a truncated file, and the loser's `os.replace` raised
    `FileNotFoundError` into the suppressor — so the next read hit invalid JSON
    and reset the whole map to `{}`. Last-writer-wins on the CONTENT is still
    possible and is harmless here (each session writes its own key, and a lost
    entry only degrades that header to env derivation); losing the FILE was not.

    Fail-open: any error is swallowed (header degrades to env derivation).
    """
    if not session_id or not model:
        return
    import contextlib
    import json

    with contextlib.suppress(OSError, ValueError):
        data: dict = {}
        if _MODEL_CACHE_FILE.exists():
            try:
                loaded = json.loads(_MODEL_CACHE_FILE.read_text())
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, ValueError):
                data = {}  # corrupt cache — start fresh, never crash the hook
        data.pop(session_id, None)  # re-insert at the end (most-recent)
        data[session_id] = model
        while len(data) > _MODEL_CACHE_MAX:
            data.pop(next(iter(data)))  # evict oldest (insertion order)
        _MODEL_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _tmp = _MODEL_CACHE_FILE.with_name(f"{_MODEL_CACHE_FILE.name}.{os.getpid()}.tmp")
        _tmp.write_text(json.dumps(data))
        os.replace(_tmp, _MODEL_CACHE_FILE)


def _cached_session_model(session_id: str) -> str:
    """Read back the cached model for `session_id`, or "" on any miss.

    Keyed by id, so a resume of session A never picks up session B's model.
    """
    if not session_id:
        return ""
    import json

    try:
        data = json.loads(_MODEL_CACHE_FILE.read_text())
    except (OSError, ValueError):
        return ""
    if isinstance(data, dict):
        return str(data.get(session_id) or "")
    return ""


def _session_config_block(effort: str, hook_model: str, roster_model: str) -> str:
    """Top Session Configuration block: effort + first-reply status-header directive.

    Model-identity precedence (highest first):
      1. ``roster_model`` (``GENESIS_ROSTER_MODEL``) — a non-Anthropic peer set
         by ``scripts/gmodel``; already a display name.
      2. ``hook_model`` — the ``model`` field from CC's SessionStart stdin JSON.
         Unlike the baked "You are powered by …" environment line, CC re-sends
         this on EVERY SessionStart including ``compact``, so it stays correct
         after a compaction (the env line is frozen at the original session
         start and goes stale after a /model switch or a compact of an
         already-switched session — the bug this fixes).
      3. Neither present (older CC with no ``model`` field) — fall back to
         env-line derivation, the legacy behavior.

    A /model switch fires NO SessionStart event, so a switch AFTER this block is
    injected can't be captured here — the precedence note covers it ("a /model
    switch after this message wins").
    """
    tmpl = (
        "Begin your first reply of this session with a one-line status header "
        f"on its own line — `[{{model}} / {effort}]` — then your normal reply."
    )
    switch_note = (
        " If you switch models with `/model` AFTER this message, use the "
        "switched-to model on your next first-of-session header instead. "
        "No emoji, no explanation.\n"
    )
    authoritative = roster_model or _model_display_name(hook_model)
    if authoritative:
        body = (
            tmpl.replace("{model}", authoritative)
            + " That model identity is authoritative for this window (from Claude "
            "Code's session-start hook input / routing, which stays correct across "
            'context compaction — unlike the "You are powered by …" environment '
            "line, which can be stale)." + switch_note
        )
    elif hook_model:
        # Present but unmapped (a model newer than the table above): inject the
        # raw id as authoritative and let the model render its own display name.
        body = (
            tmpl.replace("{model}", "<model>")
            + f" Your current model identifier is `{hook_model}` (authoritative for "
            "this window — from Claude Code's session-start hook input, which stays "
            'correct across context compaction, unlike the "You are powered by …" '
            "environment line). Map it to its display name + version (e.g. "
            "`claude-opus-4-8` → `Opus 4.8`)." + switch_note
        )
    else:
        # No model field (older CC, or absent) — legacy env-line derivation.
        body = (
            tmpl.replace("{model}", "<model>")
            + " Derive <model> from your environment's \"You are powered by the "
            'model named …" line (e.g. `Opus 4.8`), per CONVERSATION.md → Session '
            "Start. If you switched models with `/model` this session, use the "
            "switched-to model. No emoji, no explanation.\n"
        )
    return f"## Session Configuration\n\n- Thinking effort: {effort}\n\n{body}"


def _sync_genesis_hooks() -> None:
    """Self-heal Genesis git hooks at session start.

    Invokes scripts/hooks/sync-hooks.sh --quiet to bring $GIT_COMMON_DIR/hooks
    into sync with scripts/hooks/*. This is how community users who `git pull`
    Genesis updates (without re-running bootstrap.sh) pick up new or updated
    hooks — the next CC session auto-installs them via this function.

    Fail-open: any error is swallowed silently. Hook sync must NEVER block
    session startup.

    Cost: ~50-200ms for the subprocess. Negligible in the 5000ms SessionStart
    budget. Runs once per session start.
    """
    import contextlib
    import subprocess

    sync_script = Path(__file__).resolve().parent / "hooks" / "sync-hooks.sh"
    if not sync_script.is_file():
        # sync-hooks.sh doesn't exist yet on very old Genesis installs —
        # silently skip. The install was pre-Phase-6.
        return
    # Fail-open: any error here must NEVER block session startup. CC discards
    # SessionStart stderr anyway, so silent skip is the right behavior.
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, FileNotFoundError):
        subprocess.run(
            [str(sync_script), "--quiet"],
            check=False,  # exit 2 (user-modified) is fine, not a failure
            capture_output=True,
            timeout=3.0,
        )


def _emit_body() -> tuple[str, str, str] | None:
    # Harness-cap probe (PR-A0 measurement seam — inert unless the env var is
    # set, which only the probe driver does). The Claude Code harness persists
    # hook stdout above an UNDOCUMENTED size threshold, replacing it with a
    # ~2 KB preview; the threshold is not a published constant and is remotely
    # tunable, so it must be MEASURED on the installed version, not assumed.
    # Emits exactly N filler characters and nothing else, so the observed
    # inline-vs-filed outcome is attributable to N alone.
    _probe = os.environ.get("GENESIS_CTX_PROBE_BYTES")
    if _probe:
        try:
            # Clamped: this is a measurement seam, and the value comes from the
            # environment. Unbounded, `_ch * _n` lets a fat-fingered zero
            # allocate its way to an OOM inside a SessionStart hook — the one
            # place a failure costs the whole window. The ceiling is far above
            # any real probe (the cap being measured is ~10k).
            _n = max(0, min(int(_probe), _PROBE_MAX_CHARS))
        except ValueError:
            return
        _ch = "é" if os.environ.get("GENESIS_CTX_PROBE_MODE") == "multibyte" else "A"
        sys.stdout.write("PROBE-START " + _ch * _n + " PROBE-END")
        sys.stdout.flush()
        return

    # Eject lever: flag file absent → no Genesis context
    if not _FLAG.exists():
        return

    # Hook input (session_id, source) — mirrors genesis_session_end.py. CC
    # pipes SessionStart input as stdin JSON; before this was parsed, the
    # script had no session identity and could not read per-session state.
    import json as _json_stdin

    try:
        _raw_stdin = sys.stdin.read()
        _hook_input = _json_stdin.loads(_raw_stdin) if _raw_stdin.strip() else {}
    except (_json_stdin.JSONDecodeError, OSError):
        _hook_input = {}
    _hook_session_id = str(_hook_input.get("session_id", "") or "")
    _hook_source = str(_hook_input.get("source", "") or "")
    # CC sends `model` on SessionStart for `startup` and `compact` — the two
    # events that matter here, because it means the header stays correct across a
    # compaction (the baked "You are powered by …" env line freezes at original
    # start and does not). Per CC docs, `model` is OMITTED on `resume` (session
    # recovery) and `clear`, and absent on older CC. When it is present we cache
    # it (keyed by session id, in a bounded map so concurrent sessions don't
    # clobber each other); when it is absent we read that cache back so a
    # `claude --resume` of a session whose model we saw still gets the right
    # header. Any cache miss falls through to env-line derivation — no worse
    # than before.
    # …resolved BELOW, once the part is known: only the charter part renders the
    # session-config header, so only it needs the model — and only it may write
    # the cache.

    # ── Which PART of the injection this invocation emits ──────────────
    # The harness budget is PER HOOK ENTRY (measured — see the cap comment at
    # the top), so settings.json wires this script FOUR times with --part
    # charter | identity-core | identity-user | knowledge, in that order, each
    # under its own 10,000-char budget. No argument = ALL parts in sequence —
    # the test/manual path ONLY: wired as a single hook it would exceed the
    # per-entry cap, which is precisely the bug this split fixes (a wiring
    # test pins the four-entry form in settings.json).
    # FAIL CLOSED on a mis-wire. `all` emits every part under ONE hook entry,
    # which is guaranteed to exceed the cap — the exact bug this split fixes —
    # so it is reachable ONLY by an explicit `--part all` (tests, manual runs),
    # never as a fallback. A missing or unrecognised --part means the wiring
    # and this script disagree, which happens in production through version
    # skew: `.claude/hooks/genesis-hook` runs the MAIN-tree script, so a
    # worktree still carrying single-entry settings.json invokes the new script
    # with no --part. MEASURED on this branch (the inverse skew): four-entry
    # settings.json against the old main-tree script produced clustered ~31 KB
    # filings. A mis-wire therefore degrades to the charter part ALONE — small,
    # in-budget, and loud — instead of one guaranteed silent filing.
    part = "charter"
    miswired = ""
    if "--part" in sys.argv:
        try:
            requested = sys.argv[sys.argv.index("--part") + 1]
        except IndexError:
            requested = ""
        if requested in (*_PARTS, "all"):
            part = requested
        else:
            miswired = f"--part {requested!r} is not one of {(*_PARTS, 'all')}"
    else:
        miswired = "no --part argument (settings.json wiring is out of date)"

    def _in(*parts: str) -> bool:
        return part == "all" or part in parts

    # Gated on the part, NOT run unconditionally: this block used to sit above
    # the dispatch, so the 4-entry wiring had all four processes concurrently
    # read-modify-writing one JSON map. With a shared tmp name the interleaving
    # installed a truncated file and the loser's `os.replace` raised into the
    # suppressor, wiping the whole cache on the next read. Only the charter part
    # renders the header that consumes this, so only the charter part does it.
    _hook_model = ""
    if _in("charter"):
        _hook_model = str(_hook_input.get("model", "") or "")
        if _hook_model:
            _cache_session_model(_hook_session_id, _hook_model)
        elif _hook_session_id:
            _hook_model = _cached_session_model(_hook_session_id)

    # Open the bounded stream for THIS part. Everything after this point is
    # budget-enforced on the way out, and the first line emitted is the
    # recovery header — which lands inside the harness's 2 KB preview by
    # construction, the one place a filed part is still visible.
    # A dispatched session gets identity via --system-prompt, so both identity
    # parts have NO body for it. Decide that BEFORE opening the stream: emitting
    # a part whose only content is its own recovery header would put two
    # content-free blocks in every background session's window, each telling the
    # model to go read a mirror containing just that header.
    if os.environ.get("GENESIS_CC_SESSION") == "1" and part in _IDENTITY_PARTS:
        return part, _hook_session_id, miswired

    _begin_part(
        part,
        _mirror_path(_hook_session_id, part) if part != "all" else None,
        _hook_session_id,
    )

    if miswired:
        # IN-BAND, not just stderr: a SessionStart hook's stderr on exit 0 goes
        # to the debug log and NEVER reaches the model (READ from the harness's
        # attachment renderer — only stdout on SessionStart/UserPromptSubmit/
        # UserPromptExpansion is injected). A mis-wire that only whispers to a
        # log is the silent failure this whole change exists to remove, so the
        # model is told plainly, in the one channel it can see.
        _emit(_miswire_alert(miswired), block="miswire-alert")

    # Phase 6: self-heal Genesis git hooks before doing anything else.
    # Runs on every session start so community installs auto-pick up hook
    # updates without requiring a bootstrap.sh re-run. Charter part only —
    # four concurrent invocations must not race four sync subprocesses.
    #
    # `not miswired` is part of that guarantee, not belt-and-braces: a mis-wire
    # FALLS BACK to the charter part, so two mis-spelled settings entries mean
    # two concurrent invocations that both believe they are the charter, and the
    # race this gate exists to prevent happens anyway. The degraded path is
    # exactly when a stray subprocess is least welcome, and a session that is
    # mis-wired has a louder problem to report than un-synced git hooks.
    if _in("charter") and not miswired:
        _sync_genesis_hooks()

    # Bridge-dispatched sessions get identity via --system-prompt; skip those
    # sections but still inject procedures, temporal context, and capabilities.
    is_genesis_session = os.environ.get("GENESIS_CC_SESSION") == "1"

    first = True

    if not is_genesis_session and _in("charter"):
        # Record session start time for the urgent-alert UserPromptSubmit hook
        # (bridge manages its own session tracking)
        _SESSION_START_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SESSION_START_FILE.write_text(datetime.now(UTC).isoformat())

        # 0.5. Session Configuration — the effort level AND the first-reply
        # status-header directive. The header (`[<model> / <effort>]`) is fully
        # specified in CONVERSATION.md → "Session Start", but that spec sits
        # hundreds of lines deep and gets buried under the user's first task, so
        # it fired unreliably; echoing the directive in its own short block is
        # what makes it emit. (It no longer arrives "at the top" — see the
        # ordering note on the charter block below.) The MODEL is now injected
        # authoritatively from CC's SessionStart `model` field (re-sent on every
        # compact, unlike the "You are powered by …" env line, which freezes at
        # original session start and goes stale after a /model switch or a
        # compact) — see _session_config_block for the precedence. Effort comes
        # from the sidecar (written by the session_config MCP tool).
        effort = "high"  # default — user's preferred effort level
        if _SESSION_CONFIG.exists():
            import json

            try:
                cfg = json.loads(_SESSION_CONFIG.read_text())
                effort = cfg.get("effort", "high")
            except Exception as exc:
                print(f"[session_context] Failed to read session config: {exc}", file=sys.stderr)
        _emit(
            _session_config_block(
                effort, _hook_model, os.environ.get("GENESIS_ROSTER_MODEL", "") or ""
            )
        )
        first = False

        # 0.6. Session charter (advisory): the immutable origin + living mission
        # + the OPEN ledger — persisted by the PreCompact hook
        # (scripts/genesis_precompact.py) and re-asserted into every window so
        # recency-biased compaction can never erase what this session is FOR.
        # Foreground-only.
        #
        # ORDERING, MEASURED (2026-08-30, 4 real sessions): CC concatenates
        # parallel SessionStart hook output in COMPLETION order, not
        # settings-declaration order. This part does a subprocess + a DB read +
        # a worker spawn, so it reliably lands LAST (4/4), and the two disk-only
        # identity parts swap between runs. Do NOT reason about where a block
        # appears relative to another part. That costs nothing now: every part
        # is under the per-hook cap, so everything ARRIVES — landing near the
        # user's first message is if anything better for attention. It mattered
        # only in the pre-split world, where being late meant being filed away.
        try:
            _charter_block = _charter_emission_block(_hook_session_id, _hook_source)
            if _charter_block:
                _emit("\n\n---\n\n")
                _emit(_charter_block)
        except Exception:
            pass  # Charter is advisory — never block session start

    # 1. Identity files (disk, always available, no external deps), split
    # across two PARTS so each rides its own 10 K hook budget:
    #   identity-core: SOUL + STEERING (tracked; CI ceilings pin their sizes)
    #   identity-user: USER + CONVERSATION (USER is install-local — CI cannot
    #     see it, so the guard is runtime: CONVERSATION degrades to a pointer
    #     when USER leaves it no room, and an oversized USER itself truncates
    #     LOUDLY rather than silently taking the part over the cliff).
    if not is_genesis_session:
        for _pname, _files in _IDENTITY_PARTS.items():
            if not _in(_pname):
                continue
            for _idx, name in enumerate(_files):
                path = _IDENTITY_DIR / name
                if not path.exists():
                    continue
                try:
                    content = path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeDecodeError) as exc:
                    # One damaged file must not cost the whole part. Say WHICH
                    # file and why, in-band: an identity file that silently
                    # contributes nothing is the failure this hook exists to
                    # make impossible, and "absent" and "unreadable" call for
                    # different fixes.
                    _emit(
                        f"## GENESIS ALERT: `{name}` could not be read\n\n"
                        f"`{type(exc).__name__}: {exc}` — that identity file is MISSING "
                        "from this window. Read it directly if its content matters, and "
                        "tell the user it is unreadable.",
                        block=f"identity-error:{name}",
                    )
                    first = False
                    continue
                if not content:
                    continue
                # NO ARITHMETIC HERE, deliberately. This block used to compute a
                # tail reserve, a `keep`, and two `_fits` calls, and every one of
                # those numbers was found wrong by review: the audit reserve
                # double-counted, the CONVERSATION pointer reserved inside a part
                # that cannot emit it, `keep` derived from the budget constant
                # rather than the room. The writer knows all of it; the caller's
                # only job is to say what the degrade LOOKS like.
                #
                # The still-to-come reserve is the one thing the writer cannot
                # know — it is about the NEXT file — so it is passed, and it is
                # that file's own degrade, per part: identity-user can swap
                # CONVERSATION.md for a pointer, identity-core can only truncate.
                _next = _files[_idx + 1] if _idx + 1 < len(_files) else ""
                # Charged with the WRITER's own cost function, not a hand
                # guess: the pointer costs its length plus the newline `emit`
                # adds, plus the divider the next file emits ahead of it. The
                # hand-written `+ 6` here undershot the real `+ 9` by 3, and the
                # miss lands exactly on the CONVERSATION.md degrade this reserve
                # exists to protect.
                # `utf16_len`, not `len`, for the same reason `emit_cost` is used
                # beside it: this reserve is compared against a budget billed in
                # code units. Both operands are ASCII constants today, so the two
                # agree exactly and nothing changes now — but a reserve that
                # measures in one unit what it protects in another is the defect
                # this file already paid for once, and leaving two of the three
                # branches on `len` would leave the comment above true of only
                # one of them.
                _reserve = (
                    emit_cost(_CONVERSATION_POINTER) + utf16_len("\n\n---\n\n")
                    if _next == "CONVERSATION.md"
                    else utf16_len(_TRUNCATION_NOTICE_TAIL)
                    if _next
                    else 0
                )
                _writer().emit_or_degrade(
                    content,
                    block=f"identity:{name}",
                    divider="" if first else "\n\n---\n\n",
                    # CONVERSATION.md is the one identity file with a pointer to
                    # fall back to; the rest are never elided, only truncated.
                    pointer=_CONVERSATION_POINTER if name == "CONVERSATION.md" else "",
                    notice=(
                        f"\n\n_[{name} truncated at {{kept}} chars — the full file exceeds "
                        f"this hook's stdout budget; read src/genesis/identity/{name} "
                        "directly.]_"
                    ),
                    reserve=_reserve,
                )
                first = False

    # 1.5. First-run onboarding detection
    # Inject the onboarding prompt while the install is not yet FUNCTIONAL — the
    # live floor (CC login + an LLM key + an embedding key), not merely while the
    # bootstrap marker is absent. So a bootstrapped-but-keyless box is still guided.
    # If the floor helper can't be imported for any reason, fall back to the marker.
    # Charter part: highest-salience slot, and on the fresh installs where this
    # fires the charter itself is tiny, so the part budget has ample room.
    if not is_genesis_session and _in("charter"):
        try:
            from genesis.onboarding.floor import compute_floor

            _needs_onboarding = not compute_floor().floor_met
        except Exception:  # noqa: BLE001 - hook must never crash the session
            _needs_onboarding = not _SETUP_COMPLETE.exists()
        if _needs_onboarding:
            onboarding_skill = _SKILLS_DIR / "onboarding" / "SKILL.md"
            if onboarding_skill.exists():
                if not first:
                    _emit("\n\n---\n\n")
                _emit(_ONBOARDING_BLOCK)
                first = False

    # Load last session data once — used for cognitive state tier + temporal awareness
    last_session_data = _load_last_session_data()

    if is_genesis_session:
        # 2. Cognitive state from DB — for ego/background sessions only.
        # Foreground sessions get essential knowledge instead (see below).
        # Charter part (the dispatched session's highest-salience block).
        if _in("charter"):
            try:
                cog = asyncio.run(_load_cognitive_state(last_session_data))
                if cog:
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit("## Current Cognitive State\n\n" + cog)
                    first = False
            except Exception:
                if not first:
                    _emit("\n\n---\n\n")
                _emit(
                    "## GENESIS ALERT: Cognitive State Unavailable\n\n"
                    "The database query for cognitive state failed. This may indicate "
                    "a DB or system health issue.\n\n"
                    "**Action:** Use the health_status MCP tool to investigate, or check "
                    "`~/.genesis/status.json` for current resilience state."
                )
                first = False
    else:
        # 2. Essential knowledge for foreground sessions (knowledge part).
        # Replaces cognitive state — shows what Genesis knows, not system health.
        # Critical alerts only surface if genuinely user-blocking.
        _ek_file = Path.home() / ".genesis" / "essential_knowledge.md"
        _ek_emitted = False
        if _in("knowledge") and _ek_file.exists():
            try:
                ek_content = _ek_file.read_text(encoding="utf-8").strip()
                if ek_content:
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(ek_content)
                    first = False
                    _ek_emitted = True
            except (OSError, UnicodeDecodeError) as exc:
                # Loud, for the same reason the identity files above are loud —
                # and this diff made THOSE loud while leaving this one silent,
                # which is the asymmetry rather than a considered exception.
                # `.exists()` already passed, so reaching here means permissions
                # or I/O, not absence: a condition an operator can act on.
                # CLAUDE.md's own account of this incident names the loss as
                # "identity, charter AND essential knowledge" — so EK is inside
                # the class this PR just made audible everywhere else.
                # (`_load_inflight_block` stays advisory-silent: it is derived
                # state that regenerates, not a file someone maintains.)
                _emit(
                    "## GENESIS ALERT: essential knowledge could not be read\n\n"
                    f"`{type(exc).__name__}: {exc}` — L1 context is MISSING from this "
                    "window. Read ~/.genesis/essential_knowledge.md directly if it "
                    "matters, and tell the user it is unreadable.",
                    block="essential-knowledge-error",
                )
                first = False

        # In-flight working state (advisory): active autonomy tasks, live
        # worktrees, and recently-touched plan files — computed fresh here
        # because they change far faster than the L1 essential-knowledge
        # regeneration cadence. Folds directly UNDER Essential Knowledge with
        # NO "---" divider so it reads as session context for recollection, not
        # a standalone report to recite at the user. Foreground-only (this is
        # the non-genesis-session branch). Writer: genesis.memory.open_loops.
        if _in("knowledge"):
            try:
                _inflight = _load_inflight_block()
                for _chunk in _inflight_emission_chunks(
                    _inflight, ek_emitted=_ek_emitted, first=first
                ):
                    _emit(_chunk)
                if _inflight:
                    first = False
            except Exception:
                pass  # In-flight state is advisory — never block session start

        # (The charter block is emitted in the charter part, directly under
        # Session Configuration — see there for why it is not here.)

        # Repo-pulse worker (advisory): fire-and-forget the global merged-PR
        # ↔ open-ledger matcher (PR-4a). The charter block surfaces the
        # PREVIOUS completed pulse's proposals — one boundary behind at
        # worst, by design (a pulse takes ~1 gh round-trip + 1 Haiku call).
        # The helper is fail-open end-to-end; pulse must never block session
        # start. Charter part (it feeds the charter's proposal sub-block).
        if _in("charter"):
            _spawn_repo_pulse_worker(_hook_source)

        # Critical-only alert: surface genuinely user-blocking issues (DB down, etc.)
        _status_file = Path.home() / ".genesis" / "status.json"
        if _in("knowledge") and _status_file.exists():
            try:
                import json as _json_status

                status = _json_status.loads(_status_file.read_text())
                resilience = status.get("resilience_state", "")
                if resilience in ("critical", "degraded_critical"):
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(
                        "## GENESIS ALERT: System Issue\n\n"
                        f"Resilience state: **{resilience}**. "
                        "Use `health_status` MCP tool for details."
                    )
                    first = False
            except (OSError, ValueError):
                pass  # Never block session start

        # Fallback NOTICE (advisory): Genesis's server-side ConversationLoop is
        # currently running on a roster peer (e.g. GLM) because the home model
        # (Claude) is rate-limited/exhausted account-wide. This interactive CLI
        # session runs on CC-native pinned Claude and does NOT fail over itself
        # (failover is server-side, a different process) — so the [model] header
        # stays honest and we surface the server's degraded condition here as a
        # separate block. Read the cross-process state file directly (import-free,
        # fail-open), mirroring the status.json read above.
        # Writer: src/genesis/cc/fallback_state.py.
        _fallback_file = Path.home() / ".genesis" / "cc_fallback_state.json"
        if _in("knowledge") and _fallback_file.exists():
            try:
                import json as _json_fb

                fb = _json_fb.loads(_fallback_file.read_text())
                if isinstance(fb, dict) and fb.get("is_fallback"):
                    peer = fb.get("fallback") or "a roster peer"
                    home = fb.get("original") or "Claude"
                    reason = (fb.get("reason") or "unknown").replace("_", " ")
                    since = str(fb.get("since") or "")
                    since_disp = (since.replace("T", " ")[:16] + " UTC") if since else "unknown"
                    if not first:
                        _emit("\n\n---\n\n")
                    _emit(
                        "## ⚠ Genesis Fallback Active\n\n"
                        f"Genesis's server-side conversation is running on **{peer}** "
                        f"because **{home}** is unavailable (reason: {reason}; since "
                        f"{since_disp}). This is account-wide. *Your* interactive session "
                        "here still runs on native Claude — this notice reflects the "
                        "Genesis server's state, not this CLI session."
                    )
                    first = False
            except (OSError, ValueError):
                pass  # Advisory only — never block session start

        # Routed-session NOTICE (advisory): surfaces when `gmodel <peer>` launched
        # this window on a non-Anthropic roster model (see _routed_session_notice).
        # Never elided (correctness: it tells the model what it is running on).
        _routed_notice = (
            _routed_session_notice(os.environ.get("GENESIS_ROSTER_MODEL"))
            if _in("knowledge")
            else None
        )
        if _routed_notice:
            if not first:
                _emit("\n\n---\n\n")
            _emit(_routed_notice)
            first = False

    # 2.5. Active procedures (advisory, silent failure is correct)
    try:
        from genesis.learning.procedural.session_inject import load_active_procedures

        _db_path = Path.home() / "genesis" / "data" / "genesis.db"
        procedures = asyncio.run(load_active_procedures(_db_path)) if _in("knowledge") else ""
        if procedures:
            if not first:
                _emit("\n\n---\n\n")
            _emit(
                "## Active Procedures\n\n"
                "Learned procedures — follow these before inventing new approaches.\n\n"
                + procedures
            )
            first = False
    except Exception:
        pass  # Procedures are advisory; silent failure is correct

    # 2.6. Codebase L0 — package index from AST code index (advisory)
    if not is_genesis_session and _in("knowledge"):
        try:
            import aiosqlite

            _db_path_l0 = Path.home() / "genesis" / "data" / "genesis.db"
            if _db_path_l0.exists():

                async def _load_l0():
                    async with aiosqlite.connect(str(_db_path_l0), timeout=2) as db:
                        db.row_factory = aiosqlite.Row
                        cursor = await db.execute(
                            "SELECT package, COUNT(*) as modules, SUM(loc) as loc "
                            "FROM code_modules GROUP BY package ORDER BY loc DESC"
                        )
                        return await cursor.fetchall()

                rows = asyncio.run(_load_l0())
                if rows:
                    lines = ["## Codebase\n"]
                    # Show top 15 packages, summarize the rest
                    top = rows[:15]
                    rest = rows[15:]
                    for r in top:
                        lines.append(
                            f"- **{r['package']}**: {r['modules']} modules, {r['loc']} LOC"
                        )
                    if rest:
                        rest_mods = sum(r["modules"] for r in rest)
                        rest_loc = sum(r["loc"] for r in rest)
                        lines.append(
                            f"- *{len(rest)} more packages*: {rest_mods} modules, {rest_loc} LOC"
                        )
                    lines.append(
                        "\nUse `codebase_navigate` MCP tool for drill-down "
                        "(L1: modules in a package, L2: symbols in a module)."
                    )
                    # Elidable: the package index is a convenience view of data
                    # `codebase_navigate` serves on demand. The writer decides
                    # whether it fits — this block states only the alternative.
                    _writer().emit_or_degrade(
                        "\n".join(lines),
                        block="codebase-l0",
                        divider="" if first else "\n\n---\n\n",
                        pointer=(
                            "## Codebase\n\nPackage index omitted for byte budget — "
                            "use the `codebase_navigate` MCP tool (L0: packages, "
                            "L1: modules, L2: symbols)."
                        ),
                    )
                    first = False
        except Exception:
            pass  # Codebase index is advisory — silent failure is correct

    # 3. Previous session context (temporal awareness — uses pre-loaded data)
    try:
        prev_session = _format_previous_session(last_session_data) if _in("knowledge") else None
        if prev_session:
            if not first:
                _emit("\n\n---\n\n")
            _emit(prev_session)
            first = False
    except Exception:
        pass  # Previous session context is advisory

    # 4. Resume signal (user signaled they want to return)
    try:
        resume_signal = _load_resume_signal() if _in("knowledge") else None
        if resume_signal:
            if not first:
                _emit("\n\n---\n\n")
            _emit(resume_signal)
            first = False
    except Exception:
        pass  # Resume signal is advisory

    # 5. Capabilities + MCP tools (dynamic from registry, fallback to static)
    # Knowledge part only — the remaining blocks all belong to it, so parts
    # that are not "knowledge" finish here with their audit + marker.
    if not _in("knowledge"):
        return part, _hook_session_id, miswired
    if not first:
        _emit("\n\n---\n\n")

    _cap_file = Path.home() / ".genesis" / "capabilities.json"
    _mcp_fallback = (
        "## Genesis MCP Tools Available\n\n"
        "You have genesis-health, genesis-memory, genesis-outreach, and genesis-recon MCP servers.\n"
        "Use memory tools (memory_recall, memory_store) for cross-session knowledge.\n"
        "Use health tools (health_status, health_errors, health_alerts) for system state.\n"
        "Use session_config tool to switch model and/or effort.\n"
        "Use outreach tools (outreach_queue, outreach_digest) to check proactive messages.\n"
        "Use recon tools for project watchlist and findings.\n"
        "Use bookmark tools (bookmark_shelve, bookmark_unshelve) to save and find sessions."
    )
    if _cap_file.exists():
        try:
            import json

            caps = json.loads(_cap_file.read_text())
            lines = ["## Genesis Capabilities\n"]
            for cname, cinfo in caps.items():
                cstatus = cinfo.get("status", "unknown")
                cdesc = cinfo.get("description") or ""
                # Drop the description when it's absent or merely repeats the
                # capability name: a registry entry with no real description
                # renders as "reflex: reflex" (or, namespaced, "module:architect:
                # architect") — pure noise. The name still lists.
                _cname_tail = cname.split(":")[-1]
                desc_part = f": {cdesc}" if cdesc and cdesc not in (cname, _cname_tail) else ""
                if cstatus == "active":
                    lines.append(f"- **{cname}**{desc_part}")
                else:
                    cerr = cinfo.get("error", "")
                    suffix = f" — Error: {cerr}" if cerr else ""
                    lines.append(f"- **{cname}** [{cstatus}]{desc_part}{suffix}")
            # The full MCP tool list is injected separately by CC; a hardcoded
            # partial list here is redundant + goes stale, so only the Skill
            # Library pointer (not injected elsewhere) is kept.
            lines.append(
                "\n**Skill Library:** Browse `src/genesis/skills/` or "
                "`~/.genesis/skill-library/` for specialized skills "
                "(research, outreach, browser automation, etc.). "
                "The skill injection hook nudges you when one matches."
            )
            _caps_block = "\n".join(lines)
            # First tier of the elision ladder: the capability roster is the
            # largest block whose content is recoverable on demand, and CC
            # injects the real MCP tool list separately anyway (see the comment
            # above). Degrade to the fallback pointer before touching anything
            # per-session.
            _writer().emit_or_degrade(_caps_block, block="capabilities", pointer=_mcp_fallback)
        except Exception:
            _emit(_mcp_fallback)
    else:
        _emit(_mcp_fallback)

    # 6. MCP server crash warnings — loud alert when MCP servers failed to start
    _mcp_crash_dir = Path.home() / ".genesis" / "mcp_crashes"
    if _mcp_crash_dir.is_dir():
        try:
            import json as _json

            crash_entries = []
            for crash_file in sorted(_mcp_crash_dir.glob("*.json")):
                try:
                    info = _json.loads(crash_file.read_text())
                    crash_entries.append(info)
                except (ValueError, OSError):
                    crash_entries.append(
                        {"server": crash_file.stem, "error": "unreadable crash file"}
                    )
            if crash_entries:
                _emit("\n\n---\n\n")
                _emit("## GENESIS ALERT: MCP Server Crashes\n\n")
                _emit(
                    "The following MCP servers failed to start and their tools are "
                    "**UNAVAILABLE** in this session:\n\n"
                )
                for info in crash_entries:
                    srv = info.get("server", "unknown")
                    err = info.get("error", "unknown error")
                    ts = info.get("timestamp", "")
                    ts_note = f" (at {ts})" if ts else ""
                    _emit(f"- **genesis-{srv}**: `{err}`{ts_note}\n")
                _emit(
                    "\n**Impact:** Tools from crashed servers will not appear. "
                    "Fix the root cause and restart the session.\n"
                )
        except Exception:
            pass  # Crash reporting itself must not crash the hook

    return part, _hook_session_id, miswired


def main() -> None:
    """Emit one part, and ALWAYS close it out — audit line, mirror, stderr.

    The body is wrapped because a part that dies mid-emission used to leave a
    recovery header and nothing else: no audit line, no mirror, and a header
    whose own trigger condition (a persisted-output preview) was not met, so the
    model had no reason to act. That is the silent-loss shape this change
    exists to remove, re-created one level down — MEASURED with a non-UTF-8
    identity file. The audit line is the COMPLETION PROOF, and its absence is
    not itself a signal, so it must be emitted on every path.
    """
    part, session_id, miswired = "charter", "", ""
    try:
        result = _emit_body()
        if result is None:
            # A deliberate no-op: the eject lever is off, or the probe seam
            # already wrote its own payload. Nothing was opened, so there is
            # nothing to close — and emitting an audit line here would put
            # Genesis output into a session that asked for none.
            return
        part, session_id, miswired = result
    except Exception as exc:  # noqa: BLE001 — a part must never die silently
        part = _current_part(part)
        session_id = _current_session_id(session_id)
        _emit(
            f"## GENESIS ALERT: context part '{part}' FAILED mid-emission\n\n"
            f"`{type(exc).__name__}: {exc}` — this window is MISSING the rest of that "
            "part. Tell the user, and check the hook's stderr for the traceback.",
            block="part-failure",
        )
        import traceback

        traceback.print_exc(file=sys.stderr)
    finally:
        # Only if the stream was actually opened. _begin_part is what opens it,
        # and every early return above happens before that point.
        if _OUT is not None:
            _finish_part(part, session_id, miswired)


#: Append-only record of mis-wired invocations, read by the awareness watcher.
#: NOT a revival of the deleted marker layer: nothing clears it, nothing
#: read-modify-writes it, and it has no per-part lifecycle — a line ages out of
#: the watcher's lookback on its own. It exists because the in-band alert is
#: only seen by a model in a window someone reads, and a mis-wire in a `-p` or
#: unattended session would otherwise have NO observer: a mis-wired part stays
#: under the cap by design, so the harness never files it and the filings
#: watcher never sees it.
_MISWIRE_LOG = Path.home() / ".genesis" / "session_awareness" / "context_miswire.log"


def _record_miswire(reason: str) -> None:
    """Append ONE line about a mis-wired invocation. Fail-open, never fatal.

    The reason is collapsed to a single line first. It is built from `sys.argv`,
    and this log is TAB-DELIMITED and line-oriented: a newline inside it would
    append a second entry that the collector's parser then reads as a genuine,
    separately-timestamped mis-wire. The read side escapes what it renders, so
    this forges the log's STRUCTURE rather than a rendered line — normalised at
    the write, where the record's one-line-per-event shape is decided.
    """
    try:
        _MISWIRE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _MISWIRE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(UTC).isoformat()}\t{' '.join(str(reason).split())}\n")
    except OSError as exc:
        print(f"[session_context] could not record the mis-wire: {exc}", file=sys.stderr)


def _write_mirror(path: Path | None, text: str) -> bool:
    """Write this part's FULL intended text. Returns True on success.

    Fail-open: a mirror that cannot be written must never break session start.
    Loud, though — the recovery header points at this path, and a header naming
    a file that is not there is worse than one that admits the gap.
    """
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        print(
            f"[session_context] could not mirror part to {path}: {exc}. A truncated "
            "part would not be recoverable from disk in this session.",
            file=sys.stderr,
        )
        return False


def _finish_part(part: str, session_id: str, miswired: str = "") -> None:
    """Mirror the full text, then close with an honest audit line.

    There is no over-budget MARKER file any more, and nothing screams per
    prompt. Both were deleted deliberately: a marker records that the emitter
    knew it was over budget, and after the chokepoint in ``_emit`` the emitter
    can no longer GO over budget, so the state it recorded became unreachable.
    The two cases a marker never covered — the harness cap moving BELOW our
    budget, and any other hook's output being filed — are covered by the
    recovery header (in-band, inside the harness's own preview) and by the
    hourly watcher over the harness's filings, neither of which needs state.

    The audit line reports INTENDED vs EMITTED. After a chokepoint a bare
    "N/budget" is always green and therefore says nothing; the number that
    carries information is what was dropped, and where to read it.
    """
    out = _writer()
    mirror = _mirror_path(session_id, part) if part != "all" else None
    wrote_mirror = _write_mirror(mirror, out.intended)
    cut = out.cut
    if cut:
        block, dropped = cut
        where = f" — full text: {mirror}" if wrote_mirror else " — MIRROR UNAVAILABLE"
        out.emit_final(
            _audit_line(part, out.intended_chars, out.emitted_chars, cut=cut, where=where)
        )
    else:
        out.emit_final(_audit_line(part, out.emitted_chars, out.emitted_chars))
    if miswired:
        _record_miswire(miswired)
        print(
            f"[session_context] MIS-WIRED: {miswired}. Emitted the charter part only "
            "so this invocation stays under the harness cap; the rest of the context "
            "is MISSING from this session. Fix the SessionStart wiring in "
            ".claude/settings.json (four --part entries).",
            file=sys.stderr,
        )
        return
    if cut:
        print(
            f"[session_context] part '{part}' wanted {out.intended_chars} chars against a "
            f"{_PART_BUDGET}-char budget (the harness FILES hook stdout above "
            f"{_HOOK_STDOUT_CAP} chars and previews ~2 KB) — it was truncated in-band at "
            f"'{cut[0]}' rather than risking the whole part. Full text: {mirror}.",
            file=sys.stderr,
        )


def _load_last_session_data() -> dict | None:
    """Load last foreground session JSON from disk (single read for all consumers)."""
    import json

    last_session_file = Path.home() / ".genesis" / "last_foreground_session.json"
    if not last_session_file.exists():
        return None
    try:
        return json.loads(last_session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _format_previous_session(data: dict | None) -> str | None:
    """Format previous session context for temporal awareness."""
    if not data:
        return None

    ended_at = data.get("ended_at", "")
    topic_hint = data.get("topic_hint", "")
    session_id = data.get("session_id", "")

    if not ended_at:
        return None

    try:
        from genesis.util.tz import fmt as _tz_fmt

        formatted = _tz_fmt(ended_at)
    except (ValueError, TypeError, ImportError):
        formatted = ended_at

    parts = [f"Previous session: {formatted}"]
    if session_id:
        parts.append(f"ID: {session_id[:8]}")
    if topic_hint:
        parts.append(f"Topic: {topic_hint}")

    return f"[{' | '.join(parts)}]"


def _compute_activity_tier(
    last_session_data: dict | None,
    foreground_count_24h: int = 0,
) -> str:
    """Compute activity tier from session recency and frequency.

    Returns "active", "returning", or "away".
    """
    if not last_session_data:
        return "away"

    ended_at = last_session_data.get("ended_at", "")
    if not ended_at:
        return "away"

    try:
        ended_dt = datetime.fromisoformat(ended_at)
        gap_hours = (datetime.now(UTC) - ended_dt).total_seconds() / 3600
    except (ValueError, TypeError):
        return "away"

    if gap_hours < 2 or foreground_count_24h >= 3:
        return "active"
    elif gap_hours < 24:
        return "returning"
    else:
        return "away"


def _load_resume_signal() -> str | None:
    """Load resume signal if user signaled they want to return."""
    import json

    signal_file = Path.home() / ".genesis" / "last_resume_signal.json"
    if not signal_file.exists():
        return None

    try:
        data = json.loads(signal_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    signal = data.get("signal", "")
    session_id = data.get("session_id", "")

    if not signal:
        return None

    # Clear the signal file so it doesn't repeat
    import contextlib

    with contextlib.suppress(OSError):
        signal_file.unlink()

    msg = f'You signaled you wanted to return to a previous session ("{signal}").'
    if session_id:
        msg += f" Session ID: {session_id}"
    msg += " Use bookmark_unshelve to find it, or `claude --resume <id>` to resume directly."
    return msg


async def _foreground_session_count_24h(db) -> int:
    """Count foreground sessions in the last 24 hours."""
    try:
        cur = await db.execute(
            "SELECT COUNT(*) FROM cc_sessions "
            "WHERE source_tag = 'foreground' "
            "AND started_at > strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now', '-24 hours')"
        )
        row = await cur.fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


async def _load_cognitive_state(last_session_data: dict | None = None) -> str | None:
    from genesis.db.connection import get_db
    from genesis.db.crud import cognitive_state

    db = await get_db()
    try:
        fg_count = await _foreground_session_count_24h(db)
        tier = _compute_activity_tier(last_session_data, fg_count)
        return await cognitive_state.render(db, activity_tier=tier)
    finally:
        await db.close()


def _inflight_emission_chunks(inflight: str, *, ek_emitted: bool, first: bool) -> list[str]:
    """Emission chunks for the in-flight block (pure — drives the foreground branch).

    Folds directly under Essential Knowledge with NO "---" divider when EK was
    already emitted (so it reads as one continuous context block); otherwise it
    stands alone, preceded by the standard divider only when it is not the first
    block. Returns [] for an empty block (nothing to emit). Extracted so the
    fold/divider logic is unit-testable without a full subprocess run.
    """
    if not inflight:
        return []
    if ek_emitted:
        return ["\n\n" + inflight]  # fold under EK, no divider
    chunks: list[str] = []
    if not first:
        chunks.append("\n\n---\n\n")
    chunks.append(inflight)
    return chunks


def _load_inflight_block() -> str:
    """Fresh in-flight working-state block for the foreground session context.

    Opens its own short-timeout connection (mirroring the "2.6 Codebase L0"
    block above — isolated, never touches the shared runtime connection) and
    delegates assembly to genesis.memory.open_loops.build_inflight_block.
    Fail-open: any error returns "" so session start is never blocked.
    """
    try:
        import aiosqlite

        db_path = Path.home() / "genesis" / "data" / "genesis.db"
        if not db_path.exists():
            return ""
        repo_root = Path.home() / "genesis"
        plans_dir = Path.home() / ".claude" / "plans"

        async def _run() -> str:
            from genesis.memory.open_loops import build_inflight_block

            async with aiosqlite.connect(str(db_path), timeout=2) as db:
                db.row_factory = aiosqlite.Row
                return await build_inflight_block(db, repo_root=repo_root, plans_dir=plans_dir)

        return asyncio.run(_run())
    except Exception:
        return ""  # Advisory — never block session start


def _charter_db_path() -> Path:
    """genesis.db location, GENESIS_REPO_ROOT-aware (same resolution as the
    PreCompact hook so reader and writer always agree)."""
    import os

    root = os.environ.get("GENESIS_REPO_ROOT", "")
    base = Path(root) if root else Path.home() / "genesis"
    return base / "data" / "genesis.db"


def _spawn_repo_pulse_worker(source: str) -> None:
    """Fire-and-forget the detached repo-pulse worker (session-manager PR-4a).

    GLOBAL, not per-session: the worker enumerates merged PRs since its own
    cursor and matches them against open ledger rows across ALL sessions;
    its internal 30-minute debounce makes redundant spawns exit in ~100ms
    (spawn-then-exit, the ledger-shadow settings posture). Foreground
    boundaries only, never on clear (/clear is a fresh start). Fail-open
    end-to-end — cost to this hook is one Popen; a pulse cannot run in-hook
    because one gh round-trip (30s budget) exceeds the hook's whole budget.
    """
    import subprocess

    try:
        if os.environ.get("GENESIS_REPO_PULSE_DISABLED") == "1":
            return
        if source == "clear":
            return
        script = Path(__file__).resolve().parent / "repo_pulse_worker.py"
        err_log = Path.home() / ".genesis" / "session_awareness" / "repo_pulse_err.log"
        err_log.parent.mkdir(parents=True, exist_ok=True)
        with err_log.open("ab") as err_fh:
            subprocess.Popen(  # noqa: S603 — fixed argv, sys.executable
                [
                    sys.executable,
                    str(script),
                    "--trigger",
                    "session_start",
                    # The hook's home-anchored DB resolution is the source of
                    # truth: a worktree session's worker must not fall back
                    # to genesis.env's repo-anchored default (worktree/data/
                    # is a void — silent no-op coverage loss).
                    "--db-path",
                    str(_charter_db_path()),
                ],
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err_fh,
            )
    except Exception:
        pass  # fail-open: pulse is advisory, session start is not


def _pulse_floor() -> float:
    """``inject_confidence_floor`` from the merged repo_pulse config (base
    yaml ← ``~/.genesis/config/repo_pulse.local.yaml`` overlay), defaulting
    to 0.7 on any damage. Flat local merge — this hook stays free of
    genesis imports by design (dependency-light session start).
    """
    floor = 0.7
    try:
        import yaml

        root = os.environ.get("GENESIS_REPO_ROOT", "")
        base_dir = Path(root) if root else Path.home() / "genesis"
        merged: dict = {}
        for path in (
            base_dir / "config" / "repo_pulse.yaml",
            Path.home() / ".genesis" / "config" / "repo_pulse.local.yaml",
        ):
            try:
                loaded = yaml.safe_load(path.read_text())
                if isinstance(loaded, dict):
                    merged.update(loaded)
            except Exception:
                continue
        value = merged.get("inject_confidence_floor", floor)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1:
            floor = float(value)
    except Exception:
        pass
    return floor


# Mirrors MAX_MISSION_CHARS / the origin cap in db/crud/session_charters.py.
# The store bounds these on write; re-cutting them here only hid text.
_MISSION_CAP = 1_000
_ORIGIN_CAP = 1_200
# Whole-block ceiling. Generous by design: the charter is the one per-session,
# actionable block in the injection, and a real one (origin + mission + a few
# open rows) is a fraction of this. When it IS hit the degrade is structured —
# see _shrink_charter_block — never a mid-row slice.
# Squeezed between two MEASURED constraints, both pinned by
# test_charter_part_overhead_fits_the_budget:
#
#   FLOOR   the tier-4 degrade (bare ids for a full ledger) is 7,174 chars at
#           _LEDGER_FETCH_MAX rows. Below that the chokepoint would cut ids —
#           undoing the exact thing _shrink_charter_block exists to protect.
#   CEILING the part's fixed overhead is 2,098 chars (session-config 660 worst
#           case + onboarding pointer 607 + recovery header 200 + mis-wire
#           alert 487 + dividers + audit reserve) against a 9,800 budget.
#
# 7,500 sits **81** chars above the tier-4 floor — MEASURED 2026-08-31 at
# production shape (36-char session id in both the note and the footer, the
# overflow note set, since tier 4 is only reached with a full ledger). It is
# NOT the ~326 an earlier version of this comment claimed: that number came
# from a test fixture passing `footer="_footer_"` and `session_id="sid"`, which
# understated the footer by 245 chars.
#
# So this comment has now been wrong TWICE in the same way — a number derived
# from a convenient fixture and then written down as if measured from
# production. The fixture is fixed and the margin is asserted (>= 50) by
# test_charter_ceiling_stays_above_the_degrade_floor, which reports the real
# figure on failure. Do not re-derive it here; read it from the test.
#
# 81 chars is TIGHT. One more sentence in _LEDGER_OVERFLOW_NOTE or the footer
# breaches it, and the failure mode is the chokepoint cutting into ledger row
# ids — undoing the exact thing the tier-4 degrade exists for. Grow the ceiling
# (and re-check the part arithmetic pin) before growing either string.
_CHARTER_BLOCK_MAX = 7_500

#: Cap on the session id where it is RENDERED (the footer and the tier-4 note).
#: 64 is comfortably above the 36-char UUID the harness actually sends, and far
#: enough below the tier-4 margin that the ladder's "un-cuttable" claim does not
#: rest on an unvalidated external input. Never applied to a lookup.
_SESSION_ID_DISPLAY_CAP = 64

#: Sanity bound on the ledger read. Not a display cap — every open row renders,
#: and a 201st is ANNOUNCED rather than dropped (a silent cut here would be the
#: same defect as the old LIMIT 6, just further out).
_LEDGER_FETCH_MAX = 200
_LEDGER_OVERFLOW_NOTE = f"MORE than {_LEDGER_FETCH_MAX} open rows — the rest are not listed above"


def _ledger_lines(
    ledger: list[dict], escalations: dict, *, text_cap: int | None = None
) -> list[str]:
    """One line per OPEN ledger row: full text, full id, escalation link if any.

    Rows render UNCUT by default. The 120-char cut this replaces turned a
    founding agreement into a fragment that stopped mid-clause, which reads as a
    stray note rather than a commitment — the id is what `session_ledger_update`
    needs, and the text is what makes the row mean anything.

    ``text_cap`` is for the degrade tier only, and note WHERE it cuts: the id
    leads the line so shortening the text can never cost the handle. Capping a
    trailing-id format is how a first version of this dropped every id in the
    degrade — caught by its own test.
    """
    out = []
    for item in ledger:
        mark = "~" if item.get("status") == "in_progress" else " "
        row_id = str(item.get("id", ""))
        text = str(item.get("text", ""))
        link = ""
        esc = escalations.get(row_id)
        if esc:
            # A revived session must be able to close BOTH records; without the
            # id it would have to go looking for a follow-up it does not know
            # exists.
            link = f" → escalated: follow_up {str(esc[0])[:8]} ({esc[1]})"
        if text_cap is not None:
            if len(text) > text_cap:
                text = text[:text_cap] + "…"
            out.append(f"- [{mark}] (id: {row_id}) {text}{link}")
        else:
            out.append(f"- [{mark}] {text} (id: {row_id}){link}")
    return out


def _shrink_charter_block(
    header: str,
    ledger_header: str,
    ledger: list[dict],
    escalations: dict,
    footer: str,
    session_id: str,
) -> str:
    """Fit an oversized charter block WITHOUT losing an open row id.

    A flat slice at N characters cuts mid-row and takes every id after it, so
    the block silently stops naming the agreements it exists to name while still
    LOOKING complete. This degrades by TIER instead — prose, then row text, then
    rows to bare ids — rebuilding from the row DATA rather than re-cutting
    rendered strings. Re-cutting was the first version and it dropped every id,
    because the id trailed the text it was shortening; its own test caught it.
    The footer (carrying the charter.md path) survives every tier, so the full
    text is always one Read away.
    """
    note = (
        "_Origin and mission omitted for length \u2014 full charter at"
        f" ~/.genesis/sessions/{session_id}/charter.md_"
    )

    def _assemble(rows: list[str], ledger_line: str) -> str:
        return "\n".join([header, "", note, "", ledger_line, *rows, "", footer])

    # Tier 2: drop origin/mission/pointers/pulse; rows stay whole.
    tier2 = _assemble(_ledger_lines(ledger, escalations), ledger_header)
    if utf16_len(tier2) <= _CHARTER_BLOCK_MAX:
        return tier2

    # Tier 3: id FIRST, text trimmed to a lead fragment.
    tier3 = _assemble(_ledger_lines(ledger, escalations, text_cap=140), ledger_header)
    if utf16_len(tier3) <= _CHARTER_BLOCK_MAX:
        return tier3

    # Tier 4: ids only. Nothing else survives, but nothing is INVISIBLE — every
    # open row is still named and addressable by session_ledger_update.
    ids = [str(item.get("id", "")) for item in ledger]
    return _assemble(
        [f"- {i}" for i in ids],
        f"**Ledger (open) \u2014 {len(ids)} rows, ids only for length:**",
    )


def _escalation_dedup_key(ledger_id: str) -> str:
    """`follow_ups.dedup_key` linking a ledger row to its escalation follow-up.

    Inlined rather than imported: this hook is stdlib-only by design, so a broken
    venv can never wedge a session. `genesis.session_awareness.ledger_escalation_link`
    owns the formula and a parity test asserts these two agree — a change there
    that this does not follow fails loudly instead of silently unlinking rows.
    """
    import hashlib

    return hashlib.sha256(f"ledger_escalation|{ledger_id}".encode()).hexdigest()


def _load_charter_db(session_id: str, db_path: Path | None) -> tuple[dict | None, list[dict]]:
    """Charter row + open/in_progress ledger items from the canonical DB.

    Read-only WAL-aware connection (mode=ro — never immutable=1, which would
    miss un-checkpointed writes). Any failure — missing DB, missing table on
    a not-yet-migrated install, lock — returns (None, []) so the caller falls
    back to the legacy charter.json.
    """
    import json

    try:
        import aiosqlite

        db_file = db_path or _charter_db_path()
        if not db_file.exists():
            return None, []

        async def _run() -> tuple[dict | None, list[dict]]:
            async with aiosqlite.connect(f"file:{db_file}?mode=ro", uri=True, timeout=2) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute(
                    "SELECT * FROM session_charters WHERE session_id = ?",
                    (session_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return None, []
                charter = dict(row)
                try:
                    charter["pointers"] = json.loads(charter.get("pointers") or "[]")
                except (ValueError, TypeError):
                    charter["pointers"] = []
                cur = await db.execute(
                    # LIMIT is a sanity bound, not a display cap: every open row
                    # renders. The old LIMIT 6 silently dropped the 7th
                    # agreement, and a dropped row is indistinguishable from a
                    # session that never made one. Fetch ONE past the bound so
                    # the renderer can SAY that a 201st exists rather than
                    # reproducing the same silent cut one order of magnitude up.
                    # ORDER BY created_at, id: `created_at` is not unique (rows
                    # added in the same second tie), and SQLite may return tied
                    # rows in any order — so at the bound the SUBSET is
                    # arbitrary and can differ between two renders of an
                    # unchanged ledger. The id breaks the tie deterministically.
                    "SELECT id, text, status FROM session_ledger"
                    " WHERE session_id = ? AND status IN ('open','in_progress')"
                    " ORDER BY created_at, id LIMIT ?",
                    (session_id, _LEDGER_FETCH_MAX + 1),
                )
                items = [dict(r) for r in await cur.fetchall()]
                if len(items) > _LEDGER_FETCH_MAX:
                    items = items[:_LEDGER_FETCH_MAX]
                    # Recorded on the CHARTER, not as a pseudo-row: the block's
                    # degrade ladder can strip row text down to bare ids, which
                    # would eat a note carried as a row — reinstating the silent
                    # cut. The footer survives every tier.
                    charter["_ledger_overflow"] = True
                try:
                    # Escalation links (own guard, like the pulse read below):
                    # a row left undisposed long enough becomes a follow-up, and
                    # a session that comes back alive has to SEE that so it can
                    # close both. Keyed on the follow-up's dedup_key, which is
                    # uniquely indexed. An install without the follow_ups table
                    # renders exactly as before.
                    _keys = {_escalation_dedup_key(str(i["id"])): str(i["id"]) for i in items}
                    if _keys:
                        cur = await db.execute(
                            "SELECT id, status, dedup_key FROM follow_ups"  # noqa: S608
                            # Interpolates only a run of '?' placeholders whose
                            # COUNT comes from len(_keys); every value is bound.
                            f" WHERE dedup_key IN ({','.join('?' * len(_keys))})",
                            tuple(_keys),
                        )
                        _esc = {}
                        for r in await cur.fetchall():
                            _lid = _keys.get(r["dedup_key"])
                            if _lid:
                                _esc[_lid] = (r["id"], r["status"])
                        charter["_escalations"] = _esc
                except Exception:
                    charter["_escalations"] = {}
                cur = await db.execute(
                    "SELECT status, COUNT(*) FROM session_ledger"
                    " WHERE session_id = ? GROUP BY status",
                    (session_id,),
                )
                charter["_ledger_counts"] = {r[0]: r[1] for r in await cur.fetchall()}
                try:
                    # Repo-pulse proposals (PR-4a) — own guard: pre-0062
                    # installs have no pulse tables and the charter block
                    # must render byte-identically without them.
                    # Ledger-only: the rendered confirm command is
                    # session_ledger_update(...), which can only resolve a
                    # ledger id. follow_up-target proposals (target_kind added by
                    # migration 0084) are session-agnostic and get their own
                    # global surface — never surface them here with a confirm
                    # command that can't find them. Pre-0084 DBs lack the column;
                    # the enclosing try/except then renders the block empty (same
                    # graceful degradation as the pre-0062 no-tables case).
                    cur = await db.execute(
                        "SELECT item_id, item_text, pr_number, pr_title"
                        " FROM repo_pulse_annotations"
                        " WHERE item_session_id = ? AND status = 'proposed'"
                        " AND target_kind = 'ledger'"
                        " AND (confidence IS NULL OR confidence >= ?)"
                        " ORDER BY observed_at DESC LIMIT 3",
                        (session_id, _pulse_floor()),
                    )
                    charter["_pulse_proposals"] = [dict(r) for r in await cur.fetchall()]
                except Exception:
                    charter["_pulse_proposals"] = []
                return charter, items

        return asyncio.run(_run())
    except Exception:
        return None, []


def _load_charter_file(session_id: str, sessions_dir: Path | None) -> dict | None:
    """Legacy fallback: pre-0058 charter.json (still on disk for sessions the
    one-off backfill has not imported, or when the DB is unreachable).

    Goes through the shared id chokepoint like every other path in this file.
    It previously joined the raw ``session_id`` — which arrives unvalidated from
    the hook's stdin JSON — so a traversal id read any reachable charter.json
    and folded it into the block shown to the model. Read-only and fail-closed
    on a miss, but a traversal read all the same, and exactly the omission the
    chokepoint exists to prevent: the guard sat two functions away.
    """
    import json

    base = sessions_dir or (Path.home() / ".genesis" / "sessions")
    try:
        from hook_input import session_path
    except Exception:
        return None
    session_dir = session_path(base, session_id)
    if session_dir is None:
        return None
    try:
        return json.loads((session_dir / "charter.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _charter_emission_block(
    session_id: str,
    source: str,
    *,
    sessions_dir: Path | None = None,
    db_path: Path | None = None,
) -> str:
    """Session-charter block for the foreground context.

    DB-first (session_charters + open ledger items, migration 0058), falling
    back to the legacy charter.json. Emitted on startup/resume/compact so a
    chartered session gets its origin AND its open ledger back in EVERY
    window — but NOT on clear: /clear is an explicit fresh start, and
    re-asserting the old origin would fight the user.

    Returns "" when there is no charter or nothing is readable (fail-open —
    charter is advisory). An id that is unsafe as a PATH COMPONENT still gets its
    canonical DB-backed charter; only the legacy file fallback is skipped.
    """
    if not session_id or source == "clear":
        return ""
    # Bound the id BEFORE it reaches any rendered string. It is an external input
    # (the harness supplies it) and it lands in two budget-critical places: the
    # footer, and the tier-4 note in `_shrink_charter_block` — the one tier with
    # no ceiling check, which returns unconditionally on the argument that bare
    # ids always fit. MEASURED: with a 36-char UUID the tier-4 floor is 7,419
    # against the 7,500 ceiling (margin 81); with a 200-char id it is 7,747 —
    # 247 OVER, which then eats the charter part's own margin and lets the writer
    # cut into the ledger ids that the tier ladder exists to protect. Reachability
    # is low (CC sends a UUID), but "un-cuttable by construction" cannot rest on
    # an unvalidated input. The clamp is applied AFTER the lookups below, so a
    # long id still resolves to its real charter — this bounds what is RENDERED,
    # not what is queried.
    # The DB lookup binds session_id as a SQL PARAMETER, so it is safe for any id;
    # only the legacy charter.json fallback interpolates it into a PATH. Guarding
    # above both would drop a canonical, DB-backed charter (and its ledger) for an
    # id that merely fails the path rule.
    charter, ledger = _load_charter_db(session_id, db_path)
    if charter is None and "/" not in session_id and ".." not in session_id:
        charter = _load_charter_file(session_id, sessions_dir)
        ledger = []
    if charter is None:
        return ""
    session_id = session_id[:_SESSION_ID_DISPLAY_CAP]
    origin = str(charter.get("origin_prompt") or "").strip()
    if not origin:
        return ""
    if len(origin) > _ORIGIN_CAP:
        origin = origin[:_ORIGIN_CAP] + " …[truncated — full text in charter.md]"
    origin_quoted = "\n".join(f"> {line}" for line in origin.splitlines())

    lines = [
        "## Session Charter (persists across compaction)",
        "",
        f"**Origin — the prompt this session was born from"
        f" ({charter.get('origin_ts') or 'time unknown'}):**",
        origin_quoted,
    ]
    mission = str(charter.get("mission") or "").strip()
    compactions = charter.get("compaction_count", 0) or 0
    if mission:
        # Cap mirrors MAX_MISSION_CHARS in db/crud/session_charters.py — the
        # store already bounds this, so re-cutting it at 200 only ever hid text
        # the writer deliberately kept. (Not imported: this hook is stdlib-only.)
        lines += ["", f"**Mission:** {mission[:_MISSION_CAP]}"]
    elif compactions >= 1:
        # A charter whose mission was never set falls back to the raw origin
        # everywhere it is displayed, and a raw origin is often a half-formed
        # first sentence that reads as noise — so the session tunes it out and
        # the ledger goes with it. After a compaction the purpose IS knowable,
        # so say the field is empty rather than showing something that isn't.
        lines += [
            "",
            f"**Mission:** _not set after {compactions} compactions — set it via"
            " session_charter_update when the purpose is clear._",
        ]
    pointers = charter.get("pointers") or []
    if pointers:
        lines += ["", "**Pointers:**"]
        lines += [f"- {str(p)[:100]}" for p in pointers[:6]]
    escalations = charter.get("_escalations") or {}
    ledger_header = "**Ledger (open) — close via session_ledger_update:**"
    if ledger:
        lines += ["", ledger_header]
        lines += _ledger_lines(ledger, escalations)
    proposals = charter.get("_pulse_proposals") or []
    if proposals:
        # Repo-pulse fuzzy/bare-hex proposals (PR-4a): the exact marker tier
        # needs no line here — an absorb shrinks the open list above and its
        # evidence column tells the story.
        lines += ["", "**Pulse (proposed — confirm or ignore):**"]
        for p in proposals:
            # The hint carries the PR as evidence so a user-confirmed absorb
            # reconciles to 'confirmed' (same-PR attribution guard) instead
            # of 'superseded' — confirmed proposals ARE the precision metric.
            lines.append(
                f"- ~ '{str(p.get('item_text') or '')[:60]}' looks shipped by"
                f" PR #{p.get('pr_number')} '{str(p.get('pr_title') or '')[:50]}'"
                f" — confirm: session_ledger_update('{p.get('item_id') or ''}',"
                f" status='absorbed', evidence='PR #{p.get('pr_number')}')"
            )
    count = charter.get("compaction_count", 0)
    counts = charter.get("_ledger_counts") or {}
    footer = f"_Compactions: {count}"
    if counts:
        open_n = counts.get("open", 0) + counts.get("in_progress", 0)
        closed_n = sum(counts.values()) - open_n
        footer += f" · ledger: {open_n} open / {closed_n} closed"
    if charter.get("_ledger_overflow"):
        footer += f" · {_LEDGER_OVERFLOW_NOTE}"
    footer += f" · full charter: ~/.genesis/sessions/{session_id}/charter.md_"
    block = "\n".join([*lines, "", footer])
    # Measured in UTF-16 code units, the unit `_PART_BUDGET` is enforced in.
    # `_CHARTER_BLOCK_MAX` exists ONLY to keep this block inside that budget, and
    # the charter carries the most user-authored text in the injection — mission,
    # origin prompt, ledger rows, all typed by a human who may well use an emoji.
    # Sized with `len`, a charter of 7,500 codepoints could cost up to 15,000
    # units, pass this ceiling, and void the part's arithmetic pin — the pin that
    # makes the charter un-cuttable BY CONSTRUCTION. It would then be the writer's
    # cut path, not this structured degrade, that decided which agreements
    # survived. (The `text_cap`/`_ORIGIN_CAP` trims below stay in CODEPOINTS on
    # purpose: those are display trims — "140 characters of lead text" is a
    # statement about what a reader sees, not about budget.)
    if utf16_len(block) > _CHARTER_BLOCK_MAX:
        # Structured degrade, not a slice: a flat cut lands mid-row and takes
        # every id after it with no marker, so the block stops naming the
        # agreements it exists to name while still LOOKING complete.
        block = _shrink_charter_block(
            lines[0], ledger_header, ledger, escalations, footer, session_id
        )
    return block


if __name__ == "__main__":
    main()
