"""Two-pass resume review: native LLM analysis + knowledge-augmented critique.

Pass 1: Structural analysis — formatting, clarity, impact quantification,
action verbs, ATS compatibility, keyword alignment (if JD provided).

Pass 2: Knowledge-augmented critique — queries the knowledge base for
professional context and domain knowledge, cross-references with the
resume, identifies gaps and missed opportunities.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 41_resume_review_pass1 — structural analysis pass (no knowledge augmentation).
# 42_resume_review_pass2 — knowledge-augmented critique pass.
_PASS1_CALL_SITE = "41_resume_review_pass1"
_PASS2_CALL_SITE = "42_resume_review_pass2"

_PASS1_SYSTEM_PROMPT = """\
You are an expert resume reviewer. Analyze the resume for:

1. **Structure & Formatting**: Section organization, consistency, readability
2. **Clarity & Conciseness**: Are descriptions clear? Any verbose or vague language?
3. **Impact Quantification**: Are achievements backed by numbers, metrics, outcomes?
4. **Action Verbs**: Strong vs weak verb usage, active voice consistency
5. **Consistency**: Tense, formatting style, bullet point structure
6. **ATS Compatibility**: Standard sections, keyword presence, parseable format

If a job description is provided, also evaluate:
7. **Keyword Alignment**: How well does the resume match the JD requirements?
8. **Gap Analysis**: What the JD asks for that the resume doesn't address

Output a structured JSON object with:
- overall_score: float 0-10
- sections: dict mapping each dimension above to {score: float, findings: list[str], suggestions: list[str]}
- top_priorities: list of the 3 most impactful improvements
- strengths: list of what the resume does well

Output ONLY the JSON object, no markdown fences."""

_PASS2_SYSTEM_PROMPT = """\
You are an expert resume reviewer with access to the candidate's background
knowledge and domain expertise. You have two inputs:

1. The resume being reviewed
2. Background knowledge about the candidate and their field

Your job is to provide KNOWLEDGE-AUGMENTED feedback that goes beyond what
any generic AI reviewer could give:

- Cross-reference the resume with the candidate's actual experience,
  certifications, projects, and skills from the knowledge base
- Identify skills and accomplishments that the resume UNDERSELLS or OMITS
- Suggest domain-specific framing improvements grounded in actual knowledge
- Flag where the resume claims something the knowledge base provides
  stronger evidence for

Output a structured JSON object with:
- augmented_suggestions: list of {suggestion: str, grounded_in: str, priority: str}
  where grounded_in cites the specific knowledge that backs the suggestion
- missed_opportunities: list of {what: str, evidence: str}
  things in the knowledge base that should be on the resume but aren't
- framing_improvements: list of {current: str, suggested: str, reason: str}
  where the knowledge base suggests better framing
- overall_assessment: str — 2-3 sentence summary of how well the resume
  represents what the knowledge base reveals about the candidate

