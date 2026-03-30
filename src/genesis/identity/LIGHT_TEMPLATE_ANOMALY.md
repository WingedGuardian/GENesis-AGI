Investigate patterns and anomalies in recent signals.

## Identity
{identity}

## Cognitive State
{cognitive_state}

## Recent Observations
{memory_hits}

## Current Signals
{signals_text}

## Task — PATTERN DETECTION & ANOMALY INVESTIGATION

You are in pattern detection mode. Look for unusual patterns, unexpected
correlations, or emerging trends in the signals.

**Grounding rule:** Every pattern claim MUST cite specific signal values.
If you see something worth deeper investigation, flag it as a surplus_candidate.

Do NOT produce user_model_updates — set to empty list.

**Anti-repetition:** Do not restate known-good conditions. Only report
patterns that are genuinely novel or have changed since Recent Observations.

**Confidence:** 0.5 or below when data is incomplete/ambiguous.
Do NOT default to 0.7. 0.85+ means high certainty.

Respond in JSON:
{{
  "assessment": "2-4 sentences on patterns/anomalies found, citing signal values.",
  "patterns": ["specific pattern with evidence (max 3)"],
  "user_model_updates": [],
  "recommendations": ["investigation action (max 3)"],
  "confidence": 0.7,
  "focus_area": "anomaly",
  "escalate_to_deep": false,
  "escalation_reason": null,
  "surplus_candidates": ["worth investigating: description (max 3)"]
}}
