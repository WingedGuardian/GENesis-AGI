"""Identity document loader — reads and caches SOUL.md, USER.md, STEERING.md."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).parent
_STEERING_SEPARATOR = "---"
_STEERING_MAX_RULES = 20

# USER.md auto-synthesis — line cap keeps context injection under ~1500 tokens.
_MAX_USER_MD_LINES = 50

# Prefix clusters: fields sharing a prefix are merged into a single line.
_FIELD_CLUSTERS = {
    "risk_tolerance": "Risk Profile",
    "tolerance_for": "Tolerances",
    "preference_for": "Preferences",
    "trust_in": "Trust Dynamics",
    "autonomy": "Autonomy Model",
    "communication": "Communication Style",
    "decision": "Decision Making",
    "system_health": "System Health",
    "operational": "Operational Style",
    "technical": "Technical Approach",
    "recovery_program": "Recovery Approach",
    "prioritization": "Prioritization",
    "risk_appetite": "Risk Appetite",
}

# USER.md auto-synthesis — maps user model fields to profile sections.
# NOTE: write_user_md() is DISABLED — USER.md is user-edited only.
# These maps are retained for reference but not actively used.
_USER_MD_SECTION_ORDER = [
    "Identity", "Goals", "Expertise", "Observed Patterns",
]

_FIELD_SECTION_MAP = {
    "role": "Identity",
    "background": "Identity",
    "identity": "Identity",
    "goals": "Goals",
    "motivations": "Goals",
    "project_vision": "Goals",
    "expertise": "Expertise",
    "domains": "Expertise",
    "learning_areas": "Expertise",
}

# USER_KNOWLEDGE.md synthesis — bounded sections with max item counts.
# "Recent Themes" currently has no mapped fields — it populates when
# interaction_theme observations are picked up by the evolver pipeline
# or a future enhancement reads them directly into the synthesis.
_KNOWLEDGE_SECTION_ORDER = [
    "Interests & Active Curiosity",
    "Active Projects",
    "Expertise Map",
    "Goals & Priorities",
    "Interaction Patterns",
    "Recent Themes",
]

_KNOWLEDGE_SECTION_LIMITS = {
    "Interests & Active Curiosity": 15,
    "Active Projects": 10,
    "Expertise Map": 20,
    "Goals & Priorities": 10,
    "Interaction Patterns": 10,
    "Recent Themes": 10,
}

_KNOWLEDGE_FIELD_MAP = {
    # Interest signals
    "interests": "Interests & Active Curiosity",
    "curiosity": "Interests & Active Curiosity",
    "exploring": "Interests & Active Curiosity",
    "learning_areas": "Interests & Active Curiosity",
    # Projects
    "projects": "Active Projects",
    "active_work": "Active Projects",
    "current_focus": "Active Projects",
    # Expertise
    "expertise": "Expertise Map",
    "domains": "Expertise Map",
    "skills": "Expertise Map",
    "background": "Expertise Map",
    "role": "Expertise Map",
    # Goals
    "goals": "Goals & Priorities",
    "motivations": "Goals & Priorities",
    "priorities": "Goals & Priorities",
    "project_vision": "Goals & Priorities",
    # Patterns
    "communication": "Interaction Patterns",
    "decision": "Interaction Patterns",
    "autonomy": "Interaction Patterns",
    "operational": "Interaction Patterns",
}


class IdentityLoader:
    """Reads and caches identity documents from a directory.

    Expected files: SOUL.md (who Genesis is), USER.md (user profile),
    STEERING.md (hard behavioural constraints set by user).
    Files are read once and cached. Call reload() to clear cache.
    """

    def __init__(
        self,
        identity_dir: Path = _DEFAULT_DIR,
        *,
        steering_max_rules: int = _STEERING_MAX_RULES,
    ) -> None:
        self._dir = identity_dir
        self._cache: dict[str, str] = {}
        self._steering_max_rules = steering_max_rules

    def soul(self) -> str:
        return self._load("SOUL.md")

    def user(self) -> str:
        return self._load("USER.md")

    def steering(self) -> str:
        """Return STEERING.md content (hard behavioural constraints)."""
        return self._load("STEERING.md")

    def steering_rules(self) -> list[str]:
        """Return individual steering rules parsed from STEERING.md."""
        text = self.steering()
        if not text:
            return []
        rules: list[str] = []
        in_rules = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == _STEERING_SEPARATOR:
                in_rules = True
                continue
            if in_rules and stripped and not stripped.startswith("#"):
                rules.append(stripped)
        return rules

    def identity_block(self) -> str:
        """Full identity context: SOUL + USER + STEERING."""
        parts = []
        soul = self.soul()
        if soul:
            parts.append(soul)
        user = self.user()
        if user:
            parts.append(user)
        steering = self.steering()
        if steering:
            parts.append(steering)
        return "\n\n".join(parts)

    def write_user_md(
        self, model: dict, *, evidence_count: int = 0,
    ) -> None:
        """Render user model dict into USER.md and write to disk.

        Groups model fields into sections via _FIELD_SECTION_MAP.
        Clustered fields (matched by _FIELD_CLUSTERS prefix) are merged
        into single lines to keep output under _MAX_USER_MD_LINES.
        Unmapped fields go under "Observed Patterns". Clears the
        USER.md cache so the next read picks up the change.
        """
        # --- Partition fields into sections --------------------------------
        sections: dict[str, list[str]] = defaultdict(list)
        # Collect clustered "Observed Patterns" fields by cluster label
        clusters: dict[str, list[str]] = defaultdict(list)
        for field, value in model.items():
            section = _FIELD_SECTION_MAP.get(field, "Observed Patterns")
            rendered = self._render_value(value)
            if section != "Observed Patterns":
                display = field.replace("_", " ").title()
                sections[section].append(f"- **{display}**: {rendered}")
                continue
            # Check if this field belongs to a cluster
            cluster_label = self._cluster_for(field)
            if cluster_label is not None:
                truncated = rendered[:60] if len(rendered) > 60 else rendered
                clusters[cluster_label].append(truncated)
            else:
                display = field.replace("_", " ").title()
                sections["Observed Patterns"].append(
                    f"- **{display}**: {rendered}"
                )

        # --- Build output lines -------------------------------------------
        lines = ["# User Profile", ""]
        content_lines = 0  # tracks only non-header, non-blank lines

        for section_name in _USER_MD_SECTION_ORDER:
            if section_name == "Observed Patterns":
                # Render cluster lines + remaining individual lines
                cluster_lines = [
                    f"- **{label}**: {'; '.join(vals)}"
                    for label, vals in sorted(clusters.items())
                ]
                individual = sorted(sections.get("Observed Patterns", []))
                combined = cluster_lines + individual
                if not combined:
                    continue
                lines.append(f"## {section_name}")
                lines.append("")
                for entry in combined:
                    if content_lines >= _MAX_USER_MD_LINES:
                        break
                    lines.append(entry)
                    content_lines += 1
                lines.append("")
            else:
                if section_name not in sections:
                    continue
                lines.append(f"## {section_name}")
                lines.append("")
                for entry in sections[section_name]:
                    if content_lines >= _MAX_USER_MD_LINES:
                        break
                    lines.append(entry)
                    content_lines += 1
                lines.append("")

        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines.append("---")
        lines.append("")
        lines.append(
            f"*Auto-synthesized by Genesis ({evidence_count} evidence points). "
            f"Last updated: {now}. Editable via dashboard.*"
        )
        lines.append("")

        path = self._dir / "USER.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        self._cache.pop("USER.md", None)
        logger.info(
            "USER.md synthesized with %d fields (%d content lines)",
            len(model), content_lines,
        )

    def write_user_knowledge_md(
        self,
        model: dict,
        *,
        evidence_count: int = 0,
        narrative: str | None = None,
    ) -> None:
        """Render user model dict into USER_KNOWLEDGE.md (structured cache).

        Unlike write_user_md() (which targets USER.md and is disabled),
        this writes the system-owned knowledge cache.

        When ``narrative`` is provided, write LLM-synthesized prose as the
        primary content (Genesis voice, structured by theme). The narrative
        is the output of UserModelEvolver.synthesize_narrative() and replaces
        the rules-based dict rendering. When ``narrative`` is None, fall
        back to the rules-based rendering with bounded sections — used as
        graceful degradation when the LLM synthesis chain is exhausted.
        """
        if narrative is not None:
            self._write_narrative_knowledge(model, narrative, evidence_count)
            return

        sections: dict[str, list[str]] = defaultdict(list)
        unmapped: list[str] = []

        for field, value in model.items():
            section = _KNOWLEDGE_FIELD_MAP.get(field)
            rendered = self._render_value(value)
            display = field.replace("_", " ").title()

            if section:
                truncated = rendered[:120] if len(rendered) > 120 else rendered
                sections[section].append(f"- **{display}**: {truncated}")
            else:
                # Check cluster match for common prefixes
                cluster_label = self._cluster_for(field)
                if cluster_label is not None:
                    # Map cluster labels to nearest knowledge section
                    target = "Interaction Patterns"
                    sections[target].append(
                        f"- **{cluster_label}**: {rendered[:80]}"
                    )
                else:
                    unmapped.append(f"- **{display}**: {rendered}")

        # Build output
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "# User Knowledge Base",
            "",
            f"> Auto-synthesized from Genesis memory system. Last updated: {now}.",
            "> Source of truth: memory system (Qdrant + SQLite). "
            "This file is a materialized cache.",
            "> Do not hand-edit — changes will be overwritten by next synthesis cycle.",
            "",
        ]

        for section_name in _KNOWLEDGE_SECTION_ORDER:
            limit = _KNOWLEDGE_SECTION_LIMITS.get(section_name, 10)
            items = sections.get(section_name, [])

            lines.append(f"## {section_name}")
            lines.append("")
            if items:
                for entry in items[:limit]:
                    lines.append(entry)
            else:
                lines.append(f"_(no data yet — max {limit} items)_")
            lines.append("")

        # Append unmapped fields if any, under a catch-all
        if unmapped:
            lines.append("## Other Observations")
            lines.append("")
            for entry in unmapped[:10]:
                lines.append(entry)
            lines.append("")

        lines.append("---")
        lines.append("")
        lines.append(
            f"*Synthesized from {evidence_count} evidence points. "
            f"Last updated: {now}.*"
        )
        lines.append("")

        path = self._dir / "USER_KNOWLEDGE.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        self._cache.pop("USER_KNOWLEDGE.md", None)
        logger.info(
            "USER_KNOWLEDGE.md synthesized via rules (%d fields, %d sections populated)",
            len(model),
            sum(1 for s in _KNOWLEDGE_SECTION_ORDER if sections.get(s)),
        )

    def _write_narrative_knowledge(
        self, model: dict, narrative: str, evidence_count: int,
    ) -> None:
        """Write the LLM-synthesized narrative as USER_KNOWLEDGE.md.

        Adds a header preamble identifying the synthesis mode + evidence
        count, then the narrative body verbatim, then a footer timestamp.
        """
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "# User Knowledge Base",
            "",
            f"> Synthesized by Genesis from {evidence_count} evidence points. "
            f"Last updated: {now}.",
            "> Source of truth: memory system (Qdrant + SQLite). "
            "This file is a materialized cache.",
            "> Do not hand-edit — changes will be overwritten by next synthesis cycle.",
            "",
            narrative.rstrip(),
            "",
            "---",
            "",
            f"*LLM-synthesized via call site 11_user_model_synthesis. "
            f"Source model contains {len(model)} fields. Last updated: {now}.*",
            "",
        ]
        path = self._dir / "USER_KNOWLEDGE.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        self._cache.pop("USER_KNOWLEDGE.md", None)
        logger.info(
            "USER_KNOWLEDGE.md synthesized via LLM narrative "
            "(%d fields, narrative=%d chars)",
            len(model), len(narrative),
        )

    @staticmethod
    def _render_value(value: object) -> str:
        """Render a model value to a human-readable string."""
        if isinstance(value, list):
            return ", ".join(str(v) for v in value)
        if isinstance(value, dict):
            return "; ".join(f"{k}: {v}" for k, v in value.items())
        return str(value)

    @staticmethod
    def _cluster_for(field: str) -> str | None:
        """Return the cluster label if *field* matches a cluster prefix."""
        for prefix, label in _FIELD_CLUSTERS.items():
            if field.startswith(prefix):
                return label
        return None

    def add_steering_rule(self, rule: str) -> None:
        """Append a steering rule to STEERING.md with FIFO eviction.

        If the rule count exceeds the cap, the oldest rule is dropped.
        Clears the STEERING.md cache so the next read picks up the change.
        """
        path = self._dir / "STEERING.md"
        rules = self.steering_rules()
        rules.append(rule.strip())
        # FIFO eviction — drop oldest if over cap
        while len(rules) > self._steering_max_rules:
            rules.pop(0)
        self._write_steering(path, rules)
        self._cache.pop("STEERING.md", None)

    def reload(self) -> None:
        self._cache.clear()

    def _load(self, filename: str) -> str:
        if filename in self._cache:
            return self._cache[filename]
        path = self._dir / filename
        if not path.exists():
            logger.debug("Identity file not found: %s", path)
            self._cache[filename] = ""
            return ""
        text = path.read_text(encoding="utf-8").strip()
        self._cache[filename] = text
        return text

    @staticmethod
    def _write_steering(path: Path, rules: list[str]) -> None:
        """Write STEERING.md with header + rules."""
        lines = [
            "# Steering Rules",
            "",
            "Rules below are hard constraints on Genesis behavior, set by the user.",
            "Genesis MUST NOT violate these rules under any circumstances.",
            "",
            "When the user gives strong negative feedback (\"never do X\", \"stop doing Y\"),",
            "a new rule is appended here. Oldest rules are evicted when the cap is reached.",
            "",
            _STEERING_SEPARATOR,
        ]
        for rule in rules:
            lines.append(rule)
        lines.append("")  # trailing newline
        path.write_text("\n".join(lines), encoding="utf-8")
