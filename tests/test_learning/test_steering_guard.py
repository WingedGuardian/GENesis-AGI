"""Tests for the STEERING.md directive-shape guard (_looks_like_directive).

Regression harness for the 2026-06-30 incident: a benign, mis-classified
Telegram status update was written verbatim into STEERING.md as a "hard
constraint". The guard is fail-CLOSED — only terse imperative directives pass.
"""

from __future__ import annotations

import pytest

from genesis.learning.pipeline import _looks_like_directive

# A SYNTHETIC stand-in with the incident's structural shape (multi-sentence,
# chatty, opens with "Yeah", contains "never" only inside "its never too late").
# The real triggering DM is private user data and is NOT committed to this
# public repo — the guard only cares about structure, not content.
_INCIDENT_MESSAGE = (
    "Yeah sorry for the delays on all that. The first thing fell through, "
    "forget about it. The second went well, no action needed there. I skipped "
    "the third entirely. And the write-up didn't ship, but we should keep "
    "going on whatever's left--its never too late"
)


@pytest.mark.parametrize(
    "text",
    [
        "stop sending emails without approval",
        "don't use the Anthropic API",
        "never merge to main without a PR",
        "you must not send outreach without approval from me",
        "always verify before claiming confidence",
        "please stop dispatching background sessions at night",
        "Stop sending outreach. It keeps failing.",  # 2 sentences, opens imperative
    ],
)
def test_genuine_directives_pass(text: str) -> None:
    assert _looks_like_directive(text) is True


@pytest.mark.parametrize(
    "text",
    [
        _INCIDENT_MESSAGE,  # incident-shaped synthetic status message
        "I was wrong about that approach",  # no imperative opener; 'wrong' dropped
        "yeah that went well, thanks",  # chatty acknowledgement
        "the write-up didn't ship out",  # status update, not a directive
        # More chatty, positive status updates — non-imperative openers, so the
        # guard must reject them (context to note, never a hard directive).
        "Hey, good news on my end — the review went really well and they want to "
        "keep going. No action needed from you, just looping you in.",
        "Quick update: got some strong signal this week and a couple of doors "
        "opened up. Feeling good about where things are headed.",
        "Yeah that landed better than I expected, appreciate the patience. Let's "
        "build on it when there's time.",
        "Great milestone today — the Vessridge pilot cleared its first review and "
        "the team is thrilled. Nothing needed from you, just sharing the win.",
        "",  # empty
        "   ",  # whitespace only
        # opens with a directive word but rambles past 2 sentences → rejected
        # (Layer 1 dominates even a valid imperative opener)
        "Never do that. Here is a second sentence. And a third that rambles on.",
        # single imperative sentence but far too long (Layer 2 word cap)
        "stop " + " ".join(f"word{i}" for i in range(40)),
    ],
)
def test_non_directives_rejected(text: str) -> None:
    assert _looks_like_directive(text) is False


def test_incident_double_blocked() -> None:
    """The incident message fails on BOTH sentence-count and imperative-start."""
    # >2 sentences
    assert _looks_like_directive(_INCIDENT_MESSAGE) is False
    # Even truncated to its first clause, it does not open with a directive verb.
    assert _looks_like_directive("Yeah sorry for the delays on all that") is False


def test_none_safe() -> None:
    assert _looks_like_directive(None) is False  # type: ignore[arg-type]
