"""One CodeRabbit reader for the merge gate, the floor fact and the round counter (#4491).

The merge gate splits a CodeRabbit comment into findings at a markdown rule
(``cr_findings``, which masks fenced code and ``<details>``) and reads each
segment's first header. ``review_findings.is_blocking`` used to read every line
as a possible header, so a Major quoted in a code block, a ``<details>``
section or an indented block, or one with no rule before it, read as at the
floor where the gate read Minor. A fresh-context audit reproduced 9 such
inputs. What this pins:

1. ``is_blocking`` gives the gate's floor reading on every one of them;
2. ``is_finding`` never reads a comment as informational when the gate reads a
   segment above Trivial/Info, and still counts a Trivial-then-Major bundle
   with no rule between them (a round counter must never undercount);
3. a source footer line in either number opens no round unless one starts a
   segment, where the gate reads it as unknown too (#4460);
4. the round counter reads every header line, quoted ones included, so a markup
   shape the gate's mask gets wrong (#1722) cannot hide a header from it;
5. the gate reads through the moved functions, not a copy.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))

import review_findings as rf  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "git_push_guard_one_reader", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

RABBIT = "coderabbitai[bot]"
MAJ = "_🎯 Functional Correctness_ | _🟠 Major_ | _⚡ Quick win_"
MIN = "_⚠️ Potential issue_ | _🟡 Minor_"
TRIV = "_🧹 Nitpick_ | _🔵 Trivial_"
UNK = "_⚠️ Potential issue_ | _🟣 Severe_"

#: The class audit's reproduced inputs: a Minor finding whose body holds a
#: Major-shaped header somewhere the gate does not read as a new finding.
AUDIT_INPUTS = {
    "fenced": f"{MIN}\n\n**t**\n\n```md\n{MAJ}\n```\n",
    "four-backtick suggestion": f"{MIN}\n\n**t**\n\n````suggestion\n```md\n{MAJ}\n```\n````\n",
    "details": f"{MIN}\n\n**t**\n\n<details>\n<summary>x</summary>\n\n{MAJ}\n\n</details>\n",
    "indented": f"{MIN}\n\n**t**\n\n    {MAJ}\n",
    "header in prose": f"{MIN}\n\n**t**\n\nThe other reviewer wrote:\n\n{MAJ}\n",
    "no rule between": f"{MIN}\n\n**t**\n\n{MAJ}\n\n**u**",
    "rule inside a fence": f"{MIN}\n\n**t**\n\n```md\n---\n{MAJ}\n```\n",
    "unclosed fence": f"{MIN}\n\n**t**\n\n```\n{MAJ}\n",
    "fenced unreadable": f"{MIN}\n\n**t**\n\n```\n{UNK}\n```\n",
    "unreadable first, quoted Major": f"{UNK}\n\n**t**\n\n```\n{MAJ}\n```\n",
}

#: Further shapes: footers, rules, markers and empty bodies.
MORE_INPUTS = {
    "plural footer": f"{TRIV}\n\n**Rename.**\n\n_Sources: Coding guidelines, Learnings_",
    "singular footer": f"{TRIV}\n\n**Rename.**\n\n_Source: Linters/SAST tools_",
    "rule then footer": f"{TRIV}\n\n**Rename.**\n\n---\n\n_Sources: Coding guidelines_",
    "Trivial then Minor, no rule": f"{TRIV}\n\n**a**\n\n{MIN}\n\n**b**",
    "Trivial, quoted Minor": f"{TRIV}\n\n**t**\n\n```\n{MIN}\n```",
    "Trivial, Minor in details": f"{TRIV}\n\n**t**\n\n<details>\n<summary>x</summary>\n\n{MIN}\n\n</details>\n",
    "Trivial, unclosed fence, Minor": f"{TRIV}\n\n**a**\n\n```\n{MIN}\n",
    "Trivial, rule, prose": f"{TRIV}\n\n**a**\n\n---\n\nMore prose",
    "marker then Trivial": f"<!-- coderabbit-ai-generated:v1 -->\n{TRIV}\n\n**a**",
    "Trivial, rule, Major": f"{TRIV}\n\n**a**\n\n---\n\n{MAJ}\n\n**b**",
    "headerless": "**Just a title**\n\nsome prose",
    "empty": "",
}

#: Shapes the gate's fence and ``<details>`` mask reads differently from
#: CommonMark (#1722). Each hides the rule and the Major after a Trivial from
#: the gate, which reads ``['trivial']``; the round counter must still count it.
_TAIL = "\n\n```python\npass\n```\n"
FALSE_FENCE_INPUTS = {
    "inline span as a line": f"{TRIV}\n\n**a**\n\n```example```\n\n---\n\n{MAJ}{_TAIL}",
    "backtick in info string": f"{TRIV}\n\n**a**\n\nuse:\n```x`y\n\n---\n\n{MAJ}{_TAIL}",
    "inline span with prose": f"{TRIV}\n\n**a**\n\n``` `name` ``` is wrong\n\n---\n\n{MAJ}{_TAIL}",
    "tab-indented backticks": f"{TRIV}\n\n**a**\n\n\t```\n\n---\n\n{MAJ}{_TAIL}",
    "fence in a bullet item": f"{TRIV}\n\n**a**\n\n- ```python\n  x = 1\n  ```\n\n---\n\n{MAJ}{_TAIL}",
    "fence in an ordered item": f"{TRIV}\n\n**a**\n\n1. ```python\n   x = 1\n   ```\n\n---\n\n{MAJ}{_TAIL}",
    "one-line details": (
        f"{TRIV}\n\n<details><summary>a</summary>b</details>\n\n---\n\n{MAJ}"
        "\n\n<details>\n<summary>c</summary>\n\nd\n\n</details>\n</details>\n"
    ),
    "inline span, no rule": f"{TRIV}\n\n```example```\n\n{MAJ}{_TAIL}",
}

ALL_INPUTS = {**AUDIT_INPUTS, **MORE_INPUTS, **FALSE_FENCE_INPUTS}


def _gate_levels(body: str) -> list[str | None]:
    return [guard._cr_severity(seg)[0] for seg in guard._cr_findings(body)]


def _gate_floor(body: str) -> bool | None:
    """The gate's reading: a Critical or Major segment blocks; a segment it
    cannot read is unknown; anything else is below the floor."""
    levels = _gate_levels(body)
    if any(level in guard._CR_BLOCKING_SEVERITIES for level in levels):
        return True
    return None if any(level is None for level in levels) else False


@pytest.mark.parametrize("name", list(ALL_INPUTS))
def test_the_floor_fact_reads_what_the_gate_reads(name):
    body = ALL_INPUTS[name]
    assert rf.is_blocking(RABBIT, body) is _gate_floor(body)


@pytest.mark.parametrize(
    "name",
    [
        "fenced",
        "four-backtick suggestion",
        "details",
        "indented",
        "rule inside a fence",
        "unclosed fence",
        # These two are the gate's own reading, which misses a Major written
        # after a Minor with no rule between them. They flip to True with #4550.
        "header in prose",
        "no rule between",
    ],
)
def test_a_quoted_or_unsegmented_major_is_not_at_the_floor(name):
    assert rf.is_blocking(RABBIT, AUDIT_INPUTS[name]) is False


@pytest.mark.parametrize("name", list(ALL_INPUTS))
def test_the_round_counter_never_reads_less_than_the_gate(name):
    body = ALL_INPUTS[name]
    if any(level not in rf.CR_INFORMATIONAL for level in _gate_levels(body)):
        assert rf.is_finding(RABBIT, body) is True


@pytest.mark.parametrize(
    "name, want",
    [
        # A finding the gate does not split out still counts as a round.
        ("Trivial then Minor, no rule", True),
        ("Trivial, unclosed fence, Minor", True),
        ("Trivial, rule, Major", True),
        ("headerless", True),
        ("empty", True),
        # A footer that opens its own segment is unreadable there, as in the gate.
        ("rule then footer", True),
        # A header quoted in fenced or collapsed content opens a round too: the
        # counter does not trust the gate's mask (#4569), so it may overcount.
        ("Trivial, quoted Minor", True),
        ("Trivial, Minor in details", True),
        # Footers and markers open no round.
        ("plural footer", False),
        ("singular footer", False),
        ("Trivial, rule, prose", False),
        ("marker then Trivial", False),
    ],
)
def test_what_opens_a_round(name, want):
    assert rf.is_finding(RABBIT, ALL_INPUTS[name]) is want


@pytest.mark.parametrize("name", list(FALSE_FENCE_INPUTS))
def test_a_false_fence_hides_the_major_from_the_gate_not_from_the_counter(name):
    body = FALSE_FENCE_INPUTS[name]
    assert _gate_levels(body) == ["trivial"]
    assert rf.is_finding(RABBIT, body) is True


def _every_line_reading(body: str) -> bool:
    """Main's round-counter reading before #4491, minus the ruled plural-footer
    exception (#4460): every header line counts unless it is informational or a
    source footer in either number; a comment with no header counts."""
    headers = 0
    for line in body.split("\n"):
        if not line.strip():
            continue
        level, seen = rf.cr_severity(line)
        if not seen or rf._CR_SOURCE_FOOTER_RE.fullmatch(line.strip()):
            continue
        headers += 1
        if level not in rf.CR_INFORMATIONAL:
            return True
    return headers == 0


def _generated_bodies(n: int = 600) -> list[str]:
    import random

    rng = random.Random(4569)
    pool = [
        TRIV,
        MIN,
        MAJ,
        UNK,
        "**t**",
        "prose",
        "",
        "---",
        "```",
        "```md",
        "~~~",
        "```example```",
        "\t```",
        "- ```python",
        "  ```",
        "<details>",
        "<summary>x</summary>",
        "</details>",
        "<details><summary>a</summary>b</details>",
        "_Source: Linters/SAST tools_",
        "_Sources: Coding guidelines_",
    ]
    # Each body opens with a Trivial header, so its first segment reads
    # informational and the verdict rests on the header-line scan.
    return [
        TRIV + "\n" + "\n".join(rng.choice(pool) for _ in range(rng.randint(1, 12)))
        for _ in range(n)
    ]


def test_the_round_counter_counts_whatever_the_every_line_scan_counts():
    """A round counter must never undercount: whatever main's every-line scan
    counted, this reader counts too, on fixed and generated bodies alike."""
    bodies = list(ALL_INPUTS.values()) + _generated_bodies()
    missed = [b for b in bodies if _every_line_reading(b) and not rf.is_finding(RABBIT, b)]
    assert missed == []


def test_a_plural_source_footer_opens_no_round():
    """#4460: the singular pattern alone read `_Sources: …_` as an unreadable
    header, so a Trivial-only comment opened a round."""
    assert rf.is_finding(RABBIT, MORE_INPUTS["plural footer"]) is False


def test_the_gate_reads_through_the_shared_functions():
    assert guard._REVIEW_FINDINGS_ERROR is None
    assert guard._cr_findings is rf.cr_findings
    assert guard._cr_markup_mask is rf.cr_markup_mask
    assert guard._cr_masks is rf.cr_masks
    assert rf.cr_levels(AUDIT_INPUTS["fenced"]) == _gate_levels(AUDIT_INPUTS["fenced"])
