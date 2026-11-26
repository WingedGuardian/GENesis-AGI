"""Identity document loader — reads and caches SOUL.md, USER.md, STEERING.md."""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import stat
import tempfile
import unicodedata
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from genesis.identity.paths import read_identity

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).parent
_STEERING_SEPARATOR = "---"
# A captured rule longer than this is refused, never cut: the directive filter
# counts words, and one "word" can be any length (a path, or text joined by a
# non-space blank).
_MAX_RULE_CHARS = 600
# A rule line that could pass for file structure: a heading, an HTML comment
# (the replace marker is one), or a `_[` note like the resolver's own.
_STRUCTURE_LINE = re.compile(r"^\s*(#|_\[)|<!--")

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
    "Life Structure",
]

_KNOWLEDGE_SECTION_LIMITS = {
    "Interests & Active Curiosity": 15,
    "Active Projects": 10,
    "Expertise Map": 20,
    "Goals & Priorities": 10,
    "Interaction Patterns": 10,
    "Recent Themes": 10,
    "Life Structure": 10,
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
    # Life structure
    "employment": "Life Structure",
    "job": "Life Structure",
    "income": "Life Structure",
    "life_structure": "Life Structure",
    "life_domain": "Life Structure",
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
    ) -> None:
        self._dir = identity_dir
        self._cache: dict[str, str] = {}

    def soul(self) -> str:
        return self._load("SOUL.md")

    def user(self) -> str:
        return self._load("USER.md")

    def steering(self) -> str:
        """Return STEERING.md content (hard behavioural constraints)."""
        return self._load("STEERING.md")

    def steering_rules(self) -> list[str]:
        """Return individual steering rules parsed from STEERING.md.

        Rules are stored as ``---``-delimited BLOCKS after the preamble. Each
        block may carry a ``## header`` and a multi-line body, all preserved
        verbatim; the first block (before any ``---``) is the preamble and is
        never returned as a rule. Parsing by block — not by physical line — is
        what preserves section structure across ``add_steering_rule`` rewrites.
        (The 2026-06-30 incident flattened the file because the old parser
        split per line and dropped every ``#`` header and ``---`` separator.)
        """
        return self._parse_rules(self.steering())

    @staticmethod
    def _parse_rules(text: str) -> list[str]:
        """Split STEERING text into its rule blocks (the preamble excluded)."""
        if not text:
            return []
        blocks: list[str] = []
        current: list[str] = []
        for line in text.splitlines():
            if line.strip() == _STEERING_SEPARATOR:
                blocks.append("\n".join(current))
                current = []
            else:
                current.append(line)
        blocks.append("\n".join(current))
        # blocks[0] is the preamble; blocks[1:] are the individual rules.
        return [b.strip() for b in blocks[1:] if b.strip()]

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

    def add_steering_rule(self, rule: str) -> bool:
        """Append a steering rule to STEERING.local.md, the per-install layer.

        The shipped STEERING.md is never written, and no stored rule is ever
        evicted or cut. The rule becomes its own ``---``-delimited block after
        the local file's existing ones (inside a replace layer, after its
        body), so the resolver reads it after every rule already in force. A
        rule already in force (compared with whitespace normalized) is not
        added twice. A line that is exactly the separator is neutralized to an
        em-dash, so a body is never split.

        Refused with a warning, returning False: a rule over _MAX_RULE_CHARS,
        or one with a line matching _STRUCTURE_LINE. A STEERING.local.md that
        is a symlink or not a regular file raises.

        The read-modify-write holds a lock on the identity directory (held for
        milliseconds; the server is the only writer today) and swaps in a
        fresh 0600 file, fsynced, created under a random name. Returns whether
        a rule was written. Logs name a rule by a short key, never its text: the
        text is private (gitignored, 0600) and the journal reaches diagnostics.
        """
        safe_rule = re.sub(r"(?m)^\s*---\s*$", "—", "\n".join(rule.strip().splitlines()))
        if not safe_rule:
            return False
        structure = [
            n for n, line in enumerate(safe_rule.splitlines(), 1) if _STRUCTURE_LINE.search(line)
        ]
        if len(safe_rule) > _MAX_RULE_CHARS or structure:
            reason = (
                f"over {_MAX_RULE_CHARS} chars"
                if len(safe_rule) > _MAX_RULE_CHARS
                else f"a heading, comment or note on line {structure[0]}"
            )
            logger.warning(
                "Steering rule refused (%s; %d chars, key %s)", reason, len(safe_rule), _rule_tag(safe_rule)
            )
            return False
        path = self._dir / "STEERING.local.md"
        fd = os.open(self._dir, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # First, so the resolver read below never opens a symlink or a FIFO.
            text = _read_regular_file(path)
            # A staging file only outlives a write killed mid-swap; the lock means
            # no other writer is using one, so any left here is garbage. Only
            # mkstemp's own names (8 of [a-z0-9_]) are removed.
            for stale in self._dir.glob("." + "?" * 8 + "." + path.name):
                if re.fullmatch(r"\.[a-z0-9_]{8}\." + re.escape(path.name), stale.name):
                    stale.unlink(missing_ok=True)
            if _rule_key(safe_rule) in _in_force_keys(read_identity("STEERING.md", self._dir) or ""):
                logger.debug("Steering rule already in force (key %s)", _rule_tag(safe_rule))
                return False
            # Every rule follows a separator, the first one too: the resolver
            # and _parse_rules read a file's first block as a preamble, never a
            # rule, and a rule can then never become the replace marker line.
            new = text.rstrip() + ("\n\n" if text.strip() else "") + "---\n\n" + safe_rule + "\n"
            tmp_fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".", suffix="." + path.name)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as out:
                    out.write(new)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        finally:
            os.close(fd)
            self._cache.pop("STEERING.md", None)
        logger.info("Steering rule written to %s (%d chars, key %s)", path.name, len(safe_rule), _rule_tag(safe_rule))
        return True

    def reload(self) -> None:
        self._cache.clear()

    def _load(self, filename: str) -> str:
        if filename in self._cache:
            return self._cache[filename]
        text = read_identity(filename, self._dir)
        if text is None:
            logger.debug("Identity file not found: %s", self._dir / filename)
            self._cache[filename] = ""
            return ""
        text = text.strip()
        self._cache[filename] = text
        return text


def _rule_key(rule: str) -> str:
    """A rule compared for duplicates: NFC, whitespace runs collapsed.

    NFC, never NFKC or casefold: those fold characters that mean different
    things (a fullwidth digit, a superscript, a flag's case), and a false
    match drops a distinct owner rule, where a missed one only stores a
    duplicate.
    """
    return " ".join(unicodedata.normalize("NFC", rule).split())


def _rule_tag(rule: str) -> str:
    """A short, text-free name for a rule in logs."""
    return hashlib.sha256(_rule_key(rule).encode("utf-8")).hexdigest()[:12]


def _in_force_keys(resolved: str) -> set[str]:
    """Keys of every block of the resolved steering text, the first one too: a
    hand-written local or replace layer often has no separator. Whole blocks
    only, never single lines: a line of a longer rule is not that rule, and a
    false match would drop the new one. An accepted rule never holds a heading,
    so it can never equal the shipped preamble or a shipped block.
    """
    resolved = re.sub(r"\A_\[The shipped .*?\]_\n\n", "", resolved)
    return {_rule_key(b) for b in (b.strip() for b in ("\n" + resolved).split("\n---\n")) if b}


def _read_regular_file(path: Path) -> str:
    """The text of *path*, "" when absent; never through a symlink or a non-file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError(f"{path} is not a regular file")
        return handle.read().decode("utf-8")
