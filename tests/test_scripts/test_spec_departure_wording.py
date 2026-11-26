"""The departure rules keep the words that carry them, and the reviewer keeps the
exact text a pre-registered test passed with (spec-is-the-contract, PR 3)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / ".claude/skills/build-session/SKILL.md"
ARCHITECT = REPO / ".claude/agents/genesis-architect.md"
DEEP_REVIEW = REPO / ".claude/commands/deep-review.md"
CLOSING = REPO / ".claude/skills/closing-session/SKILL.md"

#: The reviewer text test 2b passed with (4 of 4 cases), line for line. A change
#: here, or in genesis-architect.md, discards that evidence: retest first.
TESTED_TEXT = (
    "The diff may cover only part of the plan: a part it does not touch is not a contradiction.",
    "Report a contradiction when the code does something different from a decision the plan made, and no approved revision",
    "entry in the plan covers it. Adding a mechanism, value or behaviour in place of, or beyond, what a plan decision fixed",
    "counts. A contradiction must be reported even when you judge it justified, since accepting it is the owner's decision.",
    "Naming, internal structure and test structure are not contradictions.",
    "Write each line directly above the verdict line.",
    "## Grade each contradiction",
    "Put each contradiction in one class:",
    "- `material`: it brings a new trade-off, a new effect a user or the machine can see, behaviour the plan never covered, or a premise the plan got wrong. When unsure, it is material.",
    "- `detail`: it changes how a plan decision is carried out, with no consequence for a user or the machine beyond what the plan already accepted.",
    'Write a material one as `Contradicts: <plan item> "<first words>" — <what the code does instead>; consequence: <one line>`.',
    'Write a detail as `Differs: <plan item> "<first words>" — <what the code does instead>`.',
    "The verdict is `CONTRADICTS` when at least one line is material, otherwise `CLEAN`.",
)


def test_build_session_keeps_the_departure_words():
    text = " ".join(BUILD.read_text(encoding="utf-8").split())
    for word in (
        "`implementation-defect`",
        "`design-decision`",
        "`plan-change`",
        "`contract-discovery`",
        "`intent-change`",
        "`Owner answer: pending`",
        "A builder never infers the owner's approval from silence, a passing test, the code as built, or an approval given for something else.",
        "An entry still reading `Owner answer: pending` approves nothing.",
        "an entry whose answer keeps the change covers its line",
        "never as an `AM-n`, since the spec does not change",
        "`git merge-base HEAD origin/<parent branch>` when it is stacked",
        "never writes a `Confirmed:` line",
        "`git diff --binary <source> > <patch>`",
        "After the parent merges, merge main in first (a pushed branch is never rebased), then use",
    ):
        assert word in text, word
    assert "rebase onto main" not in text


def test_reviewer_carries_the_tested_text_line_for_line():
    """The fence holds exactly the tested lines, in order: nothing added, moved or reworded."""
    text = ARCHITECT.read_text(encoding="utf-8")
    fences = re.findall(r"\n   ```text\n(.*?)\n   ```\n", text, re.S)
    assert len(fences) == 1, "the reviewer must carry exactly one tested fence"
    got = [ln.strip() for ln in fences[0].split("\n") if ln.strip()]
    assert got == list(TESTED_TEXT)


def test_reviewer_ends_on_the_verdict_line():
    text = ARCHITECT.read_text(encoding="utf-8")
    assert "`Spec check: <VERDICT> <spec file name>`" in text
    flat = " ".join(text.split())
    assert "the `Spec check:` line is always last" in flat
    assert (
        "With no spec named it is `Spec check: NOT GIVEN`, so every review ends on this line."
        in flat
    )
    assert (
        "`Owner answer:` keeps the change; an answer to build the change back covers nothing"
        in flat
    )
    assert "for a later one, the `New:` of the earlier `AM-n` it names" in flat
    assert "(`Old: none (new item)` has no base and is not matched)" in flat


def test_deep_review_puts_the_verdict_last():
    assert "then the `Spec check:` line last" in DEEP_REVIEW.read_text(encoding="utf-8")


def test_closing_session_confirms_amendments():
    text = " ".join(CLOSING.read_text(encoding="utf-8").split())
    assert "whatever its basis is" in text
    assert "`Confirmed: <date>" in text
    assert "with no `Confirmed:` line. Ask the owner yourself" in text
