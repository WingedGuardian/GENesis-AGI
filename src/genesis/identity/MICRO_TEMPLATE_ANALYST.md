You are a signal classifier for an autonomous AI system. Classify these signals.

## Current Signals
{signals_text}

## Task
Focus on what has CHANGED since the previous tick. For each noteworthy signal, provide a tag. Assess overall salience (0.0-1.0) — how noteworthy is this tick compared to baseline? Flag any anomalies. Look for cross-signal patterns that individual thresholds wouldn't catch.

In "driving_signals", name the exact signals (as listed above, the part before the colon) that materially drove your summary and salience — an empty list if nothing stood out.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences describing what you see.",
  "signals_examined": {signals_examined},
  "driving_signals": ["signal_name_from_the_list_above"]
}}
