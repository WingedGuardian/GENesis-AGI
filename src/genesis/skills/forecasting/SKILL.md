---
name: forecasting
description: Superforecasting with calibrated reasoning, Brier score tracking, and prediction ledger management
consumer: cc_background_research
phase: 7
skill_type: uplift
---

# Forecasting

## Purpose

Make specific, falsifiable predictions with calibrated confidence levels.
Track accuracy over time using Brier scores. Apply superforecasting
methodology (Tetlock/Good Judgment Project) to any domain — technology
trends, project outcomes, market shifts, competitive moves, risk assessment.

## When to Use

- User asks for a prediction or forecast on any topic.
- Strategic reflection identifies a decision that depends on uncertain futures.
- Surplus compute is available and a prediction review is due.
- A previously made prediction is approaching its resolution date.
- Deep reflection surfaces a trend worth formally tracking.

## Superforecasting Principles

1. **Triage** — Focus on questions where effort improves accuracy. Ignore
   questions that are either trivially knowable or fundamentally unknowable.
2. **Fermi decomposition** — Break big questions into smaller, estimable
   components. "Will X happen?" → "What's the base rate? What's different
   this time? What signals would I expect to see?"
3. **Balance inside and outside views** — Start with the reference class
   (base rate from historical analogues), then adjust with specific evidence.
   Never skip the outside view.
4. **Update incrementally** — Bayesian updating. New evidence shifts
   confidence by small amounts, not dramatic swings. Avoid overreaction.
5. **Calibration over precision** — A well-calibrated 60% is better than
   an overconfident 90%. Your 70% predictions should come true ~70% of
   the time.
6. **Distinguish noise from signal** — Most new information is noise.
   Ask: does this actually change the probability, or does it just feel
   important because it's recent?
7. **Consider contrarian views** — Actively seek evidence against your
   current position. What must be true for the opposite outcome?
8. **Post-mortem every resolution** — When a prediction resolves, analyze
   WHY you were right or wrong, not just whether. Update process, not
   just beliefs.
9. **Express uncertainty numerically** — "Likely" is ambiguous. 70% is not.
   Use the probability scale below.
