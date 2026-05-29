"""Fresh Session Test — periodic diagnostic for documentation quality.

Simulates a fresh agent session with zero memory context. Sends
CLAUDE.md + README.md to a free-tier LLM and asks 5 standard questions.
Scores how many are answerable from repo artifacts alone.

Source: learn-harness-engineering course (L03 "Fresh Session Test").
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from genesis.surplus.types import ExecutorResult, SurplusTask

if TYPE_CHECKING:
    from genesis.routing.router import Router

logger = logging.getLogger(__name__)

_QUESTIONS = [
    "What is this system? (Describe its purpose and core architecture in 2-3 sentences.)",
    "How is it organized? (Name the main packages/subsystems and their roles.)",
    "How do I run it? (What commands start the system?)",
    "How do I verify it works? (What commands test/lint/validate?)",
    "Where are we now? (What is the current state — active work, recent changes, known issues?)",
]

_CALL_SITE = "34_research_synthesis"

_PROMPT_TEMPLATE = """You are an AI agent seeing this codebase for the first time.
You have NO prior context, NO memory, NO conversation history.
You ONLY have the documentation provided below.

Answer each question based SOLELY on the documentation. If the
documentation does NOT contain enough information to answer a question,
say "UNANSWERABLE" and explain what's missing.

After answering all 5 questions, provide a JSON summary block:
```json
{{"score": <number 0-5>, "answerable": [<list of question numbers 1-5 that were answerable>], "unanswerable": [<list that were not>]}}
```

---

## Documentation

### CLAUDE.md

{claude_md}

### README.md

{readme_md}

---

## Questions

{questions}
"""


class FreshSessionTestExecutor:
    """Run the 5-question fresh session diagnostic via the router."""

    def __init__(self, *, router: Router | None = None, repo_root: Path | None = None):
        self._router = router
        self._repo_root = repo_root or Path.home() / "genesis"

    async def execute(self, task: SurplusTask) -> ExecutorResult:
        if self._router is None:
            return ExecutorResult(success=False, error="no router configured")

        # Read repo artifacts
        claude_md_path = self._repo_root / "CLAUDE.md"
        readme_path = self._repo_root / "README.md"

        try:
            claude_md = claude_md_path.read_text()[:8000]
        except (FileNotFoundError, PermissionError):
            return ExecutorResult(success=False, error=f"CLAUDE.md not found at {claude_md_path}")

        try:
            readme_md = readme_path.read_text()[:4000]
        except (FileNotFoundError, PermissionError):
            readme_md = "(README.md not found)"

        questions_text = "\n".join(
            f"{i+1}. {q}" for i, q in enumerate(_QUESTIONS)
        )

        prompt = _PROMPT_TEMPLATE.format(
            claude_md=claude_md,
            readme_md=readme_md,
            questions=questions_text,
        )

        # Route through free-tier call site
        try:
            result = await self._router.route_call(
                call_site_id=_CALL_SITE,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2000,
            )
        except Exception as exc:
            return ExecutorResult(success=False, error=f"Router call failed: {exc}")

        if not result.success:
            return ExecutorResult(
                success=False,
                error=f"LLM call failed: {result.error}",
            )

        response_text = result.content or ""

        # Parse the JSON summary from the response
        score = _extract_score(response_text)

        content = (
            f"Fresh Session Test completed (score: {score}/5)\n\n"
            f"Model: {result.model_id or result.provider_used or 'unknown'}\n\n"
            f"{response_text}"
        )

        return ExecutorResult(
            success=True,
            content=content,
            insights=[{
                "content": f"Fresh session test: {score}/5 questions answerable from repo docs alone",
                "source_task_type": task.task_type,
                "generating_model": result.model_id or result.provider_used or "unknown",
                "drive_alignment": task.drive_alignment,
                "confidence": 0.8,
                "score": score,
                "total_questions": 5,
            }],
        )


def _extract_score(text: str) -> int:
    """Extract score from the JSON summary block in the response."""
    import re
    match = re.search(r'"score"\s*:\s*(\d)', text)
    if match:
        return int(match.group(1))
    # Fallback: count non-UNANSWERABLE answers
    unanswerable_count = text.upper().count("UNANSWERABLE")
    return max(0, 5 - unanswerable_count)
