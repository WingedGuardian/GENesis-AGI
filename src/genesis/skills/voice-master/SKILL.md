---
name: voice-master
description: >
  Foundational voice authority and AI humanizer — writes content in user's
  authentic voice with built-in AI detection. Use when asked to write/draft/
  generate content, invoke /voice or /write-as-me or /humanize, run voice
  calibration, check "does this sound like me?", "make this sound human" /
  "de-AI this", or run AI detection ("does this sound like AI?", "check for
  AI patterns", "anti-slop check"). The anti-slop filter and ai-vocabulary.md
  function as an AI content detector — scanning for 290+ known AI writing
  patterns, statistical tells, and vocabulary signals across 3 severity tiers.
consumer: cc_foreground
phase: content
skill_type: generation
---

# Voice Master (AI Humanizer)

## Purpose

Write content that sounds authentically like the user — across any medium, for
any audience. The bar: a reader who knows the user cannot distinguish this from
something the user wrote themselves.

This skill is the voice authority. All downstream content skills (LinkedIn,
email, blog, etc.) reference this skill's exemplars and anti-slop rules.

## Voice Loading (MANDATORY)

BEFORE generating any content, you MUST read the following files using the Read
tool. Do not skip this step. Do not generate from memory.

1. Read `references/exemplars/index.md` in this skill directory
2. From the index, identify 3-5 exemplars whose tone, formality, and domain
   best match the current request
3. Read the appropriate exemplar file (`social.md`, `professional.md`, or
   `longform.md`) to get the actual exemplar text
4. Read `references/anti-slop.md` for patterns to avoid
5. Scan `references/ai-vocabulary.md` for Tier 1 dead-giveaway words to ban

**Precedence rule:** Exemplars always take precedence over `voice-dimensions.md`.
Exemplars show what the user actually sounds like. Voice dimensions are
supplementary guidance for edge cases.

**If exemplar files are empty or have no good matches:**
1. Use cross-medium exemplars that match on tone and domain. Note in output:
   "Used cross-medium exemplars — consider a calibration session for [medium]."
2. If no exemplars match at all, read `references/voice-dimensions.md` and use
   those rules as your only voice guidance.
3. At minimum, always apply the anti-slop filter. Never generate content with
   zero voice constraint.

## Core Pipeline

When generating content:

1. **Read the request** — topic, target medium, tone, audience, constraints
2. **Determine medium** — social, professional, or long-form? This selects
   which exemplar file to pull from
3. **Pick exemplars** — Read the index, select 3-5 best matches by
   medium + tone + formality + domain
4. **Generate content** — Use selected exemplars as stylistic reference.
   Match sentence structure, vocabulary level, directness, and rough edges.
   Do NOT copy exemplar content — match the style
5. **Hand off to medium skill** — If a downstream skill exists for this medium
   (e.g., linkedin-post-writer for LinkedIn), hand off the voice-loaded content
   for medium-specific formatting
6. **Anti-slop filter** — Review output against the three checks in
   `references/anti-slop.md`. If it fails, revise with specific feedback.
   Maximum 2 revision passes. After that, surface the best version with
   remaining flags noted

## Standalone Modes

### Generate

Write content directly when no medium-specific skill applies. Follow the full
pipeline above. Output the final content.

### Calibrate

Run an edit-and-learn session to refine the voice profile:

1. Ask the user for a topic they care about (or pick from their known domains)
2. Generate a short piece (2-3 paragraphs) using current exemplars
3. Write the generated content to a temporary file (e.g., `~/tmp/voice-calibrate-draft.md`)
4. Tell the user the file path and ask them to edit it to sound right, then
   let you know when done (or they can paste the edited version back)
5. Diff the original vs edited version
6. Analyze the diff: What changed? Added personality? Removed hedging? Changed
   vocabulary? Restructured sentences? Made it more direct?
7. Propose a voice insight: "You prefer X over Y in this context"
8. User confirms or corrects
9. Encode the insight — either as a new exemplar (if the edited version is
   strong enough) or as a voice-dimensions.md rule
10. Repeat. Initial session: ~10 rounds (20-30 minutes). Tune-ups: 2-3 rounds.

### Analyze

"Does this text sound like me?" Compare provided text against exemplars:
- Sentence length variation
- Vocabulary register
- Directness vs hedging
- Specificity vs abstraction
- Presence of AI-tell patterns

Report a verdict with specific observations.

### Curate

Propose new exemplars from recent transcripts or calibration sessions:
- Present candidates in batches of 5-10
- User rates: "yes this is me" / "no" / "this is me but for a different medium"
- Tag accepted exemplars with medium, tone, formality
- Update the appropriate exemplar file and index

## Anti-Slop Filter

Applied as the final step of every content generation. Three checks:

1. **Pattern matching** — Scan for known AI writing tells listed in
   `references/anti-slop.md`. Any match is a failure.
2. **Voice consistency** — Compare against selected exemplars. Does sentence
   length vary naturally? Does vocabulary match the user's register? Is the
   perspective personal or abstract? Does it have opinions or hedge everything?
3. **Specificity check** — Does the content contain at least one concrete
   detail, specific reference, or personal experience that could NOT have been
   generated about any topic by any person? If you removed the topic keywords,
   would it be indistinguishable from a template? If yes, fail.

**On failure:** Don't just flag — revise. Send specific feedback: "Paragraph 2
uses 'In today's landscape' — rephrase with a specific observation."

**Revision limit:** Maximum 2 passes. If still failing, surface the best version
with remaining flags noted for the user.

## References

All reference files are in this skill's `references/` directory:

- `references/exemplars/index.md` — Tagged index of all exemplars
- `references/exemplars/social.md` — Social media voice exemplars
- `references/exemplars/professional.md` — Professional comms exemplars
- `references/exemplars/longform.md` — Long-form writing exemplars
- `references/anti-slop.md` — AI-tell patterns to avoid + three-check framework
- `references/ai-vocabulary.md` — 290+ AI words/phrases in 3 severity tiers with alternatives
- `references/style-guide.md` — How to write like a human (opinions, rhythm, specificity)
- `references/voice-dimensions.md` — Supplementary voice rules for edge cases
