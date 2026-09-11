"""The Stop hook's advice must reach the model — and must not trap the turn.

Two facts, both READ from the CC 2.1.246 bundle, and the second is the one that
is easy to miss:

1. Only SessionStart, UserPromptSubmit and UserPromptExpansion put a hook's
   bare stdout in front of the model. On Stop it goes to the debug log, so the
   nudges this hook printed for months reached nobody.
2. Stop's JSON channel is not an inbox. The handler pushes `additionalContexts`
   into the array returned as `blockingErrors`, and the agent loop reads that
   as "the hook refused to let this turn end" — it CONTINUES the turn. The
   harness caps consecutive blocks (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP, default 8)
   and then prints a user-visible override warning naming the guard:
   check `stop_hook_active` and stay quiet while it is true.

So these tests pin three things, not one: that advice leaves through the JSON
door, that it says what it always said, and that it CANNOT loop.
"""

from __future__ import annotations

import json

import pytest

from tests.test_scripts.test_session_id_path_guard import _SCRIPTS, _run_hook

# A real-shaped session id (the hook uses it as a path component and skips the
# scoped read on an unsafe one; these tests are about the payload-only checks).
_SID = "a1b2c3d4-0000-4000-8000-000000000000"

_GIVING_UP = "You'll need to run the migration yourself."
# VERIFIED against the real patterns rather than assumed: this triggers
# _FINISHING_PATTERNS and matches no _VERIFICATION_EVIDENCE term. The first
# draft ("All done — the implementation is complete") matched NEITHER, so the
# multi-nudge test would have passed while exercising one nudge.
_UNVERIFIED_DONE = "Implementation complete — ready to merge."


def _emitted(payload: dict, home) -> dict | None:
    """The hook's JSON envelope, or None if it emitted no parseable JSON.

    Runs from *home*, a directory with no repository in it, so nothing this
    hook does can depend on the checkout the suite happens to run in. That
    mattered when the hook still shelled out to git: with the cwd inherited,
    the silence test passed alone and failed in the suite, purely because the
    branch had staged changes by then. It no longer shells out at all — the
    pinning is kept because a test asserting on OUTPUT should not inherit an
    ambient working directory in the first place.
    """
    proc = _run_hook("genesis_stop_hook.py", payload, home, cwd=home)
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def test_a_triggered_nudge_leaves_through_the_json_channel(tmp_path):
    """The core of it: advice must be JSON `additionalContext`, not bare stdout.

    Fails against the previous implementation, which printed prose — stdout
    that parses as nothing and that the harness never shows the model.
    """
    envelope = _emitted(
        {"session_id": _SID, "last_assistant_message": _GIVING_UP}, tmp_path
    )
    assert envelope is not None, "the hook emitted no parseable JSON — advice is inert"
    hso = envelope.get("hookSpecificOutput")
    assert isinstance(hso, dict), f"no hookSpecificOutput in {envelope!r}"
    assert hso.get("hookEventName") == "Stop"
    assert isinstance(hso.get("additionalContext"), str)
    assert hso["additionalContext"].strip(), "an empty additionalContext delivers nothing"


def test_the_advice_still_says_what_it_used_to_say(tmp_path):
    """Changing the CHANNEL must not change the content.

    Pinned because the conversion rewrites every emission site at once, and a
    nudge that survives the move but loses its subject is the quiet way this
    kind of refactor fails.
    """
    envelope = _emitted(
        {"session_id": _SID, "last_assistant_message": _GIVING_UP}, tmp_path
    )
    assert envelope is not None
    text = envelope["hookSpecificOutput"]["additionalContext"]
    assert "SELF-CHECK" in text
    assert "delegating work back to the user" in text


