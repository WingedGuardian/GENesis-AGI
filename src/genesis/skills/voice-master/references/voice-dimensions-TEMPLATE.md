# Voice Dimensions (TEMPLATE)

> This is a template. The active voice profile is loaded from a user overlay
> at `${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master/}voice-dimensions.md`.
>
> If the overlay exists, voice-master uses **that** file instead of this
> template. If no overlay is present, voice-master falls back to this
> template and emits a warning — the skill still runs, but output will be
> generic.
>
> **Do not populate this file with personal voice data.** Run the calibration
> workflow (see SKILL.md → Standalone Modes → Calibrate) to build an overlay.
> The overlay lives outside the repo so personal voice data never enters
> version control.

## Purpose of Voice Dimensions

Supplementary voice rules for edge cases the exemplars don't cover.

**When exemplars conflict with these rules, the exemplars win.** Exemplars are
the primary source of truth — they show what the user actually sounds like.
These dimensions are fallback guidance.

---

## Tone

<!-- Describe the user's natural tone. Examples: direct, conversational,
     technically grounded, formal, casual, playful, reserved. One sentence
     is fine. Two or three is better. -->

## Sentence Structure

<!-- How does the user naturally structure sentences? Short and punchy?
     Long and flowing? Mix of both? Do they use fragments for emphasis?
     Do they start sentences with conjunctions? Are run-ons common? -->

## Vocabulary

<!-- What's the user's vocabulary register? Industry jargon or plain language?
     Formal or casual? Any specific words, phrases, or idioms they gravitate
     toward? Any words they'd never use? -->

## Perspective

<!-- How does the user frame ideas? First-person experience? Third-person
     analysis? Do they take strong positions or acknowledge nuance? Do they
     challenge popular opinions or defer to consensus? -->

## Humor

<!-- What's the user's humor style? Dry, self-deprecating, absent, sarcastic,
     playful, wordplay-based? How often does it show up — everywhere, rarely,
     only in certain contexts? -->

---

## How to Build a Real Voice Profile (for new users)

This template produces generic output. To get voice-master to write like you:

1. Run a calibration session (see SKILL.md → Standalone Modes → Calibrate).
2. During calibration, the skill will generate content, you edit it to sound
   right, and the diff informs the voice profile.
3. Voice insights you confirm during calibration are written to your **overlay**
   directory (`~/.claude/skills/voice-master/voice-dimensions.md`), not this
   file. The overlay lives outside the repo.
4. After calibration, the overlay is automatically loaded on every skill
   invocation. This template stops being used.

You can also hand-edit the overlay file directly if you already know how you
sound and want to skip calibration. Use the section structure above.
