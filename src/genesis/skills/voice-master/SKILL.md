---
name: voice-master
description: >
  Foundational voice authority and AI humanizer — writes content in user's
  authentic voice with built-in AI detection, and supports stealth / anti-
  attribution writing (forum personas, anonymous posts, "write as not-me").
  Use when asked to write/draft/generate content, invoke /voice or /write-as-me
  or /humanize, run voice calibration, check "does this sound like me?", "make
  this sound human" / "de-AI this", "write a forum post as [persona]", or run
  AI detection ("does this sound like AI?", "check for AI patterns", "anti-slop
  check"). The anti-slop filter and ai-vocabulary.md function as an AI content
  detector — scanning for 290+ known AI writing patterns, statistical tells,
  and vocabulary signals across 3 severity tiers, now medium-scoped (universal
  / professional / forum / long-form).
consumer: cc_foreground
phase: content
skill_type: generation
---

# Voice Master (AI Humanizer + Stealth Writer)

## Purpose

Write content that sounds authentically like the user — across any medium, for
any audience. The bar: a reader who knows the user cannot distinguish this from
something the user wrote themselves.

This skill is the voice authority. All downstream content skills (LinkedIn,
email, blog, etc.) reference this skill's overlay-loader for voice loading and
its anti-slop rules for AI detection.

When writing as someone other than the user — forum personas, anonymous posts,
or intentionally genericized output — this skill's **stealth-writing mode**
neutralizes the user's voice fingerprints and layers a persona or anonymous
register on top.

---

## User Calibration Overlay (MANDATORY — READ THIS FIRST)

Voice-master separates **skill machinery** (rules, anti-slop, style guidance,
this instructions file) from **user data** (exemplars, voice-dimensions). The
machinery ships in the repo; the user data lives outside the repo so personal
writing samples never enter version control.

**Overlay default location**: `~/.claude/skills/voice-master/` (no trailing
slash when concatenating; see the resolution procedure below). Override via
the `GENESIS_VOICE_OVERLAY` environment variable.

```
<overlay_root>/
├── voice-dimensions.md      # user's actual voice description
└── exemplars/
    ├── index.md
    ├── social.md
    ├── professional.md
    └── longform.md
```

### Overlay Resolution Procedure (follow these steps EXACTLY)

Do NOT mentally expand shell parameter substitution. The Read tool does not
perform shell expansion on its input. You MUST resolve the overlay path
using Bash first, then pass the resolved absolute path to the Read tool.

**Step 1 — Resolve the overlay root.** Run this via the Bash tool:

```bash
echo "${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master}"
```

Capture the output. That is `<overlay_root>` for the rest of this session.
Strip any trailing slash if present.

**Step 2 — Load voice-dimensions.** Try to Read `<overlay_root>/voice-dimensions.md`
with the Read tool (absolute path, no shell syntax).

- **If Read succeeds**: this is the active voice profile. Use it as the
  authoritative description of how the user sounds.
- **If Read fails** (file not found or other error): fall back to
  `references/voice-dimensions-TEMPLATE.md` in this skill directory. The
  template is generic — your output will not match any specific user voice.
  Begin your reply with a prominent warning on its own line:

  ```
  WARNING: No voice overlay detected. Generating with generic template voice. Run the voice-master calibration workflow to build a profile.
  ```

  The warning is MANDATORY in the fallback path. Do not skip it. Do not bury
  it mid-paragraph. The user needs to see this before reading any generated
  content, because the content is not in their voice.

**Step 3 — Load the exemplar index.** Try to Read `<overlay_root>/exemplars/index.md`.

- **If Read succeeds**: this is the active exemplar registry. Use it to pick
  3-5 best matches for the request, then Read the appropriate exemplar files
  from `<overlay_root>/exemplars/{social,professional,longform}.md` as needed.
- **If Read fails**: there are no calibrated exemplars. Read
  `references/exemplars/README.md` in this skill directory for the onboarding
  instructions. Proceed with voice-dimensions guidance only. Note in your
  reply that no exemplars were loaded — the warning from Step 2 covers this
  if voice-dimensions also fell back, otherwise add a second line:

  ```
  WARNING: No exemplars in overlay — generation is based on voice-dimensions only.
  ```

**Step 4 — Never cache overlay state across invocations.** Repeat Steps 1-3
on every generation request. The overlay can be updated between calls
(e.g., the user ran calibration and added new exemplars).

### Overlay Hygiene Rules

- **Do not write personal user data into the in-repo files.** The in-repo
  `voice-dimensions-TEMPLATE.md` and `exemplars/README.md` are generic
  template scaffolding. Real writing samples and voice profiles go only
  in `<overlay_root>/`.
- **Do not mention the fallback warning if the overlay loaded successfully.**
  The warning exists to surface a missing-overlay state, not as boilerplate.
- **If the user asks you to write to the overlay** (e.g., during a
  calibration session), write to the resolved `<overlay_root>/` path, not
  to the in-repo template files.

---

## Voice Loading Pipeline (MANDATORY BEFORE GENERATION)

BEFORE generating any content, you MUST read the following files using the Read
tool. Do not skip this step. Do not generate from memory.

1. Follow the **User Calibration Overlay** resolution above to load either
   (a) the overlay's voice-dimensions + exemplars, or (b) the template fallback
   with a warning.
2. From whichever exemplar index is active, identify 3-5 exemplars whose tone,
   formality, and domain best match the current request.
3. Read the appropriate exemplar file(s) to get the actual exemplar text.
4. Read `references/anti-slop.md` for patterns to avoid. Scan **only the
   sections matching the current medium** (see Medium Detection below) plus
   the **Universal** section. Do not apply professional/LinkedIn rules to a
   forum post or vice versa — they contradict.
5. Scan `references/ai-vocabulary.md` for Tier 1 dead-giveaway words to ban.

**Precedence rule:** Exemplars always take precedence over voice-dimensions.
Exemplars show what the user actually sounds like. Voice dimensions are
supplementary guidance for edge cases the exemplars don't cover.

**If exemplar files are empty or have no good matches:**
1. Use cross-medium exemplars that match on tone and domain. Note in output:
   "Used cross-medium exemplars — consider a calibration session for [medium]."
2. If no exemplars match at all, use voice-dimensions as your only voice
   guidance.
3. At minimum, always apply the anti-slop filter. Never generate content with
   zero voice constraint.

---

## Medium Detection

Voice-master explicitly models **medium** as a first-class axis of voice.
Different mediums have different native registers, and anti-slop rules that
catch AI-sounding content in one medium actively miss-fire in another.

Medium options:

- **social** — short, public posts (Twitter/X, LinkedIn feed, short posts)
- **professional** — email, Slack DMs, work documents, LinkedIn messages,
  anything where the user is identified and the stakes are reputational
- **forum** — long-running threaded discussions on hobbyist/community sites
  (Reddit, Discourse, phpBB-style forums, HN comments). Forum register is
  casual, fragment-heavy, idiom-rich, and allergic to the structured
  polish that wins on LinkedIn.
- **longform** — blog posts, essays, long-form articles, substack pieces

Detection precedence:
1. The request explicitly names a medium — use it.
2. The request names a platform (e.g., "LinkedIn post") — map it to medium.
3. Infer from the request shape (length, audience, formatting) — default to
   `professional` if ambiguous.

The selected medium determines which section of `references/anti-slop.md`
applies and whether `references/media/forum.md` is loaded (forum only).

---

## Standalone Modes

Voice-master has two orthogonal mode dimensions:

1. **What the user asks the skill to do**: generate, calibrate, analyze, curate.
2. **Whose voice to produce**: user's actual voice (default) or stealth voice
   (user's voice fingerprints neutralized, optionally layered with a persona).

These combine: you can generate in the user's voice, generate in stealth mode,
analyze a piece of text for whose voice it matches, etc.

### Generate (default mode)

Write content directly when no medium-specific skill applies. Follow the
Voice Loading Pipeline above, then the Core Pipeline below. Output the final
content.

### Calibrate

Run an edit-and-learn session to refine the user's voice profile:

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
9. Encode the insight — write it to the **overlay** directory
   (`${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master/}exemplars/` or
   `voice-dimensions.md`). **Never** to the in-repo template files.
10. Repeat. Initial session: ~10 rounds (20-30 minutes). Tune-ups: 2-3 rounds.

### Analyze

"Does this text sound like me?" Compare provided text against the active
(overlay or fallback) exemplars:
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
- Tag accepted exemplars with medium, tone, formality, domain
- Update the appropriate exemplar file and index **in the overlay directory**

---

## Stealth Mode (Anti-Attribution Writing)

Use stealth mode when the output should **not** be attributable to the user.
Canonical use cases: forum personas, anonymous posts, sock-puppet/test
accounts, intentionally genericized writing that can't be traced.

### Activating Stealth Mode

Stealth mode is activated by the request. Triggers:
- Explicit: "stealth mode", "anti-attribution", "anonymous", "write as [persona]"
- Implicit: the request names a persona directory under `~/.claude/personas/`
- Implicit: the request targets a forum/platform where the user is not
  self-identifying (e.g., "post on this forum as a regular")

When stealth mode is active, **load** `references/stealth-writing.md` in
addition to the normal voice loading pipeline. Follow its protocol.

### Three Stealth Sub-Modes

1. **Anonymous** — no persona, just not-the-user. The output should be
   unrecognizable as the user's voice but has no other positive target.
2. **Persona** — writing AS a defined character from
   `~/.claude/personas/<persona_name>/`. The persona directory provides a
   positive voice target to replace the user's voice with.
3. **Impersonation** — mimicking a specific named voice. **Hard limits
   apply**: see stealth-writing.md for the full list. Never impersonate a
   named living public figure in adversarial/defamatory contexts.

### Stealth Mode Pipeline (abbreviated; full version in stealth-writing.md)

1. Load user's voice profile via the Calibration Overlay resolution — this
   identifies the **fingerprints to neutralize**, not the voice to produce.
2. If a persona is specified, load the persona directory:
   `~/.claude/personas/<name>/persona.md` (identity, constraints, backstory)
   and `~/.claude/personas/<name>/exemplars.md` (style targets).
3. Generate candidates with the persona's voice (or generic anonymous voice),
   actively avoiding the user's fingerprints.
4. Run the **adversarial critic** protocol (separate LLM call, no generation
   context, scores 0-10 on plausibility as the intended voice).
5. If best candidate scores ≥7, select it. Otherwise regenerate once; if
   still below threshold, surface to the user.
6. Log the result in `~/.genesis/personas.db` (persona mode only).

Full protocol in `references/stealth-writing.md`.

---

## Core Pipeline

When generating content in the **user's** voice (default, non-stealth):

1. **Read the request** — topic, target medium, tone, audience, constraints.
2. **Detect the medium** — see Medium Detection above. This selects the
   anti-slop section to apply and whether to load `references/media/forum.md`.
3. **Load voice** — follow the User Calibration Overlay resolution and the
   Voice Loading Pipeline.
4. **Pick exemplars** — from the overlay (or template fallback), select 3-5
   best matches by medium + tone + formality + domain.
5. **Generate content** — use selected exemplars as stylistic reference.
   Match sentence structure, vocabulary level, directness, and rough edges.
   Do NOT copy exemplar content — match the style.
6. **Hand off to medium skill** — if a downstream skill exists for this medium
   (e.g., linkedin-post-writer for LinkedIn), hand off the voice-loaded content
   for medium-specific formatting.
7. **Anti-slop filter** — review output against the three checks in
   `references/anti-slop.md`, scoped to the current medium. If it fails,
   revise with specific feedback. Maximum 2 revision passes. After that,
   surface the best version with remaining flags noted.

When generating in **stealth mode**, follow the Stealth Mode Pipeline above.

---

## Anti-Slop Filter

Applied as the final step of every content generation. Three checks, now
medium-scoped:

1. **Pattern matching** — scan for known AI writing tells in
   `references/anti-slop.md`. Scan the **Universal** section always, plus the
   section matching the current medium (**Professional**, **Forum**, or
   **Long-form**). Any match in an applicable section is a failure.
2. **Voice consistency** — compare against selected exemplars. Does sentence
   length vary naturally? Does vocabulary match the user's register? Is the
   perspective personal or abstract? Does it have opinions or hedge everything?
3. **Specificity check** — does the content contain at least one concrete
   detail, specific reference, or personal experience that could NOT have
   been generated about any topic by any person? If you removed the topic
   keywords, would it be indistinguishable from a template? If yes, fail.

**On failure:** don't just flag — revise. Send specific feedback: "Paragraph 2
uses 'In today's landscape' — rephrase with a specific observation."

**Revision limit:** maximum 2 passes. If still failing, surface the best
version with remaining flags noted for the user.

In stealth mode, the anti-slop filter is augmented by the adversarial critic
from `references/stealth-writing.md`.

---

## References

All reference files are in this skill's `references/` directory. User-specific
data (exemplars, voice-dimensions) is loaded from the **overlay** at
`${GENESIS_VOICE_OVERLAY:-$HOME/.claude/skills/voice-master/}`; see User
Calibration Overlay above.

**In-repo machinery (generic, shipped in the public template):**

- `references/anti-slop.md` — Medium-scoped AI-tell patterns to avoid
  (Universal / Professional / Forum / Long-form). Three-check framework.
- `references/ai-vocabulary.md` — 290+ AI words/phrases in 3 severity tiers
  with alternatives. Medium-agnostic (Tier 1 words are bad everywhere).
- `references/style-guide.md` — How to write like a human (opinions, rhythm,
  specificity).
- `references/stealth-writing.md` — Anti-attribution / persona / anonymous
  writing. Used in stealth mode.
- `references/media/forum.md` — Positive forum-writing craft (reply-culture
  norms, pacing, vibe-matching). Loaded when medium=forum.
- `references/voice-dimensions-TEMPLATE.md` — Fallback voice guidance used
  when no overlay is present. Generic; do not populate with user data.
- `references/exemplars/README.md` — Onboarding doc for building a voice
  overlay. The overlay itself lives outside the repo.

**User overlay (private, outside the repo):**

- `${overlay}/voice-dimensions.md` — User's actual voice description.
- `${overlay}/exemplars/index.md` — Exemplar registry.
- `${overlay}/exemplars/social.md` — Social media voice exemplars.
- `${overlay}/exemplars/professional.md` — Professional comms exemplars.
- `${overlay}/exemplars/longform.md` — Long-form writing exemplars.
