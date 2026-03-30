"""Tier 1 triage — filters and scores signals using free/surplus models."""

from __future__ import annotations

import json
import logging
from typing import Any

from genesis.pipeline.profiles import ResearchProfile
from genesis.pipeline.types import ResearchSignal, SignalStatus, Tier

logger = logging.getLogger(__name__)

TRIAGE_PROMPT = """You are a research signal triage agent.

Given a batch of research signals, score each for relevance to the research profile.

Profile: {profile_name}
Relevance keywords: {keywords}
Exclude keywords: {exclude}

For each signal, output:
- signal_id: the ID
- relevant: true/false
- relevance_score: 0.0-1.0 (how relevant to the profile)
- tags: list of topic tags

Signals:
{signals_text}

Respond in JSON array format:
[{{"signal_id": "...", "relevant": true, "relevance_score": 0.8, "tags": ["crypto", "narrative"]}}]
"""


class TriageFilter:
    """Tier 1 triage using free/surplus models."""

    async def triage(
        self,
        signals: list[ResearchSignal],
        profile: ResearchProfile,
        *,
        router: Any = None,
    ) -> list[ResearchSignal]:
        """Score and filter signals for relevance. Returns surviving signals."""
        if not signals:
            return []

        # If no router available, fall back to keyword matching
        if router is None:
            return self._keyword_fallback(signals, profile)

        # Build prompt
        signals_text = "\n".join(
            f"- ID: {s.id} | Content: {s.content[:200]}" for s in signals
        )
        prompt = TRIAGE_PROMPT.format(
            profile_name=profile.name,
            keywords=", ".join(profile.relevance_keywords),
            exclude=", ".join(profile.exclude_keywords),
            signals_text=signals_text,
        )

        try:
            response = await router.route(prompt, tier="free")
            scored = json.loads(response)
        except Exception:
            logger.warning("LLM triage failed, falling back to keywords", exc_info=True)
            return self._keyword_fallback(signals, profile)

        # Build lookup
        score_map: dict[str, dict[str, Any]] = {s["signal_id"]: s for s in scored if "signal_id" in s}

        surviving: list[ResearchSignal] = []
        for signal in signals:
            info = score_map.get(signal.id)
            if info is None:
                # Signal not in response — keep with low score
                signal.relevance_score = 0.1
                signal.tier = Tier.TRIAGE
                signal.status = SignalStatus.TRIAGED
                surviving.append(signal)
                continue
            if info.get("relevant", False):
                signal.relevance_score = float(info.get("relevance_score", 0.5))
                signal.tags.extend(info.get("tags", []))
                signal.tier = Tier.TRIAGE
                signal.status = SignalStatus.TRIAGED
                if signal.relevance_score >= profile.min_relevance:
                    surviving.append(signal)
                else:
                    signal.status = SignalStatus.DISCARDED
            else:
                signal.status = SignalStatus.DISCARDED

        return surviving

    def _keyword_fallback(
        self,
        signals: list[ResearchSignal],
        profile: ResearchProfile,
    ) -> list[ResearchSignal]:
        """Simple keyword-based triage when no LLM is available."""
        surviving: list[ResearchSignal] = []
        for signal in signals:
            content_lower = signal.content.lower()
            # Check exclude keywords first
            if any(kw.lower() in content_lower for kw in profile.exclude_keywords):
                signal.status = SignalStatus.DISCARDED
                continue
            # Check relevance keywords
            matches = sum(1 for kw in profile.relevance_keywords if kw.lower() in content_lower)
            if matches > 0 or not profile.relevance_keywords:
                signal.relevance_score = min(1.0, matches * 0.2) if profile.relevance_keywords else 0.5
                signal.tier = Tier.TRIAGE
                signal.status = SignalStatus.TRIAGED
                surviving.append(signal)
            else:
                signal.status = SignalStatus.DISCARDED
        return surviving
