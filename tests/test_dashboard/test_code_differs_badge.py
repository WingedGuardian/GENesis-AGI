"""The `code_differs` badge, pinned in BOTH templates that render it.

Two things went wrong here and each has a test below.

1. THE BADGE IS DUPLICATED. The same two spans exist in the sessions TAB and in
   the sessions MODAL — four copies of one piece of markup, kept in sync by
   nothing. A review of this badge named only the modal, so a fix applied there
   would have left the tab telling users the opposite thing. The durable check
   is not "the wording is X" but "the two files AGREE": whatever the badge says,
   it says it in both places.

2. THE BADGE ASSERTED A DIRECTION IT HAD NOT ESTABLISHED. `code_differs` is a sha
   INEQUALITY between a process's spawn commit and the checkout's HEAD. That is
   overwhelmingly "the checkout moved ahead" — but after a reset, or a checkout
   of an older ref, the running process is on the NEWER code and the badge still
   fires. "stale · running older code" is a claim the comparison cannot support;
   "differs" is what it actually knows.

The badge text is also the full 40-char sha, which is 40 characters of
monospace in a nowrap span sitting in a row of session chips. Every other sha
in this UI is abbreviated.

These parse the templates as text rather than rendering them: the expressions
are Alpine attribute strings, evaluated in the browser, so there is no Python
render path to assert against — and the failure being guarded is an edit landing
in one file and not its twin, which is a text property.
"""

import re
from pathlib import Path

_TEMPLATES = (
    Path(__file__).resolve().parents[2] / "src" / "genesis" / "dashboard" / "templates" / "partials"
)

TAB = _TEMPLATES / "tabs" / "sessions.html"
MODAL = _TEMPLATES / "modals" / "cc_sessions.html"

# The badge span carries exactly one `x-text` and one `:title`. Both are matched
# on their own, so a file that renames one attribute fails the count guard below
# rather than silently matching zero.
_XTEXT = re.compile(r"""x-text="'(?:[^']|\\')*restart for(?:[^"]|\\")*\"""")
_TITLE = re.compile(r""":title="'This (?:session|process)(?:[^"]|\\")*\"""")

# Two badges per file (the session row, and the per-process row inside it). If
# this drops, the scan has gone blind and every assertion below passes vacuously.
_EXPECTED_PER_FILE = 2


def _badges(path: Path) -> tuple[list[str], list[str]]:
    text = path.read_text(encoding="utf-8")
    return _XTEXT.findall(text), _TITLE.findall(text)


def _normalise(expr: str) -> str:
    """Drop the Alpine scope prefix so the tab's `s.live.x` and the modal's
    identical span compare equal on the part that is actually duplicated."""
    return re.sub(r"\s+", " ", expr).strip()


def test_the_scan_sees_both_badges_in_both_files():
    """The guard on every other test in this file: an attribute rename would
    otherwise turn all of them green by matching nothing."""
    for path in (TAB, MODAL):
        xtexts, titles = _badges(path)
        assert len(xtexts) == _EXPECTED_PER_FILE, f"{path.name}: x-text badges"
        assert len(titles) == _EXPECTED_PER_FILE, f"{path.name}: title badges"


def test_the_tab_and_the_modal_say_the_same_thing():
    """THE duplication check. A review that names one file must not be able to
    leave the other contradicting it — whatever the badge says, both say it."""
    tab_x, tab_t = _badges(TAB)
    modal_x, modal_t = _badges(MODAL)
    assert [_normalise(e) for e in tab_x] == [_normalise(e) for e in modal_x], (
        "the badge LABEL diverged between the sessions tab and the sessions modal"
    )
    assert [_normalise(e) for e in tab_t] == [_normalise(e) for e in modal_t], (
        "the badge TOOLTIP diverged between the sessions tab and the sessions modal"
    )


def test_the_badge_does_not_claim_a_direction_the_comparison_cannot_see():
    """`code_differs` is a sha inequality. It is true when the checkout moved
    ahead AND when it moved back, and the badge may not pick one."""
    for path in (TAB, MODAL):
        for expr in [*_badges(path)[0], *_badges(path)[1]]:
            low = expr.lower()
            assert "older code" not in low, f"{path.name}: asserts a direction"
            assert "'stale ·" not in low, f"{path.name}: labels the state stale"
            assert "differs" in low, f"{path.name}: does not name what it knows"


def test_the_badge_abbreviates_the_sha():
    """40 monospace characters in a nowrap chip, beside abbreviated shas
    everywhere else in this UI."""
    for path in (TAB, MODAL):
        for expr in _badges(path)[0]:
            assert ".slice(0, 8)" in expr, f"{path.name}: badge shows a full sha"


def test_the_tooltip_still_carries_the_full_sha():
    """CONTROL on the abbreviation, and it is load-bearing: truncating BOTH
    would pass the test above while removing the only place a user can read the
    commit they are being told to restart for. The label is abbreviated because
    it is a chip; the tooltip is prose and keeps the whole value."""
    for path in (TAB, MODAL):
        for expr in _badges(path)[1]:
            assert "head_commit || '?'" in expr, f"{path.name}: tooltip lost the sha"
            assert ".slice(" not in expr, f"{path.name}: tooltip truncated too"
