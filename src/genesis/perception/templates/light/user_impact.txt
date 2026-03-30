How do current conditions affect the user's goals and work?

## Identity
{identity}

## Cognitive State
{cognitive_state}

## Recent Observations
{memory_hits}

## Current Signals
{signals_text}

## Task — USER IMPACT ANALYSIS

You are in user impact analysis mode. This is the ONLY reflection mode
that produces user model updates.

**User model updates:** Only propose a delta if:
1. You have specific signal or observation evidence (cite it)
2. Confidence >= 0.9
3. The delta is genuinely NEW information not already established

Do NOT produce surplus_candidates — set to empty list.

**Anti-repetition:** Do not echo observations from above. Focus on what
signals mean for the user's active projects, not on restating system state.

**Confidence:** 0.5 or below when data is incomplete/ambiguous.
Do NOT default to 0.7. 0.85+ means high certainty.

Respond in JSON:
{{
  "assessment": "2-4 sentences on user impact, citing evidence.",
  "patterns": ["user-relevant pattern (max 3)"],
  "user_model_updates": [
    {{"field": "field_name", "value": "observed_value", "evidence": "specific signal/observation citation", "confidence": 0.8}}
  ],
  "recommendations": ["actionable for user (max 3)"],
  "confidence": 0.7,
  "focus_area": "user_impact",
  "escalate_to_deep": false,
  "escalation_reason": null,
  "surplus_candidates": []
}}