10. **Separate confidence from conviction** — High confidence (90%) means
    high probability. Strong conviction means you've thought deeply. You
    can have low confidence with strong conviction (you've analyzed it
    thoroughly and it's genuinely uncertain).

## Signal Taxonomy

| Signal Type | Weight | Description |
|-------------|--------|-------------|
| Leading indicator | High | Predicts before the event (e.g., job postings predict growth) |
| Lagging indicator | Medium | Confirms after the event (e.g., quarterly earnings) |
| Base rate | High | Historical frequency of similar events |
| Expert opinion | Medium | Domain expert assessment (weight by track record) |
| Data point | High | Quantitative measurement directly relevant |
| Anomaly | High | Deviation from expected pattern — investigate |
| Structural change | Very High | Rules of the game changing (regulation, technology shift) |
| Sentiment shift | Medium | Public/market mood change (often noise, sometimes signal) |

**Signal strength:**
- **Strong** — Multiple independent sources, quantitative, leading, from sources with track record
- **Moderate** — Single authoritative source, specialist opinion, qualitative
- **Weak** — Social buzz, anecdote, rumor, single unverified claim

## Confidence Scale

| Probability | Meaning | Betting Odds |
|-------------|---------|-------------|
| 5% | Almost certainly not | 19:1 against |
| 15% | Very unlikely | ~6:1 against |
| 25% | Unlikely but plausible | 3:1 against |
| 35% | Somewhat unlikely | ~2:1 against |
| 45% | Toss-up, leaning no | ~1.2:1 against |
| 55% | Toss-up, leaning yes | ~1.2:1 for |
| 65% | Somewhat likely | ~2:1 for |
| 75% | Likely | 3:1 for |
| 85% | Very likely | ~6:1 for |
| 95% | Almost certain | 19:1 for |

**Adjustment rules:** +/-5-15% per strong signal, +/-2-5% per moderate signal.
If gut says 80% but analysis says 55%, trust the analysis.

## Cognitive Bias Checklist

Before finalizing ANY prediction, check against these 8 biases:

| Bias | Check | Fix |
|------|-------|-----|
| Anchoring | Am I stuck on the first number I thought of? | Re-derive from base rates |
| Availability | Am I overweighting recent/vivid examples? | Search for boring counterexamples |
| Confirmation | Am I only finding evidence that agrees? | Explicitly search for disconfirming evidence |
| Narrative | Am I constructing a compelling story that feels true? | Check: does the data support this without the story? |
| Overconfidence | Am I more certain than my evidence warrants? | Would I bet real money at these odds? |
| Scope insensitivity | Am I treating "some" and "a lot" as the same? | Quantify: how much exactly? |
| Recency | Am I overweighting what happened last? | Check 5-year and 10-year base rates |
| Status quo | Am I assuming things will stay the same? | What would need to change, and how likely is each change? |

## Reasoning Chain Template

For each prediction, construct:

### 1. Reference Class (Outside View)
- What is the base rate for this type of event?
- 3-5 historical analogues with outcomes
- Starting probability from base rate alone

### 2. Specific Evidence (Inside View)
- List each signal with type, strength, and direction
- For each signal: percentage adjustment from base rate
- Net adjustment

### 3. Synthesis
- Start at base rate
- Apply net adjustment
- State final probability with explicit reasoning

### 4. Key Assumptions
- What must remain true for this prediction to hold?
- For each assumption: conditional probability shift if violated

### 5. Resolution Criteria
- Exact date or trigger for resolution
- Specific, observable criteria (not subjective)
- Data source for verification

## Brier Score

`Brier = (predicted_probability - actual_outcome)^2`

Where `actual_outcome` is 0 (didn't happen) or 1 (happened).

| Score | Quality |
|-------|---------|
| < 0.10 | Excellent |
| 0.10 - 0.15 | Good |
| 0.15 - 0.25 | Average |
| 0.25 | Coin flip (no skill) |
| > 0.30 | Worse than guessing |

Track cumulative Brier score across all resolved predictions. Review
monthly. If cumulative Brier > 0.25, recalibrate methodology.

## Contrarian Mode

When explicitly requested or when consensus confidence exceeds 85%:

1. Identify the consensus view and its evidence
2. Search specifically for counter-consensus evidence
3. Ask: "What must be true for the opposite to happen?"
4. If contrarian case is credible (>15% probability), include it
5. Always label contrarian predictions as such alongside consensus

## Domain Source Guides

| Domain | Priority Sources |
|--------|-----------------|
| Technology | GitHub trending, HN, arXiv, Crunchbase, job postings, patent filings |
| Finance | FRED, SEC filings, central bank statements, VIX, yield curves |
| Geopolitics | UN resolutions, RAND, think tank reports, diplomatic cables |
| Climate/Energy | IPCC, IEA, CDP, BloombergNEF, utility filings |
| AI/ML | arXiv, model benchmarks, API pricing trends, conference papers |

## Output Format

```yaml
prediction_id: <PRED-YYYY-MM-DD-NNN>
created: <YYYY-MM-DD>
domain: <technology | finance | geopolitics | climate | ai_ml | general>
time_horizon: <1_week | 1_month | 3_months | 1_year>
prediction: <specific, falsifiable statement>
confidence: <probability 0.05-0.95>
reasoning_chain:
  reference_class:
    base_rate: <probability>
    analogues:
      - <historical analogue and outcome>
  specific_evidence:
    - signal: <description>
      type: <leading | lagging | base_rate | expert | data | anomaly | structural | sentiment>
      strength: <strong | moderate | weak>
      adjustment: <+/- percentage>
  synthesis: <narrative combining outside and inside views>
  key_assumptions:
    - assumption: <what must hold>
      if_violated: <probability shift>
resolution:
  date: <YYYY-MM-DD>
  criteria: <exact observable condition>
  data_source: <where to verify>
bias_check: <which biases were checked and adjustments made>
status: active | resolved | expired
updates:
  - date: <YYYY-MM-DD>
    old_confidence: <previous>
    new_confidence: <updated>
    reason: <what changed>
resolution_result:
  date: <YYYY-MM-DD>
  outcome: true | false
  evidence: <what happened>
  brier_score: <calculated score>
  lesson: <what to learn from this>
```

## Prediction Review Schedule

- **Weekly:** Review all active predictions. Update confidence if new evidence.
- **Monthly:** Calculate cumulative Brier score. Identify calibration drift.
- **On resolution:** Score immediately. Post-mortem. Update procedures.

## References

- Tetlock, P. (2015). Superforecasting: The Art and Science of Prediction
- `src/genesis/learning/` — Outcome tracking for Brier score integration
- `src/genesis/identity/REFLECTION_STRATEGIC.md` — Strategic reflection context
