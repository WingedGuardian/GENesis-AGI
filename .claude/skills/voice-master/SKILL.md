---
name: voice-master
description: >
  Apply the user's voice when writing or editing content. Activate when
  the user says "use the voice-master skill", "write this in my voice",
  "make this sound like me", or any equivalent instruction. Use for any
  content type: professional proposals, long-form writing, social posts,
  emails, or short-form copy. Do NOT activate for code, technical docs,
  or any output the user hasn't asked to be written in their voice.
---

## Companion Files

This is the **project-level** skill (workflow + AI-tell audit rules). The
**user-level** companion at `~/.claude/skills/voice-master/` has the
exemplars and voice-dimensions that this skill references. Always load
both locations when using this skill:

- **Exemplars:** `~/.claude/skills/voice-master/exemplars/` (index.md,
  social.md, professional.md, longform.md)
- **Voice dimensions:** `~/.claude/skills/voice-master/voice-dimensions.md`

## Overview

Generates content that sounds like the user — not an AI impression of the
user. The exemplars are the primary source of truth. The voice-dimensions
file is fallback guidance for edge cases the exemplars don't cover.
**When exemplars conflict with voice-dimensions, exemplars win.**

The final output must pass an AI-tell audit before delivery. No exceptions.

## Quick Mode

For short content, skip the exemplar research pipeline. Quick Mode runs
Steps 5–7 only (generate + audit + deliver). The voice comes from
internalized markers rather than fresh exemplar analysis.

**Auto-triggers** (self-detected unless overridden):
- Content request is <200 words of output
- Content type is email, reply, DM, or Slack message
- Caller explicitly says "quick" / "quick mode" / "just dash this off"

**Audit-only** (editing existing text, not generating):
- Skip Steps 1–5 entirely
- Run Step 6 (full enhanced audit) on the provided text
- Deliver the cleaned version

**Quick Mode generation (Step 5 lite):**
- Characteristic openers where natural ("Here's the thing," "Frankly,"
  "Not for nothing")
- Match register to audience (use Register Quick Reference below)
- Evidence-first, no windup, no padding
- Do NOT load exemplar files — use internalized voice markers only

**Override:** If the user says "full mode" or "use exemplars," run the
complete 7-step workflow regardless of content length.

## Workflow

### Step 1 — Read the exemplar index

Read `exemplars/index.md`. This lists all curated exemplars with tone,
formality (1–5), and domain metadata.

### Step 2 — Select 3–5 matching exemplars

Match on:
- **Medium** — proposal, long-form, social, email, short-form?
- **Tone** — direct, analytical, reflective, blunt, measured?
- **Formality** — 1 (inner circle) to 5 (formal/public)?
- **Domain** — technical, strategy, AI, security, general?

For consulting proposals and professional writing: formality 3–4, select
from `professional.md` and `longform.md`. For social/short-form: formality
1–2, select from `social.md`.

### Step 3 — Read the matched exemplar files

Read the full text of each matched exemplar. Use them as stylistic
reference during generation — sentence structure, vocabulary level,
characteristic phrases, reasoning patterns. **Do not copy content.**

### Step 4 — Read voice-dimensions (if needed)

Read `voice-dimensions.md` for edge cases not covered by the exemplars —
register scaling by audience, tone, sentence structure rules, vocabulary,
humor handling.

### Step 5 — Generate

Write the content. Apply what you learned from the exemplars:
- Evidence-first, not windup
- Mix short punchy statements with longer reasoning
- Characteristic openers where natural: "Here's the thing," "Frankly,"
  "Not for nothing"
- No padding, no transitions for their own sake
- Register scales with audience — collared shirt for formal audiences,
  casual for inner circle. Drop profanity entirely at formality 3+.

### Step 6 — AI-tell audit (mandatory, every time)

Before delivering, scan the output and eliminate any of the following:

**Banned words and phrases:**
- delve, leverage, utilize, ensure, robust, seamless, streamline
- clean (as filler/intensifier, e.g. "clean architecture" — OK for
  literal cleanliness), smoking gun, landscape, ecosystem (when not
  literal), holistic, synergy, empower, elevate, harness, foster
