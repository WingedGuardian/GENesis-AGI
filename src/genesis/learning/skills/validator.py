"""Skill validator — structural validation of skill proposals before application."""

from __future__ import annotations

import re
from difflib import unified_diff

from genesis.learning.skills.types import SkillProposal, ValidationResult
from genesis.learning.skills.wiring import get_skill_path, is_genesis_core_skill

# Vague terms that indicate untestable instructions in workflow steps.
_VAGUE_TERMS = re.compile(
    r"\b(appropriately|as needed|nicely|properly|handle correctly|"
    r"good enough|reasonable|suitable|adequate)\b",
    re.IGNORECASE,
)

# Size thresholds (aligned with refiner's 500-line restructure suggestion).
_SIZE_WARN = 150
_SIZE_MAX = 300
_SIZE_MIN = 20


class SkillValidator:
    """Validates proposed SKILL.md content before auto-application.

    Runs structural checks only — no LLM calls, no I/O. Receives all
    content as parameters for testability.
    """

    def validate(
        self,
        proposal: SkillProposal,
        current_content: str | None = None,
    ) -> ValidationResult:
        """Run all validation tests against proposed content.

        Args:
            proposal: The skill proposal to validate.
            current_content: Current SKILL.md content (for consistency check).
                If None, consistency test is skipped.
        """
        failures: list[str] = []
        warnings: list[str] = []
        results: dict[str, str] = {}

        content = proposal.proposed_content

        # Test 1: Structure
        results["structure"] = self._test_structure(content, proposal.skill_name, failures)

        # Test 2: Trigger coverage
        results["trigger_coverage"] = self._test_triggers(content, failures)

        # Test 3: Testability
        results["testability"] = self._test_vague_terms(content, failures)

        # Test 4: Size
        results["size"] = self._test_size(content, failures, warnings)

        # Test 5: Examples audit
        results["examples"] = self._test_examples(content, warnings)

        # Test 6: Consistency (only if current content and change_size available)
        if current_content is not None:
            results["consistency"] = self._test_consistency(
                proposal, current_content, failures, warnings
            )

        return ValidationResult(
            passed=len(failures) == 0,
            test_results=results,
            blocking_failures=failures,
            warnings=warnings,
        )

    def _test_structure(
        self, content: str, skill_name: str, failures: list[str]
    ) -> str:
        """Test 1: YAML header with required fields, workflow section exists."""
        # Check YAML frontmatter
        if not content.startswith("---"):
            failures.append("structure: missing YAML frontmatter")
            return "FAIL: no YAML header"

        yaml_end = content.find("---", 3)
        if yaml_end == -1:
            failures.append("structure: unclosed YAML frontmatter")
            return "FAIL: unclosed YAML header"

        yaml_block = content[3:yaml_end]

        if "name:" not in yaml_block:
            failures.append("structure: YAML missing 'name' field")
            return "FAIL: no name field"

        if "description:" not in yaml_block:
            failures.append("structure: YAML missing 'description' field")
            return "FAIL: no description field"

        # Check for consumer field if this is a Genesis core skill
        path = get_skill_path(skill_name)
        if path and is_genesis_core_skill(path) and "consumer:" not in yaml_block:
            failures.append("structure: Genesis core skill missing 'consumer' field")
            return "FAIL: core skill missing consumer"

        # Check workflow section exists
        if not re.search(r"^##\s+Workflow", content, re.MULTILINE):
            failures.append("structure: missing '## Workflow' section")
            return "FAIL: no Workflow section"

        # Check output format section exists
        if not re.search(r"^##\s+Output\s+Format", content, re.MULTILINE):
            failures.append("structure: missing '## Output Format' section")
            return "FAIL: no Output Format section"

        return "PASS"

    def _test_triggers(self, content: str, failures: list[str]) -> str:
        """Test 2: Trigger coverage — description or When to Use has content."""
        # Extract YAML description
        yaml_end = content.find("---", 3)
        yaml_block = content[3:yaml_end] if yaml_end > 3 else ""

        desc_match = re.search(r"description:\s*>?\s*\n?(.*?)(?=\n\w|\n---)", yaml_block, re.DOTALL)
        has_description = bool(desc_match and len(desc_match.group(1).strip()) > 20)

        # Check for "When to Use" section
        has_when_to_use = bool(re.search(r"^##\s+When to Use", content, re.MULTILINE))

        if not has_description and not has_when_to_use:
            failures.append("trigger_coverage: no trigger phrases in description or When to Use section")
            return "FAIL: no trigger coverage"

        # Check for negative boundary somewhere in the file
        has_negative = bool(re.search(
            r"(do\s+not\s+use|don't\s+use|not\s+for|never\s+activate|should\s+not\s+trigger)",
            content,
            re.IGNORECASE,
        ))

        if not has_negative:
            # Negative boundary missing is a warning, not a hard failure
            # (some skills legitimately apply broadly)
            return "PASS (no negative boundary found — consider adding)"

        return "PASS"

    def _test_vague_terms(self, content: str, failures: list[str]) -> str:
        """Test 3: Scan workflow for untestable vague language."""
        # Extract workflow section
        workflow_match = re.search(
            r"^##\s+Workflow\s*\n(.*?)(?=^##\s|\Z)",
            content,
            re.MULTILINE | re.DOTALL,
        )
        if not workflow_match:
            return "SKIP: no workflow section found"

        workflow_text = workflow_match.group(1)
        matches = _VAGUE_TERMS.findall(workflow_text)

        if len(matches) > 2:  # noqa: PLR2004
            failures.append(
                f"testability: {len(matches)} vague terms in workflow: {matches[:5]}"
            )
            return f"FAIL: {len(matches)} vague terms ({', '.join(matches[:3])}...)"

        if matches:
            return f"PASS ({len(matches)} minor vague term(s): {', '.join(matches)})"

        return "PASS"

    def _test_size(
        self, content: str, failures: list[str], warnings: list[str]
    ) -> str:
        """Test 4: Line count within reasonable bounds."""
        lines = content.count("\n") + 1

        if lines < _SIZE_MIN:
            failures.append(f"size: too short ({lines} lines, minimum {_SIZE_MIN})")
            return f"FAIL: {lines} lines (minimum {_SIZE_MIN})"

        if lines > _SIZE_MAX:
            failures.append(f"size: too long ({lines} lines, maximum {_SIZE_MAX})")
            return f"FAIL: {lines} lines (maximum {_SIZE_MAX})"

        if lines > _SIZE_WARN:
            warnings.append(f"size: {lines} lines — consider splitting into SKILL.md + references/")
            return f"WARN: {lines} lines (consider splitting)"

        return f"PASS ({lines} lines)"

    def _test_examples(self, content: str, warnings: list[str]) -> str:
        """Test 5: Examples section present or explicitly waived."""
        has_examples = bool(re.search(r"^##\s+Examples?\b", content, re.MULTILINE))
        has_waiver = bool(re.search(
            r"^##\s+Examples?:\s+Not\s+Required", content, re.MULTILINE
        ))

        if has_examples or has_waiver:
            return "PASS"

        warnings.append(
            "examples: no Examples section and no explicit waiver. "
            "Consider adding examples or '## Examples: Not Required' with rationale."
        )
        return "WARN: no examples section"

    def _test_consistency(
        self,
        proposal: SkillProposal,
        current_content: str,
        failures: list[str],
        warnings: list[str],
    ) -> str:
        """Test 6: Change size claim vs actual diff magnitude."""
        from genesis.learning.skills.types import ChangeSize

        if proposal.change_size != ChangeSize.MINOR:
            return "SKIP: only checks MINOR claims"

        current_lines = current_content.splitlines()
        proposed_lines = proposal.proposed_content.splitlines()

        diff = list(unified_diff(current_lines, proposed_lines, lineterm=""))
        # Count actual changed lines (not headers)
        changed = sum(1 for line in diff if line.startswith("+") or line.startswith("-"))
        changed -= 4  # Subtract the 2 +++ and 2 --- header lines
        changed = max(0, changed)

        total = max(len(current_lines), 1)
        change_pct = changed / total

        if change_pct > 0.3:  # noqa: PLR2004
            warnings.append(
                f"consistency: MINOR claim but {change_pct:.0%} of lines changed "
                f"({changed}/{total}). Consider classifying as MODERATE."
            )
            return f"WARN: {change_pct:.0%} change for MINOR (threshold 30%)"

        return f"PASS ({change_pct:.0%} change)"
