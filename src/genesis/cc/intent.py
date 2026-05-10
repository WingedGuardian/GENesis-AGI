"""Intent parser — slash command extraction only.  No LLM, no NL heuristics.

Slash commands (/model, /effort, /resume, /task) are unambiguous and safe to
extract programmatically.  Natural-language intent detection (e.g. "switch to
sonnet", "think harder") is left to the LLM — regex heuristics for NL caused
false positives and required compensating guardrails.
"""

from __future__ import annotations

import re

from genesis.cc.types import CCModel, EffortLevel, IntentResult


class IntentParser:
    _SLASH_MODEL = re.compile(r"/model\s+(sonnet|opus|haiku)", re.IGNORECASE)
    _SLASH_EFFORT = re.compile(r"/effort\s+(low|medium|high|xhigh|max)", re.IGNORECASE)
    _SLASH_RESUME = re.compile(r"/resume(?:\s+(\S+))?", re.IGNORECASE)
    _SLASH_TASK = re.compile(r"/task(?:\s+(.*))?", re.IGNORECASE)

    def parse(self, text: str) -> IntentResult:
        remaining = text
        model_override = None
        effort_override = None
        resume_requested = False
        resume_session_id = None
        task_requested = False

        # Slash commands — match and strip
        m = self._SLASH_MODEL.search(remaining)
        if m:
            model_override = CCModel(m.group(1).lower())
            remaining = remaining[: m.start()] + remaining[m.end() :]

        m = self._SLASH_EFFORT.search(remaining)
        if m:
            effort_override = EffortLevel(m.group(1).lower())
            remaining = remaining[: m.start()] + remaining[m.end() :]

        m = self._SLASH_RESUME.search(remaining)
        if m:
            resume_requested = True
            resume_session_id = m.group(1)
            remaining = remaining[: m.start()] + remaining[m.end() :]

        m = self._SLASH_TASK.search(remaining)
        if m:
            task_requested = True
            # Keep the task description (group 1) in remaining text
            task_body = (m.group(1) or "").strip()
            remaining = remaining[: m.start()] + (" " + task_body if task_body else "") + remaining[m.end() :]

        # Clean remaining text
        cleaned = " ".join(remaining.split()).strip()

        # Detect intent-only messages: slash commands extracted but no
        # meaningful text remains.  Only model/effort changes qualify —
        # resume and task need CC to execute.
        has_intent = bool(model_override or effort_override)
        _intent_only = has_intent and not cleaned

        return IntentResult(
            raw_text=text,
            model_override=model_override,
            effort_override=effort_override,
            resume_requested=resume_requested,
            resume_session_id=resume_session_id,
            task_requested=task_requested,
            cleaned_text=cleaned,
            intent_only=_intent_only,
        )