Output ONLY the JSON object, no markdown fences."""


@dataclass
class ResumeReview:
    """Combined output of both review passes."""

    pass1_analysis: dict = field(default_factory=dict)
    pass2_augmented: dict = field(default_factory=dict)
    combined_output: str = ""
    knowledge_citations: list[dict] = field(default_factory=list)
    error: str | None = None


class ResumeReviewer:
    """Two-pass resume review leveraging the knowledge base."""

    def __init__(self, router: object) -> None:
        self._router = router

    async def review(
        self,
        resume_text: str,
        *,
        job_description: str | None = None,
        knowledge_domains: list[str] | None = None,
    ) -> ResumeReview:
        """Run the full two-pass review."""
        if not resume_text.strip():
            return ResumeReview(error="Empty resume text")

        # Pass 1: Native LLM analysis
        pass1 = await self._pass1(resume_text, job_description)

        # Pass 2: Knowledge-augmented critique
        kb_context = await self._query_knowledge_base(
            resume_text, knowledge_domains
        )
        pass2 = await self._pass2(resume_text, pass1, kb_context)

        # Combine outputs
        combined = self._format_combined(pass1, pass2)

        # Extract citations
        citations = []
        for item in pass2.get("augmented_suggestions", []):
            if item.get("grounded_in"):
                citations.append({
                    "suggestion": item.get("suggestion", ""),
                    "grounded_in": item["grounded_in"],
                })

        return ResumeReview(
            pass1_analysis=pass1,
            pass2_augmented=pass2,
            combined_output=combined,
            knowledge_citations=citations,
        )

    async def _pass1(
        self, resume_text: str, job_description: str | None
    ) -> dict:
        """Pass 1: Native LLM structural analysis."""
        user_content = f"Resume:\n\n{resume_text}"
        if job_description:
            user_content += f"\n\n---\n\nJob Description:\n\n{job_description}"

        messages = [
            {"role": "system", "content": _PASS1_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await self._router.route_call(_PASS1_CALL_SITE, messages)
            if result.success and result.content:
                return self._parse_json(result.content)
        except Exception:
            logger.warning("Resume review Pass 1 failed", exc_info=True)

        return {"error": "Pass 1 analysis failed"}

    async def _pass2(
        self, resume_text: str, pass1: dict, kb_context: str
    ) -> dict:
        """Pass 2: Knowledge-augmented critique."""
        if not kb_context.strip():
            return {"note": "No knowledge base context available for Pass 2"}

        user_content = (
            f"Resume:\n\n{resume_text}\n\n"
            f"---\n\nPass 1 Analysis Summary:\n{json.dumps(pass1.get('top_priorities', []))}\n\n"
            f"---\n\nCandidate Background & Domain Knowledge:\n\n{kb_context}"
        )

        messages = [
            {"role": "system", "content": _PASS2_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]

        try:
            result = await self._router.route_call(_PASS2_CALL_SITE, messages)
            if result.success and result.content:
                return self._parse_json(result.content)
        except Exception:
            logger.warning("Resume review Pass 2 failed", exc_info=True)

        return {"error": "Pass 2 analysis failed"}

    async def _query_knowledge_base(
        self, resume_text: str, domains: list[str] | None
    ) -> str:
        """Query the knowledge base for relevant context."""
        try:
            import genesis.mcp.memory_mcp as memory_mod

            memory_mod._require_init()
            if memory_mod._retriever is None or memory_mod._db is None:
                return ""

            # Query for professional context
            results = await memory_mod._retriever.recall(
                resume_text[:500], source="knowledge", limit=15
            )

            # Also do FTS search for specific terms
            fts_results = []
            try:
                fts_results = await memory_mod.knowledge.search_fts(
                    memory_mod._db, resume_text[:200], limit=10
                )
            except Exception:
                logger.warning("Failed to FTS search for resume context", exc_info=True)

            # Combine and deduplicate
            seen: set[str] = set()
            context_parts: list[str] = []

            for r in results:
                if r.memory_id not in seen:
                    seen.add(r.memory_id)
                    context_parts.append(f"[Knowledge: {r.source}]\n{r.content}")

            for fts in fts_results:
                uid = fts["unit_id"]
                if uid not in seen:
                    seen.add(uid)
                    concept = fts.get("concept", "")
                    body = fts.get("body", "")
                    context_parts.append(f"[Knowledge: {concept}]\n{body}")

            return "\n\n---\n\n".join(context_parts[:20])
        except Exception:
            logger.warning("Knowledge base query failed", exc_info=True)
            return ""

    def _format_combined(self, pass1: dict, pass2: dict) -> str:
        """Format the combined review output."""
        lines: list[str] = []

        lines.append("# Resume Review\n")

        # Pass 1 summary
        score = pass1.get("overall_score")
        if score is not None:
            lines.append(f"## Overall Score: {score}/10\n")

        strengths = pass1.get("strengths", [])
        if strengths:
            lines.append("## Strengths\n")
            for s in strengths:
                lines.append(f"- {s}")
            lines.append("")

        priorities = pass1.get("top_priorities", [])
        if priorities:
            lines.append("## Top Priorities\n")
            for i, p in enumerate(priorities, 1):
                lines.append(f"{i}. {p}")
            lines.append("")

        # Pass 2 augmented suggestions
        augmented = pass2.get("augmented_suggestions", [])
        if augmented:
            lines.append("## Knowledge-Augmented Suggestions\n")
            for item in augmented:
                suggestion = item.get("suggestion", "")
                grounded = item.get("grounded_in", "")
                priority = item.get("priority", "medium")
                lines.append(f"- [{priority.upper()}] {suggestion}")
                if grounded:
                    lines.append(f"  *Based on: {grounded}*")
            lines.append("")

        missed = pass2.get("missed_opportunities", [])
        if missed:
            lines.append("## Missed Opportunities\n")
            for item in missed:
                lines.append(f"- **{item.get('what', '')}**: {item.get('evidence', '')}")
            lines.append("")

        assessment = pass2.get("overall_assessment", "")
        if assessment:
            lines.append(f"## Assessment\n\n{assessment}\n")

        return "\n".join(lines)

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Parse JSON from LLM response."""
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [line for line in lines if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()

        try:
            result = json.loads(text)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass

        return {"raw_response": text}
