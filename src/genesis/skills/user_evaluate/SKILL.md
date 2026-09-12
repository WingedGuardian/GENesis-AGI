---
name: user_evaluate
description: Evaluate content for personal relevance to the user using the user model
consumer: cc_background_inbox
phase: 6
skill_type: workflow
---

# User Evaluate

## Purpose

Evaluate content through the lens of what Genesis knows about the user. The
differentiator vs generic AI summary is the user model — Genesis's accumulated
understanding of who this person is, what they care about, and what they're
working on. Treat the user's placement of an item as a request for analysis,
not as proof that the item is a strong personal fit or should be adopted.

## When to Use

- User drops personal content (articles, research, ideas) into the inbox.
- Content is not Genesis-architecture-relevant but matters to the user.
- Inbox monitor classifies an item as user-relevant.
- User invokes `/user-evaluate` in a foreground session.

## Workflow

1. **Assemble user context** — Read USER.md, query `memory_recall` MCP for
   topics related to the content, check user_model_cache and recent observations.
   USER.md is the floor; the memory system is the ceiling.
2. **Fetch content** — If the request supplies URLs, fetch every supplied URL
   and individually address each source; do not stop because the first source
   seems sufficient. Never evaluate based on URL text alone. Exhaust all access
   methods before reporting failure.
3. **Apply four lenses** — Evaluate through all four, do not skip or collapse:
   - **What This Is** — content-native analysis (argument, evidence, contribution)
   - **How This Could Help You** — user-model-informed value extraction
   - **What We Could Do With It** — collaborative actions (Genesis + user)
   - **What to Watch** — critical assessment (gaps, biases, counterarguments)
4. **Tag (report-only)** — Suggest Action Timeline (Now/Soon/Someday) and
   Relevance (Direct/Tangential/Background). These are recommendations, NOT
   binding metadata.
5. **Write output** — Structured evaluation in the format below.

## Personal-Relevance Evidence Protocol

Find possible value without manufacturing personal fit. For every material
claim about relevance, distinguish:

- **Explicit** — supported by a specific user statement, current goal, active
  project, known practice, or constraint in USER.md, memory, or observations.
- **Inferred** — a plausible connection derived from named evidence. Label it as
  an inference and say what would confirm or falsify it.
- **Unknown** — no user-specific evidence is available. Give the content-native
  value and the question it raises; do not backfill a personal story.
- **Constraint-conflicted** — a stated user boundary (privacy, employer policy,
  budget, time, platform, or explicit preference) materially limits the value.

The act of saving an item is evidence of attention, not evidence of agreement,
priority, relevance strength, or adoption intent. Likewise, absence from the
user model is not evidence of irrelevance. Keep these two directions separate.

Use `Direct` only when at least one specific, current user fact supports the
connection. Generic usefulness, popularity, or broad career value does not make
an item Direct. Recommendation strength, confidence, timeline, and next step
must match the evidence status. When the connection is uncertain, prefer a
bounded experiment or a concrete question over a fabricated confident action.

For external tools, separate the valuable mechanism from its packaging. Before
suggesting that Genesis or the user build an equivalent, consider direct use,
configuration, API/MCP/CLI integration, a sidecar or container, and reuse of a
separable upstream component. Treat implementation language as integration cost,
not an automatic veto, and compare those options with the full cost of building,
testing, battle-hardening, and maintaining another implementation.

## Output Format

When invoked from the inbox, follow the output template in `INBOX_EVALUATE.md`
(summary-first, then lens-by-lens). When invoked standalone (e.g.,
`/user-evaluate`), use this structure:

**{target title or URL}**

**Timeline:** {Now | Soon | Someday} · **Relevance:** {Direct | Tangential | Background}

### Summary

{1-2 paragraphs: what this is, why it matters to the user, and what to do
about it. Lead with what matters most. If a lens contributed nothing meaningful,
don't pad — this is a TLDR, not a formality. The reader should be able to stop
here and know the key takeaway.}

**Action items:**
- {concrete collaborative next step if any}

### Recommendation

```yaml
action: explore            # adopt | explore | bookmark | potential_skip
next_step: "One concrete sentence — what specifically to do next"
effort: Small              # Trivial | Small | Medium | Large
timeline: Soon             # Now | Soon | Someday
relevance: Direct          # Direct | Tangential | Background
confidence: high           # low | medium | high
```

**Action vocabulary** (commitment gradient):
- **adopt** — high confidence, start using this now
- **explore** — medium confidence, try it and experiment before committing
- **bookmark** — relevant but not urgent, save for when timing is right
- **potential_skip** — probably not relevant, but leaving the door open

Rules:
- REQUIRED on every evaluation. No exceptions.
- The `action` field must match your recommendation in the Summary.
- `next_step` must be concrete and collaborative ("we" framing). "Look into
  this more" is not concrete. "Read the chapter on progressive summarization
  and prototype a workflow in your Obsidian vault" is concrete.
- `timeline` and `relevance` here are the machine-parseable version of the
  tags. When invoked standalone, the `**Timeline:** / **Relevance:**` line
  above serves as the human-readable quick-scan version. When invoked from
  the inbox (INBOX_EVALUATE.md template), only the YAML block appears.

### What This Is

{Content-native analysis — argument, evidence, contribution}

### How This Could Help You

{User-model-informed value extraction}

### What We Could Do With It

{Collaborative actions — Genesis + user. Go beyond "adopt or ignore."
Consider: incremental improvements to something already in play, better
measurement of something currently vibes-checked, upgrades to the approach
rather than the tool, patterns that make existing work more rigorous.

When the user asks "what can we learn from this?" — the answer includes
EVERYTHING: small refinements, architectural upgrades, measurement gaps,
better approaches to the same problem. Not just "should we use this tool."

If the user already does something similar (tool, technique, approach),
consider producing a brief comparison:

| Aspect | This approach | What you currently do | Delta |
|--------|--------------|----------------------|-------|
| {aspect} | {specific} | {specific, from user model} | {improvement?} |

This is optional (unlike the Genesis eval's required Overlap Comparison),
but encouraged when the user model reveals an existing practice in the
same space. Keep it to 2-4 rows.}

### What to Watch

{Critical assessment — gaps, biases, counterarguments}

## Key Rules

- **Assume the request matters, not that the conclusion is positive.** Find
  possible value, then calibrate personal-fit claims to user-specific evidence.
- **Never dismiss** content because the user model doesn't mention this topic.
- **Never over-filter** based on the user's known profile. They may be exploring
  new interests.
- **"We" framing** — actions are collaborative (Genesis + user), not reports.
- **Tags are suggestions** — Genesis does not dictate priority to the user.

## References

- `src/genesis/identity/USER.md` — Compressed user snapshot
- `docs/actions/user/active.md` — User action item tracking
- `docs/actions/README.md` — Action item conventions
