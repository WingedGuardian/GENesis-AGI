Assess the current situation across all active signals.

## Identity
{identity}

## Cognitive State
{cognitive_state}

## Recent Observations
{memory_hits}

## Current Signals
{signals_text}

## Task — SITUATION ASSESSMENT

You are in situation assessment mode.

**Grounding rule:** Every claim in your assessment MUST cite a specific signal
value or observation timestamp. If you cannot cite evidence, do not make the claim.

**Anti-repetition:** If a condition appears in Recent Observations above AND
Current Signals still confirm it, note "persists" with the current value.
If signals no longer confirm it, omit it entirely. Silence > stale echoes.

Do NOT produce user_model_updates — set to empty list.
Do NOT produce surplus_candidates — set to empty list.

**Confidence:** 0.5 or below when data is incomplete/ambiguous.
Do NOT default to 0.7. 0.85+ means high certainty.

Respond in JSON:
{{
  "assessment": "2-4 sentences citing specific signal values.",
  "patterns": ["cross-signal pattern with evidence (max 3)"],
  "user_model_updates": [],
  "recommendations": ["actionable recommendation (max 3)"],
  "confidence": 0.7,
  "focus_area": "situation",
  "escalate_to_deep": false,
  "escalation_reason": null,
  "surplus_candidates": []
}}