def test_several_triggered_nudges_arrive_as_ONE_envelope(tmp_path):
    """Two nudges, one JSON object — not two, and not one that dropped the other.

    A hook's stdout must be a single JSON document: emitting one object per
    nudge yields concatenated JSON that parses as nothing, which is the same
    inert outcome by a different route. So the payload is assembled once and
    printed once, and both messages have to be inside it.
    """
    both = f"{_GIVING_UP} {_UNVERIFIED_DONE}"
    envelope = _emitted({"session_id": _SID, "last_assistant_message": both}, tmp_path)
    assert envelope is not None, "concatenated JSON objects parse as nothing"
    text = envelope["hookSpecificOutput"]["additionalContext"]
    assert "SELF-CHECK" in text, "the giving-up nudge was dropped"
    assert "OUTCOME VERIFICATION REMINDER" in text, "the verification nudge was dropped"


def test_silence_when_nothing_fires(tmp_path):
    """No nudge, NO OUTPUT — not an envelope carrying an empty string.

    Asserted on raw stdout rather than on the parsed payload. The first version
    of this test accepted an empty `additionalContext`, and a mutation that
    emitted one on every quiet turn survived it: the channel's whole value is
    that it is rare, so an empty advisory every turn is the failure, not a
    tidy edge case.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": "Here is the summary you asked for."},
        tmp_path,
        cwd=tmp_path,
    )
    assert proc.stdout.strip() == "", f"quiet turn still emitted: {proc.stdout!r}"


def test_it_stays_QUIET_while_the_turn_is_already_continuing(tmp_path):
    """The bound. Without it, this hook loops until the harness overrides it.

    Stop's `additionalContext` is returned to the agent loop as
    `blockingErrors`, which continues the turn rather than ending it. Speaking
    again from that state blocks again; the harness allows
    CLAUDE_CODE_STOP_HOOK_BLOCK_CAP (default 8) consecutive blocks and then
    prints a user-visible "a hook blocked the turn from ending 9 consecutive
    times" warning — which is both token burn and a hook surfacing to the user,
    against the standing axiom that hooks are for the agent.

    `stop_hook_active` is true exactly when the loop is already continuing
    because a Stop hook spoke. Staying quiet then is the harness's own
    prescribed guard, and it bounds a nudge to ONE extra turn.
    """
    payload = {"session_id": _SID, "last_assistant_message": _GIVING_UP}

    first = _emitted(payload, tmp_path)
    assert first is not None, "the nudge must fire on the first stop"

    proc = _run_hook(
        "genesis_stop_hook.py",
        {**payload, "stop_hook_active": True},
        tmp_path,
        cwd=tmp_path,
    )
    assert proc.stdout.strip() == "", (
        f"re-emitted while the turn was already continuing: {proc.stdout!r}"
    )


def test_the_unreviewed_code_reminder_is_NOT_emitted_from_stop(tmp_path):
    """It lives on UserPromptSubmit, and belongs there.

    That check is state-based rather than message-based — it re-fires until a
    review marker exists — so on a channel that CONTINUES the turn it would run
    straight to the block cap during ordinary development. It is not lost by
    being absent here: `scripts/review_enforcement_prompt.py` runs the same
    `has_code_changes()` / `is_review_current()` predicate on UserPromptSubmit,
    whose stdout the model does receive.

    The precondition is CONSTRUCTED and ASSERTED, not borrowed from whatever
    checkout the suite runs in. MEASURED against the pre-change hook: with a
    dirty index it emits the reminder (so this assertion bites), and with a
    clean one it emits nothing (so the same assertion would pass vacuously).
    CI checks out clean, so an ambient-repo version of this test would have
    passed there for the wrong reason — asserting the absence of a trigger
    rather than the absence of a behaviour.
    """
    import subprocess
    import sys as _sys

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "a.py"], check=True)

    # Guard the guard: prove the trigger condition actually holds. The hook
    # subprocess runs under a sandboxed HOME, so no review marker can exist and
    # is_review_current() is False whenever the index is dirty.
    _sys.path.insert(0, str(_SCRIPTS))
    from review_state import has_code_changes

    assert has_code_changes(cwd=str(repo)), "precondition absent — the assertion below is vacuous"

    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": "Here is the summary you asked for."},
        tmp_path,
        cwd=repo,
    )
    assert "CODE REVIEW PENDING" not in proc.stdout, (
        "the review reminder is emitted from Stop, where it can loop"
    )


@pytest.mark.parametrize(
    "message",
    [
        "I have finished the analysis. What would you like to do?",
        "I don't have credentials for the deploy target. Can you add them to secrets.env?",
        "Ready to merge. Shall I open the PR?",
        "Implementation complete. Would you like me to open a PR?",
        "All work is done — let me know how you'd like to proceed.",
        "Ready to ship. Please confirm before I push.",
    ],
)
def test_a_turn_that_YIELDS_to_the_user_is_never_blocked(tmp_path, message):
    """A turn ending in a question is already stopping correctly.

    Both nudges mean "don't stop yet", and this channel enforces that by
    refusing to end the turn — so firing on a turn that is ASKING the user
    something makes the model talk past the person it is waiting on.

    MEASURED before this guard existed: 3 of these 6 fired a nudge, including
    "Ready to merge. Shall I open the PR?" — the repo's own approval moment.
    `_FINISHING_PATTERNS` contains a literal `what would you like to do?`
    alternative, so a yielding turn matched it by construction. That cost
    nothing while the output went nowhere; it costs a model turn now.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert proc.stdout.strip() == "", f"blocked a turn that yields to the user: {message!r}"


