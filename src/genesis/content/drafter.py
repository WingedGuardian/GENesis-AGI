"""ContentDrafter — LLM-backed content generation."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from genesis.content.formatter import ContentFormatter
from genesis.content.types import DraftRequest, DraftResult

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger("genesis.content.drafter")


class ContentDrafter:
    """Drafts content via LLM, then formats for target platform."""

    def __init__(
        self,
        router: Router | None = None,
        event_bus=None,
    ) -> None:
        self._router = router
        self._event_bus = event_bus
        self._formatter = ContentFormatter()

    async def draft(
        self,
        request: DraftRequest,
        *,
        call_site_id: str = "35_content_draft",
    ) -> DraftResult:
        """Draft content. Falls back to topic as content if no router."""
        if self._router is None:
            formatted = self._formatter.format(request.topic, request.target)
            return DraftResult(content=formatted, raw_draft=request.topic)

        prompt = self._build_prompt(request)
        messages: list[dict] = []
        system_prompt = request.system_prompt or self._load_voice()
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        result = None
        try:
            result = await self._router.route_call(
                call_site_id=call_site_id,
                messages=messages,
            )
            raw = result.content or request.topic
        except Exception:
            logger.warning("LLM draft failed, using topic as fallback", exc_info=True)
            raw = request.topic

        formatted = self._formatter.format(raw, request.target)
        return DraftResult(
            content=formatted,
            model_used=getattr(result, "model_id", "") if result is not None else "",
            raw_draft=raw,
        )

    @staticmethod
    def _load_voice() -> str | None:
        """Load VOICE.md as default system prompt for unguided drafts."""
        from pathlib import Path

        path = Path(__file__).resolve().parent.parent / "identity" / "VOICE.md"
        if path.exists():
            return path.read_text(encoding="utf-8")
        return None

    @staticmethod
    def _build_prompt(request: DraftRequest) -> str:
        parts = [f"Draft content about: {request.topic}"]
        if request.context:
            parts.append(f"Context: {request.context}")
        parts.append(f"Platform: {request.target}")
        parts.append(f"Tone: {request.tone}")
        if request.max_length:
            parts.append(f"Max length: {request.max_length} characters")
        return "\n".join(parts)
