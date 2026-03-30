"""Fresh-eyes review — cross-model validation for surplus outreach."""

from __future__ import annotations

import json
import logging

from genesis.outreach.types import FreshEyesResult

logger = logging.getLogger(__name__)

_REVIEW_PROMPT = """You are reviewing a proactive message that an AI system wants to send to its user.

Draft content: {draft}
Topic: {topic}

Rate this on a scale of 1-5:
1 = Irrelevant or annoying — user would not want this
2 = Too vague to be useful
3 = Mildly interesting but not actionable
4 = Relevant and actionable — user would appreciate it
5 = Highly valuable — user would be glad to receive this

Respond with JSON only: {{"score": <int>, "reason": "<one sentence>"}}"""


class FreshEyesReview:
    """Cross-model validation for surplus outreach before sending."""

    def __init__(self, router: object, *, min_score: float = 3.0) -> None:
        self._router = router
        self._min_score = min_score

    async def review(self, draft: str, topic: str) -> FreshEyesResult:
        prompt = _REVIEW_PROMPT.format(draft=draft, topic=topic)
        try:
            messages = [{"role": "user", "content": prompt}]
            result = await self._router.route_call("23_fresh_eyes_review", messages)
            if not result.success or not result.content:
                raise ValueError(f"Review call failed: {result.error}")
            parsed = json.loads(result.content)
            score = float(parsed.get("score", 0))
            reason = parsed.get("reason", "")
            return FreshEyesResult(
                approved=score >= self._min_score,
                score=score,
                reason=reason,
                model_used=result.model_id or "unknown",
            )
        except Exception as exc:
            logger.warning("Fresh-eyes review failed: %s", exc)
            return FreshEyesResult(
                approved=False,
                score=0.0,
                reason=f"Review error: {exc}",
                model_used="none",
            )
