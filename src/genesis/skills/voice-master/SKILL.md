---
name: voice-master
description: >
  Foundational voice authority and AI humanizer — writes content in the user's
  authentic voice with built-in AI detection, and supports stealth / anti-
  attribution writing (forum personas, anonymous posts, "write as not-me").
  Use when asked to write/draft/generate content, invoke /voice, /write-as-me,
  or /humanize, run voice calibration, check "does this sound like me?", "make
  this sound human" / "de-AI this", "write a forum post as [persona]", or run
  AI detection ("does this sound like AI?", "check for AI patterns", "anti-slop
  check"). Do NOT use this skill for code, technical docs, or any output the
  user has not asked to be written in their voice — code styling defers to the
  separate code-voice skill.
consumer: cc_foreground
phase: content
skill_type: generation
---

# Voice Master (AI Humanizer + Stealth Writer)

## THIS IS A TWO-PART SKILL — BOTH PARTS ARE MANDATORY

1. **Voice matching** — load exemplars, match tone, vocabulary, rhythm.
2. **Anti-AI-slop pass** — audit the output for AI tells and fix them.

Apply BOTH on every use. Never match voice without the slop pass; never skip
the slop pass because "it sounds right." If you are editing existing text
rather than generating, run the audit (Workflow step 5) on it directly. Both
paths end with the audit. No exceptions.

## Purpose

Write content that sounds authentically like the user — across any medium, for
any audience. The bar: a reader who knows the user cannot tell it from
something they wrote. This skill is the voice authority; downstream content
skills (LinkedIn, email, blog) reference its overlay loader and anti-slop
rules. When writing as someone other than the user (forum personas, anonymous
posts, genericized output), **stealth mode** neutralizes the user's voice
fingerprints and layers a persona or anonymous register on top.

## When to Use / Boundaries

- USE for: any content the user wants in their voice — proposals, long-form,
  social posts, emails, forum posts, persona/stealth writing; voice
  calibration; AI-detection / "does this sound like AI?" audits.
- Do NOT use this skill for code, technical documentation, commit messages, or
  any output the user has not asked to be written in a human voice. Code
  styling defers to the separate `code-voice` skill. If unsure whether the
  user wants their voice applied, ask before activating.

## Quick Mode

For short content, skip the exemplar-research pipeline and generate from
internalized voice markers plus the audit.

- **Auto-triggers** (self-detected unless overridden): output < ~200 words; or
  content type is email / reply / DM / Slack; or the user says "quick" / "just
  dash this off".
- **Audit-only** (editing existing text, not generating): skip voice loading;
  run the Workflow step-5 audit on the provided text; deliver the cleaned text.
- **Override:** if the user says "full mode" / "use exemplars", run the full
  pipeline regardless of length.

In Quick Mode, use characteristic markers where natural ("Here's the thing,"
"Frankly," "Not for nothing"), match register to audience (see table below),
lead with evidence, no windup. Do NOT load exemplar files — use internalized
markers. Always still run the anti-slop audit.

## User Calibration Overlay

Voice-master separates **skill machinery** (this file, the rules, anti-slop and
style references — shipped in the repo) from **user data** (exemplars,
voice-dimensions — kept OUTSIDE the repo so personal writing never enters
version control).

- **Overlay root:** `~/.claude/skills/voice-master/` (override via
  `GENESIS_VOICE_OVERLAY`). Layout: `voice-dimensions.md` + `exemplars/`
  (index.md, social.md, professional.md, longform.md, …).
- **Resolution** (full procedure in `references/overlay-loading.md` — follow it
  exactly; resolve the path with Bash, never shell-expand inside the Read tool):
  1. Resolve `<overlay_root>` via Bash: `echo "${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master}"`. If executing Bash is unavailable, restricted, or timed out, immediately assume the fallback directory `~/.claude/skills/voice-master` silently and proceed without hanging or throwing errors.
  2. Read `<overlay_root>/voice-dimensions.md`. If missing, fall back to
     `references/voice-dimensions-TEMPLATE.md` AND emit the MANDATORY
     no-overlay warning (see overlay-loading.md) — output won't match the user.
  3. Read `<overlay_root>/exemplars/index.md`; pick 3-5 matches; read those
     files. If missing, see `references/exemplars/README.md` and warn that no
     exemplars were loaded.
  4. Never cache overlay state — re-resolve every request.
- **Hygiene:** never write user data into the in-repo template files;
  calibration writes go only to `<overlay_root>/`. Exemplars take precedence
  over voice-dimensions.

## Workflow

Two orthogonal axes: **what** (generate [default] / calibrate / analyze /
curate) × **whose voice** (user's [default] / stealth, see Stealth Mode).

1. **Detect medium** — social / professional / forum / longform. An explicit
   medium wins; else map the named platform; else infer from shape (default to
   professional). Medium selects the anti-slop section to apply and whether
   `references/media/forum.md` loads (forum only).
2. **Load voice** — run the User Calibration Overlay resolution; pick 3-5
   exemplars by medium + tone + formality + domain. (Quick Mode skips this and
   uses internalized markers.)
3. **Generate** — use exemplars as a *stylistic* reference only: match sentence
   structure, vocabulary level, directness, and rough edges. **Never copy their
   content** — not sentences, claims, phrasings, or specifics; lift the rhythm,
   never the substance. **On-topic trap:** if an exemplar happens to share the
   task's topic, that is the single highest-risk case for accidental copying —
   take its cadence and nothing else, because its facts are stale and not yours.
   **Facts, numbers, dates, and durations come ONLY from the task input** — never
   invent stale-able specifics (elapsed time like "four months building",
   star/line/version counts, adoption stats); if a number isn't given, omit it.
   Register scales with audience (see table); drop profanity at formality 3 and
   above.
4. **Hand off to medium skill** — if a downstream skill exists for the medium
   (e.g., linkedin-post-writer), hand the voice-loaded content off for
   medium-specific formatting.
5. **Anti-slop audit (mandatory, every time)** — scan against
   `references/anti-slop.md` (the Universal section plus the current-medium
   section) and Tier-1 words in `references/ai-vocabulary.md`. **Em-dash hard
   rule:** a spaced em dash (` — `) is the #1 AI tell — if it appears anywhere,
   the audit FAILED; never spaced, max 1-2 per page, prefer a comma / period /
   colon. Also check specificity (one concrete detail that couldn't apply to
   any topic) and natural sentence-length variation. On failure, revise with
   specific feedback; max 2 passes, then surface the best version with
   remaining flags noted. Final gut check — read it aloud: if it sounds like a
   polished AI response, rewrite it; if it sounds like a person thinking on the
   page, it's right. **Mechanical backstop:** the spaced-em-dash and banned-word
   tells are also enforced in code — run `python -m genesis.content.antislop
   <file>` (cleaned text to stdout, fixes/flags to stderr) to auto-fix spaced em
   dashes and flag banned words deterministically. Content sent through Genesis's
   external channels (email, Discord) and Medium drafts passes through this
   scrubber automatically; your audit is the first line, not the only one.

**Modes** beyond the default generate:

- **Calibrate** — edit-and-learn loop to refine the profile: generate a short
  piece → user edits → diff → propose a voice insight → on confirmation, write
  it to the **overlay** (never the in-repo template). Full loop in
  `references/overlay-loading.md`.
- **Analyze** — "does this sound like me?" Compare the text to the active
  exemplars on sentence variation, register, directness, specificity, and
  AI-tells; report a verdict with specific observations.
- **Curate** — propose new exemplars from transcripts in batches of 5-10; on
  acceptance, tag (medium / tone / formality / domain) and write to the overlay.

## Stealth Mode (Anti-Attribution Writing)

Use when the output must NOT be attributable to the user — forum personas,
anonymous posts, test accounts, intentionally genericized writing. Load
`references/stealth-writing.md` and follow its protocol. Three sub-modes:

1. **Anonymous** — not-the-user, with no positive target.
2. **Persona** — write AS a defined character from `~/.claude/personas/<name>/`.
3. **Impersonation** — mimic a specific named voice; **hard limits apply** (see
   stealth-writing.md); never impersonate a living public figure in
   adversarial or defamatory contexts.

Pipeline (abbreviated; full version in `references/stealth-writing.md`): load
the user's profile to identify the fingerprints to **neutralize** (not the
voice to produce); load the persona directory if one is specified; generate
candidates that avoid the user's fingerprints; run the **adversarial critic**
(a separate scoring call, 0-10 on plausibility); select if the best candidate
scores ≥7, otherwise regenerate once, then surface to the user. In persona
mode, log the result to `~/.genesis/personas.db`.

## Output Format

Output the final content directly — no preamble, no "here's the content in your
voice," no explanation of what you did. In the no-overlay fallback, prepend the
MANDATORY warning line first. In stealth mode, return only the neutralized /
persona text. When auditing existing text, return the cleaned version (note the
changes if asked).

## Examples

- **Professional proposal paragraph** (owner/founder audience): overlay →
  professional + longform exemplars (formality 3-4) → generate → audit → deliver.
- **LinkedIn post**: overlay → social exemplars → generate at public formality
  → audit (watch em dashes and hype words) → deliver.
- **Quick email reply** ("decline the meeting, keep it short"): Quick Mode
  (email + short) → markers from memory → audit → deliver.
- **"Does this sound like me?"**: Analyze mode → compare to exemplars → verdict.
- **Audit existing text** ("de-AI this paragraph"): audit-only path → step-5
  audit → cleaned version.

## Register Quick Reference

| Audience | Formality | Profanity | Exemplar source |
|---|---|---|---|
| Inner circle / Genesis | 1–2 | OK | social.md |
| Professional peers | 2–3 | OK | professional.md |
| Formal / cold outreach | 3–4 | None | professional.md, longform.md |
| Public content | 4–5 | None | longform.md |

## References

In-repo machinery (generic, shipped in the public template):

- `references/overlay-loading.md` — full overlay resolution + calibrate loop + hygiene.
- `references/anti-slop.md` — medium-scoped AI-tell patterns + the em-dash hard rule.
- `references/ai-vocabulary.md` — 290+ AI words/phrases in 3 severity tiers.
- `references/style-guide.md` — how to write like a human (opinions, rhythm, specificity).
- `references/stealth-writing.md` — anti-attribution / persona / anonymous writing.
- `references/media/forum.md` — forum-writing craft; loaded when medium=forum.
- `references/voice-dimensions-TEMPLATE.md` — fallback voice; never populate with user data.
- `references/exemplars/README.md` — onboarding for building the voice overlay.

User overlay (private, outside the repo): `${overlay}/voice-dimensions.md` and
`${overlay}/exemplars/{index,social,professional,longform}.md`.