- pivotal, crucial, enhance, underscore, vibrant, testament, showcase,
  intricate, evolving, navigate, journey
- it's worth noting, it is important to note, it's important to
- in conclusion, in summary, to summarize
- cutting-edge, game-changing, transformative, revolutionary
- I'd be happy to, certainly, absolutely, of course
- this allows us to, this enables, this ensures

**Banned structural patterns:**
- Opening with "I" on the first sentence
- Three-part lists that follow the exact same grammatical structure
- Passive voice overuse ("it was determined," "it should be noted")
- Rhetorical questions that aren't genuinely rhetorical
- Em-dash overuse and wrong format. Two rules, both hard:
  1. **Format:** `--` with NO spaces on either side. Not `--` with spaces.
     Not the Unicode `—`. Not ` — `. Just `--` butted up against the words.
     Wrong: `the memory, the learning loop, the reflection cycles -- Get those`
     Right: `the memory, the learning loop, the reflection cycles--Get those`
     Actually: don't write that at all. Use a period instead.
  2. **Frequency:** Reach for a comma, period, colon, or semicolon first.
     Every time. Em-dashes are for emphasis or asides where nothing else fits.
     Max 1-2 per page, not per paragraph. Stacking them is an AI fingerprint.
- Hedging openers ("It's worth considering that...")
- Sycophantic acknowledgments before answering
- "Importance" sentences — delete sentences stating impact, legacy,
  significance, or broader trends. Show why it matters, don't state
  that it matters.
- Contrast structures — delete "It's not X, it's Y" / "Not A. Not B.
  But C" / "Despite this..." patterns. AI cadence markers.
- Vague authority claims — delete "experts say," "industry reports,"
  "many believe," "studies show" without a named source. No source =
  no claim.
- General claims without evidence — replace with specifics or delete.
  No proof = doesn't belong.
- Sentence length bias — strong bias toward sentences under 16 words.
  Not a hard ban (user's voice includes longer reasoning chains) but
  flag and split where possible. If you can say it shorter, do.
- Universally applicable statements — delete sentences that could apply
  to 1000+ topics unchanged. Not specific to THIS subject = padding.

**Test:** Read each paragraph out loud. If it sounds like a polished AI
response, it needs a rewrite. If it sounds like a person thinking through
something and writing it down, it's right.

### Step 7 — Deliver

Output the final content directly. No preamble, no explanation of what
you did, no "here's the content written in your voice." Just the content.

## Register Quick Reference

| Audience | Formality | Profanity | Exemplar source |
|----------|-----------|-----------|-----------------|
| Inner circle / Genesis | 1–2 | OK | social.md |
| Professional peers | 2–3 | OK | professional.md |
| Formal / cold outreach | 3–4 | None | professional.md, longform.md |
| Public content | 4–5 | None | longform.md |

## Examples

### Single paragraph, professional proposal
**Input:** "Use the voice-master skill to write an intro paragraph for a
security section of a consulting proposal. Owner/founder audience."

**Action:** Index → select professional.md formality 3 exemplars + longform
formality 3 → generate at formality 3-4 → AI-tell audit → deliver.

### Social post
**Input:** "Write a LinkedIn post about this in my voice."

**Action:** Index → select social.md formality 2 exemplars → generate at
formality 4 (public content) → AI-tell audit → deliver.

### Full document section
**Input:** "Rewrite this section in my voice."

**Action:** Read the section, identify medium and tone needed → full
exemplar workflow → AI-tell audit paragraph by paragraph → deliver.

### Quick email reply
**Input:** "Write a reply declining the meeting. Keep it short."

**Action:** Quick Mode (auto-detected: email + short) → generate at
formality 3 with voice markers from memory → AI-tell audit → deliver.

### Audit existing text
**Input:** "Run the AI-tell audit on this paragraph."

**Action:** Audit-only path → Step 6 on provided text → deliver cleaned
version with changes noted.
