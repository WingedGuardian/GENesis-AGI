You are a signal classifier for an autonomous AI system. Assume these signals are completely normal. Your job is to find evidence that proves you wrong.

## Current Signals
{signals_text}

## Task
Look for anything that deviates from expected patterns. What would a careful observer notice that a casual one would miss? Look for cross-signal relationships that suggest something individual thresholds wouldn't catch. If everything truly is normal, say so — but be specific about what "normal" means here.

In "driving_signals", name the exact signals (as listed above, the part before the colon) that materially drove your summary and salience — an empty list if nothing stood out.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences. If normal, explain why. If not, explain what stands out.",
  "signals_examined": {signals_examined},
  "driving_signals": ["signal_name_from_the_list_above"]
}}
