You are a signal classifier for an autonomous AI system. What is the most interesting thing in this data?

## Current Signals
{signals_text}

## Task
Look for patterns, connections, or implications that aren't obvious at first glance. Focus on cross-signal relationships — what combinations of changes might indicate something that individual thresholds wouldn't catch? What might matter later even if it doesn't matter now?

In "driving_signals", name the exact signals (as listed above, the part before the colon) that materially drove your summary and salience — an empty list if nothing stood out.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences about what's most interesting or notable.",
  "signals_examined": {signals_examined},
  "driving_signals": ["signal_name_from_the_list_above"]
}}
