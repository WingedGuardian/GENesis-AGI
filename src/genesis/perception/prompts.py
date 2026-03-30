"""PromptBuilder — template selection, rotation, and rendering."""

from __future__ import annotations

import logging
from pathlib import Path

from genesis.perception.types import PromptContext

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_IDENTITY_DIR = Path(__file__).resolve().parent.parent / "identity"

# Mapping from template relative path to CAPS markdown filename in identity/
_IDENTITY_OVERRIDES: dict[str, str] = {
    "micro/analyst.txt": "MICRO_TEMPLATE_ANALYST.md",
    "micro/contrarian.txt": "MICRO_TEMPLATE_CONTRARIAN.md",
    "micro/curiosity.txt": "MICRO_TEMPLATE_CURIOSITY.md",
    "light/situation.txt": "LIGHT_TEMPLATE_SITUATION.md",
    "light/user_impact.txt": "LIGHT_TEMPLATE_USER_IMPACT.md",
    "light/anomaly.txt": "LIGHT_TEMPLATE_ANOMALY.md",
}

_MICRO_TEMPLATES = ["analyst", "contrarian", "curiosity"]
_LIGHT_TEMPLATES = {"situation", "user_impact", "anomaly"}
_LIGHT_DEFAULT = "situation"


class PromptBuilder:
    """Selects and renders prompt templates for reflection.

    Micro: round-robin rotation (tick_number % 3).
    Light: focus-area based (from suggested_focus or default to situation).
    """

    def __init__(
        self,
        *,
        templates_dir: Path = _TEMPLATES_DIR,
        identity_dir: Path = _IDENTITY_DIR,
    ) -> None:
        self._dir = templates_dir
        self._identity_dir = identity_dir
        self._cache: dict[str, str] = {}

    def build(self, depth: str, context: PromptContext) -> str:
        """Build a prompt for the given depth and context."""
        d = depth.lower()
        if d == "micro":
            return self._build_micro(context)
        if d == "light":
            return self._build_light(context)
        msg = f"Unsupported depth for prompt building: {depth}"
        raise ValueError(msg)

    def _build_micro(self, ctx: PromptContext) -> str:
        idx = ctx.tick_number % len(_MICRO_TEMPLATES)
        template_name = _MICRO_TEMPLATES[idx]
        template = self._load(f"micro/{template_name}.txt")
        signals_count = (
            len(ctx.signals_text.strip().split("\n")) if ctx.signals_text.strip() else 0
        )
        return template.format(
            identity=ctx.identity,
            signals_text=ctx.signals_text,
            signals_examined=signals_count,
        )

    def _build_light(self, ctx: PromptContext) -> str:
        focus = (
            ctx.suggested_focus
            if ctx.suggested_focus in _LIGHT_TEMPLATES
            else _LIGHT_DEFAULT
        )
        template = self._load(f"light/{focus}.txt")
        return template.format(
            identity=ctx.identity,
            signals_text=ctx.signals_text,
            user_profile=ctx.user_profile or "(no user profile yet)",
            cognitive_state=ctx.cognitive_state or "(no cognitive state yet)",
            memory_hits=ctx.memory_hits or "(no recent observations)",
            prior_context=ctx.prior_context or "(no prior findings)",
        )

    def _load(self, relative_path: str) -> str:
        if relative_path in self._cache:
            return self._cache[relative_path]
        # Try identity/ CAPS markdown override first
        override_name = _IDENTITY_OVERRIDES.get(relative_path)
        if override_name:
            identity_path = self._identity_dir / override_name
            if identity_path.is_file():
                text = identity_path.read_text(encoding="utf-8")
                self._cache[relative_path] = text
                logger.debug("Loaded template from identity override: %s", override_name)
                return text
        # Fall back to templates/ directory
        path = self._dir / relative_path
        text = path.read_text(encoding="utf-8")
        self._cache[relative_path] = text
        return text
