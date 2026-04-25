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
4. **Recommend** — One of: ADOPT, WATCH, IGNORE, ADAPT (take the idea, not the tool).
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

### How It Helps

{Direct applicability, ready-to-use tools, validated patterns}

### How It Doesn't Help

{Incompatibilities, misalignment, maturity concerns}

### How It COULD Help

{Patterns worth stealing, future version ideas, creative applications}

### What to Learn

{Engineering patterns, competitive positioning, design principles}

## References

- `docs/architecture/genesis-v3-vision.md` — Core philosophy for fit assessment
- `docs/architecture/genesis-v3-gap-assessment.md` — Known gaps to check against
- `docs/architecture/genesis-v3-autonomous-behavior-design.md` — System design
