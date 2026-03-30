You are reviewing system telemetry for an AI cognitive agent.

## Identity
{identity}

## Current Signals
{signals_text}

## Task
Classify these signals. For each noteworthy signal, provide a tag. Assess overall salience (0.0-1.0) — how noteworthy is this tick compared to baseline? Flag any anomalies.

Respond in JSON:
{{
  "tags": ["tag1", "tag2"],
  "salience": 0.3,
  "anomaly": false,
  "summary": "One or two sentences describing what you see.",
  "signals_examined": {signals_examined}
}}
