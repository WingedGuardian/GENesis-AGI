---
name: evaluate
description: Evaluate technologies and competitive developments against Genesis architecture
consumer: cc_background_research
phase: 6
skill_type: workflow
---

# Evaluate

## Purpose

Assess a technology, tool, article, or competitive development for relevance
to Genesis. Produce a structured evaluation with clear recommendations.

## When to Use

- New tool or library surfaces that might replace or augment a Genesis component.
- Competitive product launches or updates (e.g., Cursor Automations, Devin).
- User shares an article or resource for assessment.
- Surplus compute is available and the evaluation queue is non-empty.

## Workflow

1. **Gather context** — Read the target material. If a URL, fetch and summarize.
   If a concept, research current state.
2. **Map to Genesis** — Identify which Genesis components or design decisions
   the target intersects (routing, memory, perception, surplus, etc.).
3. **Assess fit** — Score along these axes:
   - **Capability gap**: Does this solve something Genesis lacks?
   - **Replacement risk**: Could this obsolete a Genesis component?
   - **Integration cost**: How much work to adopt or adapt?
   - **Lock-in risk**: Does adopting this violate the flexibility principle?
   - **Rigor gap**: Where Genesis has an equivalent, is ours as rigorous?
     "We have X" is not the same as "our X measures effectiveness, handles
     edge cases, and improves over time." Compare the QUALITY of our
     implementation against the reference, not just its existence.
   - **Overlap Comparison table**: When Genesis has a comparable capability,
     produce the Overlap Comparison table (see Output Format below) instead
     of prose claims like "we already have this." Required whenever rigor gap
     is not "N/A — no Genesis equivalent."
4. **Recommend** — One of: ADOPT, WATCH, IGNORE, ADAPT (take the idea, not the tool).
   **Disposition posture:** Genesis's scope is everything digital — default toward
   ADOPT/ADAPT and acting now. "No current use case", "out of our wheelhouse", and
   "not obviously valuable" are NOT valid grounds for WATCH/IGNORE. A weaker
   existing Genesis version means UPGRADE (do the rigor-gap / Overlap comparison),
   never dismiss. WATCH requires a named re-activation trigger; a trigger-less
   WATCH is a disguised IGNORE — write it as an honest IGNORE with a reason. ADAPT
   (stealing patterns/rigor without adopting the code) is common and valuable, but
   it is not a polite default for "I don't want to act" — reach for ADOPT when the
   thing itself fits.
5. **Write output** — Structured evaluation in the format below.

## Output Format

When invoked from the inbox, follow the output template in `INBOX_EVALUATE.md`
(summary-first, then lens-by-lens). When invoked standalone (e.g., `/evaluate`),
use this structure:

**{target title or URL}** — {recommendation: ADOPT | WATCH | IGNORE | ADAPT}

### Summary

{1-2 paragraphs: what this is, what it means for Genesis, and the key
architectural implications. Lead with what matters most. This is a TLDR — if
a scoring axis is unremarkable, skip it here.}

**Scores:** Capability gap: {low|medium|high} · Replacement risk: {low|medium|high} · Integration cost: {low|medium|high} · Lock-in risk: {low|medium|high}

**Action items:**
- {concrete next step if any}

### Recommendation

```yaml
action: ADAPT              # ADOPT | ADAPT | WATCH | IGNORE (default toward ADOPT/ADAPT; WATCH needs a named trigger)
next_step: "One concrete sentence — what specifically to do next"
effort: Small              # Trivial | Small | Medium | Large
scope: V4                  # V4 (do now — DEFAULT) | V5 (sequenced behind named work) | Future (named blocker) | Never (philosophy conflict)
confidence: high           # low | medium | high
architecture_impact: extends  # validates | extends | challenges | irrelevant
```

Rules:
- REQUIRED on every evaluation. No exceptions.
- The `action` field must match your recommendation in the Summary.
- `next_step` must be a single concrete sentence. "Investigate further" is
  not concrete. "Extract their prompt-versioning schema and compare to
  genesis.memory.prompt_versions table" is concrete.
- Before rating IGNORE: check if the *topic* (not just the source) is relevant
  to any active skill in `src/genesis/skills/`. A shallow source can raise an
  important idea. Evaluate the idea, not the container. If the topic matters,
  research it and rate the underlying concept — even if the source itself is thin.

### Overlap Comparison

{INCLUDE ONLY when Genesis has a comparable capability. OMIT entirely when
there is no Genesis equivalent. Minimum 3 rows.}

| Dimension | Their approach | Our approach | Gap |
|-----------|---------------|--------------|-----|
| ... | ... | ... | ... |

{1-2 sentences synthesizing the table: where we're genuinely ahead, where
we're behind, and what the actionable delta is.}

### How It Helps

{Direct applicability, ready-to-use tools, validated patterns}

### How It Doesn't Help

{Incompatibilities, misalignment, maturity concerns}

### How It COULD Help

{Patterns worth stealing, future version ideas, creative applications.
Think beyond "adopt this tool" — consider incremental improvements to how
we already do something, upgrades to existing approaches, better measurement
of something we currently vibes-check, or architectural patterns that would
make an existing subsystem more rigorous.}

### What to Learn

{Engineering patterns, competitive positioning, design principles.

When Genesis has something comparable, the Overlap Comparison table above IS
your primary evidence for this lens — synthesize what the table reveals about
our implementation quality. The question is never "do we have something that
resembles this?" It's "are we doing this well enough to get the benefits it
promises?"

Examples of what "gap" looks like in practice:
- "We have prompts" vs "we have versioned prompts with outcome linkage"
- "We have task tracking" vs "we have verified completion rate metrics"
- "We have memory" vs "we have continuous quality scoring with regression tracking"
- "We have approval gates" vs "we have pass-state gating where the harness verifies independently"

Surface the gap between having a feature and having it work at the level of
rigor the reference describes.}

## References

- `docs/architecture/genesis-v3-vision.md` — Core philosophy for fit assessment
- `docs/architecture/CURRENT.md` — the live subsystem map (what Genesis actually
  has today, with per-entry freshness stamps). Consult this for any "does Genesis
  already have X?" judgment BEFORE claiming a gap or an overlap.
- **Enumerate, don't spot-check.** Before concluding Genesis "lacks X" or is
  "weaker at X", verify by enumeration against CURRENT.md and the actual code —
  a negative from one search is not evidence of absence. Confidence is capped by
  how completely you enumerated.
