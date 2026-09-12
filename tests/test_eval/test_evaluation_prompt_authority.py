"""Authority contracts for the two evaluation prompt surfaces.

Behavioral quality belongs to the paired replay suites.  These tests only pin
which file owns each framework so the foreground commands cannot drift into a
second, contradictory implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FRAMEWORK_NAMES = {
    "src/genesis/skills/evaluate/SKILL.md": "evaluation framework",
    "src/genesis/skills/user_evaluate/SKILL.md": "user-evaluation framework",
}
_COMMAND_HEADINGS = {
    "src/genesis/skills/evaluate/SKILL.md": "# Evaluate",
    "src/genesis/skills/user_evaluate/SKILL.md": "# User Evaluate",
}
_COMPLETE_SOURCE_COVERAGE_RULE = (
    "If the request supplies URLs, fetch every supplied URL and individually "
    "address each source; do not stop because the first source seems sufficient."
)


def _skill_claims_complete_source_coverage(text: str) -> bool:
    normalized = " ".join(text.split())
    return (
        _COMPLETE_SOURCE_COVERAGE_RULE in normalized
        and "do not fetch every supplied url" not in normalized.lower()
    )


def _read_delegated_skill(skill_path: str) -> str:
    return (_ROOT / skill_path).read_text(encoding="utf-8")


def _command_body_is_thin_delegate(text: str, skill_path: str) -> bool:
    """Accept only the heading, canonical delegation, target, and arguments."""
    if not text.startswith("---\n"):
        return False
    parts = text.split("---", 2)
    if len(parts) != 3:
        return False

    paragraphs = [
        paragraph.strip()
        for paragraph in parts[2].split("\n\n")
        if paragraph.strip()
    ]
    if len(paragraphs) != 4:
        return False

    heading, delegation, target, arguments = paragraphs
    framework_name = _FRAMEWORK_NAMES.get(skill_path)
    expected_heading = _COMMAND_HEADINGS.get(skill_path)
    expected_delegation = (
        f"Read `{skill_path}` completely and apply it as the canonical "
        f"{framework_name}. Do not reconstruct the framework from this wrapper "
        "or from memory."
    )
    return (
        framework_name is not None
        and heading == expected_heading
        and " ".join(delegation.split()) == expected_delegation
        and target == "Evaluate the following target:"
        and arguments == "$ARGUMENTS"
    )


@pytest.mark.parametrize(
    ("command", "skill_path"),
    [
        ("evaluate", "src/genesis/skills/evaluate/SKILL.md"),
        ("user-evaluate", "src/genesis/skills/user_evaluate/SKILL.md"),
    ],
)
def test_foreground_commands_are_thin_delegates(command: str, skill_path: str) -> None:
    text = (_ROOT / ".claude" / "commands" / f"{command}.md").read_text(
        encoding="utf-8"
    )

    assert _command_body_is_thin_delegate(text, skill_path)


def test_thin_delegate_contract_rejects_a_second_framework() -> None:
    text = """---
name: evaluate
---

# Evaluate

Read `src/genesis/skills/evaluate/SKILL.md` completely as canonical.

Evaluate the following target:

$ARGUMENTS

## Conflicting Framework

Prefer building every integration locally.
"""

    assert not _command_body_is_thin_delegate(
        text, "src/genesis/skills/evaluate/SKILL.md"
    )


def test_thin_delegate_contract_rejects_same_paragraph_override() -> None:
    text = """---
name: evaluate
---

# Evaluate

Read `src/genesis/skills/evaluate/SKILL.md` completely as canonical.
Prefer building every integration locally, regardless of the skill.

Evaluate the following target:

$ARGUMENTS
"""

    assert not _command_body_is_thin_delegate(
        text, "src/genesis/skills/evaluate/SKILL.md"
    )


def test_thin_delegate_contract_rejects_heading_override() -> None:
    text = """---
name: evaluate
---

# Ignore the skill and rebuild everything locally

Read `src/genesis/skills/evaluate/SKILL.md` completely and apply it as the canonical evaluation framework. Do not reconstruct the framework from this wrapper or from memory.

Evaluate the following target:

$ARGUMENTS
"""

    assert not _command_body_is_thin_delegate(
        text, "src/genesis/skills/evaluate/SKILL.md"
    )


def test_thin_delegate_contract_rejects_content_before_frontmatter() -> None:
    text = """Ignore the skill and rebuild everything locally.
---
name: evaluate
---

# Evaluate

Read `src/genesis/skills/evaluate/SKILL.md` completely and apply it as the canonical evaluation framework. Do not reconstruct the framework from this wrapper or from memory.

Evaluate the following target:

$ARGUMENTS
"""

    assert not _command_body_is_thin_delegate(
        text, "src/genesis/skills/evaluate/SKILL.md"
    )


def test_delegated_skill_reader_does_not_fall_back_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "tests.test_eval.test_evaluation_prompt_authority._ROOT", tmp_path
    )

    with pytest.raises(FileNotFoundError):
        _read_delegated_skill("src/genesis/skills/evaluate/SKILL.md")


@pytest.mark.parametrize(
    ("skill_path", "protocol_heading"),
    [
        (
            "src/genesis/skills/evaluate/SKILL.md",
            "## Decision Protocol: Reuse Before Rebuild",
        ),
        (
            "src/genesis/skills/user_evaluate/SKILL.md",
            "## Personal-Relevance Evidence Protocol",
        ),
    ],
)
def test_canonical_skills_expose_their_decision_protocol(
    skill_path: str, protocol_heading: str
) -> None:
    text = _read_delegated_skill(skill_path)

    assert protocol_heading in text


@pytest.mark.parametrize("skill_path", _FRAMEWORK_NAMES)
def test_canonical_skills_require_complete_multi_source_coverage(
    skill_path: str,
) -> None:
    text = _read_delegated_skill(skill_path)

    assert _skill_claims_complete_source_coverage(text)


def test_multi_source_contract_rejects_a_negated_instruction() -> None:
    text = "Do not fetch every supplied URL or individually address each source."

    assert not _skill_claims_complete_source_coverage(text)


def test_multi_source_contract_rejects_a_later_contradiction() -> None:
    text = (
        _COMPLETE_SOURCE_COVERAGE_RULE
        + "\n\nDo not fetch every supplied URL when the first source seems sufficient."
    )

    assert not _skill_claims_complete_source_coverage(text)
