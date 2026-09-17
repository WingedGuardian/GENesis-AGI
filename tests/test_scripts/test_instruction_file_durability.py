"""The always-loaded instruction files carry DURABLE information only.

OWNER RULE (2026-09-13): no issue or PR numbers in `CLAUDE.md`. It is loaded into
every session and is supposed to hold what stays true; a ticket number is a claim
with a shelf life, and the paragraph around it is usually a STATUS — "not built
yet", "shipped in #X" — which goes stale the day the ticket closes and fails
nothing when it does.

WHY THIS IS A TEST AND NOT A CONVENTION. Two references had accumulated and
neither was noticed until the owner read one aloud: `(issue #1718)` attached to a
"NOT BUILT YET" status, and `the day #1556 moved it behind the writer` attached to
a measurement. Both were written in good faith by sessions recording real work.
A rule nobody checks is one a future session re-breaks while being helpful — which
is the same shape as the stale cross-file comments this repo keeps finding, and
the reason the fix is a lock rather than a note.

WHAT TO DO INSTEAD, when you want to write one:
  * a STATUS ("that half is not built") — say what is true regardless, or leave it
    out; the gate's own behaviour is the durable version of "nothing enforces it";
  * PROVENANCE for a measurement — name the change ("the day it was moved behind
    the writer"), not its number;
  * work that genuinely needs tracking — a GitHub issue or a follow-up row, which
    is where a ticket number belongs and where it stays current.

SCOPE, stated so this is not read as wider than it is: only the repo-root
instruction files below. `.claude/skills/**`, `docs/**` and code comments are
deliberately NOT covered — a skill citing the PR that produced a measurement is
provenance in a document a reader opens on purpose, not a claim injected into
every session.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

#: Repo-root instruction files loaded into every session. `SOUL.md` / `USER.md`
#: are install-local and untracked, so they cannot be checked from here; they are
#: listed so the omission reads as known rather than forgotten.
_ALWAYS_LOADED = ("CLAUDE.md", "AGENTS.md")

#: Three spellings of the same thing, because only matching the first one let a
#: real instance survive the gate that was written to catch it.
#:
#:   `#1718`        the bare GitHub shape, 3-5 digits. Two would collide with
#:                  ordinary prose ("#2 below"), and this repo is past #1000.
#:   `PR-3`         a NAMED reference. The digit bound is 1-5, not 3-5: this form
#:                  is most often a plan-series label ("session-manager PR-3"),
#:                  which is if anything LESS durable than a GitHub number — the
#:                  series it indexes exists only in a plan file nobody reading
#:                  CLAUDE.md can resolve.
#:   `issue 1718`   the same reference spelled out, with the `#` dropped.
#:
#: MEASURED against `CLAUDE.md` + `AGENTS.md` at the time of writing: 1 hit
#: (`CLAUDE.md:386`, `session-manager PR-3`, fixed in this change) and 0 false
#: positives. `gh pr checks <PR-number>` in the commands block does NOT match —
#: the placeholder carries no digits.
_TICKET_RE = re.compile(
    r"#\d{3,5}\b|\bPR[-‑ ]\d{1,5}\b|\b(?:issues?|PRs?|pull requests?)\s+\d{1,5}\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("name", _ALWAYS_LOADED)
def test_no_ticket_numbers_in_always_loaded_instructions(name: str) -> None:
    path = _ROOT / name
    if not path.exists():  # pragma: no cover - AGENTS.md is present today
        pytest.skip(f"{name} not present in this checkout")
    hits = [
        f"{name}:{i}: {line.strip()[:110]}"
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if _TICKET_RE.search(line)
    ]
    assert not hits, (
        f"{name} is loaded into every session and must carry only what stays "
        "true. A ticket number dates the sentence around it — usually a status "
        "that goes stale the day the ticket closes, failing nothing. State the "
        "durable fact, or move the tracking to an issue / follow-up row.\n" + "\n".join(hits)
    )


def test_the_detector_actually_matches_a_ticket_number() -> None:
    """Guard the guard: a regex that matches nothing would pass forever.

    The real historical strings are used, so this fails if the pattern is ever
    narrowed past the shape it exists to catch.
    """
    assert _TICKET_RE.search("is NOT BUILT YET (issue #1718).")
    assert _TICKET_RE.search("the day #1556 moved it behind the writer")
    # The NAMED form, which the first version of this pattern missed entirely —
    # it shipped green while `CLAUDE.md:386` still read "(session-manager PR-3)".
    # A reviewer found that, not this test, which is the reason the case is here.
    assert _TICKET_RE.search("ambient extraction (session-manager PR-3) is the net")
    assert _TICKET_RE.search("superseded by PR 1941")
    assert _TICKET_RE.search("tracked in issue 1718")
    # …and does not fire on ordinary prose, or the rule would be unfollowable.
    assert not _TICKET_RE.search("see item #2 below")
    assert not _TICKET_RE.search("a 32-hex row id")
    assert not _TICKET_RE.search("gh pr checks <PR-number>")
    assert not _TICKET_RE.search("open a PR and request a review")
