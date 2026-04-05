"""Memory classification — rule vs fact vs reference.

Pure function, no I/O. Classifies memories to enable priority-aware retrieval:
- **rule**: Actionable instruction ("NEVER do X", "ALWAYS check Y"). Gets
  activation boost — rules prevent repeat mistakes.
- **fact**: Informational ("session X discussed Y", "the system uses Z").
  Neutral weight — the default.
- **reference**: Pointer to external info ("bugs tracked in Linear", "see URL").
  Lower weight — useful but less urgent to surface proactively.
"""

from __future__ import annotations

import re

# ALL-CAPS imperative markers with word-boundary anchors.
# Kept conservative — we default to "fact" on ambiguity, so false negatives
# are cheap (rule gets normal weight) but false positives waste context
# (fact gets undeserved boost).
_RULE_PATTERNS = re.compile(
    r"\b("
    r"ALWAYS|NEVER|MUST NOT|MUST|DO NOT|SHOULD NOT"
    r")\b"
    r"|"
    r"\b(CRITICAL|MANDATORY|REQUIRED):",
)

# Reference indicators — URLs, pointer language.
_REFERENCE_PATTERNS = re.compile(
    r"(https?://\S{20,}|"
    r"\bsee also\b|\btracked in\b|\brefer to\b|\bdocumented at\b|"
    r"\bLinear project\b|\bGrafana\b|\bSlack channel\b)",
    re.IGNORECASE,
)

# Mapping from CC file memory type (user/feedback/project/reference) to class.
_CC_MEMORY_TYPE_MAP: dict[str, str] = {
    "feedback": "rule",
    "user": "rule",
    "project": "fact",
    "reference": "reference",
}

# Activation weight multipliers per class (used in activation.py).
CLASS_WEIGHTS: dict[str, float] = {
    "rule": 1.3,
    "fact": 1.0,
    "reference": 0.7,
}


def classify_memory(
    content: str,
    *,
    source: str = "",
    source_pipeline: str = "",
    cc_memory_type: str = "",
) -> str:
    """Classify a memory as rule, fact, or reference.

    Priority order:
    1. CC memory type (if provided) — deterministic, from file frontmatter.
    2. Content heuristics — pattern matching on the text.
    3. Default — "fact".

    Args:
        content: The memory text.
        source: Memory source (e.g., "session_extraction", "reflection").
        source_pipeline: Pipeline name (e.g., "harvest", "conversation").
        cc_memory_type: CC file memory type if known (user/feedback/project/reference).

    Returns:
        One of "rule", "fact", "reference".
    """
    # 1. CC file memory type takes precedence — it's explicit human classification.
    if cc_memory_type and cc_memory_type in _CC_MEMORY_TYPE_MAP:
        return _CC_MEMORY_TYPE_MAP[cc_memory_type]

    # 2. Content heuristics.
    # Check for rule patterns first (higher value to surface).
    if _RULE_PATTERNS.search(content):
        return "rule"

    # Check for reference patterns.
    if _REFERENCE_PATTERNS.search(content):
        return "reference"

    # 3. Default.
    return "fact"
