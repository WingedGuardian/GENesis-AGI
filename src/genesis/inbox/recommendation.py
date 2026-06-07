"""Parse Recommendation YAML blocks from inbox evaluation output.

Inbox evaluations produce `.genesis.md` files with structured
``### Recommendation`` YAML blocks.  This module extracts those blocks
so downstream systems (follow-up creation, digest) can consume them
programmatically.

Two vocabularies:
- Genesis-relevant: ADOPT | ADAPT | WATCH | IGNORE
- User-relevant:    adopt | explore | bookmark | potential_skip
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import yaml

logger = logging.getLogger(__name__)

# Actions that should NOT produce follow-ups.
_SKIP_ACTIONS: frozenset[str] = frozenset({
    "ignore",
    "potential_skip",
    "potential skip",
})

# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Recommendation:
    """A single parsed Recommendation from an inbox evaluation."""

    action: str
    next_step: str
    effort: str = "Small"
    confidence: str = "medium"
    # Genesis-relevant fields
    scope: str | None = None
    architecture_impact: str | None = None
    # User-relevant fields
    timeline: str | None = None
    relevance: str | None = None
    # Context
    item_title: str = ""
    classification: str = ""  # "genesis" or "user"

    @property
    def is_actionable(self) -> bool:
        """True when this recommendation should produce a follow-up."""
        return self.action.lower().replace("_", " ") not in _SKIP_ACTIONS


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

# Match ## N. Title  or  ## Title  headings (item sections in multi-item evals)
_SECTION_RE = re.compile(r"^## ", re.MULTILINE)

# Match ### Recommendation heading
_REC_HEADING_RE = re.compile(
    r"^### Recommendation\s*$", re.MULTILINE,
)

# Match ```yaml ... ``` fenced code block (tolerant of whitespace)
_YAML_BLOCK_RE = re.compile(
    r"```\s*yaml\s*\n(.*?)```", re.DOTALL,
)

def parse_recommendations(evaluation_text: str) -> list[Recommendation]:
    """Extract all Recommendation YAML blocks from an evaluation response.

    Handles multi-item evaluations where each ``## N. Title`` section has
    its own ``### Recommendation`` block with a fenced YAML code block.

    Returns an empty list if no valid recommendations are found.  Malformed
    blocks are logged and skipped — never raises.
    """
    if not evaluation_text:
        return []

    results: list[Recommendation] = []

    # Split evaluation text into per-item sections on ## headings.
    # The first chunk (before the first ##) is preamble — skip it.
    sections = _SECTION_RE.split(evaluation_text)

    for section in sections:
        # Look for ### Recommendation in this section
        rec_match = _REC_HEADING_RE.search(section)
        if not rec_match:
            continue

        # Find the YAML fenced block after the heading
        after_heading = section[rec_match.end():]
        yaml_match = _YAML_BLOCK_RE.search(after_heading)
        if not yaml_match:
            logger.debug(
                "### Recommendation heading found but no ```yaml block follows",
            )
            continue

        yaml_text = yaml_match.group(1)
        try:
            data = yaml.safe_load(yaml_text)
        except yaml.YAMLError as exc:
            logger.warning("Malformed YAML in Recommendation block: %s", exc)
            continue

        if not isinstance(data, dict):
            logger.warning(
                "Recommendation YAML is not a mapping: %s", type(data).__name__,
            )
            continue

        # Extract action — required field
        action = data.get("action")
        if not action:
            logger.warning("Recommendation block missing 'action' field")
            continue

        action = str(action).strip()
        next_step = str(data.get("next_step", "")).strip()

        # Detect classification by field presence
        has_scope = "scope" in data
        has_timeline = "timeline" in data
        if has_scope:
            classification = "genesis"
        elif has_timeline:
            classification = "user"
        else:
            # Fallback: uppercase action = genesis, lowercase = user
            classification = "genesis" if action == action.upper() else "user"

        # Extract item title from the ## heading
        title = _extract_item_title(section)

        rec = Recommendation(
            action=action,
            next_step=next_step,
            effort=str(data.get("effort", "Small")).strip(),
            confidence=str(data.get("confidence", "medium")).strip(),
            scope=str(data["scope"]).strip() if has_scope else None,
            architecture_impact=(
                str(data["architecture_impact"]).strip()
                if "architecture_impact" in data else None
            ),
            timeline=(
                str(data["timeline"]).strip()
                if has_timeline else None
            ),
            relevance=(
                str(data["relevance"]).strip()
                if "relevance" in data else None
            ),
            item_title=title,
            classification=classification,
        )
        results.append(rec)

    return results


def _extract_item_title(section_text: str) -> str:
    """Extract the item title from the first line of a ## section.

    The section_text is everything AFTER the ``## `` prefix (since we split
    on ``^## ``).  So the title is the first line.
    """
    first_line = section_text.split("\n", 1)[0].strip()
    # Strip leading number + dot (e.g., "1. Evo — Autoresearch...")
    numbered = re.match(r"^\d+\.\s*(.+)$", first_line)
    if numbered:
        return numbered.group(1).strip()
    return first_line
