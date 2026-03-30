---
name: triage-calibration
description: Daily triage accuracy calibration — use during scheduled calibration runs to verify triage classification accuracy against few-shot examples and adjust confidence thresholds
consumer: daily_calibration
phase: 6
skill_type: hybrid
---

# Triage Calibration

## Purpose

Review recent triage decisions against actual outcomes. Adjust signal weights
and depth thresholds to improve classification accuracy over time.

## When to Use

- Scheduled daily (end of day or low-activity period).
- After a significant misclassification is identified.
- After a new signal collector is added or modified.

## Workflow

1. **Collect recent triage results** — Pull the last 24h of awareness ticks
   with their depth classifications and signal readings.
2. **Match against outcomes** — For each tick, determine what actually happened:
   - Did the classified depth match the actual work required?
   - Were any signals misleadingly high or low?
3. **Compute accuracy metrics** —
   - Classification accuracy (correct depth / total ticks)
   - Over-triage rate (classified higher than needed)
   - Under-triage rate (classified lower than needed)
4. **Identify systematic errors** — Are certain signal types consistently
   miscalibrated? Are certain time periods problematic?
5. **Propose adjustments** — Suggest weight or threshold changes. Do NOT
   auto-apply in V3 (fixed weights). Log recommendations for user review.
6. **Write calibration report**.

## Output Format

```yaml
date: <YYYY-MM-DD>
period: <start> to <end>
total_ticks: <n>
accuracy: <percentage>
over_triage_rate: <percentage>
under_triage_rate: <percentage>
systematic_errors:
  - signal: <signal name>
    bias: over | under
    magnitude: <how far off>
recommendations:
  - <proposed adjustment>
```

## References

- `src/genesis/learning/triage/` — Triage pipeline
- `src/genesis/awareness/` — Awareness loop and depth classification
- `src/genesis/learning/signals/` — Signal collectors