@pytest.mark.parametrize(
    "message",
    [
        "You'll need to run the migration yourself.",
        "Implementation complete — ready to merge.",
        "You'll need to handle the transfer manually.",
        "I cannot access the deploy host, so you should run it yourself.",
    ],
)
def test_the_suppressor_does_not_blind_the_real_cases(tmp_path, message):
    """The other half of the measurement.

    A filter scored only on the false positives it removes cannot be told apart
    from one that removes everything. These are the shapes the nudges exist for
    — a silent hand-back, a completion claim with no evidence — and they must
    still fire.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert proc.stdout.strip(), f"suppressed a real nudge: {message!r}"


@pytest.mark.parametrize(
    ("message", "should_fire"),
    [
        # A reviewer's verdict, quoted. This repo's own review protocol closes
        # with `Ready to merge: Yes | No | With fixes`, so the two negative
        # values are a report that the work is NOT finished.
        ("Codex marked it Ready to merge: No, so I fixed the four findings.", False),
        ("The architect returned Ready to merge: With fixes on the dedup work.", False),
        # Markdown spellings of the same verdict. MEASURED: of 710 negative
        # verdicts in this install's transcripts, 36 arrive bolded or
        # dash-joined — a 5% hole in a rule whose only job is polarity.
        ("The architect returned Ready to merge: **No** on the dedup work.", False),
        ("Codex said **Ready to merge:** No, so I fixed the findings.", False),
        ("Codex said Ready to merge — No, so I fixed the findings.", False),
        # The affirmative verdict is a finishing claim like any other, and the
        # message around it carries no verification evidence.
        ("The architect returned Ready to merge: Yes — all closed at class level.", True),
        # Admitting the dash admits "ready to merge - no problem", where "no"
        # opens a reason rather than being the verdict. A wrong SILENCE is the
        # direction that loses the feature, so this must still fire.
        ("Ready to merge — no blockers remain on the branch.", True),
    ],
)
def test_a_NOT_ready_verdict_is_not_a_completion_claim(tmp_path, message, should_fire):
    """Polarity, from a CLOSED set of two literals — not a model of negation.

    MEASURED over 12,653 unique turn-final assistant messages from this
    install's transcripts: 3 of the 6 wrong outcome-verification fires were a
    quoted `Ready to merge: No`. The discriminator sits AFTER the phrase, in a
    vocabulary this repo defines, which is why it can be a fixed list.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert bool(proc.stdout.strip()) is should_fire, message


