"""Unified steering feedback for all enforcement layers.

Every enforcement mechanism (hooks, linters, gates, advisors) emits
SteerMessage instances. This provides a common format for:
  - Hook scripts (exit codes, stderr, JSON)
  - Autonomy gates (decisions, timeouts)
  - V4 ego sessions (learning from enforcement feedback)

See docs/architecture/enforcement-spectrum.md for the 7-layer taxonomy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from genesis.autonomy.types import ApprovalDecision, EnforcementLayer


@dataclass(frozen=True)
class SteerMessage:
    """Unified steering feedback across all enforcement layers."""

    # Identity
    layer: EnforcementLayer
    rule_id: str

    # Decision
    decision: ApprovalDecision  # BLOCK | PROPOSE | ACT
    severity: str  # critical | high | medium | low | info

    # Content
    title: str
    context: str  # What triggered this
    suggestion: str  # How to fix/proceed

    # Optional context
    tool_name: str | None = None
    file_path: str | None = None

    # Suppression
    can_suppress: bool = False
    suppress_key: str = ""  # e.g. "behavioral-lint: ignore no-hide-problems"

    def to_exit_code(self) -> int:
        """Convert decision to CC hook exit code: 0 (allow) or 2 (block)."""
        if self.decision == ApprovalDecision.BLOCK:
            return 2
        return 0

    def to_stderr(self) -> str:
        """Format as human-readable stderr output for hook scripts."""
        label = "BLOCKED" if self.decision == ApprovalDecision.BLOCK else "WARNING"
        lines = [
            f"\n{label}: {self.title}",
            f"  Rule: {self.rule_id}",
        ]
        if self.file_path:
            lines.append(f"  File: {self.file_path}")
        if self.context:
            lines.append(f"  Issue: {self.context}")
        if self.suggestion:
            lines.append(f"  Fix: {self.suggestion}")
        if self.can_suppress and self.suppress_key:
            lines.append(f"  Escape: Add '{self.suppress_key}' if user-approved")
        return "\n".join(lines) + "\n"

    def to_hook_json(self) -> dict:
        """Format as CC PreToolUse hook contract JSON.

        Intended for ADVISORY-layer messages. For BLOCK decisions, use
        :meth:`to_exit_code` and :meth:`to_stderr` instead — CC uses
        exit code 2 for blocking, not the permissionDecision field.

        Returns the dict structure CC expects on stdout:
        {
          "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": "..."
          }
        }
        """
        # Build advisory text
        parts = [self.title]
        if self.context:
            parts.append(self.context)
        if self.suggestion:
            parts.append(self.suggestion)
        advisory = "\n".join(parts)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "additionalContext": advisory,
            }
        }

    def to_hook_json_str(self) -> str:
        """Format as CC hook contract JSON string."""
        return json.dumps(self.to_hook_json())
