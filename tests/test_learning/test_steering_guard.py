"""Tests for the STEERING.md directive-shape guard (_looks_like_directive).

Regression harness for the 2026-06-30 incident: a benign, mis-classified
Telegram status update was written verbatim into STEERING.md as a "hard
constraint". The guard is fail-CLOSED — only terse imperative directives pass.
"""

from __future__ import annotations

import pytest

from genesis.learning.pipeline import _looks_like_directive

# The actual message that triggered the incident (383 chars, multi-sentence,
# opens with "Yeah", contains "never" only inside "its never too late").
_INCIDENT_MESSAGE = (
    "Yeah sorry for the delays on all that. Autonomize is dead, forget about "
    "it. The agent challenge went well, no need to do anything there except "
    "continue to iterate on our process for the next time we need to build "
    "professional artifacts. I didn't attend the conference. And no arxiv "
    "didn't go in, but we should continue doing anything else remaining on the "
    "paper--its never too late"
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
        _INCIDENT_MESSAGE,  # the exact incident message
        "I was wrong about that approach",  # no imperative opener; 'wrong' dropped
        "yeah that went well, thanks",  # chatty acknowledgement
        "the arxiv paper didn't go in",  # status update, not a directive
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