@pytest.mark.parametrize(
    ("message", "should_fire"),
    [
        # The bleed: `implementation complete` used to match the ADVERB. Both of
        # these fired under the old pattern — verified against it directly,
        # because a case that never fired proves nothing about the boundary. (A
        # first draft used "implementation-completely", where the hyphen means
        # `\s+` never matched it either way: vacuous, and it read as coverage.)
        ("The skill already says: Read the reference implementation COMPLETELY.", False),
        ("I read the implementation completely before editing.", False),
        # The inflections that are real finishing claims must survive the fix —
        # a bare `complete\b` would have lost them.
        ("Implementation completed on the branch, 12 files changed.", True),
        ("Implementation complete, 12 files changed.", True),
    ],
)
def test_a_finishing_word_is_matched_WHOLE(tmp_path, message, should_fire):
    """`complete[sd]?\\b`, so COMPLETELY is not a completion claim."""
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert bool(proc.stdout.strip()) is should_fire, message


@pytest.mark.parametrize(
    ("message", "should_fire"),
    [
        # Verification that has NOT happened. Naming it must not buy silence —
        # this is the case the reminder exists for.
        ("Implementation complete. Phase 2 next session: E2E testing.", True),
        ("Ready to merge. Design 4 is deferred: it needs a dedicated session "
         "with live testing.", True),
        ("Ready to merge. I could not run the integration test on this host.", True),
        # The qualifier AFTER the phrase — English puts it there at least as
        # often. A window that scanned only the preceding text let the exact
        # case `failed` was added for buy silence.
        ("Ready to merge. The integration test failed.", True),
        ("Ready to merge. The e2e test is still pending.", True),
        # Each of these carries exactly ONE qualifier, so it locks that entry's
        # inflection. `\b` closes the whole alternation, so a branch written as
        # a stem ("before merg") matches only a non-word and is dead on arrival.
        ("Ready to merge. The e2e test will run before merging.", True),
        ("Ready to merge. Skipping the smoke test until the host is back.", True),
        # Verification that DID happen still buys silence. The reminder that
        # fires on a verified message is worse than the one that misses.
        ("Ready to merge. The fix is verified end-to-end.", False),
        ("Ready to merge. The integration test passes on the live server.", False),
        # Two clauses about different things. `;` is a sentence break precisely
        # so the first clause's qualifier does not bind the second's evidence.
        ("Ready to merge. The first attempt failed; the integration test now passes.", False),
        ("Ready to merge. This needs no follow-up; the smoke test passed.", False),
        # `never` is deliberately not a qualifier: it occurs in a correctly
        # suppressed message ("values never printed") in a clause with nothing
        # to do with whether the test ran.
        ("Ready to merge. Values are never printed and the smoke test passes.", False),
    ],
)
def test_verification_NAMED_is_not_verification_DONE(tmp_path, message, should_fire):
    """MEASURED: 9 messages reach the evidence suppressor; 3 wrongly.

    Two of the three were verification that was planned or deferred, named in
    the same sentence as the qualifier saying so. This closed vocabulary
    repairs those two and leaves all 6 correct suppressions standing.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert bool(proc.stdout.strip()) is should_fire, message


@pytest.mark.parametrize(
    "message",
    [
        "Per the standing rule I won't merge to main without your go-ahead.",
        "The merge is your call — I don't merge to main autonomously.",
        "This is not ready to ship on its own; it rides the other PR.",
    ],
)
def test_a_negated_finishing_phrase_STILL_fires(tmp_path, message):
    """The measurement that decided against a negation filter — pinned.

    A general negation filter is the obvious repair and it is net-harmful here.
    MEASURED: a negation cue in the 60 characters before a finishing match
    selects 8 of the 130 fires, and hand-reading all 8, every one is a TRUE
    fire — a finishing-stage turn holding at the merge gate, not a status
    update saying the work is unfinished. Suppressing on negation would have
    blinded the matcher 8 times and removed none of the 6 wrong fires, whose
    polarity sits after the phrase rather than before it.

    This test exists so the next session that reaches for a negation filter has
    to argue with the measurement instead of rediscovering it.
    """
    proc = _run_hook(
        "genesis_stop_hook.py",
        {"session_id": _SID, "last_assistant_message": message},
        tmp_path,
        cwd=tmp_path,
    )
    assert proc.stdout.strip(), f"a negation filter would have blinded this: {message!r}"
