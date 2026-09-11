#!/usr/bin/env python3
"""Stop hook: detect resume signals, giving-up, and unverified completion claims.

Runs when Claude finishes responding (via .claude/settings.json Stop hook).

1. Checks the user's last message for natural language signals that they want
   to return to this session later ("let's revisit", "park this", etc.).
   If detected, writes ~/.genesis/last_resume_signal.json.

2. Checks the assistant's last message for giving-up patterns — phrases that
   delegate work back to the user instead of exhausting available tools.

3. Checks the assistant's last message for completion claims without
   verification evidence: finishing language with no integration or e2e proof.

Reads hook input from stdin as JSON:
  {"session_id": "...", "last_assistant_message": "...", "stop_hook_active": ...}

Skips background sessions (GENESIS_CC_SESSION=1).

DELIVERY — and why it is a CONTINUATION, not a message
------------------------------------------------------
Checks 2 and 3 used to `print()`, and this docstring used to say each one
"outputs a nudge for the next turn" and "gets injected into context". Both
halves were wrong, and the second was wrong in a way worth spelling out,
because the obvious repair is also wrong.

Claude Code puts a hook's bare stdout in front of the model for three events
only — SessionStart, UserPromptSubmit, UserPromptExpansion. On Stop it does
not, so those prints went to the debug log and reached nobody.

But Stop's JSON channel is not an inbox. READ from the 2.1.246 bundle: the
Stop handler pushes `additionalContexts` into the array it returns as
`blockingErrors`, and the agent loop reads a non-empty `blockingErrors` as
"the hook refused to let this turn end" — it appends the text and CONTINUES,
with transition reason `stop_hook_blocking`. So `additionalContext` here means
"don't stop yet, and here is why", never "tell the model this next time".

That is the right shape for these two nudges — handing work back to the user,
or claiming completion without evidence, are both cases where not stopping is
the point — but it is a behaviour change and not merely a delivery fix.

It also has to be BOUNDED. The harness caps consecutive blocks at
CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (default 8) and then emits a user-visible
override warning, and its own text names the guard: check `stop_hook_active`
and stay quiet while it is true. `main` does exactly that, so a nudge costs at
most one extra turn.

A third check — unreviewed code changes — deliberately does NOT live here. It
is state-based rather than message-based, so it would re-fire until a review
marker appeared and run straight to that cap. It is also already delivered:
`scripts/review_enforcement_prompt.py` runs the same `has_code_changes()` /
`is_review_current()` predicate on UserPromptSubmit, whose stdout the model
does receive.

The two messages are emitted as ONE envelope. A hook's stdout must be a single
JSON document, so printing one object per nudge would produce concatenated
JSON that parses as nothing — the same silence by a different route.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

# The shared hook-input helper lives in scripts/hooks/; this script runs from
# scripts/ (a different sys.path[0]), so add the hooks dir before importing it.
sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
from hook_input import session_path  # noqa: E402
from hook_output import print_json_bounded  # noqa: E402

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"
_GENESIS_DIR = Path.home() / ".genesis"
_RESUME_SIGNAL_FILE = _GENESIS_DIR / "last_resume_signal.json"

# Patterns that suggest the user wants to come back to this session.
# Intentionally broad — false positives are cheap (just a note on next start),
# false negatives lose a signal the user actually wanted.
_RESUME_PATTERNS = re.compile(
    r"(?:"
    r"(?:let(?:'s)?|we\s+should)\s+(?:come\s+back|revisit|return|pick\s+(?:this|it)\s+up)"
    r"|park\s+this"
    r"|shelve\s+this"
    r"|continue\s+(?:this\s+)?(?:tomorrow|later|next\s+time)"
    r"|pick\s+(?:this|it)\s+up\s+(?:tomorrow|later|next)"
    r"|think\s+on\s+this"
    r"|sleep\s+on\s+(?:this|it)"
    r"|come\s+back\s+to\s+(?:this|it)"
    r"|resume\s+(?:this\s+)?later"
    r"|save\s+(?:this|our)\s+(?:place|progress|spot)"
    r")",
    re.IGNORECASE,
)


# A turn that ENDS by handing control to the user — a question, or an explicit
# request for a decision. Both nudges below mean "don't stop yet", and this
# channel enforces that by refusing to end the turn; but a turn that is asking
# the user something is already stopping CORRECTLY, and refusing to end it makes
# the model talk past the person it is waiting on.
#
# This mattered only once the nudges started blocking. MEASURED against the real
# predicates: 3 of 6 sampled yielding turns fired one, including "Ready to merge.
# Shall I open the PR?" — the repo's own approval moment. `_FINISHING_PATTERNS`
# even contains a literal question alternative (`what would you like to do?`),
# which is a yielding turn by construction. Harmless while the output went
# nowhere; a wasted turn now.
#
# Fail direction is deliberate: a false SUPPRESS costs one advisory (the status
# quo for years, and harmless), a false FIRE costs a model turn and talks over
# the user. So this errs toward suppressing. The trailing clause is length-bound
# so a question buried before a long closing paragraph does not qualify.
#
# PRECEDENCE — the suppressor outranks BOTH nudges, including a giving-up match.
# Raised in review as a bug: a delegation phrased as a question ("You'll need to
# run the migration yourself. Can you do that?") matches _GIVING_UP_PATTERNS and
# is suppressed anyway. The mechanism is real; the remedy is not, because the
# shape is not. MEASURED over 26,630 turn-final assistant messages from this
# install's CC transcripts (the honest base rate: these predate the nudges
# reaching anyone, so nothing in the corpus was shaped by them) — the full
# cross-product of the three matchers:
#
#   giving-up only .............................  31   0.12%   fires
#   unverified-completion only .................. 430   1.61%   fires
#   unverified-completion + yielding ............ 154   0.58%   suppressed
#   giving-up + yielding ........................  44   0.17%   suppressed  <- disputed cell
#   giving-up + unverified-completion ............  1   0.00%   fires
#   neither ................................. 25,970  97.52%   silent
#
# The disputed cell is 44 occurrences but only 12 UNIQUE messages (CC forks a
# transcript per resume, so one message recurs across files). Reading all 12:
# at least 6 are plainly legitimate yields where a giving-up phrase sits far
# back in the reply and the question is about something else entirely — "…do it
# yourself…" 925 and 1,911 characters before "Does this framing make sense
# before I start implementing?" and "What's your read on this direction?". None
# of the 12 has the reviewed shape: a terminal delegation with a question
# appended. The two matchers also read different windows (giving-up scans a
# 2,000-char tail, this one 400), so overlap mostly means "unrelated sentences
# in one long reply", not "delegation dressed as a question".
#
# Inverting precedence would therefore convert ~6 correct silences into blocks
# that talk over a user who was just asked something, to catch a shape with no
# instances — the fail direction above, run backwards. Falsifiable: if that
# shape shows up at a real rate, or a discriminator separates it from a long
# reply that merely contains both, let the giving-up match take precedence.
_AWAITING_USER = re.compile(
    r"(?:\?|\bshall I\b|\bwould you like\b|\bdo you want\b|\blet me know\b"
    r"|\bawaiting your\b|\bplease (?:approve|confirm|advise|decide|provide)\b)"
    r"[^.!?]{0,120}[?.!]?\s*$",
    re.IGNORECASE,
)


def _is_awaiting_user(assistant_message: str) -> bool:
    """Is this turn handing control back to the user?"""
    if not assistant_message:
        return False
    return bool(_AWAITING_USER.search(assistant_message.rstrip()[-400:]))


def _emit(notes: list[str | None]) -> None:
    """Deliver the turn's nudges as ONE Stop `additionalContext` payload.

    Silence when nothing fired: this channel CONTINUES the turn, so an empty
    advisory would not merely be noise, it would cost a model turn.

    Routed through ``print_json_bounded`` — its first production caller — as
    chokepoint discipline rather than because this payload is near the cap. It
    is not: MEASURED worst case is both nudges at 722 characters of text, 804
    as the serialised payload `print_json_bounded` actually measures, against a
    9,800 budget. The reason is that a hook writing model-facing
    stdout outside the module that owns the cap constant is one CC version bump
    from silent loss, and the envelope is what carries the decision. (An earlier
    draft of this comment claimed the text was "unbounded by construction". It
    is not — every message is a fixed literal.)

    A ``False`` return means the payload went out oversize anyway. Noted to
    stderr, not raised: a partially-delivered nudge beats the silence this
    replaces, and an advisory must never cost the turn.
    """
    messages = [n for n in notes if n]
    if not messages:
        return
    ok = print_json_bounded(
        {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": "\n\n".join(messages),
            }
        },
        text_keys=("hookSpecificOutput.additionalContext",),
    )
    if not ok:
        # print_json_bounded already wrote a stderr warning naming the size, the
        # budget and the reason; this only attributes it to a hook.
        print("genesis_stop_hook: ^ that oversize advisory was this hook", file=sys.stderr)


def main() -> None:
    if not _FLAG.exists():
        return

    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    # Parse hook input from stdin
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        hook_input = {}

    session_id = hook_input.get("session_id", "")
    if not session_id:
        return

    # Read last user message from session-scoped buffer. The id is a PATH
    # COMPONENT only here, so an unsafe id skips this read — it must NOT skip
    # the giving-up-pattern check below, which is session-independent (it needs
    # only the assistant message from the hook payload).
    last_user_msg = ""
    messages_file = session_path(
        _GENESIS_DIR / "sessions", session_id, "messages.jsonl"
    )
    if messages_file is not None and messages_file.exists():
        try:
            lines = messages_file.read_text().strip().splitlines()
            if lines:
                last = json.loads(lines[-1])
                last_user_msg = last.get("text", "")
        except (json.JSONDecodeError, OSError):
            pass

    # Check for giving-up patterns in the assistant's response.
    # This runs regardless of whether user messages exist — it only
    # needs the assistant's last message from hook input.
    assistant_msg = hook_input.get("last_assistant_message", "")
    # `stop_hook_active` is true when the loop is ALREADY continuing because a
    # Stop hook spoke last time. Emitting again from that state is what runs to
    # CLAUDE_CODE_STOP_HOOK_BLOCK_CAP and ends in a user-visible override
    # warning — so a nudge is emitted at most once per continuation chain,
    # which is exactly what the harness's own warning text prescribes.
    #
    # The flag is set by ANY Stop hook blocking, so a sibling blocking first
    # suppresses this one for the rest of that continuation chain. That matters
    # in one real case: `hooks/deliverable_gate_guard.py` is a GATE and holds
    # until its marker clears, so a deliverable session in `rendered_unverified`
    # keeps these nudges quiet throughout. Correct as a priority — a hard gate
    # outranks an advisory — and the cost is a lost advisory, never a loop.
    if not hook_input.get("stop_hook_active") and not _is_awaiting_user(assistant_msg):
        _emit([
            _check_giving_up(assistant_msg),
            _check_outcome_verification(assistant_msg),
        ])

    if not last_user_msg:
        return

    # Check for resume signal
    match = _RESUME_PATTERNS.search(last_user_msg)
    if match:
        _GENESIS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            signal_data = {
                "session_id": session_id,
                "signal": match.group(0),
                "timestamp": datetime.now(UTC).isoformat(),
            }
            _RESUME_SIGNAL_FILE.write_text(json.dumps(signal_data))
        except OSError:
            pass


# Patterns that suggest the assistant is giving up / delegating work back
# to the user.  Tight scope to avoid false positives on legitimate
# delegation ("you'll need to approve the PR" is proper, not giving up).
_GIVING_UP_PATTERNS = re.compile(
    r"(?:"
    r"you(?:'ll| will) need to (?:do |handle |run |transfer |copy |move )"
    r"|(?:do it|handle it|run it|transfer it|copy it) (?:yourself|manually)"
    r"|(?:you|the user) (?:can |should |could )(?:do |handle |run |transfer |copy )"
    r"(?:it |this |that )?(?:yourself|manually|on your)"
    r"|I (?:can't|cannot|am unable to|don't have) (?:access to|permission to|credentials for|keys for)"
    r"|(?:you'll|you will) have to (?:do |handle |run |transfer |copy )"
    r"|not (?:something I can|within my (?:ability|access|scope))"
    r"|outside (?:my|Genesis'?) (?:scope|ability|access)"
    r")",
    re.IGNORECASE,
)


# Patterns indicating the assistant is at the "finishing" stage —
# offering to merge, create PRs, or declaring implementation complete.
_FINISHING_PATTERNS = re.compile(
    r"(?:"
    r"merge\s+(?:back\s+)?to\s+main"
    r"|create\s+a\s+pull\s+request"
    r"|push\s+and\s+create"
    # `complete[sd]?\b` and not a bare `complete`: without the boundary,
    # "implementation COMPLETELY" matches. MEASURED — 2 of the 6 wrong fires in
    # the corpus below were that exact bleed, both of them quotations of the
    # genesis-development rule "Read the reference implementation COMPLETELY
    # before deriving from it". `complete[sd]?` keeps "completed"/"completes",
    # which are real finishing claims; a bare `complete\b` would have lost them.
    r"|implementation\s+complete[sd]?\b"
    r"|what\s+would\s+you\s+like\s+to\s+do\?"
    r"|keep\s+the\s+branch\s+as-is"
    r"|discard\s+this\s+work"
    r"|ready\s+to\s+(?:ship|merge|land|deploy)\b"
    r"|all\s+(?:changes|work)\s+(?:are\s+)?(?:done|complete[sd]?\b)"
    r")",
    re.IGNORECASE,
)

# This repo's own review protocol closes every review with
# `Ready to merge: Yes | No | With fixes` (genesis-development skill, "Close
# every review with a verdict"). When the assistant QUOTES a reviewer's verdict,
# the two negative values are a report that the work is NOT finished — the exact
# inverse of what `_FINISHING_PATTERNS` is looking for.
#
# A CLOSED set of two literals from a protocol this repo defines, deliberately —
# not a general model of negation. That general model was measured and is worse
# than nothing here: over the corpus, a negation cue in the 60 characters BEFORE
# a finishing match selects 8 messages, and reading all 8 by hand, every one is a
# TRUE fire ("I won't merge to main without your go-ahead" is a finishing-stage
# turn, not a status-unfinished one) and none is a wrong fire. Suppressing on
# negation would therefore have blinded the matcher 8 times to buy nothing. The
# polarity that actually discriminates sits AFTER the match, in a fixed vocabulary.
#
# The separator is written out rather than assumed: these messages are Markdown,
# so the verdict routinely arrives bolded or dash-joined. MEASURED across this
# install's transcripts, of 710 negative-verdict occurrences the plain `: No`
# spelling covers 674 and the bold-value and dash forms account for the other 36
# — a 5% hole in a rule whose whole job is polarity. Admitting the dash also
# admits "ready to merge - no problem", which is why `no` carries the lookahead:
# without it the widening would trade one wrong fire for one wrong SILENCE, and
# a wrong silence is the direction that loses the feature.
_NOT_READY_VERDICT = re.compile(
    r"\**\s*[:—–-]\s*\**\s*"
    r"(?:no\b(?!\s+(?:blocker|issue|problem|concern|finding|change))|with\s+fixes\b)",
    re.IGNORECASE,
)


def _finishing_claim(assistant_message: str) -> bool:
    """Is the assistant claiming to be at the finishing stage?

    A finishing phrase carrying a negative review verdict is a quotation of
    someone saying the work is NOT done, so it does not count.
    """
    for m in _FINISHING_PATTERNS.finditer(assistant_message):
        if not _NOT_READY_VERDICT.match(assistant_message, m.end()):
            return True
    return False

# Patterns indicating integration/e2e verification was actually done.
# If any of these appear alongside finishing language, skip the reminder.
_VERIFICATION_EVIDENCE = re.compile(
    r"(?:"
    r"integration\s+test"
    r"|e2e\s+test"
    r"|smoke\s+test"
    r"|api\s+(?:smoke\s+)?test"
    r"|verif(?:y|ied)\s+(?:the\s+)?(?:actual|end-to-end|e2e)"
    r"|manual(?:ly)?\s+(?:test|verif)"
    r"|live\s+(?:test|verif)"
    r"|telegram\s+api\s+(?:smoke|confirmed|test)"
    r"|asyncio\s+integration"
    r"|production\s+(?:test|verif)"
    r"|outcome\s+verif"
    r")",
    re.IGNORECASE,
)

# A mention of verification is not evidence that verification HAPPENED. These
# qualifiers say it is still owed — planned, deferred, blocked, or failed — and
# a sentence carrying one alongside an evidence phrase is the case where the
# reminder is most warranted, not least.
#
# The window is the SENTENCE, not a character count: a qualifier binds the
# evidence phrase it shares a sentence with. MEASURED over the corpus, 9 messages
# reach the evidence suppressor at all and 3 of those suppressions are wrong;
# hand-reading all 9, this closed vocabulary un-suppresses 2 of the 3 and leaves
# every correct suppression standing. The word "never" is deliberately ABSENT:
# it appears in a correctly-suppressed message ("values never printed") in a
# clause that has nothing to do with whether the test ran.
#
# Every branch is spelled to its own word boundary. The trailing `\b` closes the
# WHOLE alternation, so a stem written as a stem is a contradiction: an earlier
# draft carried `before\s+(?:merg|ship|land)\b`, which matched the non-word
# "before merg" and NOT "before merge" or "before merging" — a member of a
# closed, hand-chosen vocabulary that could never fire. Verified by running the
# pattern over the inflection table rather than by reading it.
_NOT_YET_VERIFIED = re.compile(
    r"\b(?:next\s+session|needs?|needed|deferred|pending|todo|to-do|not\s+yet"
    r"|planned|could\s?n[o']?t|cannot|can'?t|failed|failing|unable\s+to"
    r"|skip(?:s|ped|ping)?|untested|unverified"
    r"|before\s+(?:merg(?:e|es|ed|ing)|ship(?:s|ped|ping)?|land(?:s|ed|ing)?))\b",
    re.IGNORECASE,
)

# Sentence bounds, for scoping the qualifier above. A newline counts: these
# messages are Markdown, where a list item is a sentence. So does `;`, which
# joins two clauses that can be about different things ("the first attempt
# failed; after the fix the integration test passes" is evidence, and treating
# the whole line as one sentence would have thrown it away). `:` is deliberately
# NOT a break — it is the joint in "Phase 2 next session: E2E testing", one of
# the two shapes this repair exists for.
#
# An abbreviation or a decimal ("e.g.", "3.5") splits a sentence that should not
# have been split, which only SHRINKS the window a qualifier is looked for in —
# so that error can lose a repair, never invent one, and the fail direction stays
# the status quo.
_SENTENCE_BREAK = ".!?;\n"


def _sentence_around(text: str, start: int, end: int) -> tuple[int, int]:
    """Bounds of the sentence containing text[start:end]."""
    left = max(text.rfind(c, 0, start) for c in _SENTENCE_BREAK) + 1
    rights = [i for i in (text.find(c, end) for c in _SENTENCE_BREAK) if i != -1]
    return left, (min(rights) if rights else len(text))


def _has_verification_evidence(assistant_message: str) -> bool:
    """Did the message claim verification was actually DONE?

    A phrase naming verification counts as evidence only when its own sentence
    does not also say the verification is still owed.

    The window is the WHOLE sentence, on both sides of the phrase. An earlier
    draft scanned only the text before it and claimed in its docstring to scan
    the sentence; English puts these qualifiers after the noun phrase at least
    as often as before it, so "the integration test failed" — the exact case the
    `failed` qualifier was added for — still bought silence.
    """
    for m in _VERIFICATION_EVIDENCE.finditer(assistant_message):
        left, right = _sentence_around(assistant_message, m.start(), m.end())
        if not _NOT_YET_VERIFIED.search(assistant_message, left, right):
            return True
    return False


def _check_outcome_verification(assistant_message: str) -> str | None:
    """Remind to verify actual outcomes before presenting completion options."""
    if not assistant_message:
        return None
    if not _finishing_claim(assistant_message):
        return None
    if _has_verification_evidence(assistant_message):
        return None
    return (
        "OUTCOME VERIFICATION REMINDER: You're presenting completion options "
        "but haven't mentioned integration or e2e verification beyond unit tests. "
        "Before the user decides: verify before completing (the `superpowers` plugin's "
        "verification-before-completion skill, where the install has it) — "
        "run the actual verification command, read the full output, THEN claim status. "
        "Evidence before assertions."
    )


def _check_giving_up(assistant_message: str) -> str | None:
    """Nudge if the assistant appears to delegate a user-assigned task."""
    if not assistant_message:
        return None
    # Giving-up phrases appear at the end of responses. Truncate to avoid
    # running complex regex over 50KB+ assistant messages.
    tail = assistant_message[-2000:] if len(assistant_message) > 2000 else assistant_message
    if not _GIVING_UP_PATTERNS.search(tail):
        return None
    return (
        "SELF-CHECK: Your last response may be delegating work back to the user. "
        "Before giving up, verify: (1) Did you check reference_lookup and "
        "reference_network_topology.md? (2) Did you read proactive memory "
        "injections? (3) Did you try all available tools and credentials? "
        "Genesis way: exhaust all options before escalating to the user."
    )




if __name__ == "__main__":
    main()
