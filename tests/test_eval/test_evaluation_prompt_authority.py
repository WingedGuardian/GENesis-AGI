"""Authority contracts for the two evaluation prompt surfaces.

Behavioral quality belongs to the paired replay suites.  These tests only pin
which file owns each framework so the foreground commands cannot drift into a
second, contradictory implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_COMMAND_SPECS = {
    "evaluate": """---
name: evaluate
description: >
  Evaluate technologies, tools, articles, videos, and competitive developments
  against Genesis architecture.
---

# Evaluate

Read `src/genesis/skills/evaluate/SKILL.md` completely and apply it as the
canonical evaluation framework. Do not reconstruct the framework from this
wrapper or from memory.

Evaluate the following target:

$ARGUMENTS
""",
    "user-evaluate": """---
name: user-evaluate
description: >
  Evaluate content for personal relevance using Genesis's accumulated user
  model while keeping evidence, inference, and unknowns distinct.
---

# User Evaluate

Read `src/genesis/skills/user_evaluate/SKILL.md` completely and apply it as the
canonical user-evaluation framework. Do not reconstruct the framework from this
wrapper or from memory.

Evaluate the following target:

$ARGUMENTS
""",
}
_DELEGATED_SKILL_PATHS = (
    "src/genesis/skills/evaluate/SKILL.md",
    "src/genesis/skills/user_evaluate/SKILL.md",
)
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


@pytest.mark.parametrize(
    "command",
    _COMMAND_SPECS,
)
def test_foreground_commands_are_exact_thin_delegates(command: str) -> None:
    text = (_ROOT / ".claude" / "commands" / f"{command}.md").read_text(
        encoding="utf-8"
    )

    assert text == _COMMAND_SPECS[command]


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


@pytest.mark.parametrize("skill_path", _DELEGATED_SKILL_PATHS)
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
